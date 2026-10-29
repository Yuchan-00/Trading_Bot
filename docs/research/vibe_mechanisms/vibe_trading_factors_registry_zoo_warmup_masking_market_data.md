# Vibe-Trading factors (registry/zoo/warmup/masking), market_data + ccxt/binance candle pipeline, quantlib module inventory, and which indicator formulas to copy into a Binance spot 15m bot's indicators module (with Qlib/alpha101/GTJA attribution requirements)

# Vibe-Trading: factors, market data, quantlib — report for the 15m Binance spot bot

All paths are absolute. Nothing was modified. Note: there is no `agent/src/br_data` ccxt code — `br_data` is Brazilian (B3/CVM/BCB) data sync, and `agent/src/providers` is the **LLM** provider layer (claude_cli, openai_codex, llm.py). The real ccxt/binance candle code lives in `agent/backtest/loaders/` (`ccxt_loader.py`, `binance_loader.py`, `okx.py`, `base.py`, `registry.py`) and `agent/src/trading/connectors/binance/sdk.py`; `agent/src/market_data.py` is the façade that routes to them.

---

## 1. Factor framework (`/home/user/hkuds/vibe-trading/agent/src/factors/`)

Files: `base.py` (499 lines, operator algebra), `registry.py` (482, AST-scan + compute + masking), `_backend.py` (bottleneck/numpy backend), `data_availability.py` (PIT availability contract), `bench_runner*.py`, `compare_runner.py`, `factor_analysis_core.py`, `cli_handlers.py`, and `zoo/` with 5 sub-zoos: `alpha101/` (101 modules), `gtja191/` (191), `qlib158/` (157 modules = 9 K-bar + 29 rolling features × 5 windows {5,10,20,30,60}), `academic/` (12), `fundamental/` (20). `__init__.py` says: "Alpha Zoo: 5 zoos ... × 460 alphas".

### 1.1 Panel contract
`base.py:3-6`: "Operators all act on **wide** ``pd.DataFrame`` where ``index = trading_date`` (DatetimeIndex) and ``columns = instrument_code`` (str). The factor compute contract returns a DataFrame of the same shape — raw scores, NaN preserved in warmup / missing data; +/- inf is forbidden (registry rejects it)."

A panel is `dict[str, pd.DataFrame]` keyed `open/high/low/close/volume/vwap/amount` (+ `fund:*`, `val:*`, `sector`). Every alpha module exposes `compute(panel) -> pd.DataFrame` (`AlphaCompute` protocol, `base.py:66-70`).

### 1.2 How a factor declares inputs and warmup
Each `zoo/<zoo>/<id>.py` carries a dict literal `__alpha_meta__` which the registry AST-parses **without importing** (`registry.py:151-187`, `load_alpha_meta_from_py`: `ast.literal_eval(meta_node)` then `AlphaMeta(**raw)`). Schema (`registry.py:90-106`, pydantic `extra="forbid", frozen=True`):

```python
id: str = Field(pattern=r"^[a-z][a-z0-9]+_[a-z0-9_]+$")
nickname: str | None = None
theme: list[Theme]              # momentum/reversal/volume/volatility/quality/value/liquidity/microstructure/sentiment/growth/leverage
formula_latex: str
columns_required: list[PanelColumn]   # open/high/low/close/volume/vwap/amount or fund:*/val:*
extras_required: list[str] = []
requires_sector: bool = False
universe: list[Universe]        # equity_us/equity_cn/equity_hk/equity_in/equity_kr/crypto/futures
frequency: list[str]
decay_horizon: int = Field(ge=0, le=512)
min_warmup_bars: int = Field(ge=0)
notes: str = ""
```

Concrete example (`zoo/qlib158/rsv5.py`):
```python
__alpha_meta__ = {
    'id': 'qlib158_rsv5', 'theme': ['momentum'],
    'formula_latex': '(close - ts_min(low,5)) / (ts_max(high,5) - ts_min(low,5))',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in', 'equity_kr'],
    'frequency': ['1d'], 'decay_horizon': 5, 'min_warmup_bars': 5,
}
def compute(panel):
    c = panel['close']; h = panel['high']; lo = panel['low']
    hh = ts_max(h, 5); ll = ts_min(lo, 5)
    return safe_div(c - ll, hh - ll)
```
`min_warmup_bars` is **declarative only** — it is the author's statement of the longest lookback (e.g. alpha101_001 declares 25 = 20-bar std + 5-bar argmax; gtja191_001 declares 7 = 1 lag + 6-bar corr; carhart_mom 252). The runtime does not read it to trim output; the operators themselves produce NaN during warmup because every rolling op uses `min_periods == window` (`ts_mean`, `ts_std`, `ts_max`, `ts_rank`, `decay_linear`, `ts_corr`…: see `base.py:265-316`). Benchmarks use `min_warmup_bars` to decide how many leading bars to drop / how much history to request.

Module path is derived, never read from data: `registry.py:17-19` "Module path derivation: ``f\"src.factors.zoo.{zoo_id}.{alpha_id_short}\"``... We never honour a ``py_module`` field from any data file." Load-time: `Registry._scan()` walks `zoo/*/*.py` (skipping `_`-prefixed), validates ids with `^[a-z][a-z0-9_]{0,31}$`, caps source size at 200 KB, records duplicates/failures in `health()`. `get_default_registry()` is a thread-safe singleton (`registry.py:464-475`).

