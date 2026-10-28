export const meta = {
  name: 'tradingbot-followups',
  description: 'Follow-ups from the system review: G4 exec-cost recheck, role token/effort/timeout budgets, news score snapping, Fear&Greed fetcher, halt/resume/heartbeat event kinds, lock staleness, status without keys; then review the diff and integrate',
  phases: [
    { title: 'Stage1', detail: 'decision-path fixes + Fear&Greed fetcher (parallel, disjoint files)' },
    { title: 'Stage2', detail: 'event kinds, notify formats, lock, CLI, wiring' },
    { title: 'Review', detail: 'adversarial review of the follow-up diff' },
    { title: 'Integrate', detail: 'fix findings, full suite, offline e2e' },
  ],
}

const REPO = args.repo, PY = args.py, BASE = args.base
const COMMON = `
PROJECT: ${REPO} — Claude API multi-agent Binance SPOT long-only bot; read ${REPO}/SPEC.md (final 'SPEC v2.1' part wins), ${REPO}/DESIGN.md. 3,701 tests are green at commit ${BASE}. Tests: cd ${REPO} && ${PY} -m pytest <files> -q -p no:cacheprovider. Offline run: MODE=paper AGENT_BACKEND=mock MOCK_SCENARIO=approve TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= ANTHROPIC_API_KEY= DATA_DIR=<fresh tmp> ${PY} -m tradingbot.main run --once --offline-fixture. No network in tests, no real keys, logging only (no print except CLI output), secrets never logged. Keep public signatures backward compatible (add keyword-only params with defaults). Update SPEC.md/DESIGN.md text where your change alters a documented contract (append a short note in the 'SPEC v2.1' 보완 사항 list rather than rewriting old sections). Do NOT git commit.`

const RESULT = {
  type: 'object',
  properties: {
    done: { type: 'array', items: { type: 'string' } }, not_done: { type: 'array', items: { type: 'string' } },
    files_changed: { type: 'array', items: { type: 'string' } }, tests: { type: 'string' },
  },
  required: ['done', 'not_done', 'files_changed', 'tests'], additionalProperties: false,
}

phase('Stage1')
const [b, c] = await parallel([
  () => agent(
`Decision-path follow-ups. Files you may edit: tradingbot/gates.py, tradingbot/agents/roles.py, tradingbot/agents/pipeline.py, the single g4_final call in tradingbot/cycle.py (around line 1424; pass one new kwarg, nothing else in cycle.py), their tests, SPEC.md/DESIGN.md notes.
1) MONEY-7: g4_final gains keyword-only exec_cost_pct: float | None = None; after the final stop distance is known (after clamp_stops/enforce_net_rr), HOLD with a Korean reason when exec_cost_pct is not None and exec_cost_pct > 0.25 * final_stop_pct. cycle passes the candidate's risk_pack.exec_cost_pct (find where the risk pack for the symbol is available in that function). Tests for the boundary and for a stop tightened by enforce_net_rr.
2) CLAUDE-API-4: thinking cannot be disabled on Opus 5.5 and counts toward max_tokens. Set role budgets: technical max_tokens 6000; derivatives_flow 8000; news_sentiment 8000; bull 8000; bear 8000; risk_manager 12000 effort medium timeout 75; head 16000 effort medium (Opus 5.5 default; 'high' thinks longer than the 15-minute cycle deadline allows) timeout 120; journal 6000. Keep all other RoleSpec fields. Make sure the per-call deadline logic (backend.set_deadline / should_stop) still bounds the whole pipeline under the 300 s cycle deadline; adjust a test only if it encoded the old numbers. Document the reasoning in DESIGN.md §4 and the SPEC v2.1 notes.
3) CONTRACT-8: snap every NewsEvent.score to the nearest step of (-1.0, -0.66, -0.33, 0.0, 0.33, 0.66, 1.0) when NewsStore.add_events stores it and before news-risk symbol derivation; log at DEBUG when a score was snapped; tests for -0.65 -> -0.66 (now counts as news risk) and 0.1 -> 0.0.
${COMMON}`,
    { label: 'stage1:decision', phase: 'Stage1', schema: RESULT, effort: 'high' }),
  () => agent(
`CONTRACT-7: create tradingbot/sentiment_sources.py with class FearGreedFetcher(cache_path: Path | None = None, ttl_s: int = 3600, *, http_get: Callable[[str, float], str] | None = None, url: str = "https://api.alternative.me/fng/?limit=1", timeout_s: float = 10.0) whose __call__() -> int | None returns today's Fear & Greed value 0..100 (JSON: {"data":[{"value":"34",...}]}), caches it in memory and in cache_path (JSON {"value", "fetched_ts"}), serves the cache within ttl_s, returns the last cached value (even if stale up to 24h) on any network/parse error, and None otherwise; never raises; default http_get uses urllib with a User-Agent and the timeout; values outside 0..100 are rejected. Tests in tests/test_sentiment_sources.py with an injected http_get (success, cache hit, stale-cache fallback on error, garbage JSON, out-of-range, no cache -> None). Do not edit any other file (wiring into main.py is done by another agent later). ${COMMON}`,
    { label: 'stage1:feargreed', phase: 'Stage1', schema: RESULT, effort: 'medium' }),
])

