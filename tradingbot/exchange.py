"""거래소 계층 (SPEC §4).

- ``MarketData`` / ``Broker``: 사이클이 의존하는 프로토콜.
- ``PaperBroker``: 가상 체결(호가 ± 슬리피지, 수수료는 quote로 차감, 잔고는 메모리).
- ``CcxtBroker``: Binance 현물(ccxt). testnet은 sandbox 모드. 모든 ccxt 호출은 ``_retry``.
- ``CcxtMarketData``: 키 없는 공개 시세 전용(paper 모드).

규칙
- 네트워크·키 없이 import 가능. 테스트는 가짜 ccxt 객체를 주입한다.
- 비밀값(API 키·시크릿)은 어떤 예외 메시지에도 남지 않는다(``_scrub_exception``).
- 거래소 오류는 ``ExchangeError``(또는 ``InsufficientFunds``)로 감싼다. 원 예외는 ``__cause__``.
- 금액 단위: 수량은 base float, 금액은 USDT float, 비율은 소수(0.02 = 2%).
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

from tradingbot.config import HardLimits, Settings, mask_secret
from tradingbot.models import Candle, Fill, FuturesContext, OrderBookSummary, OrderRequest, Ticker

try:  # risk.py가 아직 없으면 아래 _validate(로컬 미러)를 쓴다.
    from tradingbot.risk import validate_order_request as _risk_validate_order_request
except Exception:  # pragma: no cover - 동시 작성 중인 모듈 보호
    _risk_validate_order_request = None  # type: ignore[assignment]

log = logging.getLogger("tradingbot.exchange")

QUOTE = "USDT"
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 0.5
DEPTH_BAND_PCT = 0.01  # 호가 깊이 집계 범위(최우선 호가 대비 1%)
PAPER_AMOUNT_DECIMALS = 8


# ---------------------------------------------------------------- 예외
class ExchangeError(Exception):
    """거래소 계층 오류. 메시지에는 비밀값이 없다."""


class InsufficientFunds(ExchangeError):
    """잔고 부족."""


class OrderValidationError(ExchangeError, ValueError):
    """주문 전 로컬 검증 실패(화이트리스트·유한성·최소 주문 등). ValueError로도 잡힌다."""


# ---------------------------------------------------------------- 프로토콜
@runtime_checkable
class MarketData(Protocol):
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def fetch_ticker(self, symbol: str) -> Ticker: ...
    def fetch_orderbook_summary(self, symbol: str, depth: int = 50) -> OrderBookSummary: ...
    def fetch_futures_context(self, symbol: str) -> FuturesContext | None: ...
    def server_time_ms(self) -> int: ...


@runtime_checkable
class Broker(Protocol):
    mode: str

    def free_quote(self) -> float: ...
    def free_base(self, symbol: str) -> float: ...
    def market_buy(self, symbol: str, quote_amount: float) -> Fill: ...
    def market_sell(self, symbol: str, base_amount: float) -> Fill: ...
    def min_order_usdt(self, symbol: str) -> float: ...
    def amount_to_precision(self, symbol: str, amount: float) -> float: ...


# ---------------------------------------------------------------- 공용 도우미
def _is_finite_positive(x: Any) -> bool:
    """주문 수량/금액용: 숫자 타입(bool 제외)이고 유한한 양수. 문자열은 거부."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    v = float(x)
    return math.isfinite(v) and v > 0


