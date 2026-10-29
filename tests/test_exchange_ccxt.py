"""CcxtBroker / CcxtMarketData 테스트 (SPEC §4). 가짜 ccxt 객체 주입, 네트워크·키 없음.

가짜 거래소는 덕타이핑: load_markets, fetch_ohlcv, fetch_ticker, fetch_order_book, fetch_balance,
create_order, fetch_order, amount_to_precision, markets, set_sandbox_mode, options.
예외 클래스는 실제 ccxt 것을 쓴다(오프라인 import 가능).
"""
from __future__ import annotations

import logging

import ccxt
import pytest

from tradingbot.config import HardLimits, load_settings
from tradingbot.exchange import (
    Broker,
    CcxtBroker,
    CcxtMarketData,
    ExchangeError,
    InsufficientFunds,
    MarketData,
    OrderValidationError,
    _parse_fill,
    _scrub_exception,
)
from tradingbot.models import Candle, FuturesContext, OrderBookSummary, Ticker

BTC = "BTC/USDT"
API_KEY = "AKIAFAKEKEY1234567890ABCDEF"
API_SECRET = "s3cr3tS3CR3Tsecret9876543210XYZ"


def make_settings(mode: str = "testnet", symbols: str = "BTC/USDT,ETH/USDT"):
    env = {"MODE": mode, "AGENT_BACKEND": "mock", "SYMBOLS": symbols}
    if mode == "testnet":
        env["BINANCE_TESTNET_API_KEY"] = API_KEY
        env["BINANCE_TESTNET_API_SECRET"] = API_SECRET
    elif mode == "live":
        env["CONFIRM_LIVE"] = "YES"
        env["BINANCE_LIVE_API_KEY"] = API_KEY
        env["BINANCE_LIVE_API_SECRET"] = API_SECRET
    return load_settings(env)


# ---------------------------------------------------------------- 가짜 ccxt
class FakeExchange:
    def __init__(self) -> None:
        self.options: dict = {}
        self.sandbox: bool | None = None
        self.markets = {
            BTC: {
                "symbol": BTC, "base": "BTC", "quote": "USDT", "spot": True,
                "precision": {"amount": 0.00001, "price": 0.01},
                "limits": {"amount": {"min": 0.00001, "max": 9000}, "cost": {"min": 5.0, "max": None}},
            },
            "ETH/USDT": {
                "symbol": "ETH/USDT", "base": "ETH", "quote": "USDT", "spot": True,
                "precision": {"amount": 0.0001, "price": 0.01},
                "limits": {"amount": {"min": 0.0001}, "cost": {"min": 20.0}},
            },
        }
        self.balance = {"free": {"USDT": 5_000.0, "BTC": 0.0, "ETH": 0.0}, "used": {}, "total": {}}
        self.ohlcv_rows: list[list] = [[i * 900_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0] for i in range(10)]
        self.ticker = {"symbol": BTC, "last": 100.0, "bid": 99.9, "ask": 100.1, "quoteVolume": 1.5e9, "percentage": 2.5, "timestamp": 1_700_000_000_000}
        self.orderbook = {
            "bids": [[99.9, 1.0], [99.5, 2.0], [98.5, 100.0]],  # 98.5 < 99.9*0.99=98.901 -> 제외
            "asks": [[100.1, 1.0], [100.5, 2.0], [101.5, 100.0]],  # 101.5 > 100.1*1.01=101.101 -> 제외
            "timestamp": 1_700_000_000_500,
        }
        self.calls: list[tuple] = []
        self.fail_queue: dict[str, list[BaseException]] = {}
        self.order_response: dict | None = None
        self.orders: dict[str, dict] = {}
        self.load_markets_calls = 0
        self.time_ms = 1_700_000_123_456

    # 호출 전 큐에 쌓인 예외를 순서대로 던진다
    def _maybe_fail(self, name: str) -> None:
        q = self.fail_queue.get(name)
        if q:
            raise q.pop(0)

    def load_markets(self, reload: bool = False, params: dict | None = None):
        self.load_markets_calls += 1
        self._maybe_fail("load_markets")
        return self.markets

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    def fetch_time(self):
        self._maybe_fail("fetch_time")
        return self.time_ms

    def milliseconds(self):
        return 42

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self.calls.append(("fetch_ohlcv", symbol, timeframe, since, limit))
        self._maybe_fail("fetch_ohlcv")
        return [list(r) for r in self.ohlcv_rows[: (limit or len(self.ohlcv_rows))]]

    def fetch_ticker(self, symbol, params=None):
        self.calls.append(("fetch_ticker", symbol))
        self._maybe_fail("fetch_ticker")
        return dict(self.ticker, symbol=symbol)

    def fetch_order_book(self, symbol, limit=None, params=None):
        self.calls.append(("fetch_order_book", symbol, limit))
        self._maybe_fail("fetch_order_book")
        return dict(self.orderbook)

    def fetch_balance(self, params=None):
        self.calls.append(("fetch_balance",))
        self._maybe_fail("fetch_balance")
        return self.balance

    def amount_to_precision(self, symbol, amount):
        import math

        step = self.markets[symbol]["precision"]["amount"]
        digits = max(0, round(-math.log10(step)))
        return f"{math.floor(float(amount) * 10**digits) / 10**digits:.{digits}f}"

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type, side, amount, price, dict(params or {})))
        self._maybe_fail("create_order")
        if self.order_response is not None:
            resp = dict(self.order_response)
        else:
            resp = self._default_fill(symbol, side, amount, params or {})
        resp.setdefault("clientOrderId", (params or {}).get("newClientOrderId"))
        self.orders[resp["id"]] = resp
        return resp

    def _default_fill(self, symbol, side, amount, params):
        px = self.ticker["ask"] if side == "buy" else self.ticker["bid"]
        if side == "buy":
            cost = float(params["quoteOrderQty"])
            filled = cost / px
            fee = {"currency": "BTC", "cost": filled * 0.001}
        else:
            filled = float(amount)
            cost = filled * px
            fee = {"currency": "USDT", "cost": cost * 0.001}
        return {
            "id": f"o{len(self.orders) + 1}", "status": "closed", "symbol": symbol, "side": side, "type": "market",
            "filled": filled, "cost": cost, "average": px, "price": px, "timestamp": 1_700_000_000_999,
            "fee": fee, "fees": [fee], "info": {},
        }

    def fetch_order(self, id, symbol=None, params=None):
        self.calls.append(("fetch_order", id, symbol, dict(params or {})))
        self._maybe_fail("fetch_order")
        if id is None and params and params.get("origClientOrderId"):
            for o in self.orders.values():
                if o.get("clientOrderId") == params["origClientOrderId"]:
                    return o
            raise ccxt.OrderNotFound("binance order not found")
        if id in self.orders:
            return self.orders[id]
        raise ccxt.OrderNotFound(f"binance {id} not found")


