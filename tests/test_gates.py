"""gates.py 테스트 (SPEC §19 + v2.1 보완) — G0~G4 경계·기권·적대적 입력.

불변식: 어떤 입력에서도 예외가 나지 않고, G4 가 만든 계획은 손절 ≤ 2%, 익절 ≤ 4%,
비중 ≤ 10%(자본 대비), 주문액 ≤ free_quote, 손절은 head/risk/기술분석 후보보다 넓지 않다.
"""
from __future__ import annotations

import math
import random

import pytest

from tradingbot import gates
from tradingbot.agents.schemas import (
    BearOutput,
    DeathPath,
    FlowSymbolView,
    HeadDecision,
    RiskCandidateVerdict,
    TechnicalOutput,
    Thesis,
)
from tradingbot.config import HardLimits
from tradingbot.features import FeaturePack, RegimePack, RiskPack, SentimentComposite
from tradingbot.gates import (
    GateDecision,
    OrderPlan,
    combine_gates,
    g0_preflight,
    g1_candidate,
    g2_bear,
    g3_risk,
    g4_final,
    reason_codes,
)
from tradingbot.models import BotState, DailyStats, Position
from tradingbot.scanner import LiquidityGate, Thresholds

SYM = "SOL/USDT"
NAN = float("nan")
INF = float("inf")
NOW = 1_793_000_000_000  # 임의의 UTC ms (펀딩창 여부와 무관하게 인자로 준다)
TOL = 1e-9


# ====================================================================== 빌더
def make_state(*, positions: int = 0, kill: bool = False, peak: float = 10_000.0, start: float = 10_000.0,
               realized: float = 0.0, consec: int = 0, paused_until: int | None = None) -> BotState:
    pos = {}
    for i in range(positions):
        s = f"P{i}/USDT"
        pos[s] = Position(symbol=s, base_amount=1.0, entry_price=100.0, quote_spent=100.0, stop_loss_price=99.0,
                          take_profit_price=102.0, opened_cycle=1, opened_ts=1)
    return BotState(mode="paper", cycle=10, positions=pos, paper_quote_balance=10_000.0, peak_equity=peak,
                    daily=DailyStats(date="2026-10-28", start_equity=start, realized_pnl=realized,
                                     consecutive_losses=consec),
                    kill_switch=kill, kill_reason="test" if kill else None,
                    llm_paused_until_ts=paused_until, llm_pause_reason="auth" if paused_until else None)


def make_tech(**kw) -> TechnicalOutput:
    base = dict(symbol=SYM, direction="long", setup_type="trend_continuation", timing="now", mtf_agreement=True,
                score=3, confidence=0.7, stop_candidate_price=98.5, stop_pct=1.5, sl_reference="atr",
                target_candidate_price=103.0, tp_pct=3.0, invalidation="1h 종가 98.5 하회", noise_check=True,
                exit_flag=False, exit_reason=None, evidence=["per_tf.1h.vote: trend_up"])
    base.update(kw)
    return TechnicalOutput(**base)


def make_flow(**kw) -> FlowSymbolView:
    base = dict(symbol=SYM, posture="neutral", crowding_risk="low", squeeze_direction="none", cascade_risk="low",
                nearest_long_liq_cluster_pct_below=5.0, stop_inside_cascade=False, washout_setup=False,
                block_new_longs=False, block_reason=None, flow_bias="neutral", confidence=0.6,
                evidence=["funding_regime: neutral"])
    base.update(kw)
    return FlowSymbolView(**base)


def make_fp(**kw) -> FeaturePack:
    base = dict(symbol=SYM, ts=NOW, price=100.0, mtf_agreement=True, atr_pct_1h=0.8,
                noise_floor_stop_pct=0.8, noise_ok_2pct=True)
    base.update(kw)
    return FeaturePack(**base)


def make_sent(score: float | None = 55.0) -> SentimentComposite:
    return SentimentComposite(score_0_100=score, label="neutral" if score is not None else "unknown")


def g1(**kw) -> GateDecision:
    args = dict(symbol=SYM, tech=make_tech(), flow=make_flow(), news_risk_symbols=set(), sentiment=make_sent(),
                position_count=0, exposure_pct=0.0, corr_to_open={}, tech_abstained=False, fp=make_fp())
    args.update(kw)
    fp = args.pop("fp")
    return g1_candidate(**args, fp=fp)


def make_bear(verdict: str = "survivable", **kw) -> BearOutput:
    dp = DeathPath(family="liquidation_cascade", description="롱 청산 연쇄", observable_trigger="OI 급감",
                   probability_band="low", time_to_impact_bars=4)
    base = dict(symbol=SYM, death_paths=[dp, dp, dp], greed_flags=[], disconfirming_evidence=["a", "b"],
                strongest_point="펀딩 과열", what_would_disprove_bear="OI 안정", verdict=verdict,
                monitoring_conditions=["펀딩 0.05% 초과 시 청산"], confidence=0.6)
    base.update(kw)
    return BearOutput(**base)


def make_verdict(**kw) -> RiskCandidateVerdict:
    base = dict(symbol=SYM, verdict="approve", veto_reasons=[], conditions=[], max_size_pct=10.0, stop_pct=1.5,
                take_profit_pct=3.2, rr_net_estimate=1.6, bull_point_reliability=[], required_monitoring=[],
                correlation_note="정상")
    base.update(kw)
    return RiskCandidateVerdict(**base)


def make_regime(**kw) -> RegimePack:
    return RegimePack(**kw)


def make_rp(**kw) -> RiskPack:
    base = dict(symbol=SYM, spread_bps=3.0, depth_taker_usdt=40_000.0, impact_bps_est=2.5, exec_cost_pct=0.24,
                cvar_size_pct=10.0, order_notional_vs_15m_volume_share=0.002)
    base.update(kw)
    return RiskPack(**base)


def make_liq(**kw) -> LiquidityGate:
    base = dict(symbol=SYM, ok=True, reasons=[], spread_bps=3.0, quote_volume_24h=500_000_000.0,
                depth_1pct_usdt=40_000.0)
    base.update(kw)
    return LiquidityGate(**base)


def g3(**kw) -> GateDecision:
    args = dict(symbol=SYM, verdict=make_verdict(), regime=make_regime(), decay_state="healthy",
                bias={"disposition": "none", "overtrading": "none", "chasing": "none", "anchoring": "none"},
                risk_pack=make_rp(), liquidity=make_liq(), planned_notional=1_000.0, stop_pct=1.5)
    args.update(kw)
    return g3_risk(**args)


def make_head(**kw) -> HeadDecision:
    base = dict(symbol=SYM, action="BUY", confidence=0.9, size_pct=10.0, stop_pct=1.5, tp_pct=3.2,
                thesis=Thesis(reasons=["추세 지속"], assumptions=["BTC 상승 레짐"], red_lines=[], horizon_bars=16),
                disagreement_class="none", losing_side_strongest_point="펀딩", decisive_factor="mtf",
                rationale="근거")
    base.update(kw)
    return HeadDecision(**base)


