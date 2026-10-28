"""기술 지표 (SPEC §5).

순수 파이썬 구현(numpy/pandas 불필요). 모든 도우미는 입력과 같은 길이의 리스트를 돌려주고,
워밍업이 끝나기 전 구간은 ``None`` 이다. 부동소수 반올림을 하지 않으므로 같은 입력이면
항상 같은 출력이 나온다(결정성).

규약
- EMA: SMA 시드(첫 n개 평균) 후 ``k = 2/(n+1)`` 지수 가중.
- RSI/ATR/ADX: Wilder 평활. 상승·하락이 모두 0이면 RSI는 50.0.
- MACD(12,26,9): macd 선은 26봉째부터, signal/hist는 34봉째부터 계산되지만
  ``compute()`` 는 ``WARMUP["macd"]=35`` 미만이면 세 값 모두 None 으로 둔다.
- Bollinger(20, 2): 모집단 표준편차(ddof=0). ``width_pct = (upper - lower) / middle``.
- VWAP: UTC 일 단위 세션. 각 봉의 값은 그 봉이 속한 UTC 날짜의 누적 VWAP.
  마지막 원소가 "현재 세션" VWAP 이다.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from tradingbot.models import Candle, IndicatorSet

log = logging.getLogger("tradingbot.indicators")

__all__ = [
    "WARMUP",
    "compute",
    "ema",
    "sma",
    "rsi",
    "macd",
    "atr",
    "bollinger",
    "adx",
    "vwap",
]

# 각 지표가 값을 내기 위해 필요한 최소 봉 수. compute()는 이 수 미만이면 해당 필드를 None 으로 둔다.
WARMUP: dict[str, int] = {
    "ema_20": 20,
    "ema_50": 50,
    "ema_200": 200,
    "rsi_14": 15,
    "macd": 35,
    "atr_14": 15,
    "bb": 20,
    "adx_14": 28,
    "volume_ratio_20": 21,
    "high_low_20": 20,
}

_MS_PER_DAY = 86_400_000


# ---------------------------------------------------------------- 내부 도우미
def _check_n(n: int) -> None:
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"기간 n은 1 이상의 정수여야 함: {n!r}")


def _floats(values: Sequence[float]) -> list[float]:
    """입력을 float 리스트로 복사한다(입력은 바꾸지 않는다)."""
    return [float(v) for v in values]


def _ema_from(values: list[float], n: int, start: int) -> list[float | None]:
    """``values[start:]`` 에 대해 SMA 시드 EMA 를 계산해 전체 길이 리스트로 돌려준다.

    ``values[:start]`` 구간은 무시(None)된다. MACD 의 signal 처럼 "앞부분이 None 인 시리즈"
    위에 EMA 를 얹을 때 쓴다.
    """
    length = len(values)
    out: list[float | None] = [None] * length
    seed_idx = start + n - 1
    if seed_idx >= length:
        return out
    k = 2.0 / (n + 1)
    acc = 0.0
    for v in values[start:seed_idx + 1]:
        acc += v
    prev = acc / n
    out[seed_idx] = prev
    for i in range(seed_idx + 1, length):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


def _true_ranges(candles: Sequence[Candle]) -> list[float]:
    """TR 리스트. 첫 봉은 이전 종가가 없으므로 high-low."""
    out: list[float] = []
    prev_close: float | None = None
    for c in candles:
        hl = float(c.high) - float(c.low)
        if prev_close is None:
            tr = hl
        else:
            tr = max(hl, abs(float(c.high) - prev_close), abs(float(c.low) - prev_close))
        out.append(tr)
        prev_close = float(c.close)
    return out


def _utc_day(ts_ms: int) -> int:
    """epoch ms -> UTC 일 번호(바닥 나눗셈). 음수 ts 도 올바르게 처리."""
    return int(ts_ms) // _MS_PER_DAY


# ---------------------------------------------------------------- 공개 도우미
def sma(values: Sequence[float], n: int) -> list[float | None]:
    """단순이동평균. 앞 n-1 개는 None."""
    _check_n(n)
    vals = _floats(values)
    out: list[float | None] = [None] * len(vals)
    if len(vals) < n:
        return out
    window_sum = 0.0
    for i, v in enumerate(vals):
        window_sum += v
        if i >= n:
            window_sum -= vals[i - n]
        if i >= n - 1:
            out[i] = window_sum / n
    return out


def ema(values: Sequence[float], n: int) -> list[float | None]:
    """지수이동평균(SMA 시드). 앞 n-1 개는 None."""
    _check_n(n)
    return _ema_from(_floats(values), n, 0)


def rsi(values: Sequence[float], n: int = 14) -> list[float | None]:
    """Wilder RSI. 앞 n 개는 None(변화량 n개가 필요). 상승·하락 평균이 모두 0이면 50.0."""
    _check_n(n)
    vals = _floats(values)
    length = len(vals)
    out: list[float | None] = [None] * length
    if length < n + 1:
        return out

    gain_sum = 0.0
    loss_sum = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        if d > 0:
            gain_sum += d
        else:
            loss_sum -= d
    avg_gain = gain_sum / n
    avg_loss = loss_sum / n
    out[n] = _rsi_value(avg_gain, avg_loss)

    for i in range(n + 1, length):
        d = vals[i] - vals[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD. ``(macd_line, signal_line, histogram)`` 세 리스트(각각 입력과 같은 길이).

    macd 선은 ``slow-1`` 인덱스부터, signal/hist 는 ``slow+signal-2`` 인덱스부터 값이 있다.
    """
    _check_n(fast)
    _check_n(slow)
    _check_n(signal)
    if fast >= slow:
        raise ValueError(f"fast({fast})는 slow({slow})보다 작아야 함")
    vals = _floats(values)
    length = len(vals)
    fast_e = _ema_from(vals, fast, 0)
    slow_e = _ema_from(vals, slow, 0)

    macd_line: list[float | None] = [None] * length
    for i in range(slow - 1, length):
        f = fast_e[i]
        s = slow_e[i]
        if f is not None and s is not None:
            macd_line[i] = f - s

    signal_line: list[float | None] = [None] * length
    hist: list[float | None] = [None] * length
    if length >= slow:
        # macd 선이 있는 구간(slow-1 부터)에 대해 EMA(signal). None 구간은 0으로 채우되 start 로 건너뛴다.
        dense = [0.0 if m is None else m for m in macd_line]
        sig = _ema_from(dense, signal, slow - 1)
        for i in range(length):
            s = sig[i]
            m = macd_line[i]
            if s is not None and m is not None:
                signal_line[i] = s
                hist[i] = m - s
    return macd_line, signal_line, hist