class FakeFutures:
    def __init__(self, funding=0.0001, oi_value=1.23e9, lsr=1.8, fail: set[str] | None = None) -> None:
        self.funding, self.oi_value, self.lsr = funding, oi_value, lsr
        self.fail = fail or set()
        self.symbols: list[str] = []

    def fetch_funding_rate(self, symbol, params=None):
        self.symbols.append(symbol)
        if "funding" in self.fail:
            raise ccxt.ExchangeError("funding down")
        return {"symbol": symbol, "fundingRate": self.funding}

    def fetch_open_interest(self, symbol, params=None):
        if "oi" in self.fail:
            raise ccxt.NetworkError("oi down")
        return {"symbol": symbol, "openInterestValue": self.oi_value, "openInterestAmount": 1000.0, "info": {"markPrice": "100"}}

    def fetch_long_short_ratio_history(self, symbol, timeframe=None, since=None, limit=None, params=None):
        if "lsr" in self.fail:
            raise ccxt.ExchangeError("lsr down")
        return [{"longShortRatio": self.lsr}]


@pytest.fixture
def ex() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def broker(ex, settings, sleeps) -> CcxtBroker:
    return CcxtBroker(settings, exchange=ex, sleep=sleeps.append, futures_factory=FakeFutures, clock_ms=lambda: 777)


@pytest.fixture
def market(ex, sleeps) -> CcxtMarketData:
    return CcxtMarketData(exchange=ex, sleep=sleeps.append, futures_factory=FakeFutures, clock_ms=lambda: 777)


# ---------------------------------------------------------------- 구성
def test_broker_satisfies_protocols(broker):
    assert isinstance(broker, Broker)
    assert isinstance(broker, MarketData)
    assert broker.mode == "testnet"


def test_market_data_satisfies_protocol(market):
    assert isinstance(market, MarketData)


def test_testnet_enables_sandbox_and_options(ex, settings):
    CcxtBroker(settings, exchange=ex)
    assert ex.sandbox is True
    assert ex.options["adjustForTimeDifference"] is True
    assert ex.options["defaultType"] == "spot"


def test_live_does_not_enable_sandbox(ex):
    b = CcxtBroker(make_settings("live"), exchange=ex)
    assert ex.sandbox is None
    assert b.mode == "live"
    assert ex.options["defaultType"] == "spot"


def test_market_data_no_sandbox(ex):
    CcxtMarketData(exchange=ex)
    assert ex.sandbox is None
    assert ex.options["defaultType"] == "spot"


def test_constructor_does_not_hit_network(ex, settings):
    CcxtBroker(settings, exchange=ex)
    assert ex.load_markets_calls == 0
    assert ex.calls == []


def test_repr_masks_key(broker):
    r = repr(broker)
    assert API_KEY not in r and API_SECRET not in r
    assert "CcxtBroker" in r


def test_default_exchange_construction_without_network(settings):
    """exchange 미주입이면 실제 ccxt.binance 객체를 만들되 네트워크는 쓰지 않는다."""
    b = CcxtBroker(settings)
    assert isinstance(b._ex, ccxt.binance)
    assert b._ex.options["defaultType"] == "spot"
    assert b._ex.options["adjustForTimeDifference"] is True
    assert b._ex.apiKey == API_KEY
    m = CcxtMarketData()
    assert isinstance(m._ex, ccxt.binance)
    assert not m._ex.apiKey


# ---------------------------------------------------------------- OHLCV
def test_fetch_ohlcv_requests_limit_plus_one_and_drops_last(broker, ex):
    out = broker.fetch_ohlcv(BTC, "15m", 5)
    assert ex.calls[-1] == ("fetch_ohlcv", BTC, "15m", None, 6)
    assert len(out) == 5
    assert [c.ts for c in out] == [0, 900_000, 1_800_000, 2_700_000, 3_600_000]
    assert all(isinstance(c, Candle) for c in out)
    assert out[0].open == 100 and out[0].close == 100.5


