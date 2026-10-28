"""tradingbot/agents/payloads.py 테스트 (SPEC §20.3, v2.1 §20.3a 키 계약)."""
from __future__ import annotations

import copy
import hashlib
import json
import math

import pytest

from tradingbot import features as F
from tradingbot import journal as J
from tradingbot.agents import payloads as P
from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    BullPointReliability,
    DeathPath,
    FlowOutput,
    FlowSymbolView,
    NewsEvent,
    NewsOutput,
    RedLine,
    RiskCandidateVerdict,
    RiskGlobal,
    RiskPositionView,
    TechnicalOutput,
    Thesis,
    ThesisPoint,
)
from tradingbot.config import HardLimits
from tradingbot.features import (
    CandleLabels,
    FeaturePack,
    FlowPack,
    IchimokuLabels,
    RegimePack,
    RiskPack,
    SentimentComposite,
    SmcLabels,
    TechnicalLabels,
)
from tradingbot.models import BotState, ClosedTrade, DailyStats, NewsItem, Position

NOW = 1_761_609_600_000          # 2025-10-28T00:00Z (임의 고정 시각)
H = 3_600_000
BTC, ETH, SOL = "BTC/USDT", "ETH/USDT", "SOL/USDT"
HL = {"max_position_pct": 10, "max_positions": 3, "max_stop_pct": 2, "max_tp_pct": 4, "min_confidence": 0.65}


# ======================================================================= 픽스처 팩토리
def _labels(tf: str, **kw) -> TechnicalLabels:
    base = dict(timeframe=tf, ema_state="bull_aligned", price_vs_ema20_pct=1.234567, adx_14=25.55555,
                rsi_14=58.123456, rsi_5=63.987654, bb_position=0.7777777, macd_hist_sign="pos", obv_trend="up",
                volume_ratio_20=1.3333333, atr_pct=0.8765432, vote="trend_up")
    base.update(kw)
    return TechnicalLabels(**base)


def make_fp(symbol: str = BTC, price: float = 64123.456789, n_closes: int = 20, **kw) -> FeaturePack:
    base = dict(
        symbol=symbol, ts=NOW - 15 * 60_000, price=price,
        per_tf={tf: _labels(tf) for tf in ("15m", "1h", "4h", "1d")},
        ichimoku_1h=IchimokuLabels(tk_cross="bullish", price_vs_cloud="above", cloud_bullish=True,
                                   cross_strength="strong"),
        ichimoku_4h=IchimokuLabels(tk_cross="none", price_vs_cloud="above", cloud_bullish=True,
                                   cross_strength="none"),
        candles_15m=CandleLabels(patterns=["hammer"], score=1), candles_1h=CandleLabels(patterns=[], score=0),
        smc_1h=SmcLabels(structure="bos_up", fvg_dir="bullish", nearest_demand_zone=[63000.12345, 63500.98765],
                         nearest_supply_zone=None, distance_to_demand_pct=1.0123456, distance_to_supply_pct=None),
        smc_4h=None, mtf_agreement=True, hv_pct_rank=42.424242, atr_pct_1h=0.912345, vwap_distance_pct=0.3333333,
        volume_vs_hour_avg=1.1111111, cgo_60=2.7182818, abnormal_volume_multiple=1.4142135,
        last_closes_1h=[price * (1 + 0.001 * i) for i in range(n_closes)], change_24h_pct=3.1415926,
        noise_floor_stop_pct=0.912345, noise_ok_2pct=True,
    )
    base.update(kw)
    return FeaturePack(**base)


def make_flow_pack(symbol: str = BTC) -> FlowPack:
    return FlowPack(symbol=symbol, funding_now=0.000123456, funding_7d_mean=0.0000987654,
                    funding_annualized_pct=13.51851, funding_regime="neutral", oi_change_24h_pct=2.3456789,
                    oi_funding_label="neutral", perp_premium_pct=0.0123456, ls_ratio=1.2345678, ls_label="balanced",
                    divergence_flags=[], est_liq_clusters=[{"level": 61234.5678, "side": "long", "leverage": 25,
                                                            "estimated": True}],
                    nearest_long_liq_below_pct=4.5678912, cascade_risk="low", oir_1pct=0.1234567,
                    spread_bps=0.5432109, data_confidence="high")


def make_risk_pack(symbol: str = BTC, corr: dict | None = None) -> RiskPack:
    return RiskPack(symbol=symbol, var95_1h_pct=1.0123456, var99_1h_pct=1.8765432, cvar95_1h_pct=1.4567891,
                    rv_20_pct=45.678912, rv_60_pct=50.123456, daily_vol_pct=2.3456789, hv_pct_rank=42.424242,
                    kurtosis=3.3333333, corr_to_btc_1h=0.8765432, beta_btc=1.1234567,
                    corr_to_open_positions=corr or {}, inverse_vol_size_pct=10.0, cvar_size_pct=10.0,
                    spread_bps=0.5432109, depth_taker_usdt=1234567.891, impact_bps_est=0.0810005,
                    order_notional_vs_15m_volume_share=0.000123456, stop_vs_atr_noise_ok=True,
                    exec_cost_pct=0.2035)


def make_regime() -> RegimePack:
    return RegimePack(avg_pairwise_corr_1h=0.6543219, density=0.3333333, fused=False, btc_regime_4h="bull",
                      btc_shock=False, name_alarm_symbols=[], verdict="normal")


def make_sentiment() -> SentimentComposite:
    return SentimentComposite(score_0_100=55.555555, label="neutral",
                              components={"fear_greed": 60.0, "funding_z": 0.1234567, "ls_pctile": None,
                                          "headline_polarity": None})


def make_candidate(symbol: str = BTC, rank: int = 1) -> dict:
    return {"symbol": symbol, "rank": rank, "composite_z": 1.23456789,
            "factor_table": {"momentum_4h": 0.0123456, "momentum_1d": 0.0345678, "reversal_1h": -0.001234,
                             "rv_24h": 0.4567891, "volume_ratio": 1.2345678, "rs_vs_btc": 0.0,
                             "funding_z": -0.3456789, "oi_change_24h": 2.3456789},
            "reasons": ["momentum_4h 상위", "거래량 증가"]}


def make_liquidity(symbol: str = BTC) -> dict:
    return {"symbol": symbol, "ok": True, "reasons": [], "spread_bps": 0.5432109,
            "quote_volume_24h": 1234567890.12, "depth_1pct_usdt": 9876543.21}


def make_tech(symbol: str = BTC, **kw) -> TechnicalOutput:
    base = dict(symbol=symbol, direction="long", setup_type="trend_continuation", timing="now", mtf_agreement=True,
                score=3, confidence=0.7123456, stop_candidate_price=63000.123456, stop_pct=1.7512345,
                sl_reference="zone", target_candidate_price=66000.987654, tp_pct=3.5024690,
                invalidation="feature_pack.per_tf.4h.ema_state 가 mixed 로 바뀌면 무효", noise_check=True,
                exit_flag=False, exit_reason=None,
                evidence=["feature_pack.per_tf.4h.ema_state: bull_aligned — 4시간 정배열"])
    base.update(kw)
    return TechnicalOutput(**base)


def make_flow_view(symbol: str = BTC, **kw) -> FlowSymbolView:
    base = dict(symbol=symbol, posture="neutral", crowding_risk="low", squeeze_direction="none", cascade_risk="low",
                nearest_long_liq_cluster_pct_below=4.5678912, stop_inside_cascade=False, washout_setup=False,
                block_new_longs=False, block_reason=None, flow_bias="long", confidence=0.6123456,
                evidence=["flow_packs.BTC/USDT.funding_regime: neutral — 과열 없음"])
    base.update(kw)
    return FlowSymbolView(**base)


def make_bull(symbol: str = BTC) -> BullOutput:
    return BullOutput(symbol=symbol, case_strength=4,
                      thesis_points=[ThesisPoint(claim="4시간 추세 정배열", evidence_field="technical.setup_type")],
                      catalysts=["ETF 유입"], underreaction_evidence="feature_extract.cgo_60 낮음",
                      entry_trigger="1h 종가 돌파", expected_horizon_bars=16, invalidation_level=63000.123456,
                      disconfirming_evidence=["flow.crowding_risk: low", "event_signal.score: null"],
                      main_risk_to_bull_case="레짐 전환", what_would_disprove="4h 구름 하회", confidence=0.6654321)


