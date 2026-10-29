"""시스템 리뷰 — 돈 안전·하드 리밋 관점 (sysrev: money).

각 테스트는 현재 코드에서 실패해야 하는 결함 재현이다(고치기 전까지 빨간불). 전부 오프라인.
구현자 테스트(test_cycle.py)의 Rig·가짜 부품을 재사용한다.
"""
from __future__ import annotations

import math

import pytest

from test_cycle import Rig, happy, risk_with  # noqa: F401 - 픽스처 재사용
from tradingbot.agents import pipeline as PL
from tradingbot.agents.schemas import RedLine, Thesis
from tradingbot.exchange import ExchangeError, OrderValidationError
from tradingbot.models import Fill

TESTNET_ENV = {"MODE": "testnet", "BINANCE_TESTNET_API_KEY": "tk-0123456789abcdef",
               "BINANCE_TESTNET_API_SECRET": "ts-0123456789abcdef"}


# ======================================================================= 가짜 거래소 브로커(testnet/live 흉내)
class FakeExchangeBroker:
    """거래소 잔고를 들고 있는 브로커 대역. ``Cycle`` 은 paper 가 아니면 잔고를 덮어쓰지 않는다.

    - ``step``: base 수량 정밀도(내림). 0 이면 8자리.
    - ``raise_after_buy`` / ``raise_after_sell``: 체결(잔고 변경) **후** 예외 — 응답 파싱/fetch_order 실패 흉내.
    - ``min_sell_notional``: 거래소 MIN_NOTIONAL(매도 금액 하한) 흉내.
    """

    mode = "testnet"

    def __init__(self, market, usdt: float = 10_000.0, *, step: float = 0.0, raise_after_buy: int = 0,
                 raise_after_sell: int = 0, min_sell_notional: float = 0.0) -> None:
        self.market = market
        self.bal: dict[str, float] = {"USDT": float(usdt)}
        self.step = step
        self.raise_after_buy = raise_after_buy
        self.raise_after_sell = raise_after_sell
        self.min_sell_notional = min_sell_notional
        self.buys: list[tuple[str, float]] = []
        self.sells: list[tuple[str, float]] = []
        self._seq = 0

    def _base(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def free_quote(self) -> float:
        return self.bal.get("USDT", 0.0)

    def free_base(self, symbol: str) -> float:
        return self.bal.get(self._base(symbol), 0.0)

    def min_order_usdt(self, symbol: str) -> float:
        return 10.0

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        if not (isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0):
            raise OrderValidationError(f"{symbol}: 수량이 유한한 양수가 아님: {amount!r}")
        if self.step > 0:
            return math.floor(amount / self.step + 1e-9) * self.step
        return math.floor(amount * 1e8) / 1e8

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        px = self.market.prices[symbol] * 1.0001
        base = self.amount_to_precision(symbol, quote_amount / px)
        self.bal["USDT"] -= quote_amount
        self.bal[self._base(symbol)] = self.free_base(symbol) + base
        self.buys.append((symbol, quote_amount))
        if self.raise_after_buy > 0:
            self.raise_after_buy -= 1
            raise ExchangeError(f"{symbol} buy: fetch_order 실패(체결 확인 불가)")
        self._seq += 1
        return Fill(order_id=f"x-{self._seq}", symbol=symbol, side="buy", base_amount=base, price=px,
                    quote_amount=quote_amount, fee_quote=0.0, fee_base=0.0, ts=0)

    def market_sell(self, symbol: str, base_amount: float) -> Fill:
        amt = self.amount_to_precision(symbol, base_amount)
        if amt <= 0:
            raise OrderValidationError(f"{symbol} sell: 정밀도 적용 후 수량이 0")
        if amt > self.free_base(symbol) + 1e-12:
            raise OrderValidationError(f"{symbol} sell: 필요 {amt} > 가용 {self.free_base(symbol)}")
        px = self.market.prices[symbol] * 0.9999
        if amt * px < self.min_sell_notional:
            raise OrderValidationError(f"{symbol} sell: 예상 금액 {amt * px:.2f} < 거래소 최소 {self.min_sell_notional}")
        self.bal[self._base(symbol)] = self.free_base(symbol) - amt
        self.bal["USDT"] += amt * px
        self.sells.append((symbol, amt))
        if self.raise_after_sell > 0:
            self.raise_after_sell -= 1
            raise ExchangeError(f"{symbol} sell: fetch_order 실패(체결 확인 불가)")
        self._seq += 1
        return Fill(order_id=f"x-{self._seq}", symbol=symbol, side="sell", base_amount=amt, price=px,
                    quote_amount=amt * px, fee_quote=0.0, fee_base=0.0, ts=0)


def _testnet_rig(tmp_path, responses=None, *, candidates=("ETH/USDT",), **broker_kw) -> tuple[Rig, FakeExchangeBroker]:
    rig = Rig(tmp_path, responses if responses is not None else happy(), candidates=candidates,
              env_extra=TESTNET_ENV)
    assert rig.settings.mode == "testnet"
    fb = FakeExchangeBroker(rig.market, **broker_kw)
    rig.broker = fb
    rig.cycle.broker = fb
    return rig, fb


def _hold_live(rig: Rig, fb: FakeExchangeBroker, sym="ETH/USDT", entry=100.0, base=5.0, sl=None, tp=None):
    """testnet: 장부 포지션 + 거래소 base 잔고(같은 수량). USDT 는 이미 썼다고 본다."""
    pos = rig.hold_position(sym=sym, entry=entry, base=base, sl=sl, tp=tp)
    fb.bal[sym.split("/")[0]] = fb.bal.get(sym.split("/")[0], 0.0) + base
    fb.bal["USDT"] -= entry * base
    return pos


# ======================================================================= F1: 체결 후 브로커 예외 → 추적 안 되는 보유(손절 없음)
def test_buy_filled_then_broker_raises_leaves_untracked_unprotected_holding(tmp_path):
    """BUY 가 거래소에서 체결된 뒤 브로커가 예외(fetch_order/파싱 실패)를 내면 사이클은 order_rejected 로
    끝내고 포지션을 기록하지 않는다. 거래소에는 ETH 가 남지만 장부에는 없다 → 손절/익절/시간손절 없음,
    자본(free_quote + 장부 포지션)은 그만큼 줄어 가짜 낙폭이 생기고, 같은 종목에 다시 BUY 할 수 있다.
    대사(reconciliation)가 없다 — 사이클 2 이후에도 거래소 보유분이 장부 포지션으로 덮이지 않는다."""
    rig, fb = _testnet_rig(tmp_path, raise_after_buy=1)
    s1 = rig.cycle.run_once()
    assert fb.buys, "사이클 1에서 BUY 주문이 나가야 재현이 성립"
    assert any("buy:ETH/USDT" in e for e in s1.errors)
    rig.clock.advance(15 * 60)
    s2 = rig.cycle.run_once()
    tracked = sum(p.base_amount for s, p in rig.state.positions.items() if s == "ETH/USDT")
    on_exchange = fb.free_base("ETH/USDT")
    # 거래소에 있는 ETH 는 장부 포지션(손절 포함)으로 추적돼야 하고, 자본이 그만큼 사라지면 안 된다.
    assert tracked >= on_exchange * 0.99, (
        f"거래소 ETH {on_exchange:.6f} 중 장부 추적 {tracked:.6f} — 손절 없는 고아 보유 (buys={fb.buys})")
    assert s2.equity is not None and s2.equity >= 9_900.0, f"고아 보유가 자본에서 빠짐: equity={s2.equity}"


# ======================================================================= F2: 매도 체결 후 브로커 예외 → 유령 포지션(이중 계상)
def test_sell_filled_then_broker_raises_leaves_phantom_position_double_counted(tmp_path):
    """손절 매도가 거래소에서 체결된 뒤 브로커가 예외를 내면 장부 포지션이 그대로 남는다. 거래소 base 는 0 이라
    다음 사이클부터 매도는 영원히 거부되고(가용 0), testnet/live 자본 = free_quote(매도 대금 포함) + 유령 포지션
    평가액 으로 이중 계상된다 → 최고자본이 부풀고 낙폭 사다리·당일 손실 한도가 가려지며 슬롯 하나를 영구 점유."""
    rig, fb = _testnet_rig(tmp_path, candidates=(), raise_after_sell=1)
    _hold_live(rig, fb, entry=100.0, base=5.0, sl=99.0, tp=120.0)
    rig.market.prices["ETH/USDT"] = 98.0          # 손절 아래
    rig.cycle.run_once()
    assert fb.sells, "사이클 1에서 손절 매도가 나가야 재현이 성립"
    rig.clock.advance(15 * 60)
    s2 = rig.cycle.run_once()
    true_equity = fb.free_quote() + fb.free_base("ETH/USDT") * rig.market.prices["ETH/USDT"]
    assert "ETH/USDT" not in rig.state.positions, "거래소에서 이미 팔린 포지션이 장부에 남음(유령 포지션)"
    assert s2.equity == pytest.approx(true_equity, rel=1e-3), (
        f"자본 이중 계상: 보고 {s2.equity:.2f} vs 실제 {true_equity:.2f}")


# ======================================================================= F3: 정밀도 잔량(dust) 포지션은 영원히 청산 불가
def test_partial_exit_dust_residual_can_never_be_closed_and_holds_a_slot(tmp_path):
    """수량 정밀도(step) 때문에 매도 체결이 보유의 99.5% 미만이면 close_position 은 '부분 청산'으로 잔량을
    포지션으로 남긴다. 잔량이 step 미만이면 다음 사이클부터 amount_to_precision → 0 → 매도 거부가 매 사이클
    반복되고(order_rejected 스팸), 포지션은 MAX_POSITIONS 슬롯 하나와 보유 상태를 영구 점유한다.
    (예: step 1 인 알트 165.834 개 → 165 매도, 0.834 잔량.)"""
    rig, fb = _testnet_rig(tmp_path, candidates=(), step=1.0)
    _hold_live(rig, fb, entry=100.0, base=10.7, sl=99.0, tp=120.0)
    rig.market.prices["ETH/USDT"] = 98.0          # 손절 아래
    rig.cycle.run_once()
    for _ in range(3):
        rig.clock.advance(15 * 60)
        rig.cycle.run_once()
    rejected = rig.sent_with("주문 거부")
    assert "ETH/USDT" not in rig.state.positions, (
        f"팔 수 없는 잔량 포지션이 남아 슬롯 점유: {rig.state.positions.get('ETH/USDT')!r}; 거부 알림 {len(rejected)}건")


# ======================================================================= F4: 코드 레드라인 적중인데 trim 이면 50%만 매도
def test_code_red_line_hit_with_risk_trim_still_sells_only_half(tmp_path, monkeypatch):
    """레드라인 적중은 코드가 평가한 논지 반증(DESIGN §6: 적중 → 청산 결정, risk_manager 규칙: 적중이면 exit).
    그런데 cycle 은 risk_manager(LLM)의 action 만 보고 trim 이면 head SELL 을 50%로 줄인다 — LLM 라벨이
    코드 사실(red_line_hits)을 이기고 청산을 '풀어' 버린다(조이기만 원칙 위반)."""
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="trim", pos_health=5)), candidates=())
    rig.hold_position(base=4.0, sl=90.0, tp=120.0)
    rig.cycle.journal_state.thesis["ETH/USDT"] = Thesis(
        reasons=["추세"], assumptions=["BTC 안정", "거래량 유지"],
        red_lines=[RedLine(metric="btc_return_1h_pct", op="<", value=-2.0)], horizon_bars=16)
    monkeypatch.setattr(PL, "evaluate_red_lines", lambda *a, **k: ["btc_return_1h_pct < -2.0"])
    s = rig.cycle.run_once()
    assert "head" in rig.roles() and s.exits, "레드라인 적중 → 플래그 → head SELL 이 실행돼야 재현 성립"
    assert "ETH/USDT" not in rig.state.positions, (
        f"레드라인 적중 SELL 이 trim(50%)로 축소됨: 잔여 {rig.state.positions['ETH/USDT'].base_amount}")


