export const meta = {
  name: 'tradingbot-alerts',
  description: 'Add market/position alert notifications (regime transition, news risk event, position health/red line) and a notify-test CLI command, then review and fix',
  phases: [{ title: 'Implement' }, { title: 'Review' }, { title: 'Fix' }],
}
const REPO = args.repo, PY = args.py, BASE = args.base
const COMMON = `PROJECT: ${REPO} (read SPEC.md final 'SPEC v2.1' part and DESIGN.md §7). 3,822 tests are green at ${BASE}. Tests: cd ${REPO} && ${PY} -m pytest <files> -q -p no:cacheprovider. Offline e2e: MODE=paper AGENT_BACKEND=mock MOCK_SCENARIO=approve TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= ANTHROPIC_API_KEY= DATA_DIR=<fresh tmp> ${PY} -m tradingbot.main run --once --offline-fixture. No network in tests, secrets never logged. Do NOT edit README.md. Do NOT git commit.`
const RESULT = { type: 'object', properties: { done: { type: 'array', items: { type: 'string' } }, not_done: { type: 'array', items: { type: 'string' } }, files_changed: { type: 'array', items: { type: 'string' } }, tests: { type: 'string' } }, required: ['done', 'not_done', 'files_changed', 'tests'], additionalProperties: false }

phase('Implement')
const impl = await agent(
`DESIGN.md §7 promises Telegram messages that the code does not send. Implement them:
1) tradingbot/models.py EventKind: add "alert" (keep all existing values).
2) tradingbot/notify.py: a Korean formatter for kind "alert" keyed by extra.alert: "regime" ('🌐 레짐 전환 [mode]' old -> new verdict, e.g. normal -> fused/macro_alarm, and the rule change it causes: size halved / confidence floor 0.75 / no new entries; also btc_shock and NAME alarm symbols), "news_risk" ('📰 뉴스 위험 [mode] SYMBOL' with event_type, score on the 7-step scale, Korean summary, whether the symbol is held and what the code does: no new entry, flagged for head review if held), "position_health" ('🩺 포지션 점검 [mode] SYMBOL' health, red lines triggered, risk action, assumption status counts, and the head decision for it if one was made this cycle).
3) tradingbot/cycle.py: emit these alerts — regime: when scan.regime.verdict differs from the previous cycle's verdict (persist the last verdict in journal_state or a small state file in data_dir so a restart does not spam; first cycle after start sends nothing unless non-normal); news_risk: when a symbol in the watchlist or held enters the news-risk set (dedupe per symbol+headline id for 24h, persisted); position_health: when a held position's risk health <= 4 or a red line hits (once per symbol per change of condition, not every cycle). All alerts go through the existing non-blocking Notifier after state is saved; failures never break the cycle.
4) tradingbot/main.py: new subcommand 'notify-test' that reads TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID via config.read_env (works without ANTHROPIC_API_KEY, like stop/clear-stop), sends one synchronous message '✅ 텔레그램 연결 테스트 [mode] — 이 메시지가 보이면 거래 알림이 정상 전송됩니다' through TelegramTransport, prints success or a masked error (never the token), exit code 0/1; missing token or chat id -> clear Korean error, exit 2.
5) Tests: tests/test_alerts.py (recording fake Transport, async_send=False; regime transition once, no spam across cycles and restarts, news risk dedupe, health threshold edge 4/5, red line, alert formatting for each type), tests for notify-test in tests/test_main.py style with an injected http_post (success, HTTP error masked, missing env).
6) Append a short note to the SPEC v2.1 보완 사항 list and DESIGN.md §7 (now implemented).
${COMMON}`,
  { label: 'implement', phase: 'Implement', schema: RESULT, effort: 'high' })

phase('Review')
const REVIEW = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, title: { type: 'string' }, failure_scenario: { type: 'string' }, proof_test: { type: 'string' }, confirmed: { type: 'boolean' } }, required: ['id', 'severity', 'title', 'failure_scenario', 'proof_test', 'confirmed'], additionalProperties: false } }, summary: { type: 'string' } }, required: ['findings', 'summary'], additionalProperties: false }
const review = await agent(
`Adversarially review the alert/notify-test change: cd ${REPO} && git diff ${BASE} and new files (git status --porcelain). Implementer report: ${JSON.stringify(impl).slice(0, 2500)}. Find: alert spam (every cycle, after restart, flapping regime), missed alerts, alerts sent before state save, exceptions that can escape into the cycle, token leaks in notify-test output/logs/exceptions, notify-test needing an API key, Korean messages missing key info or exceeding Telegram limits, persistence files written non-atomically. Prove each with a failing test in ${REPO}/tests/test_alerts_review.py; confirmed=true only if it fails now. Do not fix. ${COMMON}`,
  { label: 'review', phase: 'Review', schema: REVIEW, effort: 'high' })

phase('Fix')
const fix = await agent(
`Fix the confirmed findings ${JSON.stringify(review ? review.findings.filter(f => f.confirmed) : [])} so tests/test_alerts_review.py passes (change a review test only if it contradicts SPEC/DESIGN, and explain). Then run the FULL suite with --junitxml until 0 failures and run the offline e2e (conservative, approve, second approve cycle) to confirm exit 0 and no traceback. Report junit totals in 'tests'. ${COMMON}`,
  { label: 'fix', phase: 'Fix', schema: RESULT, effort: 'high' })
return { impl, review, fix }
