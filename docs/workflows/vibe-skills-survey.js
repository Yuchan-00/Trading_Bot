export const meta = {
  name: 'vibe-skills-survey',
  description: 'Survey all Vibe-Trading SKILL.md files and core mechanisms, then propose an agent role roster for the Binance spot bot',
  phases: [
    { title: 'Survey', detail: 'read SKILL.md files in batches of 9 + 6 mechanism readers' },
    { title: 'Roster', detail: 'three judges propose role rosters from different lenses' },
    { title: 'Synthesize', detail: 'merge the proposals into one roster' },
  ],
}

const ROOT = args.root
const SKILLS = args.skills
const BATCH = 9
const batches = []
for (let i = 0; i < SKILLS.length; i += BATCH) batches.push(SKILLS.slice(i, i + BATCH))

const BOT_CONTEXT = `
TARGET BOT (what the skills must serve): a Binance SPOT crypto trading bot (USDT pairs, market orders only, no leverage/derivatives trading), 15-minute cycles, Python 3.11, decisions made by several LLM agents (Claude API) whose outputs are JSON (structured outputs), with code-enforced hard limits (10%/symbol, max 3 positions, stop-loss <= 2%, take-profit <= 4%, daily loss -3% halt, 3 consecutive losses halt, confidence >= 0.65 to trade, cooldowns, kill switch, drawdown ladder auto-STOP at -6%). Paper mode by default; Binance testnet; live only with explicit confirmation. Data available to agents: OHLCV candles (1m..1d) via ccxt public endpoints, order book snapshot, 24h tickers, Binance futures public data (funding rate, open interest, long/short ratio, liquidations are optional), RSS/news headlines, the bot's own state (positions, P&L, journal). No paid data, no equities, no options, no China A-shares.
MODEL TIERS decided by the user: cheap analysis roles -> claude-haiku-5-5; research/risk roles -> claude-sonnet-5-5; the final decision maker (head) -> claude-opus-5-5. Every LLM call costs money: keep the roster to roughly 7-10 calls per candidate symbol per cycle at most, and prefer fewer.
OTHER REQUIREMENTS: a Telegram message on every trade event; a local web dashboard; everything must be implementable offline-testable with a mock backend.
LICENSE: Vibe-Trading is MIT; its skill text may be adapted with attribution, but we prefer to rewrite procedures in our own words and keep only formulas/checklists.
A summary of the previous (reference) bot design is in /home/user/Trading_Bot/docs/research/previous_session_summary.md - read it once if you need details.`

const SKILL_SCHEMA = {
  type: 'object',
  properties: {
    skills: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          path: { type: 'string' },
          purpose: { type: 'string', description: 'one or two sentences, what the skill instructs the agent to do' },
          inputs_needed: { type: 'array', items: { type: 'string' }, description: 'data/tools the skill expects' },
          outputs: { type: 'array', items: { type: 'string' }, description: 'what the agent is told to produce' },
          key_heuristics: { type: 'array', items: { type: 'string' }, description: 'up to 8 concrete, reusable rules/formulas/checklist items, short verbatim or near-verbatim quotes' },
          relevance_crypto_spot: { type: 'string', enum: ['high', 'medium', 'low', 'none'] },
          relevance_reason: { type: 'string' },
          suggested_role: { type: 'string', description: 'which bot role could use it (scanner, technical, derivatives_flow, news_sentiment, bull, bear, risk_manager, head, journal, backtest, none)' },
          adaptation_notes: { type: 'string', description: 'what to change for a Binance spot bot with only public data' },
          approx_tokens: { type: 'integer', description: 'rough length of the SKILL.md in tokens' },
          referenced_files: { type: 'array', items: { type: 'string' }, description: 'other files the SKILL.md references (scripts, references/*.md)' },
        },
        required: ['name', 'path', 'purpose', 'inputs_needed', 'outputs', 'key_heuristics', 'relevance_crypto_spot', 'relevance_reason', 'suggested_role', 'adaptation_notes', 'approx_tokens', 'referenced_files'],
        additionalProperties: false,
      },
    },
  },
  required: ['skills'],
  additionalProperties: false,
}

