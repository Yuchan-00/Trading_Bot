export const meta = {
  name: 'windows-fix-review',
  description: 'Adversarial review of the Windows runtime fix diff (read-only), with per-finding refutation',
  phases: [
    { title: 'Review', detail: '5 lenses over the diff 7d925ba..winfix-wip' },
    { title: 'Verify', detail: 'one adversarial verifier per finding' },
  ],
}

const WT = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/winfix'
const SCRATCH = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review'
const PY = '/home/user/Trading_Bot/.venv/bin/python'

const CTX = [
  'You are reviewing a change set in a git worktree at ' + WT + ' (branch winfix-wip). The base is commit 7d925ba; see the change with: git -C ' + WT + ' diff 7d925ba..winfix-wip (and git -C ' + WT + ' log --oneline 7d925ba..winfix-wip for the commit messages, which describe intent).',
  'Project: a Binance spot (USDT, long-only) multi-agent trading bot driven by the Claude API (Python package tradingbot/, tests in tests/, docs README.md (Korean) and SPEC.md — the newest SPEC section "Windows 운영 점검 반영(WINRT)" describes the intended contract of this change). Paper trading is the default. Stop-loss/take-profit/time-stop are enforced by the bot code each cycle (no exchange-side stops), so anything that freezes or halts the bot, or delays exits, leaves positions unprotected. The user runs it on a Korean-locale Windows 10/11 PC with Python 3.11-3.13 from cmd/PowerShell, often with the dashboard as a second process.',
  'The change fixes Windows-only production risks found by an audit: file sharing violations on os.replace/unlink/append (new tradingbot/fsutil.py), console QuickEdit freeze (logging via a queue + pump thread), idle sleep (SetThreadExecutionState), Ctrl+C latency (1 s wait slices), post-sleep waits, lock refresh/PID reuse, control files, .env encoding/parse errors/DATA_DIR validation, SCANNER_* from .env, offline fixture without API key, live+mock refusal, Binance -1021 resync and clock-step-safe throttle, and moving LLM post-trade reviews/daily review/news fetch after code exits.',
  'Rules: READ-ONLY for the worktree and for /home/user/Trading_Bot — do not edit, create or delete files there. Put any experiment scripts under ' + SCRATCH + '/ and run them with PYTHONDONTWRITEBYTECODE=1 ' + PY + ' (the venv has all dependencies; a Python 3.11 venv is at /tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/venv311/bin/python). You may run the test-suite read-only: cd ' + WT + ' && PYTHONDONTWRITEBYTECODE=1 ' + PY + ' -m pytest -q -p no:cacheprovider tests/<file>. The full suite currently passes (3957 tests, 2 Windows-only skipped on Linux).',
  'Report only REAL defects introduced or left unfixed by this change: wrong behaviour, regressions against the previous behaviour or the tests, safety problems (e.g. a path where two bot instances could trade at once, a secret value leaking into logs/messages, a fix that does not actually work on Windows), data loss/double counting, crash paths, or tests that do not test what they claim / would be flaky or wrong on the windows-latest CI runner. Give a concrete failure scenario with inputs/state. Not style, not nits, not hypothetical refactors.',
].join('\n')

const FINDING = {
  type: 'object',
  properties: {
    id: { type: 'string' },
    title: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    file: { type: 'string', description: 'repo-relative path' },
    line: { type: 'integer' },
    scenario: { type: 'string', description: 'concrete inputs/state -> wrong outcome' },
    evidence: { type: 'string', description: 'code refs, experiment output' },
    fix: { type: 'string', description: 'minimal fix' },
    confidence: { type: 'number' },
  },
  required: ['id', 'title', 'severity', 'file', 'line', 'scenario', 'evidence', 'fix', 'confidence'],
}
const FINDINGS = {
  type: 'object',
  properties: { findings: { type: 'array', items: FINDING } },
  required: ['findings'],
}
const VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'partially_confirmed', 'refuted'] },
    reasoning: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    reproduction: { type: 'string', description: 'what you ran and what happened, or why no reproduction is possible' },
    recommended_fix: { type: 'string' },
  },
  required: ['verdict', 'reasoning', 'severity', 'reproduction', 'recommended_fix'],
}