def test_fetch_ohlcv_sorts_and_dedupes(broker, ex):
    ex.ohlcv_rows = [
        [3_000, 1, 1, 1, 1, 1],
        [1_000, 1, 1, 1, 1, 1],
        [2_000, 1, 1, 1, 1, 1],
        [1_000, 9, 9, 9, 9, 9],  # 중복 ts: 뒤 행 우선
        [4_000, 1, 1, 1, 1, 1],  # 마지막(미완)
    ]
    out = broker.fetch_ohlcv(BTC, "15m", 10)
    assert [c.ts for c in out] == [1_000, 2_000, 3_000]
    assert out[0].open == 9


def test_fetch_ohlcv_skips_malformed_rows(broker, ex):
    ex.ohlcv_rows = [[0, 1, 1, 1, 1, 1], [1, None, 1, 1, 1, 1], [2, 1, 1, 1, 1], "junk", [3, 1, 1, 1, 1, None], [4, 1, 1, 1, 1, 1]]
    out = broker.fetch_ohlcv(BTC, "15m", 10)
    assert [c.ts for c in out] == [0, 3]  # 4는 마지막 봉으로 제거, 3은 volume None -> 0.0
    assert out[1].volume == 0.0


def test_fetch_ohlcv_empty_and_single(broker, ex):
    ex.ohlcv_rows = []
    assert broker.fetch_ohlcv(BTC, "15m", 5) == []
    ex.ohlcv_rows = [[0, 1, 1, 1, 1, 1]]
    assert broker.fetch_ohlcv(BTC, "15m", 5) == []


@pytest.mark.parametrize("limit", [0, -1, 1.5, "5"])
def test_fetch_ohlcv_rejects_bad_limit(broker, limit):
    with pytest.raises(OrderValidationError):
        broker.fetch_ohlcv(BTC, "15m", limit)


def test_market_data_shares_fetch_code(market, ex):
    out = market.fetch_ohlcv(BTC, "1h", 3)
    assert ex.calls[-1] == ("fetch_ohlcv", BTC, "1h", None, 4)
    assert len(out) == 3


# ---------------------------------------------------------------- 티커/호가/시각
def test_fetch_ticker(broker):
    t = broker.fetch_ticker(BTC)
    assert isinstance(t, Ticker)
    assert t.symbol == BTC and t.last == 100.0 and t.bid == 99.9 and t.ask == 100.1
    assert t.quote_volume_24h == 1.5e9
    assert t.change_24h_pct == pytest.approx(0.025)  # ccxt % -> 소수
    assert t.ts == 1_700_000_000_000


def test_fetch_ticker_fallbacks(broker, ex):
    ex.ticker = {"last": None, "close": 50.0, "bid": None, "ask": None, "timestamp": None}
    t = broker.fetch_ticker(BTC)
    assert t.last == 50.0 and t.ts == 777 and t.change_24h_pct is None
    ex.ticker = {"last": None, "close": None, "bid": 10.0, "ask": 12.0}
    assert broker.fetch_ticker(BTC).last == 11.0
    ex.ticker = {"last": None, "close": None}
    with pytest.raises(ExchangeError):
        broker.fetch_ticker(BTC)


def test_fetch_orderbook_summary(broker, ex):
    ob = broker.fetch_orderbook_summary(BTC, depth=20)
    assert isinstance(ob, OrderBookSummary)
    assert ex.calls[-1] == ("fetch_order_book", BTC, 20)
    assert ob.best_bid == 99.9 and ob.best_ask == 100.1
    assert ob.spread_pct == pytest.approx(0.2 / 100.0)
    assert ob.bid_depth_usdt_1pct == pytest.approx(99.9 * 1 + 99.5 * 2)
    assert ob.ask_depth_usdt_1pct == pytest.approx(100.1 * 1 + 100.5 * 2)
    assert ob.ts == 1_700_000_000_500


def test_fetch_orderbook_empty_raises(broker, ex):
    ex.orderbook = {"bids": [], "asks": [[1, 1]]}
    with pytest.raises(ExchangeError):
        broker.fetch_orderbook_summary(BTC)


def test_server_time_ms(broker, ex):
    assert broker.server_time_ms() == 1_700_000_123_456
    ex.fail_queue["fetch_time"] = [ccxt.ExchangeError("x")]
    assert broker.server_time_ms() == 42  # fetch_time 실패 -> milliseconds()


def test_server_time_without_fetch_time(ex):
    del FakeExchange.fetch_time
    try:
        m = CcxtMarketData(exchange=FakeExchange(), clock_ms=lambda: 5)
        assert m.server_time_ms() == 42
    finally:
        FakeExchange.fetch_time = lambda self: self.time_ms  # type: ignore[method-assign]


# ---------------------------------------------------------------- 선물 컨텍스트
def test_fetch_futures_context_ok(broker):
    fc = broker.fetch_futures_context(BTC)
    assert isinstance(fc, FuturesContext)
    assert fc.symbol == BTC
    assert fc.funding_rate == pytest.approx(0.0001)
    assert fc.open_interest_usdt == pytest.approx(1.23e9)
    assert fc.long_short_ratio == pytest.approx(1.8)
    assert fc.ts == 777
    assert broker._futures.symbols == ["BTC/USDT:USDT"]  # 선물 심볼 형식