const MECH_SCHEMA = {
  type: 'object',
  properties: {
    topic: { type: 'string' },
    summary_md: { type: 'string', description: 'thorough markdown: what exists, how it works, file paths, key snippets (short verbatim quotes with file:line), and concrete adopt/adapt/skip recommendations for our bot' },
    adopt: { type: 'array', items: { type: 'string' }, description: 'specific ideas/patterns worth adopting, each with the file path it comes from' },
    skip: { type: 'array', items: { type: 'string' } },
  },
  required: ['topic', 'summary_md', 'adopt', 'skip'],
  additionalProperties: false,
}

const MECHANISMS = [
  { key: 'skills_loader_swarm', prompt: `Study how Vibe-Trading loads skills and how its multi-agent "swarm" teams are defined. Look under ${ROOT}/agent/src/skills (loader/registry code, not the SKILL.md folders), ${ROOT}/agent/src/swarm, and any team/preset definitions (grep for 'skills:' allowlists, 'investment committee', 'bull', 'bear', 'risk manager', 'crypto desk'). Report: the SKILL.md frontmatter format, how a skill is selected/loaded (load_skill tool, allowlists per worker), the full list of predefined swarm teams with their worker roles and which skills each worker gets (verbatim names), how workers pass results to each other, and how the final decision is formed. Note file paths and short verbatim snippets.` },
  { key: 'live_gates_risk', prompt: `Study Vibe-Trading's live-trading safety: ${ROOT}/agent/src/live (mandate, runtime, advisory, extractors), kill switch, halt handling, fail-closed gates, cancel/flatten logic, and any risk limit configuration. Also ${ROOT}/agent/src/governance and ${ROOT}/agent/src/security. Report the concrete gate checks in order, how errors fail closed, what is persisted, how credentials are redacted, and which of these a Binance spot bot with code-enforced hard limits should adopt.` },
  { key: 'backtest_validation', prompt: `Study ${ROOT}/agent/backtest: validation.py, metrics.py, run_card.py, constraints.py, regime.py, risk_xray.py, factor_costs.py, engines/, loaders/, benchmark.py, binance_* files. Report: which validation gates exist (look-ahead, cost/slippage, walk-forward, Monte Carlo, benchmark comparison, NOT_VALIDATED style verdicts), the metric definitions, the run-card format, Binance-specific reconciliation/tolerance logic, and what a rule-only 15m crypto backtest in our bot should copy.` },
  { key: 'tool_contracts_paper_trading', prompt: `Study ${ROOT}/schema/tool_contracts/paper_trade_step-v1.1.json, ${ROOT}/agent/src/trading, ${ROOT}/agent/src/portfolio, ${ROOT}/agent/src/shadow_account, and the paper_trade_step / backtest / get_market_data / technical_indicators tool implementations under ${ROOT}/agent/src/tools. Report: the paper_trade_step contract (fields, invariants like 'saved accounts move forward only', fill rules against completed bars), how tool inputs/outputs are validated (JSON schema), how market data is normalized, and what our bot should copy for its paper broker, portfolio persistence and tool/agent I/O contracts.` },
  { key: 'evals_journal_memory', prompt: `Study ${ROOT}/agent/evals/harness (NOT_EVALUABLE semantics, what is measured), ${ROOT}/agent/src/memory, ${ROOT}/agent/src/hypotheses, ${ROOT}/agent/src/goal, and the trade-journal / thesis-tracker / performance-attribution skills' referenced scripts if any. Report: how evaluation refuses to pass when instrumentation is missing, how memory/hypotheses/theses are stored and recalled across sessions, and what a trading bot's journal + post-trade review (sent to Telegram) should record.` },
  { key: 'factors_indicators_data', prompt: `Study ${ROOT}/agent/src/factors (registry, zoo, min_warmup_bars declarations, input masking), ${ROOT}/agent/src/market_data.py, the ccxt / binance data source code under ${ROOT}/agent/src/br_data or ${ROOT}/agent/src/providers, and ${ROOT}/agent/src/quantlib (list its 23 modules briefly). Report: how a factor declares warmup/inputs and how outputs are masked, how candles are fetched/normalized/cached from ccxt/binance (timeframes, pagination, staleness checks), and which indicator/factor definitions (with formulas) are worth copying into a crypto 15m bot's indicators module. Note the Qlib/alpha101 attribution requirements from ${ROOT}/NOTICE.` },
]

