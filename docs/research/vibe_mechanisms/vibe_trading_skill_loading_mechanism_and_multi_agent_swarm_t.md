# Vibe-Trading skill loading mechanism and multi-agent swarm team definitions (reference for the Binance spot bot's agent roster and skill system)

# Vibe-Trading: skill loader + swarm presets — findings for the Binance spot bot

All paths are under `/home/user/hkuds/vibe-trading/agent/src/`. Nothing was modified. License is MIT; recommendations below prefer re-implementing in our own words and keeping only formulas/thresholds.

---

## 1. SKILL.md frontmatter format

A skill is a **directory** containing `SKILL.md` (plus optional support files). The loader only reads the frontmatter + body; supporting files are loaded on demand via `Skill.load_support_file(filename)`.

Parser: `agent/frontmatter.py` (shared with memory files). It is a **tiny hand-written YAML-ish parser**, not PyYAML:

```python
# agent/frontmatter.py:10-13
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n(.*))?$",
    re.DOTALL,
)
```
It supports only `key: value` lines; values become a list if wrapped in `[a, b]`, a bool if `true/false`, else a string (`frontmatter.py:39-45`). Multi-line YAML, nested maps and quoted-colon edge cases are not handled (a quoted description containing `:` still works because `split(":", 1)` is on the key side).

Fields actually consumed (`agent/skills.py:82-94`):
```python
meta, body = _parse_frontmatter(text)
name = meta.get("name", dir_path.name)
...
return Skill(name=name, description=meta.get("description", ""),
             category=meta.get("category", "other"), body=body, dir_path=dir_path, metadata=meta)
```
So the frontmatter contract is three keys: **`name`** (defaults to folder name), **`description`** (one line; this is the ONLY thing the model sees before loading), **`category`** (used for grouped display; known order `data-source, strategy, analysis, asset-class, crypto, flow, tool, other`, `skills.py:138-141`). Field order is free (e.g. `thesis-tracker` puts `category` before `description`).

Concrete examples (verbatim):
```
---
name: technical-basic
description: Core technical indicator collection (trend EMA/ADX + mean-reversion BB/RSI + volume-price OBV/volume ratio), generates a composite signal via three-dimensional voting. Pure pandas implementation for any OHLCV data.
category: strategy
---
```
```
---
name: perp-funding-basis
description: Perpetual futures funding rate analysis and cash-carry basis trading — funding rate regimes, annualized basis signals, carry trade construction, and funding rate arbitrage between exchanges.
category: crypto
---
```
Body conventions seen across the corpus: `# Title`, `## Purpose/Overview`, `## Signal Logic`, `## Parameters` (table of defaults), `## Output Format` (a markdown report template), `## Signal Convention` (`1 = long, -1 = short, 0 = stand aside`). Body size ranges from 1.4k chars (`smc`) to 103k (`tushare`); 35/88 exceed one tool result.

## 2. How skills are discovered, summarized and loaded

### 2a. Registry: `agent/skills.py` — `SkillsLoader`
- Scans two dirs, **user first** then bundled, first-seen name wins (so user skills override bundled ones):
  ```python
  # skills.py:97, 127-135
  USER_SKILLS_DIR = Path.home() / ".vibe-trading" / "skills" / "user"
  for directory in (self._user_skills_dir, self.skills_dir):
      for path in sorted(directory.iterdir()):
          if path.is_dir() and (path / "SKILL.md").exists():
  ```
- **Progressive disclosure, level 1**: `get_descriptions()` renders one line per skill, grouped by category, into the system prompt (`skills.py:143-164`: `f"  - {skill.name}: {skill.description}"`). The main agent injects it through `agent/context.py:78-80`:
  ```
  ## Skills (use load_skill to read full docs)

  {skill_descriptions}
  ```
- **Level 2**: `get_content(name)` returns `'<skill name="{name}">\n{body}\n</skill>'` or `"Error: Unknown skill '...'. Available: ..."` (`skills.py:166-190`).
- **Level 3**: `split_sections()` / `find_sections()` (`skills.py:230-396`) map markdown ATX headings (skipping fenced code) so a long document can be served as an outline + one named section (`"Parent > Child"` path syntax for duplicate titles).

### 2b. The `load_skill` tool: `tools/load_skill_tool.py`
Tool schema (`load_skill_tool.py:153-186`): `name` (required), optional `section`, optional `offset`. Returns a JSON envelope with `mode` = `document` | `outline` | `section`, plus `offset/next_offset/complete` so a partial read is never mistaken for the whole thing. A document that does not fit `TOOL_RESULT_LIMIT` on the first read comes back as an **outline** (heading tree with char counts and first-line summaries) plus opening text (`load_skill_tool.py:299-323`).

**Per-worker allowlist** is enforced inside the tool, fail-closed:
```python
# load_skill_tool.py:224-230
name = kwargs["name"]
if self._allowed_skills is not None and name not in self._allowed_skills:
    allowed = ", ".join(sorted(self._allowed_skills)) or "(none)"
    return _error(f"Error: skill {name!r} is outside the skill allowlist for this context. Allowed skills: {allowed}")
```
`allowed_skills=None` means unrestricted (main agent); a `frozenset` is passed for swarm workers.

