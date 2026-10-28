"""LLM 비용 원장 (SPEC §8.2).

- ``PRICES_USD_PER_MTOK``: 모델별 (input, cached_input, output) USD / 1M 토큰. 기본값은 여기,
  ``CostLedger(prices=...)``로 덮어쓸 수 있다.
- ``CostLedger``: jsonl 파일에 호출마다 한 줄을 append 하고, 시작 시 파일을 읽어 당일/당월 합계를 복원한다.
  ``check()``는 예산 초과(또는 초과 예상)이면 ``BudgetExceeded``를 던진다.
- 날짜는 전부 UTC.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger("tradingbot.llm.cost")

# (input, cached_input, output) USD per 1M tokens. 2026-10 기준.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
# 미지 모델은 가장 비싼(opus) 단가로 보수 추정한다.
FALLBACK_PRICE_MODEL = "claude-opus-5-5"
# 캐시 쓰기 토큰 단가 = input 단가 × 배수 (5m: 1.25x, 1h: 2x).
CACHE_WRITE_MULTIPLIER: dict[str, float] = {"5m": 1.25, "1h": 2.0}

_MTOK = 1_000_000.0


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
        if not self.path.exists():
            return
        bad = 0
        try:
            with self.path.open("r", encoding="utf-8") as fh:
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
                    if cost != cost:  # NaN 방어
                        bad += 1
                        continue
                    self._by_date[date] = self._by_date.get(date, 0.0) + cost
        except OSError as e:
            logger.error("비용 원장 읽기 실패 %s: %s", self.path, e)
            return
        if bad:
            logger.warning("비용 원장에 손상된 줄 %d개 무시 (%s)", bad, self.path)
        logger.info(
            "비용 원장 복원: 오늘 %.4f USD, 이달 %.4f USD (%s)",
            self.spent_today(), self.spent_month(), self.path,
        )

    # ------------------------------------------------------------------ 단가
    def price_for(self, model: str) -> tuple[float, float, float]:
        """정확히 일치 → 접두사 일치(가장 긴 것) → opus 단가."""
        if model in self.prices:
            return self.prices[model]
        best: str | None = None
        for key in self.prices:
            if model.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        if best is not None:
            return self.prices[best]
        logger.warning("미지 모델 %r: %s 단가로 보수 추정", model, FALLBACK_PRICE_MODEL)
        return self.prices.get(FALLBACK_PRICE_MODEL, PRICES_USD_PER_MTOK[FALLBACK_PRICE_MODEL])

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
            # 메모리 합계를 먼저 갱신: 파일 쓰기가 실패해도 이 프로세스의 예산 검사는 정확하다.
            self._by_date[date] = self._by_date.get(date, 0.0) + cost
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as e:
                logger.error("비용 원장 쓰기 실패 %s: %s", self.path, e)
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
