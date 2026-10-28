"""리스크 게이트·손절/익절·사이징 — 순수 함수(SPEC §3).

원칙
- 상태를 바꾸지 않는다. 입력 ``BotState``/``Position``은 읽기만 한다.
- 모든 반환은 "조이기만" 한다: 손절은 가까워지기만, 익절은 멀어지지 않고 상한에 걸리기만,
  수량은 줄어들기만 한다. 잘못된(NaN/inf/음수/0) 입력은 가장 보수적인 쪽으로 해석한다.
- ``global_gates``/``symbol_gates``는 **신규 진입**에 대해서만 답한다. 청산은 항상 허용이며
  이 모듈은 청산을 막는 이유를 만들지 않는다.
- reasons/notes는 사람이 읽을 한국어 문자열이며, 프로그램이 찾기 쉽게 영문 토큰을 괄호로 덧붙인다.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tradingbot.config import HardLimits
from tradingbot.models import BotState, ExitReason, OrderRequest, Position

log = logging.getLogger("tradingbot.risk")

DrawdownLevel = Literal["normal", "caution", "block", "stop_all"]

#: 왕복 비용(수수료 편도 2회 + 슬리피지 편도 2회). 비율(0.003 = 0.3%).
ROUND_TRIP_COST_PCT: float = 2.0 * (HardLimits.FEE_PCT + HardLimits.SLIPPAGE_PCT)

#: 손절 조이기로도 순손익비를 못 맞추는 최소 손절거리. 이보다 작아지면 ``rr_impossible``.
MIN_FEASIBLE_STOP_PCT: float = 0.002

#: 낙폭 사다리 경계 비교용 허용 오차(부동소수 잡음으로 경계값이 미끄러지지 않게, 보수적으로 당긴다).
_EPS: float = 1e-12

#: 확신도 티어 (하한 포함, 상한 미포함) -> 상한(MAX_POSITION_PCT) 대비 비율.
CONFIDENCE_TIERS: tuple[tuple[float, float, float], ...] = (
    (HardLimits.MIN_CONFIDENCE, 0.75, 0.40),
    (0.75, 0.85, 0.70),
    (0.85, math.inf, 1.00),
)


class RiskVerdict(BaseModel):
    """게이트 판정. ``allowed``는 신규 진입 허용 여부, ``size_multiplier``는 0~1."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    size_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------- 도우미
def _finite(x: object) -> bool:
    """실수로 볼 수 있고 NaN/inf가 아니면 True. bool은 숫자로 치지 않는다."""
    if isinstance(x, bool):
        return False
    if not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError, OverflowError):
        return False


def _positive(x: object) -> bool:
    return _finite(x) and float(x) > 0.0


def _pct_below(entry_price: float, price: float) -> float:
    """진입가 대비 아래쪽 거리(손절). 양수면 진입가 아래."""
    return (entry_price - price) / entry_price


def _pct_above(entry_price: float, price: float) -> float:
    """진입가 대비 위쪽 거리(익절). 양수면 진입가 위."""
    return (price - entry_price) / entry_price


def _require_entry(entry_price: float) -> float:
    if not _positive(entry_price):
        raise ValueError(f"진입가가 유효하지 않음(유한한 양수여야 함): {entry_price!r}")
    return float(entry_price)


def confidence_tier_fraction(confidence: float) -> float:
    """확신도 -> 상한 대비 비율. ``MIN_CONFIDENCE`` 미만(또는 NaN)은 0."""
    if not _finite(confidence):
        return 0.0
    c = float(confidence)
    for lo, hi, frac in CONFIDENCE_TIERS:
        if lo <= c < hi:
            return frac
    return 0.0


# ---------------------------------------------------------------- 낙폭 사다리
def drawdown_level(equity: float, peak_equity: float) -> DrawdownLevel:
    """최고자본 대비 낙폭 단계. 경계값은 포함(3% -> caution, 4.5% -> block, 6% -> stop_all).

    - equity가 NaN/inf/0 이하면 신뢰할 수 없으므로 ``stop_all``.
    - peak가 NaN/inf/0 이하면 기준이 없으므로 ``normal``(갓 시작한 상태).
    """
    if not _positive(equity):
        return "stop_all"
    if not _positive(peak_equity):
        return "normal"
    dd = (float(peak_equity) - float(equity)) / float(peak_equity)
    if dd >= HardLimits.DRAWDOWN_STOP_ALL - _EPS:
        return "stop_all"
    if dd >= HardLimits.DRAWDOWN_BLOCK - _EPS:
        return "block"
    if dd >= HardLimits.DRAWDOWN_CAUTION - _EPS:
        return "caution"
    return "normal"


