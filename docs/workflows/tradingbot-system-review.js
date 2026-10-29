export const meta = {
  name: 'tradingbot-system-review',
  description: 'System-level adversarial review of the whole trading bot across 7 lenses (each proves findings with failing tests), then per-file fixes and a final integration run',
  phases: [
    { title: 'Find', detail: '7 lens reviewers, each writes failing proof tests' },
    { title: 'Fix', detail: 'one fixer per primary-file group' },
    { title: 'Integrate', detail: 'full suite + offline e2e + README accuracy' },
  ],
}

const REPO = args.repo, PY = args.py, SKILLDOC = args.skilldoc

const COMMON = `
PROJECT: ${REPO} — Claude API multi-agent Binance SPOT long-only trading bot. Contracts: ${REPO}/SPEC.md (v1, v2, and the final 'SPEC v2.1' part which wins on conflicts), ${REPO}/DESIGN.md, ${REPO}/README.md. Code in ${REPO}/tradingbot/, tests in ${REPO}/tests/ (3,578 tests, all green), prompts in ${REPO}/prompts/, skills in ${REPO}/skills/. Offline end-to-end run: cd ${REPO} && MODE=paper AGENT_BACKEND=mock MOCK_SCENARIO=approve TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= ANTHROPIC_API_KEY= DATA_DIR=<a fresh temp dir> ${PY} -m tradingbot.main run --once --offline-fixture (MOCK_SCENARIO=conservative for the HOLD path). Tests: cd ${REPO} && ${PY} -m pytest <files> -q -p no:cacheprovider (the suite takes ~40s; run only what you need unless told otherwise). FROZEN: tradingbot/models.py, tradingbot/config.py, tradingbot/agents/schemas.py (change requests go in your report). No network, no real keys. Do NOT git commit.`

