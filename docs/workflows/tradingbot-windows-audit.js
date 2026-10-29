export const meta = {
  name: 'windows-runtime-audit',
  description: 'Find and adversarially verify Windows-only production risks in the trading bot (read-only)',
  phases: [
    { title: 'Find', detail: '5 lenses: file sharing, console/lifecycle, config/paths, network/time, dashboard' },
    { title: 'Verify', detail: 'one adversarial verifier per finding' },
    { title: 'Critic', detail: 'completeness critic + verification of its new findings' },
  ],
}

const SCRATCH = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/winaudit'

const CTX = [
  'Repository: /home/user/Trading_Bot (Python package tradingbot/, tests in tests/, README.md in Korean, SPEC.md, DESIGN.md).',
  'It is a Binance spot (USDT, long-only) multi-agent trading bot driven by the Claude API. Paper trading is the default; live needs MODE=live + CONFIRM_LIVE=YES + typing LIVE on a real TTY.',
  'Stop-loss / take-profit / time-stop are enforced by the bot\'s own code at the start of every cycle (there are no exchange-side stop orders), so any period in which the bot is frozen or halted leaves open positions unprotected.',
  '',
  'The user runs it on a Windows 10/11 home PC (Korean locale: ANSI/OEM code page cp949) in the folder B:\\TradingBot with Python 3.11-3.13, started from a console (cmd or Windows PowerShell 5.1) as "python -m tradingbot.main run", usually with the local dashboard as a SECOND process ("python -m tradingbot.main dashboard", FastAPI/uvicorn on 127.0.0.1) and occasional CLI calls ("status", "stop", "clear", "notify-test"). Windows Defender real-time protection is on. The folder might be inside a synced/backed-up location.',
  '',
  'Important code facts:',
  '- tradingbot/main.py run_loop(): any OSError (or CorruptStateError, StateSaveError, LockLostError, ConfigError) escaping comp.cycle.run_once() is FATAL: the bot sends an error notification and EXITS. Other exceptions are logged and the loop continues with the next cycle.',
  '- Atomic writes use a tmp file + os.replace in many modules (portfolio.save retries 5 x 0.1 s; most other sites do not retry).',
  '- BotLock (tradingbot/main.py) writes bot.lock with pid/host/ts/started_ts, refreshes ts during waits (_wait_with_lock_refresh, slices of LOCK_REFRESH_S) and checks liveness with _pid_alive (Windows branch: ctypes OpenProcess + GetExitCodeProcess).',
  '- Already fixed for Windows (do not re-report): os.kill(pid, 0) sending CTRL_C_EVENT; UTF-8 reconfigure of redirected stdout/stderr (main.prepare_stdio); CRLF normalisation of prompt files; exact-case SKILL.md matching; POSIX 0600 permission checks skipped on Windows. Telegram messages use no parse_mode. ccxt is created with options.adjustForTimeDifference=True. No zoneinfo is used (all UTC).',
  '- CI runs the full test-suite on windows-latest (English locale) for py3.11/3.13 and passes, so look for PRODUCTION behaviour the tests do not exercise.',
  '',
  'Rules: this is a READ-ONLY phase for the repository: do not edit, create or delete anything under /home/user/Trading_Bot. You may write throwaway experiment scripts under ' + SCRATCH + '/ and run them. This machine is Linux: simulate Windows behaviour by monkeypatching (e.g. make os.replace raise PermissionError(13, "Access is denied", None, 5) - the 4th argument populates .winerror). If network access works you may fetch primary sources such as https://raw.githubusercontent.com/python/cpython/3.13/Python/thread_nt.h, Python/parking_lot.c, Modules/posixmodule.c, Python/fileutils.c, or installed packages\' sources in site-packages. Never print or copy secrets (API keys, tokens).',
  'Report only REAL defects with a concrete, realistic trigger on the user\'s PC and a consequence you traced through the actual code. Not style, not hypotheticals that the code already handles, not issues that only matter on multi-user servers. Be precise about Windows/CPython semantics: name the Win32 API or CPython source behaviour and the Python versions it applies to; if you are unsure a semantic holds, say so and lower your confidence.',
].join('\n')

