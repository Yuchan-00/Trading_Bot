# SPEC — 모듈 계약 (v1: 기반 모듈)

이 문서는 구현자가 그대로 따라야 하는 계약이다. 함수 시그니처·예외·불변식은 여기 적힌 대로 구현하고, 바꿔야 하면 이 문서를 먼저 고친다. 에이전트 역할(roster)·프롬프트·스킬 배정은 v2(`SPEC.md` §9, `DESIGN.md`)에서 확정한다.

공통 규칙
- Python ≥ 3.11, 표준 라이브러리 + `pydantic>=2`, `ccxt`, `anthropic>=2.3`, `feedparser`, `fastapi`, `numpy/pandas`(indicators/backtest만). 3.12+ 전용 문법 금지.
- 모든 모듈은 **네트워크·LLM 없이 import 가능**해야 하고, 테스트는 전부 오프라인(가짜 거래소·mock 백엔드·가짜 텔레그램 전송).
- 금액은 `float` USDT, 수량은 `float` base. 비율은 소수(0.02 = 2%). 시각은 UTC `datetime`(aware) 또는 epoch ms `int`(캔들).
- 비밀값(API 키·토큰)은 `config.mask_secret()`을 거치지 않고는 어떤 로그·예외 메시지·리포트·텔레그램·LLM 입력에도 들어가면 안 된다.
- 로깅은 `logging.getLogger("tradingbot.<module>")`. 한국어 메시지 허용.
- 에러 정책: 거래소/LLM/알림 오류는 **사이클을 죽이지 않는다**(HOLD·건너뜀·재시도). 상태 저장 실패와 손상 상태(testnet/live)는 **멈춘다**.

디렉터리
```
tradingbot/            패키지 (아래 §1~§13)
prompts/<role>.md      역할별 시스템 프롬프트 (v2)
skills/<name>/SKILL.md 스킬 (v2)
tests/                 pytest
logs/                  런타임 로그·보고서 (gitignore)
```

---

## 1. `tradingbot/config.py`

```python
class ConfigError(Exception): ...

def mask_secret(value: str | None, keep: int = 2) -> str
    # "" / None -> "(없음)"; 길이<=keep*2 -> "**"; else f"{v[:keep]}…(len={len(v)})"

class HardLimits:   # 상수 전용 클래스. 인스턴스/환경변수로 바꿀 수 없다. 테스트가 값을 고정한다.
    MAX_POSITION_PCT = 0.10          # 종목당 자본 대비 최대 비중
    MAX_POSITIONS = 3
    MAX_STOP_LOSS_PCT = 0.02         # 진입가 대비 손절 거리 상한
    MAX_TAKE_PROFIT_PCT = 0.04
    DAILY_LOSS_LIMIT_PCT = 0.03      # 당일 시작자본 대비 손실 -> 신규 진입 중단(당일)
    MAX_CONSECUTIVE_LOSSES = 3       # 연속 손실 -> 신규 진입 중단(당일)
    MIN_CONFIDENCE = 0.65
    COOLDOWN_CYCLES = 4              # 같은 종목 청산 후 재진입 금지 사이클 수
    REVENGE_WINDOW_CYCLES = 8        # 손실 청산 후 같은 종목 재진입 금지 사이클 수
    TIME_STOP_CYCLES = 96            # 보유 사이클 상한 (15분*96 = 24h)
    DRAWDOWN_CAUTION = 0.03          # 최고자본 대비 낙폭 -> 사이즈 50%
    DRAWDOWN_BLOCK = 0.045           # -> 신규 진입 금지
    DRAWDOWN_STOP_ALL = 0.06         # -> STOP_ALL (킬스위치, 전량 청산은 하지 않음, 사람이 해제)
    RISK_PER_TRADE_PCT = 0.0015      # 거래당 자본 대비 최대 손실(수수료 포함) 0.15%
    MIN_NET_RR = 1.5                 # 비용 차감 후 최소 손익비 (손절만 조여서 맞춤)
    FEE_PCT = 0.001                  # 편도 수수료 가정
    SLIPPAGE_PCT = 0.0005
    MIN_ORDER_USDT = 10.0
    ORDER_TYPES = ("market",)
    MARKET_TYPES = ("spot",)
    ENTRY_SIDES = ("buy",)           # 현물 롱만

@dataclass(frozen=True)
class Settings:
    mode: Literal["paper", "testnet", "live"]
    confirm_live: bool
    symbols: tuple[str, ...]                 # "BTC/USDT" 형식, 중복 제거, 대문자
    cycle_minutes: int                       # 기본 15
    paper_start_balance: float               # 기본 10000
    max_candidates: int                      # 기본 2
    agent_backend: Literal["anthropic", "mock"]
    anthropic_api_key: str | None
    model_analysis: str                      # 기본 claude-haiku-5-5
    model_research: str                      # 기본 claude-sonnet-5-5
    model_head: str                          # 기본 claude-opus-5-5
    llm_daily_budget_usd: float              # 기본 10
    llm_monthly_budget_usd: float            # 기본 200
    binance_api_key: str | None              # mode에 맞는 키 (testnet/live), paper면 None
    binance_api_secret: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    dashboard_host: str                      # 기본 127.0.0.1
    dashboard_port: int                      # 기본 8787
    data_dir: Path                           # 기본 프로젝트 루트 (state.json, logs/, control/)
    news_feeds: tuple[str, ...]              # RSS URL 목록 (기본: CoinDesk, CoinTelegraph, Decrypt 등 공개 RSS)

    @property
    def is_live(self) -> bool
    def model_for(self, tier: Literal["analysis", "research", "head"]) -> str
    def redacted(self) -> dict               # 로그용: 비밀은 mask_secret 적용

def load_settings(env: Mapping[str, str] | None = None, *, dotenv_path: Path | None = None) -> Settings
```
규칙
- `env`가 None이면 `os.environ` + `.env`(dotenv, 존재 시; 기존 환경변수가 우선)를 읽는다. 테스트는 `env` 딕셔너리를 넘긴다.
- `MODE=live`인데 `CONFIRM_LIVE != "YES"` → `ConfigError`. live/testnet인데 해당 키가 비어 있으면 `ConfigError`.
- `AGENT_BACKEND=anthropic`인데 키가 비어 있으면 `ConfigError`(mock이면 불필요).
- 모델 문자열은 `claude-`로 시작해야 한다. 날짜 접미사 금지(검증만, 변환 없음).
- `SYMBOLS` 파싱: 쉼표 분리, 공백 제거, `BASE/QUOTE` 형식 검증, QUOTE는 USDT만 허용.
- 예산 ≤ 0 → `ConfigError`.

