# Vibe-Trading live-trading safety (mandate, gates, kill switch, halt sweep, reconcile, audit ledger, redaction, governance/security) and what a Binance spot bot with code-enforced hard limits should adopt


# Vibe-Trading live-trading safety — study report

All paths are under `/home/user/hkuds/vibe-trading/agent/src/` unless noted. Line numbers are from the files as read on 2026-10-28. Nothing was modified.

## 0. One-paragraph mental model

Vibe-Trading treats the LLM as an *untrusted proposer*. Every order the agent wants to place passes through a code gate that (a) reads an immutable, user-committed **mandate** (hard caps) from a protected file the agent has no write path to, (b) checks a **filesystem kill switch** that works without the LLM cooperating, (c) normalises the order into a single `OrderIntent` with one authoritative USD notional, (d) reads broker truth (positions/balance), (e) runs a fixed-order, fail-closed `check_mandate`, and (f) forwards to the broker only on ALLOW — consuming a daily-count slot *only* on a confirmed non-error broker reply. Every decision (allow/deny/breach/halt) is written to a redacted, fsynced, append-only JSONL ledger, plus a hash-chained copy. A separate persistent **runner** executes ticks in the fixed order `heartbeat → halt → mandate/expiry → market-hours → reconcile → invoke LLM → audit`, and when it observes a halt it cancels resting orders and (opt-in) flattens, exactly once per halt episode, with an on-disk latch and an O_EXCL claim file so restarts and concurrent runners cannot double-flatten. Nothing mutating is ever retried.

## 1. File map

| Area | File | Lines | Role |
|---|---|---|---|
| Mandate model | `live/mandate/model.py` | 149 | Frozen dataclasses: `HardCaps`, `UniverseConstraint`, `ConsentMeta`, `Mandate` |
| Mandate loader | `live/mandate/store.py` | 132 | `load_mandate(broker)` – read-only, returns `None` on any problem |
| Mandate writer | `live/mandate/commit.py` | 623 | `commit_mandate(...)` – only writer; needs `consent_ack is True`; narrowing-only adjustments |
| Kill switch | `live/halt.py` | 280 | `trip_halt`, `clear_halt`, `halt_flag_set`, `read_halt`, `register_halt_action`/`on_halt_action` |
| Pure checks | `live/enforcement.py` | 828 | `OrderIntent`, `BreachEvent`, `check_mandate` (fixed check order) |
| MCP gate | `live/order_guard.py` | 961 | `LiveOrderGuardTool.execute` – gate for remote-MCP brokers |
| SDK gate | `live/sdk_order_gate.py` | 1116 | `execute_live_order` / `execute_live_action` – gate for ccxt/SDK brokers (Binance uses this) |
| Daily counter | `live/daily_count.py` | 204 | UTC-day counter + per-broker `flock` order-admission lock |
| Pending marker | `live/pending_action.py` | 389 | Crash-safe "order in flight" marker (Alpaca only today) |
| Audit | `live/audit.py` | 355 | `LiveActionEvent`, `write_live_action` → redacted JSONL + hash chain + trace + SSE |
| Advisory | `live/advisory/__init__.py`, `mock.py` | 273/88 | Observational, fail-OPEN, default-off risk opinions; never block |
| Extractors | `live/extractors/__init__.py` | 45 | Broker kwargs → `OrderIntent` (only Robinhood registered) |
| Classification | `live/classification.py`, `live/registry.py` | 89/240 | READ/WRITE/UNKNOWN ladder; UNKNOWN = WRITE (gate-wrapped) |
| Runner | `live/runtime/runner.py` | 1031 | `LiveRunner.run_once` fixed tick order; preemptive sweep |
| Sweep | `live/runtime/flatten.py` | 545 | `flatten_and_cancel` – cancel first, flatten second, no retry |
| Latch | `live/runtime/sweep_latch.py` | 265 | `FLATTEN_FIRED-<sha16>` + `FLATTEN_CLAIM-<sha16>` per halt episode |
| Reconcile | `live/runtime/reconcile.py` | 608 | Diff broker truth vs `runtime_state.json`; `requires_halt` |
| Liveness | `live/runtime/liveness.py` | 190 | Heartbeat file per runner, 90 s staleness |
| Budget | `live/runtime/decision_budget.py` | 222 | SQLite LLM spend/turn caps; failure → trip halt |
| Scheduler/jobs | `live/runtime/scheduler.py`, `jobstore.py`, `triggers.py` | 332/300/399 | Interval jobs; crash-safe job store refuses empty start on corruption |
| Ledger | `governance/ledger.py` | 724 | Hash-chained, fsynced JSONL; `verify_chain`; rotation never deletes |
| Manifest | `governance/manifest.py` | 569 | Content-addressed "what produced this run" (prompt hash, skill hashes, tool list, package versions) |
| Redaction | `tools/redaction.py` | 687 | `redact_payload` (key-based) + `redact_text` (pattern-based incl. Telegram bot token) |
| Security | `security/scanner.py` (+3 tiny shims) | 263 | Prompt-injection scan + special-token defang for external text |
| Binance | `trading/connectors/binance/{sdk,profiles,classification}.py` | 879/74/27 | ccxt spot; testnet/live host assertion; `quoteOrderQty` notional orders |