# ======================================================================= F5: trim 이 최소 주문 금액 미만이면 아무것도 안 팔림
def test_head_sell_trim_below_exchange_min_notional_sells_nothing(tmp_path):
    """head SELL + risk trim → 50% 매도. 절반 금액이 거래소 최소 매도 금액 미만이면 주문 거부로 끝나고
    전량 매도로 대체하지 않는다 — 총괄이 청산하기로 한 포지션이 그대로 남는다(매 사이클 같은 거부 반복)."""
    rig, fb = _testnet_rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="trim", pos_health=5)),
                           candidates=(), min_sell_notional=10.0)
    _hold_live(rig, fb, entry=100.0, base=0.15, sl=90.0, tp=120.0)      # 15 USDT 포지션, 절반 7.5 < 10
    s = rig.cycle.run_once()
    assert "head" in rig.roles(), "trim 액션 → 플래그 → head SELL 이 나와야 재현 성립"
    pos = rig.state.positions.get("ETH/USDT")
    assert pos is None or pos.base_amount < 0.15 - 1e-9, (
        f"head SELL 이 집행되지 않음(잔여 {pos.base_amount if pos else 0}); errors={s.errors}")


# ======================================================================= F6: testnet/live 리스크 팩 주문액이 실제 자본이 아니라 paper 시작잔고 기준
def test_testnet_scan_planned_notional_ignores_exchange_equity(tmp_path):
    """cycle 은 ``scanner.scan`` 에 ``planned_notional`` 을 넘기지 않는다. 그러면 Scanner 는
    ``_equity_estimate`` = ``state.paper_quote_balance``(testnet/live 는 0) + 보유 평가액, 0 이하면
    ``settings.paper_start_balance``(10,000) 로 주문액을 추정한다 → 거래소 자본이 50,000 USDT 여도 리스크 팩의
    충격(impact_bps_est)·exec_cost_pct·주문액/15분 거래대금 비율이 1,000 USDT 기준으로 5배 과소평가되어
    risk_manager(LLM)와 G3 의 fallback 경로에 들어간다."""
    from tradingbot.scanner import Scanner

    rig, fb = _testnet_rig(tmp_path, usdt=50_000.0)
    real = Scanner(rig.settings, rig.market)
    seen: list[float | None] = []
    effective_at_scan: list[float] = []
    orig = rig.scanner.scan

    def spy(state, now_ms, *, headline_polarity=None, planned_notional=None):
        seen.append(planned_notional)
        # 실제 Scanner 가 이 호출에서 쓸 계획 주문액(인자가 없으면 _equity_estimate × 10%)
        effective_at_scan.append(planned_notional if planned_notional is not None
                                 else real._equity_estimate(state, {}) * 0.10)
        return orig(state, now_ms, headline_polarity=headline_polarity, planned_notional=planned_notional)

    rig.scanner.scan = spy
    rig.cycle.run_once()
    effective = effective_at_scan[0]
    assert effective == pytest.approx(5_000.0, rel=0.05), (
        f"리스크 팩 계획 주문액 {effective:.2f} USDT ≠ 실제 자본 50,000 × 10% (planned_notional 전달={seen})")