## 2. `tradingbot/models.py` (pydantic v2, `model_config = ConfigDict(extra="forbid")`)

```python
class Candle(BaseModel): ts: int; open: float; high: float; low: float; close: float; volume: float
class Ticker(BaseModel): symbol: str; last: float; bid: float | None; ask: float | None; quote_volume_24h: float | None; change_24h_pct: float | None; ts: int
class OrderBookSummary(BaseModel): symbol: str; best_bid: float; best_ask: float; spread_pct: float; bid_depth_usdt_1pct: float; ask_depth_usdt_1pct: float; ts: int
class FuturesContext(BaseModel): symbol: str; funding_rate: float | None; open_interest_usdt: float | None; long_short_ratio: float | None; ts: int   # 선물 공개 데이터(심리 지표용). 없으면 None.
class NewsItem(BaseModel): title: str; source: str; url: str; published_ts: int | None; symbols: list[str]; red_flags: list[str]; tier: Literal[1,2,3]
class IndicatorSet(BaseModel):   # 모두 "마지막 완료 봉" 기준. 워밍업 부족 필드는 None.
    symbol: str; timeframe: str; ts: int; close: float
    ema_20: float | None; ema_50: float | None; ema_200: float | None; sma_20: float | None
    rsi_14: float | None; macd: float | None; macd_signal: float | None; macd_hist: float | None
    atr_14: float | None; atr_pct: float | None          # atr_14/close
    bb_upper: float | None; bb_lower: float | None; bb_width_pct: float | None
    adx_14: float | None; vwap_session: float | None
    volume_ratio_20: float | None                        # 최근 봉 거래량 / 20봉 평균
    return_1: float | None; return_4: float | None; return_24: float | None   # n봉 수익률
    high_20: float | None; low_20: float | None
    warmup_ok: bool                                      # 모든 필수 지표 계산 가능 여부
class MarketSnapshot(BaseModel):
    symbol: str; ts: int; ticker: Ticker; orderbook: OrderBookSummary | None
    indicators: dict[str, IndicatorSet]                  # key: timeframe ("15m","1h","4h","1d")
    futures: FuturesContext | None; news: list[NewsItem]
    candles_15m_tail: list[Candle]                       # 최근 48봉 (LLM 입력용)
class QualityReport(BaseModel): symbol: str; ok: bool; reasons: list[str]; stale_seconds: int | None

class Position(BaseModel):
    symbol: str; base_amount: float; entry_price: float; quote_spent: float; fee_quote: float
    stop_loss_price: float; take_profit_price: float; opened_cycle: int; opened_ts: int
    thesis: str; confidence: float; entry_order_id: str | None; last_mark: float | None
class ClosedTrade(BaseModel):
    symbol: str; base_amount: float; entry_price: float; exit_price: float; quote_spent: float; quote_received: float
    fee_quote_total: float; pnl_quote: float; pnl_pct: float; reason: ExitReason; opened_cycle: int; closed_cycle: int
    opened_ts: int; closed_ts: int; thesis: str; confidence: float; exit_order_id: str | None
ExitReason = Literal["stop_loss","take_profit","time_stop","head_sell","kill_switch","manual","partial"]
class DailyStats(BaseModel): date: str; start_equity: float; realized_pnl: float; trades: int; wins: int; losses: int; consecutive_losses: int; llm_cost_usd: float
class BotState(BaseModel):
    version: int = 1; mode: str; cycle: int; positions: dict[str, Position]; closed_trades: list[ClosedTrade]
    paper_quote_balance: float; peak_equity: float; daily: DailyStats
    cooldown_until_cycle: dict[str, int]                 # symbol -> cycle
    loss_cooldown_until_cycle: dict[str, int]            # 복수매매 차단
    kill_switch: bool; kill_reason: str | None
    llm_paused_until_ts: int | None; llm_pause_reason: str | None
    last_cycle_ts: int | None

class Fill(BaseModel): order_id: str; symbol: str; side: Literal["buy","sell"]; base_amount: float; price: float; quote_amount: float; fee_quote: float; fee_base: float; ts: int; raw: dict = {}
class OrderRequest(BaseModel): symbol: str; side: Literal["buy","sell"]; quote_amount: float | None = None; base_amount: float | None = None   # buy는 quote_amount, sell은 base_amount

class TradeEvent(BaseModel):   # notify/reports 공용
    kind: Literal["entry","exit","partial_exit","order_rejected","kill_switch","budget_exceeded","daily_summary","error","startup","shutdown","llm_paused"]
    ts: int; mode: str; symbol: str | None = None; side: str | None = None; base_amount: float | None = None
    price: float | None = None; quote_amount: float | None = None; stop_loss_price: float | None = None; take_profit_price: float | None = None
    confidence: float | None = None; thesis: str | None = None; reason: str | None = None; pnl_quote: float | None = None; pnl_pct: float | None = None
    equity: float | None = None; extra: dict = {}
```