## 2. The mandate: immutable, user-authored, expiring

`live/mandate/model.py:47-74` — `HardCaps` is a frozen dataclass:
```
account_funding_usd / max_order_notional_usd / max_total_exposure_usd /
max_leverage / allowed_instruments: tuple[InstrumentType, ...] / max_trades_per_day
```
Doc at `model.py:63-64`: `allowed_instruments: ... Empty == deny all (fail-closed).` `ConsentMeta` carries `created_at`, `consent_token_sha256`, `broker`, `account_ref`, `expires_at` (default 30 days, `commit.py:45`). `Mandate.flatten_on_halt: bool = False` (`model.py:149`) — cancel-only is the default halt behaviour; flattening is an explicit opt-in.

**Loader is fail-closed** (`store.py:58-70`): missing file → `None`; bad JSON → `None`; structural error → `None`. The gate then denies. `schema_version` is compared at the gate (`order_guard.py:146`): `if mandate is None or mandate.schema_version != MANDATE_SCHEMA_VERSION: return self._deny(...)`.

**Writer is unreachable from the agent** (`commit.py:1-12`): "This module is deliberately not a tool and not importable/registerable through the agent tool registry… the only code path that writes one requires the surface-originated `consent_ack` the model never produces". `commit.py:455`: `if consent_ack is not True: raise CommitError(...)`. Adjustments may only *narrow* (`_resolve_profile`, `commit.py:265-345`): numeric must be `<=`, list must be subset, leverage compared on a normalised scale where `"none"` = 1.0. Commit re-checks the resolved profile against the ceiling snapshot the user saw (`_profile_fits_ceilings`, `commit.py:348-397`) after canonicalising aliases (`_CEILING_ALIASES`, `commit.py:96-109`, fixes "audit H9" where a spelling mismatch silently skipped a limit). Proposal is one-shot (`_invalidate_proposal`, `commit.py:252-257`). Writes are atomic 0600 (`_atomic_write_json`, `commit.py:167-186`): tmp file + `os.replace`, parent dir 0700. Order of persistence: consent record first, then mandate (`commit.py:520-524`, "Persist evidence before authority").

Profile templates used when proposing (`tools/propose_mandate_tool.py:30-37`): order_fraction 0.05/0.15/0.30 of funding, daily_trade_cap 2/5/10, cash-only. Every value is clamped to ceilings (`propose_mandate_tool.py:218-239`).

## 3. Kill switch (`live/halt.py`)

- Sentinel files: global `<runtime_root>/live/HALT`, per-broker `<runtime_root>/live/<broker>/HALT`. Global always wins (`halt_flag_set`, `halt.py:155-163`).
- **Existence is the halt, JSON is only attribution** (`halt.py:23-25`): "a sentinel with unreadable / malformed contents is still treated as tripped". `read_halt` returns `{}` for a malformed sentinel (`halt.py:166-191`).
- `halt_flag_set` is "a pure filesystem check… performs NO LLM/agent-state lookup and reads NO in-process flag" (`halt.py:136-141`). An invalid broker key → `True` (fail-closed, `halt.py:159-163`).
- `trip_halt(by, reason, broker=None)` writes `{"tripped_at", "by", "reason"}` atomically via tmp + `replace` (`halt.py:92-108`), idempotent. `clear_halt` is "a privileged surface action (never an agent tool)" (`halt.py:114`).
- Action hook is deliberately separate from the trigger (`halt.py:194-209`): `register_halt_action` / `on_halt_action`; the runner, not `trip_halt`, drives the sweep so a user who merely `touch`es the file still gets the sweep when the runner observes it. `on_halt_action` does **not** swallow the action's exceptions (`halt.py:260-262`).
- Who trips it automatically: proactive mandate expiry (`runner.py:817-821`), inconsistent fill recovery (`sdk_order_gate.py:707-711`), LLM budget/usage accounting failures (`api/live_routes.py:801,812,819`), and the HTTP `POST /live/halt` (`live_routes.py:1077`).
- Registration-time belt-and-braces (`registry.py:196-204, 230-236`): when halted, WRITE/UNKNOWN tools are omitted from the tool list the model sees; the call-time gate still covers the race.

## 4. Gate checks, in order (SDK path — this is the Binance path)

`sdk_order_gate.py:62-181` `execute_live_order`:

1. `load_mandate` — `None` or wrong `schema_version` → DENY `"no valid mandate on file"` (`:106-107`).
2. Expiry — `_is_expired` (`:1086-1094`): unparseable `expires_at` → expired; `now >= expires` → DENY with `requires_reauthorization=True` (`:109-118`).
3. Kill switch — `halt_flag_set(broker)` → DENY `"live trading halted"`, no broker call (`:120-123`).
4. Notional normalisation — `_normalize_notional` (`:898-921`): a `quantity` order is priced (connector `get_quote` → data loaders), enforced as `max(explicit_notional, quantity × price)`; for a BUY LIMIT the price used is `max(quote, limit_price)` (`:946-955`, "#18"); unpriceable → DENY `"quantity order notional could not be priced (fail-closed)"`. Connectors with lot sizing expose `quantity_notional_usd` which is authoritative with **no fallback** (`:924-944`).
5. Read positions + balance via `_safe_read` (`:1002-1014`): exception or `{"status":"error"}` → `None` → the exposure check later fails closed.
6. Under the per-broker `daily_order_lock` (`:141`): read `daily_count`, run `check_mandate`, and on ALLOW call `_allow` **inside the lock** so the read-check-submit-increment window is exclusive. `DailyOrderLockUnavailable` → DENY (`:170-178`).
7. `check_mandate` (`enforcement.py:476-648`) fixed order; first failure wins:
   1. symbol/side sanity → instrument-kind DENY (`:513-520`)
   2. `exclude_symbols` → universe DENY (`:522-529`)
   3. `allowed_instruments` (empty = deny all) → instrument DENY (`:531-538`)
   4. OPTION hard-refused regardless of allowance (`:543-549`)
   5. asset class in `universe.asset_classes` → universe DENY (`:554-561`)
   6. notional resolvable and `> 0`, not NaN (`_resolve_order_notional` `:214-243`); `notional > max_order_notional_usd` → quantitative breach (`:563-577`)
   7. post-trade **gross** exposure from signed symbol-level positions (`_post_trade_gross_exposure` `:319-345`: a sell reduces a long only to zero; excess becomes short exposure); unparseable positions → breach "current positions could not be read (fail-closed)" (`:587-593`); `> max_total_exposure_usd` → breach
   8. leverage = gross / `account_funding_usd`; funding `<= 0` → breach (`:601-615`)
   9. `daily_count + 1 > max_trades_per_day` → breach (`:617-625`)
   10. BUY only: post-exposure `> account_funding_usd` → breach (`:627-635`) ("never block a sell on this")
   11. market-cap / ADV floors, data missing → DENY (`:637-646`, `:651-707`)
8. Breach routing (`sdk_order_gate.py:180-181`): `universe`/`instrument` kinds → DENY outright; `quantitative` → `pause_for_reauth` (surfaces `BreachEvent` so the UI can offer a widen prompt).
9. `_allow` (`:434-539`): `connector.place_order(...)` wrapped in `try/except` → an exception becomes `{"status":"error"}`; `is_error = status != "ok"`; on error audit `order_rejected/error` and **do not** consume a count; on ok `increment_daily_count` then audit `order_placed/accepted`; the redacted record is attached to the result under `"live_action"`.

MCP path (`order_guard.py:134-231`) is the same ceremony plus an **account-binding** step (`_bind_account`, `:620-652`): a multi-account broker must have `consent.account_ref` bound; an order naming another account is refused, never rerouted.

`execute_live_action` (`:207-361`) adds a `risk_reducing: bool` lane: "Risk-reducing actions skip halt and mandate checks but are still audit-logged" (`:222-223`) and never consume a daily count — that is how close-position / cancel paths stay usable under a halt.

## 5. How errors fail closed (concrete list)

- Mandate file missing/corrupt/wrong version → deny (`store.py`, `order_guard.py:146`).
- `expires_at` unparseable → treated as expired (`sdk_order_gate.py:1090-1092`, `runner.py:213-216`).
- Halt sentinel unreadable → still halted; invalid broker key → halted (`halt.py:159-163`).
- No price for a quantity order → deny (`sdk_order_gate.py:125-134`).
- Positions/balance unreadable, any row unpriceable → exposure check denies (`enforcement.py:292-316`, `order_guard.py:701-730`).
- Non-numeric/bool/NaN/inf anywhere → `_as_float` returns `None` → deny (`enforcement.py:463-473`).
- Universe data (market cap / ADV) unavailable → deny, not waive (`enforcement.py:671-705`).
- Daily lock contention → deny (`daily_count.py:75-105`, non-blocking `LOCK_EX|LOCK_NB`).
- Broker call raises or returns non-`ok` → audited as error, **no count consumed, never retried** (`sdk_order_gate.py:461-496`).
- Reconcile raises or reports unsafe → tick aborted (`runner.py:466-509`); an unrecognised report shape is treated as unsafe (`_report_is_unsafe`, `runner.py:141-167`).
- Corrupt `runtime_state.json` → `CorruptRuntimeState` and the runner blocks until repaired (`reconcile.py:537-567`).
- Corrupt `jobs.json` → quarantined and `CorruptJobStoreError`: "refusing empty start" (`jobstore.py:105-129`).
- Pending-action marker unreadable → deny with `reason_code="pending_action_invalid"` (`sdk_order_gate.py:184-193`).
- LLM budget accounting failure → `trip_halt` (`live_routes.py:797-821`).
- Tool classification: absent `readOnlyHint` ≠ read; unknown tools are gate-wrapped (`classification.py:10-26`).
- **Deliberately fail-OPEN** (documented): the daily-count read (`daily_count.py:5-6`, "read failure reads as 0 … never on the order"), audit writes (`order_guard.py:793-795`, "auditing must never block a decision"), heartbeat writes (`runner.py:554-568`), advisory providers (`advisory/__init__.py:13-14`), chain-ledger append after the classic ledger succeeded (`audit.py:330-344`), sweep audit writes (`flatten.py:493-546`, "A dropped audit line is a detectable gap; a permanently disabled kill-switch is not").

