"""LLM 비용 원장 (SPEC §8.2).

- ``PRICES_USD_PER_MTOK``: 모델별 (input, cached_input, output) USD / 1M 토큰. 기본값은 여기,
  ``CostLedger(prices=...)``로 덮어쓸 수 있다.
- ``CostLedger``: jsonl 파일에 호출마다 한 줄을 append 하고, 시작 시 파일을 읽어 당일/당월 합계를 복원한다.
  ``check()``는 예산 초과(또는 초과 예상)이면 ``BudgetExceeded``를 던진다.
- Windows 에서 다른 프로그램(Excel 등)이 원장 파일을 잠가 추가 쓰기가 거부되면, 잠깐 재시도한 뒤 보조 파일
  ``<원장>.pending.jsonl`` 에 쓴다(지출을 잃지도, 봇을 멈추지도 않게). 합계는 두 파일을 더해 복원한다.
- 날짜는 전부 UTC.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tradingbot import fsutil

logger = logging.getLogger("tradingbot.llm.cost")

#: 원장이 잠겼을 때 쓰는 보조 파일 접미사: cost_ledger.jsonl → cost_ledger.pending.jsonl
PENDING_SUFFIX = ".pending"
#: 원장 추가 쓰기의 공유 위반 재시도 시간(초). 그 뒤에는 보조 파일로 간다.
LEDGER_SHARING_BUDGET_S = 1.0


def pending_ledger_path(path: Path) -> Path:
    """원장 경로 → 보조(잠김 대비) 원장 경로."""
    p = Path(path)
    return p.with_name(f"{p.stem}{PENDING_SUFFIX}{p.suffix}")

# (input, cached_input, output) USD per 1M tokens. 2026-10 기준.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
# 미지 모델은 보수 추정한다(SPEC §8.2). config 는 날짜 접미사 없는 임의의 ``claude-*`` id 를 받으므로
# (예: claude-fable-5-1 $10/$50, claude-opus-5 $5/$25) Opus 5.5 단가($4/$20)는 '가장 비싼' 값이 아니다.
# 그래서 미지 모델은 FALLBACK_PRICE_MODEL 단가가 아니라 아래 상한과 표의 모든 단가 중 항목별 최댓값으로 잡는다.
FALLBACK_PRICE_MODEL = "claude-opus-5-5"  # 하위호환용 이름(로그·외부 참조). 실제 미지 모델 단가는 아래 상한.
# 설정 가능한 Claude 모델 중 가장 비싼 공개 단가(claude-fable-5-1: in $10 / cache-read $1 / out $50 per MTok).
UNKNOWN_MODEL_PRICE_CEILING: tuple[float, float, float] = (10.0, 1.0, 50.0)
# 캐시 쓰기 토큰 단가 = input 단가 × 배수 (5m: 1.25x, 1h: 2x).
CACHE_WRITE_MULTIPLIER: dict[str, float] = {"5m": 1.25, "1h": 2.0}

_MTOK = 1_000_000.0

# 문자수/4 토큰 추정의 여유 배수(새 토크나이저는 같은 텍스트에 ~30% 더 많은 토큰을 쓸 수 있다).
TOKENIZER_MARGIN = 1.3


def project_call_cost(
    ledger: Any,
    model: str,
    system_chars: int,
    user_chars: int,
    max_tokens: int,
    *,
    cache_ttl: str = "1h",
) -> float:
    """호출 전 예산 검사용 **최악** 예상 비용(USD).

    - 토큰 수 = ceil(문자수/4 × ``TOKENIZER_MARGIN``)
    - system 블록은 콜드 캐시(시간의 첫 호출·재시작 직후)에서 캐시 쓰기로 과금되므로 input 단가 ×
      ``CACHE_WRITE_MULTIPLIER``(백엔드 ttl 과 원장 ttl 중 큰 쪽)
    - 출력은 ``max_tokens`` 전부
    ``ledger.estimate(model, input, cached, output)`` 만 쓰므로 가짜 원장에도 동작한다. 러너의 예산 예약도
    이 함수를 쓰면 두 추정이 어긋나지 않는다.
    """
    sys_tok = math.ceil(max(0, int(system_chars)) / 4 * TOKENIZER_MARGIN)
    user_tok = math.ceil(max(0, int(user_chars)) / 4 * TOKENIZER_MARGIN)
    mult = max(
        CACHE_WRITE_MULTIPLIER.get(str(cache_ttl), max(CACHE_WRITE_MULTIPLIER.values())),
        CACHE_WRITE_MULTIPLIER.get(str(getattr(ledger, "cache_ttl", cache_ttl)), max(CACHE_WRITE_MULTIPLIER.values())),
    )
    return float(ledger.estimate(model, math.ceil(sys_tok * mult) + user_tok, 0, max(0, int(max_tokens))))


class BudgetExceeded(Exception):
    """일/월 LLM 예산 초과(또는 초과 예상). 호출자는 LLM 일시정지로 처리한다."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class CostLedger:
    """호출별 비용 기록·일/월 합계·예산 검사.

    파일 포맷(한 줄 JSON, sort_keys):
    ``{"ts","date","role","model","input_tokens","cached_tokens","cache_write_tokens",
    "output_tokens","cost_usd","request_id"}``
    """

    def __init__(
        self,
        path: Path,
        daily_budget_usd: float,
        monthly_budget_usd: float,
        *,
        now: Callable[[], datetime] | None = None,
        prices: Mapping[str, tuple[float, float, float]] | None = None,
        cache_ttl: Literal["5m", "1h"] = "1h",
    ) -> None:
        if daily_budget_usd <= 0 or monthly_budget_usd <= 0:
            raise ValueError("예산은 0보다 커야 합니다")
        self.path = Path(path)
        self.daily_budget_usd = float(daily_budget_usd)
        self.monthly_budget_usd = float(monthly_budget_usd)
        self._now = now or _utc_now
        self.prices: dict[str, tuple[float, float, float]] = dict(PRICES_USD_PER_MTOK)
        if prices:
            self.prices.update({k: tuple(v) for k, v in prices.items()})  # type: ignore[misc]
        if cache_ttl not in CACHE_WRITE_MULTIPLIER:
            raise ValueError(f"cache_ttl은 5m/1h 중 하나: {cache_ttl!r}")
        self.cache_ttl: Literal["5m", "1h"] = cache_ttl
        self._lock = threading.Lock()
        # date("YYYY-MM-DD") -> 누적 비용. 월 합계는 이 딕셔너리에서 계산한다(날짜 롤오버에 안전).
        self._by_date: dict[str, float] = {}
        self._restore()

    # ------------------------------------------------------------------ 시각
    def _now_utc(self) -> datetime:
        return _to_utc(self._now())

    def _today(self) -> str:
        return self._now_utc().strftime("%Y-%m-%d")

    def _month(self) -> str:
        return self._now_utc().strftime("%Y-%m")

    # ------------------------------------------------------------------ 복원
    def _restore(self) -> None:
        paths = [p for p in (self.path, pending_ledger_path(self.path)) if p.exists()]
        if not paths:
            return
        bad = 0
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            date = str(rec["date"])
                            cost = float(rec["cost_usd"])
                        except (ValueError, KeyError, TypeError):
                            bad += 1
                            continue
                        if not math.isfinite(cost) or cost < 0.0:  # NaN/inf/음수(손상·조작) 방어
                            bad += 1
                            continue
                        self._by_date[date] = self._by_date.get(date, 0.0) + cost
            except OSError as e:
                # 원장은 일/월 예산의 유일한 영속 저장소: 못 읽으면 예산 상태를 모르므로 시작을 거부한다(SPEC §0).
                logger.error("비용 원장 읽기 실패 %s: %s", path, e)
                raise
        if bad:
            logger.warning("비용 원장에 손상된 줄 %d개 무시 (%s)", bad, self.path)
        logger.info(
            "비용 원장 복원: 오늘 %.4f USD, 이달 %.4f USD (%s)",
            self.spent_today(), self.spent_month(), ", ".join(str(p) for p in paths),
        )

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        """``line`` 을 한 줄로 추가한다(공유 위반이면 ``LEDGER_SHARING_BUDGET_S`` 동안 재시도)."""
        def _op() -> None:
            with path.open("a+b") as fh:
                # 크래시로 잘린 마지막 줄(개행 없음) 뒤에 바로 붙이면 이번 정상 기록까지 손상 줄로 합쳐져
                # 재시작 시 버려진다(지출 과소 복원). 마지막 바이트가 개행이 아니면 개행을 먼저 쓴다.
                prefix = b""
                end = fh.seek(0, os.SEEK_END)
                if end > 0:
                    fh.seek(end - 1)
                    if fh.read(1) != b"\n":
                        prefix = b"\n"
                fh.write(prefix + (line + "\n").encode("utf-8"))

        fsutil.retry(_op, what="비용 원장 추가 쓰기", path=path, budget_s=LEDGER_SHARING_BUDGET_S)

    # ------------------------------------------------------------------ 단가
    def price_for(self, model: str) -> tuple[float, float, float]:
        """정확히 일치 → 접두사 일치(가장 긴 것) → 보수 상한(``_fallback_price``)."""
        if model in self.prices:
            return self.prices[model]
        best: str | None = None
        for key in self.prices:
            if model.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        if best is not None:
            return self.prices[best]
        fallback = self._fallback_price()
        logger.warning("미지 모델 %r: 상한 단가 %s 로 보수 추정", model, fallback)
        return fallback

    def _fallback_price(self) -> tuple[float, float, float]:
        """미지 모델 단가: 상한 상수와 (덮어쓴 것 포함) 모든 알려진 단가의 항목별 최댓값 — 절대 과소 추정하지 않는다."""
        candidates = [UNKNOWN_MODEL_PRICE_CEILING, *self.prices.values()]
        return (
            max(float(c[0]) for c in candidates),
            max(float(c[1]) for c in candidates),
            max(float(c[2]) for c in candidates),
        )

    def estimate(self, model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        p_in, p_cached, p_out = self.price_for(model)
        return (
            max(0, input_tokens) * p_in
            + max(0, cached_tokens) * p_cached
            + max(0, output_tokens) * p_out
        ) / _MTOK

    def cache_write_cost(self, model: str, cache_write_tokens: int) -> float:
        p_in, _, _ = self.price_for(model)
        return max(0, cache_write_tokens) * p_in * CACHE_WRITE_MULTIPLIER[self.cache_ttl] / _MTOK

    # ------------------------------------------------------------------ 기록
    def record(
        self,
        *,
        role: str,
        model: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        request_id: str | None = None,
    ) -> float:
        cost = self.estimate(model, input_tokens, cached_tokens, output_tokens) + self.cache_write_cost(
            model, cache_write_tokens
        )
        now = self._now_utc()
        date = now.strftime("%Y-%m-%d")
        rec = {
            "ts": now.isoformat(),
            "date": date,
            "role": role,
            "model": model,
            "input_tokens": int(input_tokens),
            "cached_tokens": int(cached_tokens),
            "cache_write_tokens": int(cache_write_tokens),
            "output_tokens": int(output_tokens),
            "cost_usd": cost,
            "request_id": request_id,
        }
        line = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        with self._lock:
            # 메모리 합계를 먼저 갱신: 파일 쓰기가 실패해도 이 프로세스의 예산 검사는 보수적으로 유지된다.
            self._by_date[date] = self._by_date.get(date, 0.0) + cost
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._append_line(self.path, line)
                except OSError as e:
                    if not fsutil.is_transient_sharing_error(e):
                        raise
                    # 다른 프로그램이 원장을 잠갔다(Windows) → 보조 파일에 남긴다. 합계는 두 파일을 더해 복원한다.
                    pending = pending_ledger_path(self.path)
                    self._append_line(pending, line)
                    logger.warning("비용 원장이 다른 프로그램에 잠겨 있어 보조 파일에 기록: %s (원장을 연 프로그램을 "
                                   "닫으세요)", pending.name)
            except OSError as e:
                # 상태 저장 실패는 멈춘다(SPEC §0): 삼키면 재시작 후 합계가 0 으로 복원돼 월 예산을 조용히 넘긴다.
                logger.error("비용 원장 쓰기 실패 %s: %s", self.path, e)
                raise
        logger.debug(
            "비용 기록 role=%s model=%s in=%d cached=%d cw=%d out=%d cost=%.6f",
            role, model, input_tokens, cached_tokens, cache_write_tokens, output_tokens, cost,
        )
        return cost

    # ------------------------------------------------------------------ 조회
    def spent_today(self) -> float:
        return self._by_date.get(self._today(), 0.0)

    def spent_month(self) -> float:
        prefix = self._month() + "-"
        return sum(v for k, v in self._by_date.items() if k.startswith(prefix))

    def remaining_today(self) -> float:
        return max(0.0, self.daily_budget_usd - self.spent_today())

    def remaining_month(self) -> float:
        return max(0.0, self.monthly_budget_usd - self.spent_month())

    def check(self, projected_cost_usd: float = 0.0) -> None:
        """``spent + projected``가 일/월 예산을 넘으면 ``BudgetExceeded``."""
        projected = max(0.0, float(projected_cost_usd))
        today = self.spent_today()
        if today + projected > self.daily_budget_usd:
            raise BudgetExceeded(
                f"일일 LLM 예산 초과: 사용 {today:.4f} + 예상 {projected:.4f} > 한도 {self.daily_budget_usd:.2f} USD"
            )
        month = self.spent_month()
        if month + projected > self.monthly_budget_usd:
            raise BudgetExceeded(
                f"월간 LLM 예산 초과: 사용 {month:.4f} + 예상 {projected:.4f} > 한도 {self.monthly_budget_usd:.2f} USD"
            )

    def budget_ok(self, projected_cost_usd: float = 0.0) -> bool:
        try:
            self.check(projected_cost_usd)
        except BudgetExceeded:
            return False
        return True

    def __repr__(self) -> str:
        return (
            f"CostLedger(path={str(self.path)!r}, today={self.spent_today():.4f}/{self.daily_budget_usd:.2f}, "
            f"month={self.spent_month():.4f}/{self.monthly_budget_usd:.2f})"
        )