### 1.3 Output masking (input-presence mask + sanity checks)
`Registry.compute()` (`registry.py:325-379`):
1. Pre-checks: `SkipAlpha` if any `columns_required` / `extras_required` missing from panel, or `requires_sector` and no `sector`.
2. Lazy import + `compute(panel)`; any exception → `RegistryError`.
3. **Input mask** (`registry.py:359-378`):
```python
# Enforce NaN contract: any bar where a declared dependency is missing
# must produce NaN in the output, regardless of the alpha's internal logic.
...
mask = pd.DataFrame(True, index=ref.index, columns=ref.columns)
for col in meta.get("columns_required", []):
    mask = mask & panel[col].notna()
for col in meta.get("extras_required", []):
    mask = mask & panel[col].notna()
if meta.get("requires_sector"):
    mask = mask & panel["sector"].notna()
if isinstance(result, pd.DataFrame):
    result = result.where(mask)
```
   i.e. the output cell at (t, symbol) is NaN whenever any declared input is NaN at that same bar. Note it masks only the *gap bar itself*, not the window after it (`base.py:19-20`: "the registry already masks the gap bar itself").
4. `_validate_output` (`registry.py:405-426`): must be a DataFrame, same shape as `panel["close"]`, **no ±inf** (`RegistryError`), and NaN ratio ≤ 0.95 (`"output >95% NaN"` error — this is how a too-short panel is surfaced).

### 1.4 NaN policy and the operator algebra (`base.py`)
Doctrine (`base.py:8-20`): "every operator propagates NaN; no silent ``fillna(0)``. A constant window for ``ts_corr`` / ``ts_cov`` returns NaN, not zero. A missing input is never replaced by a constant ... a full-window operator (``min_periods`` equal to its window) is NaN on every bar whose window holds the gap. The one exception ...: a recursive statistic skips a missing observation and continues from its last state — the GTJA ``SMA(A, n, m)`` written as ``.ewm(alpha=m/n, adjust=False)``".

Look-ahead ban (`base.py:28-29`): "``delta(df, d)`` requires ``d >= 1``; the negative-shift ``Ref(df, -n)`` form is intentionally absent." (`delta` raises `ValueError` for `d < 1`, `base.py:368-375`.)

Operators (all per-column rolling with `min_periods=n`): `rank` (cross-sectional pct rank, `axis=1`), `zscore`, `zscore_by_group`, `scale` (L1 to `a`), `ts_rank` (percentile of last value in window, numpy `sliding_window_view`), `ts_corr`, `ts_cov`, `ts_mean`, `ts_std` (ddof=1), `ts_slope` (OLS slope vs 0..n-1 via einsum), `ts_resi` (= x − (mean + slope·(n−1)/2)), `ts_max/min`, `ts_argmax/argmin` (0-based index from window start; bottleneck fast path with `(n-1) - bn_result` correction), `delta`, `decay_linear` (weights n..1 normalised, newest heaviest), `signed_power`, `observed_over` (presence-reach mask), `safe_div`, `vwap`.

Two helpers worth copying verbatim-in-spirit:
```python
def safe_div(a, b, eps=1e-12):   # base.py:450-462
    """Safe division: a / (b + eps * sign(b)). Where b == 0 exactly (or NaN), result is NaN — never silently inf or 0."""
def observed_over(*inputs: tuple[pd.DataFrame, int]):   # base.py:423-447
    seen = frame.notna().astype(float).rolling(n, min_periods=n).min().eq(1.0)
```
and the ternary guard pattern used by piecewise alphas (`zoo/alpha101/alpha_009.py`): compute the branch, then `return out.where(min5.notna() & max5.notna())` because "A NaN comparison is False, not NaN ... fabricating a signal during warmup instead of NaN."

`vwap()` for crypto (`base.py:480`): "``crypto``: prefer ``panel[\"vwap\"]`` if provided, else typical price" = `(O+H+L+C)/4`. ccxt candles carry no vwap, so this is what the zoo would use.

### 1.5 Data-availability / PIT contract (`data_availability.py`)
`build_availability_contract(panel, universe=...)` emits a JSON record per field with `availability_mode` (`bar_close` for price fields), `status` ∈ {verified, assumed, unknown, failed}, `pit_safe`, and a `non_null_ratio` validation. A built-in adapter exists for crypto: `"btc-usdt": _BuiltinAdapter("crypto", MarketContext("crypto", "global", "USDT", "UTC", "24/7"))` (`data_availability.py:79-82`). Useful pattern for the bot's data-quality gate: label every input with when it became knowable and refuse to call agents when a required field is `failed`.

### 1.6 Which zoo entries declare crypto
Only one: `zoo/academic/corr_rewire.py` (`"universe": ["equity_us","equity_cn","equity_hk","crypto"]`, warmup 142). Everything in alpha101 is declared `equity_us/in/kr`, gtja191 `equity_cn`, qlib158 the five equity universes, all with `frequency: ['1d']/['1D']`. So nothing in the zoo was validated on crypto or 15m bars — treat formulas as candidates, not as evidence.