def g4(**kw):
    args = dict(decision=make_head(), verdict=make_verdict(), floor=0.65, size_multiplier=1.0, equity=10_000.0,
                entry_price=100.0, atr_1h=0.8, free_quote=10_000.0)
    args.update(kw)
    tech_stop = args.pop("tech_stop_pct", None)
    return g4_final(**args, tech_stop_pct=tech_stop)


def codes(d: GateDecision) -> list[str]:
    return reason_codes(d)


def note_codes(d: GateDecision) -> list[str]:
    return [n.split(":", 1)[0] for n in d.notes]


# ====================================================================== 모델
def test_gate_decision_defaults_and_extra_forbid():
    d = GateDecision(gate="G1", passed=True)
    assert d.size_multiplier == 1.0
    assert d.confidence_floor == HardLimits.MIN_CONFIDENCE
    assert d.reasons == [] and d.notes == []
    with pytest.raises(Exception):
        GateDecision(gate="G1", passed=True, extra_field=1)


def test_gate_decision_sanitizes_multiplier_and_floor():
    assert GateDecision(gate="x", passed=True, size_multiplier=NAN).size_multiplier == 0.0
    assert GateDecision(gate="x", passed=True, size_multiplier=5.0).size_multiplier == 1.0
    assert GateDecision(gate="x", passed=True, size_multiplier=-1.0).size_multiplier == 0.0
    assert GateDecision(gate="x", passed=True, confidence_floor=0.1).confidence_floor == HardLimits.MIN_CONFIDENCE
    assert GateDecision(gate="x", passed=True, confidence_floor=NAN).confidence_floor == 1.0


def test_default_lists_not_shared():
    a = GateDecision(gate="a", passed=True)
    b = GateDecision(gate="b", passed=True)
    a.notes.append("x")
    assert b.notes == []


def test_order_plan_extra_forbid():
    OrderPlan(symbol=SYM, quote_amount=100.0, stop_loss_price=99.0, take_profit_price=102.0, size_pct=1.0, notes=[])
    with pytest.raises(Exception):
        OrderPlan(symbol=SYM, quote_amount=100.0, stop_loss_price=99.0, take_profit_price=102.0, size_pct=1.0,
                  notes=[], foo=1)


def test_combine_gates_takes_max_floor_and_product():
    a = GateDecision(gate="G2", passed=True, confidence_floor=0.75, notes=["bear_abstained"])
    b = GateDecision(gate="G3", passed=True, size_multiplier=0.5, notes=["x", "bear_abstained"])
    c = combine_gates([a, b])
    assert c.passed and c.confidence_floor == 0.75 and c.size_multiplier == 0.5
    assert c.notes == ["bear_abstained", "x"]
    d = combine_gates([a, GateDecision(gate="G3", passed=False, reasons=["r: x"])])
    assert not d.passed and d.size_multiplier == 0.0 and d.reasons == ["r: x"]


def test_combine_gates_empty_is_neutral_pass():
    c = combine_gates([])
    assert c.passed and c.size_multiplier == 1.0 and c.confidence_floor == HardLimits.MIN_CONFIDENCE


# ====================================================================== G0
def test_g0_pass_normal():
    d = g0_preflight(make_state(), 10_000.0, NOW, True, False)
    assert d.gate == "G0" and d.passed and d.size_multiplier == 1.0 and d.reasons == []


def test_g0_kill_switch_blocks():
    d = g0_preflight(make_state(kill=True), 10_000.0, NOW, True, False)
    assert not d.passed and d.size_multiplier == 0.0
    assert "kill_switch" in codes(d)


def test_g0_budget_blocks():
    d = g0_preflight(make_state(), 10_000.0, NOW, False, False)
    assert not d.passed and "llm_budget" in codes(d)


def test_g0_llm_paused_blocks():
    d = g0_preflight(make_state(paused_until=NOW + 60_000), 10_000.0, NOW, True, False)
    assert not d.passed and "llm_paused" in codes(d)


def test_g0_llm_pause_expired_passes():
    d = g0_preflight(make_state(paused_until=NOW - 1), 10_000.0, NOW, True, False)
    assert d.passed


def test_g0_drawdown_caution_halves():
    d = g0_preflight(make_state(peak=10_000.0, start=9_650.0), 9_650.0, NOW, True, False)
    assert d.passed and d.size_multiplier == 0.5
    assert "drawdown_caution" in note_codes(d)


def test_g0_drawdown_block():
    d = g0_preflight(make_state(peak=10_000.0, start=9_500.0), 9_500.0, NOW, True, False)
    assert not d.passed


def test_g0_max_positions_blocks():
    d = g0_preflight(make_state(positions=3), 10_000.0, NOW, True, False)
    assert not d.passed and "max_positions" in codes(d)


def test_g0_daily_loss_blocks():
    d = g0_preflight(make_state(realized=-300.0), 9_700.0, NOW, True, False)
    assert not d.passed


def test_g0_consecutive_losses_blocks():
    d = g0_preflight(make_state(consec=3), 10_000.0, NOW, True, False)
    assert not d.passed and "consecutive_losses" in codes(d)


def test_g0_funding_window_adds_reason_only():
    d = g0_preflight(make_state(), 10_000.0, NOW, True, True)
    assert d.passed  # 보유 검토는 계속
    assert d.size_multiplier == 0.0  # 신규 주문은 금지
    assert "funding_window" in codes(d)
    assert "funding_window" in note_codes(d)


def test_g0_funding_window_none_treated_as_inside():
    d = g0_preflight(make_state(), 10_000.0, NOW, True, None)  # type: ignore[arg-type]
    assert d.size_multiplier == 0.0


def test_g0_funding_window_with_block_keeps_both_reasons():
    d = g0_preflight(make_state(kill=True), 10_000.0, NOW, True, True)
    assert not d.passed
    assert {"kill_switch", "funding_window"} <= set(codes(d))


@pytest.mark.parametrize("eq", [NAN, INF, -1.0, 0.0])
def test_g0_invalid_equity_blocks(eq):
    d = g0_preflight(make_state(), eq, NOW, True, False)
    assert not d.passed


@pytest.mark.parametrize("now", [NAN, None, "x"])
def test_g0_invalid_now_never_raises(now):
    d = g0_preflight(make_state(paused_until=NOW), 10_000.0, now, True, False)  # type: ignore[arg-type]
    assert not d.passed


def test_g0_non_bool_budget_flag_fails_closed():
    d = g0_preflight(make_state(), 10_000.0, NOW, "yes", False)  # type: ignore[arg-type]
    assert not d.passed


def test_g0_garbage_state_is_fail_closed():
    d = g0_preflight(None, 10_000.0, NOW, True, False)  # type: ignore[arg-type]
    assert not d.passed and codes(d) == ["internal_error"]


# ====================================================================== G1 — 통과
def test_g1_technical_path_passes():
    d = g1()
    assert d.gate == "G1" and d.passed, d.reasons
    assert "setup_technical" in note_codes(d)
    assert d.confidence_floor == HardLimits.MIN_CONFIDENCE


def test_g1_confidence_boundary_055_passes():
    assert g1(tech=make_tech(confidence=0.55)).passed