const LENSES = [
  { key: 'money', title: 'Money safety and hard limits', prompt: `Can any path open, size or keep a position outside the hard limits (10% per symbol, 3 positions, stop <= 2%, take-profit <= 4%, daily -3% halt, 3 consecutive losses, confidence floor 0.65/0.75, cooldown 4 / revenge 8 cycles, drawdown ladder -3/-4.5/-6%, net R:R >= 1.5, MIN_ORDER_USDT, spot market buys only)? Trace from LLM outputs (agents/schemas -> pipeline -> gates.g4_final -> cycle execution -> exchange -> portfolio) and from code exits (risk.check_exits -> cycle). Look for: a head BUY that bypasses risk veto or G3/G4; stop/TP re-anchoring after the fill that ends outside limits; size computed on stale equity or free_quote; double buys of one symbol in one cycle; a BUY on a symbol already held; a SELL of more than held; exits blocked by a halted/kill/budget state (exits must ALWAYS run); funding-window and macro-alarm rules; trim semantics; partial fills; what happens when the broker raises after a fill. Write failing proof tests in ${REPO}/tests/test_sysrev_money.py.` },
  { key: 'persistence', title: 'Persistence, crash safety, ordering', prompt: `Check the ordering invariant "order fill -> portfolio update -> state save -> notify" for every trade path (entry, code exit, head sell, partial, kill-switch flows), crash windows (process dies between fill and save; between save and journal_state save; news_store/journal_state/cost_ledger/scanner cache writes atomic?), restart behaviour (cycle counter, cooldowns, daily rollover across restarts, peak equity, kill switch persisted, llm pause persisted), bot.lock stale takeover, control/STOP and control/CLEAR handling (idempotent, consumed exactly once), corrupt-state policy per mode, and that journal_state.thesis/entry_verdicts stay consistent with positions (orphans after a crash or a code exit). Write failing proof tests in ${REPO}/tests/test_sysrev_persistence.py.` },
  { key: 'secrets', title: 'Secrets and data hygiene', prompt: `Hunt for any path where ANTHROPIC_API_KEY, BINANCE_*_API_KEY/SECRET or TELEGRAM_BOT_TOKEN can appear in: logs (any level, including exception tracebacks via log.exception), Telegram messages, dashboard API responses (/api/state, /api/decisions, /api/cycles..., errors), logs/decisions.jsonl, cycle reports (.json/.md), journal files, cost_ledger.jsonl, LLM payloads or system prompts, repr/str of Settings/backends/brokers/transports/notifier, CLI 'status' output, and exception messages raised by ccxt/anthropic/urllib (e.g. a URL containing the bot token). Also check the dashboard binds to 127.0.0.1 by default, the kill token compare is constant-time, and no endpoint writes anything except control files. Use fake secrets with distinctive values and caplog/capsys. Write failing proof tests in ${REPO}/tests/test_sysrev_secrets.py.` },
  { key: 'claude_api', title: 'Claude API integration', prompt: `Verify tradingbot/llm/anthropic_backend.py, llm/cost.py, agents/runner.py, agents/roles.py against the installed anthropic SDK (inspect it under ${REPO}/.venv/lib/python*/site-packages/anthropic) and the API reference in ${SKILLDOC} (read python/claude-api/README.md, shared/tool-use-concepts.md 'Structured Outputs' section, shared/prompt-caching.md). Check: (1) the request kwargs are valid for client.messages.create in SDK 2.3.0 (model ids claude-haiku-5-5 / claude-sonnet-5-5 / claude-opus-5-5, output_config with both effort and format, no thinking, no tool_choice, no temperature); (2) the JSON schema sent for EVERY role output model (ROLE_OUTPUT_MODELS in agents/schemas.py) obeys the structured-output limits: every object has additionalProperties false, every property listed in required, no minimum/maximum/minLength/maxLength left, no recursive refs, $defs/$ref resolvable — write a test that builds the exact output_config.format for each role and checks these rules mechanically; (3) prompt caching: the system block is byte-identical across two cycles for every role (build it twice through the runner with different payloads/time) and carries cache_control ttl 1h; (4) cost math: input, cache read, 1h cache write (2x input) and output prices per model from PRICES_USD_PER_MTOK; Haiku 5.5 long-context price note; unknown model handling; (5) budget enforcement under parallel run_many; (6) error mapping and abstain semantics for auth/rate-limit/timeout/server/network/refusal/max_tokens/parse errors, and that an auth error leads to the 60-minute LLM pause in cycle; (7) per-role max_tokens is large enough for the output schema (estimate worst-case JSON size for head/risk with 2-3 symbols) and timeouts fit the 5-minute cycle deadline. Write failing proof tests in ${REPO}/tests/test_sysrev_claude_api.py.` },
  { key: 'contract', title: 'Payload/prompt/schema contract consistency', prompt: `Mechanically compare, for every role: (a) the keys actually produced by tradingbot/agents/payloads.py builders (build real payloads from realistic fixtures, e.g. via tradingbot/testing.py FakeMarketData + scanner + MockBackend approve scenario, or by calling the builders directly), (b) the SPEC v2.1 §20.3a key list, and (c) every backticked field path used in prompts/<role>.md (and the role's skills). Report keys the prompt cites that the payload never contains, keys present with always-null values in realistic runs (e.g. sentiment components, event_signal, news_context, prev_assumption_status, thesis_history, metrics_table blocks), and enum/value-range mismatches between prompts and agents/schemas.py. Also check pipeline wiring: does each role receive exactly what §20.3a says (bull never sees bear, technical never sees thesis), and do role outputs flow to the next stage as specified. Write failing proof tests in ${REPO}/tests/test_sysrev_contract.py (a test that asserts every prompt-cited path resolves in a realistic payload is ideal).` },
  { key: 'telegram', title: 'Telegram coverage for every trade event', prompt: `The user requirement: a Telegram message on EVERY trade. Verify that each of these paths calls Notifier.notify with a correct TradeEvent and a readable Korean message (use a recording fake Transport, async_send=False or flush): paper/testnet entry fill; code exits (stop_loss, take_profit, time_stop); head SELL exit; trim/partial exit; order rejected by the exchange; order downgraded by a gate after the head said BUY; kill switch (dashboard STOP file, CLI stop, drawdown -6% auto STOP_ALL); daily -3% / 3-loss halts; LLM budget exceeded; LLM auth pause; daily summary at UTC rollover; startup/shutdown; heartbeat every 4h; errors/cycle deadline. Check message contents (symbol, qty, price, SL/TP with %, P&L, reason, mode tag [paper]/[testnet]/[live]), that notify never blocks or raises into the cycle, failed messages land in the failed log, and that the real TelegramTransport builds the right URL/payload (sendMessage, chat_id, text, disable_web_page_preview) and handles HTTP 429 (retry_after) and long messages (> 4096 chars split or truncated). Write failing proof tests in ${REPO}/tests/test_sysrev_telegram.py.` },
  { key: 'operations', title: 'Operations: CLI, loop, dashboard, backtest, docs', prompt: `Check main.py run loop timing (cycle boundary + 5s, drift, --cycle-minutes, SIGINT/SIGTERM shutdown flush), live-mode triple confirmation cannot be bypassed (MODE=live + CONFIRM_LIVE=YES + typed LIVE; non-interactive stdin must refuse), --offline-fixture refuses non-paper, 'status' works with no state, 'stop'/'clear-stop' interplay with a running cycle, the dashboard end-to-end with a real data dir produced by an offline run (every endpoint returns sensible data from those files; the HTML references only same-origin resources), 'backtest' runnable offline (does it need network? if so, is there an offline option or a clear error?), .env loading order, and that README.md instructions (commands, env vars, file names, numbers like test counts) match the code. Write failing proof tests in ${REPO}/tests/test_sysrev_operations.py, and list README inaccuracies as findings with file README.md.` },
]

