"""피처팩 (SPEC §17 + v2.1 보완) — 코드가 숫자를 만들고 LLM 은 라벨을 해석한다.

모든 팩 모델은 이 모듈에 있다(``models.py`` 는 고정). 전부 ``extra="forbid"``.

단위 규약
- 퍼센트 필드(``*_pct``)는 **% 단위 숫자**(1.5 = 1.5%). 비율(``*_ratio``, ``bb_position``, 상관 등)은 소수.
- 펀딩(``funding_now`` 등)은 8h 원시 소수(0.0001 = 0.01%).
- ``IndicatorSet.atr_pct``·``Ticker.change_24h_pct``·``OrderBookSummary.spread_pct`` 는 v1 에서 **소수**이므로
  여기서 ×100(%) 또는 ×1e4(bps)로 바꾼다.
- LLM 에 넘기는 값은 :func:`compact` 로 4유효자리 반올림한다(팩 자체는 원래 정밀도 유지).

견고성
- 모든 함수는 짧거나 빈 입력, NaN/inf 값에 대해 예외 대신 ``None`` / ``"unknown"`` / 빈 값을 돌려준다.
  유일한 예외: :func:`build_feature_pack` 은 15m 캔들이 비어 있으면 ``ValueError``.
- 순수 함수: 입력을 바꾸지 않고, 같은 입력이면 같은 출력(결정성).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tradingbot import indicators as ind_mod
from tradingbot.models import Candle, FuturesContext, IndicatorSet, OrderBookSummary, Ticker

log = logging.getLogger("tradingbot.features")

__all__ = [
    # 모델
    "TechnicalLabels", "IchimokuLabels", "CandleLabels", "SmcLabels", "FeaturePack", "FlowPack",
    "RiskPack", "RegimePack", "SentimentComposite",
    # 상수
    "FLOW_THRESHOLDS", "TIMEFRAMES", "LIQ_LEVERAGES", "LIQ_MAINT_MARGIN",
    # 빌더
    "build_feature_pack", "build_flow_pack", "build_risk_pack", "build_regime_pack",
    "composite_sentiment", "compact",
    # 라벨 함수(단독 테스트용 공개)
    "ema_state", "vote", "bb_position", "macd_hist_sign", "obv", "obv_trend", "technical_labels",
    "ichimoku_lines", "ichimoku", "candle_patterns", "swing_points", "structure_events",
    "smc_structure", "fvg", "zones", "smc", "hv_pct_rank", "cgo_60", "abnormal_volume_multiple",
    "volume_vs_hour_avg", "mtf_agreement", "log_returns", "historical_var", "historical_cvar",
    "realized_vol_pct", "daily_vol_pct", "excess_kurtosis", "aligned_returns", "rolling_corr",
    "rolling_beta", "funding_regime", "oi_funding_label", "ls_label", "divergence_flags",
    "vwap_24h", "est_liq_clusters", "nearest_long_liq_below_pct", "cascade_risk", "oir_1pct",
    "spread_bps", "data_confidence", "inverse_vol_size_pct", "cvar_size_pct", "impact_bps_est",
    "order_notional_vs_15m_volume_share", "stop_vs_atr_noise_ok", "exec_cost_pct",
    "avg_pairwise_corr", "btc_regime_4h", "sentiment_label",
]

TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")

# §20.3a — payload 의 thresholds 와 같은 값(펀딩은 8h 원시 소수, OI 는 %).
FLOW_THRESHOLDS: dict[str, float] = {
    "funding_extreme_long": 0.0005, "funding_elevated_long": 0.0002, "funding_negative": -0.0001,
    "funding_squeeze": -0.0002, "oi_buildup_pct": 5.0, "oi_buildup_funding": 0.0003,
    "oi_deleveraging_pct": -5.0, "ls_crowded_long": 2.0, "ls_crowded_short": 0.8,
    "cascade_high_distance_pct": 2.0, "cascade_moderate_distance_pct": 3.0,
}

LIQ_LEVERAGES: tuple[int, ...] = (25, 50, 100)
LIQ_MAINT_MARGIN = 0.005

# 괴리 플래그 임계값(SPEC 미정 → 여기서 고정; % 단위, 펀딩은 8h 소수)
DIVERGENCE_PRICE_PCT = 1.0          # 24h 가격 변화 |x| 가 이보다 크면 "상승/하락"
DIVERGENCE_OI_PCT = 2.0             # 24h OI 변화 |x| 가 이보다 크면 "증가/감소"
DIVERGENCE_FUNDING_RISE = 0.0001    # funding_now − 7d 평균 > 이 값이면 "펀딩 상승"
DIVERGENCE_FLAT_PRICE_PCT = 1.0     # |24h 가격 변화| < 이 값이면 "가격 횡보"

VAR_WINDOW = 200            # 1h 로그수익률 최근 200개
VAR_MIN_SAMPLES = 30        # 이보다 적으면 VaR/CVaR/첨도 None
HV_WINDOW = 24              # 실현변동성 창(1h 봉)
HV_MIN_WINDOWS = 100        # 백분위 계산에 필요한 최소 창 수
CORR_WINDOW_RISK = 96       # BTC/보유종목 상관·베타 창
CORR_MIN_RISK = 48          # 최소 겹치는 수익률 수
CORR_WINDOW_REGIME = 48     # 레짐 평균 쌍상관 창
CORR_MIN_REGIME = 24
ANNUALIZE_1H = math.sqrt(24 * 365)

_SENTIMENT_WEIGHTS: dict[str, float] = {
    "fear_greed": 0.4, "funding_z": 0.2, "ls_pctile": 0.2, "headline_polarity": 0.2,
}


# ======================================================================= 모델
class _Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")


EmaState = Literal["bull_aligned", "bear_aligned", "mixed", "unknown"]
Vote = Literal["trend_up", "trend_down", "mean_reversion_long", "mean_reversion_short", "none"]
Pattern = Literal[
    "bullish_engulfing", "bearish_engulfing", "hammer", "shooting_star", "doji",
    "pin_bar_bull", "pin_bar_bear", "inside_bar", "outside_bar",
]
Structure = Literal["bos_up", "bos_down", "choch_up", "choch_down", "range", "unknown"]


class TechnicalLabels(_Pack):
    timeframe: str
    ema_state: EmaState = "unknown"
    price_vs_ema20_pct: float | None = None
    adx_14: float | None = None
    rsi_14: float | None = None
    rsi_5: float | None = None
    bb_position: float | None = None
    macd_hist_sign: Literal["pos", "neg", "flat", "unknown"] = "unknown"
    obv_trend: Literal["up", "down", "flat", "unknown"] = "unknown"
    volume_ratio_20: float | None = None
    atr_pct: float | None = None
    vote: Vote = "none"


class IchimokuLabels(_Pack):
    tk_cross: Literal["bullish", "bearish", "none"] = "none"
    price_vs_cloud: Literal["above", "inside", "below", "unknown"] = "unknown"
    cloud_bullish: bool | None = None
    cross_strength: Literal["strong", "medium", "weak", "none"] = "none"


class CandleLabels(_Pack):
    patterns: list[Pattern] = Field(default_factory=list)
    score: int = Field(default=0, ge=-3, le=3)


class SmcLabels(_Pack):
    structure: Structure = "unknown"
    fvg_dir: Literal["bullish", "bearish", "none"] = "none"
    nearest_demand_zone: list[float] | None = None
    nearest_supply_zone: list[float] | None = None
    distance_to_demand_pct: float | None = None
    distance_to_supply_pct: float | None = None


class FeaturePack(_Pack):
    symbol: str
    ts: int
    price: float
    per_tf: dict[str, TechnicalLabels] = Field(default_factory=dict)
    ichimoku_1h: IchimokuLabels | None = None
    ichimoku_4h: IchimokuLabels | None = None
    candles_15m: CandleLabels = Field(default_factory=CandleLabels)
    candles_1h: CandleLabels = Field(default_factory=CandleLabels)
    smc_1h: SmcLabels | None = None
    smc_4h: SmcLabels | None = None
    mtf_agreement: bool = False
    hv_pct_rank: float | None = None
    atr_pct_1h: float | None = None
    vwap_distance_pct: float | None = None
    volume_vs_hour_avg: float | None = None
    cgo_60: float | None = None
    abnormal_volume_multiple: float | None = None
    last_closes_1h: list[float] = Field(default_factory=list)
    change_24h_pct: float | None = None
    # v2.1 보완
    noise_floor_stop_pct: float | None = None   # = atr_pct_1h (손절 최소거리, %)
    noise_ok_2pct: bool | None = None           # = 1.5·atr_pct_1h < 2.0


class FlowPack(_Pack):
    symbol: str
    funding_now: float | None = None
    funding_7d_mean: float | None = None
    funding_annualized_pct: float | None = None
    funding_regime: Literal["extreme_long", "elevated_long", "neutral", "negative", "squeeze", "unknown"] = "unknown"
    oi_change_24h_pct: float | None = None
    oi_funding_label: Literal["leveraged_long_buildup", "short_buildup", "deleveraging", "neutral", "unknown"] = "unknown"
    perp_premium_pct: float | None = None
    ls_ratio: float | None = None
    ls_label: Literal["crowded_long", "crowded_short", "balanced", "unknown"] = "unknown"
    divergence_flags: list[str] = Field(default_factory=list)
    est_liq_clusters: list[dict] = Field(default_factory=list)
    nearest_long_liq_below_pct: float | None = None
    cascade_risk: Literal["low", "moderate", "high", "unknown"] = "unknown"
    oir_1pct: float | None = None
    spread_bps: float | None = None
    data_confidence: Literal["high", "low", "none"] = "none"


class RiskPack(_Pack):
    symbol: str
    var95_1h_pct: float | None = None
    var99_1h_pct: float | None = None
    cvar95_1h_pct: float | None = None
    rv_20_pct: float | None = None
    rv_60_pct: float | None = None
    daily_vol_pct: float | None = None
    hv_pct_rank: float | None = None
    kurtosis: float | None = None
    corr_to_btc_1h: float | None = None
    beta_btc: float | None = None
    corr_to_open_positions: dict[str, float] = Field(default_factory=dict)
    inverse_vol_size_pct: float | None = None
    cvar_size_pct: float | None = None
    spread_bps: float | None = None
    depth_taker_usdt: float | None = None
    impact_bps_est: float | None = None
    order_notional_vs_15m_volume_share: float | None = None
    stop_vs_atr_noise_ok: bool | None = None
    # v2.1 보완
    exec_cost_pct: float | None = None


class RegimePack(_Pack):
    avg_pairwise_corr_1h: float | None = None
    density: float | None = None
    fused: bool = False
    btc_regime_4h: Literal["bull", "bear", "range", "unknown"] = "unknown"
    btc_shock: bool = False
    name_alarm_symbols: list[str] = Field(default_factory=list)
    verdict: Literal["normal", "fused", "macro_alarm"] = "normal"


class SentimentComposite(_Pack):
    score_0_100: float | None = None
    label: Literal["extreme_fear", "fear", "neutral", "greed", "extreme_greed", "unknown"] = "unknown"
    components: dict[str, float | None] = Field(default_factory=dict)


# ======================================================================= 기초 도우미
def _num(x: Any) -> float | None:
    """유한한 실수면 float, 아니면 None (bool 은 숫자로 보지 않는다)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pos(x: Any) -> float | None:
    v = _num(x)
    return v if v is not None and v > 0 else None


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _std(xs: Sequence[float], ddof: int = 1) -> float | None:
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var) if var > 0 else 0.0


