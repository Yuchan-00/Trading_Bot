export const meta = {
  name: 'windows-fix-review-round3',
  description: 'Third adversarial review (read-only) of the round-2 fixes 548b2a3..0d98457, with per-finding refutation',
  phases: [
    { title: 'Review', detail: '3 lenses over 548b2a3..0d98457' },
    { title: 'Verify', detail: 'one adversarial verifier per finding' },
  ],
}

const WT = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review3_wt'
const SCRATCH = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review3'
const PY = '/home/user/Trading_Bot/.venv/bin/python'
const PY311 = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/venv311/bin/python'

const CTX = [
  'You are reviewing a change set in a detached git worktree at ' + WT + ' (HEAD 0d98457). The change under review is: git -C ' + WT + ' diff 548b2a3..0d98457 (commit messages: git -C ' + WT + ' log 548b2a3..0d98457). These are the newest fixes, made in response to a second adversarial review round; they have had no review yet. Earlier rounds already reviewed everything up to 548b2a3 — focus on what these commits changed and on how they interact with the surrounding code.',
  'Project: a Binance spot (USDT, long-only) multi-agent trading bot driven by the Claude API (package tradingbot/, tests/, Korean README.md, SPEC.md whose section "Windows 운영 점검 반영(WINRT)" is the intended contract). Paper is the default. Stop-loss/take-profit/time-stop are enforced by the bot each cycle, so anything that halts the bot, refuses a valid configuration, or flips the kill switch wrongly matters. The user runs it on a Korean-locale Windows PC (B:\\TradingBot) with Python 3.11-3.13, sometimes with the dashboard as a second process.',
  'What these commits changed: (a) config.py: _visible() strips Unicode Cf characters and Hangul fillers; lines whose key is only invisible characters are ignored; keys containing invisible characters are cleaned (with a warning); UTF-16 files containing U+0A00-0AFF/U+0D00-0DFF characters, or a big-endian file whose last character has low byte 0x0A, are treated as mixed-encoding (strict ConfigError with a line number, lenient warning); a strict UTF-16 decode failure now reports the first appended line number; parse-error advice distinguishes lowercase export (quote advice) from set/SET/EXPORT prefixes; only uppercase NAME values (<=40 chars) are ever printed. (b) cycle.py: read_control_file marks OSError reads as unreadable; _process_control skips an unreadable CLEAR for that cycle, pops state.control_applied_ts[kind] once the file is gone, logs a warning when skipping an already-applied leftover; _reconcile_journal skips trades already present in the journal.jsonl tail (key: symbol, opened_ts, closed_ts, exit_order_id, base_amount). (c) portfolio._dump_state omits an empty control_applied_ts. (d) main._status_text and dashboard/app.py (_control_ts, _already_applied, _stop_info, _clear_pending) report an already-applied leftover control file as handled rather than pending.',
  'Rules: READ-ONLY for ' + WT + ', /home/user/Trading_Bot and any other worktree. Put experiment scripts under ' + SCRATCH + '/ and run them with PYTHONDONTWRITEBYTECODE=1 ' + PY + ' (3.13; 3.11 at ' + PY311 + '), with ' + WT + ' first on sys.path. You may run tests read-only: cd ' + WT + ' && PYTHONDONTWRITEBYTECODE=1 ' + PY + ' -m pytest -q -p no:cacheprovider tests/<file>. The full suite passes (3994 tests; 3992 pass, 2 Windows-only skipped on Linux). Never use real exchanges or real keys.',
  'Report only REAL defects in these commits: wrong behaviour, regressions, valid real-world configurations newly rejected or silently changed, secret text reaching messages, kill-switch/control-file logic that can apply a request twice, drop a genuine new request, or re-apply an old one, journal lines lost or duplicated, status/dashboard showing something false, or tests that do not test what they claim / would be flaky on windows-latest. Give a concrete failure scenario. Not style, not nits.',
].join('\n')