# ---------------------------------------------------------------- 전역 게이트
def global_gates(state: BotState, equity: float, now_ts: int, llm_budget_ok: bool) -> RiskVerdict:
    """신규 진입 전역 게이트. 막는 이유를 **전부** 모아 한국어로 돌려준다.

    검사 순서: 킬스위치 -> 낙폭 사다리 -> 당일 손실 한도 -> 연속 손실 -> 포지션 수 -> LLM 예산 -> LLM 일시정지.
    allowed=False 는 신규 진입만 막는다. 청산은 항상 허용(이 함수는 청산에 대해 답하지 않는다).
    """
    reasons: list[str] = []
    size_multiplier = 1.0

    # 1) 킬스위치
    if state.kill_switch:
        reasons.append(f"킬스위치 활성 (kill_switch): {state.kill_reason or '사유 없음'}")

    # 2) 낙폭 사다리
    if not _positive(equity):
        reasons.append(f"자본 값이 유효하지 않음 (invalid_equity): {equity!r}")
        level: DrawdownLevel = "stop_all"
    else:
        level = drawdown_level(equity, state.peak_equity)
    if level == "stop_all":
        reasons.append(
            f"낙폭 {_dd_text(equity, state.peak_equity)} ≥ {HardLimits.DRAWDOWN_STOP_ALL:.1%} "
            "(drawdown_stop_all): 킬스위치 권고, 사람이 해제해야 함"
        )
    elif level == "block":
        reasons.append(
            f"낙폭 {_dd_text(equity, state.peak_equity)} ≥ {HardLimits.DRAWDOWN_BLOCK:.1%} "
            "(drawdown_block): 신규 진입 금지"
        )
    elif level == "caution":
        size_multiplier = min(size_multiplier, 0.5)
        log.info("낙폭 주의 단계: 사이즈 50%% (equity=%.2f peak=%.2f)", equity, state.peak_equity)

    # 3) 당일 손실 한도 (실현 손익과 자본 변화 둘 다 본다; 하나라도 걸리면 막는다)
    start = state.daily.start_equity
    if not _positive(start):
        reasons.append(f"당일 시작자본이 유효하지 않음 (invalid_start_equity): {start!r}")
    else:
        limit_quote = float(start) * HardLimits.DAILY_LOSS_LIMIT_PCT
        realized = state.daily.realized_pnl
        if not _finite(realized):
            reasons.append(f"당일 실현손익이 유효하지 않음 (invalid_realized_pnl): {realized!r}")
        elif float(realized) <= -limit_quote + _EPS * float(start):
            reasons.append(
                f"당일 실현손실 {float(realized):+.2f} USDT ≤ -{HardLimits.DAILY_LOSS_LIMIT_PCT:.0%} "
                f"(-{limit_quote:.2f}) (daily_loss_limit): 당일 신규 진입 중단"
            )
        if _finite(equity):
            change = float(equity) - float(start)
            if change <= -limit_quote + _EPS * float(start):
                reasons.append(
                    f"당일 자본 변화 {change:+.2f} USDT ≤ -{HardLimits.DAILY_LOSS_LIMIT_PCT:.0%} "
                    f"(-{limit_quote:.2f}) (daily_loss_limit_equity): 당일 신규 진입 중단"
                )

    # 4) 연속 손실
    if state.daily.consecutive_losses >= HardLimits.MAX_CONSECUTIVE_LOSSES:
        reasons.append(
            f"연속 손실 {state.daily.consecutive_losses}회 ≥ {HardLimits.MAX_CONSECUTIVE_LOSSES} "
            "(consecutive_losses): 당일 신규 진입 중단"
        )

    # 5) 포지션 수
    if len(state.positions) >= HardLimits.MAX_POSITIONS:
        reasons.append(
            f"보유 포지션 {len(state.positions)}개 ≥ {HardLimits.MAX_POSITIONS} (max_positions): 신규 진입 불가"
        )

    # 6) LLM 예산
    if not llm_budget_ok:
        reasons.append("LLM 예산 소진 (llm_budget): 신규 진입 판단 불가")

    # 7) LLM 일시정지 (예산/인증 오류 등으로 호출이 멈춘 동안은 새 판단이 없다)
    paused_until = state.llm_paused_until_ts
    if paused_until is not None and _finite(now_ts) and int(now_ts) < int(paused_until):
        reasons.append(
            f"LLM 일시정지 중 (llm_paused) until={int(paused_until)}: {state.llm_pause_reason or '사유 없음'}"
        )

    allowed = not reasons
    if not allowed:
        size_multiplier = 0.0
        log.info("전역 게이트 차단: %s", "; ".join(reasons))
    return RiskVerdict(allowed=allowed, reasons=reasons, size_multiplier=size_multiplier)


