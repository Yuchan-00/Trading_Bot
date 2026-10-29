"""risk.py 적대적 테스트 (SPEC §3).

극단값·NaN·inf·음수·0·경계값을 넣어도 불변식이 깨지지 않는지 고정한다:
손절거리 ≤ 2%, 익절거리 ≤ 4%, 비중 ≤ 10%, 확신도 < 0.65면 0, 포지션 3개면 신규 0,
당일 −3% 이후 신규 0, 킬스위치면 신규 0, 비용 반영 손익비 ≥ 1.5(불가능하면 rr_impossible).
"""
from __future__ import annotations

import logging
import math

import pytest

from tradingbot.config import HardLimits
from tradingbot.models import BotState, DailyStats, OrderRequest, Position
from tradingbot import risk
from tradingbot.risk import (
    ROUND_TRIP_COST_PCT,
    RiskVerdict,
    check_exits,
    clamp_stops,
    default_stops,
    drawdown_level,
    enforce_net_rr,
    global_gates,
    net_rr,
    size_position,
    symbol_gates,
    validate_order_request,
)

NAN = float("nan")
INF = float("inf")
NINF = float("-inf")
TOL = 1e-9  # 비율 비교 허용 오차 (부동소수 잡음)
WHITELIST = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
BAD_NUMBERS = [NAN, INF, NINF, 0.0, -1.0, -1e-12]


# ---------------------------------------------------------------- 도우미
def make_position(symbol: str = "BTC/USDT", entry: float = 100.0, sl: float = 99.0, tp: float = 102.0,
                  opened_cycle: int = 10) -> Position:
    return Position(
        symbol=symbol, base_amount=1.0, entry_price=entry, quote_spent=entry, fee_quote=0.1,
        stop_loss_price=sl, take_profit_price=tp, opened_cycle=opened_cycle, opened_ts=1_700_000_000_000,
        thesis="t", confidence=0.7,
    )


def make_state(*, cycle: int = 20, positions: int = 0, peak: float = 10_000.0, start_equity: float = 10_000.0,
               realized: float = 0.0, consecutive_losses: int = 0, kill: bool = False, kill_reason: str | None = None,
               cooldown: dict[str, int] | None = None, loss_cooldown: dict[str, int] | None = None,
               llm_paused_until: int | None = None) -> BotState:
    pos = {}
    for i in range(positions):
        sym = f"P{i}/USDT"
        pos[sym] = make_position(sym)
    return BotState(
        mode="paper", cycle=cycle, positions=pos, paper_quote_balance=10_000.0, peak_equity=peak,
        daily=DailyStats(date="2026-10-28", start_equity=start_equity, realized_pnl=realized,
                         consecutive_losses=consecutive_losses),
        cooldown_until_cycle=cooldown or {}, loss_cooldown_until_cycle=loss_cooldown or {},
        kill_switch=kill, kill_reason=kill_reason, llm_paused_until_ts=llm_paused_until,
    )


def sl_pct(entry: float, sl: float) -> float:
    return (entry - sl) / entry


def tp_pct(entry: float, tp: float) -> float:
    return (tp - entry) / entry


def has_korean(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in s)


# ================================================================ 상수/모델
def test_round_trip_cost_is_two_times_fee_plus_slippage():
    assert ROUND_TRIP_COST_PCT == pytest.approx(2 * (HardLimits.FEE_PCT + HardLimits.SLIPPAGE_PCT))
    assert ROUND_TRIP_COST_PCT == pytest.approx(0.003)


def test_risk_verdict_model_forbids_extra_and_bounds_multiplier():
    v = RiskVerdict(allowed=True, reasons=[], size_multiplier=0.5)
    assert v.allowed and v.size_multiplier == 0.5
    with pytest.raises(Exception):
        RiskVerdict(allowed=True, reasons=[], size_multiplier=1.5)
    with pytest.raises(Exception):
        RiskVerdict(allowed=True, reasons=[], size_multiplier=-0.1)
    with pytest.raises(Exception):
        RiskVerdict(allowed=True, reasons=[], size_multiplier=1.0, bogus=1)


def test_module_logger_name():
    assert risk.log.name == "tradingbot.risk"


def test_hardlimits_values_assumed_by_tests():
    assert HardLimits.MAX_STOP_LOSS_PCT == 0.02
    assert HardLimits.MAX_TAKE_PROFIT_PCT == 0.04
    assert HardLimits.MAX_POSITION_PCT == 0.10
    assert HardLimits.MIN_NET_RR == 1.5
    assert HardLimits.MIN_ORDER_USDT == 10.0
    assert HardLimits.TIME_STOP_CYCLES == 96


# ================================================================ drawdown_level
@pytest.mark.parametrize(
    "equity,expected",
    [
        (10_000.0, "normal"),
        (9_800.0, "normal"),
        (9_700.000001, "normal"),
        (9_700.0, "caution"),      # 정확히 3%
        (9_600.0, "caution"),
        (9_550.000001, "caution"),
        (9_550.0, "block"),        # 정확히 4.5%
        (9_500.0, "block"),
        (9_400.000001, "block"),
        (9_400.0, "stop_all"),     # 정확히 6%
        (9_000.0, "stop_all"),
        (1.0, "stop_all"),
    ],
)
def test_drawdown_ladder_inclusive_boundaries(equity, expected):
    assert drawdown_level(equity, 10_000.0) == expected


