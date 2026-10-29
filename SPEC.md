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
    def estimate(self, model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float      # 미지 모델은 알려진 최고 단가로 보수 추정(상한 $10/$1/$50 per MTok과 표의 항목별 최댓값)
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
    # client 기본: anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0, timeout=timeout 기본 60)
    #   재시도는 SDK 가 아니라 백엔드가 한다: 재시도 가능한 오류만 최대 2회, 역할 timeout(그리고 set_deadline 으로 받은 사이클 기한) 안에서만.
    #   타임아웃 뒤에는 재시도하지 않는다. 주입된 client 는 자체 정책을 가진 것으로 보고 백엔드 재시도 0회.
    # complete():
    #   1) ledger.check(projected = project_call_cost(...))  -> BudgetExceeded는 그대로 전파(호출자가 LLM 일시정지 처리)
    #      최악값: 토큰 = ceil(문자수/4 × 1.3 토크나이저 여유), system 은 캐시 쓰기 단가(input × 1.25/2.0, ttl 5m/1h), 출력은 max_tokens 전부.
    #      러너의 병렬 예산 예약도 같은 project_call_cost 를 쓴다.
    #   2) client.messages.create(model=settings.model_for(tier), max_tokens=max_tokens,
    #        system=[{"type":"text","text":system,"cache_control":{"type":"ephemeral","ttl":cache_ttl}}],
    #        messages=[{"role":"user","content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True)}],
    #        output_config={"effort": effort, "format": {"type":"json_schema","schema": <output_model JSON 스키마(SDK transform_schema 적용)>}},
    #        timeout=timeout_s)  → 본문 text 를 output_model.model_validate_json 으로 직접 검증.
    #      (원래 parse(output_format=...) 를 쓰려 했으나, SDK 2.x 의 parse() 는 본문이 스키마에 안 맞으면(잘림·거부) usage/stop_reason 을
    #       읽기 전에 ValidationError 를 던져 (3)·(5)를 지킬 수 없다. create() 는 항상 원본 Message 를 돌려준다. — 구현 리뷰 LLM-1/2)
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

---

# SPEC v2 — 에이전트·피처팩·스캐너·게이트·사이클

v1 모듈 위에 얹는다. 역할 구성의 근거는 `docs/research/agent_roster.md`, 개요는 `DESIGN.md`.

## 17. `tradingbot/features.py` — 피처팩 (순수 함수, 코드가 숫자를 만든다)

모든 팩 모델은 이 모듈에 둔다(`models.py`는 고정). `extra="forbid"`. 퍼센트는 **% 단위 숫자**(1.5 = 1.5%), 비율은 소수. 값은 4유효자리로 반올림해 LLM 입력을 줄인다(`_r(x)`).

```python
class TechnicalLabels(BaseModel):
    timeframe: str
    ema_state: Literal["bull_aligned","bear_aligned","mixed","unknown"]   # close>ema20>ema50(>ema200 있으면) → bull, 역순 bear, 그 외 mixed, 지표 없으면 unknown
    price_vs_ema20_pct: float | None
    adx_14: float | None; rsi_14: float | None; rsi_5: float | None
    bb_position: float | None                     # (close-lower)/(upper-lower); 밴드 밖이면 <0 / >1
    macd_hist_sign: Literal["pos","neg","flat","unknown"]   # |hist| < 1e-9*close → flat
    obv_trend: Literal["up","down","flat","unknown"]         # OBV 20봉 회귀 기울기 부호(|slope|<평균OBV*1e-4 → flat)
    volume_ratio_20: float | None; atr_pct: float | None
    vote: Literal["trend_up","trend_down","mean_reversion_long","mean_reversion_short","none"]
    # trend_up: bull_aligned ∧ adx>20 ∧ macd pos ; trend_down 대칭 ; mr_long: rsi_14<30 ∧ bb_position<0.05 ; mr_short: rsi_14>70 ∧ bb_position>0.95
class IchimokuLabels(BaseModel):
    tk_cross: Literal["bullish","bearish","none"]            # 최근 3봉 내 전환선/기준선 교차
    price_vs_cloud: Literal["above","inside","below","unknown"]
    cloud_bullish: bool | None                               # 현재 구름: senkouA > senkouB
    cross_strength: Literal["strong","medium","weak","none"] # strong: 교차가 구름 위(아래)에서 ∧ 구름 방향 일치
class CandleLabels(BaseModel):
    patterns: list[Literal["bullish_engulfing","bearish_engulfing","hammer","shooting_star","doji","pin_bar_bull","pin_bar_bear","inside_bar","outside_bar"]]
    score: int                                               # 강세 +1 / 약세 −1 합, −3..3로 클립. 마지막 2봉만 본다.
class SmcLabels(BaseModel):
    structure: Literal["bos_up","bos_down","choch_up","choch_down","range","unknown"]  # 스윙 고/저(좌우 5봉 프랙탈)
    fvg_dir: Literal["bullish","bearish","none"]             # 최근 20봉 내 미충전 3봉 갭
    nearest_demand_zone: list[float] | None                  # [low, high] 마지막 강세 BOS 직전 약세 봉 범위
    nearest_supply_zone: list[float] | None
    distance_to_demand_pct: float | None; distance_to_supply_pct: float | None
class FeaturePack(BaseModel):
    symbol: str; ts: int; price: float
    per_tf: dict[str, TechnicalLabels]                       # "15m","1h","4h","1d" (있는 것만)
    ichimoku_1h: IchimokuLabels | None; ichimoku_4h: IchimokuLabels | None
    candles_15m: CandleLabels; candles_1h: CandleLabels
    smc_1h: SmcLabels | None; smc_4h: SmcLabels | None
    mtf_agreement: bool            # (4h ema_state==bull_aligned ∨ (4h mixed ∧ 1d bull_aligned)) ∧ 1h vote∈{trend_up, mean_reversion_long} ∧ 15m vote≠trend_down
    hv_pct_rank: float | None      # 1h 로그수익률 24봉 실현변동성의 백분위(가용 창 ≥100개일 때, 아니면 None)
    atr_pct_1h: float | None; vwap_distance_pct: float | None   # 15m 세션 VWAP 대비
    volume_vs_hour_avg: float | None                           # 마지막 15m 거래량 / 직전 4봉 평균
    cgo_60: float | None            # price / VWAP(1h 60봉) − 1 (%)
    abnormal_volume_multiple: float | None                     # 마지막 15m 거래량 / 직전 96봉 중앙값
    last_closes_1h: list[float]     # 최근 20개
    change_24h_pct: float | None
class FlowPack(BaseModel):
    symbol: str; funding_now: float | None; funding_7d_mean: float | None; funding_annualized_pct: float | None   # funding_now*3*365*100
    funding_regime: Literal["extreme_long","elevated_long","neutral","negative","squeeze","unknown"]   # 8h 기준 >0.05% / 0.02~0.05 / −0.01~0.02 / −0.02~−0.01 / <−0.02
    oi_change_24h_pct: float | None
    oi_funding_label: Literal["leveraged_long_buildup","short_buildup","deleveraging","neutral","unknown"]   # OI+5%∧펀딩>0.03%→LLB; OI+5%∧펀딩<0→SB; OI−5%→DL
    perp_premium_pct: float | None                                     # (perp_mark − spot)/spot*100
    ls_ratio: float | None; ls_label: Literal["crowded_long","crowded_short","balanced","unknown"]   # >2.0 / <0.8
    divergence_flags: list[str]                                        # "price_up_oi_down","price_down_oi_up","funding_up_price_flat"
    est_liq_clusters: list[dict]                                       # {"level":..,"side":"long","leverage":25|50|100,"estimated":True} level = vwap_24h*(1−1/lev+0.005)
    nearest_long_liq_below_pct: float | None
    cascade_risk: Literal["low","moderate","high","unknown"]           # high: nearest<2% ∧ LLB ; moderate: nearest<3% ∨ extreme_long
    oir_1pct: float | None                                             # (bid_depth−ask_depth)/(bid+ask) 1% 깊이
    spread_bps: float | None
    data_confidence: Literal["high","low","none"]                      # 펀딩·OI 둘 다 있으면 high, 하나면 low, 없으면 none
class RiskPack(BaseModel):
    symbol: str; var95_1h_pct: float | None; var99_1h_pct: float | None; cvar95_1h_pct: float | None   # 1h 로그수익률 최근 200봉 히스토리컬
    rv_20_pct: float | None; rv_60_pct: float | None                  # 연율화 %: std(ret)*sqrt(24*365)*100
    daily_vol_pct: float | None                                        # std(1h ret, 96봉)*sqrt(24)*100
    hv_pct_rank: float | None; kurtosis: float | None
    corr_to_btc_1h: float | None; beta_btc: float | None; corr_to_open_positions: dict[str, float]
    inverse_vol_size_pct: float | None                                 # min(10, 0.5/daily_vol_pct*100) ; daily_vol 없으면 None
    cvar_size_pct: float | None                                        # min(10, 0.15/cvar95_1h_pct*100)
    spread_bps: float | None; depth_taker_usdt: float | None           # ask 1% 깊이
    impact_bps_est: float | None                                       # planned_notional / depth_taker_usdt * 100
    order_notional_vs_15m_volume_share: float | None                   # planned_notional / (quote_volume_24h/96)
    stop_vs_atr_noise_ok: bool | None                                  # atr_pct_1h*1.5 < 2.0
class RegimePack(BaseModel):
    avg_pairwise_corr_1h: float | None; density: float | None          # 48봉, 상관>0.7 쌍 비율
    fused: bool                                                        # avg corr > 0.8
    btc_regime_4h: Literal["bull","bear","range","unknown"]            # ema50>ema200∧adx>20 bull / adx<20 range / else bear
    btc_shock: bool                                                    # |BTC 1h 수익률| > 2×ATR%(1h)
    name_alarm_symbols: list[str]                                      # |ret_1h| > 3×ATR% 이면서 BTC |ret_1h| < 1×ATR%
    verdict: Literal["normal","fused","macro_alarm"]                   # macro_alarm: btc_shock ∧ fused
class SentimentComposite(BaseModel):
    score_0_100: float | None; label: Literal["extreme_fear","fear","neutral","greed","extreme_greed","unknown"]
    components: dict[str, float | None]                                # fear_greed(0.4), funding_z(0.2), ls_pctile(0.2), headline_polarity(0.2) — 없는 성분은 가중치 재정규화

def build_feature_pack(symbol, candles_by_tf: dict[str, list[Candle]], indicators_by_tf: dict[str, IndicatorSet], ticker: Ticker) -> FeaturePack
def build_flow_pack(symbol, futures: FuturesContext | None, funding_history: list[float], orderbook: OrderBookSummary | None, ticker: Ticker, perp_mark: float | None, candles_1h: list[Candle], prev_oi_usdt: float | None) -> FlowPack
def build_risk_pack(symbol, candles_1h, btc_candles_1h, open_candles_1h: dict[str, list[Candle]], orderbook, ticker, planned_notional: float, atr_pct_1h: float | None) -> RiskPack
def build_regime_pack(candles_1h_by_symbol: dict[str, list[Candle]], btc_symbol: str, btc_ind_4h: IndicatorSet | None, btc_ind_1h: IndicatorSet | None) -> RegimePack
def composite_sentiment(fear_greed: int | None, funding_z: float | None, ls_pctile: float | None, headline_polarity: float | None) -> SentimentComposite
def compact(obj: BaseModel) -> dict        # model_dump 후 float 4유효자리 반올림, None 유지, 결정적 키 순서
```
이 모듈의 각 라벨 함수(`ema_state`, `vote`, `ichimoku`, `candle_patterns`, `smc_structure`, `fvg`, `zones`, `obv_trend`, `historical_var`, `rolling_corr`)는 공개 함수로 노출해 단독 테스트한다.

## 18. `tradingbot/scanner.py` — 코드 전용 스캐너

```python
class Thresholds:   # 설정으로 덮어쓸 수 있는 기본값(환경변수 SCANNER_*; 없으면 기본)
    MIN_QUOTE_VOLUME_24H = 20_000_000.0; MAX_SPREAD_BPS = 10.0; MIN_DEPTH_1PCT_USDT = 200_000.0
    MIN_VOL_RATIO_15M = 0.3; MAX_SPIKE_24H_PCT = 15.0; SPIKE_VOLUME_MULT = 2.0; FUNDING_WINDOW_S = 60
class LiquidityGate(BaseModel): symbol: str; ok: bool; reasons: list[str]; spread_bps: float | None; quote_volume_24h: float | None; depth_1pct_usdt: float | None
class Candidate(BaseModel): symbol: str; rank: int; composite_z: float; factor_table: dict[str, float | None]; reasons: list[str]
class ScanResult(BaseModel):
    ts: int; universe: list[str]; candidates: list[Candidate]; skipped: list[dict]      # {"symbol","reason"}
    regime: RegimePack; sentiment: SentimentComposite; funding_window: bool
    feature_packs: dict[str, FeaturePack]; flow_packs: dict[str, FlowPack]; risk_packs: dict[str, RiskPack]
    liquidity: dict[str, LiquidityGate]; quality: dict[str, QualityReport]; news: list[NewsItem]; news_available: bool
    marks: dict[str, float]                 # 심볼별 현재가(보유 포지션 마크에도 사용)
    fetched: dict[str, int]                 # 심볼별 캔들 수 요약(디버그)

FACTOR_WEIGHTS = {"momentum_4h": 0.25, "momentum_1d": 0.15, "reversal_1h": 0.10, "rv_24h": -0.10, "volume_ratio": 0.10, "rs_vs_btc": 0.15, "funding_z": -0.10, "oi_change_24h": 0.05}
def in_funding_window(now_ms: int, window_s: int = 60) -> bool     # UTC 00:00/08:00/16:00 ± window
def zscores(values: dict[str, float | None]) -> dict[str, float | None]   # 표본 ≥4이면 z, 아니면 순위 기반 (−1..1)
def rank_candidates(features, flows, risks, btc_symbol, exclude: set[str], max_candidates: int) -> list[Candidate]
    # momentum_4h: 마지막 봉을 뺀 직전 24개 4h 로그수익률 합 ; momentum_1d: 마지막 일봉 뺀 7일 ; reversal_1h: −(최근 4개 1h 수익률 합) ; rv_24h ; volume_ratio(15m) ; rs_vs_btc = momentum_4h − BTC momentum_4h ; funding_z: 펀딩 now 의 7d 대비 z ; oi_change_24h
class Scanner:
    def __init__(self, settings: Settings, market: MarketData, *, news_fetcher=None, fear_greed_fetcher: Callable[[], int | None] | None = None, cache_dir: Path | None = None, thresholds: Thresholds | None = None)
    def scan(self, state: BotState, now_ms: int) -> ScanResult
    # 1) 유니버스 = settings.symbols (+ BTC/USDT 는 레짐 계산용으로 항상 조회)
    # 2) 캔들 15m 300 / 1h 200 / 4h 200 / 1d 120 ; 4h·1d 는 시각이 바뀌지 않았으면 메모리 캐시 재사용
    # 3) quality.check 실패 종목은 skipped(reason) — 보유 종목이면 marks 만 갱신하고 LLM 입력에서 제외
    # 4) 유동성 게이트 ; 보유/쿨다운/복수매매/급등 제외 ; 펀딩창이면 funding_window=True(후보는 남기되 cycle 이 신규 주문 금지)
    # 5) 팩 생성(후보 전체 + 보유 종목), 레짐, 종합심리(뉴스 극성은 news_sentiment 결과가 없으면 None)
    # 6) 레짐 게이트: macro_alarm → candidates=[] (skipped 에 사유) ; name_alarm 종목 제외
    # 모든 네트워크 실패는 종목 단위로 격리(ExchangeError → skipped). BTC 데이터 실패 시 regime.verdict="normal" 에 unknown 라벨.
```

## 19. `tradingbot/gates.py` — G0~G4 (순수 함수)

```python
class GateDecision(BaseModel): gate: str; passed: bool; reasons: list[str]; size_multiplier: float = 1.0; confidence_floor: float = HardLimits.MIN_CONFIDENCE; notes: list[str] = []
def g0_preflight(state: BotState, equity: float, now_ms: int, budget_ok: bool, funding_window: bool) -> GateDecision      # risk.global_gates + llm 일시정지 + 펀딩창(신규 주문 금지 사유만 추가)
def g1_candidate(symbol: str, tech: TechnicalOutput | None, flow: FlowSymbolView | None, news_risk_symbols: set[str], sentiment: SentimentComposite, position_count: int, exposure_pct: float, corr_to_open: dict[str, float], tech_abstained: bool) -> GateDecision
def g2_bear(bear: BearOutput | None) -> GateDecision            # None(기권) 은 통과시키되 notes 에 "bear_abstained"
def g3_risk(symbol: str, verdict: RiskCandidateVerdict | None, regime: RegimePack, decay_state: str, bias: dict[str, str], risk_pack: RiskPack, liquidity: LiquidityGate, planned_notional: float, stop_pct: float) -> GateDecision
    # verdict None(기권) → passed False ("risk_abstained": 리스크 기권이면 신규 진입 없음)
def g4_final(decision: HeadDecision, verdict: RiskCandidateVerdict, floor: float, size_multiplier: float, equity: float, entry_price: float, atr_1h: float | None, free_quote: float) -> tuple[GateDecision, OrderPlan | None]
class OrderPlan(BaseModel): symbol: str; quote_amount: float; stop_loss_price: float; take_profit_price: float; size_pct: float; notes: list[str]
```
G4 계산 순서: stop_pct = min(head.stop_pct, verdict.stop_pct, 2.0) → `risk.clamp_stops` → `risk.enforce_net_rr` → size_pct = min(head.size_pct, verdict.max_size_pct, 10) → `risk.size_position(... size_multiplier ...)`에서 quote 계산 후 size_pct 상한 적용 → MIN_ORDER_USDT 미만이면 HOLD.

## 20. `tradingbot/agents/` — 역할·스키마·러너·파이프라인

### 20.1 `schemas.py` (출력 모델; 전부 `extra="forbid"`, 각 모델에 `@classmethod conservative_example()`)
```python
Direction = Literal["long","flat","avoid"]
class TechnicalOutput(BaseModel):
    symbol: str; direction: Direction; setup_type: Literal["trend_continuation","mean_reversion","breakout","none"]
    timing: Literal["now","wait_pullback","wait_confirmation"]; mtf_agreement: bool; score: int  # −5..5
    confidence: float  # 0..1
    stop_candidate_price: float | None; stop_pct: float | None; sl_reference: Literal["zone","kijun","atr","none"]
    target_candidate_price: float | None; tp_pct: float | None
    invalidation: str; noise_check: bool; exit_flag: bool = False; exit_reason: str | None = None
    evidence: list[str]   # ≤3, 각 항목은 입력 필드명을 포함
class FlowSymbolView(BaseModel):
    symbol: str; posture: Literal["capitulation","fear","neutral","greed","euphoria"]; crowding_risk: Literal["low","moderate","high"]
    squeeze_direction: Literal["up","down","none"]; cascade_risk: Literal["low","moderate","high"]; nearest_long_liq_cluster_pct_below: float | None
    stop_inside_cascade: bool; washout_setup: bool; block_new_longs: bool; block_reason: str | None
    flow_bias: Literal["long","neutral","avoid"]; confidence: float; evidence: list[str]
class FlowOutput(BaseModel): per_symbol: list[FlowSymbolView]; market_wide_note: str; data_confidence: Literal["high","low"]
class NewsEvent(BaseModel): headline_id: str; score: float; event_type: Literal["hack","regulatory","listing","delisting","macro","etf","exchange","protocol","sentiment","technical_break","other"]; symbols: list[str]; summary: str; stale: bool
class NewsOutput(BaseModel): events: list[NewsEvent]; risk_event: bool; risk_event_symbols: list[str]; market_tone: float; market_note: str; feed_health_note: str
class ThesisPoint(BaseModel): claim: str; evidence_field: str
class BullOutput(BaseModel):
    symbol: str; case_strength: int; thesis_points: list[ThesisPoint]; catalysts: list[str]; underreaction_evidence: str
    entry_trigger: str; expected_horizon_bars: int; invalidation_level: float | None; disconfirming_evidence: list[str]  # ≥2
    main_risk_to_bull_case: str; what_would_disprove: str; confidence: float
class DeathPath(BaseModel): family: Literal["liquidation_cascade","news_shock","regime_flip","crowded_unwind","data_incident","other"]; description: str; observable_trigger: str; probability_band: Literal["low","mid","high","unknown"]; time_to_impact_bars: int | None
class BearOutput(BaseModel):
    symbol: str; death_paths: list[DeathPath]  # ≥3
    greed_flags: list[str]; disconfirming_evidence: list[str]; strongest_point: str; what_would_disprove_bear: str
    verdict: Literal["survivable","survivable_if_monitored","decline"]; monitoring_conditions: list[str]; confidence: float
class BullPointReliability(BaseModel): point: str; score_1_5: int; pushback: str
class RiskCandidateVerdict(BaseModel):
    symbol: str; verdict: Literal["approve","conditional","veto"]; veto_reasons: list[str]; conditions: list[str]
    max_size_pct: float; stop_pct: float; take_profit_pct: float; rr_net_estimate: float | None
    bull_point_reliability: list[BullPointReliability]; required_monitoring: list[str]; correlation_note: str
class RiskPositionView(BaseModel): symbol: str; assumption_status: list[Literal["holding","weakening","impaired","broken"]]; red_lines_triggered: list[str]; health: int; action: Literal["hold","trim","exit"]; reason: str
class RiskGlobal(BaseModel): entries_paused: bool; pause_reason: str | None; confidence_floor: float; exposure_cap_pct: float; portfolio_note: str
class RiskOutput(BaseModel): candidates: list[RiskCandidateVerdict]; positions: list[RiskPositionView]; global_: RiskGlobal = Field(alias="global"); risk_note: str; confidence: float   # model_config populate_by_name=True
class RedLine(BaseModel): metric: Literal["price","unrealized_pct","funding_rate","btc_return_1h_pct","composite_sentiment","hours_held"]; op: Literal["<",">"]; value: float
class Thesis(BaseModel): reasons: list[str]; assumptions: list[str]; red_lines: list[RedLine]; horizon_bars: int
class HeadDecision(BaseModel):
    symbol: str; action: Literal["BUY","SELL","HOLD"]; confidence: float; size_pct: float; stop_pct: float; tp_pct: float
    thesis: Thesis; disagreement_class: Literal["horizon","caliber","risk_preference","information","none"]
    losing_side_strongest_point: str; decisive_factor: str; rationale: str
class Debias(BaseModel): anchored_to_entry: bool; ignored_bearish_news: bool; overtrading: bool; chasing_spike: bool; acting_on_sentiment_alone: bool
class HeadOutput(BaseModel): decisions: list[HeadDecision]; debias: Debias; reevaluation_triggers: list[str]; hold_reasons: list[str]; cycle_note: str
class JournalPostTrade(BaseModel): trade_id: str; summary_bullets: list[str]; assumptions_at_exit: list[dict]; broken_assumption: str | None; near_miss_rules: list[str]; lesson: str; lesson_tag: Literal["thesis_broken","stop_too_tight","chased_spike","news_ignored","good_exit","time_stop","regime_exit","risk_exit","other"]; telegram_text: str
class JournalDaily(BaseModel): summary_bullets: list[str]; what_worked: list[str]; what_failed: list[str]; risks: list[str]; action_items: list[dict]; abstain_reason: str | None; do_not_change_yet: bool; telegram_text: str
```
`conservative_example()`는 "아무 것도 하지 않는" 안전값(technical: flat/confidence 0, bear: decline, risk: veto·entries_paused False, head: HOLD 전부)이어야 한다. 검증자(validator)는 `confidence`∈[0,1], `score`∈[−5,5], `stop_pct`∈(0,2], `tp_pct`∈(0,4], `size_pct`∈[0,10], `health`∈[1,10], `case_strength`∈[1,5]를 `field_validator`로 강제한다(위반은 파싱 실패 → 기권).

### 20.2 `roles.py`
```python
@dataclass(frozen=True)
class RoleSpec: role_id: str; tier: Tier; prompt_file: str; skills: tuple[str, ...]; output_model: type[BaseModel]; max_tokens: int; effort: Literal["low","medium","high"]; timeout_s: float; batched: bool
ROLES: dict[str, RoleSpec] = {
  "technical": (analysis, "prompts/technical.md", ("technical-basic","ichimoku","candlestick","smc","volatility","minute-analysis"), TechnicalOutput, 3000, "low", 30, False),
  "derivatives_flow": (analysis, "prompts/derivatives_flow.md", ("perp-funding-basis","crypto-derivatives","liquidation-heatmap","investor-lenses","market-microstructure"), FlowOutput, 5000, "low", 35, True),
  "news_sentiment": (analysis, "prompts/news_sentiment.md", ("event-driven","sentiment-analysis","research-discipline"), NewsOutput, 5000, "low", 35, True),
  "bull": (research, "prompts/bull.md", ("behavioral-finance","research-discipline","event-driven"), BullOutput, 5000, "medium", 60, False),
  "bear": (research, "prompts/bear.md", ("investor-lenses","behavioral-finance","research-discipline","correlation-regime","perp-funding-basis"), BearOutput, 5000, "medium", 60, False),
  "risk_manager": (research, "prompts/risk_manager.md", ("risk-analysis","correlation-regime","correlation-analysis","asset-allocation","market-microstructure","execution-model","thesis-tracker","trade-journal","strategy-dev-manager","volatility","behavioral-finance"), RiskOutput, 7000, "medium", 75, True),
  "head": (head, "prompts/head.md", ("investor-lenses","behavioral-finance","thesis-tracker","sentiment-analysis","event-driven","research-discipline"), HeadOutput, 9000, "high", 100, True),
  "journal": (analysis, "prompts/journal.md", ("trade-journal","performance-attribution","report-generate","strategy-dev-manager"), JournalPostTrade|JournalDaily (호출 시 지정), 4000, "low", 60, False),
}
```

### 20.3 `payloads.py` — 역할별 user payload 빌더(결정적, 컴팩트)
`build_technical(symbol, fp: FeaturePack, position: Position | None, hard_limits: dict) -> dict`, `build_flow(flow_packs, symbols, thresholds) -> dict`, `build_news(headlines_new, aliases, now_ms, feed_health, prev_events) -> dict`, `build_bull(symbol, tech, flow_view, news_events, sentiment, scanner_row, regime, position) -> dict`, `build_bear(... + corr_to_open, hv_pct) -> dict`, `build_risk(candidates_ctx, positions_ctx, risk_packs, regime, account_pack, news_risk_symbols, flow_blocks) -> dict`, `build_head(approved_ctx, flagged_positions_ctx, regime, account_pack, sentiment, vetoed_ctx, hard_limits, abstained_channels) -> dict`, `build_journal_post_trade(trade, thesis_history, verdicts) -> dict`, `build_journal_daily(metrics_table, closed_count) -> dict`. 모든 빌더는 `features.compact` 로 반올림하고 `hard_limits` 는 항상 `{"max_position_pct":10,"max_positions":3,"max_stop_pct":2,"max_tp_pct":4,"min_confidence":0.65}` 를 포함한다. 캔들 원본은 넣지 않는다(1h 종가 20개만).

### 20.4 `runner.py`
```python
class AgentRunner:
    def __init__(self, settings: Settings, backend: AgentBackend, skills: SkillRegistry, prompts_dir: Path, *, max_workers: int = 6)
    def system_prompt(self, role_id: str) -> str          # prompts/<role>.md + "\n\n# Skills\n" + skills.render(role) ; 메모리 캐시 ; 바이트 고정
    def run(self, role_id: str, payload: dict, *, output_model: type[BaseModel] | None = None) -> AgentResult   # BudgetExceeded 는 전파
    def run_many(self, calls: list[tuple[str, dict]]) -> list[AgentResult]   # 스레드풀 병렬, 순서 보존
```

### 20.5 `pipeline.py` — Step 2~4 + G1~G3
```python
class CandidateContext(BaseModel): symbol: str; candidate: Candidate; technical: TechnicalOutput | None; flow: FlowSymbolView | None; bull: BullOutput | None; bear: BearOutput | None; risk: RiskCandidateVerdict | None; gates: list[GateDecision]; dropped_at: str | None
class PositionContext(BaseModel): symbol: str; technical: TechnicalOutput | None; risk: RiskPositionView | None; red_line_hits: list[str]; health_history: list[int]; flagged_for_head: bool; flag_reason: str | None
class PipelineResult(BaseModel): candidates: list[CandidateContext]; positions: list[PositionContext]; news: NewsOutput | None; flow: FlowOutput | None; risk_global: RiskGlobal | None; head: HeadOutput | None; abstained: list[str]; llm_calls: int; llm_cost_usd: float; gate_log: list[GateDecision]; head_skipped_reason: str | None
def run_pipeline(scan: ScanResult, state: BotState, equity: float, runner: AgentRunner, journal_state: JournalState, now_ms: int, *, news_store: NewsStore, should_stop: Callable[[str], bool] | None = None) -> PipelineResult
# should_stop(stage): cycle 이 넘기는 사이클 기한 검사. 단계 경계("debate"·"risk_manager"·"head" 앞)에서 True 면 남은 LLM 단계를 HOLD 로
#   (head_skipped_reason="cycle_deadline", 살아 있던 후보 dropped_at="cycle_deadline", abstained 에 "cycle_deadline"; LLM 일시정지 없음).
#   cycle 은 파이프라인 동안 backend.set_deadline(기한 - 경과 - 25s) 를 걸고 끝나면 set_deadline(None).
```
규칙: 분석가 3종은 `run_many` 로 동시에(technical 은 후보∪보유 각각, flow 1회, news 는 새 헤드라인 있을 때만); 모두 끝난 뒤 G1; 살아남은 후보에 bull·bear 를 **한 번의 run_many** 로; G2; risk_manager 1회(후보 생존 또는 보유 있음); G3; head 는 승인/조건부 후보 ≥1 또는 플래그 포지션 ≥1 일 때만 1회. `BudgetExceeded` 가 어느 단계에서든 나면 그 시점까지의 결과로 HOLD 처리하고 `abstained` 에 "budget" 기록(예외는 cycle 로 전파해 LLM 일시정지 처리). 어떤 역할 출력도 다른 분석가의 입력이 되지 않는다(bull/bear/risk/head 만 상위 출력을 본다).

## 21. `tradingbot/journal.py`
```python
class JournalState(BaseModel): closed_count: int; decay_state: Literal["insufficient_sample","healthy","warning","decayed"]; bias: dict[str, Literal["none","mild","severe","insufficient_sample"]]; gate_stats: dict[str, int]; health_history: dict[str, list[int]]; thesis: dict[str, Thesis]; last_daily_review_date: str | None
def evaluate_red_lines(thesis: Thesis, mark: float, position: Position, funding: float | None, btc_return_1h_pct: float | None, sentiment: float | None, now_ms: int) -> list[str]
def bias_severities(trades: list[ClosedTrade], cycle_minutes: int) -> dict[str, str]    # disposition: 손실 보유시간/이익 보유시간 ≥1.2 mild ≥1.5 severe ; overtrading: 일 거래수 > 8 mild > 12 severe ; chasing: 진입 직전 1h +3% 이상 비율 >30% mild >50% severe ; anchoring: 진입가 ±0.3% 내 청산 비율 >30% ; 20건 미만 insufficient_sample
def decay_state(trades: list[ClosedTrade]) -> str                 # 최근 20건 PF: ≥1.1 healthy, 0.9~1.1 warning, <0.9 decayed ; 20건 미만 insufficient_sample
def post_trade_review(runner, trade: ClosedTrade, ctx: dict) -> JournalPostTrade | None   # 실패 시 None(코드 요약으로 대체)
def daily_review(runner, state, trades, metrics) -> JournalDaily | None
def code_metrics(trades: list[ClosedTrade], equity_curve: list[float], btc_curve: list[float] | None) -> dict   # 승률·PF·평균손익·샤프/소르티노/칼마·MDD·BTC 대비
```

## 22. `tradingbot/cycle.py` 확정 순서
1. 잠금(`bot.lock`) 확인·갱신, `control/STOP`→kill, `control/CLEAR`→kill 해제(파일 삭제).
2. 상태 로드, 날짜 롤오버(전일 `daily_summary` notify + 저널 일일 리뷰 호출은 00:05 UTC 이후 첫 사이클).
3. `Scanner.scan` → marks 로 포지션 마크 갱신, equity·peak 갱신, 낙폭 사다리 레벨 계산(−6% 이면 `set_kill("drawdown_stop_all")` + notify).
4. **코드 청산**: 보유마다 `risk.check_exits` → market_sell → `close_position` → `save` → notify(exit) → 저널 사후분석(실패 무시).
5. G0. 실패면 결정 로그·보고서 쓰고 종료(LLM 0회).
6. `run_pipeline`. `BudgetExceeded` → `pause_llm(다음 UTC 자정)` + notify(budget_exceeded); 인증 오류 기권 다수 → `pause_llm(60분)` + notify(llm_paused).
7. head 결정 처리: SELL(보유) → 매도·저장·notify(exit, reason head_sell); BUY → G4 → OrderPlan → market_buy → `open_position`(논지·레드라인 저장) → `save` → notify(entry); 강등은 notify(order_rejected 종류 "gate_downgrade"). HOLD 는 로그만.
8. 결정 로그(`decisions.jsonl`: 역할별 출력 요약·게이트·비용), 사이클 보고서, 상태 저장, `CycleSummary` 반환. 사이클 기한 5분 초과 시 남은 단계는 HOLD 처리 + notify(error "cycle_deadline").

`main.py run`: 루프는 `cycle_minutes` 경계 + 5초에 실행. 하트비트(4h) notify. SIGINT 시 notify(shutdown)·flush.

## 23. 프롬프트·스킬 규약
- `prompts/<role>.md`: 영어 지시 + "모든 자유 텍스트 필드는 한국어" 규칙. 반드시 포함: 역할·입력 필드 설명·출력 스키마 요약·하드 리밋 재진술·근거는 입력 필드명 인용·숫자 날조 금지·기권 정책·현물 롱 전용(약세는 '진입 안 함/청산'으로만 표현)·"empty feed ≠ calm market". 사이클 번호·날짜·심볼을 프롬프트에 넣지 않는다(캐시).
- `skills/<name>/SKILL.md`: frontmatter `name, description, roles: [...], version: 1, source: "adapted from HKUDS/Vibe-Trading agent/src/skills/<name>/SKILL.md (MIT)", license: MIT`. 본문 ≤ 900 단어, 우리 말로 다시 쓴 절차·임계값·체크리스트. 원문 문장 복사 금지(공식·숫자는 허용). 현물 봇에 맞지 않는 부분(옵션·A주·숏)은 뺀다.
- `THIRD_PARTY_NOTICES.md`: Vibe-Trading MIT 전문 + 적용 범위.

## 24. 테스트 요구 (v2)
`test_features.py`(라벨 함수별 합성 캔들), `test_scanner.py`(가짜 MarketData: 게이트·랭킹·레짐·제외·펀딩창), `test_gates.py`(G1~G4 경계·기권), `test_schemas.py`(검증자·conservative_example), `test_runner.py`(시스템 프롬프트 바이트 고정·병렬 순서), `test_pipeline.py`(MockBackend 시나리오: 전원 기권→HOLD, 약세 decline→탈락, 리스크 veto→탈락, 승인→BUY, 예산 초과), `test_journal.py`, `test_cycle.py`(paper 1사이클 end-to-end: 진입·저장·텔레그램 가짜 전송 기록·결정 로그·청산 사이클), `test_main.py`(CLI 인자·live 확인 입력). 목표 v1+v2 합계 600개 이상.

---

# SPEC v2.1 — payload 필드 계약 (§20.3a) 과 보완 (2026-10-28 프롬프트 교차검토 반영)

프롬프트(`prompts/*.md`)는 이 절의 필드 경로만 인용한다. payload 빌더는 이 절의 키를 **정확히** 만든다(추가 키 금지, 없는 값은 `null`). 모든 숫자는 `features.compact`(4유효자리)로 반올림, 퍼센트는 % 단위, 펀딩은 8h 원시 소수. 모든 payload 에 `"role"` 과 `"hard_limits"` 가 있다.

```python
HARD_LIMITS_PAYLOAD = {"max_position_pct": 10, "max_positions": 3, "max_stop_pct": 2, "max_tp_pct": 4, "min_confidence": 0.65}
FLOW_THRESHOLDS = {"funding_extreme_long": 0.0005, "funding_elevated_long": 0.0002, "funding_negative": -0.0001, "funding_squeeze": -0.0002,
                   "oi_buildup_pct": 5.0, "oi_buildup_funding": 0.0003, "oi_deleveraging_pct": -5.0,
                   "ls_crowded_long": 2.0, "ls_crowded_short": 0.8, "cascade_high_distance_pct": 2.0, "cascade_moderate_distance_pct": 3.0}
```

## 20.3a.1 technical — `build_technical(symbol, fp, position, mark, cycle) -> dict`
```
{"role":"technical","symbol":str,"feature_pack":FeaturePack,
 "position": null | {"entry_price","stop_loss_price","take_profit_price","last_mark","unrealized_pct","bars_held","opened_ts"},
 "hard_limits":…}
```
- `unrealized_pct = (mark/entry_price − 1)×100`, `bars_held = cycle − position.opened_cycle`. **`thesis`·`confidence` 는 넣지 않는다**(총괄 출력이 분석가 입력이 되지 않게 코드로 강제).

## 20.3a.2 derivatives_flow — `build_flow(flow_packs, symbols) -> dict`
```
{"role":"derivatives_flow","symbols":[…],"flow_packs":{sym:FlowPack},"thresholds":FLOW_THRESHOLDS,"hard_limits":…}
```
`symbols` 는 후보 ∪ 보유, 입력 순서 유지. `flow_packs` 키 집합 == `symbols`.

## 20.3a.3 news_sentiment — `build_news(headlines_new, aliases, now_ms, feed_health, prev_events) -> dict`
```
{"role":"news_sentiment",
 "headlines":[{"id","ts","age_h","source","source_grade","title"}],   # 최신순, 최대 40, id = sha1(url or title)[:12], source_grade = "high"(tier 1) | "low"(tier 2·3), age_h = (now_ms − ts)/3.6e6 (ts 없으면 null)
 "aliases":{alias_lower: symbol},                                       # news.py 별칭 + base 티커 소문자
 "now_ms":int, "feed_health":{"items_fetched","oldest_ts","newest_ts","errors":[…]},
 "prev_events":[{"headline_id","score","event_type","symbols","ts"}],  # 만료 전(24h) 이벤트, 재채점 금지
 "hard_limits":…}
```
뉴스 점수 척도(고정 7단계): −1.0, −0.66, −0.33, 0, +0.33, +0.66, +1.0. `stale = age_h > 24`, 7일 초과는 |score| ≤ 0.33.

## 20.3a.4 bull / bear — `build_bull(...)`, `build_bear(...)`
```
{"role":"bull"|"bear","symbol",
 "technical": TechnicalOutput | null,
 "feature_extract": null | {"cgo_60","rsi_5","rsi_14","bb_position","obv_trend","abnormal_volume_multiple","volume_vs_hour_avg","change_24h_pct"},   # rsi_5/rsi_14/bb_position/obv_trend 은 1h 값
 "flow": FlowSymbolView | null,
 "news_events":[{"headline_id","score","event_type","symbols","summary","stale","age_h"}],   # 이 심볼의 24h 내 이벤트
 "news_context": null | {"market_tone","risk_event_for_symbol":bool,"feed_health_note"},   # news 채널 기권이면 null
 "event_signal": {"score": float|null, "n_events": int},   # clip(Σ score·0.5^(age_h/12), −1, 1); 이벤트 없으면 score null
 "sentiment": SentimentComposite,
 "scanner_row": Candidate + {"liquidity": LiquidityGate},
 "regime": {"btc_regime_4h","fused","density","verdict","btc_shock","name_alarm_symbols"},
 "position": null | {"entry_price","unrealized_pct","bars_held","thesis": Thesis | null},
 "hard_limits":…}
bear 만 추가: "corr_to_open": {open_symbol: corr}, "hv_pct": float|null (= feature_pack.hv_pct_rank)
```
bull payload 에는 bear 키가 없고 bear payload 에는 bull 키가 없다(테스트).

## 20.3a.5 risk_manager — `build_risk(...)`
```
{"role":"risk_manager",
 "candidates":[{"symbol","candidate":Candidate,"technical","flow","bull","bear","planned_notional_usdt"}],   # G2 생존 후보
 "positions":[{"symbol","entry_price","mark","unrealized_pct","bars_held","thesis":Thesis|null,
               "prev_assumption_status":[…]|null,     # 지난 사이클 risk_manager 평가(없으면 null) — 코드 판정이 아님, 지금 다시 평가
               "red_line_hits":[…],"health_history":[…],"technical":{"exit_flag","exit_reason"}|null}],
 "risk_packs":{sym:RiskPack},      # RiskPack 에 exec_cost_pct 포함(§17 보완)
 "regime":RegimePack,
 "account_pack":ACCOUNT_PACK,
 "news_risk_symbols":[…],
 "flow_blocks":{sym:{"block_new_longs","stop_inside_cascade","cascade_risk"}},
 "hard_limits":…}
ACCOUNT_PACK = {"equity","daily_pnl_pct","drawdown_pct","drawdown_ladder_level"("normal"|"caution"|"block"|"stop_all"),"exposure_pct","position_count",
                "consecutive_losses","cooldowns":{sym: cycles_left},"trades_today","decay_state","bias_severities":{"disposition","overtrading","chasing","anchoring"},
                "noise_trade_share": float|null,   # 최근 20건 중 |pnl_pct| < 0.3 비율, 5건 미만이면 null
                "llm_budget_remaining_usd"}
```

## 20.3a.6 head — `build_head(...)`
```
{"role":"head",
 "approved":[{"symbol","candidate",
              "technical": TechnicalOutput + {"labels":{"ichimoku_4h_price_vs_cloud","cgo_60","abnormal_volume_multiple","change_24h_pct"}} | null,
              "flow","news":{"events":[…],"market_tone","risk_event_for_symbol"}|null,"bull","bear",
              "risk": RiskCandidateVerdict + {"confidence_floor","entries_paused","exposure_cap_pct","risk_confidence"},
              "gate":{"confidence_floor","size_multiplier","notes":[…]}}],     # G3 이후 실효 하한·배수 — head 확신도가 이 하한 미만이면 코드가 HOLD
 "flagged_positions":[{"symbol","entry_price","mark","unrealized_pct","bars_held","thesis","red_line_hits","health_history",
                       "risk":RiskPositionView|null,"technical":{"exit_flag","exit_reason"}|null,"flag_reason"}],
 "regime":RegimePack,"account_pack":ACCOUNT_PACK,
 "sentiment":{"composite":SentimentComposite,"event_signal":{sym:{"score","n_events","combined_score"}}},   # combined = 0.6·technical.score/5 + 0.4·event score(없으면 0), 참고용
 "vetoed":[{"symbol","stage":"g1"|"bear_decline"|"risk_veto"|"g3","reason"}],
 "hard_limits":…,"abstained_channels":[…]}
```

## 20.3a.7 journal — `build_journal_post_trade(trade, thesis, thesis_history, verdicts)`, `build_journal_daily(metrics_table, closed_count)`
```
post_trade: {"role":"journal","mode":"post_trade",
  "trade": ClosedTrade 필드 + {"trade_id": f"{symbol}-{opened_ts}-{closed_ts}", "bars_held", "thesis": Thesis|null},   # thesis 는 JournalState.thesis[symbol] (구조화)
  "thesis_history":[{"cycle","health","assumption_status","red_line_hits","action","exit_flag","unrealized_pct"}],
  "verdicts": JournalState.entry_verdicts[symbol] | null + {"news_risk_after_entry":[…]},
  "hard_limits":…}
daily: {"role":"journal","mode":"daily","closed_count":int,"hard_limits":…,
  "metrics_table":{"returns":{"day_pnl_pct","cum_pnl_pct","equity","max_drawdown_pct","btc_buy_hold_pct","excess_vs_btc_pct"},
                   "trade_stats":{"closed_today","win_rate","avg_win_pct","avg_loss_pct","profit_factor","avg_bars_held","sharpe","sortino","calmar"},
                   "by_exit_reason":{reason:{"count","pnl_pct_sum"}},"by_symbol":{sym:{"count","pnl_pct_sum"}},"by_setup":{setup_type:{"count","pnl_pct_sum"}},
                   "attribution_buckets":{"noise_trades","stop_losses","take_profits","time_stops","head_sells"},
                   "bias_severities":{…},"decay_state":str,
                   "gate_stats":{"candidates_seen","g1_dropped","g2_bear_decline","g3_risk_veto","g3_other","g4_downgrade","head_calls","bear_decline_rate","risk_veto_rate"},
                   "llm":{"calls","cost_usd","budget_usd","abstain_count"},"halts":[…]}}
```
`entry_verdicts[symbol]`(BUY 체결 시 저장) = `{"technical":{"setup_type","confidence"},"bull":{"case_strength","thesis_points","confidence"},"bear":{"verdict","strongest_point","death_paths"},"risk":{"verdict","conditions","required_monitoring","rr_net_estimate","risk_note"},"head":{"disagreement_class","decisive_factor","rationale","debias_chasing_spike"},"channel_agreement":{"technical":bool,"flow":bool,"news":bool}}`. channel_agreement: technical.direction=="long", flow.flow_bias=="long", event_signal.score>0.

## 보완 사항 (v2 기존 절 수정)
- **§17 FeaturePack** 에 `noise_floor_stop_pct: float|None`(= atr_pct_1h, 손절 최소거리)와 `noise_ok_2pct: bool|None`(= 1.5·atr_pct_1h < 2.0) 추가. **RiskPack** 에 `exec_cost_pct: float|None` = impact_bps_est/100 + spread_bps/200 + 0.20(왕복 수수료) 추가.
- **technical 규칙**: `noise_check = feature_pack.noise_ok_2pct ∧ stop_pct ≥ feature_pack.noise_floor_stop_pct`; noise_check false 면 confidence ≤ 0.50. 목표: `tp_pct = min(4.0, max(2·stop_pct, 1.5·stop_pct + 0.75))` (왕복비용 0.30% 반영 순손익비 ≥ 1.5 보장).
- **§19 G1** 노이즈 조건은 코드가 다시 계산한다: `fp.noise_ok_2pct is True ∧ tech.stop_pct ≥ fp.noise_floor_stop_pct` (LLM 의 noise_check 와 AND). **G3** 실행비용: `risk_pack.exec_cost_pct > 0.25·stop_pct` → 탈락. 손절은 기술분석 후보보다 넓히지 않는다.
- **§20.4 runner**: `system_prompt` 는 `skills.render(role, max_tokens=24000)`(역할에 배정된 스킬이 잘리지 않게; 시스템 프롬프트는 1h 캐시라 비용 미미). 테스트: 모든 역할의 스킬 전체가 렌더에 포함.
- **§21 JournalState** 에 `entry_verdicts: dict[str, dict]`, `thesis_history: dict[str, list[dict]]`, `prev_assumption_status: dict[str, list[str]]`, `news_risk_after_entry: dict[str, list[str]]`, `gate_stats_today: dict[str, int]`, `halts_today: list[str]` 추가(`health_history` 는 thesis_history 에서 파생 유지). 청산 시 해당 심볼 항목은 저널 호출 후 정리.
- **cgo_60 임계값**(모든 프롬프트·스킬 공통, % 단위): ≥ +3 → 차익 실현 압력/탐욕 측, ≤ −3 → 손실 보유자 다수(투매 후 반등 가능성).
- 프롬프트 길이: 700~1900 단어 허용(시스템 프롬프트는 캐시됨).
- **G4 실행비용 재검사(MONEY-7)**: `g4_final(..., *, exec_cost_pct: float|None = None)`. clamp_stops·enforce_net_rr 로 **최종** 손절거리가 정해진 뒤 `exec_cost_pct > 0.25 × 최종 손절거리(%)` 이면 HOLD(`exec_cost_too_high`). G3 는 조여지기 전 손절로 판단하므로 head·tech·순손익비 조임 뒤 다시 본다. cycle 은 해당 종목 `risk_pack.exec_cost_pct` 를 넘긴다. **G3 는 자기가 잰 실효 비용**(`max(팩 값, impact·주문액/깊이 충격/100 + max(팩·스캐너 스프레드)/200 + 왕복 수수료)`)**이 팩 값보다 크거나 팩 값이 None 이면 `risk_pack.exec_cost_pct` 를 그 값으로 올려 둔다**(낮추지 않음; 불변 객체면 생략) — 그래서 G4 는 G3 보다 약한 비용(스캐너 스프레드 누락)이나 None(재검사 생략)으로 최종 손절을 보지 않는다. g4_final 에 None 이면 검사 생략(기존 동작), 유한하지 않은 값은 무시하고 notes 에 남긴다.
- **역할 토큰 예산(CLAUDE-API-4)**: Opus 5.5 는 thinking 을 끌 수 없고 thinking 토큰이 `max_tokens` 에 포함된다(thinking 이 켜진 다른 티어도 토큰 계산은 같다)(§20.2 표의 값은 출력 JSON 만 보고 잡은 것이라 `stop_reason=max_tokens` 기권이 잦다). 새 값: technical 6000, derivatives_flow 8000, news_sentiment 8000, bull 8000, bear 8000, risk_manager 12000(medium, 65s), head 16000(**effort medium** — Opus 5.5 기본값, 'high' 는 사이클 기한 안에 끝나지 않을 만큼 오래 생각한다; 120s), journal 6000. 타임아웃은 bull·bear 50s, risk_manager 65s 로 줄였다 — 순차 4단계 타임아웃 합(35+50+65+120 = 270s)이 실제 LLM 예산(사이클 기한 300s − 예비 25s = 275s, 스캔 시간은 여기서 더 빠진다) 안에 들어야 앞 단계가 타임아웃까지 가도 총괄(유일하게 BUY 하는 단계)이 자기 몫을 받는다. 다른 RoleSpec 필드는 그대로. 그리고 실제 상한은 백엔드 `set_deadline(기한 − 경과 − 25s)` 가 각 호출 타임아웃을 남은 시간으로 자르고 `should_stop` 이 단계 경계에서 멈추는 것으로 보장된다(head 는 앞 단계가 오래 걸리면 120s 보다 짧게 잘릴 수 있다). 예산 사전검사는 `max_tokens` 전부를 출력 비용으로 잡으므로 호출당 예상 비용이 커진다.
- **뉴스 점수 척도 맞춤(CONTRACT-8)**: 스키마는 `NewsEvent.score ∈ [-1,1]` 만 강제하므로, 파이프라인은 news_sentiment 출력을 받자마자, 그리고 `NewsStore.add_events` 가 저장할 때 모든 점수를 (−1.0, −0.66, −0.33, 0.0, 0.33, 0.66, 1.0) 중 가장 가까운 값으로 맞춘다(`pipeline.snap_news_score`; 같은 거리면 낮은 쪽, 값이 바뀌면 DEBUG 로그). 뉴스 위험 심볼(`score ≤ −0.66 ∧ ¬stale`) 판정·저장·토론 payload 는 맞춘 값을 쓴다 — 예: −0.65 → −0.66(뉴스 위험), 0.1 → 0.0.
- **중단·재개·하트비트 이벤트 종류**: `EventKind` 에 `halt`, `halt_lifted`, `heartbeat` 추가(기존 값 유지). cycle 의 G0 중단 알림은 `kind="halt"`(`⛔ 신규 진입 중단 [mode]` + 코드별 한국어 사유 + 자본, `extra.halt_codes`), 해제는 `kind="halt_lifted"`(`✅ 신규 진입 재개 [mode]` + 해제된 코드). main 의 4시간 하트비트는 `kind="heartbeat"`(`💓 하트비트 [mode]`): 자본, 당일 손익(자본 − 당일 시작자본), 보유 포지션별 미실현 %(`last_mark` 기준, 네트워크 없음), 노출 %(보유 평가액/자본), 오늘 LLM 비용. 구버전 이벤트(`daily_summary`+`extra.heartbeat`, `error`+`extra.code="halt"`, `startup`+`extra.event="halt_lifted"`)도 같은 모양으로 그린다.
- **잠금 정체 판정(PERS-8)**: `bot.lock` 은 기록된 PID 가 죽었을 때뿐 아니라 `ts` 가 `max(3 × cycle_minutes × 60, 1800)` 초보다 오래됐을 때도 낡은 잠금으로 보고 가져온다(host/ts 없는 구버전 파일은 PID 규칙만). 기록된 `host` 가 내 호스트와 다르면(공유 DATA_DIR — NFS·한 볼륨의 두 컨테이너) PID 검사는 의미가 없으므로 **`ts` 만** 본다: 신선하면 `LockHeldError`(가져오지 않음), 오래됐거나 없으면 가져온다. 호스트 이름은 획득 시점 값으로 고정해 기록·비교한다(실행 중 hostname 변경 — macOS Bonjour·DHCP — 으로 refresh 가 `LockLostError` 를 내지 않게). `ts` 쓰기가 마지막 성공 뒤 `stale_after_s × LOCK_REFRESH_FAIL_SHARE(0.5)` 이상 계속 실패하면 refresh 는 `LockLostError`(치명 종료) — 다른 인스턴스가 낡은 잠금으로 가져가기 전에 스스로 멈춘다(그 전의 실패는 WARNING 후 재시도). 루프는 사이클마다, 그리고 사이클 사이 대기 중에도 최대 5분(`LOCK_REFRESH_S`)마다 `ts` 를 갱신하고, 대기 중 잠금을 잃으면(`LockLostError`) 치명 오류로 종료한다. `status` 는 낡은 잠금을 `낡음: <사유>` 로, 다른 호스트의 신선한 잠금을 `실행 중(다른 호스트 <host>)` 로 표시한다.
- **status 는 설정 없이도 동작**: `stop`/`clear-stop` 처럼 `load_settings` 가 실패해도(키 없음 등) `config.read_env()`(공개; `_read_env` 는 별칭)에서 `DATA_DIR` 만 풀어 파일 상태를 출력한다(stderr 에 설정 경고, 종료 코드 0).
- **Fear & Greed 연결**: `--offline-fixture` 가 아닌 실행은 `sentiment_sources.FearGreedFetcher`(캐시 `<DATA_DIR>/cache/fear_greed.json`, TTL 1h, 실패 시 24h 안의 캐시 또는 None)를 `Scanner(fear_greed_fetcher=...)` 로 넣어 composite_sentiment 에 쓴다. 오프라인 픽스처는 None(네트워크 0). 기본 `default_http_get` 은 **전체** 소요 시간을 `timeout_s` 로 자른다(데몬 스레드 + 본문 읽기 기한; urllib 의 timeout 은 소켓 연산별이고 DNS 에는 걸리지 않아 바이트를 흘리는 서버가 동기 스캔을 무한정 붙잡을 수 있다). 받기·파싱 실패 뒤 `retry_after_failure_s`(기본 1800s) 동안은 다시 받지 않는다(실패도 캐시).
- **중단 알림 상세 길이**: `kind=halt` 메시지의 `상세:` 줄은 `HALT_DETAIL_MAX_CHARS = 500` 자까지 싣는다(cycle 이 사유를 500자로 자르고, 구 `error` 형식도 500자를 실었다 — 여러 중단 코드의 뒤쪽 사유가 잘리지 않게).
- **경보 이벤트(레짐·뉴스 위험·포지션 점검) + notify-test**: `EventKind` 에 `alert` 추가(기존 값 유지), `extra.alert` 로 분기. `regime`(`🌐 레짐 전환 [mode]`: 이전 → 새 판정, 규칙 변화 — fused 사이즈 ×0.5·확신도 하한 0.75 / macro_alarm 신규 진입 금지 / normal 기본 규칙, BTC 충격, NAME 알람 종목), `news_risk`(`📰 뉴스 위험 [mode] SYMBOL`: event_type, 7단계 척도 점수, 요약, 보유 여부, 조치 — G1 신규 진입 차단, 보유면 리스크 관리자 점검·총괄 플래그 여부), `position_health`(`🩺 포지션 점검 [mode] SYMBOL`: 건강도, 레드라인 적중, 리스크 조치, 가정 상태 개수, 이번 사이클 총괄 결정·실행 여부). cycle 은 상태·저널 저장 **뒤** 판정하고 `<DATA_DIR>/alert_state.json`(마지막 레짐 판정, `심볼|헤드라인id` → 시각, 심볼 → 조건 서명)을 먼저 저장한 다음 기존 비동기 Notifier 로 보낸다(판정·저장·전송 실패는 `errors` 에만 남고 사이클을 멈추지 않는다). 레짐: 기록된 판정과 다를 때 한 번(기록이 없는 첫 실행은 normal 이 아닐 때만, 재시작해도 반복 없음). 뉴스 위험: `news_risk_symbols` 중 SYMBOLS·스캔 유니버스·보유 종목, 심볼+헤드라인 id(이벤트가 없으면 `risk_event`)당 24h 에 한 번. 포지션 점검: 리스크 관리자 건강도 ≤4 또는 코드 레드라인 적중일 때, 조건 서명(`low` + 현재값을 뺀 레드라인 규칙 목록)이 바뀔 때만(해소되면 기록 삭제, 청산된 심볼 정리; 리스크 관리자 기권이면 건강도 조건은 지난 판정 유지). `main notify-test`: `config.read_env` 에서 `TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID`·`MODE` 만 읽어(API 키 없이 동작) `TelegramTransport` 로 `✅ 텔레그램 연결 테스트 [mode] — 이 메시지가 보이면 거래 알림이 정상 전송됩니다` 를 동기 전송, 성공 0 / 실패 1(토큰 가린 오류) / 값 없음 2(한국어 안내).
- **경보 리뷰 후속(중복·진동 방지)**: 레짐 경보는 `verdict` 가 아니라 G3 가 적용하는 실효 판정(macro_alarm / `verdict=="fused"` 또는 `fused is True` → fused / 그 밖 normal)으로 판정한다(BTC 데이터 실패로 verdict 만 normal 이고 fused=True 면 FUSED 규칙 그대로 — `raw_verdict` 와 다르면 '스캐너 판정' 줄). 직전 판정으로 되돌아가는 전환은 마지막 레짐 경보 뒤 `REGIME_FLAP_COOLDOWN_MS`(1h) 동안 보류하고 그 뒤에도 다르면 알린다(`alert_state.json` 의 `regime_prev`·`regime_ts`). 보유 종목에 새 NAME 알람이 생기면 판정이 같아도 `verdict_changed=false` 레짐 경보(`🌐 NAME 알람(보유 종목)`)를 보낸다(심볼당 한 번 — `name_alarm` {심볼: 시각}, 해제 뒤 1h 가 지나야 다시). 뉴스 위험의 헤드라인은 그 심볼의 위험 이벤트(점수 ≤ −0.66, 묵지 않음)만 쓰고 없으면 `risk_event` 키(중립 기사를 근거로 보여주지 않음), 같은 심볼을 24h 안에 이미 알렸으면 `risk_event` 키로 다시 알리지 않는다(이벤트가 위험 심볼보다 먼저 만료). 포지션 점검 기록은 {low, red, health, act}: 건강도 ≤4 로 진입, 경보 중이면 ≥6 이어야 해소, 마지막으로 알린 건강도보다 2 이상 더 떨어지거나 리스크 조치가 hold→trim→exit 로 강해지거나 새 레드라인 규칙이 적중하면 다시 알린다(`악화:` 줄); 개선만 된 변화는 기록만 고친다. `PositionContext.red_lines_checked` 가 False(파이프라인이 레드라인 평가 전에 끝남·평가 실패)면 레드라인 조건은 지난 기록을 유지한다. `alert_state.json` 은 tmp → fsync → replace → 디렉터리 fsync 로 저장하고(실패 시 tmp 삭제), 읽기 실패(형식 오류·RecursionError 포함)는 예외 없이 빈 상태로 시작한다.
- (Windows 호환) runner 는 프롬프트 파일의 줄바꿈을 LF 로 정규화한다(CRLF 체크아웃이어도 시스템 프롬프트 바이트가 OS 와 무관). `.gitattributes` 로 prompts/·skills/ 는 LF 체크아웃. 스킬 파일은 `SKILL.md` 철자를 정확히 비교(대소문자 무시 파일시스템 대비). Windows 의 PID 생존 확인은 `OpenProcess`(`os.kill(pid, 0)` 은 Windows 에서 Ctrl+C 전송).
- **Windows 운영 점검 반영(WINRT)** — `docs/workflows/tradingbot-windows-audit.js` 의 확인된 결함. 앞의 항목과 충돌하면 이 항목이 우선한다.
  - **파일 교체·삭제·추가 쓰기**(`tradingbot/fsutil.py`): Windows 공유 위반(`PermissionError`, winerror 5/32/33 — 대시보드·`status`·백신·동기화 프로그램·Excel 이 파일을 열고 있음; winerror 없는 `open()` 의 `PermissionError` 는 Windows 에서만 같은 것으로 본다)이면 0.05s 부터 두 배씩(최대 1s) 기다리며 `budget_s`(기본 3s) 동안 다시 시도한다. 그 밖의 오류·POSIX 는 바로 올린다(리눅스 동작 불변). 모든 tmp→replace 저장, 잠금·제어 파일 삭제, JSONL 추가 쓰기(바이너리, LF)가 이것을 쓴다. `Portfolio.save` 는 공유 위반에 `SAVE_SHARING_BUDGET_S = 10s` 를 쓰고(그 뒤에는 기존 6회 재시도 없이 실패 — 이미 기다렸다), 다른 OSError 는 기존 6회 규칙. 비용 원장은 추가 쓰기가 공유 위반으로 `LEDGER_SHARING_BUDGET_S = 1s` 안에 안 되면 `cost_ledger.pending.jsonl` 에 쓰고(지출을 잃지도 봇을 멈추지도 않는다), 복원·`status`·대시보드는 두 파일을 더한다. 두 파일 모두 못 쓰면 기존대로 치명.
  - **사이클 순서(§4 개정)**: 코드 청산(손절·익절·시간손절) 매도는 LLM 호출·뉴스 조회 없이 연달아 한다. `_book_sell` 은 사후분석을 `run.pending_reviews` 에 넣기만 하고, 코드 청산 단계 뒤 `post_trade` 단계가 거래 순서대로 `_post_trade`(LLM 사후분석·저널 기록·심볼 정리) + '📝 사후분석' 알림을 한다(거래별 실패는 서로 막지 않음; `_FATAL` 은 전파). 총괄 SELL 도 결정 처리 뒤 같은 단계. 그다음 뉴스 조회(스캐너가 `scan(include_news=False)` + `fetch_news(symbols)` 를 지원하면 — 대역은 예전처럼 스캔 안에서), 뉴스 가용성 반영, 일일 리뷰 순. 일일 리뷰의 '첫 가동' 판정은 어제 이전에 닫힌 거래만 본다(이번 사이클 청산 제외).
  - **루프 대기**: 기본 대기는 `stop_event` 를 `WAIT_POLL_S = 1s` 조각으로 기다린다(Windows CPython ≤3.13 은 `Event.wait(timeout)` 이 Ctrl+C 로 깨지 않는다 — gh-125541). 마감은 monotonic 과 벽시계 중 먼저 오는 쪽. `_wait_with_lock_refresh(..., clock=)` 는 남은 대기를 `시작 + wait_s` 벽시계 마감으로 자르고, 마감을 `WAKE_OVERDUE_S = 120s` 넘게 지났으면(절전 복귀) `WAKE_SETTLE_S = 20s` 만 더 기다린 뒤 사이클을 돌린다. 시계가 상수인 주입 시계에서는 조각이 예전과 같다(300/300/205). 사이클 시작 간격이 `CYCLE_GAP_FACTOR(2) × 주기` 를 넘으면 `kind=alert`, `extra.alert="cycle_gap"`(`gap_s`, `cycle_minutes`, `positions`) — '그동안 손절·익절·시간손절 미집행'. 신호: SIGINT·SIGTERM·SIGBREAK(Windows Ctrl+Break). 콘솔 닫기·작업 끝내기·로그오프는 잡지 않는다(즉시 종료).
  - **Windows 절전·콘솔**: `run` 은 `keep_system_awake()`(Windows: `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`, 끝나면 `ES_CONTINUOUS`; 실패는 경고만, 다른 OS 는 아무것도 안 함)로 `run_loop` 를 감싼다. `cli()` 는 루트 로거를 `QueueHandler`(무제한 큐) → 전용 데몬 스레드(`_LogPump`) → 콘솔 핸들러로 구성한다(conhost 선택 모드가 콘솔 쓰기를 막아도 거래 스레드는 멈추지 않음; `captureWarnings`, `raiseExceptions=False`, 종료 시 2초 안에 비움). `LOG_FILE`(Settings.log_file, 상대 경로는 DATA_DIR 기준, `none`/빈 값이면 없음)은 `run` 만 잠금 획득 뒤 붙이고(UTF-8 BOM 한 번, 회전 없음) 끝나면 뗀다.
  - **잠금**: 쓰기는 `fsutil.replace`. refresh 실패는 마지막 성공 뒤 `stale_after_s × 0.5` 이상이고 **연속 2회 이상**일 때만 `LockLostError`(절전 복귀 직후 한 번의 일시 실패는 원래 예외 — 다음 갱신이 판단). acquire 는 O_EXCL 로 만든 경우 다시 쓰지 않고, 재진입 쓰기 실패는 경고만. release 삭제 실패는 경고만. `_cmd_run` 은 acquire 의 OSError 를 안내 문구 + 종료 코드 1 로. Windows(그리고 `pid_alive` 를 주입하지 않았을 때)는 살아 있는 PID 가 잠금을 쓴 프로세스인지 본다(`pid_reused`): 실행 파일 이름이 잠금의 `image` 와 다르거나 생성 시각이 `started_ts + 60s` 보다 늦으면 낡은 잠금(판단 불가면 지킨다). 잠금 내용에 `image`(Windows, 내 실행 파일 이름 소문자)가 추가된다. 낡은 잠금을 가져오면 `took_over`({pid, reason, ts})를 남기고 시작 알림 사유에 '이전 실행이 정상 종료되지 않았습니다'를, 보유가 있는데 마지막 사이클이 2주기보다 오래됐으면 그 경과를 붙인다. `LockHeldError` 문구는 '실행 중인 봇이 정말 없다면 그 파일을 지우고 다시 실행'을 안내한다.
  - **제어 파일**: `stop`/`clear-stop` 은 절대 경로(`os.path.abspath`, resolve 아님)를 쓰고 출력하며, 그 폴더의 잠금에 실행 중인 봇이 없으면 stderr 경고(종료 코드 0 유지), 있으면 `대상 봇: pid=…`. 쓰기 실패는 안내 + 종료 코드 1. STOP 작성 뒤 CLEAR 삭제 실패, CLEAR 작성 전 STOP 삭제 실패는 경고만(대시보드도 같음 — 500 은 자기 파일 쓰기 실패일 때만). 사이클은 **CLEAR 보다 오래된(ts ≤) STOP 은 적용하지 않고 지운다**(낙폭 정지 해제가 옛 STOP 에 덮이지 않게).
  - **.env·설정**: `.env` 는 바이트로 읽어 UTF-8(BOM 제거)·UTF-16 BOM(경고 후 사용)을 풀고, NUL 바이트(BOM 없는 UTF-16 덧붙임)·UTF-8 아님(cp949 등)은 줄 번호를 담은 `ConfigError`. python-dotenv 가 해석하지 못한 줄(닫히지 않은 따옴표 — 역슬래시로 끝나는 따옴표 값이 다음 줄들을 삼킴)도 `ConfigError`. `read_env(lenient=True, warnings=[...])`(status·stop·clear-stop·notify-test)는 같은 문제를 경고로 바꾸고 최대한 읽는다(로캘 인코딩 → utf-8 replace). 환경변수가 다른 값으로 `.env` 를 덮으면 이름만 프로세스당 한 번 경고. `parse_data_dir`: `DATA_DIR`·`LOG_FILE` 에 제어 문자·`"`·`%VAR%` 가 있으면 `ConfigError`. `Settings.scanner_env` 에 `SCANNER_*` 원문을 담아 `build_components` 가 `Thresholds.from_env(scanner_env)` 로 스캐너에 넘긴다(비었으면 예전처럼 os.environ). `run` 시작 로그에 읽은 `.env` 절대 경로(없으면 경고, `.env.txt` 힌트)와 데이터 폴더 절대 경로. `status` 의 데이터 폴더도 절대 경로.
  - **CLI**: `run --offline-fixture` 는 설정 검증도 `AGENT_BACKEND=mock` 으로(API 키 불필요). `run --mock-scenario conservative|approve` 는 `MOCK_SCENARIO` 보다 우선하며 mock 백엔드에서만(아니면 `ConfigError`). 실제 `build_components` 는 `MODE=live` + `AGENT_BACKEND=mock` 을 `ConfigError` 로 거부한다(남은 환경변수로 가짜 판단 실주문 방지). live 확인 전에 판단 백엔드·종목·데이터 폴더를 한 줄 보여 준다. 시작 알림은 mock 이면 '판단: mock(가짜 응답[, 시나리오 …]) — 실제 Claude 판단이 아닙니다'.
  - **거래소(ccxt)**: `_configure_exchange` 는 `fetchCurrencies=False`·`fetchMargins=False`(키가 있으면 `load_markets` 가 시계 보정보다 먼저 서명 SAPI 호출을 해서 PC 시계가 1초 이상 빠르면 시작부터 -1021). `_retry` 는 `InvalidNonce`(-1021)면 한 번 `load_time_difference()` 로 재보정하고 기다리지 않고 다시 시도한다(재보정 불가면 기존 백오프). `_submit` 도 -1021 이면 재보정 뒤 clientOrderId 조회(중복 주문 없음 — -1021 은 체결 엔진 전에 거부). 현물·선물 클라이언트의 `throttle` 은 마지막 요청 시각이 미래면(PC 시계 역행) 지금으로 당겨 최대 `rateLimit` 만 기다린다(속성이 없는 대역은 그대로).