## 3. `tradingbot/risk.py` — 순수 함수. 상태를 바꾸지 않는다. 모든 반환은 "조이기만" 한다.

```python
class RiskVerdict(BaseModel): allowed: bool; reasons: list[str]; size_multiplier: float   # 0~1

def global_gates(state: BotState, equity: float, now_ts: int, llm_budget_ok: bool) -> RiskVerdict
    # 순서대로 검사: kill_switch -> 낙폭 사다리(STOP_ALL이면 kill 권고, BLOCK이면 allowed False, CAUTION이면 0.5) -> 당일 손실 한도 -> 연속 손실 -> 포지션 수 -> LLM 예산(False면 allowed False, reason "llm_budget")
    # allowed False이면 신규 진입만 막는다(청산은 항상 허용). reasons는 사람이 읽을 한국어 문자열.

def symbol_gates(state: BotState, symbol: str) -> RiskVerdict
    # 보유 중이면 "already_held", 쿨다운/복수매매 창이면 막음, 화이트리스트 밖이면 막음(settings.symbols는 호출자가 넘긴 state가 아니라 별도 인자로 받아도 됨: symbol_gates(state, symbol, whitelist))

def default_stops(entry_price: float, atr_1h: float | None, atr_mult: float = 2.0) -> tuple[float, float]
    # 손절거리 = min(atr_mult*atr_1h/entry, MAX_STOP_LOSS_PCT); atr 없으면 MAX_STOP_LOSS_PCT*0.5
    # 익절거리 = min(2*손절거리, MAX_TAKE_PROFIT_PCT). 반환 (stop_loss_price, take_profit_price)

def clamp_stops(entry_price: float, stop_loss_price: float, take_profit_price: float) -> tuple[float, float]
    # 손절은 진입가 아래, 거리 <= MAX_STOP_LOSS_PCT (초과 시 상한으로 조임), 익절은 진입가 위, 거리 <= MAX_TAKE_PROFIT_PCT

def enforce_net_rr(entry_price: float, stop_loss_price: float, take_profit_price: float) -> tuple[float, float, str]
    # 순손익비 = (tp거리 - 왕복비용) / (sl거리 + 왕복비용) ; 왕복비용 = 2*(FEE_PCT+SLIPPAGE_PCT)
    # < MIN_NET_RR 이면 손절만 조여서(익절은 그대로) 맞춘다. 조여서도 불가능(손절거리 <= 0.002)이면 ("", "", "rr_impossible") 대신 ValueError 대신 -> 반환 메시지에 "rr_impossible"을 담고 호출자가 HOLD. 반환 (sl, tp, note)

def size_position(equity: float, entry_price: float, stop_loss_price: float, confidence: float, size_multiplier: float, free_quote: float) -> tuple[float, list[str]]
    # 1) 위험 기반: quote = equity*RISK_PER_TRADE_PCT / (sl거리 + 왕복비용)
    # 2) 상한: equity*MAX_POSITION_PCT
    # 3) 확신도 티어: conf<0.65 -> 0 ; 0.65~0.75 -> 40% ; 0.75~0.85 -> 70% ; >=0.85 -> 100%  (상한 대비 비율)
    # 4) size_multiplier(주의 상태) 곱, free_quote 이하, MIN_ORDER_USDT 미만이면 0
    # 반환 (quote_amount, notes)

def validate_order_request(req: OrderRequest, whitelist: tuple[str, ...], held: Iterable[str]) -> None
    # buy: whitelist 안의 종목만, quote_amount>0 유한. sell: held 또는 whitelist, base_amount>0 유한. 위반 시 ValueError.

def check_exits(position: Position, mark_price: float, current_cycle: int) -> ExitReason | None
    # mark<=sl -> "stop_loss"; mark>=tp -> "take_profit"; current_cycle-opened_cycle>=TIME_STOP_CYCLES -> "time_stop"; else None

def drawdown_level(equity: float, peak_equity: float) -> Literal["normal","caution","block","stop_all"]
```
불변식(테스트로 고정): 어떤 입력을 넣어도 손절거리 ≤ 2%, 익절거리 ≤ 4%, 비중 ≤ 10%, 확신도 < 0.65면 수량 0, 포지션 3개면 신규 0, 당일 −3% 이후 신규 0, 킬스위치면 신규 0, 비용 반영 손익비 ≥ 1.5(불가능하면 HOLD).