def test_g1_confidence_below_055_drops():
    d = g1(tech=make_tech(confidence=0.549))
    assert not d.passed and "no_entry_setup" in codes(d)


def test_g1_mtf_false_drops():
    d = g1(tech=make_tech(mtf_agreement=False))
    assert not d.passed and "no_entry_setup" in codes(d)


def test_g1_direction_flat_drops_without_washout():
    d = g1(tech=make_tech(direction="flat"))
    assert not d.passed


def test_g1_llm_noise_false_drops():
    assert not g1(tech=make_tech(noise_check=False)).passed


# ---------------------------------------------------------------- G1 — v2.1 노이즈 재계산
def test_g1_code_noise_recomputed_ok2pct_false_drops_even_if_llm_says_true():
    d = g1(fp=make_fp(noise_ok_2pct=False))
    assert not d.passed
    assert "noise_ok_2pct" in d.reasons[0]


def test_g1_code_noise_ok2pct_none_drops():
    assert not g1(fp=make_fp(noise_ok_2pct=None)).passed


def test_g1_code_noise_stop_below_floor_drops():
    d = g1(tech=make_tech(stop_pct=0.7), fp=make_fp(noise_floor_stop_pct=0.8))
    assert not d.passed


def test_g1_code_noise_stop_equal_floor_passes():
    assert g1(tech=make_tech(stop_pct=0.8), fp=make_fp(noise_floor_stop_pct=0.8)).passed


def test_g1_code_noise_stop_none_drops():
    assert not g1(tech=make_tech(stop_pct=None)).passed


def test_g1_code_noise_floor_none_drops():
    assert not g1(fp=make_fp(noise_floor_stop_pct=None)).passed


def test_g1_missing_feature_pack_drops_technical_path():
    d = g1(fp=None)
    assert not d.passed and "no_entry_setup" in codes(d)


def test_g1_mtf_mismatch_noted():
    d = g1(fp=make_fp(mtf_agreement=False))
    assert d.passed and "mtf_mismatch" in note_codes(d)


# ---------------------------------------------------------------- G1 — washout 경로
def test_g1_washout_path_passes_with_flat_direction():
    d = g1(tech=make_tech(direction="flat", mtf_agreement=False, confidence=0.3, stop_pct=None, noise_check=False),
           flow=make_flow(washout_setup=True))
    assert d.passed, d.reasons
    assert "setup_washout" in note_codes(d)
    assert "washout_noise_warning" in note_codes(d)


def test_g1_washout_with_avoid_drops():
    d = g1(tech=make_tech(direction="avoid"), flow=make_flow(washout_setup=True))
    assert not d.passed and "no_entry_setup" in codes(d)


def test_g1_washout_requires_flow():
    d = g1(tech=make_tech(direction="flat"), flow=None)
    assert not d.passed


# ---------------------------------------------------------------- G1 — 기권
def test_g1_tech_none_drops_technical_abstained():
    d = g1(tech=None)
    assert not d.passed and codes(d)[0] == "technical_abstained"


def test_g1_tech_abstained_flag_drops_even_with_output():
    d = g1(tech_abstained=True)
    assert not d.passed and "technical_abstained" in codes(d)


def test_g1_tech_abstained_washout_does_not_rescue():
    d = g1(tech=None, flow=make_flow(washout_setup=True))
    assert not d.passed


def test_g1_flow_none_allowed_with_note():
    d = g1(flow=None)
    assert d.passed and "flow_abstained" in note_codes(d)


def test_g1_tech_symbol_mismatch_drops():
    d = g1(tech=make_tech(symbol="ETH/USDT"))
    assert not d.passed and "symbol_mismatch" in codes(d)


def test_g1_flow_symbol_mismatch_ignored_with_note():
    d = g1(flow=make_flow(symbol="ETH/USDT", block_new_longs=True))
    assert d.passed and "flow_symbol_mismatch" in note_codes(d)


# ---------------------------------------------------------------- G1 — 탈락 조건
def test_g1_block_new_longs_drops():
    d = g1(flow=make_flow(block_new_longs=True, block_reason="펀딩 극단"))
    assert not d.passed and "flow_block_new_longs" in codes(d)
    assert "펀딩 극단" in d.reasons[0]


def test_g1_stop_inside_cascade_drops():
    assert "stop_inside_cascade" in codes(g1(flow=make_flow(stop_inside_cascade=True)))


def test_g1_cascade_high_drops():
    assert "cascade_risk_high" in codes(g1(flow=make_flow(cascade_risk="high")))


def test_g1_cascade_moderate_passes():
    assert g1(flow=make_flow(cascade_risk="moderate")).passed


def test_g1_news_risk_symbol_drops():
    d = g1(news_risk_symbols={SYM})
    assert not d.passed and "news_risk_event" in codes(d)


def test_g1_news_risk_other_symbol_passes():
    assert g1(news_risk_symbols={"ETH/USDT"}).passed


def test_g1_sentiment_boundary_80_passes():
    assert g1(sentiment=make_sent(80.0)).passed


def test_g1_sentiment_above_80_drops():
    d = g1(sentiment=make_sent(80.01))
    assert not d.passed and "sentiment_overheated" in codes(d)


def test_g1_sentiment_above_80_mean_reversion_exempt():
    d = g1(tech=make_tech(setup_type="mean_reversion"), sentiment=make_sent(90.0))
    assert d.passed and "sentiment_hot_mr_exempt" in note_codes(d)


def test_g1_sentiment_unknown_noted():
    d = g1(sentiment=make_sent(None))
    assert d.passed and "sentiment_unknown" in note_codes(d)


def test_g1_sentiment_none_object_never_raises():
    d = g1(sentiment=None)
    assert d.passed and "sentiment_unknown" in note_codes(d)


@pytest.mark.parametrize("n,ok", [(0, True), (2, True), (3, False), (4, False)])
def test_g1_position_count_boundary(n, ok):
    assert g1(position_count=n).passed is ok


@pytest.mark.parametrize("bad", [NAN, None, "2", True])
def test_g1_position_count_invalid_drops(bad):
    d = g1(position_count=bad)
    assert not d.passed


@pytest.mark.parametrize("exp,ok", [(0.0, True), (29.99, True), (30.0, False), (45.0, False)])
def test_g1_exposure_boundary(exp, ok):
    assert g1(exposure_pct=exp).passed is ok


@pytest.mark.parametrize("bad", [NAN, INF, None])
def test_g1_exposure_invalid_drops(bad):
    assert not g1(exposure_pct=bad).passed


@pytest.mark.parametrize("c,ok", [(0.8, True), (-0.8, True), (0.81, False), (-0.85, False), (0.5, True)])
def test_g1_corr_boundary(c, ok):
    d = g1(corr_to_open={"BTC/USDT": c})
    assert d.passed is ok
    if not ok:
        assert "corr_to_open" in codes(d)


def test_g1_corr_nan_drops():
    # 무효 상관 → 같은 베팅 여부 판단 불가 → fail-closed 탈락
    d = g1(corr_to_open={"BTC/USDT": NAN})
    assert not d.passed and "corr_unknown" in codes(d)


