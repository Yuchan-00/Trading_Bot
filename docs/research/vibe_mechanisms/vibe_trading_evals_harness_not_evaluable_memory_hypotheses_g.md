# Vibe-Trading evals harness (NOT_EVALUABLE), memory/hypotheses/goal persistence, and trade-journal / thesis-tracker / performance-attribution skills — what to adopt for the Binance spot bot's journal + post-trade review + Telegram

# Report: evaluation honesty, cross-session memory, and post-trade review in Vibe-Trading (hkuds/vibe-trading, MIT)

All paths below are under `/home/user/hkuds/vibe-trading/agent/` unless absolute. No files were modified.

---

## 1. The evals harness — how evaluation refuses to "pass" when instrumentation is missing

**Files:** `evals/harness/{README.md, schema.py, artifacts.py, assertions.py, observations.py, methodology.py, report.py, runner.py}`, example case `evals/harness/cases/identity/explicit_a_share.json`. Test: `tests/harness_eval/test_runner.py`.

### 1.1 What it is
A deterministic, offline, read-only verifier of a *completed* agent run. It never calls an LLM, tool, or network. `README.md:3`:

> "This package checks a completed agent run from persisted files. It is deterministic and read-only: it does not call an LLM, invoke a tool, load market data, or rewrite the case prompt."

It reads a fixed list of artifact files only (`artifacts.py:61-68`): `req.json`, `state.json`, `artifacts/grounding_evidence.json`, `llm_usage.json`, `run_manifest.json`, `trace.jsonl` (+ `prompt_path`/`content_path` sidecars). Paths are hardened: `_safe_known_path` refuses anything resolving outside the root (`artifacts.py:90-96`), JSON size capped at 16 MB, trace at 64 MB. Multi-turn traces are cut to the records from the **last** `{"type":"start"}` event (`artifacts.py:144-150`).

### 1.2 Four-valued verdicts — the core idea
`schema.py:11-17`:
```python
class VerdictStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
```
The README states the rule in one sentence (`README.md:14`): **"Missing future instrumentation is never treated as success."**

The dispatcher that decides between the two non-pass/fail states (`assertions.py:51-62`):
```python
def _artifact_problem(bundle, key, *, missing_invalid: bool):
    if key in bundle.errors:
        return VerdictStatus.INVALID_ARTIFACT
    if key not in bundle.present:
        return (VerdictStatus.INVALID_ARTIFACT if missing_invalid
                else VerdictStatus.NOT_EVALUABLE)
    return None
```
Semantics:
- **INVALID_ARTIFACT** — a *mandatory* artifact is missing or corrupt (`req.json`, `state.json`, `trace.jsonl`, `grounding_evidence.json` when identity/evidence is asserted), or artifacts contradict each other. Exit code **2** (`runner.py:81-82`).
- **NOT_EVALUABLE** — the artifact is present and valid, but the *specific field* the assertion needs was never instrumented in this run. Exit code **0** but the gap is preserved in the report (`README.md:18`: "`NOT_EVALUABLE` does not fail the command because the report preserves its coverage explicitly").
- **FAIL** — exit code 1.

Concrete NOT_EVALUABLE sites (each carries a `detail` string explaining *why*):
- `completion.status` / `safety.status` when `state.json` lacks `completion_status`/`safety_status` (`assertions.py:229-240`): `detail=f"{field_name} is not instrumented in this run"`. The case schema only allows `on_missing: "not_evaluable"` (`schema.py:88-91`) — a case author cannot opt into "missing = pass".
- `tools.false_skips` (`assertions.py:330-343`): `"current tool_skipped records omit arguments and policy, so false skips cannot be classified"`.
- `identity.constraint` when no `resolution_constraints` records exist (`assertions.py:423-433`): `"constraint provenance is not instrumented in this run"`.
- `budget.max_llm_calls`/`max_iterations` when the `end` record has no `iterations` (`assertions.py:549-559`).
- `budget.max_input_tokens` when `llm_usage.json` is absent (`missing_invalid=False`, `assertions.py:578`) **or** when `totals.calls != iterations` (`assertions.py:602-620`, `"provider usage is missing for one or more LLM calls"`) — partial usage data is refused rather than summed.
- `budget.max_visible_tools`/`max_tool_schema_tokens` always NOT_EVALUABLE (`assertions.py:638-650`, "requires per-LLM-call tool-surface instrumentation").
- `methodology.manifest_integrity` when `run_manifest.json` absent (`methodology.py:21-28`).