def _dd_text(equity: float, peak: float) -> str:
    if _positive(equity) and _positive(peak):
        return f"{(float(peak) - float(equity)) / float(peak):.2%}"
    return "(계산 불가)"


# ---------------------------------------------------------------- 종목 게이트
def symbol_gates(state: BotState, symbol: str, whitelist: Iterable[str] | None = None) -> RiskVerdict:
    """종목별 신규 진입 게이트.

    - 보유 중 -> "already_held"
    - 쿨다운(``cooldown_until_cycle``)·복수매매 창(``loss_cooldown_until_cycle``) 안이면 막는다.
      ``state.cycle <= until`` 인 동안 막는다(경계 포함, 보수적).
    - ``whitelist``가 주어지면 그 밖의 종목은 막는다. None이면 화이트리스트 검사는 생략한다.
    """
    reasons: list[str] = []
    if not isinstance(symbol, str) or not symbol.strip():
        reasons.append(f"종목 기호가 비어 있음 (invalid_symbol): {symbol!r}")
        return RiskVerdict(allowed=False, reasons=reasons, size_multiplier=0.0)

    if whitelist is not None:
        wl = tuple(whitelist)
        if symbol not in wl:
            reasons.append(f"{symbol}: 화이트리스트 밖 (not_whitelisted)")

    if symbol in state.positions:
        reasons.append(f"{symbol}: 이미 보유 중 (already_held)")

    until = state.cooldown_until_cycle.get(symbol)
    if until is not None and state.cycle <= until:
        reasons.append(f"{symbol}: 청산 후 쿨다운 (cooldown) until_cycle={until} 현재={state.cycle}")

    loss_until = state.loss_cooldown_until_cycle.get(symbol)
    if loss_until is not None and state.cycle <= loss_until:
        reasons.append(
            f"{symbol}: 손실 청산 후 재진입 금지 창 (revenge_window) until_cycle={loss_until} 현재={state.cycle}"
        )

    allowed = not reasons
    return RiskVerdict(allowed=allowed, reasons=reasons, size_multiplier=1.0 if allowed else 0.0)


# ---------------------------------------------------------------- 손절/익절
def default_stops(entry_price: float, atr_1h: float | None, atr_mult: float = 2.0) -> tuple[float, float]:
    """ATR 기반 기본 손절/익절.

    손절거리 = min(atr_mult*atr_1h/entry, MAX_STOP_LOSS_PCT); atr 없음/무효면 MAX_STOP_LOSS_PCT*0.5.
    익절거리 = min(2*손절거리, MAX_TAKE_PROFIT_PCT). 반환 (stop_loss_price, take_profit_price).
    entry_price가 유한한 양수가 아니면 ValueError.
    """
    entry = _require_entry(entry_price)
    if atr_1h is None or not _positive(atr_1h) or not _positive(atr_mult):
        sl_pct = HardLimits.MAX_STOP_LOSS_PCT * 0.5
    else:
        sl_pct = min(float(atr_mult) * float(atr_1h) / entry, HardLimits.MAX_STOP_LOSS_PCT)
    tp_pct = min(2.0 * sl_pct, HardLimits.MAX_TAKE_PROFIT_PCT)
    sl = entry * (1.0 - sl_pct)
    tp = entry * (1.0 + tp_pct)
    return clamp_stops(entry, sl, tp)


