# Vibe-Trading paper_trade_step contract, paper broker/portfolio persistence, tool I/O validation and market-data normalization — what to copy for the Binance spot bot

# Vibe-Trading: paper_trade_step, portfolio persistence, tool I/O contracts

All paths below are under `/home/user/hkuds/vibe-trading/agent/` unless absolute. Nothing was modified.

## 0. One-paragraph orientation

Vibe-Trading's paper broker is a **pure, deterministic, stateless function** (`paper_trade_step`) wrapped by a **thin stateful service** (`PaperAccounts`, SQLite, optimistic concurrency). The function takes the full ledger as input, matches orders against *completed* daily bars, and returns one JSON document that is either a success result or an in-band error envelope — never a transport exception for data/input problems. State never lives inside the engine; the caller owns it and the engine echoes a hash of what it received so the caller can prove the engine saw the same ledger it sent. This separation (pure engine + atomic commit layer + schema-frozen wire contract) is the thing most worth copying.

Module map (from `src/tools/paper_trade/__init__.py:8-14`): `schema.py` (wire contract + boundary parse), `state_hash.py` (JCS hash), `calendar.py`, `limits_cn_a.py`, `data.py` (market data supply), `rules.py` (fee/lot/gates), `engine.py` (day loop), `corporate_actions.py`, `wire.py` (serialization), `tool.py` (entry), `strategy_exec.py` (sandboxed strategy), `accounts.py` (saved accounts), `order_lifecycle.py` (standalone order state machine).

---

## 1. The `paper_trade_step` contract (pt-v1.1)

Schema file: `/home/user/hkuds/vibe-trading/schema/tool_contracts/paper_trade_step-v1.1.json` (identical copy at `src/tools/paper_trade/paper_trade_step-v1.1.json`, loaded by `schema.load_contract_schema`). It is a draft-2020-12 document holding **two named schemas**: `schemas.result` and `schemas.error_envelope`.

### 1.1 Inputs (not in the JSON file; defined by `PaperTradeStepTool.parameters` in `tool.py:318-374` and parsed by `schema.parse_request`)

| field | type | notes |
|---|---|---|
| `account_id` | string | echoed verbatim in output, even on the error path |
| `market` | enum `cn_a|hk|us` | selects rule matrix |
| `account_state` | `{cash:number, positions:[{symbol,qty,avg_cost}]}` | the full ledger, injected by caller |
| `from_date`,`to_date` | `YYYY-MM-DD` (regex `^\d{4}-\d{2}-\d{2}$`) | inclusive closed range; orders mode requires `from==to` |
| `orders` | `[{symbol, side:buy|sell, qty, limit?}]` | XOR with `strategy_path`; `[]` = pure valuation |
| `strategy_path` | string | sandboxed strategy file; XOR with `orders` |
| `fee_model` | object of numbers | per-key override merged over market defaults |

Semantic gate owned by the tool, not the parser (`tool.py:205-216`): `exactly one of orders or strategy_path must be provided, got both|neither` → `INVALID_REQUEST`.

### 1.2 Success result — required fields (`paper_trade_step-v1.1.json:12-27`, all with `additionalProperties:false`)

`schema_version` (const `"pt-v1.1"`), `tool` (const `"paper_trade_step"`), `account_id`, `market`, `range{from,to}`, `received_state_hash`, `effective_fee_model`, `fills[]`, `rejected_orders[]`, `end_state{cash,positions[]}`, `nav_series[]`, `skipped_days[]`, `warnings[]`, `data_provenance{source,...}`.

- `fills[]`: `{trade_date, symbol, side, qty, price, fee, reason}`; `reason` is `""` in orders mode, strategy text truncated to ≤200 code points in strategy mode (`wire.strategy_fill_reason`, `wire.py:71-82`).
- `rejected_orders[]`: `{trade_date, symbol, side, qty, reason_code, detail}` with **closed 11-value vocabulary** (`schema.py:78-90`): `insufficient_cash, insufficient_position, t_plus_1, limit_up, limit_down, suspended, lot_size, non_trading_day, unknown_symbol, corporate_action_unresolved, limit_not_marketable`.
- `nav_series[]`: `{date, nav, cash, position_value}` — one row per *matched* day, none for skipped days.
- `skipped_days[]`: `{date, reason}` with closed 2-value vocab `non_trading_day` (cn_a calendar truth) / `market_closed_inferred` (hk/us, always accompanied by a warning).
- `warnings[]`: "deterministic English strings, never contain timestamps" (`json:165`).
- `data_provenance`: `source` required; optional `ch_as_of`, `network_as_of`, `bars_as_of`, `corporate_actions[]`, `limit_provenance[]`; "Optional fields are omitted when unset, never null" (`json:205`).

