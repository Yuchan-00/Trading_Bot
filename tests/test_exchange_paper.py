"""PaperBroker 테스트 (SPEC §4). 전부 오프라인: 가짜 MarketData 주입."""
from __future__ import annotations

import math

import pytest

from tradingbot.config import HardLimits
from tradingbot.exchange import (
    Broker,
    ExchangeError,
    InsufficientFunds,
    MarketData,
    OrderValidationError,
    PaperBroker,
    split_symbol,
)
from tradingbot.models import Candle, Fill, FuturesContext, OrderBookSummary, Ticker

BTC = "BTC/USDT"


class FakeMarket:
    """MarketData 프로토콜을 만족하는 고정 시세."""

    def __init__(self, bid: float | None = 100.0, ask: float | None = 101.0, last: float = 100.5) -> None:
        self.bid, self.ask, self.last = bid, ask, last
        self.calls: list[str] = []
        self.raise_on_ticker: Exception | None = None

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return [Candle(ts=i * 60_000, open=1, high=2, low=0.5, close=1.5, volume=10) for i in range(limit)]

    def fetch_ticker(self, symbol: str) -> Ticker:
        self.calls.append(symbol)
        if self.raise_on_ticker is not None:
            raise self.raise_on_ticker
        return Ticker(symbol=symbol, last=self.last, bid=self.bid, ask=self.ask, ts=1_700_000_000_000)

    def fetch_orderbook_summary(self, symbol: str, depth: int = 50) -> OrderBookSummary:
        return OrderBookSummary(symbol=symbol, best_bid=self.bid or 0, best_ask=self.ask or 0, spread_pct=0.0,
                                bid_depth_usdt_1pct=0, ask_depth_usdt_1pct=0, ts=0)

    def fetch_futures_context(self, symbol: str) -> FuturesContext | None:
        return None

    def server_time_ms(self) -> int:
        return 1_700_000_000_000


@pytest.fixture
def market() -> FakeMarket:
    return FakeMarket()


@pytest.fixture
def broker(market: FakeMarket) -> PaperBroker:
    return PaperBroker(market, 10_000.0, clock_ms=lambda: 123_456)


# ---------------------------------------------------------------- 프로토콜/구성
def test_protocols_satisfied(market, broker):
    assert isinstance(market, MarketData)
    assert isinstance(broker, Broker)
    assert broker.mode == "paper"


def test_initial_balances_and_defaults(broker):
    assert broker.balances == {"USDT": 10_000.0}
    assert broker.free_quote() == 10_000.0
    assert broker.free_base(BTC) == 0.0
    assert broker.fee_pct == HardLimits.FEE_PCT
    assert broker.slippage_pct == HardLimits.SLIPPAGE_PCT
    assert broker.min_order_usdt(BTC) == HardLimits.MIN_ORDER_USDT


def test_balances_argument_overrides_start_balance(market):
    b = PaperBroker(market, 10_000.0, balances={"usdt": 500.0, "BTC": 0.25})
    assert b.balances == {"USDT": 500.0, "BTC": 0.25}
    assert b.free_quote() == 500.0
    assert b.free_base(BTC) == 0.25


def test_set_balances_replaces_and_validates(broker):
    broker.set_balances({"USDT": 42.0, "ETH": 1.5})
    assert broker.balances == {"USDT": 42.0, "ETH": 1.5}
    assert broker.free_base("ETH/USDT") == 1.5
    assert broker.free_base(BTC) == 0.0
    with pytest.raises(ValueError):
        broker.set_balances({"USDT": -1})
    with pytest.raises(ValueError):
        broker.set_balances({"USDT": float("nan")})
    with pytest.raises(ValueError):
        broker.set_balances({"USDT": "abc"})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_constructor_rejects_bad_start_balance(market, bad):
    with pytest.raises(ValueError):
        PaperBroker(market, bad)


def test_constructor_rejects_bad_fee(market):
    with pytest.raises(ValueError):
        PaperBroker(market, 100.0, fee_pct=1.5)
    with pytest.raises(ValueError):
        PaperBroker(market, 100.0, slippage_pct=-0.1)


def test_repr_has_no_surprises(broker):
    assert "PaperBroker" in repr(broker)


def test_split_symbol():
    assert split_symbol("BTC/USDT") == ("BTC", "USDT")
    with pytest.raises(OrderValidationError):
        split_symbol("BTCUSDT")
    with pytest.raises(OrderValidationError):
        split_symbol("/USDT")