Cross-artifact consistency is **INVALID_ARTIFACT**, not FAIL: `execution.reconciled_status` compares `state.json#/status` with every `trace.jsonl` `end` record (after normalising `"error"`→`"failed"`, `assertions.py:776-777`) and emits `detail="terminal artifacts disagree"` if they differ (`assertions.py:179-188`). `tools.result_join` (`assertions.py:713-773`) requires every `tool_call` to have a unique string `call_id`, every `tool_result` to join to a call with the same tool name and a status in `{ok, error}`, and counts `unfinished`/`cached`/`skipped`/`blocked` outcomes.

### 1.3 What is measured (the `expect` contract, `schema.py:79-171`)
| Section | Assertion codes | Source |
|---|---|---|
| prompt | `prompt.request_preserved` (req.json == case prompt), `prompt.trace_preserved` (first user `message` in trace) | req.json, trace |
| execution | `execution.reconciled_status` ∈ allowed | state.json + trace `end` |
| completion / safety | `completion.status`, `safety.status` ∈ allowed | state.json `completion_status`, `safety_status` |
| tools | `tools.required.N` (name + `min_calls` + `arguments` subset-match), `tools.forbidden.N`, `tools.false_skips` | trace `tool_call` |
| identity | `identity.status`, `identity.authorized_symbols` (set equality), `identity.forbidden_symbols`, `identity.constraint` | `grounding_evidence.json#/identity` |
| evidence | `evidence.required_symbols`, `evidence.forbidden_issue_codes` (e.g. `numeric_claim_conflict`, `unsourced_symbol_figures`) | `grounding_evidence.json#/evidence`, `#/validations` |
| budget | `max_llm_calls`, `max_iterations`, `max_input_tokens`, `max_visible_tools`, `max_tool_schema_tokens` | trace `end.iterations`, `llm_usage.json#/totals` |
| risk | `risk.forbidden_successful_tools` (only `tool_result status=ok` or `tool_result_cached` count), `risk.authorization_bypasses` (a successful result for a call flagged `blocked: true`, or any `authorization_bypass` record) | trace |
| methodology | `methodology.manifest_integrity` (`RunManifest.verify_hash()`) | run_manifest.json |

Subset matching (`observations.py:9-27`): expected dict keys must exist and recursively match; expected lists match if each item matches *some* observed item. `within_limit` refuses bools (`observations.py:105-113`).

`llm_usage.json` reconciliation (`observations.py:75-102`): `totals.{input_tokens,output_tokens,total_tokens,calls}` must be non-negative ints; `per_iteration` sums must equal totals; `totals.calls == len(per_iteration)`. Any mismatch → INVALID_ARTIFACT.

### 1.4 Aggregation preserves coverage
`report.py:40-58` emits `evaluation_coverage = (PASS+FAIL)/all`, plus per-metric `pass_rate` computed **only over evaluable verdicts** and separate `not_evaluable`/`invalid_artifact` counts (`report.py:75-84`). Pass rate is `None` when nothing was evaluable. This is the pattern to copy: a dashboard number must never silently include unmeasured cases.

### 1.5 Run manifest (methodology fingerprint)
`src/governance/manifest.py` docstring: system prompt stored only as a hash, each skill as `(name, content_hash)`, tool registry as sorted names + hash, curated package versions; `timestamp`/`run_id` recorded but **excluded** from `manifest_hash` so two runs under identical methodology hash identically. "No hidden clock" — caller passes the timestamp. For the bot: hash prompts + skill texts + model ids + risk-limit constants per cycle so a later post-mortem can say *which* prompt version made a trade.

---