const FINDING = {
  type: 'object',
  properties: {
    id: { type: 'string', description: 'short kebab-case slug' },
    title: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    locations: { type: 'array', items: { type: 'string' }, description: 'repo-relative path:line' },
    windows_mechanism: { type: 'string' },
    trigger: { type: 'string' },
    consequence: { type: 'string', description: 'traced through the real code' },
    fix: { type: 'string', description: 'minimal, proportionate fix that is also safe on Linux' },
    test_plan: { type: 'string', description: 'how to test: Linux monkeypatch test and, if possible, a real reproduction that runs only on the Windows CI runner' },
    confidence: { type: 'number' },
  },
  required: ['id', 'title', 'severity', 'locations', 'windows_mechanism', 'trigger', 'consequence', 'fix', 'test_plan', 'confidence'],
}
const FINDINGS = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: FINDING },
    covered: { type: 'array', items: { type: 'string' }, description: 'specific things you checked and found OK, one line each with path:line' },
  },
  required: ['findings', 'covered'],
}
const VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'partially_confirmed', 'refuted'] },
    reasoning: { type: 'string' },
    mechanism_correct: { type: 'boolean' },
    trigger_realistic: { type: 'boolean' },
    actual_consequence: { type: 'string', description: 'what really happens, traced through the code' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    recommended_fix: { type: 'string', description: 'proportionate fix, or "none"' },
    test_plan: { type: 'string' },
    evidence: { type: 'array', items: { type: 'string' }, description: 'path:line refs, experiment results, primary-source citations' },
  },
  required: ['verdict', 'reasoning', 'mechanism_correct', 'trigger_realistic', 'actual_consequence', 'severity', 'recommended_fix', 'test_plan', 'evidence'],
}

