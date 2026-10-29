export const meta = {
  name: 'tradingbot-foundation-v1',
  description: 'Implement, adversarially review and fix the SPEC v1 foundation modules of the trading bot, one pipeline per module, then integrate',
  phases: [
    { title: 'Implement', detail: 'one owner per module writes code + tests until green' },
    { title: 'Review', detail: 'adversarial reviewer proves defects with failing tests' },
    { title: 'Fix', detail: 'owner fixes confirmed defects' },
    { title: 'Integrate', detail: 'full suite, cross-module fixes' },
  ],
}

const REPO = args.repo
const PY = args.py

const COMMON = `
PROJECT: ${REPO} — Claude API 멀티에이전트 Binance 현물 트레이딩 봇 (Python >= 3.11). Read ${REPO}/SPEC.md (the contract; your module's section plus §0 common rules, §2 models, §1 config) and ${REPO}/tradingbot/models.py and ${REPO}/tradingbot/config.py before writing anything. Those two files are FROZEN: do not edit them; if the spec forces a change there, report it in your result instead and work around it locally.
RULES: match the SPEC signatures exactly (names, parameters, return types, exceptions). Korean docstrings/comments are welcome; identifiers in English. Python 3.11 compatible (no 'type X = ...' aliases, no 3.12-only syntax). Only standard library + the packages in requirements.txt. Everything must import and test with NO network and NO real API keys. Use logging.getLogger("tradingbot.<module>"), never print. Secrets must never appear in logs/exceptions/repr (use config.mask_secret). Deterministic JSON: json.dumps(..., sort_keys=True, ensure_ascii=False). Tests: pytest, offline, fast (no sleep > 0.2s total), in ${REPO}/tests/, run with: cd ${REPO} && ${PY} -m pytest <your test files> -q . Do NOT run the whole suite while other modules are being written concurrently (other agents are writing other files at the same time) — only your own test files. Do NOT git commit, do NOT touch files you do not own. If a dependency module you need is not written yet, write a minimal local test double in your tests instead of waiting.`