---

## 2. Market data pipeline (`/home/user/hkuds/vibe-trading/agent/src/market_data.py` + `agent/backtest/loaders/`)

### 2.1 Façade: `fetch_market_data()` (`market_data.py:199-552`)
- Symbol → source detection with regex table `_SOURCE_PATTERNS` (`market_data.py:21-66`): `^[A-Z0-9]+-USDT$` → `"okx"`, `^[A-Z0-9]+/USDT$` → `"ccxt"`. `normalize_crypto_symbol()` (`market_data.py:89-93`) folds `BTCUSDT`, `BTC/USDT`, `BTC-USDT` into the canonical `BTC-USDT`.
- Fallback chain for crypto (`backtest/loaders/registry.py:239-240`): `"crypto": ["okx", "binance", "ccxt", "yfinance", "local"]` — "OKX first (native), then dedicated Binance, then generic CCXT / Yahoo." Env override `MARKET_DATA_ORDER_CRYPTO=binance,ccxt,...` rewrites the order (`get_source_order_override`).
- Walk (`_fetch_via_chain`, `market_data.py:284-410`): per source in the chain, instantiate loader, `loader.fetch(codes, start, end, interval=...)`, keep non-empty frames, **retry only the still-missing symbols** down the chain, record per-symbol diagnostics `{source, reason ∈ source_unavailable|fetch_failed|no_data}`. Caps attempts at `max_fallback_attempts=5`.
- Weekly/monthly built by resampling daily (`base.py:275-346`, `resample_bars`: first open / max high / min low / last close / summed volume, bar stamped with the last day in the period).
- Output per symbol: `cap_rows(records, max_rows)` (`market_data.py:168-186`) — even-stride downsampling with the last bar pinned, default 250 rows, plus `_provenance` (`source`, `fallback_used`, `volume_unit`, `adjustment`) and `_unresolved` / `_diagnostics`. `fetch_market_data_json` turns an all-unresolved result into `{"status":"error","error_code":"no_market_data",...}` so an agent loop can react.
- Accepted intervals (`backtest/runner.py:59`): `_VALID_INTERVALS = {"1m","5m","15m","30m","1H","4H","1D","1W","1M"}`; `tools/market_data_tool.py:31-48` canonicalises case except the `1m`(minute) vs `1M`(month) trap.

### 2.2 ccxt loader (`agent/backtest/loaders/ccxt_loader.py`, 517 lines) and Binance subclass
`binance_loader.py` subclasses it and pins `ccxt.binance` (spot) / `ccxt.binanceusdm` (swap) regardless of `CCXT_EXCHANGE`; config `{"enableRateLimit": True, "timeout": _CCXT_TIMEOUT_MS}` plus proxies from `HTTP(S)_PROXY/ALL_PROXY`.

Timeframes (`ccxt_loader.py:32-48`):
```python
_INTERVAL_MAP = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m","1H":"1h","1h":"1h","4H":"4h","4h":"4h","1D":"1d","1d":"1d","1W":"1w","1w":"1w","1M":"1M"}
_TIMEFRAME_DELTA = {"15m": pd.Timedelta(minutes=15), ... "1M": pd.Timedelta(days=31)}
```
Symbol parse (`:76-86`): `BTC-USDT` → `BTC/USDT` spot; `BTC-USDT-PERP` → `BTC/USDT:USDT` swap.

Timeouts/budgets (`:66-72`): `_CCXT_TIMEOUT_MS = positive_env_int("CCXT_TIMEOUT_MS", 15_000)`, `_CCXT_FETCH_BUDGET_S = positive_env_float("CCXT_FETCH_BUDGET_S", 60.0)`; comment: "ccxt had no request timeout and an unbounded paginated fetch with no retry budget, so a transient disconnect hung get_market_data for 10+ minutes."

Pagination (`_fetch_one`, `:442-517`):
```python
limit = _page_limit(exchange)        # 1000, or exchange.features['spot']['fetchOHLCV']['limit'] if lower (OKX 300, Kraken 720)
deadline = time.monotonic() + _CCXT_FETCH_BUDGET_S
for _ in range(200):
    check_budget(deadline, label, budget_s=_CCXT_FETCH_BUDGET_S)
    ohlcv = retry_with_budget(fetch_page, transient=ccxt.NetworkError, deadline=deadline, label=label)
    if not ohlcv: hit_page_cap = False; break
    all_rows.extend(ohlcv)
    last_ts = ohlcv[-1][0]
    if last_ts >= end_ms or len(ohlcv) < limit: hit_page_cap = False; break
    cursor = last_ts + 1
```
Normalisation (`:492-505`): `DataFrame(rows, columns=[timestamp,open,high,low,close,volume])`, `trade_date = to_datetime(timestamp, unit="ms")` (naive UTC) as sorted index, half-open filter `[start, end+1day)`, `pd.to_numeric(errors="coerce")`, `dropna(subset=OHLC)`. Completeness check (`:509-516`): if the loop stopped on the page cap and `df.index[-1] < end_dt - tolerance` → `ValueError("incomplete CCXT history ...")`. Note: ccxt timeframe `1M` is mapped but has no exact `Timedelta` (31 days) so the tolerance is approximate.