const FIND_SCHEMA = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
      title: { type: 'string' }, files: { type: 'array', items: { type: 'string' }, description: 'repo-relative files that must change to fix it, primary first' },
      failure_scenario: { type: 'string' }, proof_test: { type: 'string', description: 'test node id in the sysrev test file, or "" if not provable by a test' },
      confirmed: { type: 'boolean', description: 'true only if the proof test fails against the current code, or (for docs) the inaccuracy was verified' },
      suggested_fix: { type: 'string' },
    }, required: ['id', 'severity', 'title', 'files', 'failure_scenario', 'proof_test', 'confirmed', 'suggested_fix'], additionalProperties: false } },
    test_file: { type: 'string' },
    checked_ok: { type: 'array', items: { type: 'string' }, description: 'things you verified are correct' },
    summary: { type: 'string' },
  },
  required: ['findings', 'test_file', 'checked_ok', 'summary'],
  additionalProperties: false,
}
const FIX_SCHEMA = {
  type: 'object',
  properties: {
    fixed: { type: 'array', items: { type: 'string' } },
    not_fixed: { type: 'array', items: { type: 'string' } },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_run: { type: 'string' },
    cross_file_requests: { type: 'array', items: { type: 'string' } },
  },
  required: ['fixed', 'not_fixed', 'files_changed', 'tests_run', 'cross_file_requests'],
  additionalProperties: false,
}

phase('Find')
const found = (await parallel(LENSES.map(l => () => agent(
`SYSTEM REVIEW — lens: ${l.title}.
${l.prompt}
Rules: only real defects (spec violations, unsafe behaviour, crashes, leaks, wrong math, missing required behaviour). For each, write a failing test proving it in the named test file (create it; do not edit other test files), run it, and set confirmed=true only if it fails now. Unprovable but important concerns: confirmed=false with proof_test "". Style nits are not findings. Also list in checked_ok what you verified as correct, so coverage is visible. Do not fix code.
${COMMON}`,
  { label: `find:${l.key}`, phase: 'Find', schema: FIND_SCHEMA, effort: 'high' })))).map((r, i) => r ? { ...r, lens: LENSES[i].key } : null).filter(Boolean)

const all = found.flatMap(r => r.findings.map(f => ({ ...f, lens: r.lens, test_file: r.test_file })))
const confirmed = all.filter(f => f.confirmed)
log(`find: ${all.length} findings, ${confirmed.length} confirmed, lenses ok ${found.length}/${LENSES.length}`)

// group confirmed findings by primary file
const groups = {}
for (const f of confirmed) {
  const primary = (f.files && f.files[0]) || 'unknown'
  ;(groups[primary] = groups[primary] || []).push(f)
}
const groupList = Object.entries(groups).map(([file, fs]) => ({ file, findings: fs }))
log(`fix groups: ${groupList.map(g => g.file + '(' + g.findings.length + ')').join(', ')}`)

phase('Fix')
const fixes = await parallel(groupList.map(g => () => agent(
`Fix these confirmed system-review findings whose primary file is ${g.file}: ${JSON.stringify(g.findings)}.
Edit ${g.file} (and, only if unavoidable, the other files listed in a finding — other fixers are editing other primary files concurrently, so keep edits outside ${g.file} minimal and mention them in files_changed). Make the proof tests pass (the test files are listed per finding) without breaking existing tests of the touched modules: run the proof tests and the existing test files of the modules you changed. If a proof test contradicts SPEC v2.1, fix or delete that test and explain in not_fixed. If the right fix needs a frozen file, implement the safest workaround and put the request in cross_file_requests.
${COMMON}`,
  { label: `fix:${g.file.replace(/[^a-zA-Z0-9]+/g, '_')}`, phase: 'Fix', schema: FIX_SCHEMA, effort: 'high' })))

phase('Integrate')
const INTEG_SCHEMA = {
  type: 'object',
  properties: {
    pytest_junit: { type: 'string', description: 'tests/failures/errors from a --junitxml run of the whole suite' },
    tests_failed: { type: 'integer' },
    e2e: { type: 'string', description: 'outcome of conservative + approve offline runs and a second approve cycle' },
    files_changed: { type: 'array', items: { type: 'string' } },
    unresolved: { type: 'array', items: { type: 'string' } },
    notes: { type: 'array', items: { type: 'string' } },
  },
  required: ['pytest_junit', 'tests_failed', 'e2e', 'files_changed', 'unresolved', 'notes'],
  additionalProperties: false,
}
const integ = await agent(
`Integrate the system-review fixes. (1) Run the FULL suite with --junitxml and fix any cross-module breakage until 0 failures (keep SPEC signatures; do not weaken proof tests). (2) Apply the cross-file requests the fixers reported if they are safe: ${JSON.stringify(fixes.filter(Boolean).flatMap(f => f.cross_file_requests))}. (3) Run the offline e2e: conservative, approve, then a second approve cycle in the same DATA_DIR; confirm exit 0, no traceback, expected entries/HOLD, and the notifier log lines for entries. (4) Unconfirmed findings for the record (do nothing unless trivial and clearly correct): ${JSON.stringify(all.filter(f => !f.confirmed).map(f => ({ id: f.id, title: f.title, files: f.files })))}.
${COMMON.replace('run only what you need unless told otherwise', 'run the whole suite here')}`,
  { label: 'integrate', phase: 'Integrate', schema: INTEG_SCHEMA, effort: 'high' })

return { found, fixes, integ }