const MODULES = [
  { key: 'risk', own: ['tradingbot/risk.py', 'tests/test_risk.py'], spec: '§3 risk.py', notes: 'Pure functions only. Adversarial tests are the point: NaN/inf/negative/zero prices and equities, confidence exactly at 0.65/0.75/0.85 boundaries, stop distance clamped to 2% and TP to 4% for any input, net R:R math (round-trip cost = 2*(FEE_PCT+SLIPPAGE_PCT)) with the stop tightened only, rr_impossible, size never above 10% of equity nor free_quote, MIN_ORDER_USDT floor, drawdown ladder thresholds (3%/4.5%/6%) inclusive boundaries, cooldown/revenge gates, validate_order_request on whitelist/held/finite, check_exits ordering (stop before take-profit when both touched is the CALLER concern; here just classify by mark). global_gates must list every blocking reason in Korean and must never block exits (it only answers about new entries). Aim for 80+ tests.' },
  { key: 'indicators', own: ['tradingbot/indicators.py', 'tests/test_indicators.py'], spec: '§5 indicators.py', notes: 'Pure Python (numpy optional). Each helper returns a list the same length as input with None during warmup. Wilder smoothing for RSI/ATR/ADX, EMA with SMA seed, MACD(12,26,9) with warmup 35, Bollinger(20,2) width_pct=(upper-lower)/middle, VWAP of the current UTC session (candles whose ts falls in the same UTC day as the last candle). compute() builds IndicatorSet from the LAST candle; return_n = close[-1]/close[-1-n]-1; high_20/low_20 over the last 20 candles including the last; volume_ratio_20 = last volume / mean of the previous 20 volumes (excluding the last). warmup_ok iff ema_20, rsi_14, atr_14, bb_* are all non-None. Test against hand-computed values for small series, determinism, warmup Nones, monotone constant series (RSI 50 convention: if no gains and no losses return 50.0), and that compute() tolerates 0 or 1 candles (raise ValueError on empty).' },
  { key: 'quality_news', own: ['tradingbot/quality.py', 'tradingbot/news.py', 'tests/test_quality.py', 'tests/test_news.py'], spec: '§6 quality.py and §7 news.py', notes: 'quality.check(): timeframe seconds map (1m,5m,15m,1h,4h,1d); stale if last candle close time (ts + tf) is older than now - 2*tf; gap detection by consecutive ts deltas; too_few if fewer than 220 candles on 15m (and 60 on 1h, 60 on 4h, 30 on 1d when present); outlier if |close/prev_close-1| > 0.30; spread_too_wide if ticker bid/ask spread > 0.5%; ticker_stale if ticker.ts older than 5 minutes. news.fetch_news(): use feedparser on the string returned by fetcher(url); default fetcher uses urllib with a 10s timeout and a User-Agent (not exercised in tests); symbol aliases dict for at least BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, DOT; RED_FLAGS matching case-insensitive; tier by source domain; cache_path optional JSON cache keyed by url with fetched_ts (skip refetch within 10 minutes); never raise on a bad feed. Tests use inline RSS/Atom XML strings.' },
  { key: 'exchange', own: ['tradingbot/exchange.py', 'tests/test_exchange_paper.py', 'tests/test_exchange_ccxt.py'], spec: '§4 exchange.py', notes: 'ccxt is installed but tests must inject a fake exchange object (duck-typed: load_markets, fetch_ohlcv, fetch_ticker, fetch_order_book, fetch_balance, create_order, fetch_order, amount_to_precision, markets dict with limits/precision, set_sandbox_mode, options). CcxtBroker must (1) drop the last (incomplete) OHLCV candle and sort/dedupe by ts, (2) use quoteOrderQty for market buys, (3) parse fills so that a base-asset commission reduces base_amount and fee_quote is valued at fill price, with fees list > fee > info.fills precedence, (4) retry only ccxt.NetworkError/RateLimitExceeded/ExchangeNotAvailable up to 3 times with backoff (sleep injectable), (5) scrub api key/secret from exception messages and the __cause__ chain, (6) validate with risk-like checks locally (whitelist for buys, finite positive amounts, min order) — if tradingbot/risk.py does not exist yet, implement a private _validate in exchange.py that mirrors SPEC §3 validate_order_request and note it, (7) fetch_futures_context via a separately constructed public binanceusdm client (constructor injectable), returning None on any failure. PaperBroker fills at ask*(1+slippage)/bid*(1-slippage) with fee in quote, raises InsufficientFunds, refuses non-finite amounts, exposes balances dict and set_balances(). Also implement CcxtMarketData (public, no keys) sharing the fetch code.' },
  { key: 'llm', own: ['tradingbot/llm/__init__.py', 'tradingbot/llm/backend.py', 'tradingbot/llm/cost.py', 'tradingbot/llm/anthropic_backend.py', 'tests/test_cost.py', 'tests/test_anthropic_backend.py', 'tests/test_mock_backend.py'], spec: '§8 llm/ (backend.py, cost.py, anthropic_backend.py)', notes: 'anthropic 2.3.0 is installed: client.messages.parse(model=..., max_tokens=..., system=[...], messages=[...], output_format=<pydantic model>, output_config={"effort": ...}, timeout=...) returns a ParsedMessage with .parsed_output, .stop_reason, .stop_details, .usage (input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens), ._request_id. Verify the exact kwargs by inspecting the installed SDK (inspect.signature) before writing. Tests inject a fake client object with .messages.parse recording kwargs; assert: system block carries cache_control {"type":"ephemeral","ttl":"1h"}, no "thinking" kwarg, no tool_choice, user content is sort_keys JSON, model chosen by tier via settings.model_for, effort passed in output_config, stop_reason refusal/max_tokens -> ok False with error strings, parsed_output None -> parse_error, usage recorded in the ledger with cost computed from PRICES_USD_PER_MTOK (cached tokens at the cached price, cache_write tokens at 1.25x input for 5m or 2x for 1h — use 2x since ttl is 1h), BudgetExceeded propagates BEFORE any API call, exception mapping (construct real anthropic exceptions: e.g. anthropic.RateLimitError("msg", response=httpx2.Response(429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")), body=None); check which httpx module the installed SDK uses by trying "import httpx2" first), and that the api key never appears in error strings or log records (caplog). CostLedger: jsonl append, restore totals on init, UTC dates, check() raises when spent_today + projected > daily or month > monthly, remaining_today(). MockBackend: deterministic; when a role has no configured response, call output_model.conservative_example() if it exists else try output_model.model_construct() -> if that fails return ok=False with error "no_mock_response"; records calls. AgentResult can be a plain pydantic model with output: Any.' },
  { key: 'skills_reports', own: ['tradingbot/skills.py', 'tradingbot/reports.py', 'tests/test_skills.py', 'tests/test_reports.py'], spec: '§9 skills.py and §12 reports.py', notes: 'Frontmatter parser: lines between the first two "---" lines; supports "key: value", "key: [a, b]" and YAML-style "key:\\n  - a\\n  - b" lists; no PyYAML. approx_tokens = len(body)//4 (document it). render() must be byte-deterministic (sorted by name) and must drop whole skills from the end when over max_tokens, logging a WARNING naming the dropped skills. reports: decisions.jsonl and journal.jsonl append with sort_keys; write_cycle_report writes JSON plus a short Korean Markdown summary (cycle number, equity, decisions, exits, llm calls/cost); daily_summary builds a TradeEvent(kind="daily_summary") with pnl, trades, wins/losses, cost. Tests use tmp_path.' },
  { key: 'portfolio', own: ['tradingbot/portfolio.py', 'tests/test_portfolio.py'], spec: '§10 portfolio.py', notes: 'Atomic save via tmp file + os.replace with 5 retries (sleep injectable, tests patch os.replace to fail twice). Corrupt policy per mode (paper quarantine to state.json.corrupt.<ts>; testnet/live raise CorruptStateError), mode mismatch policy. open_position deducts paper balance (quote_amount + fee_quote) only in paper mode; close_position credits quote in paper mode; partial fill rule: if fill.base_amount < position.base_amount*(1-0.005) book a proportional ClosedTrade with reason "partial" and keep the residual Position (proportional quote_spent/fee_quote), else close fully (over-fills are clamped to the position size with a WARNING). Update daily stats (realized_pnl, trades, wins, losses, consecutive_losses reset on win), cooldown_until_cycle[symbol]=cycle+COOLDOWN_CYCLES and loss_cooldown_until_cycle[symbol]=cycle+REVENGE_WINDOW_CYCLES on a loss. ensure_day: when the UTC date changes return the finished DailyStats and start a new one with start_equity=equity (consecutive_losses carries over only within the day: reset to 0 on a new day). equity(): paper = balance + sum(base*mark) using position.last_mark when a mark is missing; otherwise quote_balance param must be given. Tests: round-trip save/load equality, corrupt file per mode, partial exits, cooldown math, day rollover, kill/pause helpers.' },
  { key: 'notify', own: ['tradingbot/notify.py', 'tests/test_notify.py'], spec: '§11 notify.py', notes: 'Notifier uses a daemon worker thread and queue.Queue when async_send=True; notify() must return immediately; retries with backoff delays [1,3,9] seconds but the delays must be injectable (tests set delays to [0,0,0]); after final failure append the event JSON + error to failed_log (jsonl) and log at ERROR; flush(timeout) joins the queue. When async_send=False send synchronously (used by tests and the CLI "status"). TelegramTransport posts JSON to https://api.telegram.org/bot<token>/sendMessage via an injectable http_post(url, payload)->status_code (default uses urllib); on non-200 raise; __repr__/__str__ must mask the token and the url must never be logged. format(): Korean messages per SPEC with thousands separators, percentages with 2 decimals, thesis truncated to 200 chars, emoji per kind; exit uses 🔴 when pnl<0 else 🔵. Tests: queue+worker with a failing transport (always raises) -> failed_log has the record and notify() never blocked (measure < 0.2s), token not in repr/logs (caplog), formats for every EventKind.' },
  { key: 'dashboard', own: ['tradingbot/dashboard/__init__.py', 'tradingbot/dashboard/app.py', 'tradingbot/dashboard/static/index.html', 'tests/test_dashboard.py'], spec: '§14 dashboard', notes: 'FastAPI app reading files only (state.json per models.BotState, logs/decisions.jsonl, logs/journal.jsonl, logs/cycles/<date>/cycle_<n>.json, cost_ledger.jsonl, backtest_results/*.json). Missing files -> empty responses, never 500. POST /api/kill requires header X-Dashboard-Token equal to the content of <data_dir>/control/dashboard_token (create_app generates it with secrets.token_urlsafe(24) if missing); writes <data_dir>/control/STOP containing JSON {reason, ts}. POST /api/kill/clear removes STOP and writes control/CLEAR. GET / serves static/index.html (vanilla JS, no CDN, 10s polling, sections per SPEC, canvas sparkline of equity from cycles, STOP button with token input). Tests with fastapi.testclient.TestClient (httpx is installed) and tmp_path data_dir: every endpoint with and without files, kill with wrong/right token, kill/clear.' },
]