Retry helper (`base.py:406-460`, `retry_with_budget`): backoff `(0.5, 1.5, 4.0)`, `max_retries=3`, only the declared transient class is retried; "Anything not in ``transient`` propagates unchanged on the first occurrence". Terminal failure wrapped as `TimeoutError`.

OHLC sanity (`base.py:191-258`, `validate_ohlc`): drops/flags rows with `high < low`, high/low not bracketing open/close, or non-positive prices.

Funding-rate side path (perp only, `_fetch_perpetual` / `_fetch_funding_history`, `:326-440`): paginates `fetch_funding_rate_history(symbol, since, limit=1000)`, rounds settlement timestamps to the second ("Binance settlement timestamps carry millisecond jitter (e.g. 08:00:00.011)"), expects settlements at `_FUNDING_HOURS = {0, 8, 16}` UTC and raises if any bar at those hours lacks one. Maintenance brackets are **never fetched live** (needs a key) — supplied as a hash-verified artifact.

### 2.3 Caching (`backtest/loaders/base.py:467-931`)
Opt-in (`VIBE_TRADING_DATA_CACHE=1`), root `~/.vibe-trading/cache/loaders/<source>/<sha256>.parquet` + sidecar `.json` metadata (index columns/dtypes, frame attrs, version). Key = sha256 of `{version, source, symbol, timeframe, start_date, end_date, fields}` (`_LOADER_CACHE_VERSION = 9`, bumped whenever semantics change). Critical staleness rule (`loader_cache_range_is_final`, `:564-576`): "caching a range whose last bar is still forming (``end_date`` today or in the future) would pin a provisional bar and serve it on every later run. Only fully-elapsed days (strictly before today) are cacheable." Writes are atomic (`tmp` + `os.replace`), read/write failures are non-fatal (fall back to live fetch). Reads use duckdb `read_parquet`.

### 2.4 Staleness / spacing checks
- Candle-level: the `incomplete CCXT history` check above; OKX loader keeps only `confirm == "1"` bars unless that would leave nothing (`okx.py:327-331`) — i.e. it drops the still-forming candle.
- Series-level (`backtest/runner.py:1572-1640`): `_observed_spacing` = **median** of index diffs ("the median reports the regular part"), compared against `_INTERVAL_SECONDS[interval]` with `_SPACING_MISMATCH_RATIO = 1.5`; needs `_MIN_BARS_FOR_SPACING = 4`. Catches a 15m request that was actually served 1h bars.
- Order book (`tools/orderbook_depth_tool.py` docstring): Binance **spot** `GET /api/v3/depth` carries no timestamp, so ccxt returns `timestamp=None`; the tool substitutes local fetch time and sets `timestamp_source` "rather than silently passing a null-dated snapshot ... (an order book is only valid for seconds)". It rejects crossed books (`best_bid >= best_ask`), computes `normalized_imbalance = (bid_notional - ask_notional)/(bid_notional + ask_notional)` within N levels, and walks a 100-level ladder to price a `$10k` market order (`avg_price`, `slippage_bps`, `fully_filled`, partial fills always labelled).

### 2.5 Live Binance SDK connector (`agent/src/trading/connectors/binance/sdk.py`)
Public reads strip credentials: `_public_exchange()` = `_exchange(replace(cfg, api_key="", api_secret=""))` because "CCXT otherwise loads Binance currencies through a signed SAPI request when market loading sees an API key" (`:790-796`). `_exchange()` (`:799-836`) sets `options={"adjustForTimeDifference": True, "recvWindow": 10_000}`, `ex.set_sandbox_mode(cfg.is_testnet)`. `_assert_host()` (`:839-860`) fails closed: a paper profile must resolve to the testnet host, a live one to `api.binance.com`. `get_quote` → `fetch_ticker` (bid/ask/last/high/low/baseVolume/timestamp); `get_historical_bars` → `fetch_ohlcv(symbol, timeframe, limit)` with `_TIMEFRAME_MAP` (`:481-485`).

---

## 3. quantlib inventory (`/home/user/hkuds/vibe-trading/agent/src/quantlib/`)
`__init__.py` imports nothing on purpose ("a missing optional dependency in one domain cannot break the others"). 20 top-level modules + `valuation/` package (5) = the 25 keys of `tools/quantlib_tool.py:65-90 ALLOWED_MODULES` (23 if you ignore `valuation.contracts`/`artifact` plumbing):