phase('Stage2')
const a = await agent(
`Event/ops follow-ups. Files you may edit: tradingbot/models.py, tradingbot/config.py, tradingbot/notify.py, tradingbot/cycle.py, tradingbot/main.py, tradingbot/dashboard/app.py (only if needed for new kinds), their tests, README.md (only the lines about these behaviours), SPEC.md/DESIGN.md notes.
1) models.py: extend EventKind with "halt", "halt_lifted", "heartbeat" (keep all existing values).
2) notify.py: Korean formatters — halt: '⛔ 신규 진입 중단 [mode]' + Korean reasons per code (daily_loss, consecutive_losses, drawdown_block, drawdown_stop_all, kill_switch, llm_budget, llm_paused...) + equity; halt_lifted: '✅ 신규 진입 재개 [mode]' + which halts lifted; heartbeat: '💓 하트비트 [mode]' + equity, daily P&L, open positions with unrealized %, exposure %, LLM spend today. Keep backward compatibility: an old-style event (kind daily_summary with extra.heartbeat, kind error with extra.code=='halt', kind startup with extra.event=='halt_lifted') still renders sensibly.
3) cycle.py: _notify_halts emits kind "halt" / "halt_lifted"; main.py heartbeat emits kind "heartbeat" with the fields above.
4) config.py: add a public read_env(dotenv_path: Path | None = None) -> dict[str, str] (keep _read_env as an alias); main.py uses read_env.
5) PERS-8: main.py lock — treat bot.lock as stale when the stored host differs from socket.gethostname() or when its ts is older than max(3 * cycle_minutes * 60, 1800) seconds (the run loop already refreshes ts every cycle; make sure it also refreshes while sleeping between cycles, at least every 5 minutes), in addition to the dead-PID rule. Tests with a live PID but stale ts / other host.
6) 'status' must work without a valid backend config (like stop/clear-stop): fall back to reading DATA_DIR from read_env and printing state from files.
7) Wire tradingbot/sentiment_sources.FearGreedFetcher (created by another agent; read it) into main.build_components for non-offline runs (cache at <data_dir>/cache/fear_greed.json) and pass it to Scanner(fear_greed_fetcher=...); offline fixture keeps None.
Run the tests of every module you touched plus tests/test_sysrev_*.py.
Stage-1 report for context: ${JSON.stringify({ b, c }).slice(0, 4000)}
${COMMON}`,
  { label: 'stage2:events-ops', phase: 'Stage2', schema: RESULT, effort: 'high' })

phase('Review')
const REVIEW = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, title: { type: 'string' },
      files: { type: 'array', items: { type: 'string' } }, failure_scenario: { type: 'string' }, proof_test: { type: 'string' }, confirmed: { type: 'boolean' },
    }, required: ['id', 'severity', 'title', 'files', 'failure_scenario', 'proof_test', 'confirmed'], additionalProperties: false } },
    summary: { type: 'string' },
  },
  required: ['findings', 'summary'], additionalProperties: false,
}
const review = await agent(
`Adversarially review the follow-up diff: cd ${REPO} && git diff ${BASE} -- . ':!docs' (plus new untracked files: git status --porcelain). Look for: a path where exec cost check could be bypassed or wrongly HOLD valid trades; role budgets that still let the pipeline exceed the 300 s cycle deadline; snapped news scores changing risk semantics incorrectly; FearGreedFetcher raising or blocking the cycle (network timeout inside the scan) or leaking anything; new event kinds not handled somewhere (dashboard, reports, notify fallbacks, journal) or Telegram messages that lost information compared to before; lock staleness rules that could let two bot instances run at once (the dangerous direction) — e.g. a live bot sleeping longer than the stale threshold; status/stop/clear-stop fallbacks reading the wrong DATA_DIR. Prove each defect with a failing test in ${REPO}/tests/test_followups_review.py; confirmed=true only if it fails now. Do not fix. ${COMMON}`,
  { label: 'review', phase: 'Review', schema: REVIEW, effort: 'high' })

phase('Integrate')
const integ = await agent(
`Fix the confirmed findings ${JSON.stringify(review ? review.findings.filter(f => f.confirmed) : [])} (make tests/test_followups_review.py pass without weakening it unless it contradicts SPEC — then explain). Then run the FULL suite with --junitxml and fix any breakage until 0 failures, and run the offline e2e (conservative; approve; second approve cycle in the same DATA_DIR) confirming exit 0 and no traceback. Report junit totals and e2e outcome in 'tests'. ${COMMON}`,
  { label: 'integrate', phase: 'Integrate', schema: RESULT, effort: 'high' })

return { b, c, a, review, integ }