def make_bear(symbol: str = BTC) -> BearOutput:
    return BearOutput(symbol=symbol,
                      death_paths=[DeathPath(family=f, description="경로", observable_trigger="트리거",
                                             probability_band="low", time_to_impact_bars=4)
                                   for f in ("liquidation_cascade", "news_shock", "regime_flip")],
                      greed_flags=[], disconfirming_evidence=["technical.mtf_agreement: true"],
                      strongest_point="연쇄 청산 거리 4.6%", what_would_disprove_bear="OI 감소",
                      verdict="survivable_if_monitored", monitoring_conditions=["funding_now > 0.0005"],
                      confidence=0.5432109)


def make_verdict(symbol: str = BTC, verdict: str = "approve") -> RiskCandidateVerdict:
    return RiskCandidateVerdict(symbol=symbol, verdict=verdict, veto_reasons=[], conditions=["펀딩 감시"],
                                max_size_pct=7.7777777, stop_pct=1.5123456, take_profit_pct=3.0246912,
                                rr_net_estimate=1.6543219,
                                bull_point_reliability=[BullPointReliability(point="추세", score_1_5=4,
                                                                             pushback="레짐 의존")],
                                required_monitoring=["btc_regime_4h"], correlation_note="ETH 와 0.82")


def make_global() -> RiskGlobal:
    return RiskGlobal(entries_paused=False, pause_reason=None, confidence_floor=0.7, exposure_cap_pct=25.0,
                      portfolio_note="정상")


def make_thesis() -> Thesis:
    return Thesis(reasons=["4시간 추세 지속"], assumptions=["BTC 레짐 bull 유지", "펀딩 과열 없음"],
                  red_lines=[RedLine(metric="price", op="<", value=62000.123456),
                             RedLine(metric="funding_rate", op=">", value=0.0005)], horizon_bars=32)


def make_position(symbol: str = ETH, entry: float = 2500.0, cycle: int = 90) -> Position:
    return Position(symbol=symbol, base_amount=0.4, entry_price=entry, quote_spent=1000.0, fee_quote=1.0,
                    stop_loss_price=entry * 0.98, take_profit_price=entry * 1.04, opened_cycle=cycle,
                    opened_ts=NOW - 10 * H, thesis="문자열 논지(총괄 원문)", confidence=0.81, last_mark=entry * 1.01)


def make_event(hid: str, score: float, symbols: list[str], age_h: float | None = 1.0, **kw) -> dict:
    d = {"headline_id": hid, "score": score, "event_type": "listing", "symbols": symbols,
         "summary": f"{hid} 요약", "stale": False, "age_h": age_h}
    d.update(kw)
    return d


def make_news_output(**kw) -> NewsOutput:
    base = dict(events=[NewsEvent(headline_id="aaa111", score=0.33, event_type="listing", symbols=[BTC],
                                  summary="상장", stale=False)],
                risk_event=False, risk_event_symbols=[], market_tone=0.1234567, market_note="보통",
                feed_health_note="피드 정상")
    base.update(kw)
    return NewsOutput(**base)


# ---------------------------------------------------------------- 공통 검사기
def walk(obj, path=""):
    """(path, value) 전부 순회."""
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def keys_anywhere(obj) -> set[str]:
    out: set[str] = set()
    for _, v in walk(obj):
        if isinstance(v, dict):
            out |= set(v)
    return out


def assert_json_plain(payload):
    s = P.to_json(payload)          # allow_nan=False: NaN/inf 가 남아 있으면 실패
    assert json.loads(s) == payload
    for _, v in walk(payload):
        assert v is None or isinstance(v, (dict, list, str, int, float, bool)), type(v)
        if isinstance(v, float):
            assert math.isfinite(v)
            assert v == float(f"{v:.4g}"), f"4유효자리 아님: {v}"
    return s


def assert_no_candles(payload):
    """캔들 원본 금지: OHLC dict 없음, 긴 숫자 배열은 last_closes_1h 뿐(≤20)."""
    for path, v in walk(payload):
        if isinstance(v, dict):
            assert not {"open", "high", "low", "close"} <= set(v), path
        if isinstance(v, list) and v and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            if path.endswith("last_closes_1h"):
                assert len(v) <= 20
            else:
                assert len(v) <= 4, f"숫자 배열 {path} len={len(v)}"


def assert_common(payload, role):
    assert payload["role"] == role
    assert payload["hard_limits"] == HL
    s = assert_json_plain(payload)
    assert_no_candles(payload)
    return s


# ---------------------------------------------------------------- 시나리오 묶음
def bull_args(symbol=BTC):
    return dict(symbol=symbol, tech=make_tech(symbol), flow_view=make_flow_view(symbol),
                news_events=[make_event("e1", 0.66, [symbol], 2.0), make_event("e2", -0.33, [ETH], 1.0)],
                sentiment=make_sentiment(), scanner_row=make_candidate(symbol), regime=make_regime(),
                position=None)


def bull_kw(symbol=BTC):
    return dict(feature_pack=make_fp(symbol), news=make_news_output(), liquidity=make_liquidity(symbol),
                now_ms=NOW)


def approved_item(symbol=BTC, rank=1):
    return {"symbol": symbol, "candidate": make_candidate(symbol, rank), "technical": make_tech(symbol),
            "feature_pack": make_fp(symbol), "flow": make_flow_view(symbol), "bull": make_bull(symbol),
            "bear": make_bear(symbol), "risk": make_verdict(symbol), "risk_global": make_global(),
            "risk_confidence": 0.6789123, "gate": {"gate": "g3", "passed": True, "reasons": [],
                                                  "size_multiplier": 0.5, "confidence_floor": 0.7,
                                                  "notes": ["caution"]},
            "news_output": make_news_output(), "news_events": [make_event("n-" + symbol, 0.66, [symbol], 3.0)]}


def flagged_item(symbol=ETH):
    return {"symbol": symbol, "position": make_position(symbol), "mark": 2450.0, "cycle": 100,
            "thesis": make_thesis(), "red_line_hits": ["price < 2460"], "health_history": [7, 6, 4],
            "risk": RiskPositionView(symbol=symbol, assumption_status=["holding", "weakening"],
                                     red_lines_triggered=["price"], health=4, action="trim", reason="약화"),
            "technical": make_tech(symbol, direction="flat", setup_type="none", timing="wait_confirmation",
                                   score=-2, exit_flag=True, exit_reason="smc_1h choch_down",
                                   stop_candidate_price=None, stop_pct=None, sl_reference="none",
                                   target_candidate_price=None, tp_pct=None),
            "flag_reason": "red_line_hit", "news_events": [make_event("n-eth", -0.66, [symbol], 1.0)]}


def account_pack():
    return {"equity": 10234.56789, "daily_pnl_pct": -0.4567891, "drawdown_pct": 1.2345678,
            "drawdown_ladder_level": "normal", "exposure_pct": 9.8765432, "position_count": 1,
            "consecutive_losses": 1, "cooldowns": {SOL: 3}, "trades_today": 2, "decay_state": "insufficient_sample",
            "bias_severities": {"disposition": "none", "overtrading": "mild", "chasing": "none",
                                "anchoring": "insufficient_sample"},
            "noise_trade_share": None, "llm_budget_remaining_usd": 3.456789}


def head_args(n=2):
    syms = [BTC, SOL][:n]
    return dict(approved_ctx=[approved_item(s, i + 1) for i, s in enumerate(syms)],
                flagged_positions_ctx=[flagged_item()], regime=make_regime(), account_pack=account_pack(),
                sentiment=make_sentiment(),
                vetoed_ctx=[{"symbol": "XRP/USDT", "stage": "g1", "reasons": ["spread", "noise"]}],
                abstained_channels=["news_sentiment"], now_ms=NOW)


def risk_args(n=2):
    syms = [BTC, SOL][:n]
    return dict(
        candidates_ctx=[{"symbol": s, "candidate": make_candidate(s, i + 1), "technical": make_tech(s),
                         "flow": make_flow_view(s), "bull": make_bull(s), "bear": make_bear(s),
                         "planned_notional_usdt": 1023.456789} for i, s in enumerate(syms)],
        positions_ctx=[{"symbol": ETH, "position": make_position(), "mark": 2450.0, "cycle": 100,
                        "thesis": make_thesis(), "prev_assumption_status": ["holding", "weakening"],
                        "red_line_hits": [], "health_history": [7, 6], "technical": make_tech(ETH)}],
        risk_packs={s: make_risk_pack(s, {ETH: 0.8123456}) for s in [*syms, ETH, "DOGE/USDT"]},
        regime=make_regime(), account_pack=account_pack(), news_risk_symbols={SOL},
        flow_blocks=FlowOutput(per_symbol=[make_flow_view(s) for s in [*syms, ETH]], market_wide_note="",
                               data_confidence="high"),
    )