### 2c. Where the allowlist is wired for swarm workers
`swarm/worker.py:659-677`:
```python
registry = build_swarm_registry(agent_spec.tools, agent_config=agent_config,
    include_shell_tools=include_shell_tools, skill_allowlist=agent_spec.skills or None)
skills_loader = SkillsLoader()
skill_desc = _filter_skill_descriptions(skills_loader, agent_spec.skills)
system_prompt = build_worker_prompt(agent_spec, upstream_summaries, skill_desc, grounding_block=grounding_block)
```
`tools/__init__.py:422-427` rebuilds the tool with the boundary baked in:
```python
if skill_allowlist is not None and filtered.get("load_skill") is not None:
    filtered.register(LoadSkillTool(allowed_skills=frozenset(skill_allowlist)))
```
Note the semantics: **empty `skills: []` means unrestricted** (`agent_spec.skills or None`), and `_filter_skill_descriptions` (`worker.py:155-171`) likewise returns ALL descriptions for an empty list. So the only way to give a worker *no* skills is to omit `load_skill` from its `tools:` list (which is what `risk_committee.aggregator` and `technical_analysis_panel.signal_aggregator` do: `tools: [bash, read_file, write_file]`, `skills: []`).

### 2d. The worker system prompt layout (`worker.py:285-446`, `build_worker_prompt`)
Ordered so that the agent-invariant prefix is prompt-cache friendly:
1. `## Role\n\n{agent_spec.role}`
2. `## Available Skills (use load_skill to access full documentation)\n\n{filtered one-liners}`
3. `## Market Data Tool Policy` (only if `get_market_data` is whitelisted)
4. `## Data Citation Discipline (HARD RULE)` — every number must trace to (a) a tool result in this run, (b) the Ground Truth block, or (c) upstream context; "You may NOT cite numbers from memory or training data" (`worker.py:339-360`). Applies to aggregator roles too.
5. The YAML `system_prompt` with `{upstream_context}` replaced by `## Upstream Context (from previous agents)` + one `### {key}` section per `input_from` entry (`worker.py:365-374`).
6. Optional `## Ground Truth — Recent Market Data` block (`swarm/grounding.py:231-283`): 30 days of OHLCV pre-fetched once per run for any symbols found in `user_vars`, so workers see real recent prices.
7. `## Execution Rules`: iteration budget, "Phase 1 — Plan (0 tool calls)", "Phase 2 — Execute" (write ONE script via write_file then run), "Phase 3 — Summarize (MUST use write_file)" → `report.md` + 2-3 sentence summary. A tool-less role gets a variant telling it to answer inline.
8. `## Current Date & Time`.

## 3. Swarm team definition format (`swarm/presets/*.yaml`, parsed by `swarm/presets.py`)

Top-level keys: `name`, `title`, `description`, `agents:[]`, `tasks:[]`, `variables:[]`.

`SwarmAgentSpec` (`swarm/models.py:175-200`): `id`, `role`, `system_prompt`, `tools: list[str]` (whitelist), `skills: list[str]` (allowlist), `max_iterations=25`, `timeout_seconds=300`, `model_name: str|None` (per-agent model override — present in the model but no bundled preset uses it), `max_retries=2`. Bundled presets all use `max_iterations: 50`, `timeout_seconds: 1800`, `max_retries: 1`.

`SwarmTask` (`models.py:203-237`): `id`, `agent_id`, `prompt_template` (Python `{var}` format; `{upstream_context}` is reserved), `depends_on: [task ids]`, `input_from: {context_key: task_id}`, runtime fields `status`, `summary`, `artifacts`, `error`, `worker_iterations`. Missing template variables do not fail: `_FallbackDict.__missing__` substitutes `"(determine the appropriate {key} based on the objective)"` (`worker.py:689-697`).

Presets are discovered user-dir-first (`~/.vibe-trading/swarm/presets/`) then bundled, name validated against path traversal (`presets.py:30-78`). `inspect_preset()` (`presets.py:171-283`) is a dry run: duplicate ids, unknown agents, DAG cycles, `input_from` not upstream, undeclared/unused template variables.

## 4. How workers pass results to each other

1. The runtime computes **topological layers** of the task DAG (`task_store.topological_layers`) and runs each layer in a `ThreadPoolExecutor` (`runtime.py:1086-1110`).
2. Each finished task's `WorkerResult.summary` is stored in `task_summaries[tid]` (`runtime.py:628`).
3. For a downstream task, `upstream` is built **only from `input_from`** (not from `depends_on`):
   ```python
   # runtime.py:1086-1090
   upstream: dict[str, str] = {}
   for context_key, source_task_id in task.input_from.items():
       if source_task_id in task_summaries:
           upstream[context_key] = task_summaries[source_task_id]
   ```
   `depends_on` only gates scheduling; `input_from` decides what text is injected. This lets a late task pull a summary from two layers back (e.g. `equity_research_team.task-stock` gets both `sector_context` and `macro_context`).