### 1.3 Error envelope (`json:209-246`)

`{status:"error", code: PAPER_DATA_NOT_READY|INVALID_REQUEST, account_id, market, range, message, received_state_hash?}`.
- `PAPER_DATA_NOT_READY` = retryable ("RM 30min x 3"); `INVALID_REQUEST` = non-retryable.
- `received_state_hash` "present only when the received account_state was parseable … omitted otherwise, never null — RM reads the absence as 'state unparseable' and must not burn its retry chain" (`json:239`).
- Frozen message formats: `strategy_execution_timeout:{YYYY-MM-DD}`, `strategy_execution_error:{YYYY-MM-DD}:{ExceptionClass}`.
- `wire.build_error_payload` **raises** if `code` is outside the vocabulary: an engine bug must not ship an envelope the caller cannot route (`wire.py:282-285`).

### 1.4 Invariants worth naming

1. **Determinism**: "no clock, no randomness; every date and every client is injected" (`tool.py:23-24`, `engine.py:14-16`). The tool class sets `deterministic = False` only because served market data changes between sessions (`tool.py:377-379`).
2. **Per-day atomicity / no half-day**: "unexpected exceptions propagate … so a half-day is never observable — the call aborts with no partial result" (`engine.py:14-16`). In strategy mode "a day-k failure never returns days 1..k-1" (`strategy_exec.py:49`).
3. **Day k end_state = day k+1 input** (`engine.py:456-457`).
4. **Frozen ordering**: input order = matching order; fills/rejections day-ascending then input index; positions symbol-sorted, zero-qty rows dropped (`engine.py:360-366`).
5. **Rejection ladder, first match wins** (`rules.py:62-74`): `unknown_symbol → non_trading_day → suspended → corporate_action_unresolved → lot_size → limit_up/limit_down → limit_not_marketable → t_plus_1 → insufficient_position → insufficient_cash`. One reason per order. Implemented slot-by-slot in `engine._MatchSession.match_order` (`engine.py:199-269`).
6. **Fail-closed on unknowns**: unknown price band → check skipped **with a warning**, never fabricated (`engine.py:325-327`); missing calendar → `PAPER_DATA_NOT_READY`, never "assume weekday = trading day" (`engine.py:109-115`); split-like discontinuity → `PAPER_DATA_NOT_READY` without touching holdings (`engine.py:472-479`).
7. **Received-state hash**: `sha256(JCS({account_id, market, account_state(RAW), fee_model(EFFECTIVE), from_date, to_date}))` (`state_hash.compute_received_state_hash`, `state_hash.py:156-179`). The hash covers the **raw JSON value**, not the parsed dataclass: "a key the LLM added or dropped must break the hash exactly like a corrupted number" (`wire.py:12-16`).
8. **Effective fee echo**: output always discloses the fee parameters actually used (`effective_fee_model`, per-key merge over defaults in `state_hash.effective_fee_model`, `state_hash.py:182-192`).
9. **Byte-deterministic JSON**: `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` (`wire.py:306-308`).

### 1.5 Fill rules against completed bars

- **Fill price = that day's close, zero slippage, all-or-none** (`engine.py:230,269,277-302`; README "Matching is daily close, all-or-none"). `limit` orders: "buy fills only when close ≤ limit; sell only when close ≥ limit (fill price = close either way)" (`rules.py:210-216`).
- **Cash gate**: `qty×price(2dp) + fee ≤ cash` (`rules.buy_cost`, `rules.py:181-183`).
- **Money discipline**: Decimal end-to-end, `money(x) = Decimal(str(x))`, notional/fee quantized 2dp `ROUND_HALF_UP`, `avg_cost` fee-exclusive weighted average quantized 4dp at every mutation "so internal chaining equals the RM round-trip" (`engine.py:278-294`, `rules.py:85-92`).
- **Buy**: `cash -= notional + fee`; basis = weighted average; `bought_today[symbol] += qty` (T+1 bookkeeping). **Sell**: `cash += notional - fee`; `qty -= sold`.
- **Valuation** (`engine.add_nav_row`, `_valuation_price`, `engine.py:331-358`): close×qty per position (2dp each) + cash; a symbol without a usable bar that day values at the last positive close on/before the day, else `avg_cost` **with a warning**.
- **Price sanity gate (R-V3)**: bars with NaN/non-positive OHLC are `untradable_dates`; orders that day reject `suspended` (`data._untradable_dates`, `data.py:159-168`).
- **Readiness gate (AM-9)**: before matching, if *no* served symbol has a bar on `to_date` → `PAPER_DATA_NOT_READY` with message `target bar {day} not available from served sources ({source} frame ends {ends})` (`data.assess_readiness`, `data.py:501-517`). Single-symbol absence is a `suspended` rejection instead.
- **"Completed bar" is enforced by the stateful layer, not the engine** (`accounts.py:201-223`): the latest settleable date is today minus one if local time is before `(close_hour, close_minute)` = 15:30 Shanghai / 16:30 HK / 16:30 NY (comment: "Daily bars may remain provisional shortly after the regular market close"; README says "Settlement waits until 30 minutes after the regular local close").

