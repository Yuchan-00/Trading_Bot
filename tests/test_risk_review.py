"""적대적 리뷰 테스트 — tradingbot/risk.py (SPEC §3).

각 테스트는 현재 구현에서 **실패**해야 하며, 결함이 고쳐지면 통과한다.
구현 파일은 건드리지 않는다.
"""
from __future__ import annotations

import math

import pytest

from tradingbot import risk
from tradingbot.config import HardLimits
from tradingbot.models import BotState, DailyStats, Position

NAN = float("nan")
TOL = 1e-9  # 비율 비교 허용 오차(부동소수 잡음만 흡수, 실제 초과는 잡는다)


def _state(**kw) -> BotState:
    base = dict(
        mode="paper",
        cycle=10,
        daily=DailyStats(date="2026-10-28", start_equity=10_000.0),
        peak_equity=10_000.0,
        paper_quote_balance=10_000.0,
    )
    base.update(kw)
    return BotState(**base)


def _pos(symbol="BTC/USDT", entry=100.0, sl=99.0, tp=102.0, opened_cycle=0) -> Position:
    return Position(
        symbol=symbol, base_amount=1.0, entry_price=entry, quote_spent=entry,
        stop_loss_price=sl, take_profit_price=tp, opened_cycle=opened_cycle, opened_ts=0,
    )


# ---------------------------------------------------------------- F1: enforce_net_rr 가 하드 리밋을 넘긴다
@pytest.mark.parametrize(
    "entry,sl,tp",
    [
        (100.0, 95.0, 104.0),      # 손절 5% 입력 -> 2.17%로 "조여" 반환(2% 상한 초과)
        (100.0, 90.0, 104.0),      # 손절 10% 입력
        (83_084.2, 80_000.0, 83_084.2 * 1.04),   # 손절 3.7%, 익절 4% -> 2.17% 반환
    ],
)
def test_f1_enforce_net_rr_never_returns_stop_beyond_max_stop_loss(entry, sl, tp):
    """SPEC §3 불변식: 어떤 입력을 넣어도 손절거리 ≤ MAX_STOP_LOSS_PCT.

    enforce_net_rr 는 손절을 "조이기만" 한다고 했으나, 상한을 넘는 입력은 상한까지 조이지 않고
    2%보다 먼 손절을 그대로 돌려준다(rr_impossible 도 아님). 호출자는 이 값을 그대로 주문에 쓴다.
    """
    out_sl, out_tp, note = risk.enforce_net_rr(entry, sl, tp)
    if "rr_impossible" in note:
        return  # HOLD 는 허용
    sl_dist = (entry - out_sl) / entry
    assert sl_dist <= HardLimits.MAX_STOP_LOSS_PCT + TOL, (
        f"enforce_net_rr 가 손절거리 {sl_dist:.4%} > {HardLimits.MAX_STOP_LOSS_PCT:.0%} 를 반환: {note}"
    )


@pytest.mark.parametrize(
    "entry,sl,tp",
    [
        (100.0, 98.0, 110.0),      # 익절 10% 입력 -> 그대로 통과, note 는 "net_rr=4.22 ≥ 1.5"
        (100.0, 99.0, 150.0),
        (0.5, 0.495, 0.6),
    ],
)
def test_f1_enforce_net_rr_never_returns_tp_beyond_max_take_profit(entry, sl, tp):
    """SPEC §3 불변식: 어떤 입력을 넣어도 익절거리 ≤ MAX_TAKE_PROFIT_PCT.

    상한 밖의 익절은 손익비 계산을 부풀려(실제로는 도달 불가능한 익절) 손절을 덜 조이게 만들고,
    그 익절가가 그대로 포지션에 기록된다.
    """
    out_sl, out_tp, note = risk.enforce_net_rr(entry, sl, tp)
    if "rr_impossible" in note:
        return
    tp_dist = (out_tp - entry) / entry
    assert tp_dist <= HardLimits.MAX_TAKE_PROFIT_PCT + TOL, (
        f"enforce_net_rr 가 익절거리 {tp_dist:.4%} > {HardLimits.MAX_TAKE_PROFIT_PCT:.0%} 를 반환: {note}"
    )