const LENSES = [
  {
    key: 'file-sharing',
    focus: [
      'Cross-process file access and atomic writes. Enumerate EVERY place in tradingbot/** that writes, replaces, renames or deletes files (os.replace, Path.replace, os.rename, os.unlink, Path.unlink, mkstemp+replace, quarantine renames, open(..., "a") appends, lock and control files).',
      'For each site: which other processes may hold that path (or the tmp source) open at the same moment on the user\'s PC - the dashboard process serving its API (find exactly which files each endpoint reads and how), the status/stop/clear CLI, Defender scanning a freshly written tmp file, the Search indexer, a sync/backup client.',
      'Windows semantics to check: os.replace -> MoveFileExW(MOVEFILE_REPLACE_EXISTING) fails with ERROR_ACCESS_DENIED (WinError 5) or ERROR_SHARING_VIOLATION (WinError 32) while the destination or the source is open by a handle that lacks FILE_SHARE_DELETE; CPython open() never requests FILE_SHARE_DELETE; DeleteFileW fails likewise. Verify these claims.',
      'Then trace precisely how the resulting PermissionError propagates in THIS code: retried, swallowed with a warning, or escaping cycle.run_once() into run_loop where any OSError halts the bot. Also check the lock file paths (BotLock._write / refresh / release / acquire, including refresh failures during a cycle vs during waits) and the STOP/CLEAR control files written by the dashboard and read+deleted by the bot.',
      'Consider also: readers on Windows during a replace (partial reads?), JSONL appends by one process while another reads, quarantine renames of corrupt files.',
    ].join('\n'),
  },
  {
    key: 'console-lifecycle',
    focus: [
      'How the long-running "run" process behaves in a Windows console.',
      '(a) Ctrl+C responsiveness: on Windows, CPython lock acquisition with a timeout (threading.Event.wait / Lock.acquire) is reportedly not interruptible by Ctrl+C (3.11/3.12 Python/thread_nt.h PyThread_acquire_lock_timed: "intr_flag does nothing on Windows"; 3.13 PyMutex/parking-lot _PySemaphore_PlatformWait using WaitForSingleObjectEx without the SIGINT event); the Python-level SIGINT handler then runs only when the wait returns. Verify against primary sources, note whether/when this changed (3.14?), and trace run_loop/_wait_with_lock_refresh (slices of LOCK_REFRESH_S), notifier.flush and any other main-thread waits: what does the user experience after pressing Ctrl+C, and what do they likely do next?',
      '(b) Ctrl+Break (SIGBREAK) and closing the console window (CTRL_CLOSE_EVENT: process terminated about 5 s later, Python signal handlers are not invoked), logoff/shutdown, Windows Update restarts: what state is left behind (lock file, missing shutdown notification, in-flight orders) and is recovery on the next start correct (stale lock with a dead PID; Windows PID reuse is frequent - does the ts-staleness rule cover a reused PID?).',
      '(c) Console QuickEdit mode: when the user clicks or selects text in the console window, writes to the console screen buffer block until the selection ends. Logging goes to stderr from the main thread, so does a click freeze the whole cycle (including code-enforced stops) indefinitely? Does the code mitigate it (e.g. clearing ENABLE_QUICK_EDIT_MODE with SetConsoleMode on the stdin console handle)? What would a minimal safe mitigation be (only for a real console, never failing startup)?',
      '(d) Threads at shutdown on Windows (Telegram worker thread: daemon? join timeouts? queued messages lost?).',
      '(e) PC sleep/hibernate during a wait: effects on cycle timing, lock ts, heartbeat, daily rollover.',
    ].join('\n'),
  },
  {
    key: 'config-env-paths',
    focus: [
      'Configuration and filesystem on the user\'s Windows PC (Korean locale cp949, folder B:\\TradingBot).',
      '(a) .env and DATA_DIR are resolved relative to the current working directory (tradingbot/config.py read_env uses Path(".env"), DATA_DIR defaults to "."). What happens when the bot is started from another directory, by double-click, from a .bat without "cd /d", or by Task Scheduler (default working directory C:\\Windows\\System32)? Is there a silent fallback to defaults that could mislead the user (believing Telegram/keys/mode are configured when they are not), or writes into System32?',
      '(b) .env encoding: what does the INSTALLED python-dotenv version do with a UTF-8 BOM (Notepad "UTF-8 with BOM") or an ANSI/cp949-saved .env containing Korean comments - BOM glued to the first key, UnicodeDecodeError, silently missing values? Read its source in site-packages and trace tradingbot/config.py read_env / load_settings and their callers.',
      '(c) Windows paths in .env (DATA_DIR=B:\\TradingBot\\data unquoted, single-quoted, double-quoted; python-dotenv escape handling inside double quotes).',
      '(d) File names derived from symbols, timestamps, model names or user input (invalid Windows characters <>:"/\\|?*, trailing dots/spaces, reserved names CON/PRN/AUX/NUL/COM1/LPT1, case-insensitive collisions, MAX_PATH 260) across reports, journal, backtest, cycles, news and scanner stores.',
      '(e) README.md Windows instructions: venv activation, "copy .env.example .env", and the PowerShell log redirection "python -m tradingbot.main run *>> bot.out". In Windows PowerShell 5.1 native-command output is decoded with [Console]::OutputEncoding (the OEM code page, cp949 on Korean Windows) while prepare_stdio makes Python write UTF-8 when stdout is not a TTY, and 5.1 redirection writes UTF-16LE: is the resulting log garbled? Check every documented Windows command and suggest the correct one (e.g. cmd /c redirection, setting [Console]::OutputEncoding, or a built-in UTF-8 log file option).',
      '(f) Anywhere a Windows path or a localized OS error text flows into Telegram, the dashboard or LLM payloads in a way that breaks formatting or leaks more than intended.',
    ].join('\n'),
  },
  {
    key: 'network-time-exceptions',
    focus: [
      'Network, exchange and time behaviour on a home Windows PC, and exception classification.',
      '(a) EXCEPTION CLASSIFICATION (platform independent but critical): run_loop treats ANY OSError escaping cycle.run_once() as fatal. requests.exceptions.RequestException subclasses IOError (= OSError); raw socket errors (ConnectionResetError WinError 10054 after sleep, TimeoutError) are OSError subclasses. Find every network call reachable from cycle.run_once() (exchange/ccxt, news/RSS, sentiment sources such as fear & greed, Anthropic SDK, anything using requests/urllib/httpx/socket) and determine whether a transient network failure can escape as an OSError and halt the bot. Check ccxt\'s sync implementation in site-packages for which requests exceptions it wraps and which can leak. Verify with monkeypatched experiments if possible.',
      '(b) Clock drift vs Binance signed requests: is adjustForTimeDifference effective on every signed path; does ccxt refresh the offset periodically or only at load time; what happens over weeks of uptime or after the Windows clock is corrected; recvWindow.',
      '(c) PC sleep/resume: stale keep-alive connections in requests sessions / ccxt / httpx (Anthropic client), first calls after resume.',
      '(d) Windows system proxy (urllib reads the registry), certificates, anything else Windows-specific in network setup.',
      'Focus on paper (default) and testnet/live correctness, and trace what each failure does to the cycle.',
    ].join('\n'),
  },
  {
    key: 'dashboard-windows',
    focus: [
      'The local dashboard (tradingbot/dashboard/app.py, tradingbot/dashboard/static/index.html and the CLI command that starts it) running as a second process on Windows next to "run".',
      'Check: uvicorn/asyncio on Windows (Proactor loop, add_signal_handler unsupported, Ctrl+C handling, reload off), binding and Host-header checks when "localhost" resolves to ::1 first on Windows, the token file location and protection now that POSIX permissions are skipped on Windows (folder ACLs on a non-system drive like B:\\ often grant Authenticated Users modify - is this worth anything on a single-user home PC?), STOP/CLEAR control-file writes vs the bot\'s reads and deletes, reading large JSONL tails while the bot appends, serving paths with backslashes, any browser auto-open, and whether any dashboard file access can make the BOT fail (sharing violations caused by the dashboard holding files open).',
      'Report only real Windows-specific defects.',
    ].join('\n'),
  },
]