def _as_float(x: Any, default: float | None = None) -> float | None:
    """ccxt 값(문자열/None 포함)을 유한한 float로. 불가능하면 default."""
    if x is None or isinstance(x, bool):
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_int_ms(x: Any) -> int | None:
    """ccxt 타임스탬프(int/float/숫자 문자열)를 int ms로. NaN·비숫자·bool이면 None."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return int(v)


def split_symbol(symbol: str) -> tuple[str, str]:
    """'BTC/USDT' -> ('BTC', 'USDT'). 형식이 아니면 OrderValidationError."""
    if not isinstance(symbol, str) or "/" not in symbol:
        raise OrderValidationError(f"종목 기호 형식 오류: {symbol!r}")
    base, quote = symbol.split("/", 1)
    if not base or not quote:
        raise OrderValidationError(f"종목 기호 형식 오류: {symbol!r}")
    return base, quote


def _validate(req: OrderRequest, whitelist: tuple[str, ...], held: Iterable[str]) -> None:
    """SPEC §3 ``validate_order_request``의 로컬 미러. risk.py가 있으면 그것을 우선 사용."""
    if _risk_validate_order_request is not None:
        _risk_validate_order_request(req, whitelist, held)
        return
    if req.side == "buy":
        if req.symbol not in whitelist:
            raise ValueError(f"buy: 화이트리스트 밖의 종목: {req.symbol!r}")
        if req.base_amount is not None:
            raise ValueError("buy: base_amount는 지정할 수 없음")
        if not _is_finite_positive(req.quote_amount):
            raise ValueError(f"buy: quote_amount는 유한한 양수여야 함: {req.quote_amount!r}")
        return
    if req.side == "sell":
        if req.symbol not in set(held) and req.symbol not in whitelist:
            raise ValueError(f"sell: 보유/화이트리스트 밖의 종목: {req.symbol!r}")
        if req.quote_amount is not None:
            raise ValueError("sell: quote_amount는 지정할 수 없음")
        if not _is_finite_positive(req.base_amount):
            raise ValueError(f"sell: base_amount는 유한한 양수여야 함: {req.base_amount!r}")
        return
    raise ValueError(f"알 수 없는 주문 방향: {req.side!r}")


def _validate_request(req: OrderRequest, whitelist: tuple[str, ...], held: Iterable[str]) -> None:
    """검증 실패를 OrderValidationError(ExchangeError+ValueError)로 통일."""
    try:
        _validate(req, whitelist, held)
    except OrderValidationError:
        raise
    except ValueError as e:
        raise OrderValidationError(str(e)) from None


# ---------------------------------------------------------------- 비밀값 스크럽
def _secrets_of(settings: Settings | None) -> tuple[str, ...]:
    if settings is None:
        return ()
    out = []
    for v in (settings.binance_api_key, settings.binance_api_secret):
        if isinstance(v, str) and len(v) >= 4:
            out.append(v)
    return tuple(out)


def _scrub_text(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, mask_secret(s))
    return text


def _scrub_exception(exc: BaseException, settings: Settings | None) -> str:
    """예외 메시지에서 API 키/시크릿을 제거한다.

    ``exc``와 ``__cause__``/``__context__`` 체인의 ``args``를 제자리에서 스크럽하고,
    스크럽된 최상위 메시지를 돌려준다. 체인 어디에도 비밀값이 남지 않는다.
    """
    secrets = _secrets_of(settings)
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if secrets:
            new_args = tuple(_scrub_text(a, secrets) if isinstance(a, str) else a for a in cur.args)
            if new_args != cur.args:
                try:
                    cur.args = new_args
                except Exception:  # pragma: no cover - args가 읽기 전용인 희귀 예외
                    pass
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    msg = str(exc)
    return _scrub_text(msg, secrets) if secrets else msg


# ---------------------------------------------------------------- ccxt 예외 분류
def _ccxt_module():
    try:
        import ccxt  # noqa: WPS433 - 지연 import: 네트워크 없이 import 가능
    except Exception:  # pragma: no cover - requirements에 있음
        return None
    return ccxt


def _retryable_types() -> tuple[type[BaseException], ...]:
    ccxt = _ccxt_module()
    if ccxt is None:  # pragma: no cover
        return ()
    return (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable)


def _insufficient_types() -> tuple[type[BaseException], ...]:
    ccxt = _ccxt_module()
    if ccxt is None:  # pragma: no cover
        return ()
    return (ccxt.InsufficientFunds,)


def _ratelimit_types() -> tuple[type[BaseException], ...]:
    ccxt = _ccxt_module()
    if ccxt is None:  # pragma: no cover
        return ()
    return (ccxt.RateLimitExceeded,)


def _order_not_found_types() -> tuple[type[BaseException], ...]:
    ccxt = _ccxt_module()
    if ccxt is None:  # pragma: no cover
        return ()
    return (ccxt.OrderNotFound,)


# ---------------------------------------------------------------- 데이터 파싱(공유)
def _parse_candles(rows: Any, *, drop_last: bool) -> list[Candle]:
    """ccxt OHLCV 행 -> Candle. ts 오름차순·중복 제거(같은 ts는 뒤 행 우선). drop_last면 마지막(미완) 봉 제거."""
    by_ts: dict[int, Candle] = {}
    for row in rows or []:
        try:
            ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        except (TypeError, IndexError):
            log.warning("OHLCV 행 형식 오류, 건너뜀: %r", row)
            continue
        vals = [_as_float(o), _as_float(h), _as_float(l), _as_float(c), _as_float(v, 0.0)]
        ts_i = _as_int_ms(ts)
        if ts_i is None or any(x is None for x in vals):
            log.warning("OHLCV 행에 None/NaN/비숫자, 건너뜀: %r", row)
            continue
        by_ts[ts_i] = Candle(ts=ts_i, open=vals[0], high=vals[1], low=vals[2], close=vals[3], volume=vals[4])
    out = [by_ts[k] for k in sorted(by_ts)]
    if drop_last and out:
        out.pop()
    return out


def _parse_ticker(symbol: str, raw: dict, now_ms: int) -> Ticker:
    last = _as_float(raw.get("last"))
    if last is None:
        last = _as_float(raw.get("close"))
    bid, ask = _as_float(raw.get("bid")), _as_float(raw.get("ask"))
    if last is None:
        if bid is not None and ask is not None:
            last = (bid + ask) / 2.0
        else:
            raise ExchangeError(f"{symbol}: 티커에 가격이 없음")
    pct = _as_float(raw.get("percentage"))
    ts = _as_int_ms(raw.get("timestamp"))
    return Ticker(
        symbol=symbol,
        last=last,
        bid=bid,
        ask=ask,
        quote_volume_24h=_as_float(raw.get("quoteVolume")),
        change_24h_pct=(pct / 100.0) if pct is not None else None,  # ccxt는 % 단위 -> 소수
        ts=ts if ts is not None else now_ms,
    )


def _depth_usdt(levels: Any, limit_price: float, side: str) -> float:
    total = 0.0
    for lvl in levels or []:
        try:
            p, a = _as_float(lvl[0]), _as_float(lvl[1])
        except (TypeError, IndexError):
            continue
        if p is None or a is None:
            continue
        if side == "bid" and p < limit_price:
            break
        if side == "ask" and p > limit_price:
            break
        total += p * a
    return total


def _parse_orderbook(symbol: str, raw: dict, now_ms: int) -> OrderBookSummary:
    bids, asks = raw.get("bids") or [], raw.get("asks") or []
    if not bids or not asks:
        raise ExchangeError(f"{symbol}: 호가가 비어 있음")
    best_bid, best_ask = _as_float(bids[0][0]), _as_float(asks[0][0])
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        raise ExchangeError(f"{symbol}: 최우선 호가가 유효하지 않음")
    mid = (best_bid + best_ask) / 2.0
    ts = _as_int_ms(raw.get("timestamp"))
    return OrderBookSummary(
        symbol=symbol,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_pct=(best_ask - best_bid) / mid,
        bid_depth_usdt_1pct=_depth_usdt(bids, best_bid * (1 - DEPTH_BAND_PCT), "bid"),
        ask_depth_usdt_1pct=_depth_usdt(asks, best_ask * (1 + DEPTH_BAND_PCT), "ask"),
        ts=ts if ts is not None else now_ms,
    )


def _parse_fill(order: dict, symbol: str, side: str, *, now_ms: int | None = None) -> Fill:
    """ccxt 주문 dict -> Fill.

    - base_amount: 순수량(= filled - base로 나간 수수료)
    - quote_amount: 체결 금액(gross, cost)
    - fee_quote: 수수료 총액을 quote로 환산(base 수수료는 체결가로 환산)
    - 수수료 출처 우선순위: fees(비어 있지 않은 리스트) > fee > info.fills
    """
    base, quote = split_symbol(symbol)
    filled = _as_float(order.get("filled"))
    cost = _as_float(order.get("cost"))
    avg = _as_float(order.get("average"))
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    raw_fills = info.get("fills") if isinstance(info.get("fills"), list) else []

    if (filled is None or filled <= 0) and raw_fills:
        filled = sum((_as_float(f.get("qty"), 0.0) or 0.0) for f in raw_fills)
    if filled is None or filled <= 0:
        raise ExchangeError(f"{symbol} {side}: 체결 수량이 없음 (status={order.get('status')!r})")
    if (cost is None or cost <= 0) and raw_fills:
        cost = sum((_as_float(f.get("qty"), 0.0) or 0.0) * (_as_float(f.get("price"), 0.0) or 0.0) for f in raw_fills)
    if avg is None or avg <= 0:
        if cost is not None and cost > 0:
            avg = cost / filled
        else:
            avg = _as_float(order.get("price"))
    if avg is None or avg <= 0:
        raise ExchangeError(f"{symbol} {side}: 체결가를 알 수 없음")
    if cost is None or cost <= 0:
        cost = filled * avg

    fee_entries: list[tuple[str | None, float]] = []
    fees = order.get("fees")
    if isinstance(fees, list) and len(fees) > 0:
        for f in fees:
            if isinstance(f, dict):
                fee_entries.append((f.get("currency"), _as_float(f.get("cost"), 0.0) or 0.0))
    elif isinstance(order.get("fee"), dict) and order["fee"]:
        f = order["fee"]
        fee_entries.append((f.get("currency"), _as_float(f.get("cost"), 0.0) or 0.0))
    else:
        for f in raw_fills:
            if isinstance(f, dict):
                fee_entries.append((f.get("commissionAsset"), _as_float(f.get("commission"), 0.0) or 0.0))

    fee_base = 0.0
    fee_quote_direct = 0.0
    for cur, amt in fee_entries:
        if amt <= 0:
            continue
        cur_u = str(cur).upper() if cur is not None else None
        if cur_u == base.upper():
            fee_base += amt
        elif cur_u == quote.upper():
            fee_quote_direct += amt
        else:
            log.warning("%s: 수수료 통화 %r 는 quote로 환산 불가, 무시", symbol, cur)

    net_base = max(filled - fee_base, 0.0)
    fee_quote = fee_quote_direct + fee_base * avg
    ts = _as_int_ms(order.get("timestamp"))
    raw = {
        "id": order.get("id"),
        "status": order.get("status"),
        "filled": filled,
        "cost": cost,
        "average": avg,
        "fees": [{"currency": c, "cost": a} for c, a in fee_entries],
    }
    return Fill(
        order_id=str(order.get("id") or order.get("clientOrderId") or ""),
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        base_amount=net_base,
        price=avg,
        quote_amount=cost,
        fee_quote=fee_quote,
        fee_base=fee_base,
        ts=ts if ts is not None else (now_ms if now_ms is not None else _now_ms()),
        raw=raw,
    )


# ---------------------------------------------------------------- ccxt 공통 베이스
class _CcxtBase:
    """ccxt 호출·재시도·스크럽·시세 파싱을 공유하는 베이스. 직접 쓰지 않는다."""

    def __init__(
        self,
        settings: Settings | None,
        *,
        exchange: Any | None,
        with_keys: bool,
        sandbox: bool,
        sleep: Callable[[float], None] | None = None,
        futures_factory: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock_ms = clock_ms if clock_ms is not None else _now_ms
        self._futures_factory = futures_factory
        self._futures: Any | None = None
        self._markets: dict | None = None
        self._ex = exchange if exchange is not None else self._build_exchange(settings, with_keys=with_keys)
        self._configure_exchange(self._ex, sandbox=sandbox)

    # -- 구성 ---------------------------------------------------------
    @staticmethod
    def _build_exchange(settings: Settings | None, *, with_keys: bool) -> Any:
        ccxt = _ccxt_module()
        if ccxt is None:  # pragma: no cover
            raise ExchangeError("ccxt가 설치되어 있지 않음")
        cfg: dict = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True, "defaultType": "spot"}}
        if with_keys and settings is not None:
            cfg["apiKey"] = settings.binance_api_key or ""
            cfg["secret"] = settings.binance_api_secret or ""
        return ccxt.binance(cfg)

    @staticmethod
    def _configure_exchange(ex: Any, *, sandbox: bool) -> None:
        opts = getattr(ex, "options", None)
        if isinstance(opts, dict):
            opts["adjustForTimeDifference"] = True
            opts["defaultType"] = "spot"
        else:
            try:
                ex.options = {"adjustForTimeDifference": True, "defaultType": "spot"}
            except Exception:  # pragma: no cover
                pass
        if sandbox:
            ex.set_sandbox_mode(True)

    def _default_futures_client(self) -> Any:
        ccxt = _ccxt_module()
        if ccxt is None:  # pragma: no cover
            raise ExchangeError("ccxt가 설치되어 있지 않음")
        return ccxt.binanceusdm({"enableRateLimit": True})

    # -- 오류 처리 ----------------------------------------------------
    def _wrap(self, exc: BaseException, what: str) -> ExchangeError:
        """ccxt 예외 -> ExchangeError(비밀값 제거). 원 예외는 호출자가 ``from exc``로 연결."""
        msg = _scrub_exception(exc, self._settings)
        text = f"{what}: {type(exc).__name__}: {msg}"
        if isinstance(exc, _insufficient_types()):
            return InsufficientFunds(text)
        return ExchangeError(text)

    def _retry(self, what: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """NetworkError/RateLimitExceeded/ExchangeNotAvailable만 최대 3회(지수 백오프). 나머지는 즉시 감싸서 전파."""
        retryable = _retryable_types()
        last: BaseException | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except ExchangeError:
                raise
            except retryable as e:  # type: ignore[misc]
                last = e
                msg = _scrub_exception(e, self._settings)
                if attempt >= RETRY_ATTEMPTS:
                    break
                delay = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                log.warning("%s 실패(%d/%d) %s: %s -> %.1fs 후 재시도", what, attempt, RETRY_ATTEMPTS, type(e).__name__, msg, delay)
                self._sleep(delay)
            except Exception as e:
                wrapped = self._wrap(e, what)
                log.error("%s 실패: %s", what, wrapped)
                raise wrapped from e
        assert last is not None
        wrapped = self._wrap(last, f"{what} ({RETRY_ATTEMPTS}회 재시도 후)")
        log.error("%s", wrapped)
        raise wrapped from last

    # -- 마켓 ---------------------------------------------------------
    def _load_markets(self) -> dict:
        if self._markets is None:
            markets = self._retry("load_markets", self._ex.load_markets)
            if not isinstance(markets, dict):
                markets = getattr(self._ex, "markets", None) or {}
            self._markets = markets
        return self._markets

    def _market(self, symbol: str) -> dict:
        m = self._load_markets().get(symbol)
        if not isinstance(m, dict):
            raise ExchangeError(f"{symbol}: 거래소에 없는 마켓")
        return m

    # -- MarketData ---------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """완료 봉만 반환. ``limit+1``을 받아 마지막(미완) 봉을 버린다. ts 오름차순·중복 제거."""
        if not isinstance(limit, int) or limit <= 0:
            raise OrderValidationError(f"limit은 양의 정수여야 함: {limit!r}")
        rows = self._retry("fetch_ohlcv", self._ex.fetch_ohlcv, symbol, timeframe, None, limit + 1)
        return _parse_candles(rows, drop_last=True)

    def fetch_ticker(self, symbol: str) -> Ticker:
        raw = self._retry("fetch_ticker", self._ex.fetch_ticker, symbol)
        if not isinstance(raw, dict):
            raise ExchangeError(f"{symbol}: 티커 응답 형식 오류")
        return _parse_ticker(symbol, raw, self._clock_ms())

    def fetch_orderbook_summary(self, symbol: str, depth: int = 50) -> OrderBookSummary:
        raw = self._retry("fetch_order_book", self._ex.fetch_order_book, symbol, depth)
        if not isinstance(raw, dict):
            raise ExchangeError(f"{symbol}: 호가 응답 형식 오류")
        return _parse_orderbook(symbol, raw, self._clock_ms())

    def _futures_client(self) -> Any:
        if self._futures is None:
            factory = self._futures_factory or self._default_futures_client
            self._futures = factory()
        return self._futures

    def _fetch_mark_price(self, client: Any, fsym: str, symbol: str) -> float | None:
        """OI 금액 환산용 마크가격 보조 조회(fetch_mark_price). 실패/미지원이면 None."""
        fn = getattr(client, "fetch_mark_price", None)
        if not callable(fn):
            return None
        try:
            mp = fn(fsym)
            if isinstance(mp, dict):
                px = _as_float(mp.get("markPrice"))
                if px is None:
                    mp_info = mp.get("info") if isinstance(mp.get("info"), dict) else {}
                    px = _as_float(mp_info.get("markPrice"))
                return px
        except Exception as e:
            log.info("%s 마크가격 조회 실패: %s", symbol, _scrub_exception(e, self._settings))
        return None

    def fetch_futures_context(self, symbol: str) -> FuturesContext | None:
        """binanceusdm 공개 데이터(펀딩비·미결제약정·롱숏비). 어떤 실패에도 예외 없이 None."""
        try:
            client = self._futures_client()
            base, quote = split_symbol(symbol)
            fsym = f"{base}/{quote}:{quote}"
            funding = oi = lsr = None
            mark_px: float | None = None  # 펀딩비 응답의 markPrice(OI 금액 환산용)
            try:
                fr = client.fetch_funding_rate(fsym)
                if isinstance(fr, dict):
                    funding = _as_float(fr.get("fundingRate"))
                    mark_px = _as_float(fr.get("markPrice"))
                    if mark_px is None:
                        fr_info = fr.get("info") if isinstance(fr.get("info"), dict) else {}
                        mark_px = _as_float(fr_info.get("markPrice"))
            except Exception as e:
                log.info("%s 펀딩비 조회 실패: %s", symbol, _scrub_exception(e, self._settings))
            try:
                o = client.fetch_open_interest(fsym)
                if isinstance(o, dict):
                    oi = _as_float(o.get("openInterestValue"))
                    if oi is None:
                        # 실제 binanceusdm 응답({symbol, openInterest, time})에는 금액이 없다.
                        # 수량 × markPrice(펀딩비 응답 > OI info > fetch_mark_price)로 환산한다.
                        amt = _as_float(o.get("openInterestAmount"))
                        if amt is None:
                            amt = _as_float(o.get("baseVolume"))
                        info = o.get("info") if isinstance(o.get("info"), dict) else {}
                        px = mark_px if mark_px is not None else _as_float(info.get("markPrice"))
                        if amt is not None and px is None:
                            px = self._fetch_mark_price(client, fsym, symbol)
                        if amt is not None and px is not None and px > 0:
                            oi = amt * px
            except Exception as e:
                log.info("%s 미결제약정 조회 실패: %s", symbol, _scrub_exception(e, self._settings))
            fn = getattr(client, "fetch_long_short_ratio_history", None)
            if callable(fn):
                try:
                    rows = fn(fsym, "5m", None, 1)
                    if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
                        lsr = _as_float(rows[-1].get("longShortRatio"))
                except Exception as e:
                    log.info("%s 롱숏비 조회 실패: %s", symbol, _scrub_exception(e, self._settings))
            if funding is None and oi is None and lsr is None:
                return None
            return FuturesContext(symbol=symbol, funding_rate=funding, open_interest_usdt=oi, long_short_ratio=lsr, ts=self._clock_ms())
        except Exception as e:
            log.info("%s 선물 컨텍스트 실패: %s", symbol, _scrub_exception(e, self._settings))
            return None

    def server_time_ms(self) -> int:
        fn = getattr(self._ex, "fetch_time", None)
        if callable(fn):
            try:
                v = self._retry("fetch_time", fn)
                if v is not None:
                    return int(v)
            except ExchangeError:
                log.warning("서버 시각 조회 실패, 로컬 시각 사용")
        ms = getattr(self._ex, "milliseconds", None)
        if callable(ms):
            try:
                return int(ms())
            except Exception:
                pass
        return self._clock_ms()


# ---------------------------------------------------------------- 공개 시세 전용
class CcxtMarketData(_CcxtBase):
    """paper 모드용 공개 시세(키 없음). 재시도·파싱은 CcxtBroker와 공유."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        exchange: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        futures_factory: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(
            settings, exchange=exchange, with_keys=False, sandbox=False,
            sleep=sleep, futures_factory=futures_factory, clock_ms=clock_ms,
        )

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """PaperBroker가 실제 정밀도를 쓰고 싶을 때 선택적으로 사용."""
        self._load_markets()
        return float(self._retry("amount_to_precision", self._ex.amount_to_precision, symbol, amount))

    def __repr__(self) -> str:
        return "CcxtMarketData(binance spot, public)"