def test_fetch_futures_context_partial_failure(ex, settings):
    b = CcxtBroker(settings, exchange=ex, futures_factory=lambda: FakeFutures(fail={"funding", "lsr"}))
    fc = b.fetch_futures_context(BTC)
    assert fc is not None and fc.funding_rate is None and fc.long_short_ratio is None
    assert fc.open_interest_usdt == pytest.approx(1.23e9)


def test_fetch_futures_context_all_fail_returns_none(ex, settings):
    b = CcxtBroker(settings, exchange=ex, futures_factory=lambda: FakeFutures(fail={"funding", "oi", "lsr"}))
    assert b.fetch_futures_context(BTC) is None


def test_fetch_futures_context_factory_raises_returns_none(ex, settings):
    def boom():
        raise ccxt.NetworkError(f"cannot connect key={API_KEY}")

    b = CcxtBroker(settings, exchange=ex, futures_factory=boom)
    assert b.fetch_futures_context(BTC) is None
    assert b.fetch_futures_context("BAD") is None


def test_fetch_futures_context_client_cached(ex, settings):
    made = []

    def factory():
        made.append(1)
        return FakeFutures()

    b = CcxtBroker(settings, exchange=ex, futures_factory=factory)
    b.fetch_futures_context(BTC)
    b.fetch_futures_context(BTC)
    assert len(made) == 1


def test_fetch_futures_context_oi_amount_times_mark(ex, settings):
    class F(FakeFutures):
        def fetch_open_interest(self, symbol, params=None):
            return {"openInterestValue": None, "openInterestAmount": 1000.0, "info": {"markPrice": "100"}}

    b = CcxtBroker(settings, exchange=ex, futures_factory=F)
    assert b.fetch_futures_context(BTC).open_interest_usdt == pytest.approx(100_000.0)


def test_futures_default_client_is_binanceusdm_public(broker):
    c = broker._default_futures_client()
    assert isinstance(c, ccxt.binanceusdm)
    assert not c.apiKey


# ---------------------------------------------------------------- 잔고/마켓
def test_free_quote_and_base(broker, ex):
    assert broker.free_quote() == 5_000.0
    ex.balance["free"]["BTC"] = 0.5
    assert broker.free_base(BTC) == 0.5
    assert broker.free_base("SOL/USDT") == 0.0


def test_free_handles_alt_balance_shape(broker, ex):
    ex.balance = {"USDT": {"free": 12.5, "used": 0, "total": 12.5}}
    assert broker.free_quote() == 12.5


def test_min_order_usdt_is_max_of_exchange_and_hardlimit(broker):
    assert broker.min_order_usdt(BTC) == HardLimits.MIN_ORDER_USDT  # 5 < 10
    assert broker.min_order_usdt("ETH/USDT") == 20.0


def test_min_order_unknown_market(broker):
    with pytest.raises(ExchangeError):
        broker.min_order_usdt("DOGE/USDT")


def test_markets_loaded_lazily_once(broker, ex):
    broker.min_order_usdt(BTC)
    broker.min_order_usdt(BTC)
    broker.amount_to_precision(BTC, 1.0)
    assert ex.load_markets_calls == 1


def test_amount_to_precision(broker):
    assert broker.amount_to_precision(BTC, 0.123456789) == pytest.approx(0.12345)
    assert broker.amount_to_precision("ETH/USDT", 1.23456789) == pytest.approx(1.2345)
    for bad in (float("nan"), float("inf"), 0.0, -1.0):
        with pytest.raises(OrderValidationError):
            broker.amount_to_precision(BTC, bad)


# ---------------------------------------------------------------- 매수
def test_market_buy_uses_quote_order_qty(broker, ex):
    fill = broker.market_buy(BTC, 400.0)
    call = [c for c in ex.calls if c[0] == "create_order"][-1]
    assert call[1:5] == (BTC, "market", "buy", None)
    assert call[6]["quoteOrderQty"] == "400.00"
    assert call[6]["newClientOrderId"].startswith("tb")
    assert fill.side == "buy" and fill.symbol == BTC
    assert fill.quote_amount == pytest.approx(400.0)
    assert fill.price == pytest.approx(100.1)
    gross = 400.0 / 100.1
    assert fill.fee_base == pytest.approx(gross * 0.001)
    assert fill.base_amount == pytest.approx(gross - gross * 0.001)  # base 수수료 차감
    assert fill.fee_quote == pytest.approx(gross * 0.001 * 100.1)   # 체결가로 환산
    assert fill.order_id == "o1"
    assert fill.ts == 1_700_000_000_999


def test_market_buy_floors_quote_to_cents(broker, ex):
    broker.market_buy(BTC, 123.456789)
    call = [c for c in ex.calls if c[0] == "create_order"][-1]
    assert call[6]["quoteOrderQty"] == "123.45"


def test_market_buy_rejects_non_whitelisted(broker, ex):
    with pytest.raises(OrderValidationError):
        broker.market_buy("SOL/USDT", 100.0)
    assert not [c for c in ex.calls if c[0] == "create_order"]


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), 0.0, -10.0, None])
def test_market_buy_rejects_non_finite(broker, ex, amount):
    with pytest.raises(OrderValidationError):
        broker.market_buy(BTC, amount)  # type: ignore[arg-type]
    assert not [c for c in ex.calls if c[0] == "create_order"]