def test_g1_corr_invalid_types_never_raise():
    assert g1(corr_to_open=None).passed
    assert g1(corr_to_open=[1, 2]).passed  # 형식 오류 → 생략(노트)
    assert g1(news_risk_symbols=None).passed
    assert g1(news_risk_symbols=5).passed


def test_g1_collects_all_reasons():
    d = g1(flow=make_flow(block_new_longs=True, cascade_risk="high"), news_risk_symbols={SYM}, position_count=3,
           sentiment=make_sent(95.0), corr_to_open={"BTC/USDT": 0.9})
    assert not d.passed
    assert {"flow_block_new_longs", "cascade_risk_high", "news_risk_event", "max_positions",
            "sentiment_overheated", "corr_to_open"} <= set(codes(d))
    assert d.size_multiplier == 0.0


def test_g1_reasons_are_korean():
    d = g1(tech=None)
    assert any("가" <= ch <= "힣" for ch in d.reasons[0])


def test_g1_model_construct_garbage_never_raises():
    weird_tech = TechnicalOutput.model_construct(symbol=SYM, direction="long", confidence=NAN, mtf_agreement=True,
                                                 noise_check=True, stop_pct=NAN)
    weird_flow = FlowSymbolView.model_construct(symbol=SYM)
    d = g1(tech=weird_tech, flow=weird_flow)
    assert not d.passed  # NaN 확신도·불완전 플로우(block 미상 → 차단)


# ====================================================================== G2
def test_g2_decline_drops():
    d = g2_bear(make_bear("decline"))
    assert d.gate == "G2" and not d.passed and codes(d) == ["bear_decline"]
    assert "펀딩 과열" in d.reasons[0]


def test_g2_none_passes_with_bear_abstained_and_floor_075():
    d = g2_bear(None)
    assert d.passed
    assert "bear_abstained" in d.notes
    assert d.confidence_floor == 0.75


def test_g2_survivable_passes():
    d = g2_bear(make_bear("survivable"))
    assert d.passed and d.confidence_floor == HardLimits.MIN_CONFIDENCE and d.size_multiplier == 1.0


def test_g2_survivable_if_monitored_passes_with_conditions_note():
    d = g2_bear(make_bear("survivable_if_monitored"))
    assert d.passed
    assert any("펀딩 0.05%" in n for n in d.notes)


def test_g2_invalid_verdict_fails_closed():
    d = g2_bear(BearOutput.model_construct(symbol=SYM, verdict="maybe"))
    assert not d.passed and codes(d) == ["bear_invalid_verdict"]


def test_g2_conservative_example_is_decline():
    assert not g2_bear(BearOutput.conservative_example(SYM)).passed


# ====================================================================== G3
def test_g3_approve_passes():
    d = g3()
    assert d.gate == "G3" and d.passed, d.reasons
    assert d.size_multiplier == 1.0 and d.confidence_floor == HardLimits.MIN_CONFIDENCE


def test_g3_none_drops_risk_abstained():
    d = g3(verdict=None)
    assert not d.passed and "risk_abstained" in codes(d)


def test_g3_veto_drops():
    d = g3(verdict=make_verdict(verdict="veto", veto_reasons=["상관 0.84"], max_size_pct=0.0))
    assert not d.passed and "risk_veto" in codes(d)
    assert "상관 0.84" in d.reasons[0]


def test_g3_conservative_example_drops():
    assert not g3(verdict=RiskCandidateVerdict.conservative_example(SYM)).passed


def test_g3_conditional_passes_with_note():
    d = g3(verdict=make_verdict(verdict="conditional", conditions=["사이즈 절반"]))
    assert d.passed and "risk_conditional" in note_codes(d)


def test_g3_symbol_mismatch_drops():
    assert "symbol_mismatch" in codes(g3(verdict=make_verdict(symbol="ETH/USDT")))


def test_g3_zero_max_size_drops():
    assert "risk_zero_size" in codes(g3(verdict=make_verdict(max_size_pct=0.0)))


def test_g3_fused_halves_and_raises_floor():
    d = g3(regime=make_regime(fused=True, verdict="fused"))
    assert d.passed and d.size_multiplier == 0.5 and d.confidence_floor == 0.75


def test_g3_fused_flag_only_also_applies():
    d = g3(regime=make_regime(fused=True))
    assert d.size_multiplier == 0.5 and d.confidence_floor == 0.75


def test_g3_macro_alarm_drops():
    assert "macro_alarm" in codes(g3(regime=make_regime(fused=True, btc_shock=True, verdict="macro_alarm")))


def test_g3_name_alarm_drops():
    assert "name_alarm" in codes(g3(regime=make_regime(name_alarm_symbols=[SYM])))


def test_g3_decayed_raises_floor():
    d = g3(decay_state="decayed")
    assert d.passed and d.confidence_floor == 0.75 and d.size_multiplier == 1.0


def test_g3_warning_only_notes():
    d = g3(decay_state="warning")
    assert d.confidence_floor == HardLimits.MIN_CONFIDENCE and "decay_warning" in note_codes(d)


@pytest.mark.parametrize("key", ["disposition", "overtrading", "chasing", "anchoring"])
def test_g3_any_bias_severe_raises_floor(key):
    bias = {"disposition": "none", "overtrading": "mild", "chasing": "none", "anchoring": "insufficient_sample"}
    bias[key] = "severe"
    d = g3(bias=bias)
    assert d.passed and d.confidence_floor == 0.75


def test_g3_bias_mild_keeps_floor():
    d = g3(bias={"overtrading": "mild"})
    assert d.confidence_floor == HardLimits.MIN_CONFIDENCE


def test_g3_bias_invalid_type_never_raises():
    assert g3(bias=None).passed
    assert g3(bias=["severe"]).passed


def test_g3_volume_share_exactly_5pct_not_scaled():
    # 15분 거래대금 = 96_000_000/96 = 1_000_000 ; 5% = 50_000
    d = g3(liquidity=make_liq(quote_volume_24h=96_000_000.0), planned_notional=1_000.0,
           risk_pack=make_rp(depth_taker_usdt=1e9, impact_bps_est=0.0, exec_cost_pct=0.21))
    assert d.passed and d.size_multiplier == 1.0
    d2 = g3(liquidity=make_liq(quote_volume_24h=1_920_000.0), planned_notional=1_000.0)  # 허용 = 1000 정확히
    assert d2.passed and d2.size_multiplier == pytest.approx(1.0)


def test_g3_volume_share_scales_down():
    # 15분 거래대금 = 960_000/96 = 10_000 ; 허용 = 500 ; 주문 1000 → ×0.5
    d = g3(liquidity=make_liq(quote_volume_24h=960_000.0), planned_notional=1_000.0)
    assert d.passed, d.reasons
    assert d.size_multiplier == pytest.approx(0.5)
    assert "volume_scaled" in note_codes(d)


def test_g3_volume_share_too_small_drops():
    # 15분 거래대금 = 9_600/96 = 100 ; 허용 = 5 USDT < 최소 주문 10 → 탈락
    d = g3(liquidity=make_liq(quote_volume_24h=9_600.0), planned_notional=1_000.0)
    assert not d.passed and "volume_too_small" in codes(d)


