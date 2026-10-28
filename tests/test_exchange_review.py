"""exchange.py 적대적 리뷰 테스트 (SPEC §4).

각 테스트는 현재 코드에서 실패하며, 결함이 고쳐지면 통과해야 한다. 전부 오프라인(가짜 ccxt 객체 주입, 실제 ccxt 예외 클래스만 사용).
"""
from __future__ import annotations

import ccxt
import pytest

from tradingbot.config import load_settings
from tradingbot.exchange import CcxtBroker, CcxtMarketData, ExchangeError
from tradingbot.models import Candle, Fill

BTC = "BTC/USDT"
API_KEY = "AKIAFAKEKEY1234567890ABCDEF"
API_SECRET = "s3cr3tS3CR3Tsecret9876543210XYZ"


def make_settings():
    return load_settings({
        "MODE": "testnet", "AGENT_BACKEND": "mock", "SYMBOLS": "BTC/USDT,ETH/USDT",
        "BINANCE_TESTNET_API_KEY": API_KEY, "BINANCE_TESTNET_API_SECRET": API_SECRET,
    })


class FakeExchange:
    """덕타이핑 가짜 ccxt.binance. fail_queue[name]에 쌓인 예외를 호출 순서대로 던진다."""

    def __init__(self) -> None:
        self.options: dict = {}
        self.markets = {
            BTC: {
                "symbol": BTC, "base": "BTC", "quote": "USDT", "spot": True,
                "precision": {"amount": 0.00001, "price": 0.01},
                "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5.0}},
            },
        }
        self.balance = {"free": {"USDT": 5_000.0, "BTC": 0.5}, "used": {}, "total": {}}
        self.ticker = {"symbol": BTC, "last": 100.0, "bid": 99.9, "ask": 100.1, "quoteVolume": 1e9, "percentage": 2.5, "timestamp": 1_700_000_000_000}
        self.ohlcv_rows: list[list] = [[i * 900_000, 100, 101, 99, 100.5, 10.0] for i in range(10)]
        self.calls: list[tuple] = []
        self.fail_queue: dict[str, list[BaseException]] = {}
        self.orders: dict[str, dict] = {}

    def _maybe_fail(self, name: str) -> None:
        q = self.fail_queue.get(name)
        if q:
            raise q.pop(0)

    def load_markets(self, reload=False, params=None):
        return self.markets

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self._maybe_fail("fetch_ohlcv")
        return [list(r) for r in self.ohlcv_rows[: (limit or len(self.ohlcv_rows))]]

    def fetch_ticker(self, symbol, params=None):
        self.calls.append(("fetch_ticker", symbol))
        self._maybe_fail("fetch_ticker")
        return dict(self.ticker, symbol=symbol)

    def fetch_balance(self, params=None):
        self._maybe_fail("fetch_balance")
        return self.balance

    def amount_to_precision(self, symbol, amount):
        import math

        return f"{math.floor(float(amount) * 1e5) / 1e5:.5f}"

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type, side, amount, price, dict(params or {})))
        self._maybe_fail("create_order")
        px = self.ticker["ask"] if side == "buy" else self.ticker["bid"]
        if side == "buy":
            cost = float(params["quoteOrderQty"])
            filled = cost / px
            fee = {"currency": "BTC", "cost": filled * 0.001}
        else:
            filled = float(amount)
            cost = filled * px
            fee = {"currency": "USDT", "cost": cost * 0.001}
        resp = {
            "id": f"o{len(self.orders) + 1}", "clientOrderId": (params or {}).get("newClientOrderId"),
            "status": "closed", "symbol": symbol, "side": side, "type": "market",
            "filled": filled, "cost": cost, "average": px, "price": px, "timestamp": 1_700_000_000_999,
            "fee": fee, "fees": [fee], "info": {},
        }
        self.orders[resp["id"]] = resp
        return resp

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


@pytest.fixture
def ex() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def broker(ex, sleeps) -> CcxtBroker:
    return CcxtBroker(make_settings(), exchange=ex, sleep=sleeps.append, clock_ms=lambda: 1)