def test_drawdown_boundary_robust_to_float_noise():
    peak = 12_345.67
    for frac, expected in [(0.03, "caution"), (0.045, "block"), (0.06, "stop_all")]:
        assert drawdown_level(peak * (1 - frac), peak) == expected


def test_drawdown_equity_above_peak_is_normal():
    assert drawdown_level(11_000.0, 10_000.0) == "normal"


@pytest.mark.parametrize("bad", [NAN, INF, NINF, 0.0, -5.0])
def test_drawdown_invalid_equity_is_stop_all(bad):
    assert drawdown_level(bad, 10_000.0) == "stop_all"


@pytest.mark.parametrize("bad_peak", [NAN, INF, NINF, 0.0, -5.0])
def test_drawdown_invalid_peak_is_normal(bad_peak):
    assert drawdown_level(10_000.0, bad_peak) == "normal"


# ================================================================ global_gates
def test_global_gates_allows_clean_state():
    v = global_gates(make_state(), 10_000.0, 0, True)
    assert v.allowed is True
    assert v.reasons == []
    assert v.size_multiplier == 1.0


def test_global_gates_kill_switch_blocks():
    v = global_gates(make_state(kill=True, kill_reason="수동 정지"), 10_000.0, 0, True)
    assert v.allowed is False
    assert v.size_multiplier == 0.0
    assert any("kill_switch" in r for r in v.reasons)
    assert any("수동 정지" in r for r in v.reasons)


def test_global_gates_kill_switch_without_reason():
    v = global_gates(make_state(kill=True), 10_000.0, 0, True)
    assert not v.allowed
    assert any("kill_switch" in r for r in v.reasons)


def test_global_gates_drawdown_caution_halves_size():
    # 당일 시작자본은 이미 낮아진 상태(전날 낙폭) -> 당일 손실 한도에는 안 걸리고 낙폭 주의만
    v = global_gates(make_state(peak=10_000.0, start_equity=9_700.0), 9_700.0, 0, True)
    assert v.allowed is True
    assert v.size_multiplier == 0.5
    assert v.reasons == []


def test_global_gates_drawdown_block():
    v = global_gates(make_state(peak=10_000.0, start_equity=9_600.0), 9_550.0, 0, True)
    assert not v.allowed
    assert any("drawdown_block" in r for r in v.reasons)
    assert not any("drawdown_stop_all" in r for r in v.reasons)


def test_global_gates_drawdown_stop_all_recommends_kill():
    v = global_gates(make_state(peak=10_000.0, start_equity=9_400.0), 9_400.0, 0, True)
    assert not v.allowed
    assert any("drawdown_stop_all" in r and "킬스위치" in r for r in v.reasons)


@pytest.mark.parametrize("realized", [-300.0, -300.0000001, -500.0, -10_000.0])
def test_global_gates_daily_loss_limit_blocks(realized):
    v = global_gates(make_state(realized=realized), 10_000.0, 0, True)
    assert not v.allowed
    assert any("daily_loss_limit" in r for r in v.reasons)


def test_global_gates_daily_loss_just_inside_limit_allows():
    v = global_gates(make_state(realized=-299.99), 10_000.0, 0, True)
    assert v.allowed


def test_global_gates_daily_loss_by_equity_change_blocks_unrealized():
    # 실현손익은 0이지만 자본이 당일 시작자본 대비 3% 빠졌다(미실현 손실) -> 막는다
    v = global_gates(make_state(start_equity=10_000.0, peak=10_000.0, realized=0.0), 9_700.0, 0, True)
    assert not v.allowed
    assert any("daily_loss_limit" in r for r in v.reasons)


@pytest.mark.parametrize("n", [3, 4, 100])
def test_global_gates_consecutive_losses_block(n):
    v = global_gates(make_state(consecutive_losses=n), 10_000.0, 0, True)
    assert not v.allowed
    assert any("consecutive_losses" in r for r in v.reasons)


def test_global_gates_two_consecutive_losses_allowed():
    assert global_gates(make_state(consecutive_losses=2), 10_000.0, 0, True).allowed


@pytest.mark.parametrize("n", [3, 4])
def test_global_gates_max_positions_block(n):
    v = global_gates(make_state(positions=n), 10_000.0, 0, True)
    assert not v.allowed
    assert any("max_positions" in r for r in v.reasons)


def test_global_gates_two_positions_allowed():
    assert global_gates(make_state(positions=2), 10_000.0, 0, True).allowed


def test_global_gates_llm_budget_reason():
    v = global_gates(make_state(), 10_000.0, 0, False)
    assert not v.allowed
    assert any("llm_budget" in r for r in v.reasons)


def test_global_gates_llm_paused_blocks_until_ts():
    st = make_state(llm_paused_until=1_000)
    assert not global_gates(st, 10_000.0, 999, True).allowed
    assert any("llm_paused" in r for r in global_gates(st, 10_000.0, 999, True).reasons)
    assert global_gates(st, 10_000.0, 1_000, True).allowed
    assert global_gates(st, 10_000.0, 5_000, True).allowed