4. The "summary" that flows downstream is, in order of preference: the full `report.md` the worker wrote (`_resolve_summary`, `worker.py:1452-1462`), else the model's final text, else the **longest assistant message over 100 chars** (`_best_summary`, `worker.py:1231-1240`). So the hand-off payload is free-form markdown, not JSON.
5. `_classify_deliverable` (`worker.py:1413-1449`) marks a result `incomplete` (distinct from `failed`) if it is empty, contains fabrication markers, is a raw tool envelope, is a plan-only stub, or (for data agents) made zero tool calls and wrote no report. Incomplete must never be folded into completed.
6. Per-layer hard deadline = `timeout_seconds * (max_retries+1) + retry budget + 60s` (`runtime.py:1106-1116`).

## 5. How the final decision is formed

There is **no code-level voting or aggregation**. The final decision is simply the summary of a task in the last DAG layer:
```python
# runtime.py:752-758
if task_summaries:
    last_layer = layers[-1] if layers else []
    for tid in last_layer:
        if tid in task_summaries:
            run.final_report = task_summaries[tid]
            break
```
Everything about *how* to decide lives in the last agent's `system_prompt`. Three patterns appear:

- **Investment Committee (debate → risk → PM)**, `presets/investment_committee.yaml`. PM instruction: "Relative strength of bull vs bear arguments (weighting, not head-count voting)… Clear action: long / short / wait / hedge… Confidence 0–100% and what forces a re-evaluation… Decisions must be executable; reject vague 'it depends.'" CRO instruction: "Clear stance: support / conditional support / oppose… Risk scorecard on bull points — Reliability 1–5 per bull point plus pushback… Position sizing — Explicit suggested range." Bull/Bear each must give "Main risk to the bull case" / "What would disprove the bear case" (pre-mortem).
- **Signal Aggregator (judge)**, `presets/technical_analysis_panel.yaml:287-343`: each school outputs a `-5..+5` score + direction; judge does "Simple vote count… Average the five scores… Regime weights (Trending: up-weight MA/wave; Range: up-weight harmonic/Ichimoku; Liquidity regime: up-weight SMC)… 5/5 same way = max resonance, 4/5 strong, 3/5 medium, ≤2/5 chaotic—stand aside"; must write a "Dissent note" and "Signal shelf life — expected horizon; prices that invalidate".
- **Desk Risk Manager with explicit weights and gates**, `presets/crypto_trading_desk.yaml:124-185`: "Priority weighting: on-chain flow (40%) > funding/basis (35%) > liquidation levels (25%)… Direction: long / short / neutral / wait; Conviction level: high / medium / low; Entry zone; TP1/TP2/TP3; Stop-loss: hard stop with maximum acceptable loss %… Risk per trade: max 2% of portfolio… Risk Gates (any breach → reduce/close position)… Monitoring Checklist — 5 key metrics with specific alert thresholds… If an upstream section reported its data as unavailable, the gate that depends on it is also unavailable: say so instead of inventing a threshold."
- Crypto Research Lab synthesizer (`crypto_research_lab.yaml:200-211`): "All Three Aligned: Highest confidence… Two Aligned + One Diverging: Medium… All Three Diverging: Low confidence; reduce position or wait" with a stated priority order for conflicts.

## 6. Full list of bundled swarm presets (31) — workers and verbatim skill allowlists

Format: `agent_id (role): [skills]`. DAG shown as layers; the last listed agent produces `final_report`.