## 4. `tradingbot/exchange.py`

```python
class ExchangeError(Exception): ...
class InsufficientFunds(ExchangeError): ...

class MarketData(Protocol):
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]      # 완료 봉만 (마지막 미완 봉 제거)
    def fetch_ticker(self, symbol: str) -> Ticker
    def fetch_orderbook_summary(self, symbol: str, depth: int = 50) -> OrderBookSummary
    def fetch_futures_context(self, symbol: str) -> FuturesContext | None               # 실패/미지원이면 None (예외 금지)
    def server_time_ms(self) -> int

class Broker(Protocol):
    mode: str
    def free_quote(self) -> float
    def free_base(self, symbol: str) -> float
    def market_buy(self, symbol: str, quote_amount: float) -> Fill
    def market_sell(self, symbol: str, base_amount: float) -> Fill
    def min_order_usdt(self, symbol: str) -> float
    def amount_to_precision(self, symbol: str, amount: float) -> float

class PaperBroker(Broker):
    def __init__(self, market: MarketData, start_balance: float, fee_pct: float = HardLimits.FEE_PCT, slippage_pct: float = HardLimits.SLIPPAGE_PCT, balances: dict | None = None)
    # 체결가 = ticker.ask*(1+slippage) (buy) / bid*(1-slippage) (sell); 수수료는 quote로 차감; 잔고는 메모리(상태는 portfolio가 저장). set_balances()/balances 노출.

class CcxtBroker(Broker, MarketData):
    def __init__(self, settings: Settings, *, exchange: Any | None = None)   # 테스트는 가짜 ccxt 객체 주입
    # binance spot. testnet: exchange.set_sandbox_mode(True). options: {"adjustForTimeDifference": True, "defaultType": "spot"}
    # 모든 ccxt 호출은 _retry(3회, 지수 백오프, NetworkError/RateLimitExceeded/ExchangeNotAvailable만 재시도)
    # 주문 전: validate_order_request, 수량 정밀도 변환, 최소 주문 검사, 유한성 검사. BUY는 quoteOrderQty 사용(createOrder params {"quoteOrderQty": ...}), SELL은 amount.
    # 체결 파싱 _parse_fill: fee가 base로 나가면 순수량(base - fee_base) 보고, fee_quote는 시세로 환산. fees 리스트가 비어 있지 않으면 fees, 아니면 fee, 아니면 info.fills.
    # 예외 메시지는 _scrub_exception(settings)로 키 제거 후 ExchangeError로 감싼다 (원 예외는 __cause__).
    # 선물 공개 데이터: ccxt binanceusdm 공개 엔드포인트(fetchFundingRate, fetchOpenInterest) 키 없이. 실패 시 None.

class CcxtMarketData(MarketData):   # paper 모드용 공개 데이터 전용 (키 없음)
```
`fetch_ohlcv`는 `since` 없이 `limit+1`을 받아 마지막(미완) 봉을 버린다. 캔들은 `ts` 오름차순, 중복 제거.

## 5. `tradingbot/indicators.py` — pandas 없이도 동작(순수 파이썬 허용, numpy 선택). 워밍업 선언.