## 6. Halt handling: cancel/flatten, exactly once

`runner.py:419-464` `run_once` order: heartbeat → `halt_flag` → `load_mandate` / expiry (trips per-broker halt on expiry, `:808-834`) → market open → reconcile → invoke agent → audit.

`_halted_result` (`:570-597`) calls `_run_preemptive_sweep` (`:624-792`):
- Skips entirely if `self._flatten_fired` or no `submit_fn` wired (then "the cooperative gate alone blocks future orders").
- Captures one coherent `halt_snapshot(broker)` → `(active_episodes, newest_episode)` (`sweep_latch.py:162-185`; episode id = sentinel `tripped_at`, else mtime).
- `sweep_already_fired(broker, episode)` → set flag and return.
- `claim_sweep` is `os.open(... O_CREAT|O_EXCL)` (`sweep_latch.py:74-113`): a second process or a crash-orphaned claim means "the outcome is unknowable — re-sweeping could duplicate a close", so it skips, audits, leaves HALT tripped (`runner.py:674-692`).
- Calls `flatten_and_cancel(broker, submit, read_positions, read_open_orders)` (`flatten.py:62-135`): **cancel all resting orders first**, then flatten only if `mandate.flatten_on_halt` (or explicit `allow_flatten`); long → sell `abs(qty)` market, short → buy; qty coerced with `_coerce_position_qty` rejecting bool/NaN/inf (`flatten.py:426-448`); each broker call audited; errors recorded in `report["errors"]` and **never retried** (`flatten.py:27-30`). Nested error envelopes (`{"status":"ok","data":{"status":"error"}}`) are treated as failures (`_error_envelope_message`, `flatten.py:451-480`).
- `report["side_effects_attempted"]` decides latching (`runner.py:745-772`): no write attempted → not latched, re-check next tick (so a resting order that appears later is still cancelled); any write attempted (even errored) → `mark_sweep_fired` writes `FLATTEN_FIRED-<sha16(episode)>` with O_EXCL (`sweep_latch.py:226-264`). Latch write failure is audited as "restart may replay; verify broker state".
- Claim released in `finally` (`runner.py:791-792`). Per-tick audit noise is deduped per `(episode, intent)` (`_note_sweep_recheck`, `:599-622`).

## 7. Reconcile (crash recovery without resend)

`reconcile.py:275-366`: handed only READ callables ("structurally how we guarantee it cannot resend an order", `:21-24`). Diffs broker truth vs `<broker>/runtime_state.json`. Delta kinds: `matched`, `orphan_order` (confirmed order gone — benign, surfaced), `mid_order_ambiguous` (recorded as submitted but never confirmed; broker shows neither open nor fill), `unknown_fill` (position/qty with no record). `requires_halt` on the last two (`:105-107, :180-187`). State is persisted (atomic tmp+replace, 0600) **only** on a clean pass (`:349-365`); on a cold start broker truth becomes the baseline with zero deltas (`:324-331`). `_is_confirmed` = has broker order id and status not in `("", "pending", "submitted", "unconfirmed")` (`:240-255`).

`pending_action.py` (Alpaca-only, `_broker_key` `:375-379`) is the more rigorous version: write a strict Pydantic `PendingAction` marker with a `client_order_id = "vt-<uuid>"` **before** submit (`sdk_order_gate.py:446-459`), then on the next call recover by exact `get_order_by_client_order_id`; evidence must match symbol/side/type/tif/qty/notional/limit exactly (`_validate_recovery_evidence`, `:756-788`); a fill must reconcile against the signed position delta `before ± filled == after` (`_recover_correlated_fill`, `:620-704`) or the broker is halted (`_halt_fill_recovery`, `:707-723`). Working orders transition to `resolved_needs_revalidation` and block new orders until an operator acts.

**Gap worth knowing:** the Binance `place_order` (`connectors/binance/sdk.py:527-657`) sends no `newClientOrderId`, and the pending-marker path is Alpaca-only, so a Binance mid-order crash currently relies only on the coarser position-diff reconcile.

## 8. What is persisted (and how)