1. **commodity_research_team** — supply_analyst (Supply Analyst): [commodity-analysis, web-reader, geopolitical-risk]; demand_analyst (Demand Analyst): [commodity-analysis, seasonal, global-macro] ‖ → cycle_strategist (Cycle Strategist): [commodity-analysis, seasonal, strategy-generate]. vars: commodity, horizon.
2. **convertible_bond_team** — bond_analyst: [convertible-bond, fundamental-filter, credit-analysis]; equity_analyst: [convertible-bond, technical-basic, valuation-model]; option_analyst: [options-strategy, volatility, convertible-bond, options-payoff] ‖ → cb_strategist: [convertible-bond, strategy-generate].
3. **credit_research_team** — credit_analyst: [credit-analysis, financial-statement]; rate_analyst: [credit-analysis, macro-analysis]; sector_credit_analyst: [credit-analysis, sector-rotation, regulatory-knowledge] ‖ → fixed_income_strategist: [credit-analysis, strategy-generate, risk-analysis].
4. **crypto_research_lab** — onchain_analyst (On-Chain Data Analyst): [onchain-analysis, okx-market, stablecoin-flow]; defi_analyst (DeFi Protocol Analyst): [crypto-derivatives, defi-yield, token-unlock-treasury, web-reader]; crypto_sentiment_analyst (Crypto Sentiment Analyst): [sentiment-analysis, okx-market, perp-funding-basis, liquidation-heatmap] ‖ → alpha_synthesizer (Alpha Synthesizer): [asset-allocation, risk-analysis]. vars: target, timeframe.
5. **crypto_trading_desk** — funding_basis_analyst (Funding Rate & Basis Analyst): [perp-funding-basis, okx-market, crypto-derivatives]; liquidation_analyst (Liquidation & Microstructure Analyst): [liquidation-heatmap, market-microstructure, execution-model]; flow_analyst (On-Chain & Stablecoin Flow Analyst): [stablecoin-flow, onchain-analysis, token-unlock-treasury, defi-yield] ‖ → desk_risk_manager (Trading Desk Risk Manager): [risk-analysis, asset-allocation, volatility, hedging-strategy]. vars: target, timeframe.
6. **derivatives_strategy_desk** (sequential) — vol_analyst: [volatility, options-advanced] → strategy_designer: [options-strategy, options-advanced, hedging-strategy, options-payoff] → greeks_manager: [options-advanced, risk-analysis, volatility, options-payoff].
7. **earnings_research_desk** — fundamental_analyst: [edgar-sec-filings, financial-statement, yfinance, fundamental-filter, valuation-model]; revision_tracker: [earnings-revision, earnings-forecast]; event_options_analyst: [options-advanced, options-strategy, event-driven, volatility] ‖ → earnings_strategist: [strategy-generate, risk-analysis, report-generate].
8. **equity_research_team** (sequential) — macro_analyst: [tushare, okx-market, yfinance, web-reader, global-macro] → sector_analyst: [tushare, yfinance, fundamental-filter, multi-factor, us-etf-flow, sector-rotation] → stock_picker: [tushare, yfinance, strategy-generate, technical-basic, multi-factor, earnings-revision] → aggregator (Research Report Editor): [report-generate].
9. **etf_allocation_desk** — etf_screener: [etf-analysis, fund-analysis, tushare]; macro_allocator: [macro-analysis, asset-allocation, global-macro]; risk_budgeter: [risk-analysis, volatility, etf-analysis] ‖ → portfolio_optimizer: [etf-analysis, strategy-generate, asset-allocation].
10. **event_driven_task_force** (sequential) — event_scanner: [event-driven, corporate-events, web-reader, geopolitical-risk] → impact_analyst: [corporate-events, sentiment-analysis, valuation-model] → strategy_builder: [event-driven, strategy-generate].
11. **factor_research_committee** — factor_miner: [factor-research, multi-factor]; factor_validator: [quant-statistics, factor-research] ‖ → factor_combiner: [multi-factor, strategy-generate] → backtest_reviewer: [backtest-diagnose, quant-statistics].
12. **fund_selection_panel** (sequential) — fund_screener: [fund-analysis, fundamental-filter] → attribution_analyst: [performance-attribution, fund-analysis, multi-factor] → fof_optimizer: [asset-allocation, risk-analysis, strategy-generate, etf-analysis].
13. **fundamental_research_team** — financial_analyst: [financial-statement, fundamental-filter]; valuation_analyst: [valuation-model, earnings-forecast]; quality_analyst: [fundamental-filter, web-reader] ‖ → report_editor: [report-generate].
14. **geopolitical_war_room** — geopolitical_analyst: [geopolitical-risk, web-reader, global-macro]; energy_analyst: [commodity-analysis, geopolitical-risk]; supply_chain_analyst: [geopolitical-risk, sector-rotation, event-driven] ‖ → chief_strategist: [asset-allocation, risk-analysis, hedging-strategy].
15. **global_allocation_committee** — a_share_analyst: [tushare, technical-basic, fundamental-filter, hk-connect-flow, sector-rotation, multi-factor]; crypto_analyst (Crypto Analyst): [okx-market, perp-funding-basis, stablecoin-flow, crypto-derivatives, volatility, onchain-analysis]; us_hk_analyst: [yfinance, us-etf-flow, earnings-revision, adr-hshare, hk-connect-flow, technical-basic] ‖ → allocator: [asset-allocation, risk-analysis, correlation-analysis, strategy-generate].
16. **global_equities_desk** — a_share_researcher: [tushare, fundamental-filter, hk-connect-flow, technical-basic, multi-factor, sector-rotation]; us_hk_researcher: [yfinance, edgar-sec-filings, earnings-revision, us-etf-flow, adr-hshare, hk-connect-flow, technical-basic]; crypto_researcher (Crypto Asset Researcher): [okx-market, perp-funding-basis, stablecoin-flow, onchain-analysis, crypto-derivatives] ‖ → global_strategist: [asset-allocation, risk-analysis, strategy-generate, correlation-analysis].
17. **investment_committee** — bull_advocate (Bull-side Researcher): [technical-basic, fundamental-filter, yfinance, earnings-revision, sentiment-analysis]; bear_advocate (Bear-side Researcher): [technical-basic, fundamental-filter, yfinance, risk-analysis, volatility] ‖ → risk_officer (Chief Risk Officer): [risk-analysis, volatility, correlation-analysis] (input_from bull_report, bear_report) → portfolio_manager (Portfolio Manager): [strategy-generate, asset-allocation] (input_from full_debate: task-risk — note: PM only sees the CRO's summary, not the raw bull/bear reports). vars: target, market.
18. **macro_rates_fx_desk** — rates_analyst: [macro-analysis, global-macro, credit-analysis]; fx_strategist: [global-macro, macro-analysis, yfinance]; commodity_inflation_analyst: [commodity-analysis, global-macro, seasonal] ‖ → macro_pm: [asset-allocation, risk-analysis, hedging-strategy, strategy-generate].
19. **macro_strategy_forum** — global_economist: [global-macro, web-reader, geopolitical-risk]; domestic_economist: [macro-analysis, tushare]; policy_analyst: [regulatory-knowledge, sector-rotation, web-reader] ‖ → chief_strategist: [asset-allocation, macro-analysis].
20. **ml_quant_lab** — feature_engineer: [ml-strategy, factor-research, multi-factor]; data_scientist: [ml-strategy, quant-statistics] ‖ → backtest_engineer: [strategy-generate, backtest-diagnose, quant-statistics].
21. **pairs_research_lab** — correlation_scanner: [correlation-analysis, pair-trading, multi-factor]; cointegration_tester: [correlation-analysis, quant-statistics] ‖ → pair_strategist: [pair-trading, correlation-analysis, strategy-generate] → microstructure_reviewer: [market-microstructure, execution-model].
22. **portfolio_review_board** — attribution_analyst: [performance-attribution, multi-factor]; risk_inspector (Risk Inspector): [risk-analysis, volatility]; execution_analyst (Execution Quality Analyst): [execution-model, market-microstructure] (tools include analyze_trade_journal) ‖ → chief_investment_officer: [asset-allocation, risk-analysis]. vars: portfolio, review_period, goal.
23. **quant_strategy_desk** — screener: [tushare, fundamental-filter]; factor_miner: [multi-factor, factor-research] ‖ → backtester: [strategy-generate, technical-basic] → risk_auditor: [volatility] → report_aggregator: [report-generate] (input_from all four).
24. **risk_committee** — drawdown_analyst (Drawdown Analyst): [volatility]; tail_risk_analyst (Tail Risk Analyst): [volatility]; regime_detector (Market Regime Analyst): [volatility, technical-basic] ‖ → aggregator (Head of Risk): [] with tools [bash, read_file, write_file] (no load_skill). vars: goal.
25. **sector_rotation_team** — cycle_analyst: [macro-analysis, seasonal]; prosperity_analyst: [sector-rotation, fundamental-filter, multi-factor]; flow_analyst: [tushare, sentiment-analysis] ‖ → rotation_strategist: [strategy-generate, sector-rotation].
26. **sentiment_intelligence_team** — news_analyst (News Intelligence Analyst): [web-reader, sentiment-analysis, social-media-intelligence]; social_analyst: [behavioral-finance, sentiment-analysis, social-media-intelligence]; flow_analyst: [tushare, sentiment-analysis] ‖ → signal_synthesizer (Sentiment Signal Synthesizer): [behavioral-finance, risk-analysis]. vars: market, timeframe.
27. **social_alpha_team** — twitter_analyst: [social-media-intelligence, sentiment-analysis]; telegram_analyst: [social-media-intelligence, sentiment-analysis, onchain-analysis]; reddit_analyst: [social-media-intelligence, behavioral-finance, sentiment-analysis] ‖ → alpha_synthesizer: [social-media-intelligence, factor-research, quant-statistics].
28. **statistical_arbitrage_desk** — pair_scanner: [pair-trading, quant-statistics, correlation-analysis]; microstructure_analyst: [market-microstructure, execution-model] (tools include orderbook_depth) ‖ → arb_strategist: [pair-trading, strategy-generate, quant-statistics] → risk_monitor: [risk-analysis, volatility].
29. **technical_analysis_panel** — classic_ta_analyst (Classic Technical Analyst): [technical-basic, candlestick]; ichimoku_analyst: [ichimoku]; harmonic_analyst: [harmonic]; wave_analyst (Elliott Wave Analyst): [elliott-wave, chanlun]; smc_analyst (SMC / Order-Flow Analyst): [smc, minute-analysis] ‖ → signal_aggregator (Signal Aggregator (Judge)): [] (no load_skill). vars: target, timeframe.
30. **value_investing_committee** — buffett: [fundamental-filter, valuation-model, management-deep-dive]; munger: [risk-analysis, financial-statement, fundamental-filter]; duan_yongping: [management-deep-dive, fundamental-filter, thesis-tracker]; li_lu: [bottleneck-hunter, valuation-model, thesis-tracker, global-macro] ‖ → chair (Committee Chair — Consensus & Contradictions): [report-generate] (tools include report_audit).

Pattern summary: 24 of 31 are "N parallel analysts → 1 synthesizer" (fan-in), 5 are sequential chains, 2 are fan-in then a reviewer/auditor. Synthesizers typically get 2 skills (`asset-allocation`, `risk-analysis`) or none; analysts 1–6. Every bundled preset uses the same model for all agents (`model_name` unset).

## 7. Skill catalog relevant to the target bot (crypto spot, free data)

Of 90 bundled skills, the ones whose content applies to a Binance USDT spot bot with ccxt public data + futures public data + RSS:

| Skill | Cat | Size | What is useful | Fit |
|---|---|---|---|---|
| `technical-basic` | strategy | 2k | EMA12/26 + ADX14 (>25 = trending), BB(20,2) + RSI14 (30/70), OBV + volume ratio; "three-dimensional voting: Long = trend bullish + RSI not overbought + OBV rising"; Wilder EWM for RSI/ADX | high |
| `candlestick` | strategy | 1.9k | 15 patterns, score +1/−1, params `body_pct 0.1`, `shadow_ratio 2.0` | high |
| `volatility` | strategy | 2.1k | HV percentile ranking mean-reversion | medium (regime gating) |
| `perp-funding-basis` | crypto | 10.7k | funding regime table (8h: >+0.05% extreme long crowding → contrarian short/reduce longs; < −0.02% short-squeeze territory), `oi_funding_matrix()` (OI Δ24h >5% & funding >0.03% → "leveraged_long_buildup"), price/funding divergence table, alert thresholds (`BTC funding > +0.05% or < -0.03%`, `OI change > ±5% in 24h`) | high (Binance futures public data) |
| `liquidation-heatmap` | crypto | 12k | 24h liquidation bands (>$1B extreme … <$50M calm), long/short liq ratio (>2 longs squeezed, <0.5 shorts squeezed), `cascade_risk` (clusters within 2% → high), liquidation magnet rule (cluster 2× larger → magnet) | medium (needs liquidation feed; optional) |
| `market-microstructure` | analysis | 12k | spread, depth at ±1/2/5%, Amihud/Roll, impact | medium (order book snapshot) |
| `execution-model` | strategy | 11.5k | slippage formulas (linear / square-root impact), cost assumptions | medium (paper-mode fills) |
| `risk-analysis` | analysis | 15k | VaR/CVaR, max drawdown, Monte Carlo, GPD tail; "state the sample window every figure came from" | high (risk role) |
| `sentiment-analysis` | analysis | 4.5k (Chinese) | fear/greed zones (<20 buy zone, >80 sell zone), contrarian framing | low-medium (headline sentiment only) |
| `event-driven` | strategy | 8.5k | "The LLM acts as the NLP engine" — news → sentiment-scored signals | high (RSS news role) |
| `behavioral-finance` | analysis | 10.6k | cognitive-bias checklist, debiasing | medium (bull/bear/head prompts) |
| `research-discipline` | analysis | 2.9k | 4-bias self-check at start of any research task | medium (cheap prompt preamble) |
| `thesis-tracker` | strategy | 8.6k | per-holding written thesis, falsifiable assumptions, red lines, health score 1-10 → hold/add/trim/exit | high (position review role) |
| `trade-journal` | tool | 7.6k | 4 behavior diagnostics: disposition effect, overtrading, chasing, anchoring | medium (daily review of own journal) |
| `asset-allocation`, `hedging-strategy`, `correlation-analysis` | — | — | portfolio-level; mostly MPT/Black-Litterman/options | low (max 3 positions, no derivatives) |
| `okx-market`, `ccxt`, `minute-analysis` | data | — | ccxt: `fetch_ohlcv/fetch_ticker/fetch_order_book`, timeframes list, `enableRateLimit` | low (we already have ccxt) |
| Not applicable | | | onchain-analysis, stablecoin-flow, defi-yield, token-unlock-treasury (need Glassnode/DeFiLlama scraping), crypto-derivatives (carry/basis trades), options-*, all tushare/A-share/HK/US equity, social-media-intelligence (needs Twitter/Telegram scraping) | skip |

## 8. Mapping to the target bot (7-10 Claude calls per symbol, 15-min cycle)

Reference architecture from the previous bot (`/home/user/Trading_Bot/docs/research/previous_session_summary.md`): 7 agents `scanner, technical, news, bull, bear, risk_manager, head`, 12 calls/cycle for 2 candidates. Vibe-Trading's `investment_committee` is essentially the same shape (bull → bear → CRO → PM), which validates the roster; the useful additions are (a) per-role skill allowlists, (b) a "derivatives positioning" analyst (funding/OI/long-short from Binance futures public data), (c) a `-5..+5` scored output with a resonance/alignment rule, (d) a thesis-tracker style position review.

Proposed roster (per candidate symbol, ≤8 calls):

| # | Role | Model tier | Skills (allowlist, our rewritten versions) | Output (JSON) |
|---|---|---|---|---|
| 0 | scanner (once per cycle, not per symbol) | haiku | — (rule-based pre-filter preferred; LLM optional) | candidates[] |
| 1 | technical_analyst | haiku | technical-basic, candlestick, volatility | score −5..+5, direction, key levels, regime (trend/range), atr |
| 2 | derivatives_positioning_analyst | haiku | perp-funding-basis, liquidation-heatmap (optional section) | funding regime, oi_funding_matrix label, long/short ratio read, score −5..+5 |
| 3 | news_analyst | haiku | event-driven, sentiment-analysis (fear/greed zones) | score −5..+5, red-flag flag, RISK_OFF token |
| 4 | bull_advocate | sonnet | behavioral-finance (debias), research-discipline | thesis bullets w/ confidence, "what kills this" |
| 5 | bear_advocate | sonnet | risk-analysis, behavioral-finance | risk bullets w/ severity, "what disproves the bear case" |
| 6 | risk_manager | sonnet | risk-analysis, execution-model, market-microstructure | stance support/conditional/oppose, scorecard 1-5 per bull/bear point, size cap, SL/TP proposal, gates |
| 7 | head (decision) | opus | thesis-tracker (for position review), asset-allocation (sizing framing only) | action BUY/SELL/HOLD, confidence 0-1, size %, sl %, tp %, review_triggers[] |

Layers: {1,2,3} parallel → {4,5} parallel (receive 1-3) → 6 (receives 1-5) → 7 (receives 6 **plus** 1-5; do not repeat the `investment_committee` design where the PM only sees the CRO summary). That is 7 calls per symbol; with ≤3 candidates and the scanner that is ≤22 calls/cycle. Prompt caching: keep role + skill text + hard-limit text as the static prefix (same ordering principle as `build_worker_prompt`).

Hard limits stay in code (10%/symbol, 3 positions, SL ≤2%, TP ≤4%, −3% daily halt, 3 losses halt, conf ≥0.65, cooldowns, kill switch, −6% drawdown ladder). The head's JSON is clamped by code; Vibe-Trading has no such layer (it trusts the PM's prose), which is the main thing not to copy.