### 1.6 Saved accounts move forward only (`accounts.py`)

`PaperAccounts` is "SQLite ledger with optimistic concurrency across processes and surfaces" (`accounts.py:36`). Tables: `accounts(id, document JSON)`, `steps(account_id, revision, document)`.

- Account document fields (`accounts.py:110-124`): `id, label, market, currency, created_at, start, start_on, last_date, revision, initial_state, account_state, fee_model, last_result`.
- Creation validation reuses the tool parser: "Reuse the tool's strict numeric/fee validation at every entrance" (`accounts.py:125-136`), plus cash ≥ 0, qty/avg_cost > 0, unique symbols, non-negative fees.
- `advance(expected_revision, to_date, orders|strategy_path)`:
  - `expected_revision != account["revision"]` → `AccountConflict("Account changed; reload before advancing")` (`accounts.py:195-200`).
  - `earliest = start_on` or `last_date + 1 day`; `to_date` must satisfy `earliest <= to_date <= latest` (`accounts.py:206-223`). This is the **forward-only** rule: you can never re-settle a date at or before `last_date`.
  - Payload passes `from_date=earliest, to_date=to_date`; explicit orders execute only on `to_date` while every intervening day is valued (`_orders_at_end=True` → `engine.run_day_loop(orders_by_day={to_date: orders})`, `tool.py:275-283`).
  - Commit is atomic and compare-and-swap: `UPDATE accounts SET document=? WHERE id=? AND document=?` must affect exactly 1 row, else `AccountConflict("Account advanced concurrently; result discarded, reload")` (`accounts.py:250-258`); the step result is inserted into `steps` in the same transaction.
  - An error envelope (`"code" in result`) or an empty `nav_series` → `{committed: False}`; "Closed dates must not consume the account's first eligible session" (`accounts.py:238-242`).
  - README: "After a network timeout, read the account before retrying: the server may have committed."

### 1.7 Strategy sandbox (strategy mode)

`strategy_exec.py`: strategy file must live under `<runtime_root>/strategies`, be `.py`, ≤1MB, UTF-8; it is AST-validated with the backtest sandbox validator, then `compile/exec`'d in a **spawned child process** (never fork) with a per-day SIGKILL timeout (default 30s). The child's `ctx.history(symbol)` returns only bars strictly before the execution date (`strategy_exec.py:145-149`). Order shape validation reuses `schema._parse_order` — "one order-shape enforcement, not a second weaker one" (`strategy_exec.py:338-341`). Optional module literal `UNIVERSE = (...)` is read by pure `ast.literal_eval`, never executed (`strategy_exec.py:230-266`).

### 1.8 Standalone order state machine (`order_lifecycle.py`)

Not wired into the day engine yet, but a clean reference: statuses `new/open/filled/canceled/rejected/held/pending_cancel`; stable ids `pt-<index>` so "a replayed list reproduces identical ids and a retry is distinguishable from a new order" (`order_lifecycle.py:105-118`); cancel of terminal order raises, double cancel idempotent; `fill(qty, price, volume_cap)` clips to remaining and tracks `avg_fill_price`; `settle_day()` cancels all unfinished orders; `replay_snapshot()` renders Decimals as strings with sorted keys.

---

## 2. How tool inputs/outputs are validated