```python
WARMUP: dict[str, int] = {"ema_20": 20, "ema_50": 50, "ema_200": 200, "rsi_14": 15, "macd": 35, "atr_14": 15, "bb": 20, "adx_14": 28, "volume_ratio_20": 21, "high_low_20": 20}
def compute(symbol: str, timeframe: str, candles: list[Candle]) -> IndicatorSet
    # 봉 수가 워밍업 미만인 지표는 None. warmup_ok = 핵심 지표(ema_20, rsi_14, atr_14, bb)가 모두 계산됨.
def ema(values, n) / sma / rsi(Wilder) / macd(12,26,9) / atr(Wilder 14) / bollinger(20, 2) / adx(14) / vwap(candles) -> list[float|None] 길이 동일
```
결정성: 같은 입력이면 같은 출력(부동소수 반올림 금지, 비교는 테스트에서 `pytest.approx`).

## 6. `tradingbot/quality.py`

```python
def check(symbol: str, candles_by_tf: dict[str, list[Candle]], ticker: Ticker, now_ms: int, cycle_minutes: int) -> QualityReport
    # reasons 예: "stale_15m:1800s"(마지막 15m 봉 종료가 now - 2*tf 보다 오래됨), "nan", "non_positive", "gap_15m:3"(봉 누락), "too_few_15m:120<220", "ticker_stale", "spread_too_wide:0.8%", "outlier_15m"(종가 변동 >30%/봉)
    # ok=False면 cycle은 그 종목의 LLM 호출을 건너뛴다(청산 검사는 함).
```

## 7. `tradingbot/news.py`

```python
RED_FLAGS = ("hack","exploit","halt","delist","lawsuit","sec ","ban","insolvency","bankrupt","depeg","outage","liquidat")
def fetch_news(feeds: Iterable[str], symbols: Iterable[str], *, fetcher: Callable[[str], str] | None = None, max_age_hours: int = 24, cache_path: Path | None = None) -> list[NewsItem]
    # fetcher(url)->raw xml (테스트 주입). feedparser로 파싱. 심볼 매칭: base 티커(BTC, ETH..)와 별칭(bitcoin, ethereum..) 대소문자 무시. tier: 1=coindesk/cointelegraph/theblock/bloomberg/reuters, 2=기타 알려진, 3=나머지.
    # 피드 실패는 로그 후 건너뜀. 결과는 published 내림차순, 심볼당 최대 15개. "빈 결과"는 "조용한 시장"이 아니라 "정보 없음"으로 표시되어야 한다(호출자가 news_available=False를 LLM에 전달).
```

## 8. `tradingbot/llm/`

### 8.1 `backend.py`
```python
class AgentResult(BaseModel, Generic[T]):   # 또는 dataclass
    role: str; ok: bool; output: T | None; error: str | None; model: str; input_tokens: int; cached_tokens: int; output_tokens: int; cost_usd: float; latency_s: float; request_id: str | None
class AgentBackend(Protocol):
    def complete(self, *, role: str, tier: Literal["analysis","research","head"], system: str, user_payload: dict, output_model: type[BaseModel], max_tokens: int, effort: Literal["low","medium","high"], timeout_s: float) -> AgentResult
class MockBackend(AgentBackend):
    def __init__(self, responses: Mapping[str, Callable[[dict], BaseModel] | BaseModel] | None = None, *, default_factory: Callable[[str, type[BaseModel], dict], BaseModel] | None = None)
    # role별 고정 출력 또는 팩토리. 기본은 output_model의 "보수적 HOLD" 예시(각 출력 모델은 classmethod `conservative_example()`를 제공해야 한다 — v2 agents에서 정의). 호출 기록 self.calls: list[dict] (role, payload) 보관. cost_usd=0.
```

### 8.2 `cost.py`
```python
PRICES_USD_PER_MTOK = {  # (input, cached_input, output). 2026-10 기준. 값은 config로 덮어쓸 수 있게 하되 기본값은 여기.
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
class BudgetExceeded(Exception): ...
class CostLedger:
    def __init__(self, path: Path, daily_budget_usd: float, monthly_budget_usd: float, *, now: Callable[[], datetime] | None = None)
    def estimate(self, model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float      # 미지 모델은 opus 가격으로 보수 추정
    def record(self, *, role: str, model: str, input_tokens: int, cached_tokens: int, output_tokens: int, cache_write_tokens: int = 0, request_id: str | None = None) -> float   # jsonl append, 반환 cost
    def spent_today(self) -> float ; def spent_month(self) -> float
    def check(self, projected_cost_usd: float = 0.0) -> None    # 초과(또는 초과 예상)면 BudgetExceeded
    def remaining_today(self) -> float
    # 파일 포맷: 한 줄 JSON {"ts","date","role","model","input_tokens","cached_tokens","cache_write_tokens","output_tokens","cost_usd","request_id"}. 시작 시 파일을 읽어 당일/당월 합계 복원.
```