# ======================================================================= 상수
class TestConstants:
    def test_hard_limits_exact(self):
        assert P.HARD_LIMITS_PAYLOAD == HL
        assert P.hard_limits() == HL

    def test_hard_limits_copy_is_independent(self):
        h = P.hard_limits()
        h["max_stop_pct"] = 99
        assert P.HARD_LIMITS_PAYLOAD["max_stop_pct"] == 2

    def test_hard_limits_match_config(self):
        assert P.HARD_LIMITS_PAYLOAD["max_position_pct"] == pytest.approx(HardLimits.MAX_POSITION_PCT * 100)
        assert P.HARD_LIMITS_PAYLOAD["max_positions"] == HardLimits.MAX_POSITIONS
        assert P.HARD_LIMITS_PAYLOAD["max_stop_pct"] == pytest.approx(HardLimits.MAX_STOP_LOSS_PCT * 100)
        assert P.HARD_LIMITS_PAYLOAD["max_tp_pct"] == pytest.approx(HardLimits.MAX_TAKE_PROFIT_PCT * 100)
        assert P.HARD_LIMITS_PAYLOAD["min_confidence"] == HardLimits.MIN_CONFIDENCE

    def test_hard_limits_match_journal(self):
        assert P.HARD_LIMITS_PAYLOAD == J.HARD_LIMITS_PAYLOAD

    def test_flow_thresholds_exact(self):
        assert P.FLOW_THRESHOLDS == {
            "funding_extreme_long": 0.0005, "funding_elevated_long": 0.0002, "funding_negative": -0.0001,
            "funding_squeeze": -0.0002, "oi_buildup_pct": 5.0, "oi_buildup_funding": 0.0003,
            "oi_deleveraging_pct": -5.0, "ls_crowded_long": 2.0, "ls_crowded_short": 0.8,
            "cascade_high_distance_pct": 2.0, "cascade_moderate_distance_pct": 3.0}

    def test_flow_thresholds_match_features(self):
        assert P.FLOW_THRESHOLDS == F.FLOW_THRESHOLDS

    def test_bear_keys_superset_of_bull(self):
        assert set(P.BEAR_KEYS) - set(P.BULL_KEYS) == {"corr_to_open", "hv_pct"}


# ======================================================================= 도우미
class TestHelpers:
    def test_to_json_deterministic_and_compact(self):
        s = P.to_json({"b": 1, "a": "한글"})
        assert s == '{"a":"한글","b":1}'

    def test_to_json_rejects_nan(self):
        with pytest.raises(ValueError):
            P.to_json({"x": float("nan")})

    def test_headline_id_url(self):
        item = NewsItem(title="T", source="coindesk", url="https://x.y/1")
        assert P.headline_id(item) == hashlib.sha1(b"https://x.y/1").hexdigest()[:12]

    def test_headline_id_falls_back_to_title(self):
        assert P.headline_id({"title": "제목", "url": ""}) == hashlib.sha1("제목".encode()).hexdigest()[:12]
        assert len(P.headline_id({"title": "x"})) == 12

    def test_unrealized_pct(self):
        assert P.unrealized_pct(100, 102) == pytest.approx(2.0)
        assert P.unrealized_pct(100, None) is None
        assert P.unrealized_pct(0, 1) is None
        assert P.unrealized_pct(float("nan"), 1) is None

    def test_bars_held(self):
        pos = make_position(cycle=90)
        assert P.bars_held(pos, 100) == 10
        assert P.bars_held(pos, None) is None
        assert P.bars_held(pos, 80) == 0

    def test_event_signal_empty(self):
        assert P.event_signal([]) == {"score": None, "n_events": 0}
        assert P.event_signal(None) == {"score": None, "n_events": 0}

    def test_event_signal_decay(self):
        es = P.event_signal([make_event("a", 1.0, [BTC], 12.0)])
        assert es["n_events"] == 1
        assert es["score"] == pytest.approx(0.5)
        es = P.event_signal([make_event("a", -0.66, [BTC], 0.0), make_event("b", 0.33, [BTC], 24.0)])
        assert es["score"] == pytest.approx(-0.66 + 0.33 * 0.25)

    def test_event_signal_clipped(self):
        es = P.event_signal([make_event(str(i), 1.0, [BTC], 0.0) for i in range(5)])
        assert es["score"] == 1.0 and es["n_events"] == 5
        es = P.event_signal([make_event(str(i), -1.0, [BTC], 0.0) for i in range(5)])
        assert es["score"] == -1.0

    def test_event_signal_unknown_age_downweighted(self):
        es = P.event_signal([make_event("a", 1.0, [BTC], None)])
        assert es["score"] == pytest.approx(0.25)

    def test_event_signal_from_ts_and_models(self):
        ev = NewsEvent(headline_id="x", score=0.66, event_type="etf", symbols=[BTC], summary="s", stale=False)
        # 모델에는 age_h/ts 가 없다 → 나이 모름
        assert P.event_signal([ev])["score"] == pytest.approx(0.66 * 0.25)
        es = P.event_signal([{"headline_id": "y", "score": 1.0, "ts": NOW - 12 * H}], NOW)
        assert es["score"] == pytest.approx(0.5)

    def test_event_signal_skips_missing_score(self):
        assert P.event_signal([{"headline_id": "a", "score": None, "age_h": 1}]) == {"score": None, "n_events": 0}

    def test_combined_score(self):
        assert P.combined_score(5, 1.0) == pytest.approx(1.0)
        assert P.combined_score(-5, None) == pytest.approx(-0.6)
        assert P.combined_score(None, 0.5) is None
        assert P.combined_score(0, 0.5) == pytest.approx(0.2)

    def test_channel_agreement(self):
        assert P.channel_agreement(make_tech(), make_flow_view(), 0.1) == {
            "technical": True, "flow": True, "news": True}
        assert P.channel_agreement(None, None, None) == {"technical": False, "flow": False, "news": False}
        assert P.channel_agreement(make_tech(direction="flat"), make_flow_view(flow_bias="neutral"), 0.0) == {
            "technical": False, "flow": False, "news": False}

    def test_channel_agreement_matches_journal(self):
        jv = J.make_entry_verdicts(technical=make_tech(), flow=make_flow_view(), bull=None, bear=None, risk=None,
                                   head=None, event_signal_score=0.2)
        assert jv["channel_agreement"] == P.channel_agreement(make_tech(), make_flow_view(), 0.2)

    def test_symbol_news_events_filters(self):
        evs = [make_event("a", 0.33, [BTC], 1.0), make_event("b", 0.66, ["BTC"], 5.0),
               make_event("c", -1.0, [ETH], 1.0), make_event("d", 0.33, [BTC], 30.0),
               make_event("e", 0.0, [], 1.0), make_event("a", 0.33, [BTC], 3.0)]
        out = P.symbol_news_events(BTC, evs)
        assert [e["headline_id"] for e in out] == ["a", "b"]   # 최신순, 중복 제거, 24h 밖·타 심볼 제외
        assert out[0]["age_h"] == 1.0
        assert set(out[0]) == set(P.NEWS_EVENT_KEYS)

    def test_symbol_news_events_keeps_unknown_age_and_computes_stale(self):
        out = P.symbol_news_events(BTC, [make_event("u", -1.0, [BTC], None, stale=True)])
        assert out[0]["age_h"] is None and out[0]["stale"] is True
        out = P.symbol_news_events(BTC, [{"headline_id": "t", "score": 0.33, "event_type": "etf",
                                          "symbols": [BTC], "summary": "s", "stale": True, "ts": NOW - H}], NOW)
        assert out[0]["age_h"] == pytest.approx(1.0) and out[0]["stale"] is False

    def test_symbol_news_events_limit_keeps_strongest(self):
        evs = [make_event(f"w{i}", 0.0, [BTC], float(i) / 10) for i in range(20)]
        evs.append(make_event("hack", -1.0, [BTC], 23.0))
        out = P.symbol_news_events(BTC, evs)
        assert len(out) == P.MAX_SYMBOL_EVENTS
        assert "hack" in [e["headline_id"] for e in out]

    def test_build_aliases(self):
        al = P.build_aliases([BTC, ETH, "PEPE/USDT"])
        assert al["btc"] == BTC and al["bitcoin"] == BTC and al["xbt"] == BTC
        assert al["eth"] == ETH and al["ethereum"] == ETH
        assert al["pepe"] == "PEPE/USDT"
        assert all(k == k.lower() for k in al)
        assert list(al) == sorted(al)

    def test_feed_health_from(self):
        items = [NewsItem(title="a", source="s", url="u1", published_ts=NOW - 2 * H),
                 NewsItem(title="b", source="s", url="u2", published_ts=NOW - H),
                 NewsItem(title="c", source="s", url="u3")]
        fh = P.feed_health_from(items, ["feedX: timeout"])
        assert fh == {"items_fetched": 3, "oldest_ts": NOW - 2 * H, "newest_ts": NOW - H,
                      "errors": ["feedX: timeout"]}
        assert P.feed_health_from([], None) == {"items_fetched": 0, "oldest_ts": None, "newest_ts": None,
                                                "errors": []}

    def test_feature_extract(self):
        fe = P.feature_extract(make_fp())
        assert set(fe) == set(P.FEATURE_EXTRACT_KEYS)
        assert fe["rsi_5"] == pytest.approx(63.987654) and fe["obv_trend"] == "up"
        assert P.feature_extract(None) is None
        fp = make_fp(per_tf={"4h": _labels("4h")})
        fe = P.feature_extract(fp)
        assert fe["rsi_14"] is None and fe["cgo_60"] is not None

    def test_exit_block(self):
        assert P.exit_block(None) is None
        assert P.exit_block(make_tech(exit_flag=True, exit_reason="x")) == {"exit_flag": True, "exit_reason": "x"}
        assert P.exit_block({"exit_flag": False, "exit_reason": None}) == {"exit_flag": False, "exit_reason": None}

    def test_flow_blocks_from(self):
        fo = FlowOutput(per_symbol=[make_flow_view(BTC, block_new_longs=True, block_reason="r",
                                                   cascade_risk="high", stop_inside_cascade=True),
                                    make_flow_view(ETH)], market_wide_note="", data_confidence="low")
        fb = P.flow_blocks_from(fo)
        assert fb == {BTC: {"block_new_longs": True, "stop_inside_cascade": True, "cascade_risk": "high"},
                      ETH: {"block_new_longs": False, "stop_inside_cascade": False, "cascade_risk": "low"}}
        assert P.flow_blocks_from([make_flow_view(ETH)]) == {ETH: fb[ETH]}
        assert P.flow_blocks_from({ETH: make_flow_view(ETH)}) == {ETH: fb[ETH]}
        assert P.flow_blocks_from(None) == {}


