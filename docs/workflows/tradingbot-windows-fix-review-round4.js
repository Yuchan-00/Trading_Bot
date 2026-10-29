export const meta = {
  name: 'windows-fix-review-round4',
  description: 'Fourth adversarial review (read-only) of the round-3 redesign 0d98457..6f8f5b8, with per-finding refutation',
  phases: [
    { title: 'Review', detail: '3 lenses over 0d98457..6f8f5b8' },
    { title: 'Verify', detail: 'one adversarial verifier per finding' },
  ],
}

const WT = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review4_wt'
const SCRATCH = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/review4'
const PY = '/home/user/Trading_Bot/.venv/bin/python'
const PY311 = '/tmp/claude-0/-home-user-Trading-Bot/392eacaf-b5ef-550a-b096-354bc117fdd4/scratchpad/venv311/bin/python'

const CTX = [
  'You are reviewing a change set in a detached git worktree at ' + WT + ' (HEAD 6f8f5b8). The change under review is: git -C ' + WT + ' diff 0d98457..6f8f5b8 (commit messages: git -C ' + WT + ' log 0d98457..6f8f5b8). It is a REDESIGN made after three earlier adversarial review rounds of a Windows hardening change; earlier rounds kept finding edge-case interactions in the control-file (STOP/CLEAR) logic, so this redesign replaced the patches. It has had no review yet.',
  'Project: a Binance spot (USDT, long-only) multi-agent trading bot driven by the Claude API (package tradingbot/, tests/, Korean README.md, SPEC.md — section "Windows 운영 점검 반영(WINRT)" is the intended contract; read its 제어 파일 and .env·설정 bullets). Paper is the default. Stop-loss/take-profit/time-stop are enforced by the bot each cycle; the kill switch only blocks new entries. The user runs it on a Korean-locale Windows PC (B:\\TradingBot), Python 3.11-3.13, sometimes with the dashboard as a second process; control files are written by `python -m tradingbot.main stop/clear-stop` (main.write_stop/write_clear: tmp + os.replace, ts = now ms) and by the dashboard POST /api/kill and /api/kill/clear.',
  'The redesign: (a) new module tradingbot/control.py — read_control_file returns {reason, ts, mtime_ms[, unreadable]} (ts = finite JSON ts incl. numeric strings, else mtime; FileNotFoundError during read = absent); is_handled/file_state decide pending/handled/unreadable using state.control_applied_ts entries kind and kind_mtime (mtime compared only when the ts cannot be compared). (b) cycle._process_control: handled leftovers are only re-deleted (warning); if any non-handled file is unreadable the whole control step is deferred to the next cycle, except a lone unreadable STOP with no pending CLEAR, which is applied; events sorted by ts with the older-STOP-than-CLEAR rule; applied records (ts + mtime) are written before _save() and dropped as soon as the file is deleted or found missing. The in-memory _consumed_control dict was removed. (c) main._status_text and dashboard (_control_state, _stop_info, _clear_pending, /api/health stop_state/clear_state) use the same functions. (d) config.py: UTF-16 BOM files are rejected as mixed-encoding when the file ends with LF and an ASCII NAME= byte run follows the last 0x00 byte, or when U+0A0D appears (old U+0A00-0AFF/U+0D00-0DFF and big-endian last-character rules removed); invisible characters (Unicode Cf plus Hangul fillers U+115F/U+1160/U+3164/U+FFA0) are removed from the whole decoded text before parsing, with a line-number warning for setting lines; the post-parse key cleaning was removed.',
  'Rules: READ-ONLY for ' + WT + ', /home/user/Trading_Bot and any other worktree. Put experiment scripts under ' + SCRATCH + '/ and run them with PYTHONDONTWRITEBYTECODE=1 ' + PY + ' (3.13; 3.11 at ' + PY311 + '), with ' + WT + ' first on sys.path. You may run tests read-only: cd ' + WT + ' && PYTHONDONTWRITEBYTECODE=1 ' + PY + ' -m pytest -q -p no:cacheprovider tests/<file>. The full suite passes (4011 tests; 4009 pass, 2 Windows-only skipped on Linux). To emulate Windows file locks, monkeypatch Path.read_text / fsutil.unlink / os.replace as the existing tests do. Never use real exchanges or real keys.',
  'Report only REAL defects in this redesign: a request applied twice, a genuine new request dropped, an old request re-applied, STOP/CLEAR order inverted, the drawdown-stop release broken, a deferral that can block a STOP forever in a realistic case, state.json written in a way older/newer readers cannot load, status/dashboard showing something false, valid .env files newly rejected or appended settings silently lost, secrets in messages, crashes (including in the dashboard request path), or tests that do not test what they claim / would be flaky on windows-latest. Give a concrete failure scenario. Not style, not nits.',
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
  { key: 'control', focus: 'tradingbot/control.py and cycle._process_control/_forget_applied/_unlink_control as a whole state machine. Enumerate sequences over cycles and restarts: STOP/CLEAR written by CLI or dashboard (with their own delete-the-other-file attempts that may fail), files that are undeletable and/or unreadable for one or many cycles (Windows sharing modes: no FILE_SHARE_DELETE, no FILE_SHARE_READ, delete-pending), the bot crashing between _save() and unlink, StateSaveError, kill switch set by drawdown/daily limits in between, mtime precision (NTFS 100ns, FAT32 2s, exFAT 10ms) and clock steps, two files with equal ts, hand-written non-JSON files, numeric-string ts. For each, check the final kill switch matches the user\'s latest request and nothing is applied twice or silently dropped; check the lone-unreadable-STOP exception and whether a persistent lock can block a newer STOP forever.' },
  { key: 'config', focus: 'config.py changes: _APPENDED_8BIT search after the last 0x00 with the endswith(b"\\n") requirement and the U+0A0D signature, the strict decode-failure line number, invisible-character pre-cleaning (_visible over the whole text, the changed-line warning, interaction with quoted multi-line values, ${VAR} interpolation, escapes, BOM, CRLF, line numbers in later parse-error messages, values that legitimately contain U+200D in emoji ZWJ sequences or U+FEFF), removal of the post-parse key cleaning, and python-dotenv 1.0.x compatibility (requirements allow >=1.0; you may pip download 1.0.1 into ' + SCRATCH + ' and import it from there without touching the venvs). Find valid real-world .env files newly rejected or read differently, appended settings (cmd echo >>, Git Bash, PowerShell Add-Content -Encoding ascii/utf8/oem) still silently lost, and any secret text in messages.' },
  { key: 'status-dashboard-tests', focus: 'main._status_text and dashboard/app.py (_control_state, _stop_info, _clear_pending, /api/health and /api/state fields, index.html consumers), parity with the cycle\'s decisions in every state, extra file reads per request, behaviour when state.json is missing/corrupt/old (no control_applied_ts or legacy int-only entries from the previous commit format), and all tests added or changed in 0d98457..6f8f5b8 (do they fail without the fix; would they be flaky or wrong on windows-latest, e.g. Path.read_text monkeypatching affecting pytest internals, TestClient, file handles, st_mtime_ns resolution, the _deny_read helper). Also check SPEC.md/README.md/docs/WORKLOG.md statements changed in this range against the code.' },
]

function reviewPrompt(lens) {
  return CTX + '\n\nYOUR LENS: ' + lens.key + '\n' + lens.focus + '\n\nRead the diff and surrounding code end to end for your lens; run experiments where that settles a question. Return every real defect you find (an empty list is fine).'
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