def test_market_buy_below_min_order(broker, ex):
    with pytest.raises(OrderValidationError):
        broker.market_buy(BTC, 9.99)
    with pytest.raises(OrderValidationError):
        broker.market_buy("ETH/USDT", 15.0)  # 거래소 최소 20
    assert not [c for c in ex.calls if c[0] == "create_order"]


def test_market_buy_insufficient_local_check(broker, ex):
    ex.balance["free"]["USDT"] = 50.0
    with pytest.raises(InsufficientFunds):
        broker.market_buy(BTC, 100.0)
    assert not [c for c in ex.calls if c[0] == "create_order"]


def test_market_buy_exchange_insufficient_mapped(broker, ex):
    ex.fail_queue["create_order"] = [ccxt.InsufficientFunds("binance Account has insufficient balance")]
    with pytest.raises(InsufficientFunds) as ei:
        broker.market_buy(BTC, 100.0)
    assert isinstance(ei.value.__cause__, ccxt.InsufficientFunds)


def test_market_buy_fetches_order_when_response_unfilled(broker, ex):
    ex.order_response = {"id": "o9", "status": "open", "filled": 0.0, "cost": 0.0, "average": None, "info": {}}
    ex.orders["o9"] = {"id": "o9", "status": "closed", "filled": 1.0, "cost": 100.0, "average": 100.0,
                       "fee": {"currency": "USDT", "cost": 0.1}, "fees": [], "info": {}, "timestamp": 5}
    # create_order는 order_response를 저장하므로, 저장 후 fetch가 '닫힌' 버전을 돌려주게 한다
    orig_create = ex.create_order

    def create(*a, **k):
        r = orig_create(*a, **k)
        ex.orders["o9"] = {"id": "o9", "status": "closed", "filled": 1.0, "cost": 100.0, "average": 100.0,
                           "fee": {"currency": "USDT", "cost": 0.1}, "fees": [], "info": {}, "timestamp": 5}
        return r

    ex.create_order = create
    fill = broker.market_buy(BTC, 100.0)
    assert fill.base_amount == 1.0 and fill.fee_quote == 0.1 and fill.order_id == "o9"
    assert any(c[0] == "fetch_order" and c[1] == "o9" for c in ex.calls)


def test_market_buy_unfilled_after_fetch_raises(broker, ex, sleeps):
    ex.order_response = {"id": "o9", "status": "open", "filled": 0.0, "cost": 0.0, "average": None, "info": {}}
    with pytest.raises(ExchangeError, match="체결 수량이 없음"):
        broker.market_buy(BTC, 100.0)
    assert sum(sleeps) <= 2.0  # 주입된 sleep만 사용됐다


# ---------------------------------------------------------------- 매도
def test_market_sell_uses_amount_and_precision(broker, ex):
    ex.balance["free"]["BTC"] = 1.0
    fill = broker.market_sell(BTC, 0.123456789)
    call = [c for c in ex.calls if c[0] == "create_order"][-1]
    assert call[1:5] == (BTC, "market", "sell", 0.12345)
    assert "quoteOrderQty" not in call[6]
    assert fill.side == "sell"
    assert fill.base_amount == pytest.approx(0.12345)
    assert fill.price == pytest.approx(99.9)
    assert fill.quote_amount == pytest.approx(0.12345 * 99.9)
    assert fill.fee_quote == pytest.approx(0.12345 * 99.9 * 0.001)
    assert fill.fee_base == 0.0


def test_market_sell_more_than_held(broker, ex):
    ex.balance["free"]["BTC"] = 0.1
    with pytest.raises(InsufficientFunds):
        broker.market_sell(BTC, 0.2)
    assert not [c for c in ex.calls if c[0] == "create_order"]


def test_market_sell_held_but_not_whitelisted_allowed(broker, ex):
    ex.balance["free"]["ETH"] = 2.0
    b = CcxtBroker(make_settings(symbols="BTC/USDT"), exchange=ex, sleep=lambda s: None)
    fill = b.market_sell("ETH/USDT", 1.0)
    assert fill.symbol == "ETH/USDT"


def test_market_sell_not_held_not_whitelisted_rejected(ex):
    b = CcxtBroker(make_settings(symbols="BTC/USDT"), exchange=ex)
    with pytest.raises(OrderValidationError):
        b.market_sell("ETH/USDT", 1.0)


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), 0.0, -1.0])
def test_market_sell_rejects_non_finite(broker, ex, amount):
    ex.balance["free"]["BTC"] = 1.0
    with pytest.raises(OrderValidationError):
        broker.market_sell(BTC, amount)


def test_market_sell_below_exchange_min_amount(broker, ex):
    ex.balance["free"]["BTC"] = 1.0
    with pytest.raises(OrderValidationError):
        broker.market_sell(BTC, 0.000001)  # 정밀도 적용 후 0


def test_market_sell_below_exchange_min_cost(broker, ex):
    ex.balance["free"]["BTC"] = 1.0
    with pytest.raises(OrderValidationError, match="최소"):
        broker.market_sell(BTC, 0.01)  # 0.01*99.9 = 0.999 < 5