# ======================================================================= account pack
class TestAccountPack:
    def test_normalize_exact_keys_and_defaults(self):
        ap = P.normalize_account_pack({})
        assert tuple(sorted(ap)) == tuple(sorted(P.ACCOUNT_PACK_KEYS))
        assert ap["decay_state"] == "insufficient_sample"
        assert ap["bias_severities"] == {k: "insufficient_sample" for k in P.BIAS_KEYS}
        assert ap["cooldowns"] == {} and ap["equity"] is None and ap["drawdown_ladder_level"] is None

    def test_normalize_drops_extra_and_bad_values(self):
        ap = P.normalize_account_pack({"equity": float("nan"), "secret": "x", "drawdown_ladder_level": "panic",
                                       "decay_state": "zzz", "bias_severities": {"chasing": "severe", "x": "y"},
                                       "cooldowns": {SOL: 2, ETH: 0, BTC: -1}})
        assert "secret" not in ap
        assert ap["equity"] is None and ap["drawdown_ladder_level"] is None
        assert ap["decay_state"] == "insufficient_sample"
        assert ap["bias_severities"]["chasing"] == "severe" and set(ap["bias_severities"]) == set(P.BIAS_KEYS)
        assert ap["cooldowns"] == {SOL: 2}

    def test_normalize_accepts_legacy_drawdown_level_name(self):
        ap = P.normalize_account_pack({"drawdown_level": "caution"})
        assert ap["drawdown_ladder_level"] == "caution" and "drawdown_level" not in ap

    def _state(self):
        return BotState(mode="paper", cycle=100,
                        positions={ETH: make_position(ETH, 2500.0, 90)},
                        paper_quote_balance=9000.0, peak_equity=10500.0,
                        daily=DailyStats(date="2025-10-28", start_equity=10100.0, trades=2, consecutive_losses=1),
                        cooldown_until_cycle={SOL: 103, BTC: 99}, loss_cooldown_until_cycle={SOL: 108})

    def test_make_account_pack(self):
        st = self._state()
        js = J.JournalState()
        ap = P.make_account_pack(st, 10000.0, marks={ETH: 2600.0}, journal_state=js, llm_budget_remaining_usd=4.5)
        assert tuple(sorted(ap)) == tuple(sorted(P.ACCOUNT_PACK_KEYS))
        assert ap["equity"] == 10000.0
        assert ap["daily_pnl_pct"] == pytest.approx((10000 / 10100 - 1) * 100)
        assert ap["drawdown_pct"] == pytest.approx((1 - 10000 / 10500) * 100)
        assert ap["drawdown_ladder_level"] == "block"          # 4.76% 낙폭 (≥4.5%)
        assert ap["exposure_pct"] == pytest.approx(0.4 * 2600 / 10000 * 100)
        assert ap["position_count"] == 1 and ap["consecutive_losses"] == 1 and ap["trades_today"] == 2
        assert ap["cooldowns"] == {SOL: 8}
        assert ap["noise_trade_share"] is None
        assert ap["llm_budget_remaining_usd"] == 4.5
        assert ap["decay_state"] == "insufficient_sample"

    def test_make_account_pack_uses_last_mark_and_no_journal(self):
        st = self._state()
        ap = P.make_account_pack(st, 10000.0)
        assert ap["exposure_pct"] == pytest.approx(0.4 * 2525 / 10000 * 100)
        assert ap["bias_severities"] == {k: "insufficient_sample" for k in P.BIAS_KEYS}

    def test_make_account_pack_bad_equity(self):
        ap = P.make_account_pack(self._state(), float("nan"))
        assert ap["equity"] is None and ap["drawdown_ladder_level"] == "stop_all" and ap["exposure_pct"] is None

    def test_make_account_pack_noise_share(self):
        st = self._state()
        trades = [ClosedTrade(symbol=BTC, base_amount=1, entry_price=100, exit_price=100, quote_spent=100,
                              quote_received=100, pnl_quote=0, pnl_pct=p, reason="time_stop", opened_cycle=i,
                              closed_cycle=i + 1, opened_ts=NOW + i, closed_ts=NOW + i + 1)
                  for i, p in enumerate([0.001, 0.02, -0.01, 0.0005, 0.002])]
        st = st.model_copy(update={"closed_trades": trades})
        ap = P.make_account_pack(st, 10000.0)
        assert ap["noise_trade_share"] == pytest.approx(3 / 5)