### 8.3 `anthropic_backend.py`
```python
class AnthropicBackend(AgentBackend):
    def __init__(self, settings: Settings, ledger: CostLedger, *, client: anthropic.Anthropic | None = None, cache_ttl: Literal["5m","1h"] = "1h")
    # client 기본: anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=timeout 기본 60)
    # complete():
    #   1) ledger.check(projected = estimate(model, len(system)/4 + len(payload)/4, 0, max_tokens))  -> BudgetExceeded는 그대로 전파(호출자가 LLM 일시정지 처리)
    #   2) client.messages.parse(model=settings.model_for(tier), max_tokens=max_tokens,
    #        system=[{"type":"text","text":system,"cache_control":{"type":"ephemeral","ttl":cache_ttl}}],
    #        messages=[{"role":"user","content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True)}],
    #        output_format=output_model, output_config={"effort": effort}, timeout=timeout_s)
    #      thinking 파라미터는 보내지 않는다(5.5 계열은 기본 adaptive). tool_choice 강제 금지.
    #   3) stop_reason == "refusal" -> ok=False, error="refusal:<category>"; "max_tokens" -> ok=False, error="max_tokens"
    #   4) parsed_output이 None/검증 실패 -> ok=False, error="parse_error"
    #   5) usage 기록: ledger.record(input_tokens=usage.input_tokens, cached_tokens=usage.cache_read_input_tokens or 0, cache_write_tokens=usage.cache_creation_input_tokens or 0, output_tokens=usage.output_tokens)
    #   6) 예외 매핑(가장 구체적 먼저): AuthenticationError -> error="auth" (호출자는 LLM 60분 일시정지), RateLimitError -> "rate_limit", APITimeoutError -> "timeout", APIStatusError(>=500) -> "server", APIConnectionError -> "network", 기타 APIError -> "api:<type>". 모든 error 문자열은 mask_secret로 키 제거.
    #   7) 로그에는 role, model, 토큰 수, 비용, latency, request_id만. 프롬프트/응답 본문은 DEBUG에서도 전체가 아닌 앞 200자.
```
캐시 규칙: `system`은 역할별로 **바이트 단위로 고정**(날짜·사이클 번호·심볼 금지). 변동 데이터는 전부 `user_payload`. JSON 직렬화는 `sort_keys=True`.

## 9. `tradingbot/skills.py` (v2에서 배정 확정; 로더는 v1)

```python
class Skill(BaseModel): name: str; description: str; roles: list[str]; version: str; source: str | None; license: str | None; body: str; path: Path; approx_tokens: int
class SkillRegistry:
    def __init__(self, root: Path)                       # skills/<name>/SKILL.md 전부 로드. frontmatter(---yaml---): name, description, roles(list), version, source, license. 파싱은 표준 라이브러리로(간단 key: value / list).
    def for_role(self, role: str) -> list[Skill]          # roles에 role 포함, 이름순
    def render(self, role: str, max_tokens: int = 6000) -> str   # "## Skill: name\n<body>" 연결. 합계가 max_tokens를 넘으면 뒤 스킬부터 제외하고 WARNING 로그. 결정적.
```

## 10. `tradingbot/portfolio.py`

```python
class CorruptStateError(RuntimeError): ...
class Portfolio:
    def __init__(self, settings: Settings, path: Path)
    @classmethod
    def load(cls, settings: Settings, path: Path) -> "Portfolio"
        # 파일 없음 -> 새 상태(paper 잔고=settings.paper_start_balance). 파싱 실패: paper -> 파일을 state.json.corrupt.<ts>로 옮기고 새로 시작(ERROR 로그); testnet/live -> CorruptStateError. mode가 파일과 다르면 testnet/live는 CorruptStateError, paper는 새로 시작.
    def save(self) -> None            # tmp 파일 쓰기 -> os.replace, 실패 시 5회 재시도(0.1s), 최종 실패는 OSError 전파
    state: BotState
    def equity(self, marks: Mapping[str, float]) -> float        # paper: 잔고 + Σ base*mark ; testnet/live: 호출자가 free_quote를 넘겨 계산하도록 equity(marks, quote_balance=None)
    def ensure_day(self, now: datetime, equity: float) -> DailyStats | None   # 날짜가 바뀌면 전날 통계를 반환(보고용)하고 새 DailyStats 시작
    def open_position(self, fill: Fill, stop_loss_price: float, take_profit_price: float, thesis: str, confidence: float, cycle: int) -> Position   # paper 잔고 차감, 저장은 호출자
    def close_position(self, symbol: str, fill: Fill, reason: ExitReason, cycle: int) -> ClosedTrade
        # 부분 체결(fill.base_amount < position.base_amount*(1-0.005)) -> 체결분만 ClosedTrade(reason="partial" 또는 전달된 reason, 비례 원가), 잔여 Position 유지. 연속손실 카운트·당일 손익·쿨다운(COOLDOWN_CYCLES, 손실이면 REVENGE_WINDOW_CYCLES) 갱신. peak_equity는 호출자가 update_peak(equity)로 갱신.
    def update_peak(self, equity: float) -> None
    def set_kill(self, reason: str) -> None ; def clear_kill(self) -> None
    def pause_llm(self, until_ts: int, reason: str) -> None ; def llm_paused(self, now_ts: int) -> bool
```