| module | one-liner | bot relevance |
|---|---|---|
| `risk.py` | historical/parametric VaR, CVaR, drawdown series, ulcer/pain index, `max_drawdown_analysis`, Monte-Carlo GBM, GPD tail fit. Sign convention: "A loss is a positive number." | **yes** – daily-loss / drawdown ladder reporting |
| `timeseries.py` | ADF, cointegration, `compute_half_life` (ΔS on S lag, `-ln2/b`), OU fit, hedge ratio, Granger, `fit_garch` (needs `arch`), `fit_markov_regime` (needs `statsmodels`), heteroscedasticity/autocorr tests, VIF, `bootstrap_statistic`, `bootstrap_sharpe` | regime/vol forecast (optional deps) |
| `microstructure.py` | `roll_effective_spread` (`2*sqrt(-cov(ΔP_t,ΔP_{t-1}))`), `amihud_illiquidity` (`mean(|R_t|/DollarVol_t)`), `kyles_lambda`, `vpin` (volume-bucketed `Σ|V_B−V_S|/(N·V)`) | **yes** – liquidity gate from candles / taker-volume |
| `impact.py` | `fixed_slippage(bps=5)`, `linear_impact(coeff=0.1·V/ADV)`, `sqrt_impact(eta=0.5·σ·sqrt(V/ADV))`, `delayed_execution` | **yes** – paper-mode fill model |
| `multipletesting.py` | `probabilistic_sharpe_ratio`, `deflated_sharpe_ratio`, `benjamini_hochberg`, `probability_of_backtest_overfitting` (CSCV) | backtest honesty |
| `crossvalidation.py` | purged/embargoed k-fold, walk-forward, combinatorial purged splits, leakage detection | backtest |
| `var_backtest.py` | Kupiec POF, Christoffersen independence/conditional coverage, Basel traffic light | validate the risk module's VaR |
| `performance.py` | TWR, Modified Dietz, XIRR on irregular flows | P&L reporting with deposits |
| `portfolio.py` | HRP, inverse-variance, ERC weights | sizing across ≤3 positions (marginal) |
| `combination.py` | IC/ICIR walk-forward factor composites | if you ever combine indicator scores |
| `neutralization.py` | cross-sectional OLS residuals, PCA factors | no (needs wide cross-section) |
| `factormodel.py` | style exposures / factor returns / drift ("IT IS NOT BARRA") | no |
| `eventstudy.py` | AR/CAR/CAAR, Patell, BMP | maybe for news-event studies |
| `attribution.py` | Brinson-Fachler + Carino linking | no |
| `copula.py` | Clayton/Gumbel/Frank/Gaussian, tail dependence | no |
| `volatility.py` | Heston pricing | no |
| `options.py` | Black-Scholes, Greeks, IV, barriers | no |
| `fixedincome.py` | bond pricing/duration/curve fit | no |
| `credit.py` | Altman Z, Merton/KMV, CDS | no |
| `fundmath.py` | PE fund maths (XIRR, DPI/TVPI, waterfalls, PME) | no |
| `valuation/{dcf,comps,threestatement,contracts,artifact}.py` | equity valuation; "A missing input makes the model NOT RUNNABLE" | no |

Design principle worth stealing (`quantlib/__init__.py:3-8`): formulas previously "carried as markdown inside skills, where the LLM retyped them into throwaway Python on every run" are now "one implementation, pinned by tests, called by both the skills and the engines." For our bot: agents never compute indicators; `indicators.py` computes them deterministically and the prompt receives numbers.

---

## 4. Indicator/factor definitions worth copying into the bot's `indicators.py`
The bot trades ~3 symbols on 15m bars; cross-sectional `rank()`/`zscore()` over 3 names is meaningless, so take only **time-series, single-symbol** definitions. Windows below are in *bars*; the zoo's 5/10/20/30/60 are daily — re-tune for 15m (e.g. 16 = 4h, 96 = 1 day, 672 = 1 week). All should return NaN during warmup (`min_periods=window`), never 0.

**K-bar shape (qlib158, warmup 1, no parameters):**
- `KMID = (close − open)/open`
- `KLEN = (high − low)/open`
- `KSFT = (2·close − high − low)/open` (close vs. bar midpoint)
- `KUP = (high − max(open,close))/open`, `KLOW = (min(open,close) − low)/open` (wicks)
- alpha101 #101: `(close − open)/((high − low) + 0.001)` — body/range ratio, the simplest "conviction" of the bar
- alpha101 #54: `−((low−close)·open⁵)/((low−high)·close⁵)`