# ======================================================================= technical
class TestTechnical:
    def test_keys_exact_candidate(self):
        p = P.build_technical(BTC, make_fp(), None, None, 100)
        assert tuple(sorted(p)) == tuple(sorted(P.TECHNICAL_KEYS))
        assert p["position"] is None and p["symbol"] == BTC
        assert_common(p, "technical")

    def test_feature_pack_complete(self):
        fp = make_fp()
        p = P.build_technical(BTC, fp, None)
        assert set(p["feature_pack"]) == set(FeaturePack.model_fields)
        assert p["feature_pack"] == F.compact(fp)
        assert p["feature_pack"]["noise_floor_stop_pct"] == pytest.approx(0.9123)
        assert p["feature_pack"]["noise_ok_2pct"] is True

    def test_rounding_4_sig(self):
        p = P.build_technical(BTC, make_fp(), None)
        assert p["feature_pack"]["price"] == 64120.0
        assert p["feature_pack"]["per_tf"]["1h"]["rsi_14"] == 58.12
        assert p["feature_pack"]["cgo_60"] == 2.718

    def test_last_closes_capped_at_20(self):
        p = P.build_technical(BTC, make_fp(n_closes=50), None)
        assert len(p["feature_pack"]["last_closes_1h"]) == 20
        fp = make_fp(n_closes=50)
        assert p["feature_pack"]["last_closes_1h"] == F.compact(fp)["last_closes_1h"][-20:]
        assert_no_candles(p)

    def test_position_block(self):
        pos = make_position(BTC, 60000.0, cycle=90)
        p = P.build_technical(BTC, make_fp(), pos, 61200.0, 100)
        b = p["position"]
        assert tuple(sorted(b)) == tuple(sorted(P.TECHNICAL_POSITION_KEYS))
        assert b["unrealized_pct"] == pytest.approx(2.0)
        assert b["bars_held"] == 10
        assert b["last_mark"] == 61200.0
        assert b["opened_ts"] == NOW - 10 * H
        assert b["stop_loss_price"] == 58800.0

    def test_position_has_no_thesis_or_confidence(self):
        pos = make_position(BTC, 60000.0)
        p = P.build_technical(BTC, make_fp(), pos, 61000.0, 100)
        s = P.to_json(p)
        assert "thesis" not in keys_anywhere(p)
        assert "confidence" not in keys_anywhere(p)
        assert "문자열 논지" not in s

    def test_mark_falls_back_to_last_mark(self):
        pos = make_position(BTC, 60000.0)       # last_mark = 60600
        b = P.build_technical(BTC, make_fp(), pos)["position"]
        assert b["last_mark"] == 60600.0 and b["unrealized_pct"] == pytest.approx(1.0) and b["bars_held"] is None

    def test_legacy_hard_limits_arg_ignored(self):
        p = P.build_technical(BTC, make_fp(), None, {"max_stop_pct": 50})
        assert p["hard_limits"] == HL

    def test_deterministic_and_pure(self):
        fp = make_fp()
        before = fp.model_dump()
        a = P.to_json(P.build_technical(BTC, fp, make_position(BTC, 60000.0), 61000.0, 100))
        b = P.to_json(P.build_technical(BTC, make_fp(), make_position(BTC, 60000.0), 61000.0, 100))
        assert a == b
        assert fp.model_dump() == before

    def test_nan_becomes_null(self):
        fp = make_fp(cgo_60=float("nan"), atr_pct_1h=float("inf"))
        p = P.build_technical(BTC, fp, None)
        assert p["feature_pack"]["cgo_60"] is None and p["feature_pack"]["atr_pct_1h"] is None
        assert_json_plain(p)

    def test_size_bound(self):
        s = P.to_json(P.build_technical(BTC, make_fp(), make_position(BTC, 60000.0), 61000.0, 100))
        assert len(s) < 3500


# ======================================================================= flow
class TestFlow:
    def test_keys_exact(self):
        p = P.build_flow({BTC: make_flow_pack(BTC), ETH: make_flow_pack(ETH)}, [BTC, ETH])
        assert tuple(sorted(p)) == tuple(sorted(P.FLOW_KEYS))
        assert_common(p, "derivatives_flow")

    def test_symbols_order_and_keyset(self):
        packs = {BTC: make_flow_pack(BTC), ETH: make_flow_pack(ETH), SOL: make_flow_pack(SOL)}
        p = P.build_flow(packs, [ETH, BTC, ETH])
        assert p["symbols"] == [ETH, BTC]
        assert set(p["flow_packs"]) == {ETH, BTC}

    def test_missing_pack_is_null(self):
        p = P.build_flow({BTC: make_flow_pack(BTC)}, [BTC, SOL])
        assert p["flow_packs"][SOL] is None
        assert set(p["flow_packs"]) == set(p["symbols"])

    def test_thresholds_table(self):
        p = P.build_flow({}, [])
        assert p["thresholds"] == P.FLOW_THRESHOLDS
        assert p["symbols"] == [] and p["flow_packs"] == {}

    def test_thresholds_cannot_be_overridden(self):
        p = P.build_flow({}, [BTC], {"funding_extreme_long": 1.0})
        assert p["thresholds"] == P.FLOW_THRESHOLDS

    def test_pack_rounded(self):
        p = P.build_flow({BTC: make_flow_pack(BTC)}, [BTC])
        fp = p["flow_packs"][BTC]
        assert fp["funding_now"] == 0.0001235
        assert fp["est_liq_clusters"][0]["level"] == 61230.0
        assert set(fp) == set(FlowPack.model_fields)

    def test_deterministic(self):
        a = P.to_json(P.build_flow({BTC: make_flow_pack(BTC), ETH: make_flow_pack(ETH)}, [BTC, ETH]))
        b = P.to_json(P.build_flow({ETH: make_flow_pack(ETH), BTC: make_flow_pack(BTC)}, [BTC, ETH]))
        assert a == b


# ======================================================================= news
def _items(n=3):
    return [NewsItem(title=f"Bitcoin headline {i}", source="coindesk" if i % 2 == 0 else "decrypt",
                     url=f"https://news.example/{i}", published_ts=NOW - i * H, symbols=["BTC"],
                     tier=1 if i % 2 == 0 else 2) for i in range(n)]


class TestNews:
    def test_keys_exact(self):
        p = P.build_news(_items(), P.build_aliases([BTC]), NOW, P.feed_health_from(_items()), [])
        assert tuple(sorted(p)) == tuple(sorted(P.NEWS_KEYS))
        assert_common(p, "news_sentiment")
        assert p["now_ms"] == NOW

    def test_headline_shape(self):
        p = P.build_news(_items(2), {}, NOW, None, [])
        h0, h1 = p["headlines"]
        assert tuple(sorted(h0)) == tuple(sorted(P.HEADLINE_KEYS))
        assert h0["id"] == hashlib.sha1(b"https://news.example/0").hexdigest()[:12]
        assert h0["source_grade"] == "high" and h1["source_grade"] == "low"
        assert h0["age_h"] == 0.0 and h1["age_h"] == 1.0
        assert h0["ts"] == NOW and h0["title"] == "Bitcoin headline 0" and h0["source"] == "coindesk"

    def test_no_extra_news_fields(self):
        p = P.build_news(_items(2), {}, NOW, None, [])
        ks = keys_anywhere(p["headlines"])
        assert not ks & {"url", "tier", "red_flags", "symbols", "published_ts"}

    def test_newest_first_cap_40_dedupe(self):
        items = _items(50)
        items = list(reversed(items)) + [items[0]]       # 역순 + 중복
        items.append(NewsItem(title="no ts", source="x", url="https://news.example/nots"))
        p = P.build_news(items, {}, NOW, None, [])
        hs = p["headlines"]
        assert len(hs) == 40
        ts = [h["ts"] for h in hs]
        assert ts == sorted(ts, reverse=True)
        assert len({h["id"] for h in hs}) == 40

    def test_ts_none_sorted_last_and_age_null(self):
        items = [NewsItem(title="no ts", source="x", url="u0"), *_items(2)]
        hs = P.build_news(items, {}, NOW, None, [])["headlines"]
        assert hs[-1]["ts"] is None and hs[-1]["age_h"] is None

    def test_future_ts_age_clamped(self):
        items = [NewsItem(title="f", source="x", url="u", published_ts=NOW + H)]
        assert P.build_news(items, {}, NOW, None, [])["headlines"][0]["age_h"] == 0.0

    def test_aliases(self):
        p = P.build_news([], {"Bitcoin": BTC, "btc": BTC}, NOW, None, [])
        assert p["aliases"] == {"bitcoin": BTC, "btc": BTC}
        p = P.build_news([], [BTC, ETH], NOW, None, [])
        assert p["aliases"] == P.build_aliases([BTC, ETH])

    def test_feed_health_normalized(self):
        p = P.build_news([], {}, NOW, {"items_fetched": 5, "errors": ("a",), "junk": 1}, [])
        assert p["feed_health"] == {"items_fetched": 5, "oldest_ts": None, "newest_ts": None, "errors": ["a"]}
        p = P.build_news([], {}, NOW, None, [])
        assert tuple(sorted(p["feed_health"])) == tuple(sorted(P.FEED_HEALTH_KEYS))

    def test_prev_events_unexpired_only(self):
        prev = [{"headline_id": "old", "score": -0.66, "event_type": "hack", "symbols": [BTC], "ts": NOW - 30 * H,
                 "summary": "x"},
                {"headline_id": "new", "score": 0.33, "event_type": "etf", "symbols": [BTC], "ts": NOW - 2 * H,
                 "summary": "y", "stale": False},
                {"headline_id": "newer", "score": 1.0, "event_type": "listing", "symbols": [ETH], "ts": NOW - H},
                {"headline_id": "nots", "score": 1.0, "event_type": "listing", "symbols": [ETH], "ts": None}]
        p = P.build_news([], {}, NOW, None, prev)
        assert [e["headline_id"] for e in p["prev_events"]] == ["newer", "new"]
        for e in p["prev_events"]:
            assert tuple(sorted(e)) == tuple(sorted(P.PREV_EVENT_KEYS))

    def test_deterministic_input_order_independent(self):
        a = P.to_json(P.build_news(_items(5), [BTC], NOW, P.feed_health_from(_items(5)), []))
        b = P.to_json(P.build_news(list(reversed(_items(5))), [BTC], NOW, P.feed_health_from(_items(5)), []))
        assert a == b

    def test_size_bound_40_headlines(self):
        s = P.to_json(P.build_news(_items(60), P.build_aliases([BTC, ETH, SOL]), NOW, None, []))
        assert len(s) < 7000


