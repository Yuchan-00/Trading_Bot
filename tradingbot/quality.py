"""시장 데이터 품질 검사 (SPEC §6).

``check()``는 한 종목의 캔들(타임프레임별)과 티커를 보고 ``QualityReport``를 만든다.
``ok=False``이면 사이클은 그 종목의 LLM 호출을 건너뛴다(청산 검사는 계속한다).
순수 함수: 네트워크·상태 없음, 같은 입력이면 같은 출력.

reasons 문자열(결정적 순서: 타임프레임 짧은 것부터, 그 다음 티커):
- ``unknown_tf_<tf>``       알 수 없는 타임프레임(stale/gap 검사 생략)
- ``too_few_<tf>:<n><<min>`` 봉 수 부족 (15m 220, 1h 60, 4h 60, 1d 30)
- ``nan``                   NaN/inf 값 (캔들 또는 티커)
- ``non_positive``          가격 <= 0 또는 거래량 < 0 (캔들 또는 티커)
- ``unsorted_<tf>``         ts가 오름차순이 아니거나 중복
- ``gap_<tf>:<k>``          누락된 봉 수(연속 ts 간격으로 계산)
- ``future_<tf>``           마지막 봉 시작이 now보다 한 봉 이상 미래
- ``stale_<tf>:<s>s``       마지막 봉 종료(ts+tf)가 now - 2*tf 보다 오래됨
- ``outlier_<tf>``          봉 간 종가 변동 > 30%
- ``symbol_mismatch``       ticker.symbol != symbol
- ``crossed_book``          bid > ask
- ``spread_too_wide:<p>%``  (ask-bid)/mid > 0.5%
- ``ticker_stale``          ticker.ts가 5분보다 오래됨
"""
from __future__ import annotations

import logging
import math

from .models import Candle, QualityReport, Ticker

log = logging.getLogger("tradingbot.quality")

TF_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
}
# 타임프레임별 최소 봉 수(지표 워밍업 기준). 표에 없는 tf는 1개 이상이면 통과.
MIN_CANDLES: dict[str, int] = {"15m": 220, "1h": 60, "4h": 60, "1d": 30}
STALE_MULTIPLIER = 2            # 마지막 봉 종료가 now - STALE_MULTIPLIER*tf 보다 오래되면 stale
OUTLIER_PCT = 0.30              # 봉 간 종가 변동 상한
MAX_SPREAD_PCT = 0.005          # 호가 스프레드 상한 (0.5%)
TICKER_STALE_MS = 5 * 60 * 1000  # 티커 허용 지연 5분


def _finite(x: float | None) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def _tf_order(tf: str) -> tuple[int, str]:
    """알려진 tf는 길이순, 모르는 tf는 뒤에 이름순."""
    return (TF_SECONDS.get(tf, 10**9), tf)