## 11. `tradingbot/notify.py` — 모든 거래 이벤트는 텔레그램으로

```python
class Transport(Protocol):
    def send(self, text: str) -> None      # 실패 시 예외
class TelegramTransport(Transport):
    def __init__(self, token: str, chat_id: str, *, http_post: Callable[[str, dict], int] | None = None, timeout_s: float = 10)
    # https://api.telegram.org/bot<token>/sendMessage, parse_mode 없음(이스케이프 문제 회피), disable_web_page_preview. repr/str/로그에 토큰이 나오지 않는다(mask_secret).
class Notifier:
    def __init__(self, transport: Transport | None, *, failed_log: Path | None = None, retries: int = 3, async_send: bool = True)
    def notify(self, event: TradeEvent) -> None    # 포맷 후 큐에 넣고 즉시 반환(async_send). 워커 스레드가 전송, 실패 시 1s/3s/9s 재시도 후 failed_log(jsonl)에 기록. transport None이면 로그만.
    def flush(self, timeout_s: float = 5.0) -> None  # 종료 시 대기
    @staticmethod
    def format(event: TradeEvent) -> str
    # 한국어 포맷 예:
    # "🟢 진입 [paper] BTC/USDT\n수량 0.00481 @ 83,084.2 (400.4 USDT)\n손절 82,362.2 (-0.87%) / 익절 84,790.3 (+2.05%)\n확신도 0.72\n근거: ...(200자)"
    # "🔴 청산 [paper] BTC/USDT 손절\n0.00481 @ 82,300.0 → 손익 -3.77 USDT (-0.94%)\n자본 9,996.2"
    # 종류별 이모지: entry 🟢, exit 🔴(손실)/🔵(이익), partial_exit 🟠, order_rejected ⚠️, kill_switch ⛔, budget_exceeded 💸, daily_summary 📊, error ❗, startup/shutdown ▶️/⏹, llm_paused ⏸
```
불변식: `cycle`은 체결 직후 상태 저장 → 그 다음에 notify를 호출한다. 전송 실패가 사이클을 지연·중단시키지 않는다(테스트: transport가 항상 예외여도 사이클 완료, failed_log에 기록).

## 12. `tradingbot/reports.py`

```python
def append_decision(path: Path, record: dict) -> None                     # logs/decisions.jsonl (sort_keys)
def write_cycle_report(dir: Path, cycle: int, payload: dict) -> Path       # logs/cycles/<YYYYMMDD>/cycle_<n>.json + .md 요약
def append_journal(path: Path, trade: ClosedTrade, review: dict | None) -> None   # logs/journal.jsonl
def daily_summary(state: BotState, equity: float, ledger_spent: float) -> TradeEvent
```

## 13. `tradingbot/backtest.py` (규칙 전용 기준선; LLM 호출 없음)

```python
@dataclass
class BacktestConfig: symbols: list[str]; timeframe: str = "15m"; days: int = 90; fee_pct: float = 0.001; slippage_pct: float = 0.0005; start_balance: float = 10000.0; cache_dir: Path = Path("backtest_cache"); out_dir: Path = Path("backtest_results")
def rule_signal(ind15: IndicatorSet, ind1h: IndicatorSet | None, ind4h: IndicatorSet | None) -> tuple[Literal["BUY","HOLD"], float, str]
    # 기준선 규칙(문서화): 4h ema_50>ema_200(추세), 15m 종가>ema_20, rsi_14 45~65, macd_hist>0, volume_ratio_20>1.0, 1h atr_pct 0.3%~3% -> BUY conf 0.7 ; 아니면 HOLD. 지표가 None이면 HOLD.
def run(cfg: BacktestConfig, market: MarketData | None = None, candles: dict[str, dict[str, list[Candle]]] | None = None) -> BacktestResult
    # 선행편향 금지: 신호는 봉 t 종가 기준, 체결은 봉 t+1 시가(+슬리피지). risk.default_stops/clamp/enforce_net_rr/size_position 그대로 사용. 손절/익절은 봉 내 저가/고가로 판정(둘 다 닿으면 손절 우선 — 보수적). 시간손절 96봉.
    # 결과: 거래 목록, 순수익률, 승률, PF, MDD, 샤프(일봉 근사), 바이앤홀드 대비, 워크포워드 3구간 각각 결과, 몬테카를로(거래 순서 셔플 1000회) 5% 분위 MDD/수익, 비용 2배 스트레스.
    # verdict: "VALIDATED" (모든 구간 PF>1.1, 거래 >=30, MDD<10%, 비용 2배에서도 PF>1.0) / "NOT VALIDATED" + 사유. 결과를 out_dir/run_<ts>.json + .md 로 저장.
```