# ======================================================================= bull / bear
class TestDebate:
    def test_bull_keys_exact(self):
        p = P.build_bull(**bull_args(), **bull_kw())
        assert tuple(sorted(p)) == tuple(sorted(P.BULL_KEYS))
        assert_common(p, "bull")

    def test_bear_keys_exact(self):
        p = P.build_bear(**bull_args(), corr_to_open={ETH: 0.8123456}, hv_pct=None, **bull_kw())
        assert tuple(sorted(p)) == tuple(sorted(P.BEAR_KEYS))
        assert_common(p, "bear")
        assert p["corr_to_open"] == {ETH: 0.8123}
        assert p["hv_pct"] == 42.42             # feature_pack.hv_pct_rank

    def test_bear_explicit_hv_pct(self):
        p = P.build_bear(**bull_args(), corr_to_open=None, hv_pct=88.88888, **bull_kw())
        assert p["hv_pct"] == 88.89 and p["corr_to_open"] == {}

    def test_bull_has_no_bear_keys(self):
        p = P.build_bull(**bull_args(), **bull_kw())
        ks = keys_anywhere(p)
        assert not ks & {"corr_to_open", "hv_pct", "death_paths", "greed_flags", "bear", "verdict_bear"}
        assert not ks & (set(BearOutput.model_fields) - {"symbol", "confidence", "disconfirming_evidence", "verdict"})  # regime.verdict 는 공용

    def test_bear_has_no_bull_keys(self):
        p = P.build_bear(**bull_args(), corr_to_open={ETH: 0.5}, **bull_kw())
        ks = keys_anywhere(p)
        assert "bull" not in ks
        assert not ks & (set(BullOutput.model_fields) - {"symbol", "confidence", "disconfirming_evidence"})

    def test_shared_frozen_evidence_identical(self):
        bull = P.build_bull(**bull_args(), **bull_kw())
        bear = P.build_bear(**bull_args(), corr_to_open={ETH: 0.5}, **bull_kw())
        for k in P.BULL_KEYS:
            if k != "role":
                assert bull[k] == bear[k], k

    def test_feature_extract_and_technical(self):
        p = P.build_bull(**bull_args(), **bull_kw())
        assert set(p["feature_extract"]) == set(P.FEATURE_EXTRACT_KEYS)
        assert p["technical"] == F.compact(make_tech())
        assert p["flow"] == F.compact(make_flow_view())

    def test_news_events_filtered_to_symbol(self):
        p = P.build_bull(**bull_args(), **bull_kw())
        assert [e["headline_id"] for e in p["news_events"]] == ["e1"]
        assert tuple(sorted(p["news_events"][0])) == tuple(sorted(P.NEWS_EVENT_KEYS))
        assert p["event_signal"]["n_events"] == 1
        assert p["event_signal"]["score"] == round(0.66 * 0.5 ** (2 / 12), 4)
        assert tuple(sorted(p["event_signal"])) == tuple(sorted(P.EVENT_SIGNAL_KEYS))

    def test_news_context(self):
        no = make_news_output(risk_event=True, risk_event_symbols=[BTC])
        kw = bull_kw() | {"news": no}
        p = P.build_bull(**bull_args(), **kw)
        assert p["news_context"] == {"market_tone": 0.1235, "risk_event_for_symbol": True,
                                     "feed_health_note": "피드 정상"}

    def test_news_abstained_context_null(self):
        kw = bull_kw() | {"news": None}
        p = P.build_bull(**bull_args(), **kw)
        assert p["news_context"] is None

    def test_scanner_row_with_liquidity(self):
        p = P.build_bull(**bull_args(), **bull_kw())
        row = p["scanner_row"]
        assert set(row) == {"symbol", "rank", "composite_z", "factor_table", "reasons", "liquidity"}
        assert row["liquidity"]["ok"] is True and row["composite_z"] == 1.235

    def test_scanner_row_none_for_held_only(self):
        args = bull_args() | {"scanner_row": None}
        assert P.build_bull(**args, **bull_kw())["scanner_row"] is None

    def test_regime_extract(self):
        p = P.build_bull(**bull_args(), **bull_kw())
        assert tuple(sorted(p["regime"])) == tuple(sorted(P.REGIME_EXTRACT_KEYS))
        assert "avg_pairwise_corr_1h" not in p["regime"]

    def test_position_block(self):
        pos = make_position(BTC, 60000.0, cycle=90)
        args = bull_args() | {"position": pos}
        kw = bull_kw() | {"mark": 59400.0, "cycle": 98, "thesis": make_thesis()}
        p = P.build_bull(**args, **kw)
        b = p["position"]
        assert tuple(sorted(b)) == tuple(sorted(P.DEBATE_POSITION_KEYS))
        assert b["unrealized_pct"] == pytest.approx(-1.0) and b["bars_held"] == 8
        assert b["thesis"]["horizon_bars"] == 32 and b["thesis"]["red_lines"][0]["value"] == 62000.0

    def test_position_string_thesis_never_used(self):
        pos = make_position(BTC, 60000.0)
        p = P.build_bear(**(bull_args() | {"position": pos}), **bull_kw())
        assert p["position"]["thesis"] is None
        assert "문자열 논지" not in P.to_json(p)

    def test_thesis_mapping_and_invalid(self):
        pos = make_position(BTC, 60000.0)
        good = make_thesis().model_dump()
        p = P.build_bull(**(bull_args() | {"position": pos}), **(bull_kw() | {"thesis": good}))
        assert p["position"]["thesis"]["assumptions"] == good["assumptions"]
        p = P.build_bull(**(bull_args() | {"position": pos}), **(bull_kw() | {"thesis": {"bad": 1}}))
        assert p["position"]["thesis"] is None

    def test_abstained_channels_null(self):
        args = bull_args() | {"tech": None, "flow_view": None, "news_events": []}
        p = P.build_bull(**args, **(bull_kw() | {"feature_pack": None, "news": None}))
        assert p["technical"] is None and p["flow"] is None and p["feature_extract"] is None
        assert p["news_events"] == [] and p["event_signal"] == {"score": None, "n_events": 0}
        assert_common(p, "bull")

    def test_deterministic(self):
        a = P.to_json(P.build_bear(**bull_args(), corr_to_open={ETH: 0.5, SOL: 0.2}, **bull_kw()))
        b = P.to_json(P.build_bear(**bull_args(), corr_to_open={SOL: 0.2, ETH: 0.5}, **bull_kw()))
        assert a == b

    def test_size_bound(self):
        s = P.to_json(P.build_bear(**bull_args(), corr_to_open={ETH: 0.5}, **bull_kw()))
        assert len(s) < 3500