Under `<runtime_root>/live/` (`paths.py:7-13`, `commit.py:20-24`):
- `<broker>/mandate.json` (0600, atomic), `<broker>/proposals/<id>.json`, `<broker>/consent/<id>.json` (immutable consent record).
- `HALT`, `<broker>/HALT` — kill-switch sentinels with `{tripped_at, by, reason}`.
- `<broker>/trade_counter.json` — `{"date": "YYYY-MM-DD", "count": n, "action_ids": [...]}`; written via tmp named with pid+thread id, `fsync` file then directory (`daily_count.py:147-171`); schema validated strictly on read when action ids are used (`_read_action_ids`, `:174-204`).
- `<broker>/.order_submit.lock` — flock file for order admission.
- `<broker>/pending_action.json` — in-flight marker (O_EXCL create, fsync, dir fsync, `pending_action.py:341-363`).
- `<broker>/runtime_state.json` — last reconciled positions/open orders/balance.
- `<broker>/FLATTEN_FIRED-<sha16>`, `<broker>/FLATTEN_CLAIM-<sha16>` — per-episode sweep latch/claim.
- `<broker>/runtime/jobs.json` — scheduler jobs (mkstemp, fsync, replace, dir fsync; corruption quarantined as `jobs.json.corrupt-<ts>`).
- `<broker>/runtime/decisions.sqlite3` — LLM turn/token/cost budget (`decision_budget.py:62`).
- `runtime/heartbeats/<runner>.heartbeat` — epoch-ms, atomic replace, stale after 90 s.
- `audit.jsonl` — classic append-only compliance ledger, fsynced per line (`audit.py:307-318`).
- `audit_chain.jsonl` (+ `audit_chain.0001.jsonl` …) — hash-chained copy; `append_record` verifies the whole chain before appending and refuses to extend a broken one (`ledger.py:36-47, 369-470`); rotation at 64 MiB never deletes (`ledger.py:621-680`).

Audit record shape (`audit.py:217-245`): `audit_id la_<hex>`, `ts`, `session_id`, `kind` ∈ {`order_placed`, `order_cancelled`, `order_rejected`, `mandate_committed`, `breach`, `halt_tripped`, `halt_cleared`}, `intent_normalized` (e.g. `"buy $50 BTC-USDT (crypto)"`), `mandate_snapshot_ref` (= consent token sha256), `consent_record_ref`, `broker_request`, `broker_response`, `outcome` ∈ {`accepted`,`filled`,`rejected`,`error`,`blocked`}, `gate_decision` (`{"allowed", "decision", "checked_limits": [...]}` or breach fields), `server`, `remote_tool`, `error`. Three sinks: ledger (always), per-run trace, event bus `"live.action"` (`audit.py:248-355`).

## 9. Credential redaction (`tools/redaction.py`)

Applied **before** any sink: `LiveActionEvent.to_record()` ends with `return redact_payload(record)` (`audit.py:245`).
- Key-based (`is_sensitive_arg`, `:284-351`): exact keys `api_key, authorization, content, env, headers, passphrase, password, secret, token` plus PII exact keys (`account_number`, `account_id`, `ssn`, `routing_number`, … `:66-86`); substring markers `api_key, authorization, cookie, password, private_key, secret, token` (`:100-108`); keys are separator-folded so `apiKey`/`api-key` also match (`_fold_key`, `:150-165`). `account_ref` is intentionally preserved for the accountability chain (`:62-65`). Benign token-metadata names (`token_usage`, `total_input_tokens` …) are released only when the value has the expected shape (`:131-147, :323-339`).
- Pattern-based on every string leaf (`redact_text`, `:612-649`): PEM private keys, Cookie headers, `Bearer …`, `Authorization: Basic …`, URL userinfo, `key=value`/`"key": "value"` credential pairs, Slack/Discord webhook URLs, **Telegram bot tokens** `(?:bot)?\d+:[A-Za-z0-9_-]{35}` (`:602-604`), and issuer-prefixed bare tokens including `sk-…` Anthropic/OpenAI keys, AWS, GitHub, JWTs (`:575-592`). Idempotent and regex-bounded (no ReDoS).
- `redact_tool_result` (`:652-686`) is the single choke point for tool results: JSON → `redact_payload(sink=RESULT_SINK)`, text → `redact_text`.
- Connector side: `_public_config` (`binance/sdk.py:871-879`) shows `api_key[:4]+"***"` and `"***redacted***"` for the secret; `has_cached_oauth_token` checks presence only, "The token VALUE is never read, returned, or logged" (`registry.py:122-156`).

## 10. Governance and security extras

- `governance/manifest.py`: `build_run_manifest(run_id, timestamp, system_prompt, skills={name: body}, tool_names, package_versions, extra)` → stores only `sha256:` of the prompt and each skill body, sorted tool names + hash, curated package versions; `manifest_hash` deliberately excludes `run_id`/`timestamp` so identical methodology hashes identically (`:10-22`); `diff_manifests` names which skill changed. Never reads the clock (`:24-27`).
- `security/scanner.py`: `scan_prompt_injection` (5 regex rules: instruction override, prompt exfiltration, role claim, secret exfiltration, tool abuse; `:26-77`) and `neutralize_special_tokens` which inserts a zero-width space into `<|im_start|>`, `<｜User｜>`, `[INST]`, `<<SYS>>`, `<s>`, `<start_of_turn>` etc. (`:98-142`). Applied to external text fields via `with_security_warnings(payload, fields=[...])` (`:177-220`) — adds `security_warnings` metadata, never drops content. `network.py`/`workspace_policy.py`/`workspace_access.py` are 10–14-line re-export shims.
- `decision_budget.py`: `DecisionSettings` (interval ≥ 60 s, `max_daily_turns` 26, `max_daily_tokens` 1M, optional `max_daily_cost_usd` requiring both prices); `begin()` refuses if an unfinished decision exists (`:146-147`); `record()` marks `accounting_error` on invalid usage; `finish()` refuses "a zero bill" (`:195-206`); any `DecisionBudgetError` in the runner trips the halt (`live_routes.py:797-821`).
- Advisory layer is the opposite of a gate: `Verdict` ∈ {approve, approve_with_concerns, reject, review_unavailable}, worst-case aggregation, exceptions → `REVIEW_UNAVAILABLE`, default-off, result only lands in `gate_decision["advisory"]` (`advisory/__init__.py:1-22`, `order_guard.py:436-512`).