## 2. Memory, hypotheses, goals — storage and recall across sessions

### 2.1 PersistentMemory (`src/memory/persistent.py`) — markdown files + index
- Location: `<runtime_root>/memory/` (`persistent.py:22`). One `.md` per memory with YAML frontmatter; an index `MEMORY.md` capped at 200 lines (`MAX_INDEX_LINES`) and body capped at 8000 chars.
- Frontmatter written by `add()` (`persistent.py:566-583`): `name, description, type (user|feedback|project|reference), id (6-hex), created_at, updated_at, keywords, quality_score 0.5, access_count 0, last_accessed, importance 0.5, related_memories, category, compression_level: raw`.
- Index line format: `- [{title}]({relative/path.md}) — {description}` (`persistent.py:720`).
- **Recall paths:** (a) the frozen `MEMORY.md` snapshot is injected into the system prompt once per session (`src/agent/context.py:279-284, 329-334`) — frozen deliberately so the prompt prefix stays cacheable; (b) per-turn `find_relevant(user_message, max_results=3)` whose hits are prepended to the user message inside `<recalled-memories>` (`context.py:411-424`); (c) explicit `remember` tool with `save|recall|forget|reinforce` (`src/tools/remember_tool.py:19-68`).
- Search: optional SQLite FTS5 (`search_index.py`) with fallback token-overlap scoring where title/description/keywords weigh 2× body (`persistent.py:442-446`), multiplied by `0.5 + 0.5*importance` when decay enabled. Tokens are ASCII ≥2 chars so ticker symbols like "GE" are findable (`persistent.py:119-123`).
- Dedup: 30-second sliding window on `sha256(name|description|content)` to catch retry-loop double writes (`persistent.py:33, 493-506`).
- Concurrency: `fcntl.flock` on `<dir>/.lock`, 5 s timeout, best-effort on timeout (`persistent.py:42-73`). Atomic writes via tmp + `os.replace` in lifecycle.

### 2.2 Lifecycle: quality, decay, GC (`src/memory/lifecycle.py`)
- Importance (`persistent.py:76-92`): Ebbinghaus-style, half-life 14 days:
  `importance = clamp(quality × (exp(-ln2/14 × days_since_access) + min(0.3, 0.1 × access_count)), 0, 1)`.
- Reinforcement deltas (`lifecycle.py:79-87`): `task_success +0.1, task_failure −0.15, user_confirm +0.2, user_reject −0.3, passive_decay −0.05`; system-sourced feedback ×0.7; per-memory per-session cap ±0.5.
- GC (`lifecycle.py:93-97, 183-280`): entries older than 7 days with importance < 0.15 are **archived** (moved to `archive/`), never deleted (`ENABLE_DELETE = False`). `run_gc` defaults to `dry_run=True`; log appended to `gc.log`.
- Compression (`compression.py`): raw → daily (TF-IDF top-5 sentences + first/last, after 7 days unaccessed) → digest (top-15 keywords bullet list, after 30 days). Original archived before rewrite; retention estimated by Jaccard.
- Scheduled consolidation (`consolidation.py`): a sync job gated by `VT_MEMORY_CONSOLIDATION` **and** `VT_MEMORY_GC`; "Honest no-op contract" — with GC off it logs the reason and touches nothing rather than faking work.

### 2.3 Hypothesis registry (`src/hypotheses/registry.py`) — single JSON file
- Path `~/.vibe-trading/hypotheses.json` (env override), a JSON **list**, saved atomically via `.tmp` + `replace` (`registry.py:382-389`), sorted by `created_at`.
- Record (`registry.py:82-112`): `hypothesis_id (hyp_ + sha256(title|created_at)[:12]), title, thesis, status ∈ {exploring, testing, validated, rejected, monitoring}, universe, signal_definition, data_sources[], skills[], run_cards[], invalidation_notes, created_at, updated_at`.
- `link_backtest()` appends `{run_card_path, backtest_run_dir, metrics, validation?, notes, linked_at}` — evidence is *linked by path*, not copied.
- `search()` = token overlap over the JSON dump, ordered by score then `updated_at`; `limit=0` returns nothing (explicit comment: a tool arg an LLM fills in must be honoured).
- Exposed to the LLM as tools `create_hypothesis / update_hypothesis / ...` (`src/tools/hypothesis_tool.py`) and CLI `vibe-trading hypothesis list|show|invalidate --note` (`cli_handlers.py`).