const BACKTEST = { key: 'backtest', own: ['tradingbot/backtest.py', 'tests/test_backtest.py'], spec: '§13 backtest.py', notes: 'Depends on tradingbot/risk.py and tradingbot/indicators.py which now exist — import and use them (default_stops, clamp_stops, enforce_net_rr, size_position, check_exits; indicators.compute). No look-ahead: signal from candle t close, fill at candle t+1 open*(1+slippage); stop/TP judged on candle low/high, stop wins when both touched; time stop 96 bars; one position per symbol, max 3 concurrent; equity marked at close. Metrics: net return, win rate, profit factor, max drawdown, Sharpe from daily returns (sqrt(365)), buy&hold per symbol; walk-forward 3 equal segments; Monte Carlo trade-order shuffle (seeded random.Random(42), 1000 iters) giving 5th percentile return and 95th percentile MDD; cost x2 stress; verdict rules per SPEC. Candle fetch with cache_dir JSON when market is given; tests pass candles directly (synthetic trending/choppy series) and assert no look-ahead (changing candle t+1 close must not change the signal at t), stop priority, verdict NOT VALIDATED on random data, run card files written to tmp out_dir.' }

const IMPL_SCHEMA = {
  type: 'object',
  properties: {
    files_written: { type: 'array', items: { type: 'string' } },
    pytest_summary: { type: 'string' },
    tests_passed: { type: 'integer' },
    tests_failed: { type: 'integer' },
    deviations_from_spec: { type: 'array', items: { type: 'string' } },
    frozen_file_change_requests: { type: 'array', items: { type: 'string' }, description: 'changes you would need in models.py/config.py/SPEC.md' },
    notes: { type: 'array', items: { type: 'string' } },
  },
  required: ['files_written', 'pytest_summary', 'tests_passed', 'tests_failed', 'deviations_from_spec', 'frozen_file_change_requests', 'notes'],
  additionalProperties: false,
}
const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          title: { type: 'string' },
          description: { type: 'string' },
          proof_test: { type: 'string', description: 'name of the failing test you added in the review test file, or "" if the finding could not be proven' },
          confirmed: { type: 'boolean', description: 'true only if the proof test fails against the current code' },
        },
        required: ['id', 'severity', 'title', 'description', 'proof_test', 'confirmed'],
        additionalProperties: false,
      },
    },
    review_test_file: { type: 'string' },
    summary: { type: 'string' },
  },
  required: ['findings', 'review_test_file', 'summary'],
  additionalProperties: false,
}
const FIX_SCHEMA = {
  type: 'object',
  properties: {
    fixed: { type: 'array', items: { type: 'string' } },
    not_fixed: { type: 'array', items: { type: 'string' }, description: 'finding id + why (e.g. invalid finding, spec conflict)' },
    pytest_summary: { type: 'string' },
    tests_passed: { type: 'integer' },
    tests_failed: { type: 'integer' },
  },
  required: ['fixed', 'not_fixed', 'pytest_summary', 'tests_passed', 'tests_failed'],
  additionalProperties: false,
}