# ---------------------------------------------------------------- F1: 선물 OI가 실제 ccxt 응답에서는 항상 None
class RealisticFutures:
    """실제 ccxt.binanceusdm 응답 형태.

    - fetch_funding_rate: ccxt parse_funding_rate 는 fundingRate 와 함께 markPrice 를 돌려준다.
    - fetch_open_interest: fapiPublicGetOpenInterest 응답은 {"symbol","openInterest","time"} 뿐이라
      ccxt parse_open_interest 결과의 openInterestValue 는 None 이고 info 에 markPrice 가 없다.
    """

    def fetch_funding_rate(self, symbol, params=None):
        return {
            "info": {"symbol": "BTCUSDT", "markPrice": "50000.00", "lastFundingRate": "0.00010000"},
            "symbol": symbol, "markPrice": 50000.0, "indexPrice": 49990.0, "fundingRate": 0.0001,
            "timestamp": 1_700_000_000_000,
        }

    def fetch_open_interest(self, symbol, params=None):
        return {
            "symbol": symbol, "baseVolume": 1000.0, "quoteVolume": None,
            "openInterestAmount": 1000.0, "openInterestValue": None,
            "timestamp": 1_700_000_000_000, "datetime": None,
            "info": {"symbol": "BTCUSDT", "openInterest": "1000.000", "time": 1_700_000_000_000},
        }


def test_f1_open_interest_usdt_from_realistic_binanceusdm_responses(ex):
    """OI 수량(1000)과 markPrice(50000)가 모두 있는데 open_interest_usdt 가 None 이면 FlowPack 의 OI 지표가 영구히 비어 버린다."""
    md = CcxtMarketData(exchange=ex, futures_factory=RealisticFutures, clock_ms=lambda: 1)
    ctx = md.fetch_futures_context(BTC)
    assert ctx is not None
    assert ctx.funding_rate == pytest.approx(0.0001)
    assert ctx.open_interest_usdt == pytest.approx(1000.0 * 50000.0)


# ---------------------------------------------------------------- F2: clientOrderId 조회가 _retry 를 타지 않는다
def test_f2_client_order_lookup_is_retried_on_transient_network_error(broker, ex, sleeps):
    """SPEC: 모든 ccxt 호출은 _retry(3회). create_order 가 NetworkError 로 실패한 뒤 clientOrderId 조회가
    일시적 NetworkError 를 한 번 내면, 재시도 없이 ExchangeError 로 포기해 버린다(주문 접수 여부 불명 상태로 종료).
    기대: 조회를 재시도 → OrderNotFound → 재제출 → 체결 1건."""
    ex.fail_queue["create_order"] = [ccxt.NetworkError("timeout")]
    ex.fail_queue["fetch_order"] = [ccxt.NetworkError("lookup timeout")]
    fill = broker.market_buy(BTC, 100.0)
    assert isinstance(fill, Fill)
    assert len([c for c in ex.calls if c[0] == "create_order"]) == 2  # 1차 실패 + 재제출, 중복 없음
    assert len(ex.orders) == 1


# ---------------------------------------------------------------- F3: 청산이 티커 조회 실패로 막힌다
def test_f3_market_sell_not_blocked_by_ticker_outage(broker, ex):
    """최소 주문 금액 사전 검사를 위한 fetch_ticker 가 실패하면 매도(청산)가 아예 전송되지 않는다.
    거래소가 MIN_NOTIONAL 을 자체 검증하므로 청산은 fail-open 이어야 한다(로그 후 진행)."""
    ex.fail_queue["fetch_ticker"] = [ccxt.NetworkError("ticker down")] * 3
    fill = broker.market_sell(BTC, 0.01)
    assert fill.side == "sell" and fill.base_amount > 0
    assert any(c[0] == "create_order" for c in ex.calls)


# ---------------------------------------------------------------- F4: 잘못된 ts 가 ValueError 로 사이클까지 새어 나간다
@pytest.mark.parametrize("bad_ts", ["not-a-number", float("nan")])
def test_f4_fetch_ohlcv_bad_timestamp_does_not_leak_valueerror(ex, bad_ts):
    """SPEC §0: 거래소 오류는 ExchangeError 로 감싸거나 건너뛴다. int(ts) 가 ValueError 를 내며 그대로 전파된다."""
    ex.ohlcv_rows = [[0, 1, 2, 0.5, 1.5, 1.0], [bad_ts, 1, 2, 0.5, 1.5, 1.0], [1_800_000, 1, 2, 0.5, 1.5, 1.0]]
    md = CcxtMarketData(exchange=ex, clock_ms=lambda: 1)
    try:
        out = md.fetch_ohlcv(BTC, "15m", 2)
    except ExchangeError:
        return
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"ExchangeError 가 아닌 예외가 새어 나감: {e!r}")
    assert all(isinstance(c, Candle) for c in out)


def test_f4_fetch_ticker_bad_timestamp_does_not_leak_valueerror(ex):
    ex.ticker["timestamp"] = "yesterday"
    md = CcxtMarketData(exchange=ex, clock_ms=lambda: 1)
    try:
        t = md.fetch_ticker(BTC)
    except ExchangeError:
        return
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"ExchangeError 가 아닌 예외가 새어 나감: {e!r}")
    assert t.last == 100.0