### 2.4 Goal ledger (`src/goal/`) — SQLite, evidence-gated completion
- `store.py`: tables `goals`, `goal_claims`, `goal_criteria`, `goal_evidence`, `goal_audits` in `<runtime_root>/sessions.db` (WAL, `busy_timeout=5000`, `BEGIN IMMEDIATE`). Partial unique index guarantees **one current goal per session** (`store.py:154-164`).
- Statuses (`models.py:10-24`): `active, paused, waiting_user, needs_refresh, insufficient_evidence, compliance_blocked, blocked, budget_limited, usage_limited, complete, cancelled, superseded`.
- Stale-write guard: every mutation needs `expected_goal_id == goal_id` and the goal must be the session's current one, else `StaleGoalError` (`store.py:825-843`).
- Evidence row (`models.py:121-151`): `text, criterion_id, claim_id, evidence_type, tool_call_id, run_id, source_provider/type/uri, symbol_universe, benchmark, timeframe, method, assumptions, artifact_path, artifact_hash, retrieved_at, data_as_of, freshness_status, verification_status, confidence, caveat, contradicts_claim_ids`. `verification_status` becomes `"verified"` only if the artifact file exists **and** its sha256 matches, or the `run_id` directory exists (`store.py:845-863`).
- **Completion gate** (`store.py:899-930`): moving to `complete` requires an audit row per required criterion with result ∈ `{satisfied, satisfied_with_caveat, not_applicable_user_accepted}`, evidence ids that belong to that criterion, and at least one *verified* evidence — otherwise `ValueError("complete goals require verified evidence")`.
- Budget accounting (`store.py:770-823`): tokens/turns/seconds; crossing any budget flips status to `budget_limited`.
- Policy (`policy.py:60-72`): regexes reject objectives that look like order execution ("place/submit/execute … order", "buy … now", 下单/市价单 …) — the research product explicitly refuses to become an execution goal. Our bot is the opposite product, so this is a reference for the *shape* of a code-enforced refusal, not something to copy.
- Prompt injection: `context.py:format_goal_context` emits a `<current-research-goal>` block with criteria coverage counts; `format_goal_continuation_prompt` lists `open_required_items` and rules such as "Prefer the highest-priority open criterion with zero evidence" and "If progress is impossible, set a blocker/insufficient-evidence status instead of stopping silently" (`context.py:150-156`).

---

## 3. The three skills and their code

### 3.1 trade-journal (`src/skills/trade-journal/SKILL.md` → `src/tools/trade_journal_tool.py`, parsers in `trade_journal_parsers.py`)
Analyses a broker export into a **profile** and **4 behaviour diagnostics**. Round-trips via FIFO lot matching (`pair_trades_fifo`, `trade_journal_tool.py:76`); only closed round-trips count toward win rate / PnL ratio; drawdown starts from zero realised PnL. Mixed-currency journals are reported per currency, never summed.

Profile fields (SKILL.md:61-75): `total_trades, total_roundtrips, avg_holding_days, trade_frequency_per_week, win_rate, profit_loss_ratio, total_pnl, max_drawdown, top_symbols, market_distribution, hourly_distribution, roundtrips_sample[{symbol, buy_dt, sell_dt, pnl, pnl_pct, hold_days}]`.

Diagnostics — formulas and thresholds (SKILL.md:191-196, code `trade_journal_tool.py:379-555`):
| Bias | Metric | medium | high |
|---|---|---|---|
| Disposition effect | `avg_loser_hold / avg_winner_hold` | ≥ 1.2 | ≥ 1.5 |
| Overtrading | days bucketed by trade count into top/bottom quartile; `(quiet_avg_pnl − busy_avg_pnl) / |quiet_avg_pnl|`, only if busy < quiet | ≥ 0.3 | ≥ 1.0 |
| Chasing | fraction of buys where `price > price_of_3rd_prior_trade_in_symbol × 1.03` | ≥ 0.40 | ≥ 0.60 |
| Anchoring | fraction of symbols with ≥5 trades whose `σ(price)/mean(price) < 0.05` | ≥ 0.33 | ≥ 0.66 |