def _check_candles(tf: str, candles: list[Candle], now_ms: int) -> tuple[list[str], int | None]:
    """한 타임프레임의 캔들을 검사한다. 반환 (reasons, stale_seconds|None)."""
    reasons: list[str] = []
    tf_s = TF_SECONDS.get(tf)
    if tf_s is None:
        reasons.append(f"unknown_tf_{tf}")
    tf_ms = tf_s * 1000 if tf_s is not None else None

    n = len(candles)
    minimum = MIN_CANDLES.get(tf, 1)
    if n < minimum:
        reasons.append(f"too_few_{tf}:{n}<{minimum}")
    if n == 0:
        return reasons, None

    has_nan = False
    has_non_positive = False
    for c in candles:
        prices = (c.open, c.high, c.low, c.close)
        if not all(_finite(p) for p in prices) or not _finite(c.volume):
            has_nan = True
            continue
        if any(p <= 0 for p in prices) or c.volume < 0:
            has_non_positive = True
    if has_nan:
        reasons.append("nan")
    if has_non_positive:
        reasons.append("non_positive")

    # ts 순서·누락 봉
    unsorted = False
    missing = 0
    if tf_ms is not None:
        for prev, cur in zip(candles, candles[1:]):
            delta = cur.ts - prev.ts
            if delta <= 0:
                unsorted = True
            elif delta != tf_ms:
                missing += max(1, round(delta / tf_ms) - 1)
    else:
        for prev, cur in zip(candles, candles[1:]):
            if cur.ts - prev.ts <= 0:
                unsorted = True
    if unsorted:
        reasons.append(f"unsorted_{tf}")
    if missing:
        reasons.append(f"gap_{tf}:{missing}")

    # 신선도
    stale_seconds: int | None = None
    if tf_ms is not None:
        last_ts = max(c.ts for c in candles)  # unsorted여도 "가장 최근" 봉 기준
        if last_ts > now_ms + tf_ms:
            reasons.append(f"future_{tf}")
        close_time = last_ts + tf_ms
        age_ms = now_ms - close_time
        if close_time < now_ms - STALE_MULTIPLIER * tf_ms:
            stale_seconds = int(age_ms // 1000)
            reasons.append(f"stale_{tf}:{stale_seconds}s")

    # 종가 이상치 (NaN/0은 위에서 이미 보고했으므로 건너뜀)
    outlier = False
    for prev, cur in zip(candles, candles[1:]):
        if _finite(prev.close) and _finite(cur.close) and prev.close > 0:
            # 나눗셈 대신 곱셈 비교: 정확히 30%인 변동이 부동소수 오차로 걸리지 않게 한다
            if abs(cur.close - prev.close) > OUTLIER_PCT * prev.close:
                outlier = True
                break
    if outlier:
        reasons.append(f"outlier_{tf}")

    return reasons, stale_seconds


def _check_ticker(symbol: str, ticker: Ticker, now_ms: int) -> list[str]:
    reasons: list[str] = []
    if ticker.symbol != symbol:
        reasons.append("symbol_mismatch")

    values = [ticker.last, ticker.bid, ticker.ask]
    if any(v is not None and not _finite(v) for v in values):
        reasons.append("nan")
    elif any(v is not None and v <= 0 for v in values):
        reasons.append("non_positive")
    elif _finite(ticker.bid) and _finite(ticker.ask):
        bid, ask = float(ticker.bid), float(ticker.ask)  # type: ignore[arg-type]
        if bid > ask:
            reasons.append("crossed_book")
        else:
            mid = (bid + ask) / 2.0
            spread = (ask - bid) / mid if mid > 0 else float("inf")
            if spread > MAX_SPREAD_PCT:
                reasons.append(f"spread_too_wide:{round(spread * 100, 2):g}%")

    if now_ms - ticker.ts > TICKER_STALE_MS:
        reasons.append("ticker_stale")
    return reasons


def check(
    symbol: str,
    candles_by_tf: dict[str, list[Candle]],
    ticker: Ticker,
    now_ms: int,
    cycle_minutes: int,
) -> QualityReport:
    """종목 데이터 품질을 검사한다.

    ``stale_seconds``는 stale로 판정된 타임프레임 중 가장 큰 지연(초). stale이 없으면 None.
    ``cycle_minutes``는 보고/로그용 맥락이며 판정 기준은 타임프레임(2*tf)과 고정 상수다.
    어떤 입력에도 예외를 던지지 않는다(입력 자체가 모델 검증을 통과했다는 전제).
    """
    reasons: list[str] = []
    stale_max: int | None = None

    for tf in sorted(candles_by_tf.keys(), key=_tf_order):
        tf_reasons, stale = _check_candles(tf, list(candles_by_tf[tf] or []), now_ms)
        reasons.extend(tf_reasons)
        if stale is not None and (stale_max is None or stale > stale_max):
            stale_max = stale

    reasons.extend(_check_ticker(symbol, ticker, now_ms))

    # 중복 제거(순서 유지): "nan"/"non_positive"는 tf·티커에서 여러 번 나올 수 있다.
    seen: set[str] = set()
    unique: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)

    ok = not unique
    if not ok:
        log.info("품질 검사 실패 %s (cycle=%dm): %s", symbol, cycle_minutes, ", ".join(unique))
    else:
        log.debug("품질 검사 통과 %s (cycle=%dm, tfs=%s)", symbol, cycle_minutes, sorted(candles_by_tf))
    return QualityReport(symbol=symbol, ok=ok, reasons=unique, stale_seconds=stale_max)