# ---------------------------------------------------------------- 실거래 브로커
class CcxtBroker(_CcxtBase):
    """Binance 현물 브로커(ccxt). testnet은 sandbox. 주문은 시장가만.

    - BUY: ``quoteOrderQty``(USDT 금액)로 주문. SELL: base 수량.
    - 주문 전: validate_order_request → 유한성 → 정밀도 → 최소 주문 → 잔고.
    - 네트워크 오류로 주문 접수 여부가 불명확하면 clientOrderId로 조회 후, 없을 때만 재시도(중복 주문 방지).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        exchange: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        futures_factory: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.mode = settings.mode
        super().__init__(
            settings, exchange=exchange, with_keys=True, sandbox=(settings.mode == "testnet"),
            sleep=sleep, futures_factory=futures_factory, clock_ms=clock_ms,
        )
        log.info("CcxtBroker 준비: mode=%s key=%s", self.mode, mask_secret(settings.binance_api_key))

    def __repr__(self) -> str:
        s = self._settings
        return f"CcxtBroker(mode={self.mode!r}, key={mask_secret(s.binance_api_key if s else None)})"

    # -- 잔고 ---------------------------------------------------------
    def _free(self, currency: str) -> float:
        bal = self._retry("fetch_balance", self._ex.fetch_balance)
        free = bal.get("free") if isinstance(bal, dict) else None
        if isinstance(free, dict):
            v = _as_float(free.get(currency), 0.0)
        else:
            entry = bal.get(currency) if isinstance(bal, dict) else None
            v = _as_float(entry.get("free"), 0.0) if isinstance(entry, dict) else 0.0
        return max(float(v or 0.0), 0.0)

    def free_quote(self) -> float:
        return self._free(QUOTE)

    def free_base(self, symbol: str) -> float:
        base, _ = split_symbol(symbol)
        return self._free(base)

    # -- 마켓 정보 ----------------------------------------------------
    def _exchange_min_cost(self, symbol: str) -> float | None:
        m = self._market(symbol)
        limits = m.get("limits") if isinstance(m.get("limits"), dict) else {}
        cost = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}
        return _as_float(cost.get("min"))

    def _exchange_min_amount(self, symbol: str) -> float | None:
        m = self._market(symbol)
        limits = m.get("limits") if isinstance(m.get("limits"), dict) else {}
        amt = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
        return _as_float(amt.get("min"))

    def min_order_usdt(self, symbol: str) -> float:
        """거래소 최소 주문 금액과 HardLimits.MIN_ORDER_USDT 중 큰 값."""
        ex_min = self._exchange_min_cost(symbol)
        return max(ex_min or 0.0, HardLimits.MIN_ORDER_USDT)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        if not _is_finite_positive(amount):
            raise OrderValidationError(f"{symbol}: 수량이 유한한 양수가 아님: {amount!r}")
        self._load_markets()
        try:
            v = self._ex.amount_to_precision(symbol, amount)
        except Exception as e:
            raise self._wrap(e, "amount_to_precision") from e
        out = _as_float(v)
        if out is None:
            raise ExchangeError(f"{symbol}: 정밀도 변환 결과가 숫자가 아님: {v!r}")
        return out

    # -- 주문 ---------------------------------------------------------
    def _new_client_order_id(self) -> str:
        return "tb" + uuid.uuid4().hex[:30]

    def _lookup_by_client_id(self, symbol: str, cid: str) -> dict | None:
        """네트워크 오류 후 주문이 접수됐는지 clientOrderId로 확인. 없으면 None.

        조회 자체도 SPEC §4대로 ``_retry``(일시 오류 3회)를 탄다. 일시 오류 한 번으로 주문 접수 여부를
        모른 채 포기하면 체결된 BUY가 추적 없이 남을 수 있기 때문이다.
        """
        not_found = _order_not_found_types()

        def _lookup() -> dict | None:
            try:
                o = self._ex.fetch_order(None, symbol, {"origClientOrderId": cid})
            except not_found:  # type: ignore[misc]
                return None
            return o if isinstance(o, dict) else None

        return self._retry("fetch_order(clientOrderId)", _lookup)

    def _submit(self, symbol: str, side: str, amount: float | None, params: dict) -> dict:
        """create_order를 중복 주문 없이 최대 3회 시도한다."""
        retryable = _retryable_types()
        ratelimit = _ratelimit_types()
        cid = self._new_client_order_id()
        params = dict(params, newClientOrderId=cid)
        what = f"create_order {symbol} {side}"
        last: BaseException | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                order = self._ex.create_order(symbol, "market", side, amount, None, params)
                if not isinstance(order, dict):
                    raise ExchangeError(f"{what}: 주문 응답 형식 오류")
                return order
            except ExchangeError:
                raise
            except retryable as e:  # type: ignore[misc]
                last = e
                msg = _scrub_exception(e, self._settings)
                log.warning("%s 실패(%d/%d) %s: %s", what, attempt, RETRY_ATTEMPTS, type(e).__name__, msg)
                if not isinstance(e, ratelimit):
                    found = self._lookup_by_client_id(symbol, cid)
                    if found is not None:
                        log.warning("%s: 네트워크 오류였지만 주문 %s 은 접수됨", what, found.get("id"))
                        return found
                if attempt >= RETRY_ATTEMPTS:
                    break
                self._sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
            except Exception as e:
                wrapped = self._wrap(e, what)
                log.error("%s", wrapped)
                raise wrapped from e
        assert last is not None
        wrapped = self._wrap(last, f"{what} ({RETRY_ATTEMPTS}회 재시도 후)")
        log.error("%s", wrapped)
        raise wrapped from last

    def _settle(self, order: dict, symbol: str) -> dict:
        """체결 정보가 응답에 없으면 fetch_order로 보강한다."""
        filled = _as_float(order.get("filled"))
        status = order.get("status")
        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        if filled and filled > 0 and (status in ("closed", "filled") or info.get("fills")):
            return order
        oid = order.get("id")
        if not oid:
            return order
        for _ in range(RETRY_ATTEMPTS):
            fresh = self._retry("fetch_order", self._ex.fetch_order, oid, symbol)
            if isinstance(fresh, dict):
                f = _as_float(fresh.get("filled"))
                if f and f > 0 and fresh.get("status") in ("closed", "filled", "canceled", None):
                    return fresh
                order = fresh
            self._sleep(RETRY_BASE_DELAY_S)
        return order

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        settings = self._settings
        assert settings is not None
        _validate_request(OrderRequest(symbol=symbol, side="buy", quote_amount=quote_amount), settings.symbols, ())
        quote_amount = float(quote_amount)
        min_cost = self.min_order_usdt(symbol)
        if quote_amount < min_cost:
            raise OrderValidationError(f"{symbol} buy: {quote_amount:.2f} USDT < 최소 주문 {min_cost:.2f} USDT")
        free = self.free_quote()
        if quote_amount > free:
            raise InsufficientFunds(f"{symbol} buy: 필요 {quote_amount:.2f} USDT > 가용 {free:.2f} USDT")
        # Binance quoteOrderQty는 quote 정밀도(소수 2~8자리). 소수 2자리로 내림해 거절을 피한다.
        q = math.floor(quote_amount * 100.0) / 100.0
        if q < min_cost:
            raise OrderValidationError(f"{symbol} buy: 정밀도 적용 후 {q:.2f} USDT < 최소 주문 {min_cost:.2f} USDT")
        order = self._submit(symbol, "buy", None, {"quoteOrderQty": f"{q:.2f}"})
        order = self._settle(order, symbol)
        fill = _parse_fill(order, symbol, "buy", now_ms=self._clock_ms())
        log.info("체결 BUY %s %.8f @ %.4f (%.2f USDT, fee %.4f) id=%s", symbol, fill.base_amount, fill.price, fill.quote_amount, fill.fee_quote, fill.order_id)
        return fill

    def market_sell(self, symbol: str, base_amount: float) -> Fill:
        settings = self._settings
        assert settings is not None
        free_base = self.free_base(symbol)
        held = (symbol,) if free_base > 0 else ()
        _validate_request(OrderRequest(symbol=symbol, side="sell", base_amount=base_amount), settings.symbols, held)
        amount = self.amount_to_precision(symbol, float(base_amount))
        if amount <= 0:
            raise OrderValidationError(f"{symbol} sell: 정밀도 적용 후 수량이 0")
        min_amt = self._exchange_min_amount(symbol)
        if min_amt is not None and amount < min_amt:
            raise OrderValidationError(f"{symbol} sell: 수량 {amount} < 거래소 최소 {min_amt}")
        if amount > free_base * (1 + 1e-9):
            raise InsufficientFunds(f"{symbol} sell: 필요 {amount} > 가용 {free_base}")
        min_cost = self._exchange_min_cost(symbol)
        if min_cost is not None:
            # 최소 주문 금액 사전 검사. 티커 조회 실패는 청산을 막지 않는다(fail-open):
            # 거래소가 MIN_NOTIONAL을 자체 검증하므로 주문을 보내고 거절은 거래소에 맡긴다.
            try:
                ticker = self.fetch_ticker(symbol)
            except ExchangeError as e:
                log.warning("%s sell: 최소 금액 사전 검사용 티커 조회 실패, 검사 생략하고 진행: %s", symbol, e)
            else:
                ref = ticker.bid if ticker.bid else ticker.last
                if amount * ref < min_cost:
                    raise OrderValidationError(f"{symbol} sell: 예상 금액 {amount * ref:.2f} USDT < 거래소 최소 {min_cost:.2f} USDT")
        order = self._submit(symbol, "sell", amount, {})
        order = self._settle(order, symbol)
        fill = _parse_fill(order, symbol, "sell", now_ms=self._clock_ms())
        log.info("체결 SELL %s %.8f @ %.4f (%.2f USDT, fee %.4f) id=%s", symbol, fill.base_amount, fill.price, fill.quote_amount, fill.fee_quote, fill.order_id)
        return fill


# ---------------------------------------------------------------- 페이퍼 브로커
class PaperBroker:
    """가상 체결. 체결가 = ask*(1+slippage)(buy) / bid*(1-slippage)(sell). 수수료는 quote로 차감.

    잔고는 메모리(``balances``)에만 있고 영속 상태는 portfolio가 관리한다(``set_balances``로 복원).
    Fill.quote_amount는 체결 금액(gross), fee_quote는 별도 차감된 수수료다.
    """

    mode = "paper"

    def __init__(
        self,
        market: MarketData,
        start_balance: float,
        fee_pct: float = HardLimits.FEE_PCT,
        slippage_pct: float = HardLimits.SLIPPAGE_PCT,
        balances: dict | None = None,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not (isinstance(start_balance, (int, float)) and math.isfinite(start_balance) and start_balance >= 0):
            raise ValueError(f"start_balance는 유한한 0 이상 숫자여야 함: {start_balance!r}")
        if not (0 <= fee_pct < 1) or not (0 <= slippage_pct < 1):
            raise ValueError("fee_pct/slippage_pct는 [0, 1) 범위여야 함")
        self.market = market
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self._clock_ms = clock_ms if clock_ms is not None else _now_ms
        self._order_seq = 0
        self.balances: dict[str, float] = {}
        self.set_balances(balances if balances is not None else {QUOTE: float(start_balance)})

    def __repr__(self) -> str:
        return f"PaperBroker(balances={self.balances!r}, fee={self.fee_pct}, slippage={self.slippage_pct})"

    def set_balances(self, balances: dict) -> None:
        """잔고를 통째로 교체한다(portfolio 상태 복원용). 음수/비유한 값은 거부."""
        out: dict[str, float] = {}
        for k, v in dict(balances).items():
            fv = _as_float(v)
            if fv is None or fv < 0:
                raise ValueError(f"잔고 값이 유효하지 않음: {k}={v!r}")
            out[str(k).upper()] = fv
        self.balances = out

    def free_quote(self) -> float:
        return float(self.balances.get(QUOTE, 0.0))

    def free_base(self, symbol: str) -> float:
        base, _ = split_symbol(symbol)
        return float(self.balances.get(base.upper(), 0.0))

    def min_order_usdt(self, symbol: str) -> float:
        return HardLimits.MIN_ORDER_USDT

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        if not _is_finite_positive(amount):
            raise OrderValidationError(f"{symbol}: 수량이 유한한 양수가 아님: {amount!r}")
        fn = getattr(self.market, "amount_to_precision", None)
        if callable(fn):
            try:
                v = _as_float(fn(symbol, amount))
                if v is not None:
                    return v
            except Exception as e:
                log.debug("market.amount_to_precision 실패, 로컬 반올림 사용: %s", e)
        factor = 10 ** PAPER_AMOUNT_DECIMALS
        return math.floor(float(amount) * factor) / factor

    def _next_id(self) -> str:
        self._order_seq += 1
        return f"paper-{self._order_seq}"

    def _ticker(self, symbol: str) -> Ticker:
        try:
            return self.market.fetch_ticker(symbol)
        except ExchangeError:
            raise
        except Exception as e:
            raise ExchangeError(f"{symbol}: 티커 조회 실패: {type(e).__name__}: {e}") from e

    def market_buy(self, symbol: str, quote_amount: float) -> Fill:
        base, quote = split_symbol(symbol)
        if quote.upper() != QUOTE:
            raise OrderValidationError(f"{symbol}: {QUOTE} 페어만 지원")
        if not _is_finite_positive(quote_amount):
            raise OrderValidationError(f"{symbol} buy: quote_amount는 유한한 양수여야 함: {quote_amount!r}")
        quote_amount = float(quote_amount)
        if quote_amount < self.min_order_usdt(symbol):
            raise OrderValidationError(f"{symbol} buy: {quote_amount:.2f} USDT < 최소 주문 {self.min_order_usdt(symbol):.2f} USDT")
        ticker = self._ticker(symbol)
        ref = ticker.ask if _is_finite_positive(ticker.ask) else ticker.last
        if not _is_finite_positive(ref):
            raise ExchangeError(f"{symbol}: 매도호가/현재가가 유효하지 않음")
        price = float(ref) * (1.0 + self.slippage_pct)
        fee = quote_amount * self.fee_pct
        total = quote_amount + fee
        free = self.free_quote()
        if total > free + 1e-9:
            raise InsufficientFunds(f"{symbol} buy: 필요 {total:.2f} USDT (수수료 포함) > 가용 {free:.2f} USDT")
        base_amount = self.amount_to_precision(symbol, quote_amount / price)
        if base_amount <= 0:
            raise OrderValidationError(f"{symbol} buy: 수량이 0으로 반올림됨")
        self.balances[QUOTE] = free - total
        self.balances[base.upper()] = self.free_base(symbol) + base_amount
        fill = Fill(
            order_id=self._next_id(), symbol=symbol, side="buy", base_amount=base_amount, price=price,
            quote_amount=quote_amount, fee_quote=fee, fee_base=0.0, ts=self._clock_ms(),
            raw={"paper": True, "ref_price": float(ref), "slippage_pct": self.slippage_pct},
        )
        log.info("[paper] BUY %s %.8f @ %.4f (%.2f USDT, fee %.4f)", symbol, base_amount, price, quote_amount, fee)
        return fill

    def market_sell(self, symbol: str, base_amount: float) -> Fill:
        base, quote = split_symbol(symbol)
        if quote.upper() != QUOTE:
            raise OrderValidationError(f"{symbol}: {QUOTE} 페어만 지원")
        if not _is_finite_positive(base_amount):
            raise OrderValidationError(f"{symbol} sell: base_amount는 유한한 양수여야 함: {base_amount!r}")
        amount = self.amount_to_precision(symbol, float(base_amount))
        if amount <= 0:
            raise OrderValidationError(f"{symbol} sell: 정밀도 적용 후 수량이 0")
        held = self.free_base(symbol)
        if amount > held + 1e-12:
            raise InsufficientFunds(f"{symbol} sell: 필요 {amount} > 보유 {held}")
        ticker = self._ticker(symbol)
        ref = ticker.bid if _is_finite_positive(ticker.bid) else ticker.last
        if not _is_finite_positive(ref):
            raise ExchangeError(f"{symbol}: 매수호가/현재가가 유효하지 않음")
        price = float(ref) * (1.0 - self.slippage_pct)
        quote_amount = amount * price
        fee = quote_amount * self.fee_pct
        self.balances[base.upper()] = max(held - amount, 0.0)
        self.balances[QUOTE] = self.free_quote() + quote_amount - fee
        fill = Fill(
            order_id=self._next_id(), symbol=symbol, side="sell", base_amount=amount, price=price,
            quote_amount=quote_amount, fee_quote=fee, fee_base=0.0, ts=self._clock_ms(),
            raw={"paper": True, "ref_price": float(ref), "slippage_pct": self.slippage_pct},
        )
        log.info("[paper] SELL %s %.8f @ %.4f (%.2f USDT, fee %.4f)", symbol, amount, price, quote_amount, fee)
        return fill


__all__ = [
    "ExchangeError", "InsufficientFunds", "OrderValidationError",
    "MarketData", "Broker", "PaperBroker", "CcxtBroker", "CcxtMarketData",
    "split_symbol",
]