**Rolling price features (qlib158 family, window w):**
- `ROC_w = close_{t−w}/close_t` (note Qlib's inverted convention; for the bot use `close_t/close_{t−w} − 1`)
- `MA_w = mean(close,w)/close`; `STD_w = std(close,w)/close` (coefficient of variation — a normalised vol)
- `BETA_w = Slope(close,w)/close` (OLS slope vs time, normalised); `RSQR_w = corr(close, t, w)²` (trend quality); `RESI_w = (close − fitted_last)/close`
- `MAX_w = max(high,w)/close`, `MIN_w = min(low,w)/close`, `QTLU_w = quantile_0.8(close,w)/close`, `QTLD_w = quantile_0.2(close,w)/close`
- `RANK_w = ts_rank(close,w)` (percentile of current close within window)
- `RSV_w = (close − min(low,w))/(max(high,w) − min(low,w))` (stochastic %K raw)
- `IMAX_w = argmax(high,w)/w`, `IMIN_w = argmin(low,w)/w`, `IMXD_w = IMAX − IMIN` (recency of extremes)
- `CNTP_w = mean(1[close>close_prev], w)`, `CNTN_w`, `CNTD_w = CNTP − CNTN` (up-bar ratio)
- `SUMP_w = Σmax(Δclose,0)/Σ|Δclose|`, `SUMN_w`, `SUMD_w = SUMP − SUMN` — SUMP is exactly RSI/100 in simple-average form

**Volume features (qlib158):**
- `VMA_w = mean(vol,w)/vol`, `VSTD_w = std(vol,w)/vol`
- `CORR_w = corr(close, log(vol+1), w)`; `CORD_w = corr(close/close_prev, log(vol/vol_prev + 1), w)` (price–volume agreement)
- `WVMA_w = std(|ret|·vol, w)/mean(|ret|·vol, w)`
- `VSUMP_w/VSUMN_w/VSUMD_w` (volume-change RSI analogue)

**Wilder-smoothed classics (gtja191, `SMA(x,n,m) ≡ x.ewm(alpha=m/n, adjust=False).mean()`):**
- KDJ: `RSV9 = (close − min(low,9))/(max(high,9) − min(low,9))·100`; `K = SMA(RSV,3,1)`, `D = SMA(K,3,1)`, `J = 3K − 2D` (gtja191 #28 = J, #96 = D)
- MACD (gtja191 #89): `DIF = EMA13 − EMA27` with `alpha = 2/13`, `2/27`; `DEA = SMA(DIF,10,2)`; factor = `2·(DIF − DEA)`

**Single-asset alpha101 pieces (no `rank`):**
- #9/#10 momentum-vs-reversal switch: if `min(Δclose,5) > 0` → `Δclose`; elif `max(Δclose,5) < 0` → `Δclose`; else `−Δclose` (trend persists / reverts gate) — must mask with `.where(min5.notna() & max5.notna())`
- #12: `sign(Δvolume₁)·(−Δclose₁)` (volume-confirmed reversal)
- #23: `(mean(high,20) < high) ? −Δhigh₂ : 0`
- #46/#49/#51: curvature `x = ((close_{t−20} − close_{t−10})/10) − ((close_{t−10} − close)/10)`; thresholds 0.25/−0.1/−0.05 decide ±1 vs `−Δclose₁`
- #53: `−Δ₉[((close−low) − (high−close))/(close−low)]`
- #24: 100-bar regime: `Δ(mean(close,100),100)/close_{t−100} ≤ 0.05 ? −(close − min(close,100)) : −Δclose₃`

**Academic (price-only, convertible to time-series by dropping the z-score):**
- Amihud `ILLIQ = mean(|r_t|/(close_t·volume_t), 21)` (also in `quantlib.microstructure.amihud_illiquidity`)
- Return skew `skew(r, 60)` (negative skew → tail risk flag)
- 52-week-high analogue `close/max(close, N)`; short-term reversal `−(close_t − close_{t−21})/close_{t−21}`; skip-month momentum `(close_{t−21} − close_{t−252})/close_{t−252}`

**Liquidity/impact (quantlib):** `roll_effective_spread`, `amihud_illiquidity`, `vpin` (needs taker buy/sell volume — Binance klines expose `takerBuyBaseVolume`, so `sell = volume − takerBuy`), `sqrt_impact(price, dir, qty, adv, vol, eta=0.5)` → paper-mode slippage, and the order-book `normalized_imbalance` + ladder-walk from `orderbook_depth_tool.py`.

Not available in the zoo (keep from the previous bot's `indicators.py`): ATR(14), RSI(14) Wilder, Bollinger, EMA crossover, VWAP from candles — the zoo has no ATR/BB; their `vwap()` for crypto is just `(O+H+L+C)/4`.

---

## 5. Attribution requirements (`/home/user/hkuds/vibe-trading/NOTICE` + per-zoo LICENSE.md)
- NOTICE: "This product bundles Microsoft Qlib feature definitions under the Apache 2.0 license. See agent/src/factors/zoo/qlib158/NOTICE and LICENSE.md" and "Mathematical formulas from the following sources are reimplemented ... as factual mathematical content (formulas are not copyrightable): Kakushadze (2015) '101 Formulaic Alphas' (arXiv:1601.00991); Guotai Junan Securities (2014) '191 Short-period Trading Alpha Factors'; Fama-French / Carhart / Hou-Xue-Zhang". "Prose, tables, and figures from the source papers and reports are NOT reproduced".
- **Qlib (Apache-2.0)**: `zoo/qlib158/LICENSE.md` says Apache §4 obliges us to (a) ship/point to the upstream LICENSE, (b) state prominently that files were modified, (c) retain notices — they do it with a per-file header: `# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py` / `# (Apache-2.0). Copyright (c) Microsoft Corporation.` Their position: the individual formulas are arithmetic facts; what is borrowed from Qlib is the **curated catalogue and the short names** (KMID, KLEN, ROC, MA…). ⇒ If our `indicators.py` reuses the Alpha158 names/catalogue, add the same header + a NOTICE line + a link to Qlib's LICENSE. If we only use a handful of formulas under our own names, a NOTICE credit line is still the courteous minimum.
- **alpha101**: cite `Kakushadze (2015), arXiv:1601.00991` in the module docstring, use the display name "Kakushadze 101 Formulaic Alphas", do not reproduce paper prose, and do not add the author's former firm's name (their CI greps for it).
- **gtja191**: cite 国泰君安 (Li Chen, Liu Fubing, 2017) and "GTJA Alpha 191"; formulas only.
- **academic**: cite the papers (Amihud 2002, Harvey-Siddique 2000, George-Hwang 2004, Carhart 1997, Fama-French 1993/2015, Frazzini-Pedersen 2014).
- Vibe-Trading itself is MIT: if any of their *code* (e.g. `safe_div`, `ts_rank`, `retry_with_budget`, `_walk_book`) is copied rather than re-written, keep the MIT copyright line ("Copyright 2026 HKUDS contributors") in the file or in `vendor/LICENSES`.

---

## 6. Concrete recommendations for the 15m bot

**Adopt (as code, rewritten):**
1. The `__alpha_meta__`-style declaration for each indicator (`inputs`, `warmup_bars`, `formula`) + a registry that (a) refuses to run when inputs are missing, (b) masks the output on the input-NaN mask, (c) rejects ±inf and >95%-NaN outputs. Use `warmup_bars` to size the candle request (`limit = max(warmup) + margin`) and to skip the agent call when history is short.
2. The NaN doctrine: `min_periods=window`, `safe_div` (NaN on zero), ternary guards `.where(cond_inputs.notna())`, `delta` with `d >= 1` only. This is the cheapest look-ahead/garbage-in protection for prompts.
3. ccxt fetch skeleton: per-call timeout 15 s, wall-clock budget 60 s, retry only `ccxt.NetworkError` with backoff (0.5, 1.5, 4), `since` cursor = `last_ts + 1`, stop on short page, half-open `[start, end)` filter, `to_numeric(errors="coerce")` + `dropna(OHLC)` + `validate_ohlc` (high ≥ low ≥ 0, brackets O/C), completeness check against `end − timeframe`.
4. Staleness gates: drop the still-forming last candle (OKX `confirm` idea → for Binance: drop a bar whose `ts + 15m > now`), median-spacing check vs 900 s with ratio 1.5, and the order-book "no venue timestamp → use local time and label it" rule.
5. Cache rule: only cache ranges whose end is strictly before "now − one bar"; content-addressed key including a schema version; atomic writes; cache errors never fail the fetch.
6. Indicator catalogue in §4 (K-bar shape, RSV/KDJ, MACD via `ewm(adjust=False)`, SUMP≈RSI, CNTP, CORR/CORD, VMA/VSTD, IMAX/IMIN, Amihud, Roll spread, orderbook imbalance, sqrt-impact slippage for paper fills).
7. Binance SDK hygiene: strip API keys for public calls, `adjustForTimeDifference=True`, `recvWindow=10000`, `set_sandbox_mode(testnet)`, host assertion fail-closed before any order.
8. Provenance envelope on every data payload handed to an agent (`source`, `fetched_at`, `bars`, `truncated`, `stale: bool`) and a `_diagnostics` block on failure so the head agent can see *why* data is missing rather than hallucinate.

**Adapt:** the crypto `vwap()` fallback `(O+H+L+C)/4` (Binance klines also provide quote-asset volume → true VWAP = quoteVolume/baseVolume per bar); the quantlib `vpin` with `takerBuyBaseVolume`.

**Skip:** cross-sectional operators (`rank`, `zscore`, `scale`, `zscore_by_group`), the entire fundamental zoo, equity-only quantlib modules (valuation, credit, fixedincome, options, fundmath, attribution, factormodel, neutralization), OKX loader (we are Binance-only), perpetual/maintenance-bracket machinery, duckdb/parquet cache (SQLite or JSON is enough for 3 symbols), the multi-source fallback chain (one venue; keep a simple retry + "data unavailable → HOLD").

## 채택 후보 (adopt)

- Per-indicator metadata declaration {inputs, warmup_bars, formula} parsed statically, with registry pre-check for missing inputs — pattern from /home/user/hkuds/vibe-trading/agent/src/factors/registry.py (AlphaMeta, lines 90-106; load_alpha_meta_from_py 151-187)
- Output masking on the input-presence mask plus ±inf and >95%-NaN rejection — /home/user/hkuds/vibe-trading/agent/src/factors/registry.py lines 359-378 and _validate_output 405-426
- NaN doctrine: min_periods=window, safe_div returning NaN on zero, delta(d>=1) look-ahead ban, ternary guard .where(inputs.notna()) — /home/user/hkuds/vibe-trading/agent/src/factors/base.py (docstring 1-30, safe_div 450-462, delta 368-375) and /home/user/hkuds/vibe-trading/agent/src/factors/zoo/alpha101/alpha_009.py
- ccxt OHLCV pagination skeleton: 15s per-call timeout, 60s wall-clock budget, retry only ccxt.NetworkError with (0.5,1.5,4) backoff, cursor=last_ts+1, stop on short page, half-open range filter, to_numeric+dropna, completeness check vs end-timeframe — /home/user/hkuds/vibe-trading/agent/backtest/loaders/ccxt_loader.py _fetch_one (442-517) and /home/user/hkuds/vibe-trading/agent/backtest/loaders/base.py retry_with_budget/check_budget (381-460)
- OHLC invariant validation (high>=low, brackets open/close, positive prices) at the loader boundary — /home/user/hkuds/vibe-trading/agent/backtest/loaders/base.py validate_ohlc (191-258)
- Staleness checks: drop the still-forming candle (OKX confirm flag idea, /home/user/hkuds/vibe-trading/agent/backtest/loaders/okx.py 327-331), median bar-spacing vs declared interval with 1.5x tolerance (/home/user/hkuds/vibe-trading/agent/backtest/runner.py 1572-1640), and label order-book snapshots whose venue timestamp is None with local fetch time (/home/user/hkuds/vibe-trading/agent/src/tools/orderbook_depth_tool.py docstring)
- Cache only fully-elapsed ranges, content-addressed key with schema version, atomic tmp+os.replace writes, cache failures never fail the fetch — /home/user/hkuds/vibe-trading/agent/backtest/loaders/base.py loader_cache_range_is_final (564-576), cached_loader_fetch (639-680)
- Binance ccxt hygiene: strip API keys for public reads, adjustForTimeDifference=True, recvWindow=10000, set_sandbox_mode(testnet), fail-closed host assertion before orders — /home/user/hkuds/vibe-trading/agent/src/trading/connectors/binance/sdk.py _public_exchange/_exchange/_assert_host (790-860)
- K-bar shape features KMID/KLEN/KSFT/KUP/KLOW and rolling features RSV, MA/STD/BETA/RSQR/RESI, MAX/MIN/QTLU/QTLD, RANK, IMAX/IMIN/IMXD, CNTP/CNTN/CNTD, SUMP/SUMN/SUMD, VMA/VSTD, CORR/CORD, WVMA, VSUMP/VSUMN/VSUMD — /home/user/hkuds/vibe-trading/agent/src/factors/zoo/qlib158/*.py (with Qlib Apache-2.0 header/NOTICE attribution)
- Wilder-style smoothing SMA(x,n,m) = x.ewm(alpha=m/n, adjust=False).mean(); KDJ (RSV9, K, D, J=3K-2D) and MACD(13,27,10) via gtja191 #28/#96/#89 — /home/user/hkuds/vibe-trading/agent/src/factors/zoo/gtja191/alpha_028.py, alpha_096.py, alpha_089.py
- Single-asset alpha101 pieces: #101 body/range, #9/#10 persistence-vs-reversal switch, #12 volume-signed reversal, #23, #46/#49/#51 curvature regime, #53, #54 — /home/user/hkuds/vibe-trading/agent/src/factors/zoo/alpha101/alpha_{101,009,010,012,023,046,049,051,053,054}.py (cite Kakushadze 2015 arXiv:1601.00991)
- Liquidity and fill-cost primitives: amihud_illiquidity, roll_effective_spread, vpin (with Binance takerBuyBaseVolume), sqrt_impact(eta=0.5) for paper-mode slippage, order-book normalized_imbalance and ladder walk — /home/user/hkuds/vibe-trading/agent/src/quantlib/microstructure.py, /home/user/hkuds/vibe-trading/agent/src/quantlib/impact.py, /home/user/hkuds/vibe-trading/agent/src/tools/orderbook_depth_tool.py (_walk_book)
- Risk reporting helpers: historical_var/cvar, drawdown_series, max_drawdown_analysis, ulcer_index with 'loss is positive' sign convention — /home/user/hkuds/vibe-trading/agent/src/quantlib/risk.py
- Deterministic formulas in code, not in prompts ('one implementation, pinned by tests') — /home/user/hkuds/vibe-trading/agent/src/quantlib/__init__.py lines 3-8
- Provenance + _diagnostics envelope on every data payload (source, fallback_used, stale, attempts/reasons) so agents see why data is missing — /home/user/hkuds/vibe-trading/agent/src/market_data.py _emit (412-452) and _diagnostics (542-550)
- Point-in-time availability contract per field (availability_mode=bar_close, status verified/assumed/unknown/failed) as the shape of a data-quality gate — /home/user/hkuds/vibe-trading/agent/src/factors/data_availability.py

## 제외 (skip)

- Cross-sectional operators rank/zscore/scale/zscore_by_group and every alpha that depends on them — meaningless over ~3 symbols
- zoo/fundamental (20 factors) and fund:/val: panel columns — equities-only
- zoo/academic cross-sectional z-scored factors as-is (use only the inner time-series formula)
- corr_rewire (142-bar warmup, needs a cross-section) and gtja191 benchmark-index substitutions
- quantlib modules: valuation/*, credit, fixedincome, options, volatility (Heston), fundmath, attribution, factormodel, neutralization, copula, eventstudy (unless used for news studies)
- OKX loader, multi-source FALLBACK_CHAINS and MARKET_DATA_ORDER_* overrides — Binance-only bot needs one venue + retry + HOLD-on-missing-data
- Perpetual/mark-price/funding-settlement validation and maintenance-bracket artifacts — spot-only bot; funding/OI are optional context, not an alignment contract
- duckdb/parquet loader cache with sidecar metadata — overkill for 3 symbols; keep the caching rules, use JSON/SQLite
- bottleneck fast paths and sliding_window_view micro-optimisations — pandas rolling is fast enough at 3 symbols x few thousand bars
- Weekly/monthly resample_bars, GBp/pence and venue-currency normalisation, Canadian venue sibling retry — equity plumbing
- The bench_runner / compare_runner / data_availability PIT machinery as a whole (keep only the status vocabulary)