def test_g3_volume_unknown_uses_pack_share():
    d = g3(liquidity=make_liq(quote_volume_24h=None), risk_pack=make_rp(order_notional_vs_15m_volume_share=0.1))
    assert d.passed and d.size_multiplier == pytest.approx(0.5)
    assert "volume_share_fallback" in note_codes(d)


def test_g3_volume_fully_unknown_drops():
    d = g3(liquidity=make_liq(quote_volume_24h=None), risk_pack=make_rp(order_notional_vs_15m_volume_share=None))
    assert "volume_unknown" in codes(d)


@pytest.mark.parametrize("sp,ok", [(20.0, True), (20.01, False), (35.0, False)])
def test_g3_spread_boundary(sp, ok):
    d = g3(risk_pack=make_rp(spread_bps=sp, exec_cost_pct=0.21), liquidity=make_liq(spread_bps=1.0))
    assert d.passed is ok
    if not ok:
        assert "wide_spread" in codes(d)


def test_g3_spread_uses_worse_of_pack_and_liquidity():
    d = g3(risk_pack=make_rp(spread_bps=2.0), liquidity=make_liq(spread_bps=25.0))
    assert "wide_spread" in codes(d)


def test_g3_spread_threshold_override():
    th = Thresholds(MAX_SPREAD_BPS=1.0)
    d = g3(thresholds=th)  # 3bps > 2×1
    assert "wide_spread" in codes(d)


def test_g3_spread_unknown_drops():
    d = g3(risk_pack=make_rp(spread_bps=None), liquidity=make_liq(spread_bps=None))
    assert "spread_unknown" in codes(d)


@pytest.mark.parametrize("depth,ok", [(3_000.0, True), (2_999.0, False)])
def test_g3_depth_boundary(depth, ok):
    # 주문 1000 → 3배 3000. 실행비용은 충분히 낮게 유지(손절 2%).
    d = g3(risk_pack=make_rp(depth_taker_usdt=depth, impact_bps_est=0.0, exec_cost_pct=0.21), stop_pct=2.0,
           verdict=make_verdict(stop_pct=2.0))
    assert ("thin_depth" not in codes(d)) is ok


def test_g3_depth_unknown_falls_back_to_liquidity():
    d = g3(risk_pack=make_rp(depth_taker_usdt=None), liquidity=make_liq(depth_1pct_usdt=40_000.0))
    assert d.passed and "depth_fallback" in note_codes(d)


def test_g3_depth_fully_unknown_drops():
    d = g3(risk_pack=make_rp(depth_taker_usdt=None), liquidity=make_liq(depth_1pct_usdt=None))
    assert "depth_unknown" in codes(d)


def test_g3_exec_cost_v21_drop():
    # exec_cost 0.40% > 0.25 × 1.5 = 0.375
    d = g3(risk_pack=make_rp(exec_cost_pct=0.40))
    assert not d.passed and "exec_cost_too_high" in codes(d)


def test_g3_exec_cost_boundary_equal_passes():
    # 0.25 × 1.6 = 0.4
    d = g3(risk_pack=make_rp(exec_cost_pct=0.40, impact_bps_est=0.0, depth_taker_usdt=1e9),
           stop_pct=1.6, verdict=make_verdict(stop_pct=1.6))
    assert d.passed, d.reasons


def test_g3_impact_plus_fees_rule_drops_even_without_exec_cost():
    # impact 30bps → 0.30 + 0.20 = 0.50 > 0.25 × 1.5 = 0.375
    d = g3(risk_pack=make_rp(exec_cost_pct=None, impact_bps_est=30.0))
    assert "exec_cost_too_high" in codes(d)


def test_g3_impact_recomputed_from_depth_when_pack_impact_missing():
    # 주문 1000 / 깊이 3000 ×100 = 33.3bps → 0.533% > 0.25×2 = 0.5
    d = g3(risk_pack=make_rp(exec_cost_pct=None, impact_bps_est=None, depth_taker_usdt=3_000.0),
           stop_pct=2.0, verdict=make_verdict(stop_pct=2.0))
    assert "exec_cost_too_high" in codes(d)


def test_g3_exec_cost_uses_tightest_stop():
    # stop_pct 인자 1.0 이 verdict 1.5 보다 작다 → 한도 0.25 → exec 0.24 통과, 0.26 탈락
    assert g3(stop_pct=1.0).passed
    assert "exec_cost_too_high" in codes(g3(stop_pct=1.0, risk_pack=make_rp(exec_cost_pct=0.26)))


def test_g3_small_stop_always_drops_due_to_fees():
    # 왕복 수수료만 0.2% → 손절 0.79% 면 한도 0.1975% < 0.2
    d = g3(stop_pct=0.79, risk_pack=make_rp(exec_cost_pct=None, impact_bps_est=0.0, depth_taker_usdt=1e12))
    assert "exec_cost_too_high" in codes(d)


@pytest.mark.parametrize("bad", [NAN, None, -1.0, 0.0, INF])
def test_g3_invalid_stop_falls_back_to_verdict(bad):
    d = g3(stop_pct=bad)
    assert d.passed, d.reasons
    assert "stop_pct_invalid" in note_codes(d)


def test_g3_cvar_cap_scales_multiplier():
    d = g3(risk_pack=make_rp(cvar_size_pct=4.0))
    assert d.passed and d.size_multiplier == pytest.approx(0.4)
    assert "cvar_cap" in note_codes(d)


def test_g3_cvar_cap_combines_with_fused():
    d = g3(risk_pack=make_rp(cvar_size_pct=5.0), regime=make_regime(fused=True))
    assert d.size_multiplier == pytest.approx(0.25)


def test_g3_cvar_unknown_noted():
    d = g3(risk_pack=make_rp(cvar_size_pct=None))
    assert d.passed and d.size_multiplier == 1.0 and "cvar_unknown" in note_codes(d)


def test_g3_size_below_min_order_drops():
    # 주문 30 × cvar 0.3 = 9 < 10
    d = g3(planned_notional=30.0, risk_pack=make_rp(cvar_size_pct=3.0))
    assert "size_below_min_order" in codes(d)


def test_g3_liquidity_gate_failed_drops():
    d = g3(liquidity=make_liq(ok=False, reasons=["thin_depth:1<2"]))
    assert "liquidity_gate_failed" in codes(d)


@pytest.mark.parametrize("bad", [NAN, None, 0.0, -5.0, INF])
def test_g3_invalid_notional_drops(bad):
    d = g3(planned_notional=bad)
    assert not d.passed and "invalid_notional" in codes(d)


def test_g3_failure_zeroes_multiplier_but_keeps_floor():
    d = g3(verdict=None, regime=make_regime(fused=True))
    assert d.size_multiplier == 0.0 and d.confidence_floor == 0.75