def test_global_gates_lists_every_reason_in_korean():
    st = make_state(kill=True, positions=3, consecutive_losses=3, realized=-400.0, peak=10_000.0,
                    llm_paused_until=10)
    v = global_gates(st, 9_000.0, 0, False)
    assert not v.allowed
    tokens = ["kill_switch", "drawdown_stop_all", "daily_loss_limit", "consecutive_losses", "max_positions",
              "llm_budget", "llm_paused"]
    for tok in tokens:
        assert any(tok in r for r in v.reasons), tok
    assert all(has_korean(r) for r in v.reasons)
    # 순서: 킬스위치 -> 낙폭 -> 당일손실 -> 연속손실 -> 포지션수 -> 예산
    idx = [next(i for i, r in enumerate(v.reasons) if tok in r) for tok in tokens]
    assert idx == sorted(idx)


@pytest.mark.parametrize("bad", [NAN, INF, NINF, 0.0, -100.0])
def test_global_gates_invalid_equity_blocks(bad):
    v = global_gates(make_state(), bad, 0, True)
    assert not v.allowed
    assert any("invalid_equity" in r for r in v.reasons)


@pytest.mark.parametrize("bad", [NAN, INF, 0.0, -1.0])
def test_global_gates_invalid_start_equity_blocks(bad):
    v = global_gates(make_state(start_equity=bad), 10_000.0, 0, True)
    assert not v.allowed
    assert any("invalid_start_equity" in r for r in v.reasons)


def test_global_gates_nan_realized_pnl_blocks():
    v = global_gates(make_state(realized=NAN), 10_000.0, 0, True)
    assert not v.allowed
    assert any("invalid_realized_pnl" in r for r in v.reasons)


def test_global_gates_fresh_state_zero_peak_is_allowed():
    # 갓 시작: peak_equity=0.0 이면 낙폭 기준이 없으므로 막지 않는다
    v = global_gates(make_state(peak=0.0), 10_000.0, 0, True)
    assert v.allowed and v.size_multiplier == 1.0


def test_global_gates_does_not_mutate_state():
    st = make_state(kill=True, positions=3, realized=-500.0)
    before = st.model_dump()
    global_gates(st, 9_000.0, 0, False)
    assert st.model_dump() == before


def test_global_gates_blocked_verdict_has_zero_multiplier_even_in_caution():
    v = global_gates(make_state(peak=10_000.0, positions=3, start_equity=9_700.0), 9_700.0, 0, True)
    assert not v.allowed
    assert v.size_multiplier == 0.0


# ================================================================ symbol_gates
def test_symbol_gates_allows_fresh_symbol():
    v = symbol_gates(make_state(), "BTC/USDT", WHITELIST)
    assert v.allowed and v.reasons == [] and v.size_multiplier == 1.0


def test_symbol_gates_already_held():
    st = make_state()
    st.positions["BTC/USDT"] = make_position("BTC/USDT")
    v = symbol_gates(st, "BTC/USDT", WHITELIST)
    assert not v.allowed
    assert any("already_held" in r for r in v.reasons)
    assert v.size_multiplier == 0.0


def test_symbol_gates_without_whitelist_argument_skips_whitelist():
    v = symbol_gates(make_state(), "DOGE/USDT")
    assert v.allowed


def test_symbol_gates_outside_whitelist_blocks():
    v = symbol_gates(make_state(), "DOGE/USDT", WHITELIST)
    assert not v.allowed
    assert any("not_whitelisted" in r for r in v.reasons)


def test_symbol_gates_whitelist_is_case_sensitive_exact():
    assert not symbol_gates(make_state(), "btc/usdt", WHITELIST).allowed


@pytest.mark.parametrize("cycle,blocked", [(20, True), (23, True), (24, True), (25, False), (100, False)])
def test_symbol_gates_cooldown_inclusive_until(cycle, blocked):
    st = make_state(cycle=cycle, cooldown={"BTC/USDT": 24})
    v = symbol_gates(st, "BTC/USDT", WHITELIST)
    assert v.allowed is (not blocked)
    if blocked:
        assert any("cooldown" in r for r in v.reasons)


@pytest.mark.parametrize("cycle,blocked", [(20, True), (28, True), (29, False)])
def test_symbol_gates_revenge_window(cycle, blocked):
    st = make_state(cycle=cycle, loss_cooldown={"ETH/USDT": 28})
    v = symbol_gates(st, "ETH/USDT", WHITELIST)
    assert v.allowed is (not blocked)
    if blocked:
        assert any("revenge" in r for r in v.reasons)


def test_symbol_gates_cooldown_for_other_symbol_does_not_block():
    st = make_state(cycle=20, cooldown={"ETH/USDT": 30}, loss_cooldown={"SOL/USDT": 30})
    assert symbol_gates(st, "BTC/USDT", WHITELIST).allowed


def test_symbol_gates_collects_all_reasons():
    st = make_state(cycle=20, cooldown={"DOGE/USDT": 25}, loss_cooldown={"DOGE/USDT": 25})
    st.positions["DOGE/USDT"] = make_position("DOGE/USDT")
    v = symbol_gates(st, "DOGE/USDT", WHITELIST)
    assert not v.allowed
    joined = " ".join(v.reasons)
    for tok in ("not_whitelisted", "already_held", "cooldown", "revenge"):
        assert tok in joined
    assert all(has_korean(r) for r in v.reasons)


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_symbol_gates_invalid_symbol_blocks(bad):
    v = symbol_gates(make_state(), bad, WHITELIST)  # type: ignore[arg-type]
    assert not v.allowed
    assert any("invalid_symbol" in r for r in v.reasons)


