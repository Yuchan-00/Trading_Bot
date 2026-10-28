"""Claude API 백엔드 (SPEC §8.3).

구조화 출력은 ``client.messages.create(output_config={"format": {json_schema}})``로 받고 본문 JSON을
``output_model.model_validate_json``으로 직접 검증한다.

SPEC §8.3 (2)는 ``client.messages.parse()``를 예시로 들지만, 실제 SDK(anthropic 2.x)의 ``parse()``는
``stop_reason=max_tokens``(잘린 JSON)·``refusal``(거부 문구)처럼 본문이 스키마에 맞지 않으면 HTTP 응답을
받은 *뒤* ``pydantic.ValidationError``를 던져 ``usage``/``stop_reason``에 접근할 길이 없다. 그러면 SPEC §8.3
(3)의 ``max_tokens``/``refusal:<category>`` 매핑과 (5)의 "usage는 항상 기록"을 지킬 수 없으므로, 원본
``Message``를 돌려주는 ``create()``를 쓴다(리뷰 LLM-1/LLM-2).

- 호출 전 ``ledger.check(예상 비용)`` → ``BudgetExceeded``는 그대로 전파(호출자가 LLM 일시정지).
  예상 비용은 최악값으로 잡는다(``project_call_cost``): system 블록은 콜드 캐시의 캐시 쓰기 단가
  (input × ``CACHE_WRITE_MULTIPLIER[ttl]``, 1h = 2x)로, 문자수/4 토큰 추정에는 토크나이저 여유
  (``TOKENIZER_MARGIN``)를 곱한다. 1x input 으로 잡으면 check 를 통과한 호출이 하드 일일 예산을 넘긴다(CLAUDE-API-3).
- 재시도는 SDK 가 아니라 백엔드가 한다(SDK client 는 ``max_retries=0``으로 만든다). SDK 자동 재시도는 매 시도에
  역할별 ``timeout_s`` 를 통째로 다시 주므로 순차 4단계 파이프라인의 최악 LLM 시간이 (35+60+75+100)×3 = 810s 로
  5분 사이클 기한(300s)을 크게 넘는다(CLAUDE-API-1). 백엔드는 한 호출의 총 벽시계 시간을 ``timeout_s`` 안에 묶는다:
  타임아웃이 난 시도는 시간 창을 다 쓴 것이므로 재시도하지 않고, 빠르게 실패한 재시도 가능 오류(429·408·409·5xx·연결)만
  남은 시간 안에서 최대 ``DEFAULT_MAX_RETRIES`` 회 다시 보내며 각 재시도의 timeout 은 남은 시간이다.
  ``set_deadline(remaining_s)`` 로 사이클 기한을 알려 주면 호출 시간은 그 기한도 넘지 않고, 기한이 지났으면
  API 를 부르지 않고 ``error="deadline"`` 으로 기권한다. 주입된 ``client`` 는 자체 재시도 정책을 가진 것으로 보고
  백엔드 재시도를 하지 않는다(한 번 호출).
- system 블록은 ``cache_control={"type":"ephemeral","ttl":cache_ttl}``로 캐시한다. 역할별 system은
  바이트 단위로 고정해야 하며(날짜·사이클·심볼 금지) 변동 데이터는 전부 ``user_payload``로 보낸다.
- ``thinking``·``tool_choice``는 보내지 않는다(5.5 계열은 기본 adaptive).
- 모든 예외/거부는 ``ok=False`` + 짧은 ``error`` 코드로 보고하고, 어떤 로그·에러 문자열에도
  API 키가 들어가지 않는다(``config.mask_secret``).
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

try:  # SDK 의 parse() 가 쓰는 스키마 변환기(구조화 출력 제약에 맞게 additionalProperties 등 정리)
    from anthropic.lib._parse._transform import transform_schema as _transform_schema
except Exception:  # noqa: BLE001 - 비공개 모듈이므로 없으면 원본 스키마 사용
    _transform_schema = None  # type: ignore[assignment]

from tradingbot.config import ConfigError, Settings, mask_secret
from tradingbot.llm.backend import AgentResult, Effort, Tier
from tradingbot.llm.cost import (  # noqa: F401 - TOKENIZER_MARGIN/project_call_cost 는 여기서도 재노출(호환)
    CACHE_WRITE_MULTIPLIER,
    TOKENIZER_MARGIN,
    BudgetExceeded,
    CostLedger,
    project_call_cost,
)

logger = logging.getLogger("tradingbot.llm.anthropic_backend")

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RETRIES = 2  # 백엔드 자체 재시도 횟수(시간 예산 안에서만). SDK client 는 max_retries=0.
SDK_CLIENT_MAX_RETRIES = 0
# 재시도 백오프(SDK 기본과 같은 0.5s 시작·8s 상한·지터). retry-after 헤더가 있으면 그 값을 따르되 남은 시간 안에서만.
RETRY_INITIAL_DELAY_S = 0.5
RETRY_MAX_DELAY_S = 8.0
# 재시도 한 번에 최소한 남아 있어야 하는 시간. 이보다 적으면 다시 보내지 않는다.
MIN_ATTEMPT_S = 5.0
_LOG_SNIPPET = 200


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _output_format(output_model: type[BaseModel]) -> dict[str, Any]:
    """``output_config.format`` 값: SDK ``parse()`` 와 같은 json_schema 변환."""
    schema = output_model.model_json_schema()
    if _transform_schema is not None:
        try:
            schema = _transform_schema(schema)
        except Exception:  # noqa: BLE001 - 변환 실패 시 원본 스키마
            pass
    return {"type": "json_schema", "schema": schema}


class AnthropicBackend:
    """``AgentBackend`` 구현. 테스트는 ``client``에 가짜 객체(``.messages.create``)를 주입한다."""

    def __init__(
        self,
        settings: Settings,
        ledger: CostLedger,
        *,
        client: anthropic.Anthropic | None = None,
        cache_ttl: Literal["5m", "1h"] = "1h",
    ) -> None:
        if cache_ttl not in ("5m", "1h"):
            raise ValueError(f"cache_ttl은 5m/1h 중 하나: {cache_ttl!r}")
        self.settings = settings
        self.ledger = ledger
        self.cache_ttl: Literal["5m", "1h"] = cache_ttl
        self._secret = settings.anthropic_api_key or ""
        # 백엔드 자체 재시도: 직접 만든 client 에만(주입 client 는 자기 재시도 정책을 가진다)
        self.max_retries = 0 if client is not None else DEFAULT_MAX_RETRIES
        if client is None:
            if not settings.anthropic_api_key:
                raise ConfigError("AnthropicBackend 에는 ANTHROPIC_API_KEY 가 필요합니다")
            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                max_retries=SDK_CLIENT_MAX_RETRIES,  # 재시도는 시간 예산을 아는 백엔드가 한다(CLAUDE-API-1)
                timeout=DEFAULT_TIMEOUT_S,
            )
        self.client = client
        self._deadline_lock = threading.Lock()
        self._deadline_at: float | None = None  # time.monotonic() 기준 사이클 기한(없으면 None)
        ledger_ttl = getattr(ledger, "cache_ttl", None)
        if ledger_ttl is not None and ledger_ttl != cache_ttl:
            logger.warning("ledger.cache_ttl=%s 와 backend cache_ttl=%s 가 다름: 캐시 쓰기 비용이 어긋날 수 있음", ledger_ttl, cache_ttl)

    # ------------------------------------------------------------------ 기한
    def set_deadline(self, remaining_s: float | None) -> None:
        """사이클 기한까지 남은 초를 알려 준다(``None`` = 해제). 이후 호출은 이 기한을 넘지 않는다.

        초 단위 상대값으로 받으므로 호출자의 시계(가짜 monotonic 포함)와 무관하다.
        """
        with self._deadline_lock:
            if remaining_s is None:
                self._deadline_at = None
            else:
                r = float(remaining_s)
                self._deadline_at = time.monotonic() + (r if math.isfinite(r) else 0.0)

    def _deadline_remaining(self) -> float | None:
        with self._deadline_lock:
            at = self._deadline_at
        return None if at is None else at - time.monotonic()

    # ------------------------------------------------------------------ 재시도
    @staticmethod
    def _retryable(e: BaseException) -> bool:
        """SDK 의 재시도 판정과 같은 집합(타임아웃 제외: 타임아웃은 시간 창을 다 쓴 것이다)."""
        if isinstance(e, anthropic.APITimeoutError):
            return False
        if isinstance(e, anthropic.APIStatusError):
            response = getattr(e, "response", None)
            headers = getattr(response, "headers", None) or {}
            try:
                should = headers.get("x-should-retry")
            except Exception:  # noqa: BLE001
                should = None
            if should == "true":
                return True
            if should == "false":
                return False
            status = getattr(e, "status_code", 0) or 0
            return status in (408, 409, 429) or status >= 500
        return isinstance(e, anthropic.APIConnectionError)

    @staticmethod
    def _retry_delay(e: BaseException, retry_index: int) -> float:
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None) or {}
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            try:
                raw = headers.get(name)
                if raw is not None:
                    v = float(raw) * scale
                    if math.isfinite(v) and 0.0 <= v <= 60.0:
                        return v
            except Exception:  # noqa: BLE001 - HTTP 날짜 형식 등은 기본 백오프
                pass
        base = min(RETRY_INITIAL_DELAY_S * (2.0 ** retry_index), RETRY_MAX_DELAY_S)
        return base * (1.0 - 0.25 * random.random())

    def _create_bounded(self, kwargs: dict[str, Any], timeout_s: float, budget_s: float) -> Any:
        """``messages.create`` 를 총 ``budget_s`` 초 안에서 호출한다(재시도 포함). 실패는 마지막 예외를 던진다."""
        started = time.monotonic()
        end = started + budget_s
        retry = 0
        while True:
            attempt_timeout = timeout_s if retry == 0 and budget_s >= timeout_s else max(0.0, end - time.monotonic())
            try:
                return self.client.messages.create(**kwargs, timeout=attempt_timeout)
            except Exception as e:  # noqa: BLE001 - 판정 후 다시 던진다
                if retry >= self.max_retries or not self._retryable(e):
                    raise
                delay = self._retry_delay(e, retry)
                if time.monotonic() + delay + MIN_ATTEMPT_S > end:
                    raise
                logger.info(
                    "LLM 재시도 %d/%d error=%s delay=%.2fs 남은시간=%.1fs",
                    retry + 1, self.max_retries, self._map_exception(e), delay, end - time.monotonic(),
                )
                time.sleep(delay)
                retry += 1

    # ------------------------------------------------------------------ 보조
    def _scrub(self, text: Any) -> str:
        """문자열에서 API 키를 가린다. 모든 로그/에러 문자열은 이 함수를 거친다."""
        s = str(text)
        if self._secret and self._secret in s:
            s = s.replace(self._secret, mask_secret(self._secret))
        return s

    def _map_exception(self, e: BaseException) -> str:
        """가장 구체적인 것부터: auth > rate_limit > timeout > server > network > api:<type>."""
        if isinstance(e, anthropic.AuthenticationError):
            return "auth"
        if isinstance(e, anthropic.RateLimitError):
            return "rate_limit"
        if isinstance(e, anthropic.APITimeoutError):
            return "timeout"
        if isinstance(e, anthropic.APIStatusError):
            status = getattr(e, "status_code", 0) or 0
            if status >= 500:
                return "server"
            etype = getattr(e, "type", None) or type(e).__name__
            return f"api:{self._scrub(etype)}"
        if isinstance(e, anthropic.APIConnectionError):
            return "network"
        if isinstance(e, anthropic.APIError):
            etype = getattr(e, "type", None) or type(e).__name__
            return f"api:{self._scrub(etype)}"
        if isinstance(e, ValidationError):
            return "parse_error"
        return f"unexpected:{type(e).__name__}"

    def _fail(self, role: str, model: str, error: str, latency: float, **usage: Any) -> AgentResult:
        return AgentResult(role=role, ok=False, output=None, error=error, model=model, latency_s=latency, **usage)

    @staticmethod
    def _response_text(resp: Any) -> str:
        """응답 content 의 text 블록을 이어 붙인다(구조화 출력은 text 블록 하나에 JSON 으로 온다)."""
        parts: list[str] = []
        for block in getattr(resp, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype is None and isinstance(block, dict):
                btype = block.get("type")
            if btype != "text":
                continue
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    @staticmethod
    def _coerce_output(text: str, output_model: type[BaseModel]) -> BaseModel | None:
        if not text or not text.strip():
            return None
        try:
            return output_model.model_validate_json(text)
        except (ValidationError, ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ API
    def complete(
        self,
        *,
        role: str,
        tier: Tier,
        system: str,
        user_payload: dict,
        output_model: type[BaseModel],
        max_tokens: int,
        effort: Effort,
        timeout_s: float,
    ) -> AgentResult:
        model = self.settings.model_for(tier)
        user_text = _dumps(user_payload)

        # 0) 사이클 기한 — 지났거나 시도 하나 할 시간도 없으면 API 호출 없이 기권
        budget_s = float(timeout_s)
        remaining = self._deadline_remaining()
        if remaining is not None:
            if remaining < MIN_ATTEMPT_S:
                logger.warning("사이클 기한 임박으로 LLM 호출 생략 role=%s model=%s 남은시간=%.1fs", role, model, remaining)
                return self._fail(role, model, "deadline", 0.0)
            budget_s = min(budget_s, remaining)

        # 1) 예산 검사 — 초과면 API 호출 없이 BudgetExceeded 전파 (최악값: 캐시 쓰기 단가 + 토크나이저 여유)
        projected = project_call_cost(
            self.ledger, model, len(system), len(user_text), max_tokens, cache_ttl=self.cache_ttl
        )
        try:
            self.ledger.check(projected)
        except BudgetExceeded:
            logger.warning("LLM 예산 초과로 호출 생략 role=%s model=%s projected=%.4f", role, model, projected)
            raise

        logger.debug(
            "요청 role=%s tier=%s model=%s effort=%s max_tokens=%d payload=%s",
            role, tier, model, effort, max_tokens, self._scrub(user_text)[:_LOG_SNIPPET],  # 가린 뒤 자른다
        )

        # 2) 호출 — create() 는 본문이 스키마에 안 맞아도 원본 Message 를 돌려주므로 usage/stop_reason 을 항상 본다
        started = time.monotonic()
        try:
            resp = self._create_bounded(dict(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral", "ttl": self.cache_ttl},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
                output_config={"effort": effort, "format": _output_format(output_model)},
            ), float(timeout_s), budget_s)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 - 모든 LLM 오류는 사이클을 죽이지 않는다
            latency = time.monotonic() - started
            error = self._map_exception(e)
            request_id = getattr(e, "request_id", None)
            logger.warning(
                "LLM 호출 실패 role=%s model=%s error=%s latency=%.2fs request_id=%s detail=%s",
                role, model, error, latency, request_id,
                self._scrub(getattr(e, "message", None) or str(e))[:_LOG_SNIPPET],
            )
            if error.startswith("unexpected:"):
                logger.debug("예상 못한 예외 유형 %s", type(e).__name__)
            return self._fail(role, model, error, latency, request_id=request_id if isinstance(request_id, str) else None)
        latency = time.monotonic() - started

        # 3) 사용량 기록 (거부/잘림이어도 토큰은 소비됐다)
        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        request_id = getattr(resp, "_request_id", None)
        if not isinstance(request_id, str):
            request_id = None
        cost = self.ledger.record(
            role=role,
            model=model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            request_id=request_id,
        )
        usage_kw = dict(
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            request_id=request_id,
        )
        logger.info(
            "LLM 응답 role=%s model=%s in=%d cached=%d cache_write=%d out=%d cost=%.5f latency=%.2fs request_id=%s stop=%s",
            role, model, input_tokens, cached_tokens, cache_write_tokens, output_tokens, cost, latency, request_id,
            getattr(resp, "stop_reason", None),
        )

        # 4) 정지 사유
        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None)
            if category is None and isinstance(details, dict):
                category = details.get("category")
            category = self._scrub(category or "unknown")
            logger.warning("LLM 거부 role=%s model=%s category=%s", role, model, category)
            return self._fail(role, model, f"refusal:{category}", latency, **usage_kw)
        if stop_reason == "max_tokens":
            logger.warning("LLM 출력 잘림(max_tokens=%d) role=%s model=%s", max_tokens, role, model)
            return self._fail(role, model, "max_tokens", latency, **usage_kw)

        # 5) 구조화 출력 검증
        try:
            text = self._response_text(resp)
        except Exception as e:  # noqa: BLE001 - 응답 본문 접근 실패도 parse_error
            logger.warning("LLM 출력 파싱 실패 role=%s model=%s: %s", role, model, self._scrub(e)[:_LOG_SNIPPET])
            text = ""
        output = self._coerce_output(text, output_model)
        if output is None:
            logger.warning("LLM 출력 검증 실패 role=%s model=%s output_model=%s", role, model, output_model.__name__)
            return self._fail(role, model, "parse_error", latency, **usage_kw)

        logger.debug("응답 본문 role=%s: %s", role, self._scrub(output.model_dump_json())[:_LOG_SNIPPET])
        return AgentResult(role=role, ok=True, output=output, error=None, model=model, latency_s=latency, **usage_kw)

    def __repr__(self) -> str:
        return f"AnthropicBackend(models={self.settings.model_analysis},{self.settings.model_research},{self.settings.model_head}, cache_ttl={self.cache_ttl}, key={mask_secret(self._secret)})"