def test_g3_nan_pack_values_never_raise():
    rp = RiskPack.model_construct(symbol=SYM, spread_bps=NAN, depth_taker_usdt=NAN, impact_bps_est=NAN,
                                  exec_cost_pct=NAN, cvar_size_pct=NAN, order_notional_vs_15m_volume_share=NAN)
    liq = LiquidityGate.model_construct(symbol=SYM, ok=True, reasons=[], spread_bps=NAN, quote_volume_24h=NAN,
                                        depth_1pct_usdt=NAN)
    d = g3(risk_pack=rp, liquidity=liq)
    assert not d.passed


def test_g3_none_everything_never_raises():
    d = g3_risk(SYM, None, None, None, None, None, None, None, None)  # type: ignore[arg-type]
    assert not d.passed and "risk_abstained" in codes(d)


# ====================================================================== G4
def test_g4_happy_path_plan():
    d, plan = g4()
    assert d.gate == "G4" and d.passed, d.reasons
    assert isinstance(plan, OrderPlan)
    assert plan.symbol == SYM
    # 위험 기반: 10000×0.0015/(0.015+0.003) = 833.33
    assert plan.quote_amount == pytest.approx(833.33, abs=0.01)
    assert plan.stop_loss_price == pytest.approx(98.5)
    assert plan.take_profit_price == pytest.approx(103.2)
    assert plan.size_pct == pytest.approx(8.3333, abs=1e-3)
    assert d.size_multiplier == 1.0 and d.confidence_floor == 0.65


def test_g4_not_buy_hold():
    for action in ("HOLD", "SELL"):
        d, plan = g4(decision=make_head(action=action))
        assert not d.passed and plan is None and codes(d) == ["not_buy"]


def test_g4_head_hold_factory_is_hold():
    d, plan = g4(decision=HeadDecision.hold(SYM))
    assert plan is None and not d.passed


@pytest.mark.parametrize("conf,floor,ok", [
    (0.65, 0.65, True), (0.6499, 0.65, False), (0.75, 0.75, True), (0.7499, 0.75, False), (0.8, 0.75, True),
])
def test_g4_confidence_floor_boundaries(conf, floor, ok):
    d, plan = g4(decision=make_head(confidence=conf), floor=floor)
    assert d.passed is ok, d.reasons
    assert (plan is not None) is ok
    if not ok:
        assert codes(d) == ["below_confidence_floor"]


def test_g4_floor_below_hard_min_is_raised():
    d, plan = g4(decision=make_head(confidence=0.6), floor=0.1)
    assert plan is None and d.confidence_floor == HardLimits.MIN_CONFIDENCE


@pytest.mark.parametrize("bad", [NAN, None, INF, "x"])
def test_g4_invalid_floor_holds(bad):
    d, plan = g4(floor=bad)
    assert plan is None and "invalid_floor" in codes(d)


def test_g4_confidence_065_tier_40pct():
    # 확신도 0.65 → 상한 1000 × 40% = 400 (위험 기반 833 보다 작다)
    d, plan = g4(decision=make_head(confidence=0.65))
    assert plan is not None and plan.quote_amount == pytest.approx(400.0)


def test_g4_stop_min_of_head_risk_and_2pct():
    _, plan = g4(decision=make_head(stop_pct=2.0, tp_pct=4.0), verdict=make_verdict(stop_pct=1.2))
    assert plan is not None
    assert (100.0 - plan.stop_loss_price) / 100.0 <= 0.012 + TOL


def test_g4_stop_exactly_2pct_ok():
    _, plan = g4(decision=make_head(stop_pct=2.0, tp_pct=4.0), verdict=make_verdict(stop_pct=2.0))
    assert plan is not None
    dist = (100.0 - plan.stop_loss_price) / 100.0
    assert dist <= HardLimits.MAX_STOP_LOSS_PCT
    assert dist == pytest.approx(0.02, abs=1e-6) or dist < 0.02  # 순손익비 조임이 있을 수 있다


def test_g4_stop_never_wider_than_tech_candidate():
    _, plan = g4(decision=make_head(stop_pct=1.8, tp_pct=4.0), verdict=make_verdict(stop_pct=1.8), tech_stop_pct=1.1)
    assert plan is not None
    assert (100.0 - plan.stop_loss_price) / 100.0 <= 0.011 + TOL


@pytest.mark.parametrize("bad", [NAN, -1.0, 0.0, "1"])
def test_g4_invalid_tech_stop_ignored_with_note(bad):
    d, plan = g4(tech_stop_pct=bad)
    assert plan is not None and "tech_stop_invalid" in note_codes(d)


def test_g4_size_cap_min_of_head_risk_10():
    # head 3%, risk 10% → 300 USDT 상한
    _, plan = g4(decision=make_head(size_pct=3.0))
    assert plan is not None and plan.quote_amount == pytest.approx(300.0)
    _, plan = g4(verdict=make_verdict(max_size_pct=2.0))
    assert plan is not None and plan.quote_amount == pytest.approx(200.0)


def test_g4_size_exactly_10_is_capped_at_10pct():
    # 손절 1% → 위험 기반 10000×0.0015/0.013 = 1153 → 상한 1000(10%)
    _, plan = g4(decision=make_head(stop_pct=1.0, tp_pct=2.5, size_pct=10.0), verdict=make_verdict(stop_pct=1.0))
    assert plan is not None
    assert plan.quote_amount == pytest.approx(1000.0)
    assert plan.size_pct == pytest.approx(10.0)
    assert plan.quote_amount <= 10_000.0 * HardLimits.MAX_POSITION_PCT


def test_g4_zero_size_holds():
    d, plan = g4(decision=make_head(size_pct=0.0))
    assert plan is None and codes(d) == ["zero_size"]


def test_g4_size_multiplier_applied():
    _, plan = g4(size_multiplier=0.5)
    assert plan is not None and plan.quote_amount == pytest.approx(416.66, abs=0.01)


@pytest.mark.parametrize("bad", [0.0, -1.0, NAN, None])
def test_g4_zero_or_invalid_multiplier_holds(bad):
    d, plan = g4(size_multiplier=bad)
    assert plan is None and "zero_size_multiplier" in codes(d)


def test_g4_multiplier_above_one_is_clamped():
    _, plan = g4(size_multiplier=3.0)
    assert plan is not None and plan.quote_amount == pytest.approx(833.33, abs=0.01)


def test_g4_free_quote_cap():
    _, plan = g4(free_quote=123.45)
    assert plan is not None and plan.quote_amount <= 123.45


def test_g4_free_quote_below_min_order_holds():
    d, plan = g4(free_quote=9.99)
    assert plan is None and "below_min_order" in codes(d)


def test_g4_quote_exactly_min_order_ok():
    _, plan = g4(free_quote=10.0)
    assert plan is not None and plan.quote_amount == pytest.approx(10.0)


def test_g4_small_equity_below_min_order_holds():
    d, plan = g4(equity=50.0, free_quote=50.0)
    assert plan is None and "below_min_order" in codes(d)


def test_g4_rr_impossible_holds():
    # 익절 0.5% → (0.005-0.003)/1.5-0.003 < 0.002 → 불가능
    d, plan = g4(decision=make_head(tp_pct=0.5))
    assert plan is None and codes(d) == ["rr_impossible"]