def test_symbol_gates_accepts_list_or_generator_whitelist():
    assert symbol_gates(make_state(), "BTC/USDT", list(WHITELIST)).allowed
    assert symbol_gates(make_state(), "BTC/USDT", (s for s in WHITELIST)).allowed


# ================================================================ default_stops
def test_default_stops_without_atr_uses_half_max():
    sl, tp = default_stops(100.0, None)
    assert sl_pct(100.0, sl) == pytest.approx(0.01)
    assert tp_pct(100.0, tp) == pytest.approx(0.02)


def test_default_stops_with_small_atr():
    sl, tp = default_stops(100.0, 0.25)  # 2*0.25/100 = 0.5%
    assert sl_pct(100.0, sl) == pytest.approx(0.005)
    assert tp_pct(100.0, tp) == pytest.approx(0.01)


def test_default_stops_atr_clamped_to_max():
    sl, tp = default_stops(100.0, 5.0)  # 10% -> 2%
    assert sl_pct(100.0, sl) == pytest.approx(0.02)
    assert tp_pct(100.0, tp) == pytest.approx(0.04)


def test_default_stops_tp_clamped_independently():
    sl, tp = default_stops(100.0, 1.5)  # sl 3% -> 2%, tp 4% (상한)
    assert sl_pct(100.0, sl) <= 0.02 + TOL
    assert tp_pct(100.0, tp) <= 0.04 + TOL


def test_default_stops_custom_mult():
    sl, _ = default_stops(100.0, 0.5, atr_mult=1.0)
    assert sl_pct(100.0, sl) == pytest.approx(0.005)


@pytest.mark.parametrize("atr", BAD_NUMBERS)
def test_default_stops_invalid_atr_falls_back(atr):
    sl, tp = default_stops(100.0, atr)
    assert sl_pct(100.0, sl) == pytest.approx(0.01)
    assert tp_pct(100.0, tp) == pytest.approx(0.02)


@pytest.mark.parametrize("mult", BAD_NUMBERS)
def test_default_stops_invalid_mult_falls_back(mult):
    sl, _ = default_stops(100.0, 1.0, atr_mult=mult)
    assert sl_pct(100.0, sl) == pytest.approx(0.01)


@pytest.mark.parametrize("entry", BAD_NUMBERS)
def test_default_stops_invalid_entry_raises(entry):
    with pytest.raises(ValueError):
        default_stops(entry, 1.0)


@pytest.mark.parametrize("entry", [1e-8, 0.001, 1.0, 83_084.2, 1e9])
@pytest.mark.parametrize("atr", [None, 1e-12, 0.5, 1e6, INF])
def test_default_stops_invariants_for_any_input(entry, atr):
    sl, tp = default_stops(entry, atr)
    assert 0 < sl < entry < tp
    assert sl_pct(entry, sl) <= HardLimits.MAX_STOP_LOSS_PCT + TOL
    assert tp_pct(entry, tp) <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL


# ================================================================ clamp_stops
def test_clamp_stops_passthrough_when_within_limits():
    sl, tp = clamp_stops(100.0, 99.0, 102.0)
    assert sl == pytest.approx(99.0)
    assert tp == pytest.approx(102.0)


def test_clamp_stops_tightens_wide_stop():
    sl, tp = clamp_stops(100.0, 90.0, 102.0)
    assert sl == pytest.approx(98.0)
    assert tp == pytest.approx(102.0)


def test_clamp_stops_caps_wide_take_profit():
    sl, tp = clamp_stops(100.0, 99.0, 150.0)
    assert sl == pytest.approx(99.0)
    assert tp == pytest.approx(104.0)


def test_clamp_stops_exact_boundaries_kept():
    sl, tp = clamp_stops(100.0, 98.0, 104.0)
    assert sl == pytest.approx(98.0)
    assert tp == pytest.approx(104.0)


@pytest.mark.parametrize("bad_sl", [NAN, INF, NINF, 0.0, -1.0, 100.0, 101.0])
def test_clamp_stops_invalid_stop_replaced_with_default(bad_sl):
    sl, tp = clamp_stops(100.0, bad_sl, 102.0)
    assert sl == pytest.approx(99.0)
    assert tp == pytest.approx(102.0)


@pytest.mark.parametrize("bad_tp", [NAN, INF, NINF, 0.0, -1.0, 100.0, 99.5])
def test_clamp_stops_invalid_tp_replaced_with_2x_stop(bad_tp):
    sl, tp = clamp_stops(100.0, 99.5, bad_tp)
    assert sl == pytest.approx(99.5)
    assert tp == pytest.approx(101.0)


def test_clamp_stops_invalid_tp_default_capped_at_max():
    sl, tp = clamp_stops(100.0, 97.0, NAN)  # sl -> 2%, tp -> min(4%, 4%)
    assert sl == pytest.approx(98.0)
    assert tp == pytest.approx(104.0)