# ---------------------------------------------------------------- F2: clamp_stops 출력에 대해 size_position 이 "상한 초과" 라고 거짓 기록
@pytest.mark.parametrize("entry", [0.1, 7.77, 59.99, 83_084.2, 12_345.678, 1e-6])
def test_f2_size_position_does_not_flag_stop_over_limit_for_clamped_stop(entry):
    """clamp_stops 가 상한(2%)으로 조인 손절을 size_position 에 넣으면 'stop_over_limit' 노트가 나오면 안 된다.

    현재는 부동소수 잡음(0.020000000000000018 > 0.02)으로 거의 모든 진입가에서
    "손절거리 2.00% > 상한 2% (stop_over_limit)" 가 결정 로그/텔레그램에 남는다 — 허위 경고.
    """
    sl, tp = risk.clamp_stops(entry, entry * 0.5, entry * 2.0)
    quote, notes = risk.size_position(10_000.0, entry, sl, 0.9, 1.0, 1e9)
    assert quote > 0
    assert not any("stop_over_limit" in n for n in notes), notes


# ---------------------------------------------------------------- F3: 무효한 시각/사이클 입력에서 게이트가 열려 버린다(fail-open)
@pytest.mark.parametrize("bad_now", [NAN, math.inf, -math.inf])
def test_f3_global_gates_llm_pause_fails_closed_on_invalid_now_ts(bad_now):
    """llm_paused_until_ts 가 설정돼 있는데 now_ts 가 NaN/inf 면 일시정지 게이트가 **생략**되어 allowed=True.

    이 모듈의 다른 모든 무효 입력(자본·시작자본·실현손익)은 '막는' 쪽으로 해석하는데,
    시각만 '통과' 쪽으로 해석한다. 보수적으로 막아야 한다.
    """
    st = _state(llm_paused_until_ts=2_000_000_000_000, llm_pause_reason="auth")
    v = risk.global_gates(st, 10_000.0, bad_now, True)
    assert v.allowed is False, v
    assert v.size_multiplier == 0.0


@pytest.mark.parametrize("bad_cycle", [NAN, math.inf])
def test_f3_check_exits_time_stop_fails_closed_on_invalid_cycle(bad_cycle):
    """current_cycle 이 NaN/inf 면 시간손절 검사가 조용히 생략된다(None).

    가격 조건은 경고라도 남기지만 사이클 무효는 경고도 없이 포지션을 무기한 유지한다.
    무효 사이클은 예외(ValueError)로 드러나거나 최소한 경고가 남아야 한다.
    """
    p = _pos(opened_cycle=0)
    with pytest.raises(ValueError):
        risk.check_exits(p, 100.0, bad_cycle)


# ---------------------------------------------------------------- F4: symbol_gates 가 문자열 완전일치에만 의존 — 대소문자/공백 변형으로 우회
@pytest.mark.parametrize("variant", ["btc/usdt", " BTC/USDT", "BTC/USDT ", "Btc/Usdt"])
def test_f4_symbol_gates_already_held_not_bypassed_by_case_or_whitespace(variant):
    """BTC/USDT 보유 중에 'btc/usdt' 로 물으면 already_held 가 안 걸린다(whitelist 미전달 시).

    config 는 심볼을 대문자로 정규화하지만 symbol_gates 는 LLM/호출자 문자열을 그대로 쓴다.
    같은 종목에 중복 진입이 가능해지고 MAX_POSITIONS 계산도 두 개로 센다.
    """
    st = _state(positions={"BTC/USDT": _pos()})
    v = risk.symbol_gates(st, variant)
    assert v.allowed is False, v


@pytest.mark.parametrize("variant", ["btc/usdt", " BTC/USDT"])
def test_f4_symbol_gates_cooldown_and_revenge_not_bypassed_by_case_or_whitespace(variant):
    st = _state(cycle=10, cooldown_until_cycle={"BTC/USDT": 14}, loss_cooldown_until_cycle={"BTC/USDT": 18})
    v = risk.symbol_gates(st, variant)
    assert v.allowed is False, v