def clamp_stops(entry_price: float, stop_loss_price: float, take_profit_price: float) -> tuple[float, float]:
    """손절은 진입가 아래·거리 ≤ MAX_STOP_LOSS_PCT, 익절은 진입가 위·거리 ≤ MAX_TAKE_PROFIT_PCT 로 조인다.

    - 손절이 NaN/inf/0 이하/진입가 이상이면 기본값(MAX_STOP_LOSS_PCT*0.5 거리)으로 대체한다.
    - 익절이 NaN/inf/진입가 이하이면 min(2*손절거리, MAX_TAKE_PROFIT_PCT)로 대체한다.
    - entry_price가 유한한 양수가 아니면 ValueError.
    """
    entry = _require_entry(entry_price)
    max_sl = HardLimits.MAX_STOP_LOSS_PCT
    max_tp = HardLimits.MAX_TAKE_PROFIT_PCT

    if _positive(stop_loss_price) and float(stop_loss_price) < entry:
        sl_pct = _pct_below(entry, float(stop_loss_price))
        if sl_pct > max_sl:
            sl_pct = max_sl
    else:
        log.debug("손절가 무효(%r) -> 기본 거리 %.4f 로 대체", stop_loss_price, max_sl * 0.5)
        sl_pct = max_sl * 0.5

    if _positive(take_profit_price) and float(take_profit_price) > entry:
        tp_pct = _pct_above(entry, float(take_profit_price))
        if tp_pct > max_tp:
            tp_pct = max_tp
    else:
        tp_pct = min(2.0 * sl_pct, max_tp)
        log.debug("익절가 무효(%r) -> 기본 거리 %.4f 로 대체", take_profit_price, tp_pct)

    # 거리는 항상 (0, 상한] 안에 있어야 한다.
    sl_pct = min(max(sl_pct, 0.0), max_sl)
    tp_pct = min(max(tp_pct, 0.0), max_tp)
    if sl_pct <= 0.0:
        sl_pct = max_sl * 0.5
    if tp_pct <= 0.0:
        tp_pct = min(2.0 * sl_pct, max_tp)

    sl = entry * (1.0 - sl_pct)
    tp = entry * (1.0 + tp_pct)
    # 부동소수 잡음으로 상한을 넘지 않게 마지막으로 한 번 더 조인다.
    sl = max(sl, entry * (1.0 - max_sl))
    tp = min(tp, entry * (1.0 + max_tp))
    return sl, tp


def net_rr(entry_price: float, stop_loss_price: float, take_profit_price: float) -> float:
    """비용 차감 순손익비. 무효 입력은 -inf."""
    if not _positive(entry_price) or not _positive(stop_loss_price) or not _positive(take_profit_price):
        return -math.inf
    entry = float(entry_price)
    sl_pct = _pct_below(entry, float(stop_loss_price))
    tp_pct = _pct_above(entry, float(take_profit_price))
    if sl_pct <= 0.0 or tp_pct <= 0.0:
        return -math.inf
    return (tp_pct - ROUND_TRIP_COST_PCT) / (sl_pct + ROUND_TRIP_COST_PCT)


def enforce_net_rr(
    entry_price: float, stop_loss_price: float, take_profit_price: float
) -> tuple[float, float, str]:
    """순손익비 ≥ MIN_NET_RR 가 되도록 **손절만** 조인다(익절은 그대로).

    순손익비 = (tp거리 - 왕복비용) / (sl거리 + 왕복비용), 왕복비용 = 2*(FEE_PCT+SLIPPAGE_PCT).
    조여서도 불가능(필요 손절거리 ≤ 0.002)하거나 손절/익절이 무효면 입력을 그대로 돌려주고
    note에 "rr_impossible"을 담는다 -> 호출자는 HOLD. entry_price 무효는 ValueError.
    반환 (stop_loss_price, take_profit_price, note).
    """
    entry = _require_entry(entry_price)
    cost = ROUND_TRIP_COST_PCT
    min_rr = HardLimits.MIN_NET_RR

    if not (_positive(stop_loss_price) and float(stop_loss_price) < entry):
        return stop_loss_price, take_profit_price, f"rr_impossible: 손절가 무효 {stop_loss_price!r}"
    if not (_positive(take_profit_price) and float(take_profit_price) > entry):
        return stop_loss_price, take_profit_price, f"rr_impossible: 익절가 무효 {take_profit_price!r}"

    sl = float(stop_loss_price)
    tp = float(take_profit_price)
    sl_pct = _pct_below(entry, sl)
    tp_pct = _pct_above(entry, tp)
    rr = (tp_pct - cost) / (sl_pct + cost)
    if rr >= min_rr:
        return sl, tp, f"net_rr={rr:.2f} ≥ {min_rr}"

    # 손절만 조인다: (tp_pct - cost) / (needed + cost) = min_rr  ->  needed = (tp_pct - cost)/min_rr - cost
    needed = (tp_pct - cost) / min_rr - cost
    if needed <= MIN_FEASIBLE_STOP_PCT:
        return (
            sl,
            tp,
            f"rr_impossible: 순손익비 {rr:.2f} < {min_rr}, 필요 손절거리 {needed:.4%} ≤ "
            f"{MIN_FEASIBLE_STOP_PCT:.2%} (익절거리 {tp_pct:.2%})",
        )
    # 부동소수 잡음에 대비해 아주 조금 더 조인다(조이기만 한다).
    new_sl_pct = min(sl_pct, needed * (1.0 - 1e-9))
    new_sl = entry * (1.0 - new_sl_pct)
    new_rr = (tp_pct - cost) / (new_sl_pct + cost)
    note = (
        f"stop_tightened: 손절거리 {sl_pct:.4%} -> {new_sl_pct:.4%}, "
        f"순손익비 {rr:.2f} -> {new_rr:.2f} (익절 유지 {tp_pct:.2%})"
    )
    log.debug(note)
    return new_sl, tp, note


