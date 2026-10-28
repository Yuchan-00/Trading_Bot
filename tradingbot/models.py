"""공용 데이터 모델 (pydantic v2).

모든 모델은 ``extra="forbid"``: 오타 필드가 조용히 무시되지 않는다.
시각은 epoch ms(``ts``) 정수, 금액은 USDT float, 비율은 소수(0.02 = 2%).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ExitReason = Literal[
    "stop_loss", "take_profit", "time_stop", "head_sell", "kill_switch", "manual", "partial",
]
Side = Literal["buy", "sell"]
EventKind = Literal[
    "entry", "exit", "partial_exit", "order_rejected", "kill_switch", "budget_exceeded",
    "daily_summary", "error", "startup", "shutdown", "llm_paused",
    "halt", "halt_lifted", "heartbeat",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 시장 데이터
class Candle(_Model):
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Ticker(_Model):
    symbol: str
    last: float
    bid: float | None = None
    ask: float | None = None
    quote_volume_24h: float | None = None
    change_24h_pct: float | None = None
    ts: int


class OrderBookSummary(_Model):
    symbol: str
    best_bid: float
    best_ask: float
    spread_pct: float
    bid_depth_usdt_1pct: float
    ask_depth_usdt_1pct: float
    ts: int


class FuturesContext(_Model):
    """선물 공개 데이터. 현물 봇은 거래하지 않고 심리 지표로만 읽는다. 없으면 None."""

    symbol: str
    funding_rate: float | None = None
    open_interest_usdt: float | None = None
    long_short_ratio: float | None = None
    ts: int


class NewsItem(_Model):
    title: str
    source: str
    url: str
    published_ts: int | None = None
    symbols: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    tier: Literal[1, 2, 3] = 3


class IndicatorSet(_Model):
    """마지막 완료 봉 기준 지표. 워밍업이 부족한 필드는 None."""

    symbol: str
    timeframe: str
    ts: int
    close: float
    ema_20: float | None = None
    ema_50: float | None = None
    ema_200: float | None = None
    sma_20: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    atr_14: float | None = None
    atr_pct: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_width_pct: float | None = None
    adx_14: float | None = None
    vwap_session: float | None = None
    volume_ratio_20: float | None = None
    return_1: float | None = None
    return_4: float | None = None
    return_24: float | None = None
    high_20: float | None = None
    low_20: float | None = None
    warmup_ok: bool = False


class MarketSnapshot(_Model):
    symbol: str
    ts: int
    ticker: Ticker
    orderbook: OrderBookSummary | None = None
    indicators: dict[str, IndicatorSet] = Field(default_factory=dict)
    futures: FuturesContext | None = None
    news: list[NewsItem] = Field(default_factory=list)
    candles_15m_tail: list[Candle] = Field(default_factory=list)


class QualityReport(_Model):
    symbol: str
    ok: bool
    reasons: list[str] = Field(default_factory=list)
    stale_seconds: int | None = None


# ---------------------------------------------------------------- 포지션/상태
class Position(_Model):
    symbol: str
    base_amount: float
    entry_price: float
    quote_spent: float
    fee_quote: float = 0.0
    stop_loss_price: float
    take_profit_price: float
    opened_cycle: int
    opened_ts: int
    thesis: str = ""
    confidence: float = 0.0
    entry_order_id: str | None = None
    last_mark: float | None = None


class ClosedTrade(_Model):
    symbol: str
    base_amount: float
    entry_price: float
    exit_price: float
    quote_spent: float
    quote_received: float
    fee_quote_total: float = 0.0
    pnl_quote: float
    pnl_pct: float
    reason: ExitReason
    opened_cycle: int
    closed_cycle: int
    opened_ts: int
    closed_ts: int
    thesis: str = ""
    confidence: float = 0.0
    exit_order_id: str | None = None


class DailyStats(_Model):
    date: str
    start_equity: float
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    llm_cost_usd: float = 0.0


class BotState(_Model):
    version: int = 1
    mode: str
    cycle: int = 0
    positions: dict[str, Position] = Field(default_factory=dict)
    closed_trades: list[ClosedTrade] = Field(default_factory=list)
    paper_quote_balance: float = 0.0
    peak_equity: float = 0.0
    daily: DailyStats
    cooldown_until_cycle: dict[str, int] = Field(default_factory=dict)
    loss_cooldown_until_cycle: dict[str, int] = Field(default_factory=dict)
    kill_switch: bool = False
    kill_reason: str | None = None
    llm_paused_until_ts: int | None = None
    llm_pause_reason: str | None = None
    last_cycle_ts: int | None = None


# ---------------------------------------------------------------- 주문/체결
class Fill(_Model):
    order_id: str
    symbol: str
    side: Side
    base_amount: float
    price: float
    quote_amount: float
    fee_quote: float = 0.0
    fee_base: float = 0.0
    ts: int
    raw: dict = Field(default_factory=dict)


class OrderRequest(_Model):
    symbol: str
    side: Side
    quote_amount: float | None = None
    base_amount: float | None = None


# ---------------------------------------------------------------- 이벤트
class TradeEvent(_Model):
    kind: EventKind
    ts: int
    mode: str
    symbol: str | None = None
    side: str | None = None
    base_amount: float | None = None
    price: float | None = None
    quote_amount: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    confidence: float | None = None
    thesis: str | None = None
    reason: str | None = None
    pnl_quote: float | None = None
    pnl_pct: float | None = None
    equity: float | None = None
    extra: dict = Field(default_factory=dict)