def test_g4_rr_tightens_stop_only():
    # 익절 2.5%, 손절 2% → rr=(0.025-0.003)/(0.023)=0.96 → 손절 조임
    _, plan = g4(decision=make_head(stop_pct=2.0, tp_pct=2.5), verdict=make_verdict(stop_pct=2.0))
    assert plan is not None
    sl = (100.0 - plan.stop_loss_price) / 100.0
    tp = (plan.take_profit_price - 100.0) / 100.0
    assert sl < 0.02
    assert tp == pytest.approx(0.025)
    assert (tp - 0.003) / (sl + 0.003) >= HardLimits.MIN_NET_RR - 1e-9


def test_g4_tp_capped_at_4pct():
    d = make_head().model_copy(update={"tp_pct": 9.0})
    _, plan = g4(decision=d)
    assert plan is not None and (plan.take_profit_price - 100.0) / 100.0 <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL


def test_g4_tp_fallback_to_risk_when_head_tp_zero():
    d, plan = g4(decision=make_head(tp_pct=0.0))
    assert plan is not None and "tp_from_risk" in note_codes(d)
    assert plan.take_profit_price == pytest.approx(103.2)


def test_g4_head_stop_zero_holds():
    d, plan = g4(decision=make_head(stop_pct=0.0))
    assert plan is None and "invalid_stop" in codes(d)


def test_g4_verdict_none_holds():
    d, plan = g4(verdict=None)
    assert plan is None and "risk_abstained" in codes(d)


def test_g4_verdict_veto_holds():
    d, plan = g4(verdict=make_verdict(verdict="veto", max_size_pct=0.0))
    assert plan is None and "risk_veto" in codes(d)


def test_g4_conditional_allowed():
    _, plan = g4(verdict=make_verdict(verdict="conditional", conditions=["x"]))
    assert plan is not None


def test_g4_symbol_mismatch_holds():
    d, plan = g4(verdict=make_verdict(symbol="ETH/USDT"))
    assert plan is None and "symbol_mismatch" in codes(d)


def test_g4_decision_none_holds():
    d, plan = g4(decision=None)
    assert plan is None and "head_missing" in codes(d)


@pytest.mark.parametrize("field,bad", [
    ("equity", NAN), ("equity", 0.0), ("equity", -1.0), ("equity", INF), ("equity", None),
    ("entry_price", NAN), ("entry_price", 0.0), ("entry_price", -5.0), ("entry_price", None),
    ("free_quote", NAN), ("free_quote", 0.0), ("free_quote", -1.0), ("free_quote", None),
    ("atr_1h", NAN), ("atr_1h", None), ("atr_1h", -1.0),
])
def test_g4_invalid_numeric_inputs_never_raise(field, bad):
    d, plan = g4(**{field: bad})
    if field == "atr_1h":
        assert plan is not None  # atr 은 참고용
    else:
        assert plan is None and not d.passed


def test_g4_stop_inside_atr_noted():
    d, plan = g4(atr_1h=2.0)  # ATR 2% > 손절 1.5%
    assert plan is not None and "stop_inside_atr" in note_codes(d)


def test_g4_hold_decision_has_zero_multiplier():
    d, _ = g4(decision=make_head(confidence=0.1))
    assert d.size_multiplier == 0.0


def test_g4_plan_notes_and_decision_notes_are_korean_strings():
    d, plan = g4()
    assert plan is not None and plan.notes and all(isinstance(n, str) for n in plan.notes)
    assert any("최종" in n for n in plan.notes)


def test_g4_garbage_objects_never_raise():
    head = HeadDecision.model_construct(symbol=SYM, action="BUY", confidence=NAN, size_pct=NAN, stop_pct=NAN, tp_pct=NAN)
    d, plan = g4(decision=head)
    assert plan is None and not d.passed
    head2 = HeadDecision.model_construct(symbol=SYM, action="BUY", confidence=0.9, size_pct=50.0, stop_pct=7.0, tp_pct=20.0)
    verdict2 = RiskCandidateVerdict.model_construct(symbol=SYM, verdict="approve", max_size_pct=50.0, stop_pct=9.0,
                                                    take_profit_pct=20.0)
    d2, plan2 = g4(decision=head2, verdict=verdict2)
    assert plan2 is not None
    assert (100.0 - plan2.stop_loss_price) / 100.0 <= HardLimits.MAX_STOP_LOSS_PCT + TOL
    assert (plan2.take_profit_price - 100.0) / 100.0 <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL
    assert plan2.quote_amount <= 10_000.0 * HardLimits.MAX_POSITION_PCT + 1e-6
    assert plan2.size_pct <= 10.0 + 1e-9
    _, plan3 = g4(decision=object(), verdict=object())  # type: ignore[arg-type]
    assert plan3 is None


def test_g4_internal_error_fail_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-token-should-not-leak")

    monkeypatch.setattr(gates.risk, "size_position", boom)
    d, plan = g4()
    assert plan is None and not d.passed and codes(d) == ["internal_error"]
    assert "secret" not in " ".join(d.reasons)


def test_g4_entry_extreme_prices():
    for entry in (1e-6, 0.0123, 65_000.0, 1e9):
        d, plan = g4(entry_price=entry)
        assert plan is not None, (entry, d.reasons)
        assert (entry - plan.stop_loss_price) / entry <= HardLimits.MAX_STOP_LOSS_PCT + TOL
        assert (plan.take_profit_price - entry) / entry <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL


# ====================================================================== 적대적 퍼징 — G4 불변식
def _rand_val(rng: random.Random, lo: float, hi: float):
    r = rng.random()
    if r < 0.05:
        return NAN
    if r < 0.08:
        return INF
    if r < 0.12:
        return -rng.uniform(0, hi)
    if r < 0.15:
        return 0.0
    if r < 0.2:
        return rng.uniform(hi, hi * 10)
    return rng.uniform(lo, hi)