## 11. Binance connector facts relevant to our bot

- Profiles (`binance/profiles.py`): `binance-paper-trade` (testnet, `orders.place`), `binance-live-trade` (`orders.place.requires_mandate`) — the capability string tells the service layer that the gate is mandatory.
- Host assertion is the structural paper/live guard (`sdk.py:839-859`): a paper profile must resolve to `testnet.binance.vision`, a live one to `api.binance.com`; `ex.set_sandbox_mode(cfg.is_testnet)` (`:830`).
- Market orders by quote notional via `params["quoteOrderQty"]` (`:627-633`); exactly one of quantity/notional (`:585-588`); any ccxt exception → `{"status":"error"}` (`:638-642`).
- ccxt options `adjustForTimeDifference: True`, `recvWindow: 10_000` (`:811-814`); `-1021` clock-skew read retry adds 1 s (`_read_with_clock_retry`, `:768-787`).
- Curated class map (`classification.py`): `create_order`, `cancel_order` WRITE; `fetch_*`, `load_markets` READ.

## 12. Recommendations for our Binance spot bot

### Adopt (as-is pattern, rewritten in our own code)
1. **Fixed-order, fail-closed gate function** returning `None | Breach` (`enforcement.check_mandate`). Our order: kill switch → mode/host assertion → halt-state (daily −3 %, 3 losses, drawdown ladder) → cooldown → confidence ≥ 0.65 → symbol whitelist → max 3 positions → 10 %/symbol (notional computed as `max(requested, qty×price)`) → SL ≤ 2 % / TP ≤ 4 % on the *actual fill price* → daily trade cap. First failure wins; every denial has a machine `limit` name and a human `detail`.
2. **Filesystem kill switch where existence = halt** (`halt.py`). Keep our STOP/STOP_ALL files but make `is_halted()` a pure `Path.exists()` and treat an unreadable file as tripped. Store `{tripped_at, by, reason}` for Telegram/dashboard attribution. Separate "trip" from "action"; the cycle loop runs the sweep when it observes the file.
3. **Cancel-before-flatten sweep, once per halt episode, no retry** (`flatten.py` + `sweep_latch.py`). For spot: cancel open orders (OCO legs) first, then market-sell positions only if `flatten_on_halt` is true in config; latch per episode (`tripped_at`) with `O_CREAT|O_EXCL`; keep a `FLATTEN_CLAIM` so a second bot instance never double-sells; do **not** latch when nothing was attempted.
4. **Count a trade only on a confirmed non-error exchange reply; never retry a mutating call** (`sdk_order_gate._allow`). Wrap `create_order` in try/except → error envelope; audit both branches.
5. **Admission lock around read-check-submit-record** (`daily_count.daily_order_lock`, non-blocking flock). Extend our `bot.lock` with a per-order lock so the dashboard/CLI cannot submit concurrently with the cycle.
6. **Reconcile before every cycle; abort on ambiguity** (`reconcile.py`). Diff exchange balances/open orders vs `state.json`; `unknown_fill` / `mid_order_ambiguous` ⇒ trip STOP and send Telegram, never resend. Persist the baseline only on a clean pass. Cold start = adopt exchange truth.
7. **Pending-order marker with `newClientOrderId`** (`pending_action.py`): write the marker *before* `create_order`, pass `newClientOrderId="tb-<uuid>"` to Binance, recover via `fetch_order(clientOrderId=…)` on restart. This closes the gap Vibe-Trading itself has for Binance.
8. **Audit ledger: redact → append → fsync; hash-chained copy; audit failures never block trading** (`audit.py`, `governance/ledger.py`). Our `logs/decisions.jsonl` should carry `audit_id`, `kind`, `outcome`, `gate_decision.checked_limits`, redacted request/response. The hash-chain can be a small (<150 line) helper; verify on startup and report breaks to Telegram.
9. **Redaction before every sink including Telegram** (`redaction.py`). Keep key-based scrubbing (`api_key|secret|token|password|authorization|cookie` markers, folded keys) and add the pattern set for Telegram bot tokens and `sk-` keys to our `mask_secret`. Telegram error messages from `httpx` echo the bot URL — this is exactly the leak `_TEXT_TELEGRAM_BOT_TOKEN_PATTERN` exists for.
10. **Testnet/live host assertion inside the exchange wrapper** (`binance/sdk.py:_assert_host`) in addition to `MODE=live + CONFIRM_LIVE`.
11. **LLM spend budget that halts trading when accounting fails** (`decision_budget.py`): per-UTC-day turn/token/cost caps; an unfinished decision on restart blocks until inspected; budget exhaustion ⇒ STOP, not silent continue.
12. **Treat `NaN`/`inf`/bool/non-numeric as absent → deny** everywhere (`_as_float`, `_coerce_position_qty`, `_resolve_order_notional`). Apply to agent JSON outputs (confidence, sizes) and exchange payloads.
13. **Prompt-injection scan + special-token defang on RSS/news text** before it reaches the news/scanner agents (`security/scanner.py`); attach `security_warnings` so the head agent can discount flagged items.
14. **Run manifest** (`governance/manifest.py`): hash system prompts, skill texts, roster, model ids, package versions per cycle; store `manifest_hash` in the cycle report so backtests/journal entries can be attributed to a methodology version.
15. **Heartbeat + stale detection** for the dashboard (`liveness.py`): 15-min cycles ⇒ staleness threshold ≈ 2 cycles.

