"""portfolio.py 적대적 리뷰 테스트 (SPEC §10).

각 테스트는 현재 구현에서 **실패**해야 하고, 결함이 고쳐지면 통과한다.
구현 파일(tradingbot/portfolio.py, tests/test_portfolio.py)은 건드리지 않는다.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingbot.config import load_settings
from tradingbot.models import Fill
from tradingbot.portfolio import Portfolio

UTC = timezone.utc


# ---------------------------------------------------------------- 도우미
def make_settings(mode: str = "paper"):
    env = {"AGENT_BACKEND": "mock", "MODE": mode, "PAPER_START_BALANCE_USDT": "10000"}
    if mode == "testnet":
        env.update(BINANCE_TESTNET_API_KEY="tkey-abcdef", BINANCE_TESTNET_API_SECRET="tsec-abcdef")
    return load_settings(env)


def buy(symbol: str = "BTC/USDT", base: float = 1.0, price: float = 100.0, fee: float = 0.1, ts: int = 1, **kw) -> Fill:
    return Fill(order_id=f"b-{symbol}", symbol=symbol, side="buy", base_amount=base, price=price,
                quote_amount=kw.pop("quote_amount", base * price), fee_quote=fee, ts=ts, **kw)


def sell(symbol: str = "BTC/USDT", base: float = 1.0, price: float = 90.0, fee: float = 0.1, ts: int = 2, **kw) -> Fill:
    return Fill(order_id=f"s-{symbol}", symbol=symbol, side="sell", base_amount=base, price=price,
                quote_amount=kw.pop("quote_amount", base * price), fee_quote=fee, ts=ts, **kw)


@pytest.fixture
def paper(tmp_path: Path) -> Portfolio:
    pf = Portfolio(make_settings("paper"), tmp_path / "state.json")
    pf.sleep = lambda s: None
    return pf


@pytest.fixture
def testnet(tmp_path: Path) -> Portfolio:
    pf = Portfolio(make_settings("testnet"), tmp_path / "state.json")
    pf.sleep = lambda s: None
    return pf


# ---------------------------------------------------------------- F1: base 수수료 이중 차감
class TestBaseFeeDoubleCount:
    """SPEC §4 Fill 규약: 수수료가 base로 나가면 ``base_amount``는 **순수량**(filled - fee_base),
    ``fee_quote``는 그 base 수수료를 체결가로 환산한 값이다(Binance 현물 BUY의 기본 수수료 방식).

    순수량이 줄어든 것 자체가 이미 수수료를 반영하므로, 손익에서 환산 수수료를 또 빼면 이중 차감이다.
    실현 손익 = (매도 금액 - 매도 현금수수료) - (매수 금액 + 매수 현금수수료) 여야 한다.
    """

    def test_full_close_pnl_equals_cash_delta_when_entry_fee_in_base(self, testnet: Portfolio):
        # 1.0 BTC 체결, 수수료 0.001 BTC -> 순수량 0.999, 현금 지출 50,000, fee_quote = 0.001*50,000 = 50(환산)
        entry = Fill(order_id="b", symbol="BTC/USDT", side="buy", base_amount=0.999, price=50_000.0,
                     quote_amount=50_000.0, fee_quote=50.0, fee_base=0.001, ts=1)
        testnet.open_position(entry, 49_000.0, 52_000.0, "t", 0.8, cycle=1)
        # 순수량 0.999를 같은 가격에 매도, 매도 수수료는 USDT 49.95 (현금)
        exit_ = Fill(order_id="s", symbol="BTC/USDT", side="sell", base_amount=0.999, price=50_000.0,
                     quote_amount=49_950.0, fee_quote=49.95, fee_base=0.0, ts=2)
        trade = testnet.close_position("BTC/USDT", exit_, "manual", cycle=2)
        cash_delta = (49_950.0 - 49.95) - 50_000.0  # = -99.95 (실제 USDT 변화)
        assert trade.pnl_quote == pytest.approx(cash_delta, abs=1e-6)
        assert testnet.state.daily.realized_pnl == pytest.approx(cash_delta, abs=1e-6)

    def test_partial_close_pnl_equals_cash_delta_when_entry_fee_in_base(self, testnet: Portfolio):
        entry = Fill(order_id="b", symbol="BTC/USDT", side="buy", base_amount=0.999, price=50_000.0,
                     quote_amount=50_000.0, fee_quote=50.0, fee_base=0.001, ts=1)
        testnet.open_position(entry, 49_000.0, 52_000.0, "t", 0.8, cycle=1)
        half = Fill(order_id="s1", symbol="BTC/USDT", side="sell", base_amount=0.4995, price=50_000.0,
                    quote_amount=24_975.0, fee_quote=24.975, ts=2)
        rest = Fill(order_id="s2", symbol="BTC/USDT", side="sell", base_amount=0.4995, price=50_000.0,
                    quote_amount=24_975.0, fee_quote=24.975, ts=3)
        t1 = testnet.close_position("BTC/USDT", half, "manual", cycle=2)
        t2 = testnet.close_position("BTC/USDT", rest, "manual", cycle=3)
        cash_delta = 2 * (24_975.0 - 24.975) - 50_000.0
        assert t1.pnl_quote + t2.pnl_quote == pytest.approx(cash_delta, abs=1e-6)


# ---------------------------------------------------------------- F2: 날짜 역행 시 당일 보호장치 초기화
class TestBackwardsDateKeepsDailyGuards:
    """당일 손실 한도(-3%)·연속 손실(3회)은 '당일' 신규 진입을 막는 안전장치다.
    시계가 자정 전후로 뒤로 튀면(NTP 보정 등) ``ensure_day``가 과거 날짜로 '롤오버'하며
    realized_pnl·consecutive_losses를 0으로 지워 신규 진입이 다시 열린다. 날짜 역행은 롤오버가 아니라
    경고 + 기존 통계 유지여야 한다.
    """

    def _three_losses(self, pf: Portfolio) -> None:
        for i in range(3):
            sym = f"S{i}/USDT"
            pf.open_position(buy(sym, base=1.0, price=100.0), 98.0, 104.0, "t", 0.8, cycle=i)
            pf.close_position(sym, sell(sym, base=1.0, price=90.0), "stop_loss", cycle=i + 1)
        assert pf.state.daily.consecutive_losses == 3
        assert pf.state.daily.realized_pnl < 0

    def test_backwards_clock_does_not_reset_consecutive_losses(self, paper: Portfolio):
        today = datetime.now(UTC)
        assert paper.ensure_day(today, 10_000.0) is None
        self._three_losses(paper)
        realized_before = paper.state.daily.realized_pnl
        paper.ensure_day(today - timedelta(days=1), 9_970.0)  # 시계가 하루 뒤로
        assert paper.state.daily.consecutive_losses == 3
        assert paper.state.daily.realized_pnl == pytest.approx(realized_before)


# ---------------------------------------------------------------- F3: 새 상태의 날짜가 호출자 시계가 아닌 벽시계
class TestFreshStateAdoptsCallerClock:
    """``Cycle``은 ``now`` 콜러블을 주입받는다(SPEC §15). 새 ``Portfolio``는 ``_utcnow()``(벽시계)로
    DailyStats 날짜를 박기 때문에, 주입된 시계와 날짜가 다르면 첫 ``ensure_day``가 거래가 전혀 없던
    '가짜 전날' 통계를 반환해 daily_summary 알림이 나가고, 과거 날짜면 역행 경고까지 찍힌다.
    새 상태는 첫 ``ensure_day``의 날짜를 채택하고 None을 돌려줘야 한다.
    """

    def test_first_ensure_day_on_fresh_state_returns_none(self, paper: Portfolio):
        injected = datetime(2024, 1, 1, 0, 10, tzinfo=UTC)
        finished = paper.ensure_day(injected, 10_000.0)
        assert finished is None
        assert paper.state.daily.date == "2024-01-01"


# ---------------------------------------------------------------- F4: 비유한 confidence 허용 -> 비표준 JSON 저장
class TestNonFiniteConfidenceRejected:
    """``open_position``은 수량·금액·손절/익절은 유한성 검사를 하지만 ``confidence``는 하지 않는다.
    NaN/inf가 들어가면 ``save()``가 RFC 8259에 없는 ``NaN``/``Infinity`` 토큰을 state.json에 쓴다
    (엄격한 파서·jq·다른 언어의 대시보드가 깨진다). 다른 입력처럼 ValueError로 거부해야 한다.
    """

    @pytest.mark.parametrize("conf", [float("nan"), float("inf"), -float("inf")])
    def test_open_position_rejects_non_finite_confidence(self, paper: Portfolio, conf: float):
        with pytest.raises(ValueError):
            paper.open_position(buy(), 98.0, 104.0, "t", conf, cycle=1)
        assert paper.state.positions == {}
        assert paper.state.paper_quote_balance == 10_000.0

    def test_saved_state_is_strict_json(self, paper: Portfolio):
        try:
            paper.open_position(buy(), 98.0, 104.0, "t", float("nan"), cycle=1)
        except ValueError:
            return  # 거부했으면 통과
        paper.save()

        def reject(const: str):
            raise ValueError(f"비표준 JSON 토큰: {const}")

        json.loads(paper.path.read_text(encoding="utf-8"), parse_constant=reject)


# ---------------------------------------------------------------- F5: 0원 매수 체결 수용
class TestZeroCostBuyRejected:
    """``quote_amount=0``·``price=0``인 buy Fill을 받아 ``entry_price=0``인 포지션을 만든다.
    그런 포지션은 자본 평가 0, ``check_exits``(mark<=sl) 즉시 손절, pnl_pct 0 등 모든 하류 계산을 망친다.
    유효한 매수 체결은 진입가(=quote/base)가 양수여야 한다.
    """

    def test_open_position_rejects_zero_cost_fill(self, paper: Portfolio):
        zero = Fill(order_id="z", symbol="BTC/USDT", side="buy", base_amount=0.01, price=0.0,
                    quote_amount=0.0, fee_quote=0.0, ts=1)
        with pytest.raises(ValueError):
            paper.open_position(zero, 49_000.0, 52_000.0, "t", 0.8, cycle=1)
        assert "BTC/USDT" not in paper.state.positions