`_severity(score, (med, high))` (`trade_journal_tool.py:379-386`). Each diagnostic returns `{severity, <metrics>, evidence: <one-sentence numeric explanation>}` and degrades honestly to `severity: "low"` with a reason such as `"fewer than 4 trading days"` when data is insufficient. Guidance thresholds: `win_rate < 0.4 AND profit_loss_ratio < 1.0` → explicit "losing on both" warning; `avg_holding_days < 1 AND frequency > 15/week` → intraday-heavy flag; `symbols_count <= 3` → concentration note.

### 3.2 thesis-tracker (`src/skills/thesis-tracker/SKILL.md`, prose only — no script)
Buy-side discipline: write the thesis **before/at** buy, re-check on events. Structure: core thesis in 5 one-sentence answers (business, moat, management, price-vs-value, bounded downside — "If you can't fill 5 sentences, the thesis itself is flawed", SKILL.md:48); 3–7 **falsifiable assumptions** each with "how to verify / frequency / status"; **red lines** with severity (Fatal/Severe/Warning) and a pre-committed action; valuation anchors (buy/optimistic/neutral/pessimistic). Tracking statuses 🟢 Holding / 🟡 Weakening / 🔴 Impaired / ⚫ Broken.

Health formula (SKILL.md:131): `health = clamp(10 − 3×broken − 2×impaired − 1×weakening − 5×red_lines_triggered, 1, 10)`; 9–10 add, 7–8 hold, 5–6 hold+alert, 3–4 trim, 1–2 exit. Each check appends a row `| date | health | key change | action |`. Principles (SKILL.md:155-159): "Write sell conditions before buying", "Thesis breaking ≠ stock falling", "Be honest about mistakes". Equity-specific content (PE, earnings, demographics) is irrelevant to crypto.

### 3.3 performance-attribution (`src/skills/performance-attribution/SKILL.md` → `src/quantlib/attribution.py`)
Brinson-Fachler (`brinson_fachler`, `attribution.py:209`) and Carino linking (`carino_link`, `:384`) are implemented and tested; the skill insists "Do not retype these formulas". Per-sector: `Allocation=(w_p−w_b)(r_b,i−R_b)`, `Selection=w_b(r_p,i−r_b,i)`, `Interaction=(w_p−w_b)(r_p,i−r_b,i)`; multi-period via `k_t/k` log-scaling. Also: alpha/beta OLS (≥60 points), Treynor-Mazuy and Henriksson-Merton timing, bull/bear capture, and the risk-adjusted table (Sharpe >1.5 excellent, Sortino, Calmar, IR). Benchmark for crypto: BTC-USDT (SKILL.md:220). Notes worth keeping: "Attribution ≠ prediction", multiple-testing warning, "daily computation with monthly reporting".

Relevance to our bot: Brinson needs sector weights and a benchmark portfolio — overkill for ≤3 spot positions. The useful subset is the risk-adjusted metrics table, BTC buy-and-hold as the benchmark, and *rule-based* attribution (see §4.3).

---

## 4. What the bot's journal + post-trade review should record (concrete design)

### 4.1 Storage choice
Follow the repo's split: **append-only JSONL for events** (like `trace.jsonl`, one object per line, every record carries a unique id so later records can join to it), **SQLite for queryable ledgers** (like `sessions.db`: WAL, `BEGIN IMMEDIATE`, `busy_timeout`), **markdown + frontmatter for human-readable memories** that get injected into prompts. Write everything with tmp + `os.replace`. Keep the paper/testnet/live mode in every record.

