"""features.py (SPEC §17 + v2.1 보완) — 라벨 함수별 합성 캔들 테스트. 전부 오프라인·결정적."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from pydantic import ValidationError

from tradingbot import features as F
from tradingbot import indicators as I
from tradingbot.models import Candle, FuturesContext, IndicatorSet, OrderBookSummary, Ticker

H = 3_600_000
M15 = 900_000


# ----------------------------------------------------------------- 합성 캔들 도우미
def bar(ts, o, h, lo, c, v=10.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=v)


def from_closes(closes, step=H, wick=0.2, vols=None, start=0) -> list[Candle]:
    """open = 직전 종가, high/low = 몸통 ± wick."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        v = vols[i] if vols is not None else 10.0
        out.append(bar(start + i * step, prev, max(prev, c) + wick, min(prev, c) - wick, c, v))
        prev = c
    return out


def flat_bars(levels, step=H, start=0) -> list[Candle]:
    """범위 0 봉(open=high=low=close) — 일목 중간값을 정확히 통제하기 위해."""
    return [bar(start + i * step, x, x, x, x) for i, x in enumerate(levels)]


def from_log_returns(rets, p0=100.0, step=H, start=0, vol=10.0) -> list[Candle]:
    closes = [p0]
    for r in rets:
        closes.append(closes[-1] * math.exp(r))
    return from_closes(closes, step=step, wick=0.0, vols=[vol] * len(closes), start=start)


def waypoints(points) -> list[float]:
    """[(bar, price), ...] 사이를 선형 보간한 종가 리스트."""
    out: list[float] = []
    for (b0, p0), (b1, p1) in zip(points, points[1:]):
        for b in range(b0, b1):
            out.append(p0 + (p1 - p0) * (b - b0) / (b1 - b0))
    out.append(points[-1][1])
    return out


def geometric(n, g=1.005, p0=100.0):
    return [p0 * g ** i for i in range(n)]


def wiggle_trend(n, g=1.004, amp=0.006, p0=100.0):
    """추세 + 결정적 흔들림(상승/하락 봉 혼재)."""
    return [p0 * g ** i * (1 + amp * math.sin(i * 1.7)) for i in range(n)]


def ticker(last, ts=0, change=None, qv=None, bid=None, ask=None, symbol="ETH/USDT") -> Ticker:
    return Ticker(symbol=symbol, last=last, bid=bid, ask=ask, quote_volume_24h=qv, change_24h_pct=change, ts=ts)


def ind(**kw) -> IndicatorSet:
    base = dict(symbol="X", timeframe="1h", ts=0, close=100.0)
    base.update(kw)
    return IndicatorSet(**base)


def book(bid_depth=100_000.0, ask_depth=100_000.0, spread_pct=0.0002) -> OrderBookSummary:
    return OrderBookSummary(symbol="ETH/USDT", best_bid=99.99, best_ask=100.01, spread_pct=spread_pct,
                            bid_depth_usdt_1pct=bid_depth, ask_depth_usdt_1pct=ask_depth, ts=0)


