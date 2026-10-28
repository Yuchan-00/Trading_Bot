# Vibe-Trading backtest module (agent/backtest): validation gates, metrics, run card, Binance reconciliation, and what a rule-only 15m Binance spot backtest should copy

# Vibe-Trading `agent/backtest` — study report

Root studied: `/home/user/hkuds/vibe-trading/agent/backtest/` (MIT, "Copyright (c) 2026 Vibe-Trading Contributors"). Files read in full: `validation.py`, `metrics.py`, `run_card.py`, `constraints.py`, `regime.py`, `risk_xray.py`, `factor_costs.py`, `benchmark.py`, `models.py`, `binance_account_reconciliation.py`, `binance_shadow_evidence.py`, `binance_tolerance_calibration.py`, `engines/crypto.py`, `loaders/binance_loader.py`, `perpetual_evidence.py`; targeted reads of `engines/base.py` (2418 lines), `runner.py`, `engines/_market_hooks.py`, `loaders/ccxt_loader.py`, `loaders/base.py`, plus the skills `strategy-generate`, `backtest-diagnose`, `execution-model`, `strategy-discovery`.

Headline: there is **no literal `NOT_VALIDATED` verdict** anywhere in `agent/backtest`. The verdict-ish concepts live in three places: (a) a numeric `validation` block (MC p-value, bootstrap CI, walk-forward consistency) that is *reported, never thresholded* by code; (b) the skill-level "Hard Gates" (`passed=false` when metrics/equity missing, exit_code != 0, equity NaN, or `trade_count == 0`); (c) the strategy-store lifecycle enum `UNVALIDATED -> IN_VALIDATION -> VALIDATED -> APPROVED` in `agent/src/strategy_store/models.py:64-75` and the strategy-discovery evidence flags `insufficient` / `marginal` / `cost_sensitive` / `stale`. The previous Trading_Bot session's `backtest.py` "게이트 판정" (gate verdict) idea is therefore *our* invention layered on these ingredients, not a Vibe-Trading feature.

---

## 1. Validation gates that exist

### 1.1 Look-ahead prevention (structural, in the engine — the strongest gate)

- **Signal shift by one bar, next-bar-open fills.** `engines/base.py:253-254` (`_align`): "Signal is shifted by 1 bar (next-bar-open semantics) then normalised so `sum(abs(weights)) <= 1.0`". Implementation at `base.py:341-344`:
  ```python
  # shift(1) + fillna(0): prepend 0, drop last
  shifted_vals = np.empty_like(sig_vals)
  shifted_vals[0] = 0.0
  shifted_vals[1:] = sig_vals[:-1]
  ```
  Signals are clipped to [-1, 1] and the row is scaled so gross weight ≤ 1 (`base.py:364-365`): `scale = pos.abs().sum(axis=1).clip(lower=1.0); pos = pos.div(scale, axis=0)` — i.e. no accidental leverage.
- **Fill price = current bar's open, slipped.** `base.py:844-846` `execution_open()` returns `bar["open"]`; every open/close goes through `apply_slippage(open_price, ±direction)` (`base.py:1552-1554`, `1602-1611`).
- **Order sizing uses only the open.** `base.py:1242-1245`: "Value the book at prices observable when orders execute. Rebalances happen at the bar open, so using close_df[ts] here would let the yet-unknown decision-bar close affect order size." → `_calc_open_equity` (`base.py:1433-1474`).
- **Price-limit base price is historical only.** `base.py:635-641` `historical_base_price`: "this engine fills at the CURRENT bar's open, so the current bar's close is not available information: using it is lookahead."
- **Warm-up boundary.** `evaluation_start_index` (`base.py:~170-242`) reads `warmup_bars` **or** `evaluation_start_date` (declaring both raises). Signals are computed over the full panel, then `dates/close/target_pos/ret` are sliced from `warmup_end` (`base.py:962-973`), so "no trade, no equity point, no benchmark return and no metric can come from them." Needs ≥2 evaluated bars.
- **Regime smoothing is causal.** `regime.py:84-85`: "Trailing mean = causal. A centered window here silently reads the future." `risk_xray.py:15-16`: "Look-ahead is structurally impossible: all statistics are computed over trailing returns only."
- **`delayed_execution` refuses negative delay** (skill `execution-model` + `src/quantlib/impact.py:295`): a negative `delay_bars` "would pull future signal values into the past, which is look-ahead bias".
- **Event/news enrichment is point-in-time**: `_maybe_enrich_events` attaches "a point-in-time-safe `event_score` column before signal generation" with `as_of=config["end_date"]` (`base.py:483-498`).