@pytest.mark.parametrize("entry", BAD_NUMBERS)
def test_clamp_stops_invalid_entry_raises(entry):
    with pytest.raises(ValueError):
        clamp_stops(entry, 1.0, 2.0)


@pytest.mark.parametrize("entry", [1e-6, 0.5, 100.0, 83_084.2, 1e8])
@pytest.mark.parametrize("sl_in", [NAN, INF, NINF, -1.0, 0.0, 1e-9, 0.5, 0.99, 1.0, 1.5, 1e9])
@pytest.mark.parametrize("tp_in", [NAN, INF, NINF, -1.0, 0.0, 0.5, 1.0, 1.01, 1.5, 1e9])
def test_clamp_stops_invariants_for_any_input(entry, sl_in, tp_in):
    sl_price = sl_in * entry if math.isfinite(sl_in) else sl_in
    tp_price = tp_in * entry if math.isfinite(tp_in) else tp_in
    sl, tp = clamp_stops(entry, sl_price, tp_price)
    assert math.isfinite(sl) and math.isfinite(tp)
    assert 0 < sl < entry < tp
    assert sl_pct(entry, sl) <= HardLimits.MAX_STOP_LOSS_PCT + TOL
    assert tp_pct(entry, tp) <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL


def test_clamp_stops_only_tightens():
    # 유효한 입력이면 손절은 올라가기만(진입가 쪽), 익절은 내려가기만
    for sl_in, tp_in in [(97.0, 105.0), (99.0, 102.0), (98.5, 110.0), (90.0, 101.0)]:
        sl, tp = clamp_stops(100.0, sl_in, tp_in)
        assert sl >= sl_in - TOL
        assert tp <= tp_in + TOL


# ================================================================ enforce_net_rr / net_rr
def test_net_rr_formula():
    # sl 1%, tp 2%: (0.02-0.003)/(0.01+0.003) = 1.3077
    assert net_rr(100.0, 99.0, 102.0) == pytest.approx((0.02 - 0.003) / (0.01 + 0.003))


@pytest.mark.parametrize("entry,sl,tp", [(NAN, 99.0, 102.0), (100.0, NAN, 102.0), (100.0, 99.0, INF),
                                         (100.0, 101.0, 102.0), (100.0, 99.0, 99.0), (0.0, 1.0, 2.0)])
def test_net_rr_invalid_inputs_minus_inf(entry, sl, tp):
    assert net_rr(entry, sl, tp) == NINF


def test_enforce_net_rr_unchanged_when_ok():
    # sl 1%, tp 4%: (0.04-0.003)/(0.013) = 2.846
    sl, tp, note = enforce_net_rr(100.0, 99.0, 104.0)
    assert sl == pytest.approx(99.0)
    assert tp == pytest.approx(104.0)
    assert "rr_impossible" not in note
    assert "net_rr" in note


def test_enforce_net_rr_tightens_stop_only():
    # sl 2%, tp 3%: (0.03-0.003)/(0.023)=1.17 -> 조임. needed = 0.027/1.5 - 0.003 = 0.015
    sl, tp, note = enforce_net_rr(100.0, 98.0, 103.0)
    assert tp == pytest.approx(103.0)
    assert sl > 98.0
    assert sl_pct(100.0, sl) == pytest.approx(0.015, abs=1e-7)
    assert net_rr(100.0, sl, tp) >= HardLimits.MIN_NET_RR - 1e-9
    assert "rr_impossible" not in note
    assert "stop_tightened" in note


def test_enforce_net_rr_exact_threshold_not_changed():
    # tp 2.4%: needed sl = (0.024-0.003)/1.5-0.003 = 0.011. sl=1.1% -> rr == 1.5 (대략)
    sl, tp, note = enforce_net_rr(100.0, 98.9, 102.4)
    assert tp == pytest.approx(102.4)
    assert sl >= 98.9 - 1e-9
    assert net_rr(100.0, sl, tp) >= HardLimits.MIN_NET_RR - 1e-9


@pytest.mark.parametrize("tp_dist", [0.0001, 0.003, 0.005, 0.0105])
def test_enforce_net_rr_impossible_when_tp_too_small(tp_dist):
    # tp 1.05%: needed = (0.0105-0.003)/1.5-0.003 = 0.002 -> 경계 포함 -> 불가능
    # tp 1.2%: needed = 0.009/1.5-0.003 = 0.003 -> 가능 (아래 테스트)
    sl, tp, note = enforce_net_rr(100.0, 99.0, 100.0 * (1 + tp_dist))
    assert "rr_impossible" in note
    assert sl == pytest.approx(99.0)
    assert tp == pytest.approx(100.0 * (1 + tp_dist))


def test_enforce_net_rr_feasible_just_above_floor():
    sl, tp, note = enforce_net_rr(100.0, 99.0, 101.2)  # needed 0.003 > 0.002
    assert "rr_impossible" not in note
    assert sl_pct(100.0, sl) == pytest.approx(0.003, abs=1e-7)
    assert net_rr(100.0, sl, tp) >= HardLimits.MIN_NET_RR - 1e-9


@pytest.mark.parametrize("bad_sl", [NAN, INF, NINF, 0.0, -1.0, 100.0, 101.0])
def test_enforce_net_rr_invalid_stop_is_impossible(bad_sl):
    sl, tp, note = enforce_net_rr(100.0, bad_sl, 104.0)
    assert "rr_impossible" in note