def _median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


def _closes(candles: Sequence[Candle]) -> list[float]:
    return [float(c.close) for c in candles]


def _candle_finite(c: Candle) -> bool:
    return all(_num(v) is not None for v in (c.open, c.high, c.low, c.close))


def _last_value(series: Sequence[float | None]) -> float | None:
    return _num(series[-1]) if series else None


# ======================================================================= compact
def _r(x: float | None) -> float | None:
    """4유효자리 반올림. 비유한 값은 None, −0.0 은 0.0."""
    v = _num(x)
    if v is None:
        return None
    if v == 0:
        return 0.0
    out = float(f"{v:.4g}")
    return 0.0 if out == 0 else out


def _compact_value(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return _compact_value(v.model_dump(mode="python", by_alias=True))
    if isinstance(v, Mapping):
        return {k: _compact_value(v[k]) for k in sorted(v, key=lambda k: str(k))}
    if isinstance(v, (list, tuple)):
        return [_compact_value(x) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return _r(v)
    return v


def compact(obj: Any) -> Any:
    """model_dump 후 모든 float 를 4유효자리로 반올림(재귀). None 유지, 키는 정렬해 결정적 순서.

    BaseModel 이면 dict 를, dict/list 를 넘기면 같은 형태를 돌려준다. int·bool·str 은 그대로.
    """
    return _compact_value(obj)


# ======================================================================= 기술 라벨
def ema_state(close: float | None, ema_20: float | None, ema_50: float | None,
              ema_200: float | None = None) -> str:
    """close>ema20>ema50(>ema200 있으면) → bull_aligned, 역순 bear_aligned, 그 외 mixed, 값 없으면 unknown."""
    c, e20, e50, e200 = _num(close), _num(ema_20), _num(ema_50), _num(ema_200)
    if c is None or e20 is None or e50 is None:
        return "unknown"
    chain = [c, e20, e50] + ([e200] if e200 is not None else [])
    if all(a > b for a, b in zip(chain, chain[1:])):
        return "bull_aligned"
    if all(a < b for a, b in zip(chain, chain[1:])):
        return "bear_aligned"
    return "mixed"


def macd_hist_sign(hist: float | None, close: float | None) -> str:
    """|hist| < 1e-9·close → flat, 양수 pos, 음수 neg, 값 없으면 unknown."""
    h, c = _num(hist), _num(close)
    if h is None or c is None:
        return "unknown"
    if abs(h) < 1e-9 * abs(c):
        return "flat"
    return "pos" if h > 0 else "neg"


def bb_position(close: float | None, upper: float | None, lower: float | None) -> float | None:
    """(close−lower)/(upper−lower). 밴드 밖이면 <0 또는 >1. 밴드 폭 0/값 없음 → None."""
    c, up, lo = _num(close), _num(upper), _num(lower)
    if c is None or up is None or lo is None or up <= lo:
        return None
    return (c - lo) / (up - lo)


def vote(state: str, adx_14: float | None, macd_sign: str, rsi_14: float | None,
         bb_pos: float | None) -> str:
    """trend_up: bull_aligned ∧ adx>20 ∧ macd pos ; trend_down 대칭 ;
    mean_reversion_long: rsi_14<30 ∧ bb_position<0.05 ; mean_reversion_short: rsi_14>70 ∧ bb_position>0.95.
    추세 투표가 평균회귀보다 우선."""
    adx_v, rsi_v, bb_v = _num(adx_14), _num(rsi_14), _num(bb_pos)
    if adx_v is not None and adx_v > 20:
        if state == "bull_aligned" and macd_sign == "pos":
            return "trend_up"
        if state == "bear_aligned" and macd_sign == "neg":
            return "trend_down"
    if rsi_v is not None and bb_v is not None:
        if rsi_v < 30 and bb_v < 0.05:
            return "mean_reversion_long"
        if rsi_v > 70 and bb_v > 0.95:
            return "mean_reversion_short"
    return "none"


def obv(candles: Sequence[Candle]) -> list[float]:
    """On-Balance Volume(첫 봉 0). 비유한 종가/거래량 봉은 기여 0."""
    out: list[float] = []
    acc = 0.0
    prev: float | None = None
    for c in candles:
        close = _num(c.close)
        vol = _num(c.volume)
        if prev is not None and close is not None and vol is not None:
            if close > prev:
                acc += vol
            elif close < prev:
                acc -= vol
        if close is not None:
            prev = close
        out.append(acc)
    return out


def _ls_slope(ys: Sequence[float]) -> float:
    n = len(ys)
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    return sxy / sxx if sxx > 0 else 0.0


def obv_trend(candles: Sequence[Candle], n: int = 20) -> str:
    """OBV 최근 n봉 최소제곱 기울기 부호. |slope| < |평균 OBV|·1e-4 (또는 0) → flat. n봉 미만 → unknown."""
    if len(candles) < n or n < 2:
        return "unknown"
    window = obv(candles)[-n:]
    slope = _ls_slope(window)
    thr = abs(sum(window) / n) * 1e-4
    if slope == 0 or abs(slope) < thr:
        return "flat"
    return "up" if slope > 0 else "down"


def _rsi5(candles: Sequence[Candle]) -> float | None:
    if len(candles) < 6:
        return None
    try:
        return _last_value(ind_mod.rsi(_closes(candles), 5))
    except (ValueError, TypeError):  # pragma: no cover - 방어
        return None


def _safe_compute(symbol: str, timeframe: str, candles: Sequence[Candle]) -> IndicatorSet | None:
    if not candles:
        return None
    try:
        return ind_mod.compute(symbol, timeframe, list(candles))
    except (ValueError, TypeError) as exc:
        log.debug("%s %s 지표 계산 실패: %s", symbol, timeframe, exc)
        return None


def technical_labels(timeframe: str, candles: Sequence[Candle], ind: IndicatorSet | None) -> TechnicalLabels:
    """한 타임프레임의 TechnicalLabels. ind 가 없으면 unknown/None 위주로 채운다."""
    if ind is None:
        return TechnicalLabels(timeframe=timeframe, rsi_5=_rsi5(candles), obv_trend=obv_trend(candles))  # type: ignore[arg-type]
    close = _num(ind.close)
    state = ema_state(close, ind.ema_20, ind.ema_50, ind.ema_200)
    e20 = _pos(ind.ema_20)
    pve = (close / e20 - 1.0) * 100.0 if (close is not None and e20 is not None) else None
    bbp = bb_position(close, ind.bb_upper, ind.bb_lower)
    msign = macd_hist_sign(ind.macd_hist, close)
    atr_pct = _num(ind.atr_pct)
    return TechnicalLabels(
        timeframe=timeframe,
        ema_state=state,  # type: ignore[arg-type]
        price_vs_ema20_pct=pve,
        adx_14=_num(ind.adx_14),
        rsi_14=_num(ind.rsi_14),
        rsi_5=_rsi5(candles),
        bb_position=bbp,
        macd_hist_sign=msign,  # type: ignore[arg-type]
        obv_trend=obv_trend(candles),  # type: ignore[arg-type]
        volume_ratio_20=_num(ind.volume_ratio_20),
        atr_pct=atr_pct * 100.0 if atr_pct is not None else None,
        vote=vote(state, ind.adx_14, msign, ind.rsi_14, bbp),  # type: ignore[arg-type]
    )


def mtf_agreement(per_tf: Mapping[str, TechnicalLabels]) -> bool:
    """(4h bull_aligned ∨ (4h mixed ∧ 1d bull_aligned)) ∧ 1h vote∈{trend_up, mean_reversion_long} ∧ 15m vote≠trend_down.
    4h·1h·15m 중 하나라도 없으면 False(확인 불가 = 동의 아님)."""
    t4, t1, t15, t1d = per_tf.get("4h"), per_tf.get("1h"), per_tf.get("15m"), per_tf.get("1d")
    if t4 is None or t1 is None or t15 is None:
        return False
    regime = t4.ema_state == "bull_aligned" or (
        t4.ema_state == "mixed" and t1d is not None and t1d.ema_state == "bull_aligned")
    return bool(regime and t1.vote in ("trend_up", "mean_reversion_long") and t15.vote != "trend_down")


# ======================================================================= 일목
ICHI_TENKAN, ICHI_KIJUN, ICHI_SENKOU_B, ICHI_SHIFT = 9, 26, 52, 26
ICHI_MIN_BARS = ICHI_SENKOU_B + ICHI_SHIFT   # 78


def _midpoints(candles: Sequence[Candle], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    for i in range(n - 1, len(candles)):
        win = candles[i - n + 1:i + 1]
        highs = [_num(c.high) for c in win]
        lows = [_num(c.low) for c in win]
        if any(v is None for v in highs) or any(v is None for v in lows):
            continue
        out[i] = (max(highs) + min(lows)) / 2.0  # type: ignore[type-var,operator]
    return out


def ichimoku_lines(candles: Sequence[Candle]) -> dict[str, list[float | None]]:
    """전환선(9)·기준선(26)·선행스팬 A/B(**이동 전** 원시값; 봉 t 의 현재 구름은 t−26 의 값)."""
    tenkan = _midpoints(candles, ICHI_TENKAN)
    kijun = _midpoints(candles, ICHI_KIJUN)
    span_a: list[float | None] = [
        (t + k) / 2.0 if (t is not None and k is not None) else None for t, k in zip(tenkan, kijun)]
    span_b = _midpoints(candles, ICHI_SENKOU_B)
    return {"tenkan": tenkan, "kijun": kijun, "span_a_raw": span_a, "span_b_raw": span_b}


def _cloud_at(lines: dict[str, list[float | None]], i: int) -> tuple[float, float] | None:
    j = i - ICHI_SHIFT
    if j < 0:
        return None
    a, b = lines["span_a_raw"][j], lines["span_b_raw"][j]
    if a is None or b is None:
        return None
    return a, b


def _vs_cloud(level: float, cloud: tuple[float, float]) -> str:
    a, b = cloud
    if level > max(a, b):
        return "above"
    if level < min(a, b):
        return "below"
    return "inside"


def _last_nonzero_diff(tenkan: list[float | None], kijun: list[float | None], i: int) -> float | None:
    """봉 i 부터 거슬러 올라가며 처음 만나는 0 이 아닌 (전환−기준). 값이 없으면 None."""
    for j in range(i, -1, -1):
        a, b = tenkan[j], kijun[j]
        if a is None or b is None:
            return None
        d = a - b
        if d != 0:
            return d
    return None


def ichimoku(candles: Sequence[Candle]) -> IchimokuLabels | None:
    """일목 라벨. 78봉 미만이거나 현재 구름을 만들 수 없으면 None.

    - 현재 구름 = 26봉 전에 계산된 선행스팬 A/B. ``cloud_bullish`` = A > B.
    - ``tk_cross``: 최근 3봉(t−2..t) 안에서 (전환−기준)이 >0 이 되었고 그 직전의 마지막 0 이 아닌 값이
      <0 이면 bullish, 대칭으로 bearish (가장 최근 교차 우선). 0 에 접촉했다가 같은 쪽으로 돌아가는 것은
      교차가 아니다.
    - ``cross_strength``: 교차 봉의 교차 수준((전환+기준)/2)을 그 봉의 구름과 비교.
      bullish: 구름 위 ∧ 구름 강세 → strong, 구름 위(약세 구름) → medium, 구름 안·아래 → weak
      (skills/ichimoku: "weak (it happened below or inside the cloud)").
      bearish 는 대칭. 교차 없음 → none.
    """
    n = len(candles)
    if n < ICHI_MIN_BARS:
        return None
    lines = ichimoku_lines(candles)
    t = n - 1
    cloud = _cloud_at(lines, t)
    close = _num(candles[t].close)
    if cloud is None or close is None:
        return None
    tenkan, kijun = lines["tenkan"], lines["kijun"]
    cross = "none"
    cross_idx: int | None = None
    for i in range(t, t - 3, -1):
        t0, k0, t1, k1 = tenkan[i - 1], kijun[i - 1], tenkan[i], kijun[i]
        if None in (t0, k0, t1, k1):
            continue
        d0, d1 = t0 - k0, t1 - k1  # type: ignore[operator]
        # 봉 i 에서 부호가 새로 생기거나 뒤집힌 경우만 후보(d0 == 0 이면 접촉 이전 값으로 판정)
        if d1 == 0 or d0 * d1 > 0:
            continue
        prev = _last_nonzero_diff(tenkan, kijun, i - 1)
        if prev is None:
            continue
        if prev < 0 < d1:
            cross, cross_idx = "bullish", i
            break
        if prev > 0 > d1:
            cross, cross_idx = "bearish", i
            break
    strength = "none"
    if cross_idx is not None:
        cl = _cloud_at(lines, cross_idx) or cloud
        level = (tenkan[cross_idx] + kijun[cross_idx]) / 2.0  # type: ignore[operator]
        pos = _vs_cloud(level, cl)
        bullish_cloud = cl[0] > cl[1]
        bearish_cloud = cl[0] < cl[1]
        if cross == "bullish":
            strength = "weak" if pos != "above" else ("strong" if bullish_cloud else "medium")
        else:
            strength = "weak" if pos != "below" else ("strong" if bearish_cloud else "medium")
    return IchimokuLabels(
        tk_cross=cross,  # type: ignore[arg-type]
        price_vs_cloud=_vs_cloud(close, cloud),  # type: ignore[arg-type]
        cloud_bullish=cloud[0] > cloud[1],
        cross_strength=strength,  # type: ignore[arg-type]
    )


# ======================================================================= 캔들 패턴
_PATTERN_ORDER: tuple[str, ...] = (
    "bullish_engulfing", "bearish_engulfing", "hammer", "shooting_star", "doji",
    "pin_bar_bull", "pin_bar_bear", "inside_bar", "outside_bar",
)
_PATTERN_SCORE: dict[str, int] = {
    "bullish_engulfing": 1, "hammer": 1, "pin_bar_bull": 1,
    "bearish_engulfing": -1, "shooting_star": -1, "pin_bar_bear": -1,
}


def candle_patterns(candles: Sequence[Candle]) -> CandleLabels:
    """마지막 2봉만 본다. 단일봉 패턴은 마지막 봉, 2봉 패턴은 (직전, 마지막).

    - engulfing: 색이 반대이고 마지막 몸통이 직전 몸통을 감싼다(경계 포함, 몸통이 더 큼).
    - hammer: 아래꼬리 ≥ 2×몸통 ∧ 위꼬리 ≤ 몸통. shooting_star: 대칭.
    - doji: 몸통 ≤ 범위의 10%. pin_bar_bull/bear: 아래/위 꼬리 ≥ 범위의 66%.
    - inside_bar: 고가 ≤ 직전 고가 ∧ 저가 ≥ 직전 저가(완전 동일 제외). outside_bar: 대칭.
    점수: 강세 +1 / 약세 −1 합을 −3..3 으로 클립. 범위 0인 봉·비유한 봉은 패턴 없음.
    """
    found: set[str] = set()
    if candles:
        last = candles[-1]
        if _candle_finite(last):
            o, h, lo, c = float(last.open), float(last.high), float(last.low), float(last.close)
            rng = h - lo
            if rng > 0:
                body = abs(c - o)
                upper = h - max(o, c)
                lower = min(o, c) - lo
                if lower >= 2 * body and upper <= body:
                    found.add("hammer")
                if upper >= 2 * body and lower <= body:
                    found.add("shooting_star")
                if body <= 0.10 * rng:
                    found.add("doji")
                if lower >= 0.66 * rng:
                    found.add("pin_bar_bull")
                if upper >= 0.66 * rng:
                    found.add("pin_bar_bear")
        if len(candles) >= 2:
            prev = candles[-2]
            if _candle_finite(prev) and _candle_finite(last):
                po, ph, pl, pc = float(prev.open), float(prev.high), float(prev.low), float(prev.close)
                o, h, lo, c = float(last.open), float(last.high), float(last.low), float(last.close)
                p_top, p_bot = max(po, pc), min(po, pc)
                l_top, l_bot = max(o, c), min(o, c)
                engulfs = l_top >= p_top and l_bot <= p_bot and (l_top - l_bot) > (p_top - p_bot)
                if engulfs and pc < po and c > o:
                    found.add("bullish_engulfing")
                if engulfs and pc > po and c < o:
                    found.add("bearish_engulfing")
                same = h == ph and lo == pl
                if h <= ph and lo >= pl and not same:
                    found.add("inside_bar")
                if h >= ph and lo <= pl and not same:
                    found.add("outside_bar")
    patterns = [p for p in _PATTERN_ORDER if p in found]
    score = sum(_PATTERN_SCORE.get(p, 0) for p in patterns)
    return CandleLabels(patterns=patterns, score=max(-3, min(3, score)))  # type: ignore[arg-type]


# ======================================================================= SMC
SWING_LEFT = SWING_RIGHT = 5
FVG_LOOKBACK = 20


def swing_points(candles: Sequence[Candle], left: int = SWING_LEFT,
                 right: int = SWING_RIGHT) -> tuple[list[int], list[int]]:
    """프랙탈 스윙(좌우 각 5봉). 고점: 왼쪽 5봉보다 엄격히 높고 오른쪽 5봉 이상(같은 고가가 이어지는
    고원은 첫 봉이 스윙). 저점은 대칭. 반환 (스윙 고점 인덱스들, 스윙 저점 인덱스들)."""
    highs: list[int] = []
    lows: list[int] = []
    n = len(candles)
    for i in range(left, n - right):
        hi = _num(candles[i].high)
        lo = _num(candles[i].low)
        lh = [_num(c.high) for c in candles[i - left:i]]
        rh = [_num(c.high) for c in candles[i + 1:i + right + 1]]
        ll = [_num(c.low) for c in candles[i - left:i]]
        rl = [_num(c.low) for c in candles[i + 1:i + right + 1]]
        if (hi is not None and all(v is not None and hi > v for v in lh)
                and all(v is not None and hi >= v for v in rh)):
            highs.append(i)
        if (lo is not None and all(v is not None and lo < v for v in ll)
                and all(v is not None and lo <= v for v in rl)):
            lows.append(i)
    return highs, lows


def structure_events(candles: Sequence[Candle]) -> list[tuple[int, str]]:
    """구조 돌파 이벤트 목록 [(봉 인덱스, 종류)].

    스윙은 오른쪽 5봉이 확정된 시점(i+5)부터 사용(선행편향 없음). 가장 최근 확정 스윙 고점을 종가가
    넘으면 상승 돌파, 스윙 저점을 종가가 깨면 하락 돌파. 직전 돌파 방향과 반대면 choch_*, 아니면 bos_*.
    각 스윙 수준은 한 번만 돌파된다.
    """
    highs, lows = swing_points(candles)
    hi_at = {i + SWING_RIGHT: i for i in highs}
    lo_at = {i + SWING_RIGHT: i for i in lows}
    events: list[tuple[int, str]] = []
    trend: str | None = None
    act_hi: float | None = None
    act_lo: float | None = None
    for t, c in enumerate(candles):
        if t in hi_at:
            act_hi = float(candles[hi_at[t]].high)
        if t in lo_at:
            act_lo = float(candles[lo_at[t]].low)
        close = _num(c.close)
        if close is None:
            continue
        if act_hi is not None and close > act_hi:
            events.append((t, "choch_up" if trend == "down" else "bos_up"))
            trend, act_hi = "up", None
        elif act_lo is not None and close < act_lo:
            events.append((t, "choch_down" if trend == "up" else "bos_down"))
            trend, act_lo = "down", None
    return events


def smc_structure(candles: Sequence[Candle]) -> str:
    """마지막 구조 이벤트 라벨. 이벤트 없으면 range, 스윙을 만들 수 없을 만큼 짧으면(<11봉) unknown."""
    if len(candles) < SWING_LEFT + SWING_RIGHT + 1:
        return "unknown"
    events = structure_events(candles)
    return events[-1][1] if events else "range"


def fvg(candles: Sequence[Candle], lookback: int = FVG_LOOKBACK) -> str:
    """최근 lookback 봉 안에서 형성된 미충전 3봉 갭의 방향(가장 최근 것).

    bullish: low[i] > high[i−2], 이후 어떤 봉의 저가도 high[i−2] 이하로 내려오지 않음(완전 충전 아님).
    bearish: high[i] < low[i−2], 이후 어떤 봉의 고가도 low[i−2] 이상으로 오르지 않음.
    """
    n = len(candles)
    for i in range(n - 1, max(2, n - lookback) - 1, -1):
        a, c = candles[i - 2], candles[i]
        ah, al, ch, cl = _num(a.high), _num(a.low), _num(c.high), _num(c.low)
        if None in (ah, al, ch, cl):
            continue
        later = candles[i + 1:]
        if cl > ah:  # type: ignore[operator]
            if not any((_num(k.low) is not None and float(k.low) <= ah) for k in later):  # type: ignore[operator]
                return "bullish"
        elif ch < al:  # type: ignore[operator]
            if not any((_num(k.high) is not None and float(k.high) >= al) for k in later):  # type: ignore[operator]
                return "bearish"
    return "none"


def zones(candles: Sequence[Candle], price: float | None) -> dict[str, Any]:
    """수요/공급 존과 거리(%).

    - 수요 존 = 마지막 **상승 돌파**(bos_up/choch_up) 직전의 마지막 약세 봉의 ``[low, high]`` (SPEC §17).
    - 공급 존 = 마지막 하락 돌파(bos_down/choch_down) 직전의 마지막 강세 봉의 ``[low, high]`` (대칭).
    - distance_to_demand_pct: 가격이 존 위면 (price−zone_high)/price·100, 존 안이면 0, 아래면 음수
      (price−zone_low)/price·100. distance_to_supply_pct: 가격이 존 아래면 (zone_low−price)/price·100,
      안이면 0, 가격이 이미 존 위로 올라섰으면 None (공급 존은 가격 위에 있다는 소비자 전제 —
      음수 거리는 technical 4단계 대기·smc 목표 캡을 오작동시킨다).
    """
    out: dict[str, Any] = {"nearest_demand_zone": None, "nearest_supply_zone": None,
                           "distance_to_demand_pct": None, "distance_to_supply_pct": None}
    events = structure_events(candles) if len(candles) >= SWING_LEFT + SWING_RIGHT + 1 else []
    p = _pos(price)
    last_up = next((t for t, k in reversed(events) if k in ("bos_up", "choch_up")), None)
    last_dn = next((t for t, k in reversed(events) if k in ("bos_down", "choch_down")), None)
    if last_up is not None:
        for j in range(last_up - 1, -1, -1):
            c = candles[j]
            if _candle_finite(c) and float(c.close) < float(c.open):
                zl, zh = float(c.low), float(c.high)
                out["nearest_demand_zone"] = [zl, zh]
                if p is not None:
                    if p > zh:
                        out["distance_to_demand_pct"] = (p - zh) / p * 100.0
                    elif p >= zl:
                        out["distance_to_demand_pct"] = 0.0
                    else:
                        out["distance_to_demand_pct"] = (p - zl) / p * 100.0
                break
    if last_dn is not None:
        for j in range(last_dn - 1, -1, -1):
            c = candles[j]
            if _candle_finite(c) and float(c.close) > float(c.open):
                zl, zh = float(c.low), float(c.high)
                out["nearest_supply_zone"] = [zl, zh]
                if p is not None:
                    if p < zl:
                        out["distance_to_supply_pct"] = (zl - p) / p * 100.0
                    elif p <= zh:
                        out["distance_to_supply_pct"] = 0.0
                    # 가격이 존 위: 거리 None (음수 거리 금지)
                break
    return out


def smc(candles: Sequence[Candle], price: float | None) -> SmcLabels | None:
    """SmcLabels. 캔들이 없으면 None."""
    if not candles:
        return None
    return SmcLabels(structure=smc_structure(candles), fvg_dir=fvg(candles), **zones(candles, price))  # type: ignore[arg-type]


# ======================================================================= 변동성·거래량
def log_returns(candles: Sequence[Candle]) -> list[tuple[int, float]]:
    """연속 봉의 로그수익률 [(봉 ts, ln(c_t/c_{t−1}))]. 양의 유한 종가 쌍만."""
    out: list[tuple[int, float]] = []
    for a, b in zip(candles, candles[1:]):
        ca, cb = _pos(a.close), _pos(b.close)
        if ca is not None and cb is not None:
            out.append((int(b.ts), math.log(cb / ca)))
    return out


def hv_pct_rank(candles_1h: Sequence[Candle], window: int = HV_WINDOW,
                min_windows: int = HV_MIN_WINDOWS) -> float | None:
    """1h 로그수익률 24봉 실현변동성의 현재 값이 가용 롤링 창 전체(현재 포함)에서 차지하는 백분위(0..100).
    백분위 = (현재 이하인 창 수)/(창 수)·100. 창이 100개 미만이면 None."""
    rets = [r for _, r in log_returns(candles_1h)]
    rvs: list[float] = []
    for j in range(window, len(rets) + 1):
        s = _std(rets[j - window:j])
        if s is not None:
            rvs.append(s)
    if len(rvs) < min_windows:
        return None
    cur = rvs[-1]
    return sum(1 for v in rvs if v <= cur) / len(rvs) * 100.0


def _vwap(candles: Sequence[Candle]) -> float | None:
    pv = vol = 0.0
    for c in candles:
        h, lo, cl, v = _num(c.high), _num(c.low), _num(c.close), _num(c.volume)
        if None in (h, lo, cl, v) or v < 0:  # type: ignore[operator]
            continue
        pv += (h + lo + cl) / 3.0 * v  # type: ignore[operator]
        vol += v  # type: ignore[operator]
    return pv / vol if vol > 0 else None


def cgo_60(price: float | None, candles_1h: Sequence[Candle], n: int = 60) -> float | None:
    """price / VWAP(최근 1h 60봉) − 1 (%). 60봉 미만·거래량 0 → None."""
    p = _pos(price)
    if p is None or len(candles_1h) < n:
        return None
    v = _vwap(candles_1h[-n:])
    return (p / v - 1.0) * 100.0 if v else None


def abnormal_volume_multiple(candles_15m: Sequence[Candle], n: int = 96) -> float | None:
    """마지막 15m 거래량 / 직전 96봉 거래량 중앙값. 97봉 미만·중앙값 0 → None."""
    if len(candles_15m) < n + 1:
        return None
    last = _num(candles_15m[-1].volume)
    prev = [v for v in (_num(c.volume) for c in candles_15m[-n - 1:-1]) if v is not None]
    med = _median(prev)
    if last is None or med is None or med <= 0:
        return None
    return last / med


def volume_vs_hour_avg(candles_15m: Sequence[Candle], n: int = 4) -> float | None:
    """마지막 15m 거래량 / 직전 4봉 평균. 5봉 미만·평균 0 → None."""
    if len(candles_15m) < n + 1:
        return None
    last = _num(candles_15m[-1].volume)
    prev = [_num(c.volume) for c in candles_15m[-n - 1:-1]]
    if last is None or any(v is None for v in prev):
        return None
    m = sum(prev) / n  # type: ignore[arg-type]
    return last / m if m > 0 else None


def _change_pct_from_candles(candles: Sequence[Candle], bars: int) -> float | None:
    if len(candles) <= bars:
        return None
    a, b = _pos(candles[-1 - bars].close), _pos(candles[-1].close)
    return (b / a - 1.0) * 100.0 if (a and b) else None


def _ticker_change_pct(ticker: Ticker | None, candles_1h: Sequence[Candle]) -> float | None:
    """24h 변화(%). Ticker.change_24h_pct(소수)를 우선, 없으면 1h 24봉 전 대비."""
    if ticker is not None:
        v = _num(ticker.change_24h_pct)
        if v is not None:
            return v * 100.0
    return _change_pct_from_candles(candles_1h, 24)


# ======================================================================= FeaturePack
def build_feature_pack(symbol: str, candles_by_tf: dict[str, list[Candle]],
                       indicators_by_tf: dict[str, IndicatorSet], ticker: Ticker) -> FeaturePack:
    """피처팩 생성. 15m 캔들이 비어 있으면 ValueError. 다른 타임프레임은 있는 것만 쓴다.
    indicators_by_tf 에 없는 타임프레임은 캔들로 직접 계산한다."""
    c15 = list(candles_by_tf.get("15m") or [])
    if not c15:
        raise ValueError(f"{symbol}: 15m 캔들이 비어 있음")
    indicators_by_tf = indicators_by_tf or {}
    c1h = list(candles_by_tf.get("1h") or [])
    c4h = list(candles_by_tf.get("4h") or [])

    price = _pos(getattr(ticker, "last", None)) or _pos(c15[-1].close) or 0.0

    inds: dict[str, IndicatorSet | None] = {}
    per_tf: dict[str, TechnicalLabels] = {}
    for tf in TIMEFRAMES:
        candles = list(candles_by_tf.get(tf) or [])
        ind = indicators_by_tf.get(tf)
        if ind is None and candles:
            ind = _safe_compute(symbol, tf, candles)
        inds[tf] = ind
        if candles or ind is not None:
            per_tf[tf] = technical_labels(tf, candles, ind)

    ind1h = inds.get("1h")
    atr1 = _num(ind1h.atr_pct) if ind1h is not None else None
    atr_pct_1h = atr1 * 100.0 if atr1 is not None else None
    ind15 = inds.get("15m")
    vw = _pos(ind15.vwap_session) if ind15 is not None else None
    vwap_dist = (price / vw - 1.0) * 100.0 if (vw and price > 0) else None

    return FeaturePack(
        symbol=symbol,
        ts=int(getattr(ticker, "ts", None) or c15[-1].ts),
        price=price,
        per_tf=per_tf,
        ichimoku_1h=ichimoku(c1h),
        ichimoku_4h=ichimoku(c4h),
        candles_15m=candle_patterns(c15),
        candles_1h=candle_patterns(c1h),
        smc_1h=smc(c1h, price),
        smc_4h=smc(c4h, price),
        mtf_agreement=mtf_agreement(per_tf),
        hv_pct_rank=hv_pct_rank(c1h),
        atr_pct_1h=atr_pct_1h,
        vwap_distance_pct=vwap_dist,
        volume_vs_hour_avg=volume_vs_hour_avg(c15),
        cgo_60=cgo_60(price, c1h),
        abnormal_volume_multiple=abnormal_volume_multiple(c15),
        last_closes_1h=[v for v in (_num(c.close) for c in c1h[-20:]) if v is not None],
        change_24h_pct=_ticker_change_pct(ticker, c1h),
        noise_floor_stop_pct=atr_pct_1h,
        noise_ok_2pct=stop_vs_atr_noise_ok(atr_pct_1h),
    )


# ======================================================================= FlowPack
def funding_regime(funding: float | None) -> str:
    """8h 펀딩: >0.05% extreme_long / ≥0.02% elevated_long / ≥−0.01% neutral / ≥−0.02% negative / 그 아래 squeeze."""
    f = _num(funding)
    if f is None:
        return "unknown"
    th = FLOW_THRESHOLDS
    if f > th["funding_extreme_long"]:
        return "extreme_long"
    if f >= th["funding_elevated_long"]:
        return "elevated_long"
    if f >= th["funding_negative"]:
        return "neutral"
    if f >= th["funding_squeeze"]:
        return "negative"
    return "squeeze"


def oi_funding_label(oi_change_pct: float | None, funding: float | None) -> str:
    """OI ≥ +5% ∧ 펀딩 > 0.03% → leveraged_long_buildup ; OI ≥ +5% ∧ 펀딩 < 0 → short_buildup ;
    OI ≤ −5% → deleveraging ; 그 외 neutral. OI 변화 없음(또는 OI 급증인데 펀딩 없음) → unknown."""
    oi, f = _num(oi_change_pct), _num(funding)
    th = FLOW_THRESHOLDS
    if oi is None:
        return "unknown"
    if oi <= th["oi_deleveraging_pct"]:
        return "deleveraging"
    if oi >= th["oi_buildup_pct"]:
        if f is None:
            return "unknown"
        if f > th["oi_buildup_funding"]:
            return "leveraged_long_buildup"
        if f < 0:
            return "short_buildup"
    return "neutral"


def ls_label(ratio: float | None) -> str:
    """롱숏비 > 2.0 crowded_long, < 0.8 crowded_short, 그 외 balanced, 없으면 unknown."""
    r = _num(ratio)
    if r is None:
        return "unknown"
    if r > FLOW_THRESHOLDS["ls_crowded_long"]:
        return "crowded_long"
    if r < FLOW_THRESHOLDS["ls_crowded_short"]:
        return "crowded_short"
    return "balanced"


def divergence_flags(price_change_24h_pct: float | None, oi_change_24h_pct: float | None,
                     funding_now: float | None, funding_7d_mean: float | None) -> list[str]:
    """괴리 플래그(정해진 순서).
    price_up_oi_down: 가격 > +1% ∧ OI < −2% ; price_down_oi_up: 가격 < −1% ∧ OI > +2% ;
    funding_up_price_flat: funding_now − 7d 평균 > 0.01% ∧ |가격| < 1%."""
    p, oi = _num(price_change_24h_pct), _num(oi_change_24h_pct)
    fn, fm = _num(funding_now), _num(funding_7d_mean)
    flags: list[str] = []
    if p is not None and oi is not None:
        if p > DIVERGENCE_PRICE_PCT and oi < -DIVERGENCE_OI_PCT:
            flags.append("price_up_oi_down")
        if p < -DIVERGENCE_PRICE_PCT and oi > DIVERGENCE_OI_PCT:
            flags.append("price_down_oi_up")
    if p is not None and fn is not None and fm is not None:
        if fn - fm > DIVERGENCE_FUNDING_RISE and abs(p) < DIVERGENCE_FLAT_PRICE_PCT:
            flags.append("funding_up_price_flat")
    return flags


def vwap_24h(candles_1h: Sequence[Candle]) -> float | None:
    """최근 24개 1h 봉의 VWAP(전형가 기준). 봉 없음·거래량 0 → None."""
    if not candles_1h:
        return None
    return _vwap(candles_1h[-24:])


def est_liq_clusters(vwap: float | None) -> list[dict]:
    """추정 롱 청산 군집: level = vwap·(1 − 1/lev + 0.005), lev ∈ {25, 50, 100}."""
    v = _pos(vwap)
    if v is None:
        return []
    return [{"level": v * (1.0 - 1.0 / lev + LIQ_MAINT_MARGIN), "side": "long", "leverage": lev, "estimated": True}
            for lev in LIQ_LEVERAGES]


def nearest_long_liq_below_pct(price: float | None, clusters: Iterable[Mapping[str, Any]]) -> float | None:
    """가격 아래에 있는 롱 청산 수준까지의 최소 거리(%). 아래에 없으면 None."""
    p = _pos(price)
    if p is None:
        return None
    dists = []
    for c in clusters:
        lvl = _num(c.get("level"))
        if c.get("side", "long") == "long" and lvl is not None and lvl < p:
            dists.append((p - lvl) / p * 100.0)
    return min(dists) if dists else None


def cascade_risk(nearest_pct: float | None, oi_label: str, regime: str, has_clusters: bool) -> str:
    """high: nearest<2% ∧ leveraged_long_buildup ; moderate: nearest<3% ∨ extreme_long ;
    군집을 추정할 수 없으면 unknown ; 그 외 low."""
    d = _num(nearest_pct)
    th = FLOW_THRESHOLDS
    if d is not None and d < th["cascade_high_distance_pct"] and oi_label == "leveraged_long_buildup":
        return "high"
    if (d is not None and d < th["cascade_moderate_distance_pct"]) or regime == "extreme_long":
        return "moderate"
    if not has_clusters:
        return "unknown"
    return "low"


def oir_1pct(orderbook: OrderBookSummary | None) -> float | None:
    """(bid_depth − ask_depth)/(bid_depth + ask_depth), 1% 깊이 기준."""
    if orderbook is None:
        return None
    b, a = _num(orderbook.bid_depth_usdt_1pct), _num(orderbook.ask_depth_usdt_1pct)
    if b is None or a is None or b < 0 or a < 0 or (b + a) <= 0:
        return None
    return (b - a) / (b + a)


def spread_bps(orderbook: OrderBookSummary | None, ticker: Ticker | None = None) -> float | None:
    """스프레드(bps). 호가 요약 spread_pct(소수)×1e4, 없으면 티커 bid/ask 로 계산."""
    if orderbook is not None:
        s = _num(orderbook.spread_pct)
        if s is not None and s >= 0:
            return s * 1e4
    if ticker is not None:
        b, a = _pos(ticker.bid), _pos(ticker.ask)
        if b is not None and a is not None and a >= b:
            return (a - b) / ((a + b) / 2.0) * 1e4
    return None


def data_confidence(funding: float | None, open_interest: float | None) -> str:
    """펀딩·OI 둘 다 있으면 high, 하나면 low, 없으면 none."""
    n = sum(1 for v in (_num(funding), _num(open_interest)) if v is not None)
    return ("none", "low", "high")[n]


def build_flow_pack(symbol: str, futures: FuturesContext | None, funding_history: list[float],
                    orderbook: OrderBookSummary | None, ticker: Ticker, perp_mark: float | None,
                    candles_1h: list[Candle], prev_oi_usdt: float | None) -> FlowPack:
    """파생/자금흐름 팩. 선물 데이터가 없어도(None) 예외 없이 unknown/none 으로 채운다."""
    candles_1h = list(candles_1h or [])
    f_now = _num(futures.funding_rate) if futures is not None else None
    oi_now = _pos(futures.open_interest_usdt) if futures is not None else None
    ls_r = _num(futures.long_short_ratio) if futures is not None else None
    hist = [v for v in (_num(x) for x in (funding_history or [])) if v is not None]
    f_mean = _mean(hist)
    prev_oi = _pos(prev_oi_usdt)
    oi_chg = (oi_now / prev_oi - 1.0) * 100.0 if (oi_now is not None and prev_oi is not None) else None
    spot = _pos(getattr(ticker, "last", None)) or (_pos(candles_1h[-1].close) if candles_1h else None)
    mark = _pos(perp_mark)
    premium = (mark - spot) / spot * 100.0 if (mark is not None and spot is not None) else None
    regime = funding_regime(f_now)
    oi_lbl = oi_funding_label(oi_chg, f_now)
    clusters = est_liq_clusters(vwap_24h(candles_1h))
    nearest = nearest_long_liq_below_pct(spot, clusters)
    return FlowPack(
        symbol=symbol,
        funding_now=f_now,
        funding_7d_mean=f_mean,
        funding_annualized_pct=f_now * 3 * 365 * 100 if f_now is not None else None,
        funding_regime=regime,  # type: ignore[arg-type]
        oi_change_24h_pct=oi_chg,
        oi_funding_label=oi_lbl,  # type: ignore[arg-type]
        perp_premium_pct=premium,
        ls_ratio=ls_r,
        ls_label=ls_label(ls_r),  # type: ignore[arg-type]
        divergence_flags=divergence_flags(_ticker_change_pct(ticker, candles_1h), oi_chg, f_now, f_mean),
        est_liq_clusters=clusters,
        nearest_long_liq_below_pct=nearest,
        cascade_risk=cascade_risk(nearest, oi_lbl, regime, bool(clusters)),  # type: ignore[arg-type]
        oir_1pct=oir_1pct(orderbook),
        spread_bps=spread_bps(orderbook, ticker),
        data_confidence=data_confidence(f_now, oi_now),  # type: ignore[arg-type]
    )


# ======================================================================= RiskPack
def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    """선형 보간 분위수(numpy 기본 'linear'). 입력은 정렬·비어 있지 않음."""
    h = (len(sorted_xs) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (h - lo) * (sorted_xs[hi] - sorted_xs[lo])


def _clean(returns: Iterable[Any]) -> list[float]:
    return [v for v in (_num(r) for r in returns) if v is not None]


def historical_var(returns: Sequence[float], alpha: float = 0.05,
                   min_samples: int = VAR_MIN_SAMPLES) -> float | None:
    """히스토리컬 VaR(%, 손실을 양수로): −quantile(alpha)·100, 0 미만은 0. 표본 부족 → None."""
    xs = _clean(returns)
    if len(xs) < max(2, min_samples) or not (0 < alpha < 1):
        return None
    return max(0.0, -_quantile(sorted(xs), alpha) * 100.0)


def historical_cvar(returns: Sequence[float], alpha: float = 0.05,
                    min_samples: int = VAR_MIN_SAMPLES) -> float | None:
    """히스토리컬 CVaR(%): quantile(alpha) 이하 수익률 평균의 −100배, 0 미만은 0."""
    xs = _clean(returns)
    if len(xs) < max(2, min_samples) or not (0 < alpha < 1):
        return None
    s = sorted(xs)
    q = _quantile(s, alpha)
    tail = [x for x in s if x <= q] or [s[0]]
    return max(0.0, -(sum(tail) / len(tail)) * 100.0)


def realized_vol_pct(returns: Sequence[float], n: int) -> float | None:
    """연율화 실현변동성(%): std(최근 n개 1h 수익률, 표본)·sqrt(24·365)·100. n개 미만 → None."""
    xs = _clean(returns)
    if n < 2 or len(xs) < n:
        return None
    s = _std(xs[-n:])
    return s * ANNUALIZE_1H * 100.0 if s is not None else None


def daily_vol_pct(returns: Sequence[float], n: int = 96) -> float | None:
    """일간 변동성(%): std(최근 96개 1h 수익률)·sqrt(24)·100. 96개 미만 → None."""
    xs = _clean(returns)
    if len(xs) < n:
        return None
    s = _std(xs[-n:])
    return s * math.sqrt(24) * 100.0 if s is not None else None


def excess_kurtosis(returns: Sequence[float], min_samples: int = VAR_MIN_SAMPLES) -> float | None:
    """초과 첨도(모집단): m4/m2² − 3. 분산 0·표본 부족 → None."""
    xs = _clean(returns)
    n = len(xs)
    if n < max(4, min_samples):
        return None
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 <= 0:
        return None
    m4 = sum((x - m) ** 4 for x in xs) / n
    return m4 / (m2 * m2) - 3.0


def aligned_returns(candles_a: Sequence[Candle], candles_b: Sequence[Candle],
                    n: int) -> tuple[list[float], list[float]]:
    """두 시계열의 1봉 로그수익률을 봉 ts 로 맞춰 최근 n개(공통 ts 오름차순)."""
    ra = dict(log_returns(candles_a))
    rb = dict(log_returns(candles_b))
    common = sorted(set(ra) & set(rb))[-n:] if n > 0 else []
    return [ra[t] for t in common], [rb[t] for t in common]


def rolling_corr(candles_a: Sequence[Candle], candles_b: Sequence[Candle], n: int = CORR_WINDOW_RISK,
                 min_overlap: int = CORR_MIN_RISK) -> float | None:
    """ts 정렬된 최근 n개 1h 수익률의 피어슨 상관. 겹침 < min_overlap 또는 분산 0 → None."""
    xs, ys = aligned_returns(candles_a, candles_b, n)
    if len(xs) < max(2, min_overlap):
        return None
    return _pearson(xs, ys)


def rolling_beta(candles: Sequence[Candle], bench: Sequence[Candle], n: int = CORR_WINDOW_RISK,
                 min_overlap: int = CORR_MIN_RISK) -> float | None:
    """베타 = cov(x, bench)/var(bench), 같은 정렬·창."""
    xs, ys = aligned_returns(candles, bench, n)
    k = len(xs)
    if k < max(2, min_overlap):
        return None
    mx, my = sum(xs) / k, sum(ys) / k
    var_b = sum((y - my) ** 2 for y in ys)
    if var_b <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var_b


def inverse_vol_size_pct(daily_vol: float | None) -> float | None:
    """min(10, 0.5/daily_vol_pct·100). daily_vol 없음/≤0 → None."""
    d = _pos(daily_vol)
    return min(10.0, 0.5 / d * 100.0) if d is not None else None


def cvar_size_pct(cvar95: float | None) -> float | None:
    """min(10, 0.15/cvar95_1h_pct·100). cvar 없음/≤0 → None."""
    c = _pos(cvar95)
    return min(10.0, 0.15 / c * 100.0) if c is not None else None


def impact_bps_est(planned_notional: float | None, depth_taker_usdt: float | None) -> float | None:
    """planned_notional / depth_taker_usdt · 100."""
    p, d = _num(planned_notional), _pos(depth_taker_usdt)
    if p is None or p < 0 or d is None:
        return None
    return p / d * 100.0


def order_notional_vs_15m_volume_share(planned_notional: float | None,
                                       quote_volume_24h: float | None) -> float | None:
    """planned_notional / (quote_volume_24h/96)."""
    p, q = _num(planned_notional), _pos(quote_volume_24h)
    if p is None or p < 0 or q is None:
        return None
    return p / (q / 96.0)


def stop_vs_atr_noise_ok(atr_pct_1h: float | None) -> bool | None:
    """atr_pct_1h(%)·1.5 < 2.0. 없으면 None."""
    a = _num(atr_pct_1h)
    return (a * 1.5 < 2.0) if a is not None else None


def exec_cost_pct(impact_bps: float | None, spread: float | None) -> float | None:
    """impact_bps_est/100 + spread_bps/200 + 0.20(왕복 수수료) (%). 둘 중 하나라도 없으면 None."""
    i, s = _num(impact_bps), _num(spread)
    if i is None or s is None:
        return None
    return i / 100.0 + s / 200.0 + 0.20


def build_risk_pack(symbol: str, candles_1h: list[Candle], btc_candles_1h: list[Candle],
                    open_candles_1h: dict[str, list[Candle]], orderbook: OrderBookSummary | None,
                    ticker: Ticker, planned_notional: float, atr_pct_1h: float | None) -> RiskPack:
    """리스크 팩. atr_pct_1h 는 % 단위(FeaturePack.atr_pct_1h)."""
    candles_1h = list(candles_1h or [])
    rets_all = [r for _, r in log_returns(candles_1h)]
    rets = rets_all[-VAR_WINDOW:]
    cvar95 = historical_cvar(rets, 0.05)
    dvol = daily_vol_pct(rets_all)
    btc = list(btc_candles_1h or [])
    corr_open: dict[str, float] = {}
    for sym in sorted(open_candles_1h or {}):
        if sym == symbol:
            continue
        c = rolling_corr(candles_1h, open_candles_1h[sym] or [])
        if c is not None:
            corr_open[sym] = c
    sp = spread_bps(orderbook, ticker)
    depth = _num(orderbook.ask_depth_usdt_1pct) if orderbook is not None else None
    impact = impact_bps_est(planned_notional, depth)
    return RiskPack(
        symbol=symbol,
        var95_1h_pct=historical_var(rets, 0.05),
        var99_1h_pct=historical_var(rets, 0.01),
        cvar95_1h_pct=cvar95,
        rv_20_pct=realized_vol_pct(rets_all, 20),
        rv_60_pct=realized_vol_pct(rets_all, 60),
        daily_vol_pct=dvol,
        hv_pct_rank=hv_pct_rank(candles_1h),
        kurtosis=excess_kurtosis(rets),
        corr_to_btc_1h=rolling_corr(candles_1h, btc) if btc else None,
        beta_btc=rolling_beta(candles_1h, btc) if btc else None,
        corr_to_open_positions=corr_open,
        inverse_vol_size_pct=inverse_vol_size_pct(dvol),
        cvar_size_pct=cvar_size_pct(cvar95),
        spread_bps=sp,
        depth_taker_usdt=depth,
        impact_bps_est=impact,
        order_notional_vs_15m_volume_share=order_notional_vs_15m_volume_share(
            planned_notional, getattr(ticker, "quote_volume_24h", None)),
        stop_vs_atr_noise_ok=stop_vs_atr_noise_ok(atr_pct_1h),
        exec_cost_pct=exec_cost_pct(impact, sp),
    )


# ======================================================================= RegimePack
def avg_pairwise_corr(candles_1h_by_symbol: Mapping[str, Sequence[Candle]], n: int = CORR_WINDOW_REGIME,
                      min_overlap: int = CORR_MIN_REGIME, dense_thr: float = 0.7) -> tuple[float | None, float | None]:
    """(평균 쌍상관, density=상관>0.7 쌍 비율). 최근 n개 ts 정렬 1h 수익률. 유효 심볼 < 3 또는 쌍 없음 → (None, None)."""
    syms = sorted(s for s, c in candles_1h_by_symbol.items() if c and len(log_returns(c)) >= min_overlap)
    if len(syms) < 3:
        return None, None
    corrs: list[float] = []
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            r = rolling_corr(candles_1h_by_symbol[a], candles_1h_by_symbol[b], n, min_overlap)
            if r is not None:
                corrs.append(r)
    if not corrs:
        return None, None
    return sum(corrs) / len(corrs), sum(1 for r in corrs if r > dense_thr) / len(corrs)


def btc_regime_4h(ind: IndicatorSet | None) -> str:
    """ema50>ema200 ∧ adx>20 → bull ; adx<20 → range ; 그 외 bear ; 값 없으면 unknown."""
    if ind is None:
        return "unknown"
    e50, e200, adx_v = _num(ind.ema_50), _num(ind.ema_200), _num(ind.adx_14)
    if e50 is None or e200 is None or adx_v is None:
        return "unknown"
    if e50 > e200 and adx_v > 20:
        return "bull"
    if adx_v < 20:
        return "range"
    return "bear"


def _ret_atr_1h(candles: Sequence[Candle] | None, ind: IndicatorSet | None = None) -> tuple[float | None, float | None]:
    """(마지막 1h 단순수익률, ATR%) 둘 다 소수. ind 값을 우선, 없으면 캔들로 계산."""
    ret = _num(ind.return_1) if ind is not None else None
    atrp = _num(ind.atr_pct) if ind is not None else None
    candles = list(candles or [])
    if ret is None and len(candles) >= 2:
        a, b = _pos(candles[-2].close), _pos(candles[-1].close)
        ret = b / a - 1.0 if (a and b) else None
    if atrp is None and len(candles) >= 15:
        try:
            atr_v = _last_value(ind_mod.atr(candles, 14))
        except (ValueError, TypeError):  # pragma: no cover - 방어
            atr_v = None
        close = _pos(candles[-1].close)
        atrp = atr_v / close if (atr_v is not None and close) else None
    return ret, atrp


def build_regime_pack(candles_1h_by_symbol: dict[str, list[Candle]], btc_symbol: str,
                      btc_ind_4h: IndicatorSet | None, btc_ind_1h: IndicatorSet | None) -> RegimePack:
    """시장 레짐 팩. BTC 데이터가 없으면 btc_regime_4h unknown, btc_shock False, name alarm 없음."""
    data = candles_1h_by_symbol or {}
    avg, density = avg_pairwise_corr(data)
    fused = avg is not None and avg > 0.8
    btc_ret, btc_atr = _ret_atr_1h(data.get(btc_symbol), btc_ind_1h)
    btc_shock = bool(btc_ret is not None and btc_atr is not None and btc_atr > 0 and abs(btc_ret) > 2 * btc_atr)
    alarms: list[str] = []
    btc_calm = btc_ret is not None and btc_atr is not None and abs(btc_ret) < btc_atr
    if btc_calm:
        for sym in sorted(data):
            if sym == btc_symbol:
                continue
            r, a = _ret_atr_1h(data[sym])
            if r is not None and a is not None and a > 0 and abs(r) > 3 * a:
                alarms.append(sym)
    verdict = "macro_alarm" if (btc_shock and fused) else ("fused" if fused else "normal")
    return RegimePack(
        avg_pairwise_corr_1h=avg, density=density, fused=fused,
        btc_regime_4h=btc_regime_4h(btc_ind_4h),  # type: ignore[arg-type]
        btc_shock=btc_shock, name_alarm_symbols=alarms, verdict=verdict,  # type: ignore[arg-type]
    )


# ======================================================================= 종합 심리
def sentiment_label(score: float | None) -> str:
    """<20 extreme_fear, <40 fear, <60 neutral, <80 greed, ≥80 extreme_greed, None → unknown."""
    s = _num(score)
    if s is None:
        return "unknown"
    if s < 20:
        return "extreme_fear"
    if s < 40:
        return "fear"
    if s < 60:
        return "neutral"
    if s < 80:
        return "greed"
    return "extreme_greed"


def _clip100(x: float) -> float:
    return max(0.0, min(100.0, x))


def composite_sentiment(fear_greed: int | None, funding_z: float | None, ls_pctile: float | None,
                        headline_polarity: float | None) -> SentimentComposite:
    """가중 합(0..100). 각 성분을 0..100 으로 정규화한 뒤 가중치 fear_greed 0.4, funding_z 0.2, ls_pctile 0.2,
    headline_polarity 0.2 — 없는 성분은 빼고 가중치를 재정규화.

    정규화: fear_greed 는 그대로(0..100 클립), funding_z 는 표준정규 CDF×100, ls_pctile 은 0..100 백분위
    그대로(클립), headline_polarity(−1..1)는 (p+1)·50. ``components`` 에는 **0..100 정규화 값**을 담는다
    (skills/sentiment-analysis 와 같은 척도; 키 4개 고정, 없는 성분은 None).
    """
    raw = {"fear_greed": _num(fear_greed), "funding_z": _num(funding_z),
           "ls_pctile": _num(ls_pctile), "headline_polarity": _num(headline_polarity)}
    norm: dict[str, float] = {}
    if raw["fear_greed"] is not None:
        norm["fear_greed"] = _clip100(raw["fear_greed"])
    if raw["funding_z"] is not None:
        norm["funding_z"] = _clip100(50.0 * (1.0 + math.erf(raw["funding_z"] / math.sqrt(2.0))))
    if raw["ls_pctile"] is not None:
        norm["ls_pctile"] = _clip100(raw["ls_pctile"])
    if raw["headline_polarity"] is not None:
        norm["headline_polarity"] = _clip100((max(-1.0, min(1.0, raw["headline_polarity"])) + 1.0) * 50.0)
    components: dict[str, float | None] = {k: norm.get(k) for k in _SENTIMENT_WEIGHTS}
    if not norm:
        return SentimentComposite(score_0_100=None, label="unknown", components=components)
    wsum = sum(_SENTIMENT_WEIGHTS[k] for k in norm)
    score = sum(_SENTIMENT_WEIGHTS[k] * v for k, v in norm.items()) / wsum
    return SentimentComposite(score_0_100=score, label=sentiment_label(score), components=components)  # type: ignore[arg-type]