const FINDING = {
  type: 'object',
  properties: {
    id: { type: 'string' }, title: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    file: { type: 'string' }, line: { type: 'integer' },
    scenario: { type: 'string' }, evidence: { type: 'string' }, fix: { type: 'string' }, confidence: { type: 'number' },
  },
  required: ['id', 'title', 'severity', 'file', 'line', 'scenario', 'evidence', 'fix', 'confidence'],
}
const FINDINGS = { type: 'object', properties: { findings: { type: 'array', items: FINDING } }, required: ['findings'] }
const VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'partially_confirmed', 'refuted'] },
    reasoning: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    reproduction: { type: 'string' }, recommended_fix: { type: 'string' },
  },
  required: ['verdict', 'reasoning', 'severity', 'reproduction', 'recommended_fix'],
}

const LENSES = [
  { key: 'config-env', focus: 'config.py changes (_visible, _INVISIBLE_EXTRA, _MIXED_UTF16 and the big-endian last-character rule, the UTF-16 decode-failure line number, _DOTENV_ASSIGN/_SHOWN_NAME message selection, key cleaning after dotenv_values). Try hard to find valid real-world .env files now rejected or read differently: UTF-16 files written by Windows tools (PowerShell 5.1 Out-File/Set-Content -Encoding Unicode / BigEndianUnicode, Notepad "UTF-16 LE/BE") containing Korean, Chinese, Japanese, emoji (surrogate pairs), symbols like — · … ※ ★, Indic text, a final line with or without newline; UTF-8 files with emoji/ZWJ sequences (U+200D is Cf!) in comments or values; values (not keys) containing invisible characters; interpolation ${VAR}; and any path where a secret or part of one can appear in a message. Compare with python-dotenv 1.0.x behaviour where relevant.' },
  { key: 'control-cycle', focus: 'cycle.py read_control_file unreadable marker, _process_control (unreadable CLEAR skip, control_applied_ts pop on absence, skip-path warning, interaction with the older-STOP rule and with _consumed_control, ordering of reads, a STOP and CLEAR both present, files appearing/disappearing between cycles, restarts, the dashboard/CLI writers write_stop/write_clear and /api/kill, Windows delete-pending files and sharing modes), _reconcile_journal dedupe and _journal_has_trade (tail window boundaries cutting a line, huge journals, corrupt lines, float equality of base_amount after JSON round-trip, exit_order_id None, partial exits, the abort path in run_once/_journal_pending_on_abort).' },
  { key: 'status-dashboard-dump', focus: 'portfolio._dump_state omission of empty control_applied_ts (determinism, round-trip, any reader that expects the key, test helpers that build state.json themselves), main._status_text and dashboard/app.py _control_ts/_already_applied/_stop_info/_clear_pending (ts derivation parity with cycle.read_control_file incl. non-JSON and unreadable files, extra state reads per request, behaviour when state.json is missing/corrupt, /api/state and /api/health fields consumed by dashboard/static/index.html), and the new/changed tests in tests/test_windows_runtime.py for these (do they really fail without the fix; Windows-runner flakiness: Path.read_text monkeypatching, TestClient usage, temp file handles).' },
]

function reviewPrompt(lens) {
  return CTX + '\n\nYOUR LENS: ' + lens.key + '\n' + lens.focus + '\n\nRead the diff and surrounding code end to end for your lens; run small experiments where that settles a question. Return every real defect you find (an empty list is fine).'
}
function verifyPrompt(f) {
  return CTX + '\n\nYou are an ADVERSARIAL VERIFIER. A reviewer claims the defect below. Try hard to REFUTE it: re-read the code and reproduce it with an experiment script, or explain precisely why it cannot happen. Mark refuted if it cannot happen with the real code paths; partially_confirmed if real but overstated (correct the severity and the fix). Keep the recommended fix minimal and safe on both Linux and Windows.\n\nCLAIM:\n' + JSON.stringify(f, null, 2)
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
log(items.length + ' findings, ' + items.filter(i => i.verdict && i.verdict.verdict !== 'refuted').length + ' not refuted')
return { items }