@pytest.mark.parametrize("bad_tp", [NAN, INF, NINF, 0.0, -1.0, 100.0, 99.0])
def test_enforce_net_rr_invalid_tp_is_impossible(bad_tp):
    _, _, note = enforce_net_rr(100.0, 99.0, bad_tp)
    assert "rr_impossible" in note


@pytest.mark.parametrize("entry", BAD_NUMBERS)
def test_enforce_net_rr_invalid_entry_raises(entry):
    with pytest.raises(ValueError):
        enforce_net_rr(entry, 1.0, 2.0)


@pytest.mark.parametrize("sl_d", [0.001, 0.002, 0.005, 0.01, 0.015, 0.02])
@pytest.mark.parametrize("tp_d", [0.001, 0.005, 0.01, 0.012, 0.02, 0.03, 0.04])
def test_enforce_net_rr_invariant_rr_or_impossible(sl_d, tp_d):
    entry = 83_084.2
    sl, tp, note = enforce_net_rr(entry, entry * (1 - sl_d), entry * (1 + tp_d))
    assert tp == pytest.approx(entry * (1 + tp_d))  # 익절은 절대 안 바뀐다
    if "rr_impossible" in note:
        assert sl == pytest.approx(entry * (1 - sl_d))  # 입력 그대로
    else:
        assert sl >= entry * (1 - sl_d) - 1e-6  # 손절은 조이기만
        if "stop_tightened" in note:
            assert sl_pct(entry, sl) > risk.MIN_FEASIBLE_STOP_PCT - 1e-12
        else:
            assert sl == pytest.approx(entry * (1 - sl_d))
        assert net_rr(entry, sl, tp) >= HardLimits.MIN_NET_RR - 1e-9


def test_pipeline_default_clamp_enforce_keeps_all_invariants():
    for entry in (0.01, 1.0, 100.0, 83_084.2):
        for atr in (None, entry * 0.001, entry * 0.01, entry * 0.1):
            sl, tp = default_stops(entry, atr)
            sl, tp = clamp_stops(entry, sl, tp)
            sl2, tp2, note = enforce_net_rr(entry, sl, tp)
            assert tp2 == pytest.approx(tp)
            assert sl2 >= sl - 1e-9
            assert sl_pct(entry, sl2) <= HardLimits.MAX_STOP_LOSS_PCT + TOL
            assert tp_pct(entry, tp2) <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL
            if "rr_impossible" not in note:
                assert net_rr(entry, sl2, tp2) >= HardLimits.MIN_NET_RR - 1e-9


# ================================================================ size_position
EQ = 10_000.0


def test_size_full_confidence_capped_at_10pct():
    # sl 1%: 위험 기반 = 15/0.013 = 1153.8 > cap 1000 -> 1000
    q, notes = size_position(EQ, 100.0, 99.0, 0.9, 1.0, EQ)
    assert q == pytest.approx(1_000.0)
    assert any("상한" in n for n in notes)


def test_size_risk_based_below_cap():
    # sl 2%: 위험 기반 = 15/0.023 = 652.17 < cap -> 652.17
    q, _ = size_position(EQ, 100.0, 98.0, 0.9, 1.0, EQ)
    assert q == pytest.approx(math.floor(EQ * 0.0015 / 0.023 * 100) / 100)
    assert q <= EQ * HardLimits.MAX_POSITION_PCT


@pytest.mark.parametrize(
    "conf,expected",
    [
        (0.6499999, 0.0), (0.65, 400.0), (0.7, 400.0), (0.7499999, 400.0),
        (0.75, 700.0), (0.8, 700.0), (0.8499999, 700.0),
        (0.85, 1_000.0), (0.95, 1_000.0), (1.0, 1_000.0),
    ],
)
def test_size_confidence_tiers_inclusive_lower_bounds(conf, expected):
    q, _ = size_position(EQ, 100.0, 99.0, conf, 1.0, EQ)
    assert q == pytest.approx(expected)


@pytest.mark.parametrize("conf", [0.0, 0.5, 0.64, -1.0, NAN, INF, NINF])
def test_size_low_or_invalid_confidence_is_zero(conf):
    q, notes = size_position(EQ, 100.0, 99.0, conf, 1.0, EQ)
    assert q == 0.0
    assert notes


def test_size_confidence_above_one_treated_as_full_tier():
    q, _ = size_position(EQ, 100.0, 99.0, 1.7, 1.0, EQ)
    assert q == pytest.approx(1_000.0)


def test_size_multiplier_applied():
    q, notes = size_position(EQ, 100.0, 99.0, 0.9, 0.5, EQ)
    assert q == pytest.approx(500.0)
    assert any("배수" in n for n in notes)


@pytest.mark.parametrize("mult", [0.0, -1.0, NAN, INF, NINF])
def test_size_zero_or_invalid_multiplier_is_zero(mult):
    q, _ = size_position(EQ, 100.0, 99.0, 0.9, mult, EQ)
    assert q == 0.0


@pytest.mark.parametrize("mult", [1.5, 100.0, 1e12])
def test_size_multiplier_above_one_clamped(mult):
    q, _ = size_position(EQ, 100.0, 99.0, 0.9, mult, EQ)
    assert q == pytest.approx(1_000.0)