# ---------------------------------------------------------------- 사이징
def size_position(
    equity: float,
    entry_price: float,
    stop_loss_price: float,
    confidence: float,
    size_multiplier: float,
    free_quote: float,
) -> tuple[float, list[str]]:
    """진입 금액(USDT) 산출. 줄이기만 한다.

    1) 위험 기반: quote = equity*RISK_PER_TRADE_PCT / (sl거리 + 왕복비용)
    2) 상한: equity*MAX_POSITION_PCT
    3) 확신도 티어(상한 대비 비율): <0.65 -> 0 ; 0.65~0.75 -> 40% ; 0.75~0.85 -> 70% ; ≥0.85 -> 100%
       -> quote = min(위험 기반, 상한*티어비율)
    4) size_multiplier(0~1로 클램프) 곱, free_quote 이하, MIN_ORDER_USDT 미만이면 0
    무효 입력(NaN/inf/0 이하 등)은 전부 0. 반환 (quote_amount, notes). 소수 2자리로 내림.
    """
    notes: list[str] = []

    if not _positive(equity):
        notes.append(f"자본이 유효하지 않음 (invalid_equity): {equity!r} -> 0")
        return 0.0, notes
    if not _positive(entry_price):
        notes.append(f"진입가가 유효하지 않음 (invalid_entry): {entry_price!r} -> 0")
        return 0.0, notes
    if not (_positive(stop_loss_price) and float(stop_loss_price) < float(entry_price)):
        notes.append(f"손절가가 유효하지 않음 (invalid_stop): {stop_loss_price!r} -> 0")
        return 0.0, notes
    if not _finite(confidence):
        notes.append(f"확신도가 유효하지 않음 (invalid_confidence): {confidence!r} -> 0")
        return 0.0, notes
    if not _finite(free_quote) or float(free_quote) <= 0.0:
        notes.append(f"가용 잔고 없음 (no_free_quote): {free_quote!r} -> 0")
        return 0.0, notes

    eq = float(equity)
    entry = float(entry_price)
    sl_pct = _pct_below(entry, float(stop_loss_price))
    # 손절거리가 상한을 넘으면(호출자가 clamp를 빼먹은 경우) 위험 계산에는 그대로(더 작은 수량) 쓰고 경고만 남긴다.
    if sl_pct > HardLimits.MAX_STOP_LOSS_PCT:
        notes.append(f"손절거리 {sl_pct:.2%} > 상한 {HardLimits.MAX_STOP_LOSS_PCT:.0%} (stop_over_limit): 위험 계산에 그대로 반영")

    # 1) 위험 기반
    risk_quote = eq * HardLimits.RISK_PER_TRADE_PCT / (sl_pct + ROUND_TRIP_COST_PCT)
    notes.append(f"위험 기반 {risk_quote:.2f} USDT (자본 {eq:.2f} × {HardLimits.RISK_PER_TRADE_PCT:.2%} / (sl {sl_pct:.3%} + 비용 {ROUND_TRIP_COST_PCT:.2%}))")

    # 2) 상한
    cap = eq * HardLimits.MAX_POSITION_PCT
    notes.append(f"종목 상한 {cap:.2f} USDT ({HardLimits.MAX_POSITION_PCT:.0%})")

    # 3) 확신도 티어
    frac = confidence_tier_fraction(float(confidence))
    if frac <= 0.0:
        notes.append(f"확신도 {float(confidence):.2f} < {HardLimits.MIN_CONFIDENCE} (low_confidence) -> 0")
        return 0.0, notes
    tier_cap = cap * frac
    quote = min(risk_quote, cap, tier_cap)
    notes.append(f"확신도 {float(confidence):.2f} 티어 {frac:.0%} -> {quote:.2f} USDT")

    # 4) 주의 배수, 가용 잔고, 최소 주문
    if not _finite(size_multiplier):
        mult = 0.0
    else:
        mult = min(max(float(size_multiplier), 0.0), 1.0)
    if mult < 1.0:
        quote *= mult
        notes.append(f"사이즈 배수 {mult:.2f} -> {quote:.2f} USDT")
    if quote > float(free_quote):
        quote = float(free_quote)
        notes.append(f"가용 잔고 한도 -> {quote:.2f} USDT")

    quote = math.floor(quote * 100.0) / 100.0
    quote = min(quote, cap)  # 마지막 안전장치: 어떤 경우에도 상한을 넘지 않는다
    if quote < HardLimits.MIN_ORDER_USDT:
        notes.append(f"{quote:.2f} USDT < 최소 주문 {HardLimits.MIN_ORDER_USDT:.2f} (below_min_order) -> 0")
        return 0.0, notes
    notes.append(f"최종 {quote:.2f} USDT")
    return quote, notes