const implement = (m) => agent(
`You own these files (create them): ${m.own.join(', ')}. Implement SPEC section ${m.spec} completely, with thorough tests, and iterate until your test files are green.
MODULE NOTES: ${m.notes}
${COMMON}
Return the structured result when done. tests_failed must be 0.`,
  { label: `impl:${m.key}`, phase: 'Implement', schema: IMPL_SCHEMA, effort: 'high' })

const review = (implRes, m) => agent(
`Adversarially review the module(s) ${m.own.filter(f => f.endsWith('.py') && !f.startsWith('tests/')).join(', ')} against SPEC section ${m.spec} in ${REPO}/SPEC.md. The implementer reported: ${JSON.stringify(implRes).slice(0, 3000)}
Your job: find real defects — spec violations, wrong math, unsafe behavior (a limit that can be exceeded, a secret that can leak, a crash on bad input, an exit that could be blocked), concurrency/persistence bugs, and missing tests for spec invariants. For EACH defect, add a failing test to a NEW file ${REPO}/tests/test_${m.key}_review.py (do not edit the implementer's files) that fails against the current code and would pass once fixed; run it with cd ${REPO} && ${PY} -m pytest tests/test_${m.key}_review.py -q and set confirmed=true only when it actually fails. Unprovable concerns go in with confirmed=false and proof_test "". Do not fix the code. Style nits are not findings. ${COMMON}`,
  { label: `review:${m.key}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })

const fix = (reviewRes, m) => agent(
`Fix the confirmed defects in ${m.own.filter(f => !f.startsWith('tests/')).join(', ')} so that BOTH the original tests (${m.own.filter(f => f.startsWith('tests/')).join(', ')}) and the review tests (${reviewRes ? reviewRes.review_test_file : 'tests/test_' + m.key + '_review.py'}) pass. Findings: ${JSON.stringify(reviewRes ? reviewRes.findings : [])}
If a review test is itself wrong (contradicts SPEC), fix or delete that test and explain in not_fixed. Keep the SPEC signatures. Run: cd ${REPO} && ${PY} -m pytest ${m.own.filter(f => f.startsWith('tests/')).join(' ')} tests/test_${m.key}_review.py -q  — iterate until green. ${COMMON}`,
  { label: `fix:${m.key}`, phase: 'Fix', schema: FIX_SCHEMA, effort: 'medium' })

const groupA = MODULES.filter(m => m.key !== 'dashboard')
const dash = MODULES.filter(m => m.key === 'dashboard')

const pA = pipeline(groupA, (m) => implement(m), (r, m) => review(r, m), (r, m) => fix(r, m))
const pD = pipeline(dash, (m) => implement(m), (r, m) => review(r, m), (r, m) => fix(r, m))
const resA = await pA
log(`group A done: ${resA.filter(Boolean).length}/${groupA.length}`)
const resB = await pipeline([BACKTEST], (m) => implement(m), (r, m) => review(r, m), (r, m) => fix(r, m))
const resD = await pD
log(`backtest + dashboard done`)

phase('Integrate')
const INTEG_SCHEMA = {
  type: 'object',
  properties: {
    pytest_summary: { type: 'string' },
    tests_passed: { type: 'integer' },
    tests_failed: { type: 'integer' },
    files_changed: { type: 'array', items: { type: 'string' } },
    unresolved: { type: 'array', items: { type: 'string' } },
    frozen_file_change_requests: { type: 'array', items: { type: 'string' } },
    notes: { type: 'array', items: { type: 'string' } },
  },
  required: ['pytest_summary', 'tests_passed', 'tests_failed', 'files_changed', 'unresolved', 'frozen_file_change_requests', 'notes'],
  additionalProperties: false,
}
const integ = await agent(
`All SPEC v1 modules were written by different agents. Run the FULL suite: cd ${REPO} && ${PY} -m pytest -q 2>&1 | tail -40 . Fix cross-module problems (import name mismatches, duplicated helpers that should use the real module, e.g. exchange.py's private validator should now call risk.validate_order_request if present, tests that assumed a double but a real module exists and differs), keep every module's public API as in SPEC, and iterate until the whole suite is green and runs in under ~90 seconds. Also run: ${PY} -c "import tradingbot, tradingbot.risk, tradingbot.indicators, tradingbot.quality, tradingbot.news, tradingbot.exchange, tradingbot.llm.backend, tradingbot.llm.cost, tradingbot.llm.anthropic_backend, tradingbot.skills, tradingbot.reports, tradingbot.portfolio, tradingbot.notify, tradingbot.backtest, tradingbot.dashboard.app" . Report the final pytest summary and anything unresolved. ${COMMON.replace('Do NOT run the whole suite while other modules are being written concurrently (other agents are writing other files at the same time) — only your own test files. ', '')}`,
  { label: 'integrate', phase: 'Integrate', schema: INTEG_SCHEMA, effort: 'high' })

return { modules: [...resA, ...resB, ...resD], integration: integ }