def test_size_limited_by_free_quote():
    q, notes = size_position(EQ, 100.0, 99.0, 0.9, 1.0, 123.45)
    assert q == pytest.approx(123.45)
    assert any("가용" in n for n in notes)


@pytest.mark.parametrize("free", [0.0, -1.0, NAN, NINF, 9.99])
def test_size_no_or_tiny_free_quote_is_zero(free):
    q, _ = size_position(EQ, 100.0, 99.0, 0.9, 1.0, free)
    assert q == 0.0


def test_size_min_order_floor_exact_ten_allowed():
    q, _ = size_position(EQ, 100.0, 99.0, 0.9, 1.0, 10.0)
    assert q == pytest.approx(10.0)


def test_size_below_min_order_is_zero_with_note():
    q, notes = size_position(100.0, 100.0, 99.0, 0.9, 1.0, 100.0)  # cap 10, 확신 100% -> 10 ... 경계
    assert q == pytest.approx(10.0)
    q2, notes2 = size_position(99.0, 100.0, 99.0, 0.9, 1.0, 99.0)  # cap 9.9 -> 0
    assert q2 == 0.0
    assert any("below_min_order" in n for n in notes2)


@pytest.mark.parametrize("equity", BAD_NUMBERS)
def test_size_invalid_equity_is_zero(equity):
    q, notes = size_position(equity, 100.0, 99.0, 0.9, 1.0, 1e9)
    assert q == 0.0 and notes


@pytest.mark.parametrize("entry", BAD_NUMBERS)
def test_size_invalid_entry_is_zero(entry):
    q, _ = size_position(EQ, entry, 1.0, 0.9, 1.0, EQ)
    assert q == 0.0


@pytest.mark.parametrize("sl", [NAN, INF, NINF, 0.0, -1.0, 100.0, 101.0])
def test_size_invalid_stop_is_zero(sl):
    q, _ = size_position(EQ, 100.0, sl, 0.9, 1.0, EQ)
    assert q == 0.0


def test_size_tiny_stop_distance_still_capped():
    q, _ = size_position(EQ, 100.0, 99.999999, 0.9, 1.0, EQ)
    assert q == pytest.approx(1_000.0)


def test_size_wide_stop_over_limit_reduces_size():
    q_ok, _ = size_position(EQ, 100.0, 98.0, 0.9, 1.0, EQ)
    q_wide, notes = size_position(EQ, 100.0, 50.0, 0.9, 1.0, EQ)
    assert q_wide < q_ok
    assert any("stop_over_limit" in n for n in notes)


@pytest.mark.parametrize("equity", [11.0, 100.0, 1_234.56, 10_000.0, 1e7])
@pytest.mark.parametrize("sl_d", [1e-6, 0.002, 0.01, 0.02, 0.5])
@pytest.mark.parametrize("conf", [0.0, 0.65, 0.75, 0.85, 1.0, NAN])
@pytest.mark.parametrize("mult", [0.0, 0.5, 1.0, NAN])
def test_size_invariants_for_any_input(equity, sl_d, conf, mult):
    free = equity * 0.9
    q, notes = size_position(equity, 100.0, 100.0 * (1 - sl_d), conf, mult, free)
    assert math.isfinite(q) and q >= 0.0
    assert q <= equity * HardLimits.MAX_POSITION_PCT + 1e-9
    assert q <= free + 1e-9
    assert q == 0.0 or q >= HardLimits.MIN_ORDER_USDT
    if not (conf >= HardLimits.MIN_CONFIDENCE):
        assert q == 0.0
    if not (mult > 0):
        assert q == 0.0
    assert isinstance(notes, list) and all(isinstance(n, str) for n in notes)


def test_size_risk_formula_matches_spec():
    equity, entry, sl = 5_000.0, 200.0, 197.0  # sl 1.5%
    q, _ = size_position(equity, entry, sl, 0.9, 1.0, 1e9)
    expected = min(equity * 0.0015 / (0.015 + 0.003), equity * 0.10)
    assert q == pytest.approx(math.floor(expected * 100) / 100)


# ================================================================ validate_order_request
def test_validate_buy_ok():
    validate_order_request(OrderRequest(symbol="BTC/USDT", side="buy", quote_amount=50.0), WHITELIST, [])


def test_validate_sell_held_ok():
    validate_order_request(OrderRequest(symbol="DOGE/USDT", side="sell", base_amount=1.0), WHITELIST, ["DOGE/USDT"])


def test_validate_sell_whitelisted_but_not_held_ok():
    validate_order_request(OrderRequest(symbol="ETH/USDT", side="sell", base_amount=1.0), WHITELIST, [])


def test_validate_buy_outside_whitelist_raises():
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="DOGE/USDT", side="buy", quote_amount=50.0), WHITELIST, ["DOGE/USDT"])


def test_validate_sell_neither_held_nor_whitelisted_raises():
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="DOGE/USDT", side="sell", base_amount=1.0), WHITELIST, ["BTC/USDT"])


@pytest.mark.parametrize("amt", [None, 0.0, -1.0, NAN, INF, NINF])
def test_validate_buy_bad_quote_amount_raises(amt):
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="BTC/USDT", side="buy", quote_amount=amt), WHITELIST, [])