def test_market_sell_dust_above_exchange_min_but_below_hardlimit_ok(broker, ex):
    """청산은 거래소 최소(5)만 지키면 된다(HardLimits 10은 진입 규칙)."""
    ex.balance["free"]["BTC"] = 1.0
    fill = broker.market_sell(BTC, 0.07)  # 6.99 USDT
    assert fill.quote_amount == pytest.approx(0.07 * 99.9)


# ---------------------------------------------------------------- 체결 파싱
def _order(**kw):
    base = {"id": "x", "status": "closed", "filled": 2.0, "cost": 200.0, "average": 100.0, "timestamp": 1, "info": {}}
    base.update(kw)
    return base


def test_parse_fill_fees_list_precedence():
    o = _order(fees=[{"currency": "BTC", "cost": 0.002}, {"currency": "USDT", "cost": 0.05}],
               fee={"currency": "USDT", "cost": 99.0},
               info={"fills": [{"commission": "50", "commissionAsset": "USDT"}]})
    f = _parse_fill(o, BTC, "buy")
    assert f.fee_base == pytest.approx(0.002)
    assert f.base_amount == pytest.approx(1.998)
    assert f.fee_quote == pytest.approx(0.05 + 0.002 * 100.0)
    assert f.quote_amount == 200.0 and f.price == 100.0


def test_parse_fill_fee_when_fees_empty():
    o = _order(fees=[], fee={"currency": "USDT", "cost": 0.2}, info={"fills": [{"commission": "50", "commissionAsset": "USDT"}]})
    f = _parse_fill(o, BTC, "sell")
    assert f.fee_quote == pytest.approx(0.2) and f.fee_base == 0.0 and f.base_amount == 2.0


def test_parse_fill_info_fills_fallback():
    o = _order(fees=None, fee=None, info={"fills": [
        {"price": "100", "qty": "1", "commission": "0.001", "commissionAsset": "BTC"},
        {"price": "100", "qty": "1", "commission": "0.001", "commissionAsset": "BTC"},
    ]})
    f = _parse_fill(o, BTC, "buy")
    assert f.fee_base == pytest.approx(0.002)
    assert f.base_amount == pytest.approx(1.998)
    assert f.fee_quote == pytest.approx(0.2)


def test_parse_fill_derives_filled_cost_avg_from_info_fills():
    o = _order(filled=None, cost=None, average=None, fees=[], fee=None,
               info={"fills": [{"price": "100", "qty": "1", "commission": "0", "commissionAsset": "USDT"},
                               {"price": "102", "qty": "1", "commission": "0", "commissionAsset": "USDT"}]})
    f = _parse_fill(o, BTC, "buy")
    assert f.base_amount == pytest.approx(2.0)
    assert f.quote_amount == pytest.approx(202.0)
    assert f.price == pytest.approx(101.0)


def test_parse_fill_average_from_cost_and_price_fallback():
    f = _parse_fill(_order(average=None, fees=[], fee=None), BTC, "buy")
    assert f.price == pytest.approx(100.0)
    f2 = _parse_fill(_order(average=None, cost=None, price=50.0, fees=[], fee=None), BTC, "buy")
    assert f2.price == 50.0 and f2.quote_amount == pytest.approx(100.0)


def test_parse_fill_unknown_fee_currency_ignored_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="tradingbot.exchange")
    f = _parse_fill(_order(fees=[{"currency": "BNB", "cost": 0.01}]), BTC, "buy")
    assert f.fee_quote == 0.0 and f.fee_base == 0.0 and f.base_amount == 2.0
    assert "BNB" in caplog.text


def test_parse_fill_no_fill_raises():
    with pytest.raises(ExchangeError):
        _parse_fill(_order(filled=0, cost=0), BTC, "buy")
    with pytest.raises(ExchangeError):
        _parse_fill(_order(filled=1, cost=None, average=None, price=None), BTC, "buy")


def test_parse_fill_ts_default_and_raw_serializable():
    import json

    f = _parse_fill(_order(timestamp=None, fees=[]), BTC, "buy", now_ms=99)
    assert f.ts == 99
    json.dumps(f.model_dump(), sort_keys=True, ensure_ascii=False)
    assert f.raw["filled"] == 2.0


def test_parse_fill_fee_case_insensitive_and_string_costs():
    f = _parse_fill(_order(fees=[{"currency": "btc", "cost": "0.001"}]), BTC, "buy")
    assert f.fee_base == pytest.approx(0.001)


# ---------------------------------------------------------------- 재시도
def test_retry_on_network_error_then_success(broker, ex, sleeps):
    ex.fail_queue["fetch_ticker"] = [ccxt.NetworkError("timeout"), ccxt.RateLimitExceeded("429")]
    t = broker.fetch_ticker(BTC)
    assert t.last == 100.0
    assert len([c for c in ex.calls if c[0] == "fetch_ticker"]) == 3
    assert sleeps == [0.5, 1.0]  # 지수 백오프, 주입된 sleep


def test_retry_exchange_not_available(broker, ex, sleeps):
    ex.fail_queue["fetch_ohlcv"] = [ccxt.ExchangeNotAvailable("503")]
    assert len(broker.fetch_ohlcv(BTC, "15m", 3)) == 3
    assert sleeps == [0.5]