# ======================================================================= risk
class TestRisk:
    def test_keys_exact(self):
        p = P.build_risk(**risk_args())
        assert tuple(sorted(p)) == tuple(sorted(P.RISK_KEYS))
        assert_common(p, "risk_manager")

    def test_candidates_shape(self):
        p = P.build_risk(**risk_args())
        assert [c["symbol"] for c in p["candidates"]] == [BTC, SOL]
        c = p["candidates"][0]
        assert tuple(sorted(c)) == tuple(sorted(P.RISK_CANDIDATE_KEYS))
        assert c["planned_notional_usdt"] == 1023.0
        assert c["bull"]["case_strength"] == 4 and c["bear"]["verdict"] == "survivable_if_monitored"
        assert c["technical"] == F.compact(make_tech(BTC))

    def test_positions_shape(self):
        p = P.build_risk(**risk_args())
        pos = p["positions"][0]
        assert tuple(sorted(pos)) == tuple(sorted(P.RISK_POSITION_KEYS))
        assert pos["entry_price"] == 2500.0 and pos["mark"] == 2450.0
        assert pos["unrealized_pct"] == pytest.approx(-2.0)
        assert pos["bars_held"] == 10
        assert pos["prev_assumption_status"] == ["holding", "weakening"]
        assert pos["technical"] == {"exit_flag": False, "exit_reason": None}
        assert pos["thesis"]["reasons"] == ["4시간 추세 지속"]
        assert pos["health_history"] == [7, 6]

    def test_position_technical_only_exit_flags(self):
        p = P.build_risk(**risk_args())
        assert set(p["positions"][0]["technical"]) == set(P.EXIT_BLOCK_KEYS)
        # 보유 포지션 블록에 Position.thesis 문자열·confidence 가 새지 않는다
        assert "문자열 논지" not in P.to_json(p)
        assert "confidence" not in p["positions"][0]

    def test_position_explicit_numbers(self):
        args = risk_args()
        args["positions_ctx"] = [{"symbol": ETH, "entry_price": 2000.0, "mark": 2100.0, "bars_held": 3,
                                  "thesis": None, "prev_assumption_status": [], "red_line_hits": ["x"],
                                  "health_history": [], "technical": None}]
        pos = P.build_risk(**args)["positions"][0]
        assert pos["unrealized_pct"] == pytest.approx(5.0) and pos["bars_held"] == 3
        assert pos["prev_assumption_status"] is None and pos["technical"] is None and pos["thesis"] is None

    def test_risk_packs_limited_to_relevant(self):
        p = P.build_risk(**risk_args())
        assert set(p["risk_packs"]) == {BTC, SOL, ETH}
        rp = p["risk_packs"][BTC]
        assert set(rp) == set(RiskPack.model_fields)
        assert rp["exec_cost_pct"] == 0.2035

    def test_flow_blocks_and_news(self):
        p = P.build_risk(**risk_args())
        assert set(p["flow_blocks"]) == {BTC, SOL, ETH}
        for b in p["flow_blocks"].values():
            assert tuple(sorted(b)) == tuple(sorted(P.FLOW_BLOCK_KEYS))
        assert p["news_risk_symbols"] == [SOL]

    def test_flow_blocks_mapping_input(self):
        args = risk_args() | {"flow_blocks": {BTC: {"block_new_longs": True, "stop_inside_cascade": False,
                                                    "cascade_risk": "high", "extra": 1}}}
        p = P.build_risk(**args)
        assert p["flow_blocks"] == {BTC: {"block_new_longs": True, "stop_inside_cascade": False,
                                          "cascade_risk": "high"}}

    def test_account_pack_exact(self):
        p = P.build_risk(**risk_args())
        assert tuple(sorted(p["account_pack"])) == tuple(sorted(P.ACCOUNT_PACK_KEYS))
        assert p["account_pack"]["equity"] == 10230.0

    def test_regime_full(self):
        p = P.build_risk(**risk_args())
        assert set(p["regime"]) == set(RegimePack.model_fields)

    def test_empty(self):
        p = P.build_risk([], [], {}, make_regime(), {}, [], None)
        assert p["candidates"] == [] and p["positions"] == [] and p["risk_packs"] == {}
        assert p["flow_blocks"] == {}
        assert_common(p, "risk_manager")

    def test_attribute_objects_accepted(self):
        class Ctx:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        args = risk_args()
        args["candidates_ctx"] = [Ctx(**c) for c in args["candidates_ctx"]]
        a = P.to_json(P.build_risk(**args))
        assert a == P.to_json(P.build_risk(**risk_args()))

    def test_deterministic_and_pure(self):
        args = risk_args()
        snapshot = copy.deepcopy(args["account_pack"])
        a = P.to_json(P.build_risk(**args))
        b = P.to_json(P.build_risk(**risk_args()))
        assert a == b
        assert args["account_pack"] == snapshot

    def test_size_bound_two_candidates(self):
        s = P.to_json(P.build_risk(**risk_args(2)))
        assert len(s) < 10000