@pytest.mark.parametrize("amt", [None, 0.0, -1.0, NAN, INF, NINF])
def test_validate_sell_bad_base_amount_raises(amt):
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="BTC/USDT", side="sell", base_amount=amt), WHITELIST, ["BTC/USDT"])


def test_validate_buy_with_base_amount_raises():
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="BTC/USDT", side="buy", quote_amount=50.0, base_amount=1.0), WHITELIST, [])


def test_validate_sell_with_quote_amount_raises():
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="BTC/USDT", side="sell", base_amount=1.0, quote_amount=5.0), WHITELIST, [])


def test_validate_empty_symbol_raises():
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="", side="buy", quote_amount=50.0), ("",), [])


def test_validate_held_accepts_generator_and_set():
    validate_order_request(OrderRequest(symbol="X/USDT", side="sell", base_amount=1.0), WHITELIST, (s for s in ["X/USDT"]))
    validate_order_request(OrderRequest(symbol="X/USDT", side="sell", base_amount=1.0), WHITELIST, {"X/USDT"})


def test_validate_rejects_non_orderrequest():
    with pytest.raises(ValueError):
        validate_order_request({"symbol": "BTC/USDT", "side": "buy", "quote_amount": 1.0}, WHITELIST, [])  # type: ignore[arg-type]


def test_validate_empty_whitelist_blocks_all_buys():
    with pytest.raises(ValueError):
        validate_order_request(OrderRequest(symbol="BTC/USDT", side="buy", quote_amount=50.0), (), [])


# ================================================================ check_exits
def test_check_exits_none_when_inside_band():
    assert check_exits(make_position(), 100.5, 20) is None


@pytest.mark.parametrize("mark", [99.0, 98.99, 50.0, 1e-9])
def test_check_exits_stop_loss_inclusive(mark):
    assert check_exits(make_position(), mark, 20) == "stop_loss"


@pytest.mark.parametrize("mark", [102.0, 102.01, 1e9])
def test_check_exits_take_profit_inclusive(mark):
    assert check_exits(make_position(), mark, 20) == "take_profit"


def test_check_exits_time_stop_boundary():
    p = make_position(opened_cycle=10)
    assert check_exits(p, 100.5, 10 + 95) is None
    assert check_exits(p, 100.5, 10 + 96) == "time_stop"
    assert check_exits(p, 100.5, 10 + 500) == "time_stop"


def test_check_exits_stop_wins_over_time_stop():
    assert check_exits(make_position(opened_cycle=0), 90.0, 1000) == "stop_loss"


def test_check_exits_take_profit_wins_over_time_stop():
    assert check_exits(make_position(opened_cycle=0), 110.0, 1000) == "take_profit"


def test_check_exits_stop_wins_when_band_inverted():
    # 손절 ≥ 익절인 손상된 포지션: 마크가 둘 다 만족하면 손절(보수적)
    p = make_position(sl=101.0, tp=99.0)
    assert check_exits(p, 100.0, 20) == "stop_loss"


@pytest.mark.parametrize("mark", [NAN, INF, NINF, 0.0, -1.0])
def test_check_exits_invalid_mark_skips_price_checks(mark, caplog):
    with caplog.at_level(logging.WARNING, logger="tradingbot.risk"):
        assert check_exits(make_position(opened_cycle=10), mark, 20) is None
    assert any("마크" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("mark", [NAN, INF, 0.0])
def test_check_exits_invalid_mark_still_time_stops(mark):
    assert check_exits(make_position(opened_cycle=0), mark, 96) == "time_stop"


def test_check_exits_negative_cycle_diff_is_none():
    assert check_exits(make_position(opened_cycle=50), 100.5, 10) is None


def test_check_exits_does_not_mutate_position():
    p = make_position()
    before = p.model_dump()
    check_exits(p, 50.0, 500)
    assert p.model_dump() == before


# ================================================================ 종합 불변식 (SPEC §3 마지막 문단)
def test_spec_invariants_summary():
    st = make_state(positions=3)
    assert not global_gates(st, 10_000.0, 0, True).allowed                      # 포지션 3개면 신규 0
    assert not global_gates(make_state(realized=-300.0), 10_000.0, 0, True).allowed  # 당일 -3%
    assert not global_gates(make_state(kill=True), 10_000.0, 0, True).allowed       # 킬스위치
    assert size_position(10_000.0, 100.0, 99.0, 0.6499, 1.0, 10_000.0)[0] == 0.0  # 확신도 < 0.65
    for entry in (0.5, 100.0, 50_000.0):
        sl, tp = clamp_stops(entry, entry * 0.5, entry * 2)
        assert sl_pct(entry, sl) <= 0.02 + TOL and tp_pct(entry, tp) <= 0.04 + TOL


def test_global_gates_never_mentions_exits():
    # 게이트는 신규 진입에 대해서만 답한다: 청산을 막는 문구가 없어야 한다
    st = make_state(kill=True, positions=3, realized=-500.0, consecutive_losses=5)
    v = global_gates(st, 9_000.0, 0, False)
    assert not any("청산 금지" in r or "청산 불가" in r for r in v.reasons)
    # 청산 분류는 게이트와 무관하게 동작한다
    assert check_exits(make_position(), 50.0, 20) == "stop_loss"