### Adapt
- Replace the mandate's *user-consent commit* with our static config: keep immutability (frozen dataclass loaded once, no runtime setter), keep `schema_version` and an `expires_at` for live mode (re-confirm live every N days), keep "narrowing only" if we ever allow runtime overrides (dashboard may tighten limits, never loosen).
- `BreachEvent.kind`: we have no re-auth UI, so collapse `pause_for_reauth` into DENY but keep the `kind` field (`structural` vs `quantitative`) for Telegram wording.
- `risk_reducing` lane: our code-driven SL/TP/time-stop sells and the halt sweep bypass confidence/cooldown/position-count checks but still pass kill-switch? In Vibe-Trading risk-reducing actions skip the halt check entirely; for spot we should let SL/TP exits run even when STOP is set (a halt must not trap us in a losing position) but block new buys.
- Reconcile for spot: "position" = free+locked base balance per whitelisted asset; compare to `state.json` quantities with a dust tolerance (reuse our `SELL_DUST_TOLERANCE`).

### Skip
- MCP tool classification ladder, `LiveOrderGuardTool`, per-broker extractors, account binding — we own the exchange wrapper, so one `OrderIntent` builder suffices.
- Consent UX / proposal profiles (`commit.py`, `propose_mandate_tool.py`) — no multi-user consent flow.
- Market-hours triggers / US holiday tables (`triggers.py`) — crypto is 24/7.
- USD-M futures shadow account code (`usdm.py`) — spot only.
- Advisory layer as a *non-blocking* opinion — our risk_manager agent is part of the decision roster; but keep its design rule that a failing advisor returns `review_unavailable` rather than crashing the cycle.
- `pandas`-based loader quote fallback — we have ccxt tickers.
- 64 MiB ledger rotation — unnecessary at our volume; a single chained file is fine.


## 채택 후보 (adopt)