def test_retry_gives_up_after_three(broker, ex, sleeps):
    ex.fail_queue["fetch_ticker"] = [ccxt.NetworkError("1"), ccxt.NetworkError("2"), ccxt.NetworkError("3")]
    with pytest.raises(ExchangeError) as ei:
        broker.fetch_ticker(BTC)
    assert len([c for c in ex.calls if c[0] == "fetch_ticker"]) == 3
    assert sleeps == [0.5, 1.0]
    assert isinstance(ei.value.__cause__, ccxt.NetworkError)
    assert "3회" in str(ei.value)


@pytest.mark.parametrize("exc", [ccxt.ExchangeError("bad"), ccxt.BadSymbol("nope"), ccxt.AuthenticationError("401"), RuntimeError("plain")])
def test_no_retry_on_other_errors(broker, ex, sleeps, exc):
    ex.fail_queue["fetch_ticker"] = [exc]
    with pytest.raises(ExchangeError) as ei:
        broker.fetch_ticker(BTC)
    assert len([c for c in ex.calls if c[0] == "fetch_ticker"]) == 1
    assert sleeps == []
    assert ei.value.__cause__ is exc


def test_exchange_error_not_double_wrapped(broker, ex):
    ex.fail_queue["fetch_ticker"] = [ExchangeError("already")]
    with pytest.raises(ExchangeError, match="^already$"):
        broker.fetch_ticker(BTC)


def test_create_order_rate_limit_retries_plainly(broker, ex, sleeps):
    ex.fail_queue["create_order"] = [ccxt.RateLimitExceeded("429")]
    fill = broker.market_buy(BTC, 100.0)
    assert fill.base_amount > 0
    assert len([c for c in ex.calls if c[0] == "create_order"]) == 2
    assert not [c for c in ex.calls if c[0] == "fetch_order"]
    assert sleeps == [0.5]


def test_create_order_network_error_checks_client_id_before_retry(broker, ex, sleeps):
    ex.fail_queue["create_order"] = [ccxt.NetworkError("timeout")]
    fill = broker.market_buy(BTC, 100.0)
    creates = [c for c in ex.calls if c[0] == "create_order"]
    lookups = [c for c in ex.calls if c[0] == "fetch_order" and c[1] is None]
    assert len(creates) == 2 and len(lookups) == 1
    assert lookups[0][3]["origClientOrderId"] == creates[0][6]["newClientOrderId"]
    assert creates[0][6]["newClientOrderId"] == creates[1][6]["newClientOrderId"]
    assert fill.base_amount > 0


def test_create_order_network_error_but_order_was_accepted(broker, ex):
    """타임아웃이었지만 주문이 접수된 경우: 중복 주문 없이 접수된 주문을 사용한다."""
    orig = ex.create_order

    def flaky(symbol, type, side, amount, price=None, params=None):
        orig(symbol, type, side, amount, price, params)  # 주문은 저장됨
        raise ccxt.RequestTimeout("timed out")

    ex.create_order = flaky
    fill = broker.market_buy(BTC, 100.0)
    assert fill.order_id == "o1"
    assert len(ex.orders) == 1


def test_create_order_network_error_exhausted(broker, ex, sleeps):
    ex.fail_queue["create_order"] = [ccxt.NetworkError("1"), ccxt.NetworkError("2"), ccxt.NetworkError("3")]
    with pytest.raises(ExchangeError):
        broker.market_buy(BTC, 100.0)
    assert len([c for c in ex.calls if c[0] == "create_order"]) == 3
    assert len(ex.orders) == 0


def test_create_order_lookup_failure_wrapped(broker, ex):
    ex.fail_queue["create_order"] = [ccxt.NetworkError("1")]
    ex.fail_queue["fetch_order"] = [ccxt.AuthenticationError(f"bad key {API_KEY}")]
    with pytest.raises(ExchangeError) as ei:
        broker.market_buy(BTC, 100.0)
    assert API_KEY not in str(ei.value)


def test_retry_uses_default_sleep_only_when_not_injected(ex, settings, monkeypatch):
    calls = []
    import tradingbot.exchange as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: calls.append(s))
    b = CcxtBroker(settings, exchange=ex)
    ex.fail_queue["fetch_ticker"] = [ccxt.NetworkError("x")]
    b.fetch_ticker(BTC)
    assert calls == [0.5]


# ---------------------------------------------------------------- 비밀값 스크럽
def test_scrub_exception_message_and_cause_chain(settings):
    inner = ccxt.AuthenticationError(f"binance {{\"code\":-2015,\"msg\":\"Invalid API-key {API_KEY} secret {API_SECRET}\"}}")
    try:
        try:
            raise inner
        except ccxt.AuthenticationError as e:
            raise ccxt.ExchangeError(f"wrapped: key={API_KEY}") from e
    except ccxt.ExchangeError as outer:
        msg = _scrub_exception(outer, settings)
        assert API_KEY not in msg and API_SECRET not in msg
        assert API_KEY not in str(outer) and API_KEY not in str(outer.__cause__)
        assert API_SECRET not in str(outer.__cause__)
        assert "…(len=" in str(outer.__cause__)


def test_scrub_handles_context_chain_and_cycles(settings):
    a = ValueError(f"a {API_SECRET}")
    b = ValueError(f"b {API_KEY}")
    a.__context__ = b
    b.__context__ = a  # 순환
    assert API_SECRET not in _scrub_exception(a, settings)
    assert API_KEY not in str(b)