### 2.1 Tool base class (`src/agent/tools.py:13-65`)
```python
class BaseTool(ABC):
    name: str = ""; description: str = ""
    parameters: Dict[str, Any] = {}        # JSON Schema shown to the LLM
    repeatable: bool = False; is_readonly: bool = True
    deterministic: bool = False; replay_after_compaction: bool = False
    def execute(self, **kwargs) -> str      # ALWAYS returns a JSON string
```
`parameters` is the LLM-facing JSON schema (converted with `to_openai_schema`). It is **not** used to validate at runtime; every tool re-validates by hand inside `execute` and returns an error JSON on bad input. Examples:
- `market_data_tool.py:170-231`: checks list-of-non-empty-strings, strict `YYYY-MM-DD` via regex **and** `date.fromisoformat` (because 3.11's `fromisoformat` also accepts `YYYYMMDD`), `start<=end`, `source in enum`, interval canonicalization (`1m` vs `1M` case-exact first), `max_rows` int-not-bool, negative clamps to default rather than unbounded. Errors are `{"ok": false, "error": msg}`.
- `_arguments.boolean_argument` (`src/tools/_arguments.py:8-34`): accepts bool/null/0/1/"true"/"false"…; raises on anything else because "an unknown dry-run string must never silently enable writes".

### 2.2 paper_trade_step: "parse, don't validate"
`schema.parse_request` (`schema.py:246-294`) converts the JSON into frozen `slots=True` dataclasses (`PaperTradeStepRequest`, `AccountState`, `Position`, `Order`) and raises `PaperTradeSchemaError` with a path-named message (e.g. `account_state.positions[2].qty must be a JSON number, got str`). Details worth copying:
- `_require_number` rejects `bool` (bool is an int subclass), non-finite floats, and ints beyond double range (`schema.py:176-188`).
- "Only a missing or explicit null fee_model defaults to {} … a PRESENT falsy non-dict ([] / "" / false / 0) is a contract violation, not an absence — `or {}` would launder it" (`schema.py:270-279`).
- Shape errors → `INVALID_REQUEST`; semantic errors (XOR, single-day) are handled one layer up in `tool.py`.

### 2.3 Output validation: a dependency-free JSON-schema subset
`schema.validate_payload(payload, schema) -> list[str]` (`schema.py:423-487`) implements only `type/const/enum/required/properties/items/additionalProperties(bool)`; the contract file deliberately "stays within the keyword subset the dependency-free validator … implements … so the file is a standard draft-2020-12 document that the real jsonschema library can also consume" (`json:2`). `jsonschema` is not imported ("zero-new-dependency red line", `schema.py:19-23`). Golden fixtures live in `tests/fixtures/paper_trade/` (`golden/`, `jcs_vectors.json`, `market/`, `strategies/`); the JCS vectors are "the SOLE interpretation authority" shared by engine and consumer (`state_hash.py:5-8`).

### 2.4 Error discipline shared by all tools
Success and error travel on the same channel as a JSON string; `fetch_market_data_json` even converts an all-unresolved result into `{"status":"error","error_code":"no_market_data", ...}` because "a bare `{"_unresolved": [...]}` classified as a successful call, so the agent loop neither refused an identical retry nor told the model to change the request" (`src/market_data.py:557-561`). Non-finite floats are scrubbed to `null` before dumping (`backtest_summary._json_safe`, `market_data._json_safe`); `allow_nan=False` everywhere.

### 2.5 Result-size budget
`backtest_tool._serialize_result` (`backtest_tool.py:312-336`) degrades in a fixed order when the envelope exceeds `TOOL_RESULT_LIMIT`: blank stdout/stderr with an `*_omitted` notice → halve `equity_preview` points (first/last pinned) → drop `summary` → drop `artifacts`. `market_data.cap_rows` returns `{rows, returned, truncated:true, policy:"every-Nth-row (even stride; last bar pinned)", hint, data}` instead of silently truncating (`market_data.py:168-186`).

---

## 3. How market data is normalized

### 3.1 Shared fetch layer (`src/market_data.py`)
- `detect_source(code)`: regex table symbol→preferred source (`BTC-USDT`→okx, `BTC/USDT`→ccxt, `AAPL.US`→yahoo …) (`market_data.py:21-85`). `normalize_crypto_symbol` folds `BTCUSDT`/`BTC/USDT` to `BTC-USDT` (`:89-93`).
- `fetch_market_data(...)` groups codes by `(source, market)`, walks the market's `FALLBACK_CHAINS` with a capped attempt budget, retries **per symbol** down the chain, and records `_diagnostics[symbol].attempts = [{source, reason: source_unavailable|fetch_failed|no_data}]` plus a `recovery` hint (`:284-410`, `:542-550`). Weekly/monthly bars are resampled from daily.
- Every symbol gets `_provenance`: `{source, requested_source, detected_source, fallback_used, currency_conversion, volume_unit ('lots'|'shares'|null), adjustment ('raw'|'split'|'split_dividend'|'split_dividend_additive'|'na'|'unknown'), quote_currency?, venue_fallback?, resolved_symbol?}` (`:435-452`). The tool description tells the LLM to read `_provenance.volume_unit` and `.adjustment` before comparing values (`market_data_tool.py:83-96`).

### 3.2 Paper-engine canonical frame (`src/tools/paper_trade/data.py`)
`_canonical_frame` (`data.py:107-126`): tz-naive midnight `DatetimeIndex` named `trade_date`; exactly columns `open,high,low,close,volume` (`vol`→`volume`, missing columns NaN), `pd.to_numeric(errors="coerce")`, duplicates dropped keep-last, sorted ascending. Then:
- `_trim_window`: frozen lookback of **K=120 trading bars before from_date** (fetched over a 200-calendar-day span) plus `[from,to]`; "later bars never enter the snapshot" (`data.py:141-156`).
- `_untradable_dates`: NaN/≤0 OHLC flagged (R-V3).
- `_valuation`: last positive close, else `("avg_cost", warning)`.
- `SymbolData(symbol, frame, bar_dates:frozenset, untradable_dates, valuation_price, valuation_basis)`; `MarketDataResult(market, from_date, to_date, symbols(sorted), data, provenance, warnings)` (`data.py:293-319`).
- **Caliber check**: "Adjusted and unknown network frames are refused … a matching timestamp does not make them compatible with a raw-price cash ledger" (`data.py:26-30`, enforced in `_fetch_leg` via `frame_caliber(...) != "raw"` → warning + frame rejected, `data.py:279-286`). `raw_loader.TencentRawLoader` tags `frame.attrs["adjustment"]="raw"`.
- Leg failures degrade to a warning (`network fetch (name) failed: ExcType`), never a crash (`data.py:272-276`).
- `data_provenance.source` for the merged cn_a case is `clickhouse+tencent(merged)`; `bars_as_of`/`ch_as_of`/`network_as_of` are max index dates.

### 3.3 Indicator tool normalization (`technical_indicator_tool.py`)
`_extract_series` accepts DataFrame / column dict / `list[dict]` / `{"data": [...]}` and finds the close/volume column by a spelling list; keeps a real `DatetimeIndex` when available, otherwise a `RangeIndex` "so callers do not report a row number as a date" (`:47-109`). Volume keeps NaN for dated bars (`keep_missing=True`) so a gap cannot slide an older value into "latest"; `_compute_volume_stats` reports the trailing mean only when all 20 bars are finite (`:122-143`). RSI is Wilder EWM (`ewm(alpha=1/period, min_periods=period)`), MACD 12/26/9 with `adjust=False`, Bollinger 20/2σ (`:160-262`). Output carries `read_identity = {mode: pinned_boundary|latest_boundary, requested_end_date, bar_count, dataset_fingerprint: sha256([interval, [[date, close, volume]...]]), provenance}` (`:446-454`) — a hash of the exact bars the numbers came from.

### 3.4 Connector normalization (portfolio + binance)
- `portfolio/normalization.normalize_position` (`normalization.py:56-196`): one wire shape `{broker, symbol, quote_symbol, name, asset_type, market, currency, quantity, cost_price, market_price, price_currency, source_market_value, source_unrealized_pnl, free, used, source, updated_at}`, with a long `row.get("quantity", row.get("qty", row.get("position", …)))` alias chain; `_decimal()` coerces anything to a finite `Decimal` or default; `binance` rows get `asset_type` `stablecoin`/`crypto` and `quote_symbol = f"{symbol}/USDT"`; unpriced stays `None` "rather than guessed".
- `value_position` computes `priced`, `market_value_usd`, `unrealized_pnl_usd` and drops connector-only fields (`:199-241`).
- Binance connector (`src/trading/connectors/binance/sdk.py`): `BinanceConfig` is a frozen dataclass; `profile` ∈ `paper|live-readonly|live`; host is the authoritative paper/live discriminator — `_assert_host` fails closed if a paper profile resolves to anything but the testnet host (`:839-859`); `_exchange` builds ccxt with `enableRateLimit`, `adjustForTimeDifference: True`, `recvWindow: 10_000`, `set_sandbox_mode(is_testnet)` (`:799-836`); `_public_exchange` strips credentials for public data (`:790-796`); `_public_config` redacts secrets (`:871-879`). `place_order` validates side/type/`quantity XOR notional`/positive numbers before touching the network and returns `{"status":"error","error":…}` fail-closed; success returns `{status:"ok", order_id, symbol, side, profile, is_testnet, paper_guard:"host_separated", order_type, order_status, filled, amount, price}` (`:527-657`). Market `notional` orders use `params["quoteOrderQty"]`.
- Service layer (`src/trading/service.py:716-780`): paper profile → direct connector call; live profile → `execute_live_order` (mandate + kill switch + fail-closed pre-trade checks + audit, `src/live/sdk_order_gate.py`). Cancel is "risk-reducing, so it is not blocked by the mandate or the kill switch" but is still audited (`:791-797`).

### 3.5 Portfolio snapshot persistence (`portfolio/store.py`, `portfolio/service.py`)
Immutable append-only snapshots: `portfolio_snapshots(id, created_at, complete, total_usd TEXT, total_cny TEXT, payload JSON)`; totals stored as text "to avoid binary float drift"; `complete` flag + `valuation_version` so history charts never mix partial and complete snapshots ("Charting a mix … would draw a value drop that never happened", `store.py:154-157`); `latest_successful_source` points back at the last healthy observation for a failing source without re-adding it to totals. Snapshot envelope (`service.py:480-516`): `{snapshot_id, valuation_version, created_at, complete, totals{usd,cny,display}, valuation{priced_usd, cash_usd, unpriced_or_other_usd, identified_coverage}, fx{...stale}, accounts[], positions[], combined_holdings[], warnings[]}`.

### 3.6 Shadow account persistence (`shadow_account/storage.py`, `models.py`)
Frozen dataclasses as "the stable boundary between extractor / codegen / backtester / reporter"; JSON files under `<runtime_root>/shadow_accounts/<id>.json`; `journal_hash` (SHA1 of input bytes) for idempotent re-runs; `shadow_total_pnl`/`delta_pnl` are `None` when metrics are unusable because "a failed comparison must never render as 0.0" (`models.py:149-151`).

---

## 4. What our bot should copy (concrete)

### 4.1 Paper broker = pure step function + separate commit layer
Design `paper_step(ledger, bars, orders, fee_model) -> StepResult | StepError` with **no clock, no I/O, no randomness**; the 15-minute cycle loop supplies the completed 15m candle(s) and the current ledger. Keep a separate `PaperAccount` persistence class that owns wall-clock eligibility and atomic commit. Benefits for us: offline golden tests with recorded candles, exact replay of any cycle from the journal, and a mock backend that is just "another caller of the same function".

Adaptations for Binance spot:
- Fill price: Vibe fills at the completed bar's **close**. For a 15m bot that submits at cycle start, fill at **next bar open** (or close of the just-completed bar + configurable slippage bps + 0.1% taker fee). Keep "all-or-none, one reason per rejection".
- Replace market rules: no T+1/lots/price limits; add Binance **`LOT_SIZE` step, `MIN_NOTIONAL`, price tick** (reject `lot_size`/`min_notional` rather than silently rounding — Vibe's rule: "the paper engine never silently rounds a user-stated qty", `rules.py:17-18`). Fee in base asset on BUY (net qty) as the previous bot already did.
- Readiness gate: if the latest 15m candle is not closed (`close_time > now`) or missing → `DATA_NOT_READY` (retryable), never trade on a provisional bar. Map Vibe's "settle 30 min after close" to "candle `closeTime` + small grace".
- Keep the **closed rejection vocabulary** and extend it with our hard-limit reasons so dashboards/Telegram can key on codes: e.g. `insufficient_cash, insufficient_position, lot_size, min_notional, data_not_ready, unknown_symbol, max_positions, symbol_cap_10pct, stop_loss_cap, take_profit_cap, confidence_below_0_65, cooldown, daily_loss_halt, consecutive_loss_halt, kill_switch, drawdown_ladder_stop`.

### 4.2 Portfolio persistence
- SQLite with `accounts(id, document)` + `steps(account_id, revision, document)`, **revision + compare-and-swap** commit (`UPDATE … WHERE document = <previous>`), forward-only `last_cycle_ts`; a step that errors or produces no NAV row is `committed:false` and does not advance the revision. This replaces the previous bot's `state.json` + lock file with something that also gives us the full step history for the dashboard for free.
- Store money as text/Decimal strings in SQL (`store.py:97-99`), compute in Decimal (`money()`, 2dp/4dp `ROUND_HALF_UP` quantization at every mutation), convert to float only at the JSON boundary.
- Append-only snapshot table with `complete` + `valuation_version` fields for the equity curve; never chart mixed partial/complete rows.
- Every persisted step carries `received_state_hash` (sha256 of canonical JSON of the ledger it was computed from) so the dashboard/Telegram can prove which state a trade came from and so replays can be verified.

### 4.3 Tool/agent I/O contracts
- Put each contract in a **versioned JSON-schema file** (`schema/<name>-v1.json`) with `schema_version` as a `const`, `additionalProperties:false`, closed enums, and `$comment`s stating what is frozen. Validate **every LLM structured output** against it before it reaches risk code; validate every tool output in tests. Stay inside the small keyword subset so a 60-line validator (`schema._validate_node`) works offline and `jsonschema` is optional.
- "Parse, don't validate": convert LLM JSON to frozen `slots=True` dataclasses at the boundary with path-named errors; reject `bool`-as-number, NaN/inf, and "present-but-falsy" fields (`fee_model` lesson).
- Errors are **in-band JSON** on the same channel, with a closed `code` vocabulary split into retryable vs non-retryable; serializer raises on unknown codes.
- Byte-deterministic serialization (`sort_keys, compact separators, ensure_ascii=False, allow_nan=False`) and **no timestamps in warning strings**; put timestamps in dedicated fields so goldens stay stable.
- Echo request identity and effective parameters in every result (`account_id`, `range`, `effective_fee_model`) — for us: `cycle_id`, `symbol`, effective risk limits used, model id per agent.
- Provenance on every data payload handed to an agent: `{source, fallback_used, bars_as_of, adjustment/caliber, volume_unit, dataset_fingerprint}`; the head agent's decision record should carry the fingerprints of the data it saw.
- Result-size budgets with explicit `*_omitted` notices and even-stride sampling with the last bar pinned, instead of silent truncation.
- Per-tool `repeatable / is_readonly / deterministic` flags on tool classes to let the loop cache or refuse repeats.

### 4.4 Safety/structure ideas from the trading/live layers
- Host separation as the paper/live discriminator (`_assert_host`); paper and live use different keys and hosts and the profile is recorded on every order payload — matches our "live only with explicit confirmation".
- Keep **cancel/flatten always allowed** under kill switch (risk-reducing), but audit every live action.
- Strategy/agent code never runs in the main process without timeouts: if we ever let an LLM emit code, copy the spawn + AST-validate + SIGKILL pattern; for pure JSON agents, just copy the per-call timeout and "whole-cycle fails, no partial result" rule.

## 5. What to skip
See the `skip` list: ClickHouse federation, A-share calendars/limit bands/T+1, HK board lots, corporate-action split detector, strategy_path subprocess sandbox (we don't run LLM-written code), multi-broker connector registry, FX/multi-currency portfolio valuation, shadow-account journal mining.

## 채택 후보 (adopt)

- Pure stateless paper step function (ledger in → fills/rejections/end_state/nav out, no clock/I/O/randomness) separated from an atomic commit layer — src/tools/paper_trade/tool.py:162-304 (run_paper_trade_step) + src/tools/paper_trade/accounts.py:176-263 (PaperAccounts.advance)
- Forward-only saved accounts with revision + compare-and-swap commit (`UPDATE accounts SET document=? WHERE id=? AND document=?` rowcount==1 else AccountConflict), step history table, committed:false on error/empty nav — src/tools/paper_trade/accounts.py:194-263
- Versioned JSON-schema contract file with schema_version const, additionalProperties:false, closed enums, retryable vs non-retryable error codes, optional fields omitted-never-null — /home/user/hkuds/vibe-trading/schema/tool_contracts/paper_trade_step-v1.1.json
- Dependency-free subset JSON-schema validator (type/const/enum/required/properties/items/additionalProperties) usable offline in tests — src/tools/paper_trade/schema.py:423-487 (validate_payload)
- 'Parse, don't validate' boundary parser to frozen slots dataclasses with path-named errors; reject bool-as-number, non-finite, and present-but-falsy fields — src/tools/paper_trade/schema.py:176-294 (parse_request, _require_number, fee_model handling)
- In-band error envelope on the same channel as success; serializer raises on an out-of-vocabulary code — src/tools/paper_trade/wire.py:272-296 (build_error_payload) and tool.py:83-105
- Byte-deterministic JSON output: sort_keys, compact separators, ensure_ascii=False, allow_nan=False; warnings never contain timestamps — src/tools/paper_trade/wire.py:299-308
- received_state_hash = sha256(JCS(raw ledger + effective params)) echoed in every result so the caller can verify what the engine saw; hash the RAW JSON, not the parsed object — src/tools/paper_trade/state_hash.py:121-179, wire.py:149-191
- Effective-parameter echo (effective_fee_model per-key merge over defaults) so disclosed and used parameters cannot diverge — src/tools/paper_trade/state_hash.py:182-192
- Decimal money discipline: money(x)=Decimal(str(x)), 2dp ROUND_HALF_UP notional/fee, 4dp avg_cost quantized at every mutation, float only at the JSON edge — src/tools/paper_trade/rules.py:85-92, engine.py:277-302
- Closed rejection-reason ladder, first match wins, one reason per order, rejected qty echoed, never silently round a user-stated qty — src/tools/paper_trade/rules.py:62-74, engine.py:199-269
- Readiness gate before matching: target bar missing for all symbols → retryable DATA_NOT_READY with deterministic message; single symbol missing → per-order 'suspended' rejection — src/tools/paper_trade/data.py:501-517, engine.py:395-404
- Completed-bar settlement rule enforced in the stateful layer (settle only after close + grace, latest eligible date computed from market-local clock) — src/tools/paper_trade/accounts.py:201-223
- Fail-closed on unknown data: skip a check WITH a warning rather than fabricate a value; valuation falls back to last positive close then avg_cost with a warning — engine.py:304-358, data.py:171-178
- Canonical OHLCV frame normalization (tz-naive index, fixed column set, to_numeric coerce, dedupe keep-last, sorted) + frozen lookback window + untradable-bar flagging — src/tools/paper_trade/data.py:107-168
- Per-symbol provenance on every data payload: source, fallback_used, adjustment/caliber, volume_unit, bars_as_of, plus _diagnostics attempts and a recovery hint on failure — src/market_data.py:412-452, 542-550; and dataset_fingerprint/read_identity on indicator output — src/tools/technical_indicator_tool.py:146-157, 446-454
- Result-size budget with explicit *_omitted notices and even-stride sampling with first/last pinned instead of silent truncation — src/tools/backtest_tool.py:312-336, src/market_data.py:168-186
- Tool class flags repeatable/is_readonly/deterministic/replay_after_compaction and execute() always returning a JSON string — src/agent/tools.py:13-65
- Stable replayable order ids (pt-<index>), terminal-state refusal, idempotent cancel, settle_day cancels unfinished orders, Decimal-as-string snapshot — src/tools/paper_trade/order_lifecycle.py
- Append-only portfolio snapshots with complete flag + valuation_version so equity charts never mix partial and complete rows; money stored as text — src/portfolio/store.py:54-125, 143-207
- Binance connector hygiene: frozen config dataclass, host assertion as paper/live discriminator, ccxt options adjustForTimeDifference/recvWindow/enableRateLimit, credential-free public client, redacted config dump, quantity XOR notional validation before any network call, quoteOrderQty for market-notional orders — src/trading/connectors/binance/sdk.py:60-156, 527-657, 790-879
- Cancel/flatten stays allowed under kill switch (risk-reducing) but every live action is audited — src/trading/service.py:783-808
- Whole-call failure semantics for agent/strategy faults (no partial day ever observable; per-call timeout) — src/tools/paper_trade/engine.py:14-16, strategy_exec.py:43-54
- Strict date validation: regex YYYY-MM-DD AND date.fromisoformat (3.11 accepts YYYYMMDD) and case-exact interval canonicalization (1m vs 1M) — src/tools/market_data_tool.py:24-76, 212-221

## 제외 (skip)

- ClickHouse two-leg federation and cn_a calendar truth (src/tools/paper_trade/data.py:403-471, calendar.py) — Binance has one data source and 24/7 trading; no exchange calendar needed
- A-share rules: T+1, 100-share lots, limit_up/limit_down bands, ST/suspension provenance (rules.py, limits_cn_a.py) — not applicable to crypto spot
- HK board-lot table and fallback diagnostics (rules.py lot_assumptions) — not applicable
- Corporate-action split detector (corporate_actions.py) — no splits on crypto pairs; keep only the idea of refusing to trade on an unexplained price discontinuity if we ever add a data-quality gate
- strategy_path subprocess sandbox with AST validation (strategy_exec.py) — our agents emit JSON decisions, not code; keep only the per-call timeout and whole-cycle-fail rule
- Three-market fee models (cn_a/hk/us commission, stamp tax, SEC fee) — replace with a single Binance taker/maker fee model (0.1% default, fee in base asset on buys)
- Multi-broker connector registry, TradingProfile capability matrix, OAuth/MCP transports (src/trading/service.py, profiles.py, capability_matrix.py) — single-exchange bot
- FX conversion and multi-currency portfolio valuation (src/portfolio/fx.py, normalization.py USD/CNY/HKD totals) — USDT-only accounting
- Shadow-account journal mining, PDF reporting, codegen (src/shadow_account/*) — out of scope for a live decision bot
- Binance USD-M read-only observation module (connectors/binance/usdm.py) — no derivatives trading; funding/OI data comes from public endpoints instead
- Backtest run-directory/bench bookkeeping (backtest_tool._record_bench_if_declared, run_card.json) — our backtester is simpler; take only the summary-envelope shape and size-degradation order
- Daily-bar only assumptions (settlement at 15:30/16:30 local, K=120 trading-day lookback) — rewrite for 15-minute candle closeTime + grace