### 4.2 Journal record per trade — fields
Pre-trade (written at decision time, before the order, because "write sell conditions before buying"):
- `trade_id`, `cycle_id`, `mode` (paper/testnet/live), `symbol`, `side`, `decided_at`, `data_as_of` (candle close timestamp used), `methodology_hash` (hash of prompt texts + skill texts + model ids + risk constants — the run-manifest idea)
- Per-agent outputs as structured JSON: `agent_name, model, verdict, confidence, key_reasons[], input_tokens, output_tokens, latency_ms` — plus `totals.calls` so a later check can refuse to score a cycle whose usage is incomplete
- Head decision: `action, confidence, thesis` (crypto-adapted 5 sentences: setup, trigger, invalidator, risk/size justification, what-would-make-me-wrong), `assumptions[]` (each falsifiable with a measurable check), `red_lines[]` with pre-committed action
- Risk gate: every hard-limit check with `{limit, observed, result}` (10%/symbol, 3 positions, SL ≤2%, TP ≤4%, daily −3%, 3 consecutive losses, confidence ≥0.65, cooldown, kill switch, drawdown ladder); `blocked: true` on refusal, and the reason. Mirror `risk.authorization_bypasses`: any fill for a blocked decision is an invariant violation surfaced on the dashboard.
- Planned `stop_loss, take_profit, size_usdt, size_pct, atr14_1h, expected_rr_net_of_fees`

Execution:
- `order_id, requested_qty, filled_qty, avg_fill_price, fees (asset + USDT), slippage_bps vs decision price`, `fill_status ∈ {ok, partial, rejected, error}`, `exchange_ts`
- `stop_loss/take_profit re-anchored to fill price`

Exit:
- `exit_reason ∈ {stop_loss, take_profit, time_stop, head_sell, kill_switch, drawdown_ladder, daily_halt}`, `exit_price, exit_fees, realised_pnl_usdt, pnl_pct_net, hold_minutes, mae_pct / mfe_pct` (max adverse / favourable excursion — needed to evaluate whether SL/TP levels were sensible)