# ================================================================= compact / _r
class TestCompact:
    def test_round_4_significant(self):
        assert F._r(123456.789) == 123500.0
        assert F._r(1.23456) == 1.235
        assert F._r(0.000123456) == 0.0001235
        assert F._r(-9.87654) == -9.877

    def test_r_none_nan_inf_zero(self):
        assert F._r(None) is None
        assert F._r(float("nan")) is None
        assert F._r(float("inf")) is None
        assert F._r(0.0) == 0.0
        z = F._r(-0.0)
        assert z == 0.0 and math.copysign(1, z) == 1

    def test_compact_recursive_and_sorted(self):
        out = F.compact({"b": [1.23456, {"z": 2.34567, "a": None}], "a": 7, "c": True, "d": "txt"})
        assert list(out) == ["a", "b", "c", "d"]
        assert out["a"] == 7 and isinstance(out["a"], int)
        assert out["c"] is True
        assert out["b"][0] == 1.235
        assert list(out["b"][1]) == ["a", "z"]
        assert out["b"][1]["z"] == 2.346 and out["b"][1]["a"] is None

    def test_compact_model_and_tuple(self):
        lab = F.TechnicalLabels(timeframe="1h", rsi_14=55.55555, adx_14=None)
        out = F.compact(lab)
        assert isinstance(out, dict)
        assert out["rsi_14"] == 55.56 and out["adx_14"] is None
        assert out == F.compact(lab)  # 결정적
        assert list(out) == sorted(out)
        assert F.compact((1.11111, 2)) == [1.111, 2]

    def test_compact_nested_models_and_nan(self):
        fp = F.FeaturePack(symbol="X", ts=1_700_000_000_123, price=float("nan"),
                           per_tf={"1h": F.TechnicalLabels(timeframe="1h", rsi_14=33.33333)})
        out = F.compact(fp)
        assert out["price"] is None
        assert out["ts"] == 1_700_000_000_123  # int 는 반올림하지 않음
        assert out["per_tf"]["1h"]["rsi_14"] == 33.33
        json.dumps(out, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def test_compact_alias_dump(self):
        from tradingbot.agents.schemas import RiskOutput
        ro = RiskOutput.conservative_example()
        out = F.compact(ro)
        assert "global" in out and "global_" not in out


# ================================================================= 모델
class TestModels:
    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            F.TechnicalLabels(timeframe="1h", bogus=1)
        with pytest.raises(ValidationError):
            F.RiskPack(symbol="X", nope=True)
        with pytest.raises(ValidationError):
            F.RegimePack(extra_field=1)

    def test_literal_validation(self):
        with pytest.raises(ValidationError):
            F.TechnicalLabels(timeframe="1h", ema_state="sideways")
        with pytest.raises(ValidationError):
            F.CandleLabels(patterns=["morning_star"], score=0)
        with pytest.raises(ValidationError):
            F.CandleLabels(patterns=[], score=4)

    def test_v21_fields_present(self):
        assert "noise_floor_stop_pct" in F.FeaturePack.model_fields
        assert "noise_ok_2pct" in F.FeaturePack.model_fields
        assert "exec_cost_pct" in F.RiskPack.model_fields

    def test_spec_field_sets(self):
        assert set(F.TechnicalLabels.model_fields) == {
            "timeframe", "ema_state", "price_vs_ema20_pct", "adx_14", "rsi_14", "rsi_5", "bb_position",
            "macd_hist_sign", "obv_trend", "volume_ratio_20", "atr_pct", "vote"}
        assert set(F.IchimokuLabels.model_fields) == {"tk_cross", "price_vs_cloud", "cloud_bullish", "cross_strength"}
        assert set(F.SmcLabels.model_fields) == {
            "structure", "fvg_dir", "nearest_demand_zone", "nearest_supply_zone",
            "distance_to_demand_pct", "distance_to_supply_pct"}
        assert set(F.FeaturePack.model_fields) == {
            "symbol", "ts", "price", "per_tf", "ichimoku_1h", "ichimoku_4h", "candles_15m", "candles_1h",
            "smc_1h", "smc_4h", "mtf_agreement", "hv_pct_rank", "atr_pct_1h", "vwap_distance_pct",
            "volume_vs_hour_avg", "cgo_60", "abnormal_volume_multiple", "last_closes_1h", "change_24h_pct",
            "noise_floor_stop_pct", "noise_ok_2pct"}
        assert set(F.FlowPack.model_fields) == {
            "symbol", "funding_now", "funding_7d_mean", "funding_annualized_pct", "funding_regime",
            "oi_change_24h_pct", "oi_funding_label", "perp_premium_pct", "ls_ratio", "ls_label",
            "divergence_flags", "est_liq_clusters", "nearest_long_liq_below_pct", "cascade_risk",
            "oir_1pct", "spread_bps", "data_confidence"}
        assert set(F.RiskPack.model_fields) == {
            "symbol", "var95_1h_pct", "var99_1h_pct", "cvar95_1h_pct", "rv_20_pct", "rv_60_pct",
            "daily_vol_pct", "hv_pct_rank", "kurtosis", "corr_to_btc_1h", "beta_btc",
            "corr_to_open_positions", "inverse_vol_size_pct", "cvar_size_pct", "spread_bps",
            "depth_taker_usdt", "impact_bps_est", "order_notional_vs_15m_volume_share",
            "stop_vs_atr_noise_ok", "exec_cost_pct"}
        assert set(F.RegimePack.model_fields) == {
            "avg_pairwise_corr_1h", "density", "fused", "btc_regime_4h", "btc_shock",
            "name_alarm_symbols", "verdict"}
        assert set(F.SentimentComposite.model_fields) == {"score_0_100", "label", "components"}

    def test_flow_thresholds_constant(self):
        assert F.FLOW_THRESHOLDS == {
            "funding_extreme_long": 0.0005, "funding_elevated_long": 0.0002, "funding_negative": -0.0001,
            "funding_squeeze": -0.0002, "oi_buildup_pct": 5.0, "oi_buildup_funding": 0.0003,
            "oi_deleveraging_pct": -5.0, "ls_crowded_long": 2.0, "ls_crowded_short": 0.8,
            "cascade_high_distance_pct": 2.0, "cascade_moderate_distance_pct": 3.0}


# ================================================================= 기술 라벨
class TestEmaState:
    def test_bull_bear_mixed(self):
        assert F.ema_state(110, 105, 100) == "bull_aligned"
        assert F.ema_state(90, 95, 100) == "bear_aligned"
        assert F.ema_state(102, 105, 100) == "mixed"
        assert F.ema_state(105, 105, 100) == "mixed"  # 같음은 정렬 아님

    def test_ema200_participates_when_present(self):
        assert F.ema_state(110, 105, 100, 95) == "bull_aligned"
        assert F.ema_state(110, 105, 100, 101) == "mixed"
        assert F.ema_state(90, 95, 100, 105) == "bear_aligned"
        assert F.ema_state(90, 95, 100, 99) == "mixed"

    def test_unknown(self):
        assert F.ema_state(100, None, 90) == "unknown"
        assert F.ema_state(100, 95, None) == "unknown"
        assert F.ema_state(None, 95, 90) == "unknown"
        assert F.ema_state(float("nan"), 95, 90) == "unknown"


class TestMacdBb:
    def test_macd_sign(self):
        assert F.macd_hist_sign(0.5, 100) == "pos"
        assert F.macd_hist_sign(-0.5, 100) == "neg"
        assert F.macd_hist_sign(1e-8, 100) == "flat"      # < 1e-9*100 = 1e-7
        assert F.macd_hist_sign(-5e-8, 100) == "flat"
        assert F.macd_hist_sign(2e-7, 100) == "pos"
        assert F.macd_hist_sign(None, 100) == "unknown"
        assert F.macd_hist_sign(0.1, None) == "unknown"

    def test_bb_position_inside_outside(self):
        assert F.bb_position(100, 110, 90) == pytest.approx(0.5)
        assert F.bb_position(90, 110, 90) == pytest.approx(0.0)
        assert F.bb_position(85, 110, 90) == pytest.approx(-0.25)
        assert F.bb_position(120, 110, 90) == pytest.approx(1.5)

    def test_bb_position_degenerate(self):
        assert F.bb_position(100, 100, 100) is None
        assert F.bb_position(100, 90, 110) is None
        assert F.bb_position(100, None, 90) is None
        assert F.bb_position(float("inf"), 110, 90) is None


class TestVote:
    def test_trend_up_and_down(self):
        assert F.vote("bull_aligned", 25, "pos", 60, 0.7) == "trend_up"
        assert F.vote("bear_aligned", 25, "neg", 40, 0.3) == "trend_down"

    def test_trend_requires_all_three(self):
        assert F.vote("bull_aligned", 20, "pos", 60, 0.7) == "none"   # adx>20 엄격
        assert F.vote("bull_aligned", 25, "neg", 60, 0.7) == "none"
        assert F.vote("bull_aligned", 25, "flat", 60, 0.7) == "none"
        assert F.vote("mixed", 40, "pos", 60, 0.7) == "none"
        assert F.vote("bull_aligned", None, "pos", 60, 0.7) == "none"
        assert F.vote("bear_aligned", 25, "pos", 40, 0.3) == "none"

    def test_mean_reversion(self):
        assert F.vote("mixed", 15, "neg", 25, 0.01) == "mean_reversion_long"
        assert F.vote("mixed", 15, "pos", 75, 0.99) == "mean_reversion_short"
        assert F.vote("mixed", 15, "neg", 30, 0.01) == "none"      # rsi<30 엄격
        assert F.vote("mixed", 15, "neg", 25, 0.05) == "none"      # bb<0.05 엄격
        assert F.vote("mixed", 15, "pos", 70, 0.99) == "none"
        assert F.vote("mixed", 15, "pos", 75, 0.95) == "none"
        assert F.vote("mixed", 15, "neg", 25, -0.3) == "mean_reversion_long"   # 밴드 밖
        assert F.vote("mixed", 15, "neg", None, 0.0) == "none"
        assert F.vote("mixed", 15, "neg", 25, None) == "none"

    def test_trend_beats_mean_reversion(self):
        assert F.vote("bull_aligned", 30, "pos", 85, 1.2) == "trend_up"
        assert F.vote("bear_aligned", 30, "neg", 15, -0.2) == "trend_down"


class TestObv:
    def test_obv_series(self):
        cs = from_closes([10, 11, 11, 9, 12], vols=[5, 1, 2, 3, 4])
        assert F.obv(cs) == [0.0, 1.0, 1.0, -2.0, 2.0]

    def test_obv_skips_non_finite(self):
        cs = from_closes([10, 11, 12], vols=[1, 1, 1])
        cs[1] = bar(cs[1].ts, 10, 11, 10, 11, float("nan"))
        assert F.obv(cs) == [0.0, 0.0, 1.0]

    def test_trend_up_down(self):
        up = from_closes([100 + i for i in range(30)])
        dn = from_closes([100 - i for i in range(30)])
        assert F.obv_trend(up) == "up"
        assert F.obv_trend(dn) == "down"

    def test_flat_when_no_change(self):
        assert F.obv_trend(from_closes([100.0] * 25)) == "flat"

    def test_flat_threshold_relative_to_mean_obv(self):
        # 큰 누적 OBV(+1e6) 뒤 미세 변화: 기울기 < 평균 OBV*1e-4 → flat
        closes = [100.0, 101.0] + [101.0 + (0.01 if i % 2 else 0) for i in range(25)]
        vols = [1.0, 1e6] + [1.0] * 25
        assert F.obv_trend(from_closes(closes, vols=vols)) == "flat"

    def test_uses_last_20_least_squares(self):
        # 앞쪽은 하락, 최근 20봉은 상승 → up
        closes = [200 - i for i in range(40)] + [160 + i for i in range(20)]
        assert F.obv_trend(from_closes(closes)) == "up"

    def test_short(self):
        assert F.obv_trend(from_closes([1.0 + i for i in range(19)])) == "unknown"
        assert F.obv_trend([]) == "unknown"
        assert F.obv_trend(from_closes([1.0 + i for i in range(20)])) == "up"

    def test_ls_slope_known(self):
        assert F._ls_slope([0, 2, 4, 6]) == pytest.approx(2.0)
        assert F._ls_slope([5, 5, 5]) == 0.0


class TestTechnicalLabels:
    def test_uptrend_labels(self):
        cs = from_closes(geometric(260, 1.004), wick=0.1)
        lab = F.technical_labels("1h", cs, I.compute("X", "1h", cs))
        assert lab.timeframe == "1h"
        assert lab.ema_state == "bull_aligned"
        assert lab.macd_hist_sign == "pos"
        assert lab.obv_trend == "up"
        assert lab.adx_14 is not None and lab.adx_14 > 20
        assert lab.vote == "trend_up"
        assert lab.price_vs_ema20_pct is not None and lab.price_vs_ema20_pct > 0
        assert lab.rsi_5 == pytest.approx(100.0)

    def test_downtrend_labels(self):
        cs = from_closes([300 - 0.3 * i - 0.002 * i * i for i in range(260)], wick=0.05)  # 가속 하락
        lab = F.technical_labels("4h", cs, I.compute("X", "4h", cs))
        assert lab.macd_hist_sign == "neg"
        assert lab.ema_state == "bear_aligned"
        assert lab.vote == "trend_down"
        assert lab.obv_trend == "down"

    def test_units_percent(self):
        cs = from_closes(wiggle_trend(120), wick=0.3)
        ix = I.compute("X", "1h", cs)
        lab = F.technical_labels("1h", cs, ix)
        assert lab.atr_pct == pytest.approx(ix.atr_pct * 100)
        assert lab.price_vs_ema20_pct == pytest.approx((ix.close / ix.ema_20 - 1) * 100)
        assert lab.bb_position == pytest.approx((ix.close - ix.bb_lower) / (ix.bb_upper - ix.bb_lower))
        assert lab.volume_ratio_20 == ix.volume_ratio_20

    def test_no_indicators(self):
        lab = F.technical_labels("1d", [], None)
        assert lab.ema_state == "unknown" and lab.vote == "none" and lab.obv_trend == "unknown"
        assert lab.macd_hist_sign == "unknown" and lab.rsi_5 is None

    def test_short_series_no_ema200(self):
        cs = from_closes(geometric(60, 1.004))
        lab = F.technical_labels("1d", cs, I.compute("X", "1d", cs))
        assert lab.ema_state == "bull_aligned"  # ema200 없으면 close>ema20>ema50 만


class TestMtfAgreement:
    @staticmethod
    def labs(s4="bull_aligned", v1="trend_up", v15="none", s1d=None):
        d = {"4h": F.TechnicalLabels(timeframe="4h", ema_state=s4),
             "1h": F.TechnicalLabels(timeframe="1h", vote=v1),
             "15m": F.TechnicalLabels(timeframe="15m", vote=v15)}
        if s1d is not None:
            d["1d"] = F.TechnicalLabels(timeframe="1d", ema_state=s1d)
        return d

    def test_true_cases(self):
        assert F.mtf_agreement(self.labs()) is True
        assert F.mtf_agreement(self.labs(v1="mean_reversion_long")) is True
        assert F.mtf_agreement(self.labs(s4="mixed", s1d="bull_aligned")) is True
        assert F.mtf_agreement(self.labs(v15="trend_up")) is True
        assert F.mtf_agreement(self.labs(v15="mean_reversion_short")) is True

    def test_false_cases(self):
        assert F.mtf_agreement(self.labs(s4="mixed")) is False
        assert F.mtf_agreement(self.labs(s4="mixed", s1d="mixed")) is False
        assert F.mtf_agreement(self.labs(s4="bear_aligned", s1d="bull_aligned")) is False
        assert F.mtf_agreement(self.labs(v1="none")) is False
        assert F.mtf_agreement(self.labs(v1="trend_down")) is False
        assert F.mtf_agreement(self.labs(v15="trend_down")) is False
        assert F.mtf_agreement(self.labs(s4="unknown")) is False

    def test_missing_timeframes(self):
        d = self.labs()
        del d["15m"]
        assert F.mtf_agreement(d) is False
        assert F.mtf_agreement({}) is False


# ================================================================= 일목
def _ichi_shape(base: list[float]) -> list[float]:
    """bars 0..63 = base, 64..88 = 100(70번 120, 72번 90, 85번 110), 89 = 140 → 마지막 봉에서 상승 교차.

    t−1: 전환(80..88) = (110+100)/2 = 105, 기준(63..88) = (120+90)/2 = 105 → 차 0
    t  : 전환(81..89) = (140+100)/2 = 120, 기준(64..89) = (140+90)/2 = 115 → 차 +5, 교차 수준 117.5
    현재 구름 = 63번 봉의 선행스팬.
    """
    assert len(base) == 64
    mid = [100.0] * 25
    mid[70 - 64] = 120.0
    mid[72 - 64] = 90.0
    mid[85 - 64] = 110.0
    return list(base) + mid + [140.0]


class TestIchimoku:
    def test_lines_known_values(self):
        cs = flat_bars(_ichi_shape([30.0 + i for i in range(64)]))
        L = F.ichimoku_lines(cs)
        assert L["tenkan"][63] == pytest.approx((93 + 85) / 2)
        assert L["kijun"][63] == pytest.approx((93 + 68) / 2)
        assert L["span_a_raw"][63] == pytest.approx(((93 + 85) / 2 + (93 + 68) / 2) / 2)
        assert L["span_b_raw"][63] == pytest.approx((93 + 42) / 2)
        assert L["tenkan"][7] is None and L["tenkan"][8] is not None
        assert L["kijun"][24] is None and L["kijun"][25] is not None
        assert L["span_b_raw"][50] is None and L["span_b_raw"][51] is not None
        assert L["tenkan"][89] == pytest.approx(120.0) and L["kijun"][89] == pytest.approx(115.0)
        assert L["tenkan"][88] == pytest.approx(105.0) and L["kijun"][88] == pytest.approx(105.0)

    def test_strong_bullish_cross(self):
        lab = F.ichimoku(flat_bars(_ichi_shape([30.0 + i for i in range(64)])))
        assert lab is not None
        assert lab.tk_cross == "bullish"
        assert lab.cloud_bullish is True       # A 84.75 > B 67.5
        assert lab.price_vs_cloud == "above"
        assert lab.cross_strength == "strong"

    def test_weak_bullish_cross_below_cloud(self):
        lab = F.ichimoku(flat_bars(_ichi_shape([230.0 - i for i in range(64)])))
        assert lab.tk_cross == "bullish"
        assert lab.cloud_bullish is False      # A 175.25 < B 192.5
        assert lab.price_vs_cloud == "below"   # 140 < 175.25
        assert lab.cross_strength == "weak"

    def test_weak_inside_cloud(self):
        lab = F.ichimoku(flat_bars(_ichi_shape([117.5] * 64)))
        assert lab.tk_cross == "bullish"
        assert lab.cross_strength == "weak"  # 교차 수준 117.5 == 구름(두께 0) → inside → weak (skills/ichimoku)
        assert lab.cloud_bullish is False
        assert lab.price_vs_cloud == "above"

    def test_medium_above_bearish_cloud(self):
        # 140 − 0.75i : A = 98.94 < B = 111.875, 둘 다 교차 수준 117.5 아래
        lab = F.ichimoku(flat_bars(_ichi_shape([140.0 - 0.75 * i for i in range(64)])))
        assert lab.tk_cross == "bullish"
        assert lab.cloud_bullish is False
        assert lab.cross_strength == "medium"  # 구름 위지만 구름 방향 불일치

    def test_bearish_mirror_strong(self):
        levels = [300.0 - x for x in _ichi_shape([30.0 + i for i in range(64)])]
        lab = F.ichimoku(flat_bars(levels))
        assert lab.tk_cross == "bearish"
        assert lab.cloud_bullish is False
        assert lab.price_vs_cloud == "below"
        assert lab.cross_strength == "strong"

    def test_bearish_mirror_weak(self):
        levels = [300.0 - x for x in _ichi_shape([230.0 - i for i in range(64)])]
        lab = F.ichimoku(flat_bars(levels))
        assert lab.tk_cross == "bearish"
        assert lab.cross_strength == "weak"

    def test_cross_window_is_last_3_bars(self):
        base = _ichi_shape([30.0 + i for i in range(64)])
        within = F.ichimoku(flat_bars(base + [140.0, 140.0]))       # 교차가 t−2
        assert within.tk_cross == "bullish"
        stale = F.ichimoku(flat_bars(base + [140.0, 140.0, 140.0]))  # 교차가 t−3
        assert stale.tk_cross == "none" and stale.cross_strength == "none"

    def test_current_cloud_is_span_from_26_bars_ago(self):
        # 100×30, 200×30, 130×26 : 26봉 전 구름 [150, 200] (강세) → 130 은 below.
        # 이동하지 않은(현재 봉) 스팬이라면 A=130, B=165 → inside/약세 로 다르게 나온다.
        cs = flat_bars([100.0] * 30 + [200.0] * 30 + [130.0] * 26)
        lab = F.ichimoku(cs)
        assert lab.price_vs_cloud == "below"
        assert lab.cloud_bullish is True
        L = F.ichimoku_lines(cs)
        assert L["span_a_raw"][-1] == pytest.approx(130.0)
        assert L["span_b_raw"][-1] == pytest.approx(165.0)

    def test_warmup(self):
        assert F.ichimoku(flat_bars([100.0] * 77)) is None
        lab = F.ichimoku(flat_bars([100.0] * 78))
        assert lab is not None and lab.price_vs_cloud == "inside" and lab.tk_cross == "none"
        assert F.ichimoku([]) is None

    def test_non_finite_does_not_raise(self):
        cs = flat_bars([100.0 + i for i in range(90)])
        cs[-1] = bar(cs[-1].ts, 1, float("nan"), 1, float("nan"))
        assert F.ichimoku(cs) is None
        cs2 = flat_bars([100.0 + i for i in range(90)])
        cs2[40] = bar(cs2[40].ts, 1, float("inf"), 1, 1)
        F.ichimoku(cs2)  # 예외 없음


# ================================================================= 캔들 패턴
class TestCandlePatterns:
    def test_bullish_engulfing(self):
        cs = [bar(0, 105, 106, 99, 100), bar(1, 99, 107, 98, 106)]
        lab = F.candle_patterns(cs)
        assert "bullish_engulfing" in lab.patterns
        assert "outside_bar" in lab.patterns
        assert lab.score == 1

    def test_bearish_engulfing(self):
        cs = [bar(0, 100, 106, 99, 105), bar(1, 106, 107, 98, 99)]
        lab = F.candle_patterns(cs)
        assert "bearish_engulfing" in lab.patterns and lab.score == -1

    def test_engulfing_needs_opposite_colours_and_cover(self):
        same_colour = [bar(0, 100, 106, 99, 105), bar(1, 99, 108, 98, 107)]
        assert "bullish_engulfing" not in F.candle_patterns(same_colour).patterns
        not_cover = [bar(0, 105, 106, 99, 100), bar(1, 101, 107, 98, 106)]  # 몸통 아래쪽이 덜 감쌈
        assert "bullish_engulfing" not in F.candle_patterns(not_cover).patterns
        equal_body = [bar(0, 105, 106, 99, 100), bar(1, 100, 106, 99, 105)]
        assert "bullish_engulfing" not in F.candle_patterns(equal_body).patterns

    def test_hammer(self):
        # body 1 (100→101), lower wick 3 (≥2×), upper wick 0.5 (≤ body)
        lab = F.candle_patterns([bar(0, 100, 101.5, 97, 101)])
        assert "hammer" in lab.patterns
        assert "shooting_star" not in lab.patterns
        assert lab.score >= 1

    def test_hammer_boundaries(self):
        exact = F.candle_patterns([bar(0, 100, 102, 98, 101)])  # lower 2 = 2×1, upper 1 = body
        assert "hammer" in exact.patterns
        too_short = F.candle_patterns([bar(0, 100, 101.5, 98.1, 101)])  # lower 1.9
        assert "hammer" not in too_short.patterns
        big_upper = F.candle_patterns([bar(0, 100, 102.5, 97, 101)])  # upper 1.5 > body
        assert "hammer" not in big_upper.patterns

    def test_shooting_star(self):
        lab = F.candle_patterns([bar(0, 101, 104, 99.5, 100)])  # body 1, upper 3, lower 0.5
        assert "shooting_star" in lab.patterns and "hammer" not in lab.patterns
        assert lab.score <= -1

    def test_doji(self):
        lab = F.candle_patterns([bar(0, 100, 105, 95, 100.5)])  # body 0.5 ≤ 10% of 10
        assert "doji" in lab.patterns
        not_doji = F.candle_patterns([bar(0, 100, 105, 95, 101.1)])
        assert "doji" not in not_doji.patterns

    def test_pin_bars(self):
        bull = F.candle_patterns([bar(0, 103, 104, 90, 104)])   # lower 13 / range 14 = 0.93
        assert "pin_bar_bull" in bull.patterns
        bear = F.candle_patterns([bar(0, 91, 104, 90, 90)])     # upper 13 / 14
        assert "pin_bar_bear" in bear.patterns
        edge = F.candle_patterns([bar(0, 100, 100, 0.0 + 66, 100)])  # lower 34/34 = 1.0
        assert "pin_bar_bull" in edge.patterns
        below = F.candle_patterns([bar(0, 100, 101, 70, 100.5)])  # lower 30/31 ok
        assert "pin_bar_bull" in below.patterns
        no = F.candle_patterns([bar(0, 100, 110, 96, 105)])  # lower 4/14
        assert "pin_bar_bull" not in no.patterns and "pin_bar_bear" not in no.patterns

    def test_pin_bar_ratio_boundary(self):
        # range 100, lower wick 66 → 정확히 66% (포함), 65 → 미포함
        yes = F.candle_patterns([bar(0, 166, 200, 100, 170)])
        assert "pin_bar_bull" in yes.patterns
        no = F.candle_patterns([bar(0, 165, 200, 100, 170)])
        assert "pin_bar_bull" not in no.patterns

    def test_inside_outside(self):
        inside = F.candle_patterns([bar(0, 100, 110, 90, 105), bar(1, 101, 108, 92, 104)])
        assert "inside_bar" in inside.patterns and "outside_bar" not in inside.patterns
        outside = F.candle_patterns([bar(0, 100, 108, 92, 104), bar(1, 103, 110, 90, 102)])
        assert "outside_bar" in outside.patterns and "inside_bar" not in outside.patterns
        same = F.candle_patterns([bar(0, 100, 110, 90, 105), bar(1, 100, 110, 90, 101)])
        assert "inside_bar" not in same.patterns and "outside_bar" not in same.patterns

    def test_only_last_two_bars(self):
        # 오래된 강세 장악형은 무시
        cs = [bar(0, 105, 106, 99, 100), bar(1, 99, 107, 98, 106), bar(2, 106, 108, 104, 107.5),
              bar(3, 107.5, 108.5, 106, 108)]
        assert "bullish_engulfing" not in F.candle_patterns(cs).patterns

    def test_score_clip_and_canonical_order(self):
        # 장악형 + 망치형 + 강세 핀바 → +3 (클립 범위 안)
        cs = [bar(0, 100.4, 100.5, 99.9, 100.0), bar(1, 99.9, 100.6, 90.0, 100.5)]
        lab = F.candle_patterns(cs)
        assert lab.patterns == [p for p in F._PATTERN_ORDER if p in lab.patterns]
        assert {"bullish_engulfing", "hammer", "pin_bar_bull"} <= set(lab.patterns)
        assert lab.score == 3

    def test_empty_and_degenerate(self):
        assert F.candle_patterns([]).patterns == [] and F.candle_patterns([]).score == 0
        zero_range = F.candle_patterns([bar(0, 100, 100, 100, 100)])
        assert zero_range.patterns == []
        nan = F.candle_patterns([bar(0, 100, float("nan"), 99, 100)])
        assert nan.patterns == [] and nan.score == 0


# ================================================================= SMC
def path_bos_up():
    return from_closes(waypoints([(0, 100), (10, 110), (20, 100), (35, 120), (45, 105), (60, 125)]))


def path_choch_up():
    return from_closes(waypoints([(0, 120), (10, 110), (20, 115), (35, 100), (45, 108), (52, 104), (62, 112)]))


class TestSwingsAndStructure:
    def test_swing_points_simple(self):
        hs = [1, 2, 3, 4, 5, 10, 5, 4, 3, 2, 1, 0]
        cs = [bar(i, h, h, h - 0.5, h) for i, h in enumerate(hs)]
        highs, lows = F.swing_points(cs)
        assert highs == [5] and lows == []

    def test_swing_needs_5_each_side(self):
        hs = [1, 2, 3, 4, 5, 10, 5, 4, 3, 2]  # 오른쪽 4봉뿐
        cs = [bar(i, h, h, h - 0.5, h) for i, h in enumerate(hs)]
        assert F.swing_points(cs) == ([], [])

    def test_swing_rejects_equal_left(self):
        hs = [0, 1, 2, 3, 4, 10, 10, 5, 4, 3, 2, 1, 0]
        cs = [bar(i, h, h, h - 0.5, h) for i, h in enumerate(hs)]
        highs, _ = F.swing_points(cs)
        assert highs == [5]  # 고원은 첫 봉만

    def test_bos_up(self):
        cs = path_bos_up()
        highs, lows = F.swing_points(cs)
        assert 10 in highs and 35 in highs and 20 in lows and 45 in lows
        ev = F.structure_events(cs)
        assert [k for _, k in ev] == ["bos_up", "bos_up"]
        assert F.smc_structure(cs) == "bos_up"

    def test_choch_up_after_downtrend(self):
        cs = path_choch_up()
        ev = F.structure_events(cs)
        assert [k for _, k in ev] == ["bos_down", "choch_up"]
        assert ev[-1][0] == 58  # 108.8 > 108.2 (45번 스윙 고점)
        assert F.smc_structure(cs) == "choch_up"

    def test_down_structures(self):
        cs = from_closes([300 - x for x in waypoints(
            [(0, 100), (10, 110), (20, 100), (35, 120), (45, 105), (60, 125)])])
        assert F.smc_structure(cs) == "bos_down"
        cs2 = from_closes([300 - x for x in waypoints(
            [(0, 120), (10, 110), (20, 115), (35, 100), (45, 108), (52, 104), (62, 112)])])
        assert [k for _, k in F.structure_events(cs2)] == ["bos_up", "choch_down"]
        assert F.smc_structure(cs2) == "choch_down"

    def test_range_and_unknown(self):
        osc = from_closes([100 + 2 * math.sin(i / 3.0) for i in range(80)])
        assert F.smc_structure(osc) == "range"
        assert F.smc_structure(from_closes([100.0 + i for i in range(10)])) == "unknown"
        assert F.smc_structure([]) == "unknown"

    def test_no_lookahead_prefix_consistency(self):
        cs = path_choch_up()
        full = F.structure_events(cs)
        for k in range(12, len(cs)):
            pre = F.structure_events(cs[:k])
            assert pre == [e for e in full if e[0] < k]


class TestFvg:
    @staticmethod
    def base(n=25):
        return [bar(i, 99.5, 100.0, 99.0, 99.5) for i in range(n)]

    def test_bullish_unfilled(self):
        cs = self.base()
        n = len(cs)
        cs += [bar(n, 99.5, 102.5, 99.4, 102.0), bar(n + 1, 102.0, 103.0, 101.0, 102.5),
               bar(n + 2, 102.5, 103.0, 100.6, 102.8)]
        assert F.fvg(cs) == "bullish"

    def test_bullish_filled(self):
        cs = self.base()
        n = len(cs)
        cs += [bar(n, 99.5, 102.5, 99.4, 102.0), bar(n + 1, 102.0, 103.0, 101.0, 102.5),
               bar(n + 2, 102.5, 103.0, 99.9, 100.2)]
        assert F.fvg(cs) == "none"

    def test_bearish(self):
        cs = self.base()
        n = len(cs)
        cs += [bar(n, 99.5, 99.6, 96.5, 97.0), bar(n + 1, 97.0, 98.0, 96.0, 96.5)]
        assert F.fvg(cs) == "bearish"  # high 98 < low 99.0 (2봉 전)
        cs.append(bar(n + 2, 96.5, 99.2, 96.4, 99.0))  # 고가 99.2 ≥ 99.0 → 충전
        assert F.fvg(cs) == "none"

    def test_only_last_20_bars(self):
        cs = self.base(5)
        n = len(cs)
        cs += [bar(n, 99.5, 102.5, 99.4, 102.0), bar(n + 1, 102.0, 103.0, 101.0, 102.5)]
        cs += [bar(n + 2 + i, 102.5, 103.0, 101.5, 102.5) for i in range(19)]
        assert F.fvg(cs) == "bullish"     # 갭 3번째 봉이 정확히 최근 20봉 안
        cs.append(bar(len(cs), 102.5, 103.0, 101.5, 102.5))
        assert F.fvg(cs) == "none"        # 21봉 전

    def test_most_recent_wins(self):
        cs = self.base()
        n = len(cs)
        cs += [bar(n, 99.5, 102.5, 99.4, 102.0), bar(n + 1, 102.0, 103.0, 101.0, 102.5)]  # bullish (low 101 > 100)
        cs += [bar(n + 2, 102.5, 102.6, 101.2, 101.5), bar(n + 3, 101.5, 101.6, 101.1, 101.2),
               bar(n + 4, 101.2, 101.3, 101.05, 101.1)]
        # 마지막: high 101.3 < low(n+2)=101.2? 아니오 → bearish 아님 ; bullish 는 여전히 미충전
        assert F.fvg(cs) == "bullish"

    def test_short(self):
        assert F.fvg([]) == "none"
        assert F.fvg(self.base(2)) == "none"


class TestZones:
    def test_demand_zone_is_last_bearish_before_bullish_break(self):
        cs = path_choch_up()
        z = F.zones(cs, cs[-1].close)
        assert z["nearest_demand_zone"] == [cs[52].low, cs[52].high]
        assert cs[52].close < cs[52].open
        # 공급 존: bos_down(31번 근처) 직전 마지막 강세 봉 = 20번(115까지 상승) [low, high]
        bos_dn_idx = F.structure_events(cs)[0][0]
        j = max(i for i in range(bos_dn_idx) if cs[i].close > cs[i].open)
        assert z["nearest_supply_zone"] == [cs[j].low, cs[j].high]

    def test_distance_demand_above_inside_below(self):
        cs = path_choch_up()
        lo, hi = F.zones(cs, 1.0)["nearest_demand_zone"]
        p_above = hi * 1.02
        assert F.zones(cs, p_above)["distance_to_demand_pct"] == pytest.approx((p_above - hi) / p_above * 100)
        assert F.zones(cs, (lo + hi) / 2)["distance_to_demand_pct"] == 0.0
        p_below = lo * 0.99
        d = F.zones(cs, p_below)["distance_to_demand_pct"]
        assert d < 0 and d == pytest.approx((p_below - lo) / p_below * 100)

    def test_distance_supply(self):
        cs = path_choch_up()
        lo, hi = F.zones(cs, 1.0)["nearest_supply_zone"]
        p = lo * 0.97
        assert F.zones(cs, p)["distance_to_supply_pct"] == pytest.approx((lo - p) / p * 100)
        assert F.zones(cs, (lo + hi) / 2)["distance_to_supply_pct"] == 0.0
        assert F.zones(cs, hi * 1.05)["distance_to_supply_pct"] is None  # 가격이 존 위 → 음수 대신 None

    def test_bos_up_only_has_demand(self):
        z = F.zones(path_bos_up(), 125.0)
        assert z["nearest_demand_zone"] is not None and z["nearest_supply_zone"] is None
        assert z["distance_to_supply_pct"] is None

    def test_no_price(self):
        z = F.zones(path_choch_up(), None)
        assert z["nearest_demand_zone"] is not None and z["distance_to_demand_pct"] is None

    def test_smc_wrapper(self):
        cs = path_choch_up()
        lab = F.smc(cs, cs[-1].close)
        assert lab.structure == "choch_up" and lab.nearest_demand_zone is not None
        assert F.smc([], 100.0) is None
        short = F.smc(from_closes([100.0, 101.0]), 101.0)
        assert short.structure == "unknown" and short.nearest_demand_zone is None


# ================================================================= 변동성·거래량
class TestHvPctRank:
    @staticmethod
    def alt(n, a):
        return [a if i % 2 else -a for i in range(n)]

    def test_spike_at_end_is_top(self):
        cs = from_log_returns(self.alt(150, 0.001) + self.alt(24, 0.01))
        assert F.hv_pct_rank(cs) == pytest.approx(100.0)

    def test_calm_end_is_low(self):
        cs = from_log_returns(self.alt(100, 0.01) + self.alt(74, 0.001))
        r = F.hv_pct_rank(cs)
        assert r is not None and r < 50

    def test_min_windows_boundary(self):
        rets = [0.001 * math.sin(i) for i in range(200)]
        assert F.hv_pct_rank(from_log_returns(rets[:123])) is not None   # 124봉 → 100창
        assert F.hv_pct_rank(from_log_returns(rets[:122])) is None       # 123봉 → 99창
        assert F.hv_pct_rank([]) is None

    def test_matches_reference(self):
        rets = [0.002 * math.sin(i * 0.7) + 0.001 * math.cos(i * 1.3) for i in range(199)]
        cs = from_log_returns(rets)
        lr = np.diff(np.log([c.close for c in cs]))
        rvs = [np.std(lr[j - 24:j], ddof=1) for j in range(24, len(lr) + 1)]
        ref = sum(1 for v in rvs if v <= rvs[-1]) / len(rvs) * 100
        assert F.hv_pct_rank(cs) == pytest.approx(ref)


class TestVolumeAndVwap:
    def test_cgo_60(self):
        cs = [bar(i * H, 100, 101, 99, 100, 5.0) for i in range(60)]  # 전형가 100
        assert F.cgo_60(103.0, cs) == pytest.approx(3.0)
        assert F.cgo_60(97.0, cs) == pytest.approx(-3.0)
        assert F.cgo_60(103.0, cs[:59]) is None
        zero = [bar(i * H, 100, 101, 99, 100, 0.0) for i in range(60)]
        assert F.cgo_60(103.0, zero) is None
        assert F.cgo_60(None, cs) is None

    def test_cgo_uses_last_60_volume_weighted(self):
        old = [bar(i * H, 50, 50, 50, 50, 1000.0) for i in range(40)]
        recent = [bar((40 + i) * H, 100, 100, 100, 100, 1.0) for i in range(30)] + \
                 [bar((70 + i) * H, 110, 110, 110, 110, 3.0) for i in range(30)]
        vw = (100 * 30 + 110 * 90) / 120
        assert F.cgo_60(110.0, old + recent) == pytest.approx((110 / vw - 1) * 100)

    def test_abnormal_volume_multiple(self):
        vols = [float(i % 7 + 1) for i in range(96)] + [40.0]
        cs = from_closes([100.0] * 97, step=M15, vols=vols)
        assert F.abnormal_volume_multiple(cs) == pytest.approx(40.0 / float(np.median(vols[:96])))
        assert F.abnormal_volume_multiple(cs[1:]) is None
        zeros = from_closes([100.0] * 97, step=M15, vols=[0.0] * 96 + [5.0])
        assert F.abnormal_volume_multiple(zeros) is None

    def test_volume_vs_hour_avg(self):
        cs = from_closes([100.0] * 6, step=M15, vols=[99, 1, 2, 3, 4, 5])
        assert F.volume_vs_hour_avg(cs) == pytest.approx(5 / 2.5)
        assert F.volume_vs_hour_avg(cs[:4]) is None
        assert F.volume_vs_hour_avg(from_closes([1.0] * 5, vols=[0, 0, 0, 0, 3])) is None


# ================================================================= FeaturePack
def full_inputs(n15=300, n1h=200, n4h=200, n1d=120, g=1.002):
    return {
        "15m": from_closes(wiggle_trend(n15, g ** 0.25, 0.002), step=M15, start=10 * 86_400_000,
                           vols=[10 + (i % 5) for i in range(n15)]),
        "1h": from_closes(wiggle_trend(n1h, g, 0.004), vols=[10 + (i % 3) for i in range(n1h)]),
        "4h": from_closes(wiggle_trend(n4h, g * 1.002, 0.006), step=4 * H),
        "1d": from_closes(wiggle_trend(n1d, g * 1.004, 0.01), step=24 * H),
    }


class TestBuildFeaturePack:
    def test_empty_15m_raises(self):
        with pytest.raises(ValueError):
            F.build_feature_pack("X", {"15m": [], "1h": from_closes([1.0, 2.0])}, {}, ticker(100.0))
        with pytest.raises(ValueError):
            F.build_feature_pack("X", {}, {}, ticker(100.0))

    def test_full_pack(self):
        cbt = full_inputs()
        inds = {tf: I.compute("ETH/USDT", tf, cs) for tf, cs in cbt.items()}
        t = ticker(cbt["15m"][-1].close, ts=123456789, change=0.034)
        fp = F.build_feature_pack("ETH/USDT", cbt, inds, t)
        assert fp.symbol == "ETH/USDT" and fp.ts == 123456789 and fp.price == t.last
        assert set(fp.per_tf) == {"15m", "1h", "4h", "1d"}
        assert fp.ichimoku_1h is not None and fp.ichimoku_4h is not None
        assert fp.smc_1h is not None and fp.smc_4h is not None
        assert fp.hv_pct_rank is not None
        assert fp.atr_pct_1h == pytest.approx(inds["1h"].atr_pct * 100)
        assert fp.noise_floor_stop_pct == fp.atr_pct_1h
        assert fp.noise_ok_2pct is (1.5 * fp.atr_pct_1h < 2.0)
        assert fp.vwap_distance_pct == pytest.approx((t.last / inds["15m"].vwap_session - 1) * 100)
        assert fp.change_24h_pct == pytest.approx(3.4)          # 소수 → %
        assert fp.last_closes_1h == [c.close for c in cbt["1h"][-20:]]
        assert fp.cgo_60 == pytest.approx(F.cgo_60(t.last, cbt["1h"]))
        assert fp.abnormal_volume_multiple == pytest.approx(F.abnormal_volume_multiple(cbt["15m"]))
        assert fp.volume_vs_hour_avg == pytest.approx(F.volume_vs_hour_avg(cbt["15m"]))
        assert fp.mtf_agreement == F.mtf_agreement(fp.per_tf)
        assert fp.candles_15m == F.candle_patterns(cbt["15m"])
        out = F.compact(fp)
        json.dumps(out, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def test_missing_indicators_are_computed(self):
        cbt = full_inputs()
        inds = {tf: I.compute("ETH/USDT", tf, cs) for tf, cs in cbt.items()}
        t = ticker(cbt["15m"][-1].close, ts=1)
        assert F.build_feature_pack("ETH/USDT", cbt, {}, t) == F.build_feature_pack("ETH/USDT", cbt, inds, t)

    def test_only_15m(self):
        cs = from_closes(wiggle_trend(50, 1.001), step=M15)
        fp = F.build_feature_pack("X", {"15m": cs}, {}, ticker(cs[-1].close, ts=5))
        assert set(fp.per_tf) == {"15m"}
        assert fp.ichimoku_1h is None and fp.ichimoku_4h is None
        assert fp.smc_1h is None and fp.smc_4h is None
        assert fp.hv_pct_rank is None and fp.atr_pct_1h is None
        assert fp.noise_floor_stop_pct is None and fp.noise_ok_2pct is None
        assert fp.candles_1h.patterns == [] and fp.cgo_60 is None
        assert fp.mtf_agreement is False
        assert fp.last_closes_1h == [] and fp.change_24h_pct is None

    def test_short_inputs_tolerated(self):
        cbt = {"15m": from_closes([100.0], step=M15), "1h": from_closes([100.0, 101.0]),
               "4h": from_closes([100.0]), "1d": []}
        fp = F.build_feature_pack("X", cbt, {}, ticker(101.0))
        assert "1d" not in fp.per_tf
        assert fp.per_tf["1h"].ema_state == "unknown"
        assert fp.volume_vs_hour_avg is None and fp.abnormal_volume_multiple is None

    def test_change_24h_fallback_from_1h(self):
        cbt = full_inputs()
        fp = F.build_feature_pack("X", cbt, {}, ticker(cbt["15m"][-1].close, change=None))
        c = cbt["1h"]
        assert fp.change_24h_pct == pytest.approx((c[-1].close / c[-25].close - 1) * 100)

    def test_price_fallback_when_ticker_bad(self):
        cs = from_closes(wiggle_trend(30), step=M15)
        fp = F.build_feature_pack("X", {"15m": cs}, {}, ticker(float("nan"), ts=9))
        assert fp.price == cs[-1].close

    def test_noise_ok_threshold(self):
        assert F.stop_vs_atr_noise_ok(1.3) is True
        assert F.stop_vs_atr_noise_ok(1.33) is True
        assert F.stop_vs_atr_noise_ok(1.34) is False
        assert F.stop_vs_atr_noise_ok(2.0 / 1.5) is False
        assert F.stop_vs_atr_noise_ok(1.4) is False
        assert F.stop_vs_atr_noise_ok(None) is None

    def test_deterministic(self):
        cbt = full_inputs()
        t = ticker(cbt["15m"][-1].close, ts=3)
        a = F.build_feature_pack("X", cbt, {}, t)
        b = F.build_feature_pack("X", cbt, {}, t)
        assert json.dumps(F.compact(a), sort_keys=True) == json.dumps(F.compact(b), sort_keys=True)


# ================================================================= FlowPack
class TestFlowLabels:
    def test_funding_regime_thresholds(self):
        assert F.funding_regime(0.0006) == "extreme_long"
        assert F.funding_regime(0.0005) == "elevated_long"
        assert F.funding_regime(0.0003) == "elevated_long"
        assert F.funding_regime(0.0002) == "elevated_long"
        assert F.funding_regime(0.00019) == "neutral"
        assert F.funding_regime(0.0) == "neutral"
        assert F.funding_regime(-0.0001) == "neutral"
        assert F.funding_regime(-0.00015) == "negative"
        assert F.funding_regime(-0.0002) == "negative"
        assert F.funding_regime(-0.00021) == "squeeze"
        assert F.funding_regime(None) == "unknown"
        assert F.funding_regime(float("nan")) == "unknown"

    def test_oi_funding_label(self):
        assert F.oi_funding_label(6.0, 0.0004) == "leveraged_long_buildup"
        assert F.oi_funding_label(5.0, 0.00031) == "leveraged_long_buildup"
        assert F.oi_funding_label(6.0, 0.0003) == "neutral"   # >0.03% 엄격
        assert F.oi_funding_label(6.0, -0.0001) == "short_buildup"
        assert F.oi_funding_label(6.0, 0.0) == "neutral"
        assert F.oi_funding_label(4.9, 0.001) == "neutral"
        assert F.oi_funding_label(-5.0, 0.001) == "deleveraging"
        assert F.oi_funding_label(-7.0, None) == "deleveraging"
        assert F.oi_funding_label(None, 0.001) == "unknown"
        assert F.oi_funding_label(8.0, None) == "unknown"
        assert F.oi_funding_label(1.0, None) == "neutral"

    def test_ls_label(self):
        assert F.ls_label(2.5) == "crowded_long"
        assert F.ls_label(2.0) == "balanced"
        assert F.ls_label(0.8) == "balanced"
        assert F.ls_label(0.7) == "crowded_short"
        assert F.ls_label(None) == "unknown"

    def test_divergence_flags(self):
        assert F.divergence_flags(3.0, -4.0, None, None) == ["price_up_oi_down"]
        assert F.divergence_flags(-3.0, 4.0, None, None) == ["price_down_oi_up"]
        assert F.divergence_flags(0.5, 0.0, 0.0004, 0.0001) == ["funding_up_price_flat"]
        assert F.divergence_flags(0.5, 0.0, 0.00015, 0.0001) == []   # 상승폭 부족
        assert F.divergence_flags(3.0, 1.0, None, None) == []
        assert F.divergence_flags(None, None, None, None) == []
        assert F.divergence_flags(2.0, -3.0, 0.001, 0.0) == ["price_up_oi_down"]

    def test_vwap_24h_and_clusters(self):
        old = [bar(i * H, 50, 50, 50, 50, 100.0) for i in range(10)]
        last24 = [bar((10 + i) * H, 100, 100, 100, 100, 1.0) for i in range(24)]
        assert F.vwap_24h(old + last24) == pytest.approx(100.0)
        cl = F.est_liq_clusters(100.0)
        assert [c["leverage"] for c in cl] == [25, 50, 100]
        assert all(c["side"] == "long" and c["estimated"] is True for c in cl)
        assert [c["level"] for c in cl] == pytest.approx([100 * (1 - 1 / 25 + 0.005),
                                                          100 * (1 - 1 / 50 + 0.005),
                                                          100 * (1 - 1 / 100 + 0.005)])
        assert F.est_liq_clusters(None) == [] and F.est_liq_clusters(0.0) == []
        assert F.vwap_24h([]) is None

    def test_nearest_long_liq_below(self):
        cl = F.est_liq_clusters(100.0)   # 96.5, 98.5, 99.5
        assert F.nearest_long_liq_below_pct(100.0, cl) == pytest.approx(0.5)
        assert F.nearest_long_liq_below_pct(99.0, cl) == pytest.approx((99 - 98.5) / 99 * 100)
        assert F.nearest_long_liq_below_pct(96.0, cl) is None
        assert F.nearest_long_liq_below_pct(None, cl) is None

    def test_cascade_risk(self):
        assert F.cascade_risk(1.5, "leveraged_long_buildup", "neutral", True) == "high"
        assert F.cascade_risk(1.5, "neutral", "neutral", True) == "moderate"
        assert F.cascade_risk(2.5, "leveraged_long_buildup", "neutral", True) == "moderate"
        assert F.cascade_risk(5.0, "neutral", "extreme_long", True) == "moderate"
        assert F.cascade_risk(5.0, "neutral", "neutral", True) == "low"
        assert F.cascade_risk(None, "neutral", "neutral", True) == "low"
        assert F.cascade_risk(None, "unknown", "unknown", False) == "unknown"
        assert F.cascade_risk(None, "unknown", "extreme_long", False) == "moderate"
        assert F.cascade_risk(3.0, "neutral", "neutral", True) == "low"   # <3 엄격

    def test_oir_spread_confidence(self):
        assert F.oir_1pct(book(300_000, 100_000)) == pytest.approx(0.5)
        assert F.oir_1pct(book(0.0, 0.0)) is None
        assert F.oir_1pct(None) is None
        assert F.spread_bps(book(spread_pct=0.0002)) == pytest.approx(2.0)
        assert F.spread_bps(None, ticker(100, bid=99.95, ask=100.05)) == pytest.approx(10.0)
        assert F.spread_bps(None, ticker(100)) is None
        assert F.data_confidence(0.0001, 1e9) == "high"
        assert F.data_confidence(0.0, None) == "low"
        assert F.data_confidence(None, 1e9) == "low"
        assert F.data_confidence(None, None) == "none"


class TestBuildFlowPack:
    def test_full(self):
        c1h = [bar(i * H, 100, 100, 100, 100, 1.0) for i in range(48)]
        fut = FuturesContext(symbol="ETH/USDT", funding_rate=0.0004, open_interest_usdt=1.1e9,
                             long_short_ratio=2.4, ts=0)
        fp = F.build_flow_pack("ETH/USDT", fut, [0.0001] * 21, book(300_000, 100_000, 0.0003),
                               ticker(100.0, change=0.004), 100.1, c1h, 1.0e9)
        assert fp.funding_now == 0.0004
        assert fp.funding_7d_mean == pytest.approx(0.0001)
        assert fp.funding_annualized_pct == pytest.approx(0.0004 * 3 * 365 * 100)
        assert fp.funding_regime == "elevated_long"
        assert fp.oi_change_24h_pct == pytest.approx(10.0)
        assert fp.oi_funding_label == "leveraged_long_buildup"
        assert fp.perp_premium_pct == pytest.approx(0.1)
        assert fp.ls_ratio == 2.4 and fp.ls_label == "crowded_long"
        assert fp.divergence_flags == ["funding_up_price_flat"]
        assert len(fp.est_liq_clusters) == 3
        assert fp.nearest_long_liq_below_pct == pytest.approx(0.5)
        assert fp.cascade_risk == "high"
        assert fp.oir_1pct == pytest.approx(0.5)
        assert fp.spread_bps == pytest.approx(3.0)
        assert fp.data_confidence == "high"
        json.dumps(F.compact(fp), sort_keys=True, allow_nan=False)

    def test_no_futures(self):
        fp = F.build_flow_pack("X", None, [], None, ticker(100.0), None, [], None)
        assert fp.funding_now is None and fp.funding_regime == "unknown"
        assert fp.oi_change_24h_pct is None and fp.oi_funding_label == "unknown"
        assert fp.perp_premium_pct is None and fp.ls_label == "unknown"
        assert fp.est_liq_clusters == [] and fp.nearest_long_liq_below_pct is None
        assert fp.cascade_risk == "unknown"
        assert fp.data_confidence == "none"
        assert fp.divergence_flags == [] and fp.spread_bps is None

    def test_divergence_uses_24h_price_and_oi(self):
        c1h = [bar(i * H, 100, 100, 100, 100, 1.0) for i in range(30)]
        fut = FuturesContext(symbol="X", funding_rate=0.0001, open_interest_usdt=0.9e9, ts=0)
        fp = F.build_flow_pack("X", fut, [0.0001], None, ticker(100.0, change=0.05), None, c1h, 1.0e9)
        assert fp.divergence_flags == ["price_up_oi_down"]
        assert fp.data_confidence == "high"
        # ticker 변화율이 없으면 1h 24봉 전 대비
        c2 = [bar(i * H, 100, 100, 100, 100, 1.0) for i in range(6)] + \
             [bar((6 + i) * H, 95, 95, 95, 95, 1.0) for i in range(24)]
        fut2 = FuturesContext(symbol="X", funding_rate=0.0001, open_interest_usdt=1.1e9, ts=0)
        fp2 = F.build_flow_pack("X", fut2, [], None, ticker(95.0), None, c2, 1.0e9)
        assert fp2.divergence_flags == ["price_down_oi_up"]

    def test_bad_values_do_not_raise(self):
        fut = FuturesContext(symbol="X", funding_rate=float("nan"), open_interest_usdt=-1.0, ts=0)
        fp = F.build_flow_pack("X", fut, [float("nan"), 0.0001], None, ticker(100.0), float("inf"),
                               [], 0.0)
        assert fp.funding_now is None and fp.funding_7d_mean == pytest.approx(0.0001)
        assert fp.oi_change_24h_pct is None and fp.perp_premium_pct is None


# ================================================================= RiskPack
class TestVarCvar:
    RETS = [i / 1000 for i in range(-50, 50)]

    def test_var_matches_numpy_linear_quantile(self):
        assert F.historical_var(self.RETS, 0.05) == pytest.approx(-np.quantile(self.RETS, 0.05) * 100)
        assert F.historical_var(self.RETS, 0.05) == pytest.approx(4.505)
        assert F.historical_var(self.RETS, 0.01) == pytest.approx(-np.quantile(self.RETS, 0.01) * 100)

    def test_cvar(self):
        q = np.quantile(self.RETS, 0.05)
        tail = [r for r in self.RETS if r <= q]
        assert F.historical_cvar(self.RETS, 0.05) == pytest.approx(-np.mean(tail) * 100)
        assert F.historical_cvar(self.RETS, 0.05) == pytest.approx(4.8)
        assert F.historical_cvar(self.RETS) >= F.historical_var(self.RETS)

    def test_positive_number_and_floor(self):
        assert F.historical_var(self.RETS) > 0
        gains = [0.01 + i / 10000 for i in range(50)]
        assert F.historical_var(gains) == 0.0
        assert F.historical_cvar(gains) == 0.0

    def test_insufficient(self):
        assert F.historical_var(self.RETS[:29]) is None
        assert F.historical_cvar([]) is None
        assert F.historical_var(self.RETS, 0.0) is None
        assert F.historical_var(self.RETS[:5], min_samples=2) is not None
        assert F.historical_var([float("nan")] * 40) is None


class TestVolStats:
    def test_realized_vol(self):
        rets = [0.01 * math.sin(i) for i in range(100)]
        ref = np.std(rets[-20:], ddof=1) * math.sqrt(24 * 365) * 100
        assert F.realized_vol_pct(rets, 20) == pytest.approx(ref)
        assert F.realized_vol_pct(rets[:19], 20) is None

    def test_daily_vol(self):
        rets = [0.01 * math.cos(i * 0.3) for i in range(120)]
        assert F.daily_vol_pct(rets) == pytest.approx(np.std(rets[-96:], ddof=1) * math.sqrt(24) * 100)
        assert F.daily_vol_pct(rets[:95]) is None

    def test_excess_kurtosis_population(self):
        two_point = [1.0, -1.0] * 20
        assert F.excess_kurtosis(two_point) == pytest.approx(-2.0)
        xs = [0.01 * math.sin(i * 1.1) ** 3 for i in range(80)]
        a = np.array(xs)
        m = a.mean()
        ref = np.mean((a - m) ** 4) / np.mean((a - m) ** 2) ** 2 - 3
        assert F.excess_kurtosis(xs) == pytest.approx(ref)
        assert F.excess_kurtosis([0.0] * 50) is None
        assert F.excess_kurtosis(xs[:10]) is None


class TestCorrBeta:
    @staticmethod
    def rets(n, k=1.0, phase=0.0):
        return [k * 0.01 * math.sin(i * 0.9 + phase) for i in range(n)]

    def test_identical_and_inverse(self):
        a = from_log_returns(self.rets(120))
        b = from_log_returns(self.rets(120))
        inv = from_log_returns(self.rets(120, k=-1.0))
        assert F.rolling_corr(a, b) == pytest.approx(1.0)
        assert F.rolling_corr(a, inv) == pytest.approx(-1.0)

    def test_beta(self):
        bench = from_log_returns(self.rets(120))
        lev = from_log_returns(self.rets(120, k=2.0))
        assert F.rolling_beta(lev, bench) == pytest.approx(2.0)
        assert F.rolling_beta(bench, bench) == pytest.approx(1.0)

    def test_aligned_by_ts_and_window(self):
        rets = self.rets(150)
        a = from_log_returns(rets)
        # b 는 같은 수익률이지만 시작 시각이 50봉 늦다 → 공통 ts 만 사용
        b = from_log_returns(rets[50:], start=50 * H, p0=a[50].close)
        xs, ys = F.aligned_returns(a, b, 96)
        assert len(xs) == 96 and xs == pytest.approx(ys)
        assert F.rolling_corr(a, b) == pytest.approx(1.0)
        # 겹침이 없으면 None
        c = from_log_returns(rets, start=10_000 * H)
        assert F.rolling_corr(a, c) is None

    def test_uses_last_96(self):
        early = self.rets(100)
        late = self.rets(96)
        a = from_log_returns(early + late)
        b = from_log_returns([-x for x in early] + late)   # 앞 100개 반대, 최근 96개 동일
        assert F.rolling_corr(a, b) == pytest.approx(1.0)

    def test_insufficient_or_flat(self):
        a = from_log_returns(self.rets(40))
        assert F.rolling_corr(a, a) is None          # 39 < 48
        flat = from_log_returns([0.0] * 100)
        assert F.rolling_corr(from_log_returns(self.rets(100)), flat) is None
        assert F.rolling_beta(from_log_returns(self.rets(100)), flat) is None
        assert F.rolling_corr([], []) is None


class TestSizingAndExecution:
    def test_inverse_vol(self):
        assert F.inverse_vol_size_pct(10.0) == pytest.approx(5.0)
        assert F.inverse_vol_size_pct(2.0) == 10.0
        assert F.inverse_vol_size_pct(None) is None
        assert F.inverse_vol_size_pct(0.0) is None

    def test_cvar_size(self):
        assert F.cvar_size_pct(3.0) == pytest.approx(5.0)
        assert F.cvar_size_pct(1.0) == 10.0
        assert F.cvar_size_pct(None) is None and F.cvar_size_pct(0.0) is None

    def test_impact_and_share(self):
        assert F.impact_bps_est(1000.0, 200_000.0) == pytest.approx(0.5)
        assert F.impact_bps_est(1000.0, 0.0) is None
        assert F.impact_bps_est(None, 1.0) is None
        assert F.order_notional_vs_15m_volume_share(1000.0, 9_600_000.0) == pytest.approx(0.01)
        assert F.order_notional_vs_15m_volume_share(1000.0, None) is None

    def test_exec_cost(self):
        assert F.exec_cost_pct(4.0, 3.0) == pytest.approx(0.04 + 0.015 + 0.20)
        assert F.exec_cost_pct(None, 3.0) is None
        assert F.exec_cost_pct(4.0, None) is None


class TestBuildRiskPack:
    def test_full(self):
        rets = [0.01 * math.sin(i * 0.9) + 0.004 * math.cos(i * 2.1) for i in range(220)]
        c = from_log_returns(rets)
        btc = from_log_returns([0.5 * r for r in rets])
        other = from_log_returns([-r for r in rets])
        rp = F.build_risk_pack("ETH/USDT", c, btc, {"SOL/USDT": other, "ETH/USDT": c},
                               book(ask_depth=500_000.0, spread_pct=0.0003),
                               ticker(c[-1].close, qv=96_000_000.0), 1000.0, 1.2)
        lr = [r for _, r in F.log_returns(c)]
        last200 = lr[-200:]
        assert rp.var95_1h_pct == pytest.approx(-np.quantile(last200, 0.05) * 100)
        assert rp.var99_1h_pct == pytest.approx(-np.quantile(last200, 0.01) * 100)
        assert rp.cvar95_1h_pct == pytest.approx(F.historical_cvar(last200))
        assert rp.rv_20_pct == pytest.approx(np.std(lr[-20:], ddof=1) * math.sqrt(8760) * 100)
        assert rp.rv_60_pct == pytest.approx(np.std(lr[-60:], ddof=1) * math.sqrt(8760) * 100)
        assert rp.daily_vol_pct == pytest.approx(np.std(lr[-96:], ddof=1) * math.sqrt(24) * 100)
        assert rp.hv_pct_rank == F.hv_pct_rank(c)
        assert rp.kurtosis == pytest.approx(F.excess_kurtosis(last200))
        assert rp.corr_to_btc_1h == pytest.approx(1.0)
        assert rp.beta_btc == pytest.approx(2.0)
        assert rp.corr_to_open_positions == {"SOL/USDT": pytest.approx(-1.0)}  # 자기 자신 제외
        assert rp.inverse_vol_size_pct == pytest.approx(min(10, 0.5 / rp.daily_vol_pct * 100))
        assert rp.cvar_size_pct == pytest.approx(min(10, 0.15 / rp.cvar95_1h_pct * 100))
        assert rp.spread_bps == pytest.approx(3.0)
        assert rp.depth_taker_usdt == 500_000.0
        assert rp.impact_bps_est == pytest.approx(0.2)
        assert rp.order_notional_vs_15m_volume_share == pytest.approx(1000 / 1_000_000)
        assert rp.stop_vs_atr_noise_ok is True
        assert rp.exec_cost_pct == pytest.approx(0.2 / 100 + 3.0 / 200 + 0.20)
        json.dumps(F.compact(rp), sort_keys=True, allow_nan=False)

    def test_empty_inputs(self):
        rp = F.build_risk_pack("X", [], [], {}, None, ticker(100.0), 1000.0, None)
        for name in ("var95_1h_pct", "var99_1h_pct", "cvar95_1h_pct", "rv_20_pct", "rv_60_pct",
                     "daily_vol_pct", "hv_pct_rank", "kurtosis", "corr_to_btc_1h", "beta_btc",
                     "inverse_vol_size_pct", "cvar_size_pct", "spread_bps", "depth_taker_usdt",
                     "impact_bps_est", "order_notional_vs_15m_volume_share", "stop_vs_atr_noise_ok",
                     "exec_cost_pct"):
            assert getattr(rp, name) is None, name
        assert rp.corr_to_open_positions == {}

    def test_short_history(self):
        c = from_log_returns([0.01 * math.sin(i) for i in range(25)])
        rp = F.build_risk_pack("X", c, c, {}, book(), ticker(100.0, bid=99.9, ask=100.1), 0.0, 1.5)
        assert rp.var95_1h_pct is None and rp.daily_vol_pct is None and rp.rv_20_pct is not None
        assert rp.corr_to_btc_1h is None
        assert rp.impact_bps_est == 0.0
        assert rp.stop_vs_atr_noise_ok is False


# ================================================================= RegimePack
def regime_series(n=60, k=1.0, phase=0.0, start=0):
    return from_log_returns([k * 0.01 * math.sin(i * 0.8 + phase) for i in range(n)], start=start)


class TestRegime:
    def test_avg_corr_identical(self):
        data = {s: regime_series() for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT")}
        avg, dens = F.avg_pairwise_corr(data)
        assert avg == pytest.approx(1.0) and dens == pytest.approx(1.0)

    def test_avg_corr_mixed(self):
        data = {"A": regime_series(), "B": regime_series(), "C": regime_series(k=-1.0)}
        avg, dens = F.avg_pairwise_corr(data)
        assert avg == pytest.approx((1 - 1 - 1) / 3)
        assert dens == pytest.approx(1 / 3)

    def test_uses_last_48(self):
        n = 100
        base = [0.01 * math.sin(i * 0.8) for i in range(n)]
        flipped = [-x for x in base[:n - 48]] + base[n - 48:]
        data = {"A": from_log_returns(base), "B": from_log_returns(base), "C": from_log_returns(flipped)}
        assert F.avg_pairwise_corr(data)[0] == pytest.approx(1.0)

    def test_fewer_than_3(self):
        data = {"BTC/USDT": regime_series(), "ETH/USDT": regime_series()}
        assert F.avg_pairwise_corr(data) == (None, None)
        rp = F.build_regime_pack(data, "BTC/USDT", None, None)
        assert rp.avg_pairwise_corr_1h is None and rp.density is None and rp.fused is False
        short = {"A": regime_series(), "B": regime_series(), "C": regime_series(n=10)}
        assert F.avg_pairwise_corr(short) == (None, None)

    def test_btc_regime_4h(self):
        assert F.btc_regime_4h(ind(ema_50=110, ema_200=100, adx_14=25)) == "bull"
        assert F.btc_regime_4h(ind(ema_50=110, ema_200=100, adx_14=15)) == "range"
        assert F.btc_regime_4h(ind(ema_50=90, ema_200=100, adx_14=15)) == "range"
        assert F.btc_regime_4h(ind(ema_50=90, ema_200=100, adx_14=25)) == "bear"
        assert F.btc_regime_4h(ind(ema_50=110, ema_200=100, adx_14=20)) == "bear"
        assert F.btc_regime_4h(ind(ema_50=110, ema_200=None, adx_14=25)) == "unknown"
        assert F.btc_regime_4h(None) == "unknown"

    def test_fused_and_macro_alarm(self):
        data = {s: regime_series() for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT")}
        calm = ind(return_1=0.001, atr_pct=0.01)
        shock = ind(return_1=-0.03, atr_pct=0.01)
        rp = F.build_regime_pack(data, "BTC/USDT", ind(ema_50=110, ema_200=100, adx_14=30), calm)
        assert rp.fused is True and rp.btc_shock is False and rp.verdict == "fused"
        assert rp.btc_regime_4h == "bull"
        rp2 = F.build_regime_pack(data, "BTC/USDT", None, shock)
        assert rp2.btc_shock is True and rp2.verdict == "macro_alarm"

    def test_shock_without_fused_is_normal(self):
        data = {"BTC/USDT": regime_series(), "ETH/USDT": regime_series(k=-1.0), "SOL/USDT": regime_series(phase=1.5)}
        rp = F.build_regime_pack(data, "BTC/USDT", None, ind(return_1=0.05, atr_pct=0.01))
        assert rp.btc_shock is True and rp.fused is False and rp.verdict == "normal"

    def test_shock_boundary(self):
        data = {"BTC/USDT": regime_series()}
        assert F.build_regime_pack(data, "BTC/USDT", None, ind(return_1=0.02, atr_pct=0.01)).btc_shock is False
        assert F.build_regime_pack(data, "BTC/USDT", None, ind(return_1=0.0201, atr_pct=0.01)).btc_shock is True

    def test_btc_from_candles_when_no_indicator(self):
        closes = [100 * (1 + 0.002 * math.sin(i)) for i in range(40)] + [110.0]
        data = {"BTC/USDT": from_closes(closes, wick=0.1)}
        assert F.build_regime_pack(data, "BTC/USDT", None, None).btc_shock is True

    def test_name_alarm(self):
        calm_closes = [100 * (1 + 0.002 * math.sin(i)) for i in range(40)]
        jump = from_closes(calm_closes + [calm_closes[-1] * 1.10], wick=0.2)
        quiet = from_closes(calm_closes + [calm_closes[-1] * 1.001], wick=0.2)
        data = {"BTC/USDT": quiet, "DOGE/USDT": jump, "ETH/USDT": quiet}
        rp = F.build_regime_pack(data, "BTC/USDT", None, ind(return_1=0.001, atr_pct=0.01))
        assert rp.name_alarm_symbols == ["DOGE/USDT"]
        # BTC 가 조용하지 않으면 name alarm 없음
        rp2 = F.build_regime_pack(data, "BTC/USDT", None, ind(return_1=0.015, atr_pct=0.01))
        assert rp2.name_alarm_symbols == []

    def test_empty(self):
        rp = F.build_regime_pack({}, "BTC/USDT", None, None)
        assert rp == F.RegimePack()
        assert rp.verdict == "normal" and rp.btc_regime_4h == "unknown"


# ================================================================= 종합 심리
class TestSentiment:
    def test_all_missing(self):
        s = F.composite_sentiment(None, None, None, None)
        assert s.score_0_100 is None and s.label == "unknown"
        assert s.components == {"fear_greed": None, "funding_z": None, "ls_pctile": None, "headline_polarity": None}

    def test_single_component_renormalized(self):
        s = F.composite_sentiment(10, None, None, None)
        assert s.score_0_100 == pytest.approx(10.0) and s.label == "extreme_fear"
        s2 = F.composite_sentiment(None, None, None, 1.0)
        assert s2.score_0_100 == pytest.approx(100.0) and s2.label == "extreme_greed"

    def test_weights(self):
        s = F.composite_sentiment(50, None, None, 1.0)
        assert s.score_0_100 == pytest.approx((0.4 * 50 + 0.2 * 100) / 0.6)
        assert s.label == "greed"
        s4 = F.composite_sentiment(80, 0.0, 30.0, -1.0)
        assert s4.score_0_100 == pytest.approx(0.4 * 80 + 0.2 * 50 + 0.2 * 30 + 0.2 * 0)
        # components 는 0..100 정규화 값
        assert s4.components == {"fear_greed": 80.0, "funding_z": pytest.approx(50.0), "ls_pctile": 30.0,
                                 "headline_polarity": 0.0}
        assert list(s4.components) == ["fear_greed", "funding_z", "ls_pctile", "headline_polarity"]

    def test_components_normalized_missing_none(self):
        s = F.composite_sentiment(None, 1.0, None, 0.5)
        assert s.components["fear_greed"] is None and s.components["ls_pctile"] is None
        assert s.components["funding_z"] == pytest.approx(84.134, abs=1e-3)
        assert s.components["headline_polarity"] == pytest.approx(75.0)
        assert all(v is None or 0 <= v <= 100 for v in F.composite_sentiment(200, 9.0, 150.0, 3.0).components.values())

    def test_funding_z_cdf(self):
        s = F.composite_sentiment(None, 1.0, None, None)
        assert s.score_0_100 == pytest.approx(84.134, abs=1e-3)
        assert F.composite_sentiment(None, -10.0, None, None).score_0_100 == pytest.approx(0.0, abs=1e-6)

    def test_clipping_and_nan(self):
        s = F.composite_sentiment(150, None, -20.0, 5.0)
        assert 0 <= s.score_0_100 <= 100
        assert F.composite_sentiment(None, float("nan"), None, None).label == "unknown"

    def test_label_bands(self):
        assert F.sentiment_label(0) == "extreme_fear"
        assert F.sentiment_label(19.99) == "extreme_fear"
        assert F.sentiment_label(20) == "fear"
        assert F.sentiment_label(40) == "neutral"
        assert F.sentiment_label(59.9) == "neutral"
        assert F.sentiment_label(60) == "greed"
        assert F.sentiment_label(80) == "extreme_greed"
        assert F.sentiment_label(None) == "unknown"


# ================================================================= 공통 견고성
class TestRobustness:
    def test_label_functions_on_empty(self):
        assert F.ichimoku([]) is None
        assert F.candle_patterns([]).score == 0
        assert F.smc_structure([]) == "unknown"
        assert F.fvg([]) == "none"
        assert F.zones([], 100.0)["nearest_demand_zone"] is None
        assert F.hv_pct_rank([]) is None
        assert F.cgo_60(100.0, []) is None
        assert F.abnormal_volume_multiple([]) is None
        assert F.volume_vs_hour_avg([]) is None
        assert F.log_returns([]) == []
        assert F.rolling_corr([], []) is None
        assert F.obv([]) == []

    def test_non_positive_closes_skipped_in_returns(self):
        cs = from_closes([100.0, 0.0, 101.0, 102.0], wick=0.0)
        rets = F.log_returns(cs)
        assert [t for t, _ in rets] == [3 * H]

    def test_nan_candles_everywhere(self):
        nan = float("nan")
        cs = [bar(i * H, nan, nan, nan, nan, nan) for i in range(150)]
        assert F.ichimoku(cs) is None
        assert F.candle_patterns(cs).patterns == []
        assert F.smc_structure(cs) == "range"
        assert F.fvg(cs) == "none"
        assert F.hv_pct_rank(cs) is None
        assert F.obv_trend(cs) == "flat"
        assert F.cgo_60(100.0, cs) is None
        rp = F.build_risk_pack("X", cs, cs, {"Y": cs}, None, ticker(100.0), 100.0, None)
        assert rp.var95_1h_pct is None
        fl = F.build_flow_pack("X", None, [], None, ticker(100.0), None, cs, None)
        assert fl.est_liq_clusters == []


class TestExtraEdges:
    def test_negative_score_clip(self):
        # 약세 장악형 + 유성형 + 약세 핀바 → −3
        cs = [bar(0, 100.0, 100.5, 99.9, 100.4), bar(1, 100.5, 110.0, 99.9, 99.8)]
        lab = F.candle_patterns(cs)
        assert {"bearish_engulfing", "shooting_star", "pin_bar_bear"} <= set(lab.patterns)
        assert lab.score == -3

    def test_demand_zone_bos_up_path(self):
        cs = path_bos_up()
        last_up = [t for t, k in F.structure_events(cs) if k in ("bos_up", "choch_up")][-1]
        j = max(i for i in range(last_up) if cs[i].close < cs[i].open)
        z = F.zones(cs, cs[-1].close)
        assert z["nearest_demand_zone"] == [cs[j].low, cs[j].high]
        assert z["distance_to_demand_pct"] > 0

    def test_compact_feature_pack_keys_sorted_everywhere(self):
        cbt = full_inputs()
        out = F.compact(F.build_feature_pack("X", cbt, {}, ticker(cbt["15m"][-1].close)))

        def check(o):
            if isinstance(o, dict):
                assert list(o) == sorted(o)
                for v in o.values():
                    check(v)
            elif isinstance(o, list):
                for v in o:
                    check(v)
            elif isinstance(o, float):
                assert o == float(f"{o:.4g}")
        check(out)

    def test_inputs_not_mutated(self):
        cbt = full_inputs()
        before = {tf: [c.model_dump() for c in cs] for tf, cs in cbt.items()}
        F.build_feature_pack("X", cbt, {}, ticker(cbt["15m"][-1].close))
        assert {tf: [c.model_dump() for c in cs] for tf, cs in cbt.items()} == before