# ---------------------------------------------------------------- 주문 검증
def validate_order_request(req: OrderRequest, whitelist: tuple[str, ...], held: Iterable[str]) -> None:
    """주문 요청 검증. 위반 시 ValueError.

    - buy: 화이트리스트 안의 종목만, quote_amount > 0 유한, base_amount는 None.
    - sell: 보유(held) 또는 화이트리스트 종목, base_amount > 0 유한, quote_amount는 None.
    """
    if not isinstance(req, OrderRequest):
        raise ValueError(f"OrderRequest가 아님: {type(req).__name__}")
    if not isinstance(req.symbol, str) or not req.symbol.strip():
        raise ValueError(f"종목 기호가 비어 있음: {req.symbol!r}")
    wl = tuple(whitelist)
    held_set = set(held)

    if req.side == "buy":
        if req.side not in HardLimits.ENTRY_SIDES:
            raise ValueError(f"허용되지 않는 진입 방향: {req.side!r}")
        if req.symbol not in wl:
            raise ValueError(f"buy: 화이트리스트 밖의 종목: {req.symbol!r}")
        if req.base_amount is not None:
            raise ValueError("buy: base_amount는 지정할 수 없음 (quote_amount만 사용)")
        if not _positive(req.quote_amount):
            raise ValueError(f"buy: quote_amount는 유한한 양수여야 함: {req.quote_amount!r}")
        return

    if req.side == "sell":
        if req.symbol not in held_set and req.symbol not in wl:
            raise ValueError(f"sell: 보유/화이트리스트 밖의 종목: {req.symbol!r}")
        if req.quote_amount is not None:
            raise ValueError("sell: quote_amount는 지정할 수 없음 (base_amount만 사용)")
        if not _positive(req.base_amount):
            raise ValueError(f"sell: base_amount는 유한한 양수여야 함: {req.base_amount!r}")
        return

    raise ValueError(f"알 수 없는 주문 방향: {req.side!r}")


# ---------------------------------------------------------------- 청산 분류
def check_exits(position: Position, mark_price: float, current_cycle: int) -> ExitReason | None:
    """마크 가격·보유 사이클로 청산 사유를 분류한다.

    mark ≤ 손절 -> "stop_loss"; mark ≥ 익절 -> "take_profit";
    current_cycle - opened_cycle ≥ TIME_STOP_CYCLES -> "time_stop"; 아니면 None.
    마크가 NaN/inf/0 이하면 가격 조건은 판단하지 않고(시간손절만 본다) 경고를 남긴다.
    """
    if _positive(mark_price):
        mark = float(mark_price)
        if _finite(position.stop_loss_price) and mark <= float(position.stop_loss_price):
            return "stop_loss"
        if _finite(position.take_profit_price) and mark >= float(position.take_profit_price):
            return "take_profit"
    else:
        log.warning("%s: 마크 가격이 유효하지 않음(%r) -> 가격 기반 청산 판단 건너뜀", position.symbol, mark_price)

    if _finite(current_cycle) and int(current_cycle) - int(position.opened_cycle) >= HardLimits.TIME_STOP_CYCLES:
        return "time_stop"
    return None


__all__ = [
    "CONFIDENCE_TIERS",
    "MIN_FEASIBLE_STOP_PCT",
    "ROUND_TRIP_COST_PCT",
    "DrawdownLevel",
    "RiskVerdict",
    "check_exits",
    "clamp_stops",
    "confidence_tier_fraction",
    "default_stops",
    "drawdown_level",
    "enforce_net_rr",
    "global_gates",
    "net_rr",
    "size_position",
    "symbol_gates",
    "validate_order_request",
]