## 9. Implementation notes for our skill system

- Keep Vibe-Trading's three-key frontmatter (`name`, `description`, `category`) and the directory-per-skill layout, but parse with a real YAML loader (PyYAML `safe_load` on the frontmatter block) and validate with pydantic; the hand-rolled parser in `frontmatter.py` cannot handle multi-line descriptions.
- Our agents are **single-turn structured-output calls**, so we should NOT expose a `load_skill` tool. Instead, resolve each role's `skills: [...]` allowlist at prompt-build time and inline the full body of each allowed skill into the system prompt (skills are short — 2-12k chars — and this keeps the cacheable prefix stable). Keep the allowlist concept and the fail-closed error for unknown names (`"skill {name!r} is outside the skill allowlist"`), applied at config-load time.
- Add a `model_tier` field per role (Vibe-Trading's `SwarmAgentSpec.model_name` exists but is unused by presets) and an `input_from: {key: role}` mapping to name the upstream sections (`### technical`, `### derivatives`, …) exactly like `build_worker_prompt`.
- Copy the **Data Citation Discipline** rule into every role prompt in our own words: numbers must come from the provided data block or an upstream section; otherwise omit and mark "not verified".
- Copy the `incomplete ≠ failed` classification idea: a JSON that parses but has `confidence` null or an empty rationale is "incomplete" and is treated as HOLD, never as a tradable signal.
- User-override directory (`~/.<bot>/skills/user/` first, bundled second, first name wins) is cheap and useful for the user to tweak checklists without touching the repo.
- Attribution: add a NOTICE line "threshold tables for funding-rate regimes, OI×funding matrix and liquidation bands adapted from Vibe-Trading (HKUDS, MIT)" in the skill files that keep those numbers.


## 채택 후보 (adopt)

- Three-key SKILL.md frontmatter (name, description, category) with directory-per-skill layout and user-dir-overrides-bundled discovery — agent/src/agent/skills.py:97-135 (re-implement with PyYAML + pydantic validation instead of agent/src/agent/frontmatter.py)
- Per-role skill allowlist that fails closed on unknown names — agent/src/tools/load_skill_tool.py:224-230 and agent/src/tools/__init__.py:422-427; for our single-turn JSON agents, resolve the allowlist at prompt-build time and inline full skill bodies instead of exposing a load_skill tool
- Two-level progressive disclosure (one-line descriptions in the prompt, full body on demand) — agent/src/agent/skills.py:143-190; we only need level 1 for a future 'skill catalog' dashboard page and level 2 inlined per role
- Worker prompt layout with cache-stable prefix (Role → Skills → hard rules) and per-call tail (upstream sections, data block, date) — agent/src/swarm/worker.py:285-446
- Data Citation Discipline hard rule: every number must trace to provided data or an upstream section, otherwise omit and mark unverified — agent/src/swarm/worker.py:339-360
- input_from {context_key: upstream_role} mapping rendered as '### key' sections, separate from depends_on scheduling — agent/src/swarm/runtime.py:1086-1090 and worker.py:365-374; let the head receive bull/bear/risk AND the raw analyst sections (investment_committee only passes the CRO summary to the PM)
- Deliverable classification 'incomplete' distinct from 'failed' (empty, fabricated, plan-only) → treated as HOLD — agent/src/swarm/worker.py:1413-1449 and models.py:153-172
- Investment-committee role contract: bull must list 'Main risk to the bull case', bear must list 'What would disprove the bear case', CRO gives support/conditional/oppose + 1-5 reliability scorecard per point + explicit size cap, PM decides by weighting not head-count — agent/src/swarm/presets/investment_committee.yaml:37-44, 108-115, 180-189, 229-238
- Signal aggregator resonance rule: each analyst outputs −5..+5, judge counts votes, averages, applies regime weights, '≤2/5 chaotic — stand aside', writes a dissent note and a signal shelf-life with invalidation prices — agent/src/swarm/presets/technical_analysis_panel.yaml:299-335
- Desk risk-manager explicit gate list and 'if upstream data unavailable the gate is unavailable — say so instead of inventing a threshold' — agent/src/swarm/presets/crypto_trading_desk.yaml:154-180
- Signal-alignment confidence ladder (all aligned → high, 2-of-3 → medium, all diverging → low/wait) with a stated priority order for conflicts — agent/src/swarm/presets/crypto_research_lab.yaml:202-211
- technical-basic voting rule and defaults (EMA12/26, ADX14>25, BB20/2, RSI14 30/70, OBV, Wilder EWM) — agent/src/skills/technical-basic/SKILL.md:12-47
- candlestick 15-pattern +1/−1 scoring with body_pct 0.1 / shadow_ratio 2.0 — agent/src/skills/candlestick/SKILL.md:12-48
- Funding-rate regime table and alert thresholds (8h > +0.05% extreme long crowding; < −0.02% short-squeeze; annualize = rate×3×365) — agent/src/skills/perp-funding-basis/SKILL.md:25-48, 204-212
- oi_funding_matrix() labels (OI Δ24h >5% & funding >0.03% → leveraged_long_buildup, etc.) and price/funding divergence table — agent/src/skills/perp-funding-basis/SKILL.md:152-185
- Liquidation 24h bands, long/short liquidation ratio thresholds and cascade_risk cluster-gap rule (<2% high) as an OPTIONAL analyst section — agent/src/skills/liquidation-heatmap/SKILL.md:171-191, 123-151
- thesis-tracker position-review pattern (written thesis, falsifiable assumptions, red lines, 1-10 health → hold/add/trim/exit) for the head's open-position review — agent/src/skills/thesis-tracker/SKILL.md frontmatter
- event-driven 'LLM as the NLP engine' news → scored signal framing for the news analyst — agent/src/skills/event-driven/SKILL.md
- Per-agent model_name override field in the role spec (exists in agent/src/swarm/models.py:199 but unused by presets) → our model_tier: haiku/sonnet/opus per role
- inspect_preset-style dry-run validation of the roster (duplicate ids, unknown upstream, DAG cycles, undeclared template vars) — agent/src/swarm/presets.py:171-283

## 제외 (skip)

- load_skill as a runtime tool with outline/section/offset paging (agent/src/tools/load_skill_tool.py) — our agents are single-turn structured-output calls with no tool loop; inline skills instead
- Hand-rolled frontmatter parser (agent/src/agent/frontmatter.py) — use PyYAML safe_load + pydantic
- Multi-iteration ReAct workers with bash/write_file/report.md and 1800s timeouts (agent/src/swarm/worker.py Execution Rules) — too slow and expensive for a 15-minute cycle; also the hand-off payload is free-form markdown, we need JSON
- Final decision = last-layer summary with no code-level clamping (agent/src/swarm/runtime.py:752-758) — our hard limits must be enforced in code on the head's JSON
- Empty skills: [] meaning 'unrestricted' (worker.py:669) — in our config an empty list must mean no skills
- Options/derivatives-trading skills: crypto-derivatives (carry/basis trades), options-strategy, options-advanced, options-payoff, hedging-strategy — spot only, no leverage
- On-chain and DeFi skills: onchain-analysis, stablecoin-flow, defi-yield, token-unlock-treasury — require Glassnode/DeFiLlama/Dune data we do not have
- All equity/A-share/HK/US data skills: tushare, akshare, eastmoney, mootdx, yfinance, sec-edgar, edgar-sec-filings, hk-connect-flow, adr-hshare, us-etf-flow, ashare-pre-st-filter, convertible-bond, fund-analysis, etf-analysis, fundamental-filter, financial-statement, valuation-model, earnings-*, dividend-analysis
- social-media-intelligence / social_alpha_team presets — need Twitter/Telegram/Reddit scraping
- Pattern-school TA skills needing extra libraries and long lookbacks (ichimoku, harmonic, elliott-wave, chanlun/czsc, smc/smartmoneyconcepts) — low value on 15-min spot cycles and add dependencies; technical-basic + candlestick suffice
- crypto_trading_desk 40/35/25 weighting (on-chain > funding > liquidation) — on-chain unavailable; re-derive weights for technical/derivatives/news
- Grounding block that pre-fetches 30 days of OHLCV per symbol (agent/src/swarm/grounding.py) — our cycle already passes exact candles; keep the 'prefer provided data over memory' instruction only
- Vibe-Trading's prose SKILL.md text verbatim — rewrite procedures in our own words, keep only formulas/thresholds with an MIT attribution note