# ======================================================================= head
class TestHead:
    def test_keys_exact(self):
        p = P.build_head(**head_args())
        assert tuple(sorted(p)) == tuple(sorted(P.HEAD_KEYS))
        assert_common(p, "head")

    def test_approved_shape(self):
        p = P.build_head(**head_args())
        assert [a["symbol"] for a in p["approved"]] == [BTC, SOL]
        a = p["approved"][0]
        assert tuple(sorted(a)) == tuple(sorted(P.HEAD_APPROVED_KEYS))
        assert set(a["technical"]) == set(TechnicalOutput.model_fields) | {"labels"}
        assert a["technical"]["labels"] == {"ichimoku_4h_price_vs_cloud": "above", "cgo_60": 2.718,
                                            "abnormal_volume_multiple": 1.414, "change_24h_pct": 3.142}
        assert set(a["risk"]) == set(RiskCandidateVerdict.model_fields) | set(P.HEAD_RISK_EXTRA_KEYS)
        assert a["risk"]["confidence_floor"] == 0.7 and a["risk"]["entries_paused"] is False
        assert a["risk"]["exposure_cap_pct"] == 25.0 and a["risk"]["risk_confidence"] == 0.6789
        assert a["gate"] == {"confidence_floor": 0.7, "size_multiplier": 0.5, "notes": ["caution"]}
        assert a["bull"]["case_strength"] == 4 and a["bear"]["strongest_point"]
        assert a["flow"]["flow_bias"] == "long"

    def test_approved_news_block(self):
        p = P.build_head(**head_args())
        n = p["approved"][0]["news"]
        assert tuple(sorted(n)) == tuple(sorted(P.HEAD_NEWS_KEYS))
        assert [e["headline_id"] for e in n["events"]] == ["n-" + BTC]
        assert n["risk_event_for_symbol"] is False and n["market_tone"] == 0.1235

    def test_news_abstained_null(self):
        args = head_args()
        args["approved_ctx"][0]["news_output"] = None
        p = P.build_head(**args)
        assert p["approved"][0]["news"] is None
        # 이벤트 신호는 여전히 코드가 계산(참고용)
        assert p["sentiment"]["event_signal"][BTC]["n_events"] == 1

    def test_news_block_given_directly(self):
        args = head_args()
        item = args["approved_ctx"][0]
        del item["news_output"], item["news_events"]
        item["news"] = {"events": [make_event("z", -0.33, [BTC], 2.0)], "market_tone": -0.2,
                        "risk_event_for_symbol": True}
        n = P.build_head(**args)["approved"][0]["news"]
        assert n["risk_event_for_symbol"] is True and n["events"][0]["headline_id"] == "z"

    def test_labels_without_feature_pack(self):
        args = head_args()
        del args["approved_ctx"][0]["feature_pack"]
        a = P.build_head(**args)["approved"][0]
        assert a["technical"]["labels"] == {k: None for k in P.HEAD_TECH_LABEL_KEYS}

    def test_gates_list_combined(self):
        args = head_args()
        item = args["approved_ctx"][0]
        del item["gate"]
        item["gates"] = [{"gate": "g1", "passed": True, "reasons": [], "size_multiplier": 0.5,
                          "confidence_floor": 0.65, "notes": ["a"]},
                         {"gate": "g3", "passed": True, "reasons": [], "size_multiplier": 0.5,
                          "confidence_floor": 0.75, "notes": ["a", "b"]}]
        g = P.build_head(**args)["approved"][0]["gate"]
        assert g == {"confidence_floor": 0.75, "size_multiplier": 0.25, "notes": ["a", "b"]}

    def test_flagged_shape(self):
        p = P.build_head(**head_args())
        f = p["flagged_positions"][0]
        assert tuple(sorted(f)) == tuple(sorted(P.FLAGGED_KEYS))
        assert f["unrealized_pct"] == pytest.approx(-2.0) and f["bars_held"] == 10
        assert f["technical"] == {"exit_flag": True, "exit_reason": "smc_1h choch_down"}
        assert f["risk"]["action"] == "trim" and f["flag_reason"] == "red_line_hit"
        assert f["health_history"] == [7, 6, 4] and f["red_line_hits"] == ["price < 2460"]
        assert "prev_assumption_status" not in f

    def test_sentiment_block(self):
        p = P.build_head(**head_args())
        s = p["sentiment"]
        assert tuple(sorted(s)) == tuple(sorted(P.HEAD_SENTIMENT_KEYS))
        assert s["composite"] == F.compact(make_sentiment())
        assert set(s["event_signal"]) == {BTC, SOL, ETH}
        es = s["event_signal"][BTC]
        assert tuple(sorted(es)) == tuple(sorted(P.HEAD_EVENT_SIGNAL_KEYS))
        ev = 0.66 * 0.5 ** (3 / 12)
        assert es["score"] == pytest.approx(ev, rel=1e-3)
        assert es["combined_score"] == pytest.approx(0.6 * 3 / 5 + 0.4 * ev, rel=1e-3)
        eth = s["event_signal"][ETH]
        assert eth["combined_score"] == pytest.approx(0.6 * -2 / 5 + 0.4 * -0.66 * 0.5 ** (1 / 12), rel=1e-3)

    def test_sentiment_mapping_composite(self):
        args = head_args() | {"sentiment": {"composite": make_sentiment(), "event_signal": {"junk": 1}}}
        p = P.build_head(**args)
        assert p["sentiment"]["composite"] == F.compact(make_sentiment())
        assert "junk" not in p["sentiment"]["event_signal"]

    def test_vetoed_shape(self):
        args = head_args()
        args["vetoed_ctx"] = [
            {"symbol": "XRP/USDT", "stage": "g1", "reasons": ["spread", "noise"], "technical": make_tech("XRP/USDT")},
            {"symbol": "ADA/USDT", "dropped_at": "g2", "gates": [
                {"gate": "g1", "passed": True, "reasons": []},
                {"gate": "g2", "passed": False, "reasons": ["bear_decline"]}]},
            {"symbol": "DOT/USDT", "stage": "risk_veto", "reason": "x" * 1000},
        ]
        v = P.build_head(**args)["vetoed"]
        assert v[0] == {"symbol": "XRP/USDT", "stage": "g1", "reason": "spread; noise"}
        assert v[1] == {"symbol": "ADA/USDT", "stage": "bear_decline", "reason": "bear_decline"}
        assert v[2]["stage"] == "risk_veto" and len(v[2]["reason"]) <= P.MAX_REASON_CHARS
        for row in v:
            assert tuple(sorted(row)) == tuple(sorted(P.VETOED_KEYS))
        # 탈락 후보의 상위 출력은 head 에 가지 않는다
        assert "XRP/USDT" not in [a["symbol"] for a in P.build_head(**args)["approved"]]

    def test_abstained_sorted_unique(self):
        args = head_args() | {"abstained_channels": ["news_sentiment", "bear", "news_sentiment"]}
        assert P.build_head(**args)["abstained_channels"] == ["bear", "news_sentiment"]

    def test_legacy_positional_signature(self):
        a = head_args()
        p1 = P.build_head(a["approved_ctx"], a["flagged_positions_ctx"], a["regime"], a["account_pack"],
                          a["sentiment"], a["vetoed_ctx"], {"max_stop_pct": 99}, ["bear"], now_ms=NOW)
        assert p1["hard_limits"] == HL and p1["abstained_channels"] == ["bear"]
        p2 = P.build_head(a["approved_ctx"], a["flagged_positions_ctx"], a["regime"], a["account_pack"],
                          a["sentiment"], a["vetoed_ctx"], ["bear"], now_ms=NOW)
        assert p2["abstained_channels"] == ["bear"] and p2["hard_limits"] == HL

    def test_empty(self):
        p = P.build_head([], [], make_regime(), {}, make_sentiment(), [], None, [], now_ms=NOW)
        assert p["approved"] == [] and p["flagged_positions"] == [] and p["vetoed"] == []
        assert p["sentiment"]["event_signal"] == {}
        assert_common(p, "head")

    def test_deterministic_and_pure(self):
        args = head_args()
        snap = copy.deepcopy(args["vetoed_ctx"])
        a = P.to_json(P.build_head(**args))
        b = P.to_json(P.build_head(**head_args()))
        assert a == b and args["vetoed_ctx"] == snap

    def test_size_bound_two_candidates(self):
        s = P.to_json(P.build_head(**head_args(2)))
        assert len(s) < 11000

    def test_no_candles_anywhere(self):
        assert_no_candles(P.build_head(**head_args()))


# ======================================================================= journal
def _trade():
    return ClosedTrade(symbol=BTC, base_amount=0.01, entry_price=60000.0, exit_price=61200.0, quote_spent=600.0,
                       quote_received=612.0, fee_quote_total=1.2, pnl_quote=10.8, pnl_pct=0.018,
                       reason="take_profit", opened_cycle=10, closed_cycle=30, opened_ts=NOW - 5 * H, closed_ts=NOW,
                       thesis="문자열", confidence=0.8)


class TestJournal:
    def test_post_trade_matches_journal(self):
        hist = [{"cycle": 11, "health": 7, "assumption_status": ["holding"], "red_line_hits": [], "action": "hold",
                 "exit_flag": False, "unrealized_pct": 0.5}]
        verd = J.make_entry_verdicts(technical=make_tech(), flow=make_flow_view(), bull=make_bull(),
                                     bear=make_bear(), risk=make_verdict(), head=None, event_signal_score=0.3)
        p = P.build_journal_post_trade(_trade(), make_thesis(), hist, verd, ["SEC 소송"])
        assert p == J.build_journal_post_trade(_trade(), make_thesis(), hist, verd, ["SEC 소송"])
        assert p["role"] == "journal" and p["mode"] == "post_trade" and p["hard_limits"] == HL
        assert set(p) == {"role", "mode", "trade", "thesis_history", "verdicts", "hard_limits"}
        assert p["trade"]["trade_id"] == f"{BTC}-{NOW - 5 * H}-{NOW}"
        assert p["verdicts"]["news_risk_after_entry"] == ["SEC 소송"]
        assert_json_plain(p)

    def test_daily_matches_journal(self):
        p = P.build_journal_daily({}, 7)
        assert p == J.build_journal_daily({}, 7)
        assert set(p) == {"role", "mode", "closed_count", "hard_limits", "metrics_table"}
        assert p["mode"] == "daily" and p["closed_count"] == 7 and p["hard_limits"] == HL
        assert_json_plain(p)

    def test_journal_deterministic(self):
        a = P.to_json(P.build_journal_post_trade(_trade(), None, [], None))
        b = P.to_json(P.build_journal_post_trade(_trade(), None, [], None))
        assert a == b


# ======================================================================= 교차 검사
class TestCrossCutting:
    def _all(self):
        return {
            "technical": P.build_technical(BTC, make_fp(), make_position(BTC, 60000.0), 61000.0, 100),
            "derivatives_flow": P.build_flow({BTC: make_flow_pack(BTC)}, [BTC]),
            "news_sentiment": P.build_news(_items(), [BTC], NOW, None, []),
            "bull": P.build_bull(**bull_args(), **bull_kw()),
            "bear": P.build_bear(**bull_args(), corr_to_open={ETH: 0.5}, **bull_kw()),
            "risk_manager": P.build_risk(**risk_args()),
            "head": P.build_head(**head_args()),
            "journal": P.build_journal_daily({}, 0),
        }

    def test_every_payload_has_role_and_hard_limits(self):
        for role, p in self._all().items():
            assert p["role"] == role
            assert p["hard_limits"] == HL
            assert_json_plain(p)
            assert_no_candles(p)

    def test_roles_cover_roles_registry(self):
        from tradingbot.agents.roles import ROLES

        assert set(self._all()) == set(ROLES)

    def test_sort_keys_stable(self):
        for p in self._all().values():
            s = P.to_json(p)
            assert P.to_json(json.loads(s)) == s
            assert json.dumps(p, sort_keys=True, ensure_ascii=False) == json.dumps(
                json.loads(s), sort_keys=True, ensure_ascii=False)

    def test_payload_dicts_are_independent_copies(self):
        p = P.build_technical(BTC, make_fp(), None)
        p["hard_limits"]["max_stop_pct"] = 50
        assert P.build_technical(BTC, make_fp(), None)["hard_limits"]["max_stop_pct"] == 2
        assert P.HARD_LIMITS_PAYLOAD["max_stop_pct"] == 2