def atr(candles: Sequence[Candle], n: int = 14) -> list[float | None]:
    """Wilder ATR. 앞 n 개는 None(이전 종가가 있는 TR n개로 시드)."""
    _check_n(n)
    length = len(candles)
    out: list[float | None] = [None] * length
    if length < n + 1:
        return out
    trs = _true_ranges(candles)
    seed = 0.0
    for i in range(1, n + 1):
        seed += trs[i]
    prev = seed / n
    out[n] = prev
    for i in range(n + 1, length):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def bollinger(
    values: Sequence[float], n: int = 20, k: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    """볼린저 밴드. ``(upper, middle, lower, width_pct)``. 모집단 표준편차(ddof=0).

    ``width_pct = (upper - lower) / middle`` 이며 middle 이 0이면 None.
    """
    _check_n(n)
    if not (isinstance(k, (int, float)) and math.isfinite(k) and k >= 0):
        raise ValueError(f"k는 0 이상의 유한한 수여야 함: {k!r}")
    vals = _floats(values)
    length = len(vals)
    upper: list[float | None] = [None] * length
    middle: list[float | None] = [None] * length
    lower: list[float | None] = [None] * length
    width: list[float | None] = [None] * length
    if length < n:
        return upper, middle, lower, width
    for i in range(n - 1, length):
        window = vals[i - n + 1:i + 1]
        mean = sum(window) / n
        var = sum((v - mean) ** 2 for v in window) / n
        # 부동소수 오차로 아주 작은 음수가 될 수 있다.
        std = math.sqrt(var) if var > 0 else 0.0
        up = mean + k * std
        lo = mean - k * std
        upper[i] = up
        middle[i] = mean
        lower[i] = lo
        width[i] = (up - lo) / mean if mean != 0 else None
    return upper, middle, lower, width


def adx(candles: Sequence[Candle], n: int = 14) -> list[float | None]:
    """Wilder ADX. 앞 2n-1 개는 None (DM/TR 평활 n 봉 + DX 평균 n 봉)."""
    _check_n(n)
    length = len(candles)
    out: list[float | None] = [None] * length
    if length < 2 * n:
        return out

    trs = _true_ranges(candles)
    plus_dm: list[float] = [0.0] * length
    minus_dm: list[float] = [0.0] * length
    for i in range(1, length):
        up = float(candles[i].high) - float(candles[i - 1].high)
        down = float(candles[i - 1].low) - float(candles[i].low)
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down

    # Wilder 평활: 첫 값은 1..n 합, 이후 prev - prev/n + cur
    sm_tr = 0.0
    sm_plus = 0.0
    sm_minus = 0.0
    for i in range(1, n + 1):
        sm_tr += trs[i]
        sm_plus += plus_dm[i]
        sm_minus += minus_dm[i]

    def _dx(tr_s: float, p_s: float, m_s: float) -> float:
        if tr_s == 0.0:
            return 0.0
        pdi = 100.0 * p_s / tr_s
        mdi = 100.0 * m_s / tr_s
        denom = pdi + mdi
        if denom == 0.0:
            return 0.0
        return 100.0 * abs(pdi - mdi) / denom

    dx_vals: list[float] = [_dx(sm_tr, sm_plus, sm_minus)]  # index n
    for i in range(n + 1, length):
        sm_tr = sm_tr - sm_tr / n + trs[i]
        sm_plus = sm_plus - sm_plus / n + plus_dm[i]
        sm_minus = sm_minus - sm_minus / n + minus_dm[i]
        dx_vals.append(_dx(sm_tr, sm_plus, sm_minus))

    # dx_vals[j] 는 index n+j 에 해당. 첫 ADX = 첫 n 개 DX 평균 (index 2n-1).
    first = sum(dx_vals[:n]) / n
    out[2 * n - 1] = first
    prev = first
    for j in range(n, len(dx_vals)):
        prev = (prev * (n - 1) + dx_vals[j]) / n
        out[n + j] = prev
    return out


def vwap(candles: Sequence[Candle]) -> list[float | None]:
    """UTC 일 세션 VWAP. 각 봉의 값은 그 봉이 속한 UTC 날짜 안의 누적 ``Σ(tp*vol)/Σvol``.

    날짜가 바뀌면 누적을 리셋한다. 누적 거래량이 0이면 None. 마지막 원소가 현재 세션 VWAP.
    """
    out: list[float | None] = [None] * len(candles)
    cur_day: int | None = None
    pv = 0.0
    vol = 0.0
    for i, c in enumerate(candles):
        day = _utc_day(c.ts)
        if day != cur_day:
            cur_day = day
            pv = 0.0
            vol = 0.0
        tp = (float(c.high) + float(c.low) + float(c.close)) / 3.0
        v = float(c.volume)
        pv += tp * v
        vol += v
        out[i] = pv / vol if vol > 0 else None
    return out


# ---------------------------------------------------------------- 집계
def _last(series: Sequence[float | None]) -> float | None:
    return series[-1] if series else None


def _return(closes: list[float], n: int) -> float | None:
    if len(closes) <= n:
        return None
    base = closes[-1 - n]
    if base == 0:
        return None
    return closes[-1] / base - 1.0


def compute(symbol: str, timeframe: str, candles: list[Candle]) -> IndicatorSet:
    """마지막 봉 기준 IndicatorSet. 워밍업 미만 지표는 None. 빈 입력은 ValueError.

    ``warmup_ok`` 는 ema_20, rsi_14, atr_14, bb_* 가 모두 계산됐을 때만 True.
    """
    if not candles:
        raise ValueError(f"{symbol} {timeframe}: 캔들이 비어 있음")
    n = len(candles)
    closes = [float(c.close) for c in candles]
    last = candles[-1]
    close = closes[-1]

    ema_20 = _last(ema(closes, 20)) if n >= WARMUP["ema_20"] else None
    ema_50 = _last(ema(closes, 50)) if n >= WARMUP["ema_50"] else None
    ema_200 = _last(ema(closes, 200)) if n >= WARMUP["ema_200"] else None
    sma_20 = _last(sma(closes, 20)) if n >= WARMUP["ema_20"] else None
    rsi_14 = _last(rsi(closes, 14)) if n >= WARMUP["rsi_14"] else None

    macd_v = macd_sig = macd_hist = None
    if n >= WARMUP["macd"]:
        m_line, s_line, h_line = macd(closes, 12, 26, 9)
        macd_v, macd_sig, macd_hist = m_line[-1], s_line[-1], h_line[-1]

    atr_14 = _last(atr(candles, 14)) if n >= WARMUP["atr_14"] else None
    atr_pct = (atr_14 / close) if (atr_14 is not None and close != 0) else None

    bb_upper = bb_lower = bb_width = None
    if n >= WARMUP["bb"]:
        up, _mid, lo, width = bollinger(closes, 20, 2.0)
        bb_upper, bb_lower, bb_width = up[-1], lo[-1], width[-1]

    adx_14 = _last(adx(candles, 14)) if n >= WARMUP["adx_14"] else None
    vwap_session = _last(vwap(candles))

    volume_ratio_20 = None
    if n >= WARMUP["volume_ratio_20"]:
        prev_vols = [float(c.volume) for c in candles[-21:-1]]
        mean_vol = sum(prev_vols) / 20.0
        volume_ratio_20 = float(last.volume) / mean_vol if mean_vol > 0 else None

    high_20 = low_20 = None
    if n >= WARMUP["high_low_20"]:
        tail = candles[-20:]
        high_20 = max(float(c.high) for c in tail)
        low_20 = min(float(c.low) for c in tail)

    warmup_ok = all(
        v is not None for v in (ema_20, rsi_14, atr_14, bb_upper, bb_lower, bb_width)
    )
    if not warmup_ok:
        log.debug("%s %s: 워밍업 부족 (봉 %d개)", symbol, timeframe, n)

    return IndicatorSet(
        symbol=symbol,
        timeframe=timeframe,
        ts=int(last.ts),
        close=close,
        ema_20=ema_20,
        ema_50=ema_50,
        ema_200=ema_200,
        sma_20=sma_20,
        rsi_14=rsi_14,
        macd=macd_v,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        atr_14=atr_14,
        atr_pct=atr_pct,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_width_pct=bb_width,
        adx_14=adx_14,
        vwap_session=vwap_session,
        volume_ratio_20=volume_ratio_20,
        return_1=_return(closes, 1),
        return_4=_return(closes, 4),
        return_24=_return(closes, 24),
        high_20=high_20,
        low_20=low_20,
        warmup_ok=warmup_ok,
    )