phase('Survey')
const surveyTasks = batches.map((b, i) => () => agent(
`You are surveying skill definition files from the open-source Vibe-Trading project (MIT) to decide which are useful for a specific trading bot.
${BOT_CONTEXT}

Read each of these SKILL.md files completely (and skim files they reference under the same directory, e.g. references/*.md or scripts, enough to describe them):
${b.map(s => `- ${ROOT}/agent/src/skills/${s}/SKILL.md`).join('\n')}

For EVERY skill listed, return one entry (do not skip any, even if irrelevant). Be concrete: key_heuristics must be actionable rules/formulas/thresholds/checklists that could be put into an agent prompt, quoted briefly. relevance_crypto_spot must consider that we only have public Binance spot/futures data and RSS news, no equities/options/A-shares/paid data.`,
  { label: `skills:${i}`, phase: 'Survey', schema: SKILL_SCHEMA, effort: 'medium' }))

const mechTasks = MECHANISMS.map(m => () => agent(
`${m.prompt}

${BOT_CONTEXT}

Be thorough and concrete; this report is the only thing the designer will read about this topic. Quote short verbatim snippets with file paths. Do not modify any files.`,
  { label: `mech:${m.key}`, phase: 'Survey', schema: MECH_SCHEMA, effort: 'medium' }))

const surveyOut = await parallel([...surveyTasks, ...mechTasks])
const skillBatches = surveyOut.slice(0, batches.length).filter(Boolean)
const mechOut = surveyOut.slice(batches.length).filter(Boolean)
const allSkills = skillBatches.flatMap(r => r.skills)
const missing = SKILLS.filter(s => !allSkills.some(x => x.name === s || x.path.includes(`/${s}/`)))
log(`survey: ${allSkills.length}/${SKILLS.length} skills described; missing: ${missing.join(', ') || 'none'}; mechanisms: ${mechOut.length}/${MECHANISMS.length}`)

const relevant = allSkills.filter(s => s.relevance_crypto_spot === 'high' || s.relevance_crypto_spot === 'medium')
const compact = relevant.map(s => ({ name: s.name, relevance: s.relevance_crypto_spot, purpose: s.purpose, suggested_role: s.suggested_role, inputs_needed: s.inputs_needed, key_heuristics: s.key_heuristics.slice(0, 5), adaptation_notes: s.adaptation_notes }))
const swarmMech = mechOut.find(m => m.topic.toLowerCase().includes('swarm') || m.topic.toLowerCase().includes('skill'))
const swarmText = swarmMech ? swarmMech.summary_md.slice(0, 12000) : '(no swarm report)'

const ROSTER_SCHEMA = {
  type: 'object',
  properties: {
    lens: { type: 'string' },
    roles: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          role_id: { type: 'string', description: 'snake_case id, e.g. scanner, technical, derivatives_flow, news_sentiment, bull, bear, risk_manager, head, journal' },
          name_ko: { type: 'string' },
          model: { type: 'string', enum: ['claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5', 'none'] },
          purpose: { type: 'string' },
          skills: { type: 'array', items: { type: 'string' }, description: 'Vibe-Trading skill names assigned to this role (verbatim)' },
          inputs: { type: 'array', items: { type: 'string' } },
          output_fields: { type: 'array', items: { type: 'string' }, description: 'JSON fields the role returns' },
          when: { type: 'string', description: 'once per cycle / per candidate / per open position / post-trade' },
          why_this_model: { type: 'string' },
        },
        required: ['role_id', 'name_ko', 'model', 'purpose', 'skills', 'inputs', 'output_fields', 'when', 'why_this_model'],
        additionalProperties: false,
      },
    },
    cycle_flow: { type: 'string', description: 'ordered description of one cycle: who runs when, what passes to whom, how the head decides, where code gates apply' },
    calls_per_cycle_estimate: { type: 'string' },
    rejected_skills: { type: 'array', items: { type: 'string' }, description: 'relevant-looking skills deliberately not used, with the reason' },
    telegram_events: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' }, description: 'ways this roster could go wrong (cost, latency, herd thinking, prompt size) and mitigations' },
  },
  required: ['lens', 'roles', 'cycle_flow', 'calls_per_cycle_estimate', 'rejected_skills', 'telegram_events', 'risks'],
  additionalProperties: false,
}

