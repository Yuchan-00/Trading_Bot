export const meta = {
  name: 'windows-fix-review-round2',
  description: 'Second adversarial review (read-only) of the follow-up fixes 7db6134..548b2a3, with per-finding refutation',
  phases: [
    { title: 'Review', detail: '5 lenses over the follow-up diff 7db6134..548b2a3' },
    { title: 'Verify', detail: 'one adversarial verifier per finding' },
  ],
}

const WT = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review2_wt'
const SCRATCH = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review2'
const PY = '/home/user/Trading_Bot/.venv/bin/python'
const PY311 = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/venv311/bin/python'

const CTX = [
  'You are reviewing a change set in a detached git worktree at ' + WT + ' (HEAD 548b2a3). The change under review is: git -C ' + WT + ' diff 7db6134..548b2a3 (read the commit messages with git -C ' + WT + ' log 7db6134..548b2a3 — they describe intent). These commits are FOLLOW-UP FIXES made in response to a previous adversarial review round of an earlier Windows hardening change; they are the newest code and have had no review yet.',
  'Project: a Binance spot (USDT, long-only) multi-agent trading bot driven by the Claude API (Python package tradingbot/, tests in tests/, docs README.md (Korean) and SPEC.md — the SPEC section "Windows 운영 점검 반영(WINRT)" describes the intended contract). Paper trading is the default. Stop-loss/take-profit/time-stop are enforced by the bot code each cycle (no exchange-side stops), so anything that freezes or halts the bot, or delays exits, leaves positions unprotected. The user runs it on a Korean-locale Windows 10/11 PC (bot folder B:\\TradingBot) with Python 3.11-3.13 from cmd/PowerShell, often with the dashboard as a second process.',
  'What the follow-up commits changed (verify each actually works and introduces no regression): (1) config.py .env checks: parse-error messages show only an uppercase NAME= name (max 40 chars) or just the line number (no secret text); line numbers counted from preceding raw text (blank lines, CRLF once); a non-error binding whose key is non-ASCII with value None (garbled line from UTF-16 + appended 8-bit bytes) is a strict ConfigError / lenient warning; undecodable UTF-16-BOM file: strict ConfigError, lenient decode errors=replace + warning; a .env that is a directory counts as absent (FIFO still read); empty .env values are not counted in the env-override warning; notify-test maps ConfigError to exit 2. (2) main.py logging: LOG_FILE gets its own queue + pump thread (second QueueHandler on root) and detach_log_file removes the feed then stops (drains) that pump before closing; _LogPump.stop returns bool; cli() calls os._exit(code) after flushing a non-tty stdout when the console pump is stuck. (3) BotLock.refresh: a single failed ts write is fatal (LockLostError) when failing_s >= stale_after_s - (LOCK_REFRESH_S + CYCLE_DEADLINE_S). (4) BotState.control_applied_ts persisted; _process_control skips (only retries deletion of) a control file whose ts equals the persisted or in-memory consumed ts; all handled events record their ts before the save. (5) Cycle.run_once is now a wrapper: on ANY exception from _run_once it journals remaining pending_reviews with code summaries (no LLM/notification) then re-raises; _drain_reviews re-inserts the popped item on _FATAL. (6) anthropic_backend: deadline stored as (set_time, remaining) and remaining = r - elapsed; _create_bounded attempt timeout = budget_s - elapsed. (7) runner.run_many waits with concurrent.futures.wait in 1 s slices (WAIT_SLICE_S) so the main thread can run the Ctrl+C handler. (8) Docs/messages: README Ctrl+C timing, Windows ACL/icacls guidance for the whole bot folder, setx removal guidance, UTF-16 .env wording; config/main messages point to README §4.',
  'Rules: READ-ONLY for ' + WT + ', for /home/user/Trading_Bot and for every other worktree — do not edit, create or delete files there. Put any experiment scripts under ' + SCRATCH + '/ and run them with PYTHONDONTWRITEBYTECODE=1 ' + PY + ' (Python 3.13 venv with all dependencies; a Python 3.11 venv is ' + PY311 + '). Import the code under review by putting ' + WT + ' first on sys.path / PYTHONPATH. You may run tests read-only: cd ' + WT + ' && PYTHONDONTWRITEBYTECODE=1 ' + PY + ' -m pytest -q -p no:cacheprovider tests/<file>. The full suite passes (3985 tests; 3983 pass, 2 POSIX-only skipped... on Linux the 2 skipped are Windows-only). Never run anything against real exchanges or real API keys.',
  'Report only REAL defects introduced or left unfixed by these follow-up commits: wrong behaviour, regressions against the previous behaviour or the tests, safety problems (two bot instances trading at once, a kill switch flipping wrongly, a secret value reaching logs/messages/errors, a fix that does not actually work on Windows), data loss/double counting/duplicate journal lines, crash or hang paths, valid configurations now rejected, or tests that do not test what they claim / would be flaky or wrong on the windows-latest CI runner. Also report documentation statements in the changed README/SPEC/.env.example/WORKLOG text that are wrong or would not work as written (e.g. a Windows command). Give a concrete failure scenario with inputs/state. Not style, not nits, not hypothetical refactors.',
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
  { key: 'config-env', focus: 'tradingbot/config.py (_DOTENV_NAME, _NEWLINES, _UTF8_FIX, _dotenv_present, dotenv_location, _decode_dotenv UTF-16 lenient path, _dotenv_line_problem, _dotenv_items loop, read_env override rule, load_settings dotenv_path branch) and main.py notify_test/_lenient_env/_log_locations. Look for: valid real-world .env files (Notepad UTF-8 with/without BOM and CRLF, Korean comments, export lines, quoted values with spaces, ${VAR}, empty values, a lone non-ASCII word, NBSP/zero-width characters, lowercase keys) that previously loaded and are now rejected in strict mode; any remaining way secret text reaches an error/warning (including the NAME= capture); wrong line numbers (BOM, mixed newlines, lone CR, multi-line quoted values, errors after quoted multi-line values); lenient paths that can still raise; behaviour with python-dotenv 1.0.x (requirements allow >=1.0; check what parse_stream/Binding/Original look like there — the venv has 1.2.4, you may pip download 1.0.1 into ' + SCRATCH + ' to compare without installing into the venvs).' },
  { key: 'logging-exit', focus: 'tradingbot/main.py: _LogPump (stop returning bool), configure_logging, attach_log_file (per-file pump, QueueHandler feed on root, formatting/exc_info/stack_info, levels, records logged by the pump threads themselves, captureWarnings), detach_log_file (ordering, timeout, handler not closed when stuck, repeated attach/detach, file handle left open on Windows), _exit_past_blocked_console and cli() (os._exit skipping atexit/finalizers: does anything rely on atexit — notifier sender threads, uvicorn dashboard shutdown, ccxt sessions, buffered file writes like decisions/journal/cost ledger, the file pump queue; exit codes; KeyboardInterrupt or SystemExit raised from main(); behaviour when stdout is a console on Windows; when the console pump is merely slow rather than stuck). Check every command path through cli() (run, run --once, dashboard, backtest, status, stop, clear-stop, notify-test).' },
  { key: 'lock-control-cycle', focus: 'tradingbot/main.py BotLock.refresh near_stale rule (all cycle_minutes values incl. 1 and 60+, stale_after_s minimum 1800, refresh call sites in the cycle lock stage and _wait_with_lock_refresh, consecutive failure counter reset, interplay with wake/settle path); tradingbot/models.py BotState.control_applied_ts and tradingbot/cycle.py _process_control (old state.json without the field, dashboard/status readers, a genuinely new STOP/CLEAR carrying the same ts as an applied one, hand-written control files with mtime ts, the older-STOP rule, events when the save fails, kill switch correctness across restarts and across the dashboard/CLI writers in main.py write_stop/write_clear and dashboard/app.py); Cycle.run_once wrapper and _journal_pending_on_abort / _drain_reviews re-insert (duplicate journal lines, journal_state consistency vs _reconcile_journal after restart, exceptions inside the abort handler masking the original, partial exits, BaseException catch effects).' },
  { key: 'llm-runner', focus: 'tradingbot/llm/anthropic_backend.py set_deadline/_deadline_remaining/_create_bounded changes (retry path timeouts, budget exhaustion, negative or non-finite inputs, thread-safety, set_deadline(None) races with in-flight calls) and tradingbot/agents/runner.py run_many wait slices (result ordering, exception propagation, the stop/first_exc logic, executor shutdown, behaviour when called from a non-main thread, CPU/latency, interplay with cycle deadline and the pipeline in tradingbot/agents/pipeline.py). Check the new tests in tests/test_anthropic_backend.py and tests/test_windows_runtime.py for these.' },
  { key: 'tests-docs', focus: 'The tests added in 7db6134..548b2a3 (tests/test_windows_runtime.py, tests/test_anthropic_backend.py, tests/test_sysrev_secrets.py) and the README.md / SPEC.md / .env.example / docs/WORKLOG.md text changed in that range. For tests: could each pass with the bug present; flakiness or wrong behaviour on windows-latest (Python 3.11 / 3.13, cmd/pwsh, slow runners): the subprocess child with a blocking stderr, signal.raise_signal from a Timer thread, global logging state restoration (cli_logging), Path.open/os.replace/fsutil.os monkeypatching, elapsed-time thresholds, file handles kept open so tmp_path cleanup fails on Windows, tests that leave threads running. For docs: exact correctness of the Windows commands (icacls with SIDs and (OI)(CI) quoting in cmd vs PowerShell, [Environment]::SetEnvironmentVariable/GetEnvironmentVariable, reg delete, setx /M), the Ctrl+C timing claims vs the code, UTF-16 .env claims vs the code, test counts, and any SPEC statement that contradicts the code.' },
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