def test_g4_fuzz_invariants():
    rng = random.Random(20261028)
    produced = 0
    for _ in range(3000):
        head = HeadDecision.model_construct(
            symbol=SYM, action=rng.choice(["BUY", "BUY", "BUY", "SELL", "HOLD"]),
            confidence=_rand_val(rng, 0.0, 1.0), size_pct=_rand_val(rng, 0.0, 10.0),
            stop_pct=_rand_val(rng, 0.0, 2.0), tp_pct=_rand_val(rng, 0.0, 4.0))
        verdict = RiskCandidateVerdict.model_construct(
            symbol=SYM, verdict=rng.choice(["approve", "conditional", "veto"]),
            max_size_pct=_rand_val(rng, 0.0, 10.0), stop_pct=_rand_val(rng, 0.0, 2.0),
            take_profit_pct=_rand_val(rng, 0.0, 4.0))
        equity = _rand_val(rng, 10.0, 100_000.0)
        entry = _rand_val(rng, 0.001, 70_000.0)
        free = _rand_val(rng, 0.0, 100_000.0)
        tech_stop = rng.choice([None, _rand_val(rng, 0.0, 2.0)])
        floor = rng.choice([0.65, 0.75, _rand_val(rng, 0.0, 1.0)])
        mult = _rand_val(rng, 0.0, 1.0)
        d, plan = g4_final(head, verdict, floor, mult, equity, entry, rng.choice([None, 1.0, NAN]), free,
                           tech_stop_pct=tech_stop)
        assert isinstance(d, GateDecision)
        assert d.passed is (plan is not None)
        if plan is None:
            continue
        produced += 1
        e = float(entry)
        sl_dist = (e - plan.stop_loss_price) / e
        tp_dist = (plan.take_profit_price - e) / e
        assert 0.0 < sl_dist <= HardLimits.MAX_STOP_LOSS_PCT + TOL
        assert 0.0 < tp_dist <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL
        assert sl_dist <= float(head.stop_pct) / 100.0 + TOL
        assert sl_dist <= float(verdict.stop_pct) / 100.0 + TOL
        if isinstance(tech_stop, float) and math.isfinite(tech_stop) and tech_stop > 0:
            assert sl_dist <= tech_stop / 100.0 + TOL
        assert plan.quote_amount >= HardLimits.MIN_ORDER_USDT
        assert plan.quote_amount <= float(equity) * HardLimits.MAX_POSITION_PCT + 1e-6
        assert plan.quote_amount <= float(free) + 1e-6
        assert plan.size_pct <= 10.0 + 1e-6
        assert plan.size_pct <= min(float(head.size_pct), float(verdict.max_size_pct)) + 1e-6
        assert float(head.confidence) >= max(HardLimits.MIN_CONFIDENCE, float(floor)) - 1e-12
        assert head.action == "BUY" and verdict.verdict in ("approve", "conditional")
        assert (plan.take_profit_price / e - 1 - 0.003) / (sl_dist + 0.003) >= HardLimits.MIN_NET_RR - 1e-6
    assert produced > 50  # 퍼징이 실제로 계획을 만들어 봤는지


def test_gate_pipeline_end_to_end_combination():
    """G1→G2→G3 결합 후 G4: FUSED + 약세 기권이면 하한 0.75·배수 0.5 로 진입."""
    d1 = g1()
    d2 = g2_bear(None)
    d3 = g3(regime=make_regime(fused=True))
    comb = combine_gates([d1, d2, d3])
    assert comb.passed and comb.confidence_floor == 0.75 and comb.size_multiplier == 0.5
    d4, plan = g4(decision=make_head(confidence=0.74), floor=comb.confidence_floor, size_multiplier=comb.size_multiplier)
    assert plan is None and codes(d4) == ["below_confidence_floor"]
    d4, plan = g4(decision=make_head(confidence=0.8), floor=comb.confidence_floor, size_multiplier=comb.size_multiplier,
                  tech_stop_pct=1.5)
    assert plan is not None
    # 확신도 0.8 → 티어 70% = 700, 위험 기반 833 → 700 × 0.5 = 350
    assert plan.quote_amount == pytest.approx(350.0)


def test_reason_codes_g0_extracts_parenthesized_token():
    d = g0_preflight(make_state(kill=True), 10_000.0, NOW, False, False)
    assert "kill_switch" in reason_codes(d) and "llm_budget" in reason_codes(d)


# ---------------------------------------------------------------- G4 실행비용 재검사 (MONEY-7)
def test_g4_exec_cost_none_is_backward_compatible():
    d0, p0 = g4()
    d1, p1 = g4(exec_cost_pct=None)
    assert d1.passed and p1 is not None and p0 is not None
    assert p1.quote_amount == p0.quote_amount and p1.stop_loss_price == p0.stop_loss_price
    assert not any(n.startswith("exec_cost") for n in d1.notes)


def test_g4_exec_cost_boundary_at_quarter_of_final_stop():
    # 최종 손절 1.5% → 기준 0.25×1.5 = 0.375%
    d, plan = g4(exec_cost_pct=0.375)
    assert d.passed and plan is not None, d.reasons
    assert any(n.startswith("exec_cost_final_ok") for n in d.notes)
    d, plan = g4(exec_cost_pct=0.376)
    assert plan is None and not d.passed and codes(d) == ["exec_cost_too_high"]
    assert d.size_multiplier == 0.0
    assert "최종 손절거리" in d.reasons[0] and "HOLD" in d.reasons[0]


def test_g4_exec_cost_rechecked_after_enforce_net_rr_tightens_stop():
    # 손절 2% → G3 기준 0.5% 통과 수준의 비용(0.45%)이라도, net-RR 이 손절을 조이면 최종 손절로 다시 본다
    head, verdict = make_head(stop_pct=2.0, tp_pct=2.5), make_verdict(stop_pct=2.0)
    _, base = g4(decision=head, verdict=verdict)
    assert base is not None
    final_stop_pct = (100.0 - base.stop_loss_price) / 100.0 * 100.0
    assert final_stop_pct < 2.0 and 0.45 > 0.25 * final_stop_pct   # 조여진 손절 기준을 넘는다
    assert 0.45 <= 0.25 * 2.0                                      # 조이기 전 손절(2%) 기준으로는 통과
    d, plan = g4(decision=head, verdict=verdict, exec_cost_pct=0.45)
    assert plan is None and codes(d) == ["exec_cost_too_high"]
    # 최종 손절 기준 바로 아래 비용은 통과
    ok_cost = 0.25 * final_stop_pct - 1e-6
    d, plan = g4(decision=head, verdict=verdict, exec_cost_pct=ok_cost)
    assert d.passed and plan is not None, d.reasons


@pytest.mark.parametrize("bad", [NAN, INF, "x", True])
def test_g4_invalid_exec_cost_ignored_with_note(bad):
    d, plan = g4(exec_cost_pct=bad)
    assert d.passed and plan is not None
    assert any(n.startswith("exec_cost_invalid") for n in d.notes)


# ---------------------------------------------------------------- G3 → G4 실행비용 전달(MONEY-7 보완)
def test_g3_records_effective_exec_cost_on_risk_pack():
    """G3 가 스캐너 스프레드로 잰 비용(0.315%)이 팩 값(0.24%)보다 크면 팩에 올려 둔다 — G4 재검사가 같은 값을 본다."""
    rp = make_rp(exec_cost_pct=0.24)
    d = g3(risk_pack=rp, liquidity=make_liq(spread_bps=18.0))
    assert d.passed, d.reasons
    assert rp.exec_cost_pct == pytest.approx(2.5 / 100 + 18.0 / 200 + 0.20)


def test_g3_records_exec_cost_when_pack_value_missing():
    rp = make_rp(exec_cost_pct=None, impact_bps_est=12.5)
    assert g3(risk_pack=rp, stop_pct=2.0).passed
    assert rp.exec_cost_pct == pytest.approx(12.5 / 100 + 3.0 / 200 + 0.20)


def test_g3_never_lowers_pack_exec_cost():
    rp = make_rp(exec_cost_pct=0.30, impact_bps_est=0.0, depth_taker_usdt=1e9)
    g3(risk_pack=rp)
    assert rp.exec_cost_pct == pytest.approx(0.30)


def test_g3_exec_cost_record_tolerates_immutable_pack():
    from types import MappingProxyType

    rp = MappingProxyType(make_rp(exec_cost_pct=0.10).model_dump())
    assert g3(risk_pack=rp).passed