# ---------------------------------------------------------------- 매수
def test_market_buy_fill_price_fee_and_balances(broker, market):
    fill = broker.market_buy(BTC, 1_000.0)
    assert isinstance(fill, Fill)
    assert fill.side == "buy"
    assert fill.symbol == BTC
    assert fill.price == pytest.approx(101.0 * (1 + HardLimits.SLIPPAGE_PCT))
    assert fill.quote_amount == 1_000.0
    assert fill.fee_quote == pytest.approx(1_000.0 * HardLimits.FEE_PCT)
    assert fill.fee_base == 0.0
    assert fill.base_amount == pytest.approx(1_000.0 / fill.price, rel=1e-7)
    assert fill.ts == 123_456
    assert fill.order_id == "paper-1"
    # 잔고: quote는 금액+수수료 차감, base는 수량 증가
    assert broker.free_quote() == pytest.approx(10_000.0 - 1_000.0 - 1.0)
    assert broker.free_base(BTC) == pytest.approx(fill.base_amount)
    assert market.calls == [BTC]


def test_market_buy_amount_is_floored_to_precision(broker):
    fill = broker.market_buy(BTC, 1_000.0)
    # 8자리로 내림되므로 수량*가격 <= 금액
    assert fill.base_amount * fill.price <= 1_000.0 + 1e-6
    assert round(fill.base_amount, 8) == fill.base_amount


def test_market_buy_uses_last_when_ask_missing(market):
    market.ask = None
    b = PaperBroker(market, 1_000.0)
    fill = b.market_buy(BTC, 100.0)
    assert fill.price == pytest.approx(market.last * (1 + HardLimits.SLIPPAGE_PCT))


def test_market_buy_custom_fee_and_slippage(market):
    b = PaperBroker(market, 1_000.0, fee_pct=0.01, slippage_pct=0.02)
    fill = b.market_buy(BTC, 100.0)
    assert fill.price == pytest.approx(101.0 * 1.02)
    assert fill.fee_quote == pytest.approx(1.0)
    assert b.free_quote() == pytest.approx(1_000.0 - 101.0)


def test_market_buy_insufficient_funds_including_fee(market):
    b = PaperBroker(market, 100.0)
    with pytest.raises(InsufficientFunds):
        b.market_buy(BTC, 100.0)  # 100 + 0.1 수수료 > 100
    # 실패 시 잔고 불변
    assert b.free_quote() == 100.0
    assert b.free_base(BTC) == 0.0


def test_market_buy_exactly_affordable(market):
    b = PaperBroker(market, 100.1)
    fill = b.market_buy(BTC, 100.0)
    assert fill.quote_amount == 100.0
    assert b.free_quote() == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -float("inf"), 0.0, -5.0, None, "100"])
def test_market_buy_rejects_non_finite_or_non_positive(broker, amount):
    with pytest.raises(OrderValidationError):
        broker.market_buy(BTC, amount)  # type: ignore[arg-type]
    assert broker.free_quote() == 10_000.0


def test_validation_error_is_also_value_error_and_exchange_error(broker):
    with pytest.raises(ValueError):
        broker.market_buy(BTC, float("nan"))
    with pytest.raises(ExchangeError):
        broker.market_buy(BTC, float("nan"))


def test_market_buy_below_min_order(broker):
    with pytest.raises(OrderValidationError):
        broker.market_buy(BTC, HardLimits.MIN_ORDER_USDT - 0.01)
    broker.market_buy(BTC, HardLimits.MIN_ORDER_USDT)  # 경계는 허용


def test_market_buy_non_usdt_pair_rejected(broker):
    with pytest.raises(OrderValidationError):
        broker.market_buy("BTC/BUSD", 100.0)


def test_market_buy_bad_symbol_rejected(broker):
    with pytest.raises(OrderValidationError):
        broker.market_buy("BTCUSDT", 100.0)


def test_market_buy_ticker_failure_wrapped(market):
    market.raise_on_ticker = RuntimeError("boom")
    b = PaperBroker(market, 1_000.0)
    with pytest.raises(ExchangeError) as ei:
        b.market_buy(BTC, 100.0)
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert b.free_quote() == 1_000.0


def test_market_buy_invalid_price_rejected(market):
    market.ask = None
    market.last = 0.0
    b = PaperBroker(market, 1_000.0)
    with pytest.raises(ExchangeError):
        b.market_buy(BTC, 100.0)


# ---------------------------------------------------------------- 매도
def test_market_sell_fill_price_fee_and_balances(broker):
    buy = broker.market_buy(BTC, 1_000.0)
    quote_after_buy = broker.free_quote()
    sell = broker.market_sell(BTC, buy.base_amount)
    assert sell.side == "sell"
    assert sell.order_id == "paper-2"
    assert sell.price == pytest.approx(100.0 * (1 - HardLimits.SLIPPAGE_PCT))
    assert sell.base_amount == pytest.approx(buy.base_amount)
    assert sell.quote_amount == pytest.approx(buy.base_amount * sell.price)
    assert sell.fee_quote == pytest.approx(sell.quote_amount * HardLimits.FEE_PCT)
    assert sell.fee_base == 0.0
    assert broker.free_base(BTC) == pytest.approx(0.0, abs=1e-12)
    assert broker.free_quote() == pytest.approx(quote_after_buy + sell.quote_amount - sell.fee_quote)