phase('Roster')
const LENSES = [
  { id: 'risk-first', text: 'Capital preservation first: every role must reduce the chance of a bad trade; prefer fewer, cheaper calls and strong veto power for the risk manager.' },
  { id: 'signal-quality-first', text: 'Decision quality first: maximize the diversity and independence of evidence (technical, derivatives/flow, news, on-chain proxies) before the head decides; avoid roles that just restate each other.' },
  { id: 'cost-and-simplicity-first', text: 'Lowest cost and simplest pipeline that still uses the genuinely useful skills; merge roles where their skills overlap; the head must still see a bull and a bear case.' },
]
const proposals = (await parallel(LENSES.map(l => () => agent(
`Design the agent role roster for the trading bot described below, from this lens: ${l.text}
${BOT_CONTEXT}

Skill survey (only skills rated high/medium relevance), as JSON:
${JSON.stringify(compact)}

How Vibe-Trading itself organizes skills into swarm teams (reference, do not copy blindly):
${swarmText}

Requirements for the roster: 6-10 roles; each role names the exact skills it uses (from the survey) and the model tier; the head role makes the final BUY/SELL/HOLD decision with a confidence and a thesis; a bull and a bear case must exist; a risk role must exist; include a post-trade journal role if you think it is worth its cost (it may be a cheap model); describe the ordered cycle flow and where code-enforced gates (not LLM) apply; estimate LLM calls per cycle with 2 candidate symbols and 1 open position. Skills that need data we do not have must be adapted or rejected (say which).`,
  { label: `roster:${l.id}`, phase: 'Roster', schema: ROSTER_SCHEMA, effort: 'high' })))).filter(Boolean)
log(`roster proposals: ${proposals.length}`)

phase('Synthesize')
const FINAL_SCHEMA = {
  type: 'object',
  properties: {
    roles: ROSTER_SCHEMA.properties.roles,
    cycle_flow: { type: 'string' },
    calls_per_cycle_estimate: { type: 'string' },
    rejected_skills: { type: 'array', items: { type: 'string' } },
    telegram_events: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    rationale_md: { type: 'string', description: 'markdown: how the three proposals differed and what was taken from each' },
  },
  required: ['roles', 'cycle_flow', 'calls_per_cycle_estimate', 'rejected_skills', 'telegram_events', 'risks', 'rationale_md'],
  additionalProperties: false,
}
const final = await agent(
`Three designers proposed agent role rosters for the trading bot described below, from different lenses. Merge them into ONE final roster: keep what at least two proposals agree on, take the best unique ideas from each, and resolve conflicts explicitly in rationale_md. Keep 6-10 roles, keep the user's model tiers (cheap analysis -> haiku, research/risk -> sonnet, head -> opus), and keep total LLM calls per cycle (2 candidates, 1 open position) at or below 14.
${BOT_CONTEXT}

Proposals:
${JSON.stringify(proposals)}

Skill survey (high/medium relevance):
${JSON.stringify(compact)}`,
  { label: 'synthesize', phase: 'Synthesize', schema: FINAL_SCHEMA, effort: 'high' })

return { skills: allSkills, missing, mechanisms: mechOut, proposals, final }