### 1.2 Cost / slippage

- Crypto engine defaults (`engines/crypto.py:46-60`): `maker_rate=0.0002`, `taker_rate=0.0005`, `slippage=0.0005`, `funding_rate=0.0001` per 8h settlement, `leverage=1.0`.
  ```python
  rate = self.taker_rate if self.perpetual_strict or is_open else self.maker_rate   # crypto.py:125
  return price * (1 + direction * self.slippage_rate)                                # crypto.py:132
  ```
  Opens pay taker, closes pay maker (non-strict mode). Sizes rounded to 6 decimals (`round_size`, `crypto.py:117`). **No Binance LOT_SIZE / MIN_NOTIONAL / tick filters anywhere** — a gap for our bot.
- Commission is charged on both legs and included in `TradeRecord.commission` (`base.py:2158-2177`); `_close_position` does `self.capital += margin + pnl - exit_comm`.
- Capital-fit search: if the planned basket costs more than cash, the engine bisects one common scale factor over all target weights (50 iterations) rather than clipping names in order (`base.py:1287-1349`); the dropped sleeve is reported once as `insufficient_capital` in `plan_rejections`.
- Plan rejection reasons (`_plan_open_order`, `base.py:1581-1621`): `no_target_weight`, `already_held`, `no_data`, `no_bar`, `execution_blocked`, `invalid_price`, `zero_size`, plus `insufficient_capital`. Counted into metrics via `_plan_rejection_metrics()` so a strategy that "trades zero times while the run reports a normal result" is visible.
- `factor_costs.py` (weight-space, factor benches only): ADV participation cap `DEFAULT_MAX_PARTICIPATION = 0.10` (line 70), spread cost `turnover * slippage_bps / 10_000` (line 364), sqrt impact `traded * coeff * vol * sqrt(participation)` (line 393-395), borrow cost for shorts. Key design rules worth copying as text: "Every cost this module returns is a POSITIVE drag, to be subtracted from a gross return" (line 35-37); unfilled capacity is carried forward, "It is not silently filled, and it is not silently dropped: both would flatter the backtest" (line 11-13). `src/quantlib/impact.py` constants: `DEFAULT_SLIPPAGE_BPS = 5.0`, `DEFAULT_LINEAR_IMPACT_COEFF = 0.1`, `DEFAULT_SQRT_IMPACT_ETA = 0.5`, `DEFAULT_DELAY_BARS = 1`.
- Skill `execution-model` reference table (SKILL.md:56-58): BTC spot 2-5 bps slippage, ETH spot 3-8 bps, small alts 10-50 bps; crypto one-way total ≈0.1%, round-trip ≈0.2%; recommendation "Crypto: `commission = 0.001` (including slippage)". Sensitivity table pattern: rerun at 0/3/5/10/20 bps and report return/Sharpe/MaxDD per row (SKILL.md:279-291).
- Funding (perp only, not for our spot bot): `calc_crypto_funding_fee` in `_market_hooks.py:479-546` settles at hours {0,8,16} UTC, dedups per (symbol,date,hour), and for bars ≥ 8h charges `max(1, span // 8)` settlements per bar (#1290 fix).

### 1.3 Monte Carlo permutation test — `validation.py:30-117`

- Null: "the observed Sharpe / max-drawdown is no better than a random ordering of the same trades." Shuffles the trade-PnL sequence `n_simulations` (default 1000, seed 42) times with `np.random.default_rng(seed)`; each path's equity = `initial_capital + cumsum(shuffled)`, prefixed with the starting cash (`_path_metrics`, line 126).
- p-value is a one-sided count: `p_value_sharpe = #(sim_sharpe >= actual_sharpe) / n`; `p_value_max_dd = #(sim_max_dd >= actual_max_dd) / n` ("less negative = better").
- Returns: `actual_sharpe, actual_max_dd, p_value_sharpe, p_value_max_dd, simulated_sharpe_mean/std/p5/p95, n_simulations, n_trades, sharpe_samples[]` and an `equity_paths` fan chart (steps, actual, band_p5/p25/p50/p75/p95, 30 sample paths) when `n_sim * n_trades <= 2_000_000`.
- Guards: needs ≥3 trades, integer `n_simulations >= 1`, `seed >= 0` (bools rejected). Errors are returned as `{"error": ..., "p_value_sharpe": 1.0}` — i.e. **fail-closed to p=1.0**, not an exception.
- Sharpe inside the test is per-trade (returns = diff/prev equity along the trade sequence) scaled by `sqrt(bars_per_year)` — a known simplification; it is only comparable *within* the permutation set.

### 1.4 Bootstrap Sharpe CI — `validation.py:144-221`

- Resamples per-bar returns with replacement (`n_bootstrap` default 1000, `confidence` default 0.95, seed 42). Returns `observed_sharpe, ci_lower, ci_upper, median_sharpe, prob_positive` (fraction of bootstrap Sharpes > 0), plus `sharpe_samples` when `n_bootstrap <= 20_000`.
- Uses `period_returns(equity, initial_capital)` so the first bar's return is measured from starting cash (same convention as `calc_metrics`), inf→0, NaN dropped, needs ≥5 observations.

### 1.5 Walk-forward — `validation.py:232-319`

- **Non-overlapping sequential split of the *realized equity curve*** into `n_windows` (default 5) equal windows; it is NOT train/test re-fitting. Per window: `return = eq[-1]/eq[0]-1`, Sharpe from adjacent pct-changes, max DD inside the window, trade count (trades whose `entry_time` falls in the window), win rate.
- Consistency summary: `profitable_windows`, `consistency_rate = profitable/n`, `return_mean/std`, `sharpe_mean/std`. Needs ≥ `2 * n_windows` bars and finite start/end equity in every window (otherwise `{"error": ...}`).

### 1.6 Dispatcher & standalone CLI

- `run_validation(config, equity, trades, initial_capital, bars_per_year)` (`validation.py:325-389`) runs whichever of `monte_carlo` / `bootstrap` / `walk_forward` keys exist in `config["validation"]`; `bars_per_year=None` resolves through `effective_bars_per_year(index)`.
- Engine hook: `base.py:1151-1163` — only runs if `config.get("validation")` is truthy; result stored at `metrics["validation"]` and written with `write_validation_json` (strict RFC-8259: NaN/inf → null, `allow_nan=False`).
- `python -m backtest.validation <run_dir>` re-runs all three on saved `artifacts/equity.csv` + `trades.csv`, reconstructing `TradeRecord`s from `event == "exit"` rows and reading `bars_per_year` from `run_card.json["annualisation"]` (`_saved_bars_per_year`, line 523-560).
- **No thresholds are applied in code.** The skill text says only "is Sharpe significantly better than random?" (`strategy-generate/SKILL.md:179`). Interpretation (p<0.05, ci_lower>0, consistency_rate≥0.6, etc.) is left to the reader/LLM.

### 1.7 Benchmark comparison

- `benchmark.py:24-33` `MARKET_BENCHMARKS`: `"crypto": "BTC-USDT"`; `_infer_market` treats `source in ("okx","ccxt","binance")`, `/` in symbol, or `-USDT/-USDC/-USD/-BTC/-ETH` suffixes as crypto (line 185-187).
- Default benchmark when none declared = **equal-weight basket of the traded symbols** (`bench_ret = ret_df.mean(axis=1)`, `base.py:984`). If a declared ticker cannot be fetched, a run-card warning says metrics "are graded against the equal-weight basket of the traded symbols instead" (`base.py:1027-1031`).
- Benchmark return is re-measured over the *evaluated* window (`BenchmarkResult.total_return_over(dates)`, `benchmark.py:43-66`) so a warm-up boundary cannot create a mismatched-window comparison (#1240), and it is a price relative `last/first - 1`, not `prod(1+r)-1` (#872, `metrics.py:315-340`).
- `calc_metrics` benchmark block (`metrics.py:675-707`): `benchmark_return`, `excess_return = total_ret - bench_return`, `information_ratio = mean(active)/std(active)*sqrt(bpy)`, `tracking_error = std(active)*sqrt(bpy)`, `benchmark_beta = cov(port,bench)/var(bench)`.

### 1.8 Annualisation sanity gate (served-bar spacing) — `runner.py:1575-1728`

- `_INTERVAL_SECONDS` table (`15m: 900`), `_SPACING_MISMATCH_RATIO = 1.5`, `_MIN_BARS_FOR_SPACING = 4`. Median spacing of served bars is compared with the declared interval; on mismatch the run annualises at the interval the data actually matches and appends a warning to the run card ("The loader served bars coarser or finer than requested").

### 1.9 OHLC invariant gate — `loaders/base.py:191-258`

`validate_ohlc(frame, strategy="drop"|"warn"|"raise")`: structural `(high<low)|(high<open)|(high<close)|(low>open)|(low>close)` plus non-positive prices are dropped by default, logged. CCXT loader also raises on **incomplete history** (`ccxt_loader.py:512-516`: "incomplete CCXT history ... requested [..) received [..]") and uses retry-with-budget (`CCXT_TIMEOUT_MS=15000`, `CCXT_FETCH_BUDGET_S=60`, page limit 1000, ≤200 pages).

### 1.10 Hard gates (skill level, the closest thing to a verdict)

`strategy-generate/SKILL.md:187-199` and `backtest-diagnose/SKILL.md:57-67`:
1. `artifacts/metrics.csv` exists & non-empty; 2. `artifacts/equity.csv` exists & non-empty; 3. `exit_code == 0`; 4. equity has no NaN; 5. `trade_count > 0`. Any failure → `passed=false`; otherwise score ≥ 60 → passed; "Poor return / low Sharpe alone should not push the score below 60". Machine tokens: `hard-gate:exit-nonzero`, `hard-gate:metrics-missing`, `hard-gate:zero-trades`, `hard-gate:equity-empty`, `hard-gate:equity-nan`. A failing run "produces no evidence rows — never partial rows".
Bug categories: zero trades, first trade > 2 years after start, capital utilisation < 50%, open position at end.

### 1.11 Evidence-quality flags (strategy-discovery skill, `SKILL.md:94-158`)

| Condition | Flag |
|---|---|
| trade count < 10 | `insufficient` ("the row carries no claim") |
| coverage < 2 years | `marginal` |
| breakeven < 5 bps | `cost_sensitive` |
| evidence window end ≥ 180 d old (or unparseable) | `stale` (excluded, fail-closed) |
| 90–180 d | `aging` |

Cost screen formula (single-position strategies only): `breakeven_bps = ln(1 + g) / (2 · n · s) · 10⁴` (g = gross edge, n = trades, s = avg sizing). Multi-position runs get `breakeven_fee_bps = null` + warning because no closed form exists; "A null is a better answer than a number that is wrong by a factor between 1.1 and 6.5." They deliberately refuse to publish an `estimated_net_sharpe`.

---

## 2. Metric definitions (`metrics.py`)

- **Annualisation table**: `_TRADING_DAYS["binance"]=365`, `_BARS_PER_DAY["15m"]["binance"]=96` → **35,040 bars/year for 15m crypto** (`calc_bars_per_year`, line 188-208; alias `binance → ccxt`). `effective_bars_per_year(index)` = `n_bars / (calendar_days/365.25)` for cross-market or `None` (line 211-247).
- **Returns**: `period_returns` (line 547-560): `pct_change`, but `ret[0] = equity[0]/initial_cash - 1` so `prod(1+r)-1 == equity[-1]/initial_cash - 1`. `bar_returns` (line 255-312): defined only when previous price is finite & > 0, else 0.0; ffill before shift.
- **`calc_metrics`** (line 563-730):
  - `total_return = equity[-1]/initial_cash - 1`
  - `annual_return = (1+total)^(bpy/n) - 1`; total ≤ -1 → -1.0; overflow → inf→ reported as null.
  - `sharpe = mean(r)/(std(r)+1e-10)*sqrt(bpy)` (ddof=1, 0 if non-finite or n ≤ 1). **No risk-free rate.**
  - `max_drawdown`: `peak = equity.cummax().clip(lower=initial_cash)` — starting cash is the initial high-water mark; `dd = (equity-peak)/peak`.
  - `calmar = annual_return / |max_dd|`
  - `sortino = mean(r) / downside_dev * sqrt(bpy)` with **zero-target downside RMS over all periods**: `sqrt(sum(r[r<0]^2) / len(r))` (line 647-657).
  - `win_rate`, `profit_loss_ratio = avg_win/avg_loss`, `profit_factor = gross_profit/gross_loss` (both `None` when no losing trade, line 370-378), `max_consecutive_loss`, `avg_holding_days` (actually bars), `trade_count`.
  - Turnover: `0.5 * sum|w_t - w_{t-1}|` from weights (line 454-476) or, preferred, from fill evidence: `traded_margin / (2*equity)` (line 522-544) → `avg_turnover`, `total_turnover`.
  - Benchmark block as in §1.7.
- `by_symbol_stats` {count, win_rate, total_pnl, avg_pnl}; `by_exit_reason_stats` {count, total_pnl}.
- `risk_xray.py`: HHI, `effective_n = 1/HHI`, top1/top3 weight, `annualized_vol = std*sqrt(ppy)`, downside deviation, max DD with start/trough dates, **historical VaR/ES at 95/99** (`var = quantile(losses, level)`, `es = mean(losses >= var)`), diversification ratio `w·σ / σ_p`, avg |pairwise corr|, max pair, beta to equal-weight. `MIN_HISTORY_DAYS = 30`. Long-only only.
- `regime.py`: edge density = fraction of pairs with rolling |ρ| ≥ 0.5 over 60 bars; 5-bar trailing mean; hysteresis FUSED enter 0.65 / exit 0.45 ("Descriptive risk context — not a trading signal").
- `constraints.py`: `max_weight` cap with pro-rata redistribution, `min_weight` floor, `group_exposure` caps — applied on signed weights, never opens/closes positions.

## 3. Run-card format (`run_card.py`, `SCHEMA_VERSION = "1.0"`)

`write_run_card(run_dir, config, metrics, data_sources, strategy_path, warnings, artifact_refs, tool_traces)` writes `run_card.json` (sorted keys, `allow_nan=False`, non-finite → null) and `run_card.md`. Top-level keys:

```
schema_version, generated_at (UTC Z), run_dir,
backtest: {codes, start_date, end_date, interval, engine, initial_cash, source},
reproducibility: {config_hash (sha256 of config.json), strategy_hash (sha256 of code/signal_engine.py)},
data_sources: [...],
metrics: {scalar metrics only},
structured_metrics: {non-scalar metrics ≤ 4096 bytes each, else listed under "_omitted"},
validation: {monte_carlo, bootstrap, walk_forward} (own key, not duplicated in metrics),
warnings: [content-filter, caliber, annualisation, unrecognized-config, benchmark, model-provenance warnings],
model_provenance: {files:{config.json, code/signal_engine.py → provider/model_id/model_source}, training_cutoff, cutoff_source},
annualisation: {bars_per_year},
artifacts: [{path, size_bytes, sha256}] for config.json, code/signal_engine.py, artifacts/**,
citations: [{metric, artifact_id:"artifacts/metrics.csv", column, row:1, sha256}] (only values that match the CSV cell exactly),
tool_traces: [{tool, args_hash, started_at, ended_at, status, result_hash}],
artifact_refs (optional)
```
Markdown sections in order: Backtest Summary, Reproducibility, Model provenance, Data Sources, Metrics, Structured metrics, Validation, Warnings, Execution records, Metric evidence, Artifacts.
Notable rule: the LLM-training-cutoff check — if `end_date < model_training_cutoff` the card warns "results may reflect what the model remembers" (`run_card.py:145-164`). Artifacts written by the engine (`base.py:2267-2381`): `ohlcv_<code>.csv`, `equity.csv` (ret, equity, drawdown, benchmark_equity, active_ret), `positions.csv` (execution truth) vs `target_positions.csv` (requests), `trades.csv` (two rows per round-trip, `event ∈ {entry, exit}`, columns timestamp/code/side/price/qty/reason/pnl/holding_days/holding_bars/return_pct/event), `fills.jsonl` (immutable, append-only), `metrics.csv` (one row, flat), `validation.json`, `risk_xray.json/.md`, `rebalance_notes.json/.md`.

## 4. Binance-specific reconciliation / tolerance (`binance_*.py`) — USD-M futures only

These are **offline comparison** tools for perpetual accounts, not for spot, and never for order execution. Still, the pattern transfers directly to a spot paper/testnet bot.

- `ReconciliationTolerance` (`binance_account_reconciliation.py:223-236`): `absolute=1e-8`, `relative=1e-8`, `max_timestamp_skew_seconds=0.0`, `version="reconciliation-tolerance-v1"`. Comparison rule (line 281-294):
  ```python
  absolute_delta = abs(local_value - exchange_value)
  allowed_delta = max(tolerance.absolute, tolerance.relative * max(abs(local_value), abs(exchange_value)))
  within_tolerance = absolute_delta <= allowed_delta
  ```
- `reconcile_binance_account(local_account, local_risk, exchange_snapshot, expected_timestamp, tolerance)` → `ReconciliationReport{status="comparison_complete", has_drift, comparisons[], missing_on_exchange[], unexpected_on_exchange[], structural_differences[], fidelity_flags, comparison_scope="account_snapshot_fields_only", liquidation_engine_assessment="not_assessed"}`. `has_drift = missing or unexpected or structural or any(!within_tolerance)`. Raises when snapshot is not `complete` or timestamp skew exceeds tolerance.
- Snapshot normalisation fails closed: requires `status=="ok"`, `schema_version=="binance-usdm-account-observation-v1"`, `source_profile=="binance-live-sdk-readonly"`, SHA-256 `configuration_hash`, fidelity flags `{"client_observation_time","sequential_signed_reads"}`, zero open-order margin, canonical `*-USDT-PERP` symbols.
- `binance_shadow_evidence.py`: `build_binance_drift_evidence` returns either a `comparison_complete` record (severity `drift`/`none`) or a `comparison_rejected` record with `rejection{code,message,details}` (codes `snapshot_incomplete`, `snapshot_unsupported_provenance`, `timestamp_skew`). `write_binance_drift_evidence` appends to `artifacts/binance_drift.jsonl` and atomically replaces `artifacts/binance_drift_summary.json` (counts: out_of_tolerance_count, missing_symbol_count, unexpected_symbol_count, structural_difference_count, rejection_code). `_validate_evidence` re-derives `has_drift` and rejects internally inconsistent records.
- `binance_tolerance_calibration.py`: `calibrate_tolerance(samples, version, min_samples_per_field=4, safety_factor=2.0, floors=1e-8)` → per-field `proposed_absolute = max(floor, 2×max|Δ|)`, `proposed_relative = max(floor, 2×max relative Δ)`; the tolerance is the max over fields; `verify_tolerance_covers` lists samples a tolerance does not bound. "A calibrated tolerance is evidence about recorded observations only; it never relaxes validation or touches accounting state."
- Perp evidence (`perpetual_evidence.py`): summary carries explicit `assumptions` (`single_asset_usdt`, `no_open_order_margin`, `one_way_positions`, `bar_extrema_order_unknown`, `not_an_identical_binance_liquidation_engine_reproduction`) and `fidelity_flags` — honesty labels we should mirror.
- Loader: `loaders/binance_loader.py` is a 44-line subclass of the CCXT loader forcing `ccxt.binance` (spot) / `ccxt.binanceusdm` (swap), `enableRateLimit=True`, no API key. `-PERP` codes fetch trade + mark candles, require aligned indices, and verify every 0/8/16h bar has a funding record. Binance settlement timestamps are rounded to the second because they "carry millisecond jitter (e.g. 08:00:00.011)" (`ccxt_loader.py:428-433`).

## 5. What a rule-only 15m Binance SPOT backtest in our bot should copy

Context from the previous bot (`/home/user/Trading_Bot/docs/research/previous_session_summary.md:23`): `backtest.py` did "선행편향 차단, 비용·스트레스 테스트, 워크포워드 3구간, 몬테카를로, 게이트 판정" and the LLM decisions were explicitly not reproducible ("규칙 전용 기준선" = rule-only baseline). Keep that framing: the backtest validates the *deterministic rule layer* (entry/exit rules, SL/TP ladder, hard limits), while LLM agents are only fed the gate verdict as context.

**Copy (formulas/mechanics, rewritten in our words):**
1. Decision on bar t close → fill at bar t+1 open × (1 ± slippage) — the `_align` shift + `execution_open` pattern. Size orders from equity marked at the fill bar's open, never the close.
2. Warm-up slice: compute indicators on the full history, then drop `warmup_bars` from evaluation (refuse if both `warmup_bars` and `evaluation_start_date` given; need ≥2 bars).
3. Cost model for spot: taker fee both legs (Binance spot 0.10%, 0.075% with BNB) + fixed slippage 5 bps BTC/ETH, 10–50 bps alts; run the 0/3/5/10/20 bps sensitivity table. Market orders only → always taker; drop the maker/taker split.
4. Metrics exactly as `calc_metrics` with `bars_per_year = 365*96 = 35,040`: total/annual return, Sharpe (no rf), Sortino (zero-target RMS over all bars), max DD with initial cash as first HWM, Calmar, win rate, profit factor (None when no losses), max consecutive losses, avg holding bars, turnover from fills, benchmark = BTC-USDT buy-and-hold **and** equal-weight basket, measured over the same evaluated window as a price relative.
5. The three statistical checks with the same outputs: MC permutation (p_value_sharpe, p_value_max_dd, fan-chart bands; p=1.0 and `error` when < 3 trades), bootstrap Sharpe CI (ci_lower/upper, prob_positive), walk-forward on the realized curve (consistency_rate). Keep seeds in the output.
6. Hard gates + evidence flags as a *verdict layer we add*: `NOT_VALIDATED` when any hard gate fails (no equity/metrics, NaN equity, 0 trades, nonzero exit) or `trades < 10` (`insufficient`); `MARGINAL` when coverage is short or `breakeven_bps < 5`; `VALIDATED` only if gates pass and the stat checks clear thresholds we choose (e.g. p_value_sharpe < 0.05, ci_lower > 0, consistency_rate ≥ 0.6). Vibe-Trading never thresholds in code; we should, but record the thresholds in the run card.
7. Plan-rejection accounting: count `zero_size`, `insufficient_capital`, `execution_blocked`, etc., so a rule that never trades is visible. For spot add `below_min_notional` and `lot_rounded_to_zero` using Binance `LOT_SIZE`/`MIN_NOTIONAL` filters (Vibe-Trading lacks these).
8. Artifacts: `equity.csv` (ret/equity/drawdown/benchmark_equity/active_ret), `trades.csv` (entry+exit rows, `event`, `reason`), `fills.jsonl` (immutable), `metrics.csv` (single flat row), `validation.json`, and a `run_card.json`/`.md` with config hash, strategy hash, data sources, bars_per_year, warnings, artifact sha256 list, and the verdict. Serialize with `allow_nan=False` and NaN→null.
9. Data hygiene from the loader: `validate_ohlc` drop rule, raise on incomplete paginated history, median-spacing check vs declared 15m (ratio 1.5), and rate-limited ccxt with timeout/budget.
10. Reconciliation pattern for paper/testnet: compare local positions/balances vs exchange snapshot with `allowed = max(abs_tol, rel_tol × max(|local|,|exchange|))`, versioned tolerance, timestamp-skew check, missing/unexpected symbol sets, append-only drift JSONL + latest summary; a drift verdict is evidence, never an automatic accounting correction. Calibrate tolerance from ≥4 samples per field × safety factor 2. Send a Telegram alert on `has_drift`.
11. Honesty labels: carry `assumptions` and `fidelity_flags` in the summary (e.g. `bar_open_fill`, `no_partial_fills`, `no_order_book_depth`, `llm_decisions_not_reproduced`).

**Adapt:** `rebalance_tolerance`/`position_adjustment` are for weight-driven strategies; our bot is entry/exit-driven ("hold" mode) with ≤3 positions and 10%/symbol caps — encode those as hard limits inside the simulated risk layer rather than as weight constraints. Risk-xray VaR/ES (historical 95/99) is cheap to add for the dashboard. Correlation-regime hysteresis could feed a "FUSED market" flag to the risk agent.

**Skip:** everything perpetual/USD-M (funding, mark price, liquidation tiers, maintenance brackets, `perpetual_strict`), `factor_costs.py` (ADV caps, borrow, sqrt impact — weight-space factor benches), optimizers, group-exposure constraints, model-provenance/training-cutoff warnings (replace with a simpler "LLM not reproduced in backtest" note), yfinance benchmark fallback, and multi-market annualisation (`effective_bars_per_year`).


## 채택 후보 (adopt)

- Next-bar-open fill with 1-bar signal shift and open-based sizing (engines/base.py:253-254, 341-344, 844-846, 1242-1245)
- Warm-up boundary: compute indicators on full panel, evaluate only after warmup_bars/evaluation_start_date; refuse both keys (engines/base.py evaluation_start_index ~170-242, 962-973)
- Monte Carlo trade-order permutation test with one-sided p-values and fan-chart bands; p=1.0 fail-closed when <3 trades (validation.py:30-138)
- Bootstrap Sharpe CI with ci_lower/ci_upper/prob_positive (validation.py:144-226)
- Walk-forward consistency on realized equity: profitable_windows / consistency_rate / per-window Sharpe, DD, win rate (validation.py:232-319)
- Metric definitions: cash-seeded period_returns, Sharpe/Sortino(zero-target RMS)/Calmar, max DD with initial cash as HWM, profit_factor None when no losses, fill-based turnover (metrics.py:343-402, 547-730)
- bars_per_year for 15m Binance = 365 x 96 = 35,040 (metrics.py:23-49, 92-104, 188-208)
- Benchmark measured over the evaluated window as a price relative, with equal-weight basket fallback and explicit warning (benchmark.py:43-66, metrics.py:315-340, engines/base.py:984-1031)
- Hard gates -> passed=false and machine tokens hard-gate:* (skills/strategy-generate/SKILL.md:187-199, skills/backtest-diagnose/SKILL.md:57-67)
- Evidence flags insufficient(<10 trades)/marginal/cost_sensitive(<5 bps)/stale and breakeven_bps = ln(1+g)/(2 n s) x 1e4 (skills/strategy-discovery/SKILL.md:94-130)
- Plan-rejection reason counters so zero-trade sleeves are visible (engines/base.py:1581-1621, 1325-1349)
- Run card schema: config/strategy sha256, bars_per_year, warnings, artifact sha256 manifest, validation block, strict JSON NaN->null (run_card.py:168-255)
- Artifact set: equity.csv, trades.csv with event entry/exit, fills.jsonl immutable, metrics.csv single row, validation.json (engines/base.py:2267-2381)
- Crypto cost defaults and sensitivity table: taker 0.05%/slippage 5 bps; rerun at 0/3/5/10/20 bps (engines/crypto.py:46-60,125-132; skills/execution-model/SKILL.md:47-58, 279-291)
- OHLC invariant drop, incomplete-history raise, median bar-spacing vs declared interval check ratio 1.5 (loaders/base.py:191-258, loaders/ccxt_loader.py:509-516, runner.py:1588-1630)
- Reconciliation tolerance rule allowed = max(abs, rel x max(|local|,|exchange|)), versioned, timestamp skew, missing/unexpected symbols, append-only drift JSONL + summary (binance_account_reconciliation.py:223-294, 314-437; binance_shadow_evidence.py:65-240)
- Tolerance calibration: >=4 samples per field, safety factor 2, floors 1e-8, verify_tolerance_covers (binance_tolerance_calibration.py:84-158)
- Explicit assumptions/fidelity_flags lists in summaries (perpetual_evidence.py:58-64)
- Causal-only smoothing and trailing-only statistics as a stated invariant (regime.py:84-85, risk_xray.py:15-16)
- Historical VaR/ES 95/99 and HHI/effective_n for the dashboard risk panel (risk_xray.py:185-263)

## 제외 (skip)

- All USD-M perpetual machinery: funding settlement, mark-price candles, liquidation tiers, maintenance brackets, perpetual_strict, margin modes (engines/crypto.py, engines/_market_hooks.py:471-591, perpetual_risk.py, loaders/ccxt_loader.py:327-440)
- factor_costs.py ADV participation caps, sqrt/linear impact and borrow cost (weight-space factor benches, not a 3-position spot bot)
- Portfolio optimizers and constraints.py group-exposure/min-weight layers
- position_adjustment=rebalance / rebalance_mask / rebalance_tolerance semantics (entry/exit-driven bot uses hold-style logic)
- Model-provenance and training-cutoff warnings in run_card.py (replace with a single 'LLM decisions not reproduced' note)
- yfinance benchmark fallback and multi-market effective_bars_per_year annualisation
- Binance snapshot schema/profile strings (binance-usdm-account-observation-v1, binance-live-sdk-readonly) — define our own spot schema
- Maker/taker fee split (market orders only -> always taker)
- Strategy-store lifecycle enum and SDM evidence cache (overkill for a single bot)