def test_scrub_without_settings_or_keys():
    e = RuntimeError("plain message")
    assert _scrub_exception(e, None) == "plain message"
    assert _scrub_exception(e, make_settings("paper")) == "plain message"


def test_broker_errors_never_contain_secrets(broker, ex, caplog):
    caplog.set_level(logging.DEBUG, logger="tradingbot.exchange")
    ex.fail_queue["fetch_balance"] = [ccxt.AuthenticationError(f"Invalid API-key {API_KEY}, secret {API_SECRET}")]
    with pytest.raises(ExchangeError) as ei:
        broker.free_quote()
    chain, cur = [], ei.value
    while cur is not None:
        chain.append(str(cur))
        cur = cur.__cause__
    text = "\n".join(chain) + repr(ei.value) + caplog.text
    assert API_KEY not in text and API_SECRET not in text
    assert "AuthenticationError" in str(ei.value)


def test_retry_warnings_scrubbed(broker, ex, caplog):
    caplog.set_level(logging.WARNING, logger="tradingbot.exchange")
    ex.fail_queue["fetch_ticker"] = [ccxt.NetworkError(f"timeout for {API_KEY}")]
    broker.fetch_ticker(BTC)
    assert API_KEY not in caplog.text and "재시도" in caplog.text


def test_futures_failure_log_scrubbed(ex, settings, caplog):
    caplog.set_level(logging.INFO, logger="tradingbot.exchange")

    def boom():
        raise RuntimeError(f"secret {API_SECRET}")

    CcxtBroker(settings, exchange=ex, futures_factory=boom).fetch_futures_context(BTC)
    assert API_SECRET not in caplog.text


# ---------------------------------------------------------------- 로컬 검증 미러
def test_local_validate_mirror_matches_spec():
    from tradingbot.exchange import _validate
    from tradingbot.models import OrderRequest

    _validate(OrderRequest(symbol=BTC, side="buy", quote_amount=10.0), (BTC,), ())
    _validate(OrderRequest(symbol=BTC, side="sell", base_amount=1.0), (), (BTC,))
    for req, wl, held in [
        (OrderRequest(symbol="SOL/USDT", side="buy", quote_amount=10.0), (BTC,), ()),
        (OrderRequest(symbol=BTC, side="buy", quote_amount=float("nan")), (BTC,), ()),
        (OrderRequest(symbol=BTC, side="buy", quote_amount=-1.0), (BTC,), ()),
        (OrderRequest(symbol=BTC, side="buy", quote_amount=None), (BTC,), ()),
        (OrderRequest(symbol=BTC, side="sell", base_amount=1.0), (), ()),
        (OrderRequest(symbol=BTC, side="sell", base_amount=float("inf")), (BTC,), ()),
        (OrderRequest(symbol=BTC, side="sell", base_amount=0.0), (BTC,), ()),
    ]:
        with pytest.raises(ValueError):
            _validate(req, wl, held)


def test_local_validate_fallback_without_risk(monkeypatch):
    import tradingbot.exchange as mod
    from tradingbot.models import OrderRequest

    monkeypatch.setattr(mod, "_risk_validate_order_request", None)
    mod._validate(OrderRequest(symbol=BTC, side="buy", quote_amount=10.0), (BTC,), ())
    with pytest.raises(ValueError):
        mod._validate(OrderRequest(symbol="SOL/USDT", side="buy", quote_amount=10.0), (BTC,), ())
    with pytest.raises(ValueError):
        mod._validate(OrderRequest(symbol=BTC, side="sell", base_amount=1.0), (), ())
    with pytest.raises(ValueError):
        mod._validate(OrderRequest(symbol=BTC, side="buy", quote_amount=10.0, base_amount=1.0), (BTC,), ())


def test_fetch_perp_mark_reuses_funding_mark_then_falls_back(ex, sleeps):
    """CONTRACT-2: perp mark for FlowPack.perp_premium_pct — funding response markPrice, else fetch_mark_price."""
    calls: list[str] = []

    class F(FakeFutures):
        def fetch_funding_rate(self, symbol, params=None):
            return {"symbol": symbol, "fundingRate": 0.0001, "markPrice": "101.5"}

        def fetch_mark_price(self, symbol, params=None):
            calls.append(symbol)
            return {"symbol": symbol, "markPrice": "99.0"}

    now = [1_000_000]
    m = CcxtMarketData(exchange=ex, sleep=sleeps.append, futures_factory=F, clock_ms=lambda: now[0])
    m.fetch_futures_context(BTC)
    assert m.fetch_perp_mark(BTC) == pytest.approx(101.5) and calls == []
    now[0] += 120_000                                   # stale cache → dedicated mark-price lookup
    assert m.fetch_perp_mark(BTC) == pytest.approx(99.0) and calls == ["BTC/USDT:USDT"]


def test_fetch_perp_mark_never_raises(ex, sleeps):
    def boom():
        raise RuntimeError("no futures")

    m = CcxtMarketData(exchange=ex, sleep=sleeps.append, futures_factory=boom, clock_ms=lambda: 777)
    assert m.fetch_perp_mark(BTC) is None
    assert CcxtMarketData(exchange=ex, sleep=sleeps.append, futures_factory=FakeFutures,
                          clock_ms=lambda: 777).fetch_perp_mark(BTC) is None   # no markPrice anywhere