- Fixed-order fail-closed gate returning None|Breach with a named `limit` and `kind` per failure — live/enforcement.py:476-648 (check_mandate) and live/sdk_order_gate.py:62-181 (execute_live_order ordering: mandate → expiry → halt → notional normalisation → reads → lock → check → allow)
- Filesystem kill switch where file existence is the halt and JSON is attribution only; unreadable sentinel = tripped; invalid key = tripped — live/halt.py:135-163 (halt_flag_set), 72-108 (trip_halt atomic write), 194-280 (separate register/on_halt_action hook)
- Order notional enforced as max(explicit notional, quantity x price) with buy-limit priced at max(quote, limit_price); unpriceable = deny — live/sdk_order_gate.py:898-958 and live/order_guard.py:235-292
- Daily trade count consumed only on a confirmed non-error exchange reply; mutating calls never retried; connector exception converted to error envelope — live/sdk_order_gate.py:434-539 (_allow) and live/daily_count.py:125-171 (atomic counter with fsync of file and dir)
- Non-blocking per-broker order-admission flock held across read-check-submit-increment — live/daily_count.py:74-105 (daily_order_lock) used at live/sdk_order_gate.py:140-169
- Cancel-before-flatten halt sweep, flatten opt-in via config flag, each broker call audited, errors recorded never retried, nested error envelopes treated as failure, NaN/bool qty rejected — live/runtime/flatten.py:62-135, 232-424, 426-480
- Once-per-halt-episode latch with O_CREAT|O_EXCL per-episode files plus an exclusive FLATTEN_CLAIM so restarts and concurrent runners cannot double-flatten; latch only when a side effect was attempted — live/runtime/sweep_latch.py:74-113, 162-185, 226-264 and live/runtime/runner.py:624-792
- Reconcile exchange truth vs persisted state before every cycle using READ callables only; unknown_fill / mid_order_ambiguous => halt and surface, never resend; persist baseline only on a clean pass; corrupt state file blocks start — live/runtime/reconcile.py:78-107, 275-366, 537-608
- Crash-safe pending-order marker written before submit with a client_order_id, recovered by exact identity and signed position delta; inconsistent fill => trip halt — live/pending_action.py:233-373 and live/sdk_order_gate.py:542-723 (add newClientOrderId to Binance create_order; Vibe-Trading itself lacks this for Binance)
- Redact-then-write audit record with audit_id/kind/outcome/gate_decision.checked_limits; classic ledger fsynced per line; hash-chained copy; audit failure never blocks a decision — live/audit.py:172-355 and governance/ledger.py:134-150 (compute_record_hash), 369-470 (append_record verifies whole chain, refuses to extend a broken one), 310-325 (verify_chain)
- Credential redaction: key-based (folded keys, marker substrings, exact PII keys) plus pattern-based scrub of string leaves including Telegram bot tokens `(?:bot)?\d+:[A-Za-z0-9_-]{35}`, sk- keys, JWTs, Bearer/Basic headers, URL userinfo, webhook URLs — tools/redaction.py:66-108, 284-399, 441-649; connector secret masking tools/../connectors/binance/sdk.py:871-879
- Runner tick in fixed order heartbeat -> halt -> mandate/expiry (proactive expiry trips halt) -> reconcile -> invoke LLM -> audit; unrecognised reconcile report shape treated as unsafe — live/runtime/runner.py:419-464, 141-167, 808-834
- Testnet/live host assertion inside the exchange wrapper and ccxt sandbox mode keyed to profile; adjustForTimeDifference + recvWindow + -1021 clock-skew retry — trading/connectors/binance/sdk.py:839-859, 799-836, 768-787
- LLM decision budget in SQLite (daily turns/tokens/cost per UTC day); unfinished decision blocks restart; accounting failure or exhaustion trips the kill switch — live/runtime/decision_budget.py:25-50, 138-206 and api/live_routes.py:784-821
- Fail-closed numeric coercion helpers rejecting bool/NaN/inf/non-numeric — live/enforcement.py:214-243 (_resolve_order_notional), 463-473 (_as_float); live/runtime/flatten.py:426-448 (_coerce_position_qty)
- Prompt-injection scan and chat-template special-token defang on external text (RSS/news) with security_warnings metadata, never dropping content — security/scanner.py:26-77, 98-142, 177-220
- Run manifest hashing system prompt, each skill body, tool list, model/provider extras and package versions into one methodology hash that excludes run_id/timestamp; diff_manifests names changed skills — governance/manifest.py:128-186, 359-433, 481-534
- Crash-safe job/state store: mkstemp + fsync + os.replace + dir fsync; corrupt store quarantined and start refused rather than empty — live/runtime/jobstore.py:105-163; same tmp+replace pattern in live/mandate/commit.py:167-186 and live/runtime/reconcile.py:570-608
- Heartbeat file per runner with staleness threshold for dashboard liveness; heartbeat failure never aborts trading — live/runtime/liveness.py:79-150 and live/runtime/runner.py:554-568
- Immutable limits config: frozen dataclass read once at boot, no runtime setter reachable from agents, schema_version checked, expiry for live mode, any runtime override may only narrow — live/mandate/model.py:47-149, live/mandate/store.py:42-70, live/mandate/commit.py:265-345 (narrowing-only adjustments)
- Risk-reducing action lane: exits/cancels skip entry gates but are still audited and never consume a trade-count slot — live/sdk_order_gate.py:207-252 (execute_live_action risk_reducing=True)

## 제외 (skip)

- MCP tool classification ladder (annotations -> curated map -> default-deny) and LiveOrderGuardTool wrapper — we own the exchange wrapper, one OrderIntent builder is enough (live/classification.py, live/order_guard.py, live/registry.py)
- Per-broker order-intent extractor registry — only Robinhood is registered; our bot builds OrderIntent directly (live/extractors/__init__.py)
- Consent UX: proposal profiles, commit_mandate with consent_ack, ceiling alias normalisation, re-authorisation pause (pause_for_reauth) — no multi-user consent flow in our bot; collapse to DENY but keep the breach kind field (live/mandate/commit.py, tools/propose_mandate_tool.py)
- Multi-account binding (_bind_account) — Binance spot keys map to one account (live/order_guard.py:620-652)
- Market-hours triggers, US holiday/early-close tables, MARKET trigger jobs — crypto is 24/7 (live/runtime/triggers.py)
- USD-M futures shadow-account observation code and fapi reads — spot only (trading/connectors/binance/usdm.py)
- Advisory provider orchestration as a separate non-blocking layer — our risk_manager agent is part of the decision roster; keep only the rule that a failing advisor yields review_unavailable instead of crashing (live/advisory/)
- pandas data-loader quote/ADV/market-cap fallback chain and universe floors — we use ccxt tickers and a fixed USDT-pair whitelist (live/enforcement.py:246-290, 651-820)
- Ledger rotation at 64 MiB with archive segments — unnecessary at our volume (governance/ledger.py:593-725)
- Scheduler kernel façade and durable job store for interval jobs — our 15-minute loop is a single process; a simple sleep loop plus heartbeat suffices (live/runtime/scheduler.py, jobstore.py)
- security/network.py, workspace_policy.py, workspace_access.py — 10-14 line re-export shims for channel adapters, nothing to adopt
- Legacy single-record FLATTEN_FIRED compatibility fallback — no legacy format to support (live/runtime/sweep_latch.py:200-224)