## 14. `tradingbot/dashboard/app.py` + `static/index.html` (v1 범위)

- `create_app(settings: Settings, data_dir: Path) -> FastAPI`. 읽기 전용 파일 기반(state.json, logs/*.jsonl, cost_ledger.jsonl, backtest_results/).
- 엔드포인트: `GET /api/health`, `GET /api/state`, `GET /api/positions`, `GET /api/trades?limit=100`, `GET /api/decisions?limit=100`, `GET /api/cycles?limit=20`, `GET /api/cycles/{date}/{n}`, `GET /api/costs`(오늘/이달/모델별), `GET /api/backtests`, `GET /api/journal?limit=50`, `POST /api/kill` (body `{"reason": "..."}`, 헤더 `X-Dashboard-Token`이 `control/dashboard_token`과 같아야 함 → `control/STOP` 파일 생성; cycle이 다음 사이클에 읽어 kill_switch=True), `POST /api/kill/clear`(STOP 파일 삭제 + 상태의 kill 해제 요청 파일 `control/CLEAR`).
- `index.html`: 외부 CDN 없음(오프라인), 10초 폴링, 섹션: 상태 헤더(모드·사이클·자본·당일 손익·낙폭 수준·LLM 일시정지·킬스위치), 포지션 표, 최근 결정(역할별 요약 펼치기), 거래 내역, 비용(오늘/이달/예산 대비 막대), 백테스트 결과, STOP 버튼(토큰 입력). 자본 스파크라인은 `<canvas>` 직접 그림.

## 15. `tradingbot/cycle.py` / `tradingbot/main.py` (v2에서 에이전트 흐름 확정; 골격은 v1)

```python
class Cycle:
    def __init__(self, settings, market: MarketData, broker: Broker, portfolio: Portfolio, backend: AgentBackend, ledger: CostLedger, notifier: Notifier, skills: SkillRegistry, *, now: Callable[[], datetime] | None = None)
    def run_once(self) -> CycleSummary
```
순서(고정): 잠금 확인 → control/STOP·CLEAR 파일 처리 → 상태 로드·날짜 롤오버(전일 요약 notify) → 시세·마크 갱신·peak 갱신 → **코드 청산 검사(SL/TP/시간)** 실행·저장·notify → 전역 게이트 → 후보 데이터 수집·품질 검사 → (LLM 일시정지/예산 초과면 건너뜀) 에이전트 파이프라인(v2) → 결정별 risk 조임·주문·**저장→notify** → 결정 로그·사이클 보고서 → 요약 반환. 모든 예외는 단계별로 잡아 로그·`error` 이벤트 notify 후 다음 단계 진행(상태 저장 실패·CorruptStateError·ConfigError 제외).

`main.py` CLI: `run [--once] [--cycle-minutes N]`, `dashboard`, `backtest [--days 90] [--symbols ...]`, `status`, `stop --reason ...`, `clear-stop`. `MODE=live`면 시작 시 콘솔에 "LIVE"를 입력받아야 진행(환경변수로 우회 불가). `bot.lock`(PID) 단일 인스턴스.

## 16. 테스트 요구 (v1 범위)
- `tests/test_config.py`, `test_risk.py`(적대적: 극단값·NaN·음수·경계), `test_indicators.py`(워밍업·결정성·알려진 값), `test_quality.py`, `test_news.py`, `test_exchange_paper.py`, `test_exchange_ccxt.py`(가짜 ccxt: 수수료 base 차감, fees 우선순위, 예외 스크럽, 재시도), `test_cost.py`(예산 초과·복원), `test_anthropic_backend.py`(가짜 client: parse 호출 인자 검증 — system cache_control·sort_keys·effort·output_format, refusal/max_tokens/예외 매핑, 로그에 키 없음), `test_skills.py`, `test_portfolio.py`(원자 저장·손상 정책·부분 체결·쿨다운·날짜 롤오버), `test_notify.py`(큐·재시도·실패 로그·토큰 비노출·포맷), `test_reports.py`, `test_backtest.py`(합성 캔들로 선행편향 없음·손절 우선·verdict), `test_dashboard.py`(TestClient: 각 엔드포인트, kill 토큰).
- 목표: v1에서 300개 이상, 전부 오프라인, 1분 이내.