Post-trade review (one Haiku call *or* pure code — prefer code for the numbers, Haiku for the one-line lesson):
- `thesis_outcome ∈ {confirmed, weakened, impaired, broken}`, `assumptions_status[]`, `red_line_triggered`, `health_score` (thesis-tracker formula), `lesson` (≤ 200 chars), `tags[]`, `agent_accuracy` (which agents' verdict agreed with the realised direction) — this is the data that lets you later reweight or drop a role.

### 4.3 Periodic review (daily / weekly, code-only)
- Profile: `trades, roundtrips, win_rate, profit_loss_ratio, total_pnl, max_drawdown, avg_hold, frequency/week, per-symbol table, hour-of-day distribution` (trade-journal profile).
- Bias diagnostics with the four formulas in §3.1 (translate "days" to cycles/minutes for 15-minute cycles; chasing compares to the bot's own last 3 fills of the symbol; anchoring uses CV < 5 %). Each returns `severity` + a numeric `evidence` sentence, and `"low"` with an explicit reason when the sample is too small.
- Benchmark: BTC-USDT buy-and-hold over the same window; report Sharpe/Sortino/Calmar and the "NOT VALIDATED" style verdict already used in the previous bot.
- Rule-based attribution instead of Brinson: PnL grouped by `exit_reason`, by `symbol`, by `head_confidence bucket`, by `agent agreement count`, by hour. Every number carries the sample size; buckets with n < 10 are labelled `not_evaluable`, never averaged into a headline.
- Evaluation coverage: like `report.py`, show `evaluable / total` and never compute a pass-rate from missing fields.

### 4.4 What goes to Telegram
Per trade event (`BUY_FILLED`, `SELL_FILLED`, `BLOCKED`, `HALT`, `KILL_SWITCH`, `MODE_CHANGE`, `ERROR`): mode badge, symbol/side/qty/price/fees, size % of equity, SL/TP, head confidence and the one-line thesis, the risk-gate result (which limit bound the size), and `trade_id`. On exit add exit reason, net P&L %, hold time, MAE/MFE, thesis outcome, health score, lesson, running day P&L and consecutive-loss counter. Daily digest: profile numbers, bias table (severity + evidence), bucket attribution, benchmark comparison, "coverage" line. Mask tokens (`mask_secret` from previous bot).

### 4.5 Cross-session memory for the agents (cheap and bounded)
- A `lessons.md`-style index (≤200 lines) regenerated from the journal: per-symbol and per-regime one-liners with `quality_score`, `last_accessed`, importance decay (14-day half-life formula). Inject the frozen index once per cycle into the head/risk prompts; per-symbol recall of the top-3 lessons into the analyst prompts. Reinforce a lesson `+0.1` when a trade citing it wins, `−0.15` when it loses (system source ×0.7), archive at importance < 0.15 after 7 days, never delete.
- A `hypotheses.json` list for strategy-level ideas (`exploring → testing → validated|rejected|monitoring`) with linked backtest run dirs, so the head can be told which setups are `rejected` and must not be traded.

---

## 5. Offline testability (mock backend)
The harness is itself the model: fixtures are a case JSON + a run directory with synthetic artifacts (`cases/identity/fixtures/...`), and `test_runner.py` drives the verifier without any network. For the bot: a `JournalVerifier` with the same four verdict states run in CI against recorded mock cycles, asserting e.g. "every fill joins to a decision with `blocked=false`", "every decision has `totals.calls == len(agent_calls)`", "no SELL without a thesis_outcome", with NOT_EVALUABLE when a field is absent and INVALID_ARTIFACT when the journal and the state file disagree about open positions.

## 6. Licensing
Repo `LICENSE` is MIT ("Copyright (c) 2026 Vibe-Trading Contributors"). Formulas/thresholds (bias detection, health score, importance decay, reinforcement deltas, verdict taxonomy) are adopted as ideas and re-implemented; no skill prose is copied. Cite the repo in `docs/` as the source of the checklists.

## 채택 후보 (adopt)

- Four-valued verdict taxonomy PASS / FAIL / NOT_EVALUABLE / INVALID_ARTIFACT with 'missing instrumentation is never success' and exit codes 0/1/2 — agent/evals/harness/schema.py:11-17, assertions.py:51-62, runner.py:80-85, README.md:14
- Coverage-preserving aggregation: pass_rate only over evaluable verdicts, separate not_evaluable / invalid_artifact counts, evaluation_coverage ratio — agent/evals/harness/report.py:40-84
- Cross-artifact reconciliation as INVALID_ARTIFACT (state.json status vs trace end records; tool_call/tool_result join by unique call_id with ok|error status; successful result for a blocked call = authorization bypass) — agent/evals/harness/assertions.py:138-206, 654-773
- LLM usage ledger that refuses partial data: totals must equal per-call sums and totals.calls must equal number of LLM calls, else NOT_EVALUABLE/INVALID — agent/evals/harness/observations.py:75-102, assertions.py:602-620
- Methodology hash per run (prompt text hash, skill (name, content_hash), tool list hash, package versions; timestamp excluded from hash; no hidden clock) — agent/src/governance/manifest.py:1-70
- Trade-journal behaviour diagnostics formulas and thresholds: disposition ratio (1.2/1.5), overtrading quartile gap (0.3/1.0), chasing >3% vs 3rd-prior own fill (0.40/0.60), anchoring CV<5% (0.33/0.66), each returning severity + numeric evidence and degrading to 'low' with a stated reason on small samples — agent/src/tools/trade_journal_tool.py:379-555, SKILL.md:191-196
- Profile metrics with FIFO round-trip matching, closed trades only for win-rate/PL ratio, per-currency separation — agent/src/skills/trade-journal/SKILL.md:61-87, trade_journal_tool.py:76-360
- Write the thesis and sell conditions before the buy: 5-sentence thesis, falsifiable assumptions with check method, red lines with pre-committed action, health = clamp(10 - 3*broken - 2*impaired - 1*weakening - 5*redline, 1, 10) mapped to add/hold/trim/exit, tracking table row per check — agent/src/skills/thesis-tracker/SKILL.md:39-70, 102-149
- Benchmark = BTC-USDT buy-and-hold for crypto; risk-adjusted table (Sharpe/Sortino/Calmar/IR bands); 'attribution explains the past, does not predict'; daily computation, periodic reporting — agent/src/skills/performance-attribution/SKILL.md:210-232, 310-316
- Memory importance decay: importance = clamp(quality * (exp(-ln2/14 * days_since_access) + min(0.3, 0.1*access_count)), 0, 1) — agent/src/memory/persistent.py:76-92
- Reinforcement deltas for lessons (task_success +0.1, task_failure -0.15, user_confirm +0.2, user_reject -0.3; system source x0.7; per-session cap 0.5) and archive-only GC (importance < 0.15, age >= 7 days, never delete, dry_run default) — agent/src/memory/lifecycle.py:79-97, 183-241
- Frozen memory index injected once into the system prompt (prompt-cache friendly) plus per-turn top-3 relevant recall prepended to the user message in a <recalled-memories> envelope — agent/src/agent/context.py:279-284, 329-334, 411-424
- Markdown-with-frontmatter memory files and a capped MEMORY.md index line format '- [title](path) — description'; 30-second content-hash dedup window against retry double-writes — agent/src/memory/persistent.py:23-39, 493-506, 566-583, 720
- Hypothesis registry as a single atomically-written JSON list with status lifecycle exploring/testing/validated/rejected/monitoring and run artifacts linked by path with linked_at — agent/src/hypotheses/registry.py:20-26, 82-112, 282-327, 382-389
- Evidence rows with provenance (tool_call_id, run_id, artifact_path + sha256 artifact_hash, data_as_of, freshness_status, verification_status) and a completion gate that refuses 'complete' without verified evidence per criterion — agent/src/goal/models.py:121-151, store.py:845-863, 899-930
- Stale-write guard (expected_id must match the current record) and one-current-goal-per-session partial unique index — agent/src/goal/store.py:154-164, 825-843
- SQLite settings for a shared ledger: WAL, busy_timeout=5000, BEGIN IMMEDIATE transactions, RLock-serialized connection — agent/src/goal/store.py:118-124, 248-258
- Honest no-op logging when a feature is gated off instead of faking work — agent/src/memory/consolidation.py:79-97
- Offline verifier pattern: case JSON + synthetic run-dir fixtures evaluated in CI with no network — agent/evals/harness/README.md:3-31, cases/identity/explicit_a_share.json, tests/harness_eval/test_runner.py

## 제외 (skip)

- Brinson-Fachler sector attribution and Carino linking (agent/src/quantlib/attribution.py) — needs sector weights and a benchmark portfolio; meaningless for <=3 spot positions
- Fama-French / Treynor-Mazuy / Henriksson-Merton regressions — need >=60 periods and factor return data we do not have for free
- Equity-specific thesis content (PE/PB, earnings seasons, demographics, management integrity red lines) in thesis-tracker SKILL.md
- Broker-export parsers for 同花顺/东方财富/富途 (agent/src/tools/trade_journal_parsers.py) — our journal is our own JSONL, no CSV import needed
- Goal policy regexes that reject execution-looking objectives (agent/src/goal/policy.py) — our product is execution; only the idea of code-enforced refusal transfers
- SQLite FTS5 search index, semantic BM25 links, hierarchy directories and TF-IDF compression pipeline (search_index.py, semantic_links.py, hierarchy.py, compression.py) — a bot's lesson store is a few hundred lines; token-overlap scan plus the capped index is enough
- Identity/grounding evidence checks (grounding_evidence.json, symbol resolution, numeric_claim_conflict) — built for research answers with cited figures; our agents emit structured JSON verdicts, so the equivalent check is the schema validation already enforced by the SDK
- Multi-turn research-goal continuation prompts (<goal-continuation>) — the bot runs fixed 15-minute cycles with no open-ended turn loop
- Rich/argparse CLI handlers for hypotheses (cli_handlers.py) — the bot's dashboard and Telegram digest cover the same need