function finderPrompt(lens) {
  return CTX + '\n\nYOUR LENS: ' + lens.key + '\n' + lens.focus +
    '\n\nMethod: read the relevant code end to end (not excerpts), trace real call paths, run small experiments where that settles a question. ' +
    'Return every real defect you find (an empty list is fine if there are none) plus a "covered" list of the specific things you checked and found OK.'
}

function verifyPrompt(f) {
  return CTX + '\n\nYou are an ADVERSARIAL VERIFIER. A reviewer claims the following Windows-related defect. Try hard to REFUTE it:\n' +
    '1. Is the Windows/CPython mechanism exactly right for CPython 3.11-3.13 on Windows 10/11? Check primary sources if you can reach them.\n' +
    '2. Is the trigger realistic on the user\'s single-user home PC?\n' +
    '3. Trace the real code path yourself (do not trust the claim\'s trace): what actually happens? Run a monkeypatched experiment if it settles the question.\n' +
    '4. Is it already mitigated somewhere else in the code?\n' +
    '5. Is the proposed fix proportionate, minimal, and safe on Linux too? Propose a better one if not.\n' +
    'If the claim is partly right, mark partially_confirmed and correct it. Mark refuted if the mechanism is speculative or the consequence does not follow from the code.\n\nCLAIM:\n' +
    JSON.stringify(f, null, 2)
}

function criticPrompt(summary, covered) {
  return CTX + '\n\nYou are the COMPLETENESS CRITIC of a Windows production-readiness audit of this bot. Below are the findings so far (with verification verdicts) and the areas the finders checked and found OK. ' +
    'Identify Windows-specific production risks that are still UNEXAMINED (a modality not run, a module not read, a scenario not traced), check them in the code yourself, and return ONLY NEW real defects (not duplicates of the list). Return an empty list if the audit is complete. Put what you additionally checked and found OK in "covered".\n\n' +
    'FINDINGS SO FAR:\n' + summary + '\n\nCHECKED AND FOUND OK:\n' + covered.join('\n')
}

phase('Find')
const results = await pipeline(
  LENSES,
  lens => agent(finderPrompt(lens), { label: 'find:' + lens.key, phase: 'Find', schema: FINDINGS }),
  (res, lens) => parallel(((res && res.findings) || []).map(f => () =>
    agent(verifyPrompt(f), { label: 'verify:' + f.id, phase: 'Verify', schema: VERDICT })
      .then(v => Object.assign({}, f, { lens: lens.key, verdict: v }))
  )).then(vs => ({ lens: lens.key, covered: (res && res.covered) || [], items: vs.filter(Boolean) })),
)

const done = results.filter(Boolean)
const items = done.flatMap(r => r.items)
const covered = done.flatMap(r => r.covered.map(c => r.lens + ': ' + c))
const survived = items.filter(i => i.verdict && i.verdict.verdict !== 'refuted')
log(items.length + ' findings verified, ' + survived.length + ' survived; lenses completed: ' + done.length + '/' + LENSES.length)

phase('Critic')
const summary = items.map(i => '- [' + (i.verdict ? i.verdict.verdict : 'unverified') + '] ' + i.title + ' (' + (i.locations || []).join(', ') + ')').join('\n')
const critic = await agent(criticPrompt(summary, covered), { label: 'critic', phase: 'Critic', schema: FINDINGS })
const extra = (await parallel(((critic && critic.findings) || []).map(f => () =>
  agent(verifyPrompt(f), { label: 'verify:' + f.id, phase: 'Critic', schema: VERDICT })
    .then(v => Object.assign({}, f, { lens: 'critic', verdict: v }))
))).filter(Boolean)
log('critic added ' + extra.length + ' findings')

return {
  items: items.concat(extra),
  covered: covered.concat(((critic && critic.covered) || []).map(c => 'critic: ' + c)),
}