const LENSES = [
  { key: 'main-runtime', focus: 'tradingbot/main.py: BotLock (acquire/refresh/release, pid_reused, _windows_process_identity ctypes signatures and FILETIME math, took_over), _poll_wait/_wait_with_lock_refresh (deadlines, settle path, interaction with injected waits/clocks, LockLostError propagation, lock refresh cadence), run_loop cycle_gap alert, keep_system_awake (thread affinity, return-value semantics), _LogPump/configure_logging/attach_log_file/detach_log_file (lost records, hangs at exit, handler leaks, double output, levels), install_signal_handlers SIGBREAK, _cmd_run (order of LIVE prompt / lock / log file / finally), _cmd_control/_describe_lock/write_stop/write_clear, main() offline-fixture/--mock-scenario handling, offline_fixture_settings, _startup_recovery_note, build_components live+mock guard and scanner thresholds.' },
  { key: 'config-env', focus: 'tradingbot/config.py: _decode_dotenv/_dotenv_items/read_env(lenient, warnings)/parse_data_dir/_log_file/load_settings changes and the new Settings fields. Look especially for valid .env files that worked before and are now rejected or parsed differently (e.g. values with spaces, quotes, ${VAR} interpolation, export lines, comments, CRLF, empty values, keys only in os.environ), warnings that could leak secret values, the env-override warning dedupe, behaviour of the dotenv logger suppression, and every caller of read_env/load_settings in main.py (strict vs lenient, ConfigError handling paths for each CLI command).' },
  { key: 'cycle-ledger-fsutil', focus: 'tradingbot/cycle.py (run_once stage order, _book_sell/_drain_reviews/pending_reviews, _can_defer_news/_fetch_news_after_exits, _maybe_daily_review first-run rule, _process_control older-STOP rule, _unlink_control), tradingbot/scanner.py include_news/fetch_news, tradingbot/llm/cost.py pending ledger file (restore, double counting, both-files-locked, status/dashboard readers), tradingbot/fsutil.py (retry/replace/unlink/append_bytes semantics, budget math, partial writes on retry), tradingbot/portfolio.py save retry changes, reports/journal/notify JSONL appends, dashboard/app.py kill endpoints and cost summary.' },
  { key: 'exchange', focus: 'tradingbot/exchange.py: _configure_exchange fetchCurrencies/fetchMargins=False (any side effect on markets, precision, limits, fees, currency codes, balances, testnet sandbox, load_markets in installed ccxt 4.5.90 — read its source in the venv site-packages), _resync_time and the InvalidNonce paths in _retry and _submit (retry counts, duplicate orders, infinite loops, interaction with RateLimitExceeded/ExchangeNotAvailable), _install_step_safe_throttle on the real ccxt sync binance/binanceusdm (signature, where throttle is called from, attribute names, idempotence, any async client).' },
  { key: 'tests-docs', focus: 'tests/test_windows_runtime.py and the README.md / .env.example / SPEC.md changes: do the tests really exercise the fixed behaviour (could they pass with the bug present?), are any of them flaky, order-dependent or wrong on the windows-latest GitHub runner (Python 3.11/3.13, English locale, cmd/pwsh) — e.g. signal tests, open-handle replace test, os.name monkeypatching, timing thresholds, global logging state, file handles left open preventing tmp cleanup, environment variables leaking between tests; and are the README/.env.example/SPEC statements accurate for the code (commands that would not work as written on Windows or Linux, wrong file names, wrong defaults).' },
]

function reviewPrompt(lens) {
  return CTX + '\n\nYOUR LENS: ' + lens.key + '\n' + lens.focus +
    '\n\nRead the diff and the surrounding code end to end for your lens; run small experiments or targeted tests where that settles a question. Return every real defect you find (an empty list is fine).'
}

function verifyPrompt(f) {
  return CTX + '\n\nYou are an ADVERSARIAL VERIFIER. A reviewer claims the defect below in this change set. Try hard to REFUTE it: re-read the code yourself, and reproduce it with an experiment script (preferred) or explain precisely why a reproduction is impossible. Mark refuted if the scenario cannot happen with the real code paths or the claim misreads the code; partially_confirmed if real but overstated (correct the severity and the fix). Keep the recommended fix minimal and safe on both Linux and Windows.\n\nCLAIM:\n' + JSON.stringify(f, null, 2)
}

phase('Review')
const results = await pipeline(
  LENSES,
  lens => agent(reviewPrompt(lens), { label: 'review:' + lens.key, phase: 'Review', schema: FINDINGS }),
  (res, lens) => parallel(((res && res.findings) || []).map(f => () =>
    agent(verifyPrompt(f), { label: 'verify:' + f.id, phase: 'Verify', schema: VERDICT })
      .then(v => Object.assign({}, f, { lens: lens.key, verdict: v }))
  )).then(vs => vs.filter(Boolean)),
)
const items = results.filter(Boolean).flat()
const kept = items.filter(i => i.verdict && i.verdict.verdict !== 'refuted')
log(items.length + ' findings, ' + kept.length + ' not refuted')
return { items }
