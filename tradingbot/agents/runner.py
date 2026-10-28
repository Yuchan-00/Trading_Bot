"""에이전트 러너 (SPEC §20.4, v2.1).

- ``system_prompt(role)`` = ``prompts/<role>.md`` 원문 + ``"\\n\\n# Skills\\n"`` + ``skills.render(role, max_tokens=24000)``.
  처음 한 번 디스크에서 읽어 메모리에 캐시하고, 이후에는 **바이트 단위로 같은** 문자열을 돌려준다
  (Anthropic 프롬프트 캐시 적중을 위해 사이클 번호·날짜 같은 가변 정보는 절대 넣지 않는다).
- ``run()`` 은 백엔드를 한 번 호출한다. ``BudgetExceeded`` 는 그대로 전파하고, 그 밖의 백엔드 예외는
  ``AgentResult(ok=False, error="exception:<타입>")`` 로 바꿔 돌려준다(사이클을 죽이지 않는다 = 기권).
  예외 메시지는 비밀값이 섞일 수 있어 결과/로그에 싣지 않는다(타입 이름만).
  단, 비용 원장(``tradingbot/llm/cost.py``) 기록 실패에서 나온 ``OSError`` 는 상태 저장 실패이므로
  기권으로 바꾸지 않고 전파한다(SPEC §0: 상태 저장 실패는 멈춘다).
- ``ok=False`` 결과의 ``output`` 은 항상 ``None`` 으로 정리한다(실패 = 기권, 신호로 읽히면 안 된다).
- 백엔드가 비용 원장(``backend.ledger``)을 갖고 있으면 호출 전에 예상 비용을 **예약**한다.
  병렬 호출이 모두 같은 ``spent_today()`` 로 검사를 통과해 일일 하드 예산을 넘기는 경합을 막는다.
- ``run_many()`` 는 스레드풀로 병렬 실행하고 입력 순서를 보존한다. ``BudgetExceeded`` 가 한 번이라도
  나면 아직 시작하지 않은 호출은 백엔드로 보내지 않는다(DESIGN §2: 예산 초과 시 LLM 중단).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ValidationError

from tradingbot.agents.roles import ROLES, RoleSpec, get_role
from tradingbot.config import Settings
from tradingbot.llm.backend import AgentBackend, AgentResult
from tradingbot.llm.cost import BudgetExceeded
from tradingbot.skills import SkillRegistry

log = logging.getLogger("tradingbot.agents.runner")

SKILLS_HEADER = "\n\n# Skills\n"
SKILLS_MAX_TOKENS = 24000  # SPEC v2.1: 역할 배정 스킬이 잘리지 않게

# 예약 시 예상 비용에 곱하는 안전 배수. 예상치(문자수/4 + max_tokens)는 실제 입력 토큰을 과소평가할 수 있어,
# 예약 합계가 예산에 가까워지면 진행 중 호출이 실제 비용을 기록할 때까지 기다린 뒤 실제 합계로 다시 검사한다.
RESERVATION_SAFETY = 2.0

_COST_MODULE_FILE = Path(__import__("tradingbot.llm.cost", fromlist=["_"]).__file__).resolve()


def _raised_from_cost_ledger(exc: BaseException) -> bool:
    """예외가 비용 원장 모듈(cost.py) 안에서 발생했는지(= 원장 상태 저장 실패) 트레이스백으로 판별한다."""
    tb: TracebackType | None = exc.__traceback__
    while tb is not None:
        try:
            if Path(tb.tb_frame.f_code.co_filename).resolve() == _COST_MODULE_FILE:
                return True
        except (OSError, ValueError):
            pass
        tb = tb.tb_next
    return False


class AgentRunner:
    """역할별 시스템 프롬프트를 조립하고 백엔드 호출을 감싼다."""

    def __init__(
        self,
        settings: Settings,
        backend: AgentBackend,
        skills: SkillRegistry,
        prompts_dir: Path,
        *,
        max_workers: int = 6,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 는 1 이상이어야 합니다")
        self.settings = settings
        self.backend = backend
        self.skills = skills
        self.prompts_dir = Path(prompts_dir)
        self.max_workers = int(max_workers)
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        # 예산 예약(병렬 호출 간 check→record 경합 방지)
        self._budget_cond = threading.Condition(threading.Lock())
        self._reserved_usd = 0.0
        self._inflight = 0

    # ------------------------------------------------------------ 시스템 프롬프트
    def prompt_path(self, role_id: str) -> Path:
        """역할의 프롬프트 파일 경로: ``prompts_dir/<RoleSpec.prompt_file 의 파일명>``."""
        spec = get_role(role_id)
        return self.prompts_dir / Path(spec.prompt_file).name

    def _build_system_prompt(self, role_id: str) -> str:
        spec = get_role(role_id)
        path = self.prompt_path(role_id)
        if not path.is_file():
            raise FileNotFoundError(
                f"역할 {role_id!r} 의 프롬프트 파일이 없습니다: {path} "
                f"(prompts 디렉터리에 {Path(spec.prompt_file).name} 를 만들어 주세요)"
            )
        # newline="" 로 읽어 줄바꿈을 원문 그대로 유지(바이트 고정)
        with path.open("r", encoding="utf-8", newline="") as fh:
            prompt = fh.read()
        self._check_skill_assignment(spec)
        rendered = self.skills.render(role_id, max_tokens=SKILLS_MAX_TOKENS)
        return prompt + SKILLS_HEADER + rendered

    def _check_skill_assignment(self, spec: RoleSpec) -> None:
        """RoleSpec.skills 와 SKILL.md frontmatter 의 roles 배정이 어긋나면 WARNING 만 남긴다."""
        assigned = {s.name for s in self.skills.for_role(spec.role_id)}
        expected = set(spec.skills)
        missing = sorted(expected - assigned)
        extra = sorted(assigned - expected)
        if missing:
            log.warning("역할 %r: 배정 스킬 누락(로드 안 됨 또는 roles 미포함) %s", spec.role_id, ", ".join(missing))
        if extra:
            log.warning("역할 %r: RoleSpec 에 없는 스킬이 렌더됨 %s", spec.role_id, ", ".join(extra))

    def system_prompt(self, role_id: str) -> str:
        """역할 시스템 프롬프트. 첫 호출에 조립해 캐시하고 이후 같은 객체를 돌려준다."""
        cached = self._cache.get(role_id)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(role_id)
            if cached is None:
                cached = self._build_system_prompt(role_id)
                self._cache[role_id] = cached
                log.info("시스템 프롬프트 캐시 role=%s chars=%d", role_id, len(cached))
            return cached

    def clear_cache(self) -> None:
        """프롬프트/스킬 파일을 바꾼 뒤 다시 읽고 싶을 때(운영 중에는 쓰지 않는다)."""
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------ 호출
    @staticmethod
    def _coerce_output(result: AgentResult, output_model: type[BaseModel]) -> AgentResult:
        """백엔드가 dict 나 다른 타입을 돌려준 경우를 정리한다(타입 불일치 = parse_error)."""
        if not result.ok:
            if result.output is not None:
                return result.model_copy(update={"output": None})
            return result
        out = result.output
        if isinstance(out, output_model):
            return result
        if isinstance(out, dict):
            try:
                parsed = output_model.model_validate(out)
            except ValidationError:
                return result.model_copy(update={"ok": False, "output": None, "error": "parse_error"})
            return result.model_copy(update={"output": parsed})
        return result.model_copy(update={"ok": False, "output": None, "error": "parse_error"})

    def _reserve_budget(self, spec: RoleSpec, system: str, payload: dict) -> float | None:
        """백엔드에 비용 원장이 있으면 예상 비용을 예약한다. 예약 포함 초과면 ``BudgetExceeded``.

        반환값은 예약한 금액(원장이 없거나 추정 실패면 ``None`` = 예약 없음). 호출이 끝나면(성공·실패 무관) ``_release_budget`` 로 푼다.
        백엔드가 응답 직후 ``ledger.record`` 로 실제 비용을 기록하므로, 해제 시점에는 실제 비용이 이미 합계에 들어 있다.
        """
        ledger = getattr(self.backend, "ledger", None)
        if ledger is None or not callable(getattr(ledger, "check", None)) or not callable(getattr(ledger, "estimate", None)):
            return None
        try:
            model_name = self.settings.model_for(spec.tier)
            user_text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
            projected = float(ledger.estimate(model_name, len(system) // 4 + len(user_text) // 4, 0, spec.max_tokens))
        except Exception as exc:  # noqa: BLE001 - 추정 실패 시 예약 없이 백엔드 자체 검사에 맡긴다
            log.warning("예산 예약 추정 실패 role=%s type=%s", spec.role_id, type(exc).__name__)
            return None
        amount = max(0.0, projected) * RESERVATION_SAFETY
        with self._budget_cond:
            while True:
                if self._inflight == 0:
                    # 진행 중 호출이 없으면 실제 합계 기준(= 순차 실행과 같은 판정)
                    ledger.check(amount / RESERVATION_SAFETY)  # BudgetExceeded 전파
                    break
                try:
                    ledger.check(self._reserved_usd + amount)
                    break
                except BudgetExceeded:
                    pass
                # 예약 포함 초과 → 진행 중 호출이 실제 비용을 기록할 때까지 대기
                self._budget_cond.wait()
            self._reserved_usd += amount
            self._inflight += 1
        return amount

    def _release_budget(self, amount: float | None) -> None:
        if amount is None:
            return
        with self._budget_cond:
            self._reserved_usd = max(0.0, self._reserved_usd - amount)
            self._inflight = max(0, self._inflight - 1)
            self._budget_cond.notify_all()

    def run(self, role_id: str, payload: dict, *, output_model: type[BaseModel] | None = None) -> AgentResult:
        """한 역할을 한 번 호출한다. ``BudgetExceeded`` 와 비용 원장 기록 실패(OSError)만 예외로 전파한다.

        알 수 없는 역할(KeyError)·프롬프트 파일 누락(FileNotFoundError)은 설정 오류이므로 전파한다.
        """
        spec = get_role(role_id)
        model = output_model or spec.output_model
        system = self.system_prompt(role_id)
        started = time.monotonic()
        try:
            reserved = self._reserve_budget(spec, system, payload)
        except BudgetExceeded:
            log.warning("예산 초과(예약 포함) role=%s — 호출 중단", role_id)
            raise
        try:
            result = self.backend.complete(
                role=role_id,
                tier=spec.tier,
                system=system,
                user_payload=payload,
                output_model=model,
                max_tokens=spec.max_tokens,
                effort=spec.effort,
                timeout_s=spec.timeout_s,
            )
        except BudgetExceeded:
            log.warning("예산 초과 role=%s — 호출 중단", role_id)
            raise
        except OSError as exc:
            if _raised_from_cost_ledger(exc):
                # SPEC §0: 상태 저장(비용 원장) 실패는 사이클을 멈춘다 — 기권으로 삼키지 않는다
                log.critical("비용 원장 기록 실패 role=%s type=%s — 중단", role_id, type(exc).__name__)
                raise
            elapsed = time.monotonic() - started
            log.error("백엔드 예외 role=%s type=%s (기권 처리)", role_id, type(exc).__name__)
            return AgentResult(role=role_id, ok=False, output=None, error=f"exception:{type(exc).__name__}", latency_s=elapsed)
        except Exception as exc:  # noqa: BLE001 - 어떤 백엔드 오류도 기권으로 변환
            elapsed = time.monotonic() - started
            # 메시지에는 비밀값이 섞일 수 있으므로 타입 이름만 남긴다
            log.error("백엔드 예외 role=%s type=%s (기권 처리)", role_id, type(exc).__name__)
            return AgentResult(role=role_id, ok=False, output=None, error=f"exception:{type(exc).__name__}", latency_s=elapsed)
        finally:
            self._release_budget(reserved)
        if not isinstance(result, AgentResult):
            log.error("백엔드가 AgentResult 가 아닌 값을 반환 role=%s type=%s", role_id, type(result).__name__)
            return AgentResult(
                role=role_id, ok=False, output=None, error="bad_backend_result", latency_s=time.monotonic() - started
            )
        result = self._coerce_output(result, model)
        if result.latency_s == 0.0:
            result = result.model_copy(update={"latency_s": time.monotonic() - started})
        if result.ok:
            log.info(
                "역할 완료 role=%s model=%s in=%d cached=%d out=%d cost=%.4f",
                role_id,
                result.model,
                result.input_tokens,
                result.cached_tokens,
                result.output_tokens,
                result.cost_usd,
            )
        else:
            log.warning("역할 기권 role=%s error=%s", role_id, result.error)
        return result

    def run_many(self, calls: list[tuple[Any, ...]]) -> list[AgentResult]:
        """여러 호출을 스레드풀로 병렬 실행하고 **입력 순서대로** 결과를 돌려준다.

        각 항목은 ``(role_id, payload)`` (선택적으로 세 번째 원소로 output_model).
        하나라도 ``BudgetExceeded`` 를 던지면 워커 수를 넘어 **대기열에 있던** 호출(인덱스 >= 워커 수)은
        건너뛰고(백엔드 미호출), 동시에 출발한 호출이 끝난 뒤 그 예외를 다시 던진다. 원장 기록 실패(OSError)도 같은 방식으로 전파한다.
        """
        if not calls:
            return []
        items: list[tuple[str, dict, type[BaseModel] | None]] = []
        for call in calls:
            if len(call) == 2:
                role_id, payload = call  # type: ignore[misc]
                model = None
            elif len(call) == 3:
                role_id, payload, model = call  # type: ignore[misc]
            else:
                raise ValueError(f"run_many 항목은 (role_id, payload[, output_model]) 이어야 합니다: 길이 {len(call)}")
            get_role(role_id)  # 알 수 없는 역할은 병렬 실행 전에 실패
            items.append((role_id, payload, model))
        # 시스템 프롬프트를 먼저(직렬로) 조립: 파일 누락은 여기서 바로 드러나고, 스레드 간 경합도 없다
        for role_id in dict.fromkeys(r for r, _, _ in items):
            self.system_prompt(role_id)

        workers = min(self.max_workers, len(items))
        stop = threading.Event()
        first_exc: list[BaseException] = []
        exc_lock = threading.Lock()

        def _task(idx: int, role_id: str, payload: dict, model: type[BaseModel] | None) -> AgentResult | None:
            # 첫 워커 수만큼은 동시에 출발한 호출(이미 진행 중으로 본다), 나머지는 대기열 → 중단 신호면 생략
            if idx >= workers and stop.is_set():
                log.info("중단 신호 이후 호출 생략 role=%s", role_id)
                return None
            try:
                return self.run(role_id, payload, output_model=model)
            except (BudgetExceeded, OSError) as exc:
                # run() 은 OSError 를 원장 기록 실패일 때만 전파한다
                with exc_lock:
                    if not first_exc:
                        first_exc.append(exc)
                stop.set()
                return None

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent") as pool:
            futures = [pool.submit(_task, i, r, p, m) for i, (r, p, m) in enumerate(items)]
            results = [fut.result() for fut in futures]
        if first_exc:
            raise first_exc[0]
        return [r for r in results if r is not None]

    def __repr__(self) -> str:
        return f"AgentRunner(prompts_dir={str(self.prompts_dir)!r}, roles={sorted(ROLES)}, max_workers={self.max_workers})"