def test_round_trip_loses_fees_and_spread(broker):
    buy = broker.market_buy(BTC, 1_000.0)
    broker.market_sell(BTC, buy.base_amount)
    assert broker.free_quote() < 10_000.0
    assert broker.balances["BTC"] == pytest.approx(0.0, abs=1e-12)


def test_market_sell_partial_keeps_remainder(broker):
    buy = broker.market_buy(BTC, 1_000.0)
    half = broker.amount_to_precision(BTC, buy.base_amount / 2)
    broker.market_sell(BTC, half)
    assert broker.free_base(BTC) == pytest.approx(buy.base_amount - half)


def test_market_sell_more_than_held(broker):
    buy = broker.market_buy(BTC, 1_000.0)
    with pytest.raises(InsufficientFunds):
        broker.market_sell(BTC, buy.base_amount * 1.01)
    assert broker.free_base(BTC) == pytest.approx(buy.base_amount)


def test_market_sell_nothing_held(broker):
    with pytest.raises(InsufficientFunds):
        broker.market_sell(BTC, 0.001)


def test_market_sell_uses_last_when_bid_missing(market):
    market.bid = None
    b = PaperBroker(market, 0.0, balances={"USDT": 0.0, "BTC": 1.0})
    fill = b.market_sell(BTC, 1.0)
    assert fill.price == pytest.approx(market.last * (1 - HardLimits.SLIPPAGE_PCT))


def test_market_sell_small_notional_allowed(market):
    """손절로 10 USDT 밑으로 내려간 포지션도 청산할 수 있어야 한다(최소 주문은 매수에만)."""
    b = PaperBroker(market, 0.0, balances={"BTC": 0.01})  # 0.01 * 100 = 1 USDT
    fill = b.market_sell(BTC, 0.01)
    assert fill.quote_amount == pytest.approx(0.01 * 100.0 * (1 - HardLimits.SLIPPAGE_PCT))


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), 0.0, -1.0, None])
def test_market_sell_rejects_non_finite_or_non_positive(broker, amount):
    broker.set_balances({"USDT": 0.0, "BTC": 1.0})
    with pytest.raises(OrderValidationError):
        broker.market_sell(BTC, amount)  # type: ignore[arg-type]
    assert broker.free_base(BTC) == 1.0


def test_market_sell_ticker_failure_leaves_balances(market):
    b = PaperBroker(market, 0.0, balances={"BTC": 1.0})
    market.raise_on_ticker = RuntimeError("down")
    with pytest.raises(ExchangeError):
        b.market_sell(BTC, 1.0)
    assert b.free_base(BTC) == 1.0


# ---------------------------------------------------------------- 정밀도
def test_amount_to_precision_floors_8_decimals(broker):
    assert broker.amount_to_precision(BTC, 0.123456789123) == pytest.approx(0.12345678)
    assert broker.amount_to_precision(BTC, 1.0) == 1.0


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), 0.0, -1.0])
def test_amount_to_precision_rejects_bad(broker, amount):
    with pytest.raises(OrderValidationError):
        broker.amount_to_precision(BTC, amount)


def test_amount_to_precision_delegates_to_market_when_available(market):
    class PreciseMarket(FakeMarket):
        def amount_to_precision(self, symbol, amount):
            return "0.5"

    b = PaperBroker(PreciseMarket(), 100.0)
    assert b.amount_to_precision(BTC, 0.5678) == 0.5


def test_amount_to_precision_falls_back_when_market_raises():
    class BrokenMarket(FakeMarket):
        def amount_to_precision(self, symbol, amount):
            raise RuntimeError("no markets")

    b = PaperBroker(BrokenMarket(), 100.0)
    assert b.amount_to_precision(BTC, 0.123456789123) == pytest.approx(0.12345678)


# ---------------------------------------------------------------- 결정성/다중 종목
def test_multiple_symbols_tracked_independently(broker):
    broker.market_buy(BTC, 100.0)
    broker.market_buy("ETH/USDT", 200.0)
    assert broker.free_base(BTC) > 0
    assert broker.free_base("ETH/USDT") > 0
    assert set(broker.balances) == {"USDT", "BTC", "ETH"}
    assert broker.free_quote() == pytest.approx(10_000.0 - 300.0 - 0.3)


def test_fill_is_json_serializable(broker):
    import json

    fill = broker.market_buy(BTC, 100.0)
    s = json.dumps(fill.model_dump(), sort_keys=True, ensure_ascii=False)
    assert "paper-1" in s


def test_balances_never_nan_after_operations(broker):
    broker.market_buy(BTC, 100.0)
    broker.market_sell(BTC, broker.free_base(BTC))
    assert all(math.isfinite(v) for v in broker.balances.values())
