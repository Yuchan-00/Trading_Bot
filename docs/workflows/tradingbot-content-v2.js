export const meta = {
  name: 'tradingbot-content-v2',
  description: 'Write the 25 adapted SKILL.md files, the 8 role prompts and THIRD_PARTY_NOTICES, then review them for consistency and license hygiene',
  phases: [
    { title: 'Write', detail: 'skills in batches of 3, one prompt per role, notices' },
    { title: 'Review', detail: 'license/plagiarism check per skill batch; cross-prompt consistency' },
    { title: 'Fix', detail: 'apply confirmed review findings' },
  ],
}

const REPO = args.repo, VIBE = args.vibe, PY = args.py
const SKILLS = args.skills, ROLES = args.roles
const batches = []
for (let i = 0; i < SKILLS.length; i += 3) batches.push(SKILLS.slice(i, i + 3))

const ROLE_SKILLS = {
  technical: ['technical-basic', 'ichimoku', 'candlestick', 'smc', 'volatility', 'minute-analysis'],
  derivatives_flow: ['perp-funding-basis', 'crypto-derivatives', 'liquidation-heatmap', 'investor-lenses', 'market-microstructure'],
  news_sentiment: ['event-driven', 'sentiment-analysis', 'research-discipline'],
  bull: ['behavioral-finance', 'research-discipline', 'event-driven'],
  bear: ['investor-lenses', 'behavioral-finance', 'research-discipline', 'correlation-regime', 'perp-funding-basis'],
  risk_manager: ['risk-analysis', 'correlation-regime', 'correlation-analysis', 'asset-allocation', 'market-microstructure', 'execution-model', 'thesis-tracker', 'trade-journal', 'strategy-dev-manager', 'volatility', 'behavioral-finance'],
  head: ['investor-lenses', 'behavioral-finance', 'thesis-tracker', 'sentiment-analysis', 'event-driven', 'research-discipline'],
  journal: ['trade-journal', 'performance-attribution', 'report-generate', 'strategy-dev-manager'],
}
const skillRoles = {}
for (const [r, ss] of Object.entries(ROLE_SKILLS)) for (const s of ss) { (skillRoles[s] = skillRoles[s] || []).push(r) }
const rolesOf = (b) => { const o = {}; for (const s of b) o[s] = skillRoles[s] || []; return JSON.stringify(o) }

const CONTEXT = `
PROJECT: ${REPO} — a Binance SPOT (long-only, USDT pairs, market orders, 15-minute cycles) trading bot whose decisions are made by Claude agents with code-enforced hard limits (10%/symbol, max 3 positions, stop-loss <= 2%, take-profit <= 4%, daily loss -3% halt, 3 consecutive losses halt, confidence >= 0.65, cooldowns, kill switch, drawdown ladder with auto-STOP at -6%). Read ${REPO}/DESIGN.md (sections 1-3 and 11), ${REPO}/SPEC.md sections 17-23 (features/packs, scanner, gates, agent schemas, prompts/skills conventions) and ${REPO}/tradingbot/agents/schemas.py (the exact output models) before writing. The role roster and the reasons behind it are in ${REPO}/docs/research/agent_roster.md; per-skill survey notes (purpose, key heuristics, adaptation notes) are in ${REPO}/docs/research/vibe_skill_survey.md.
Agents receive ONLY code-computed, labelled JSON (feature_pack / flow_pack / risk_pack / regime_pack / sentiment / headlines / account state / other roles' outputs as specified in SPEC §20.3). They never see raw candles beyond 20 hourly closes, never have tools, never fetch data, never compute indicators themselves. Every claim must cite an input field name. Spot-only: bearish views are expressed as "no entry / exit", never "short". Free-text output fields must be written in Korean; field names and enums are fixed by the schema.
Do NOT git commit. Do not touch files outside the ones you are told to write.`

const SKILL_RULES = `
SKILL.md FORMAT (SPEC §23): a YAML-like frontmatter between two '---' lines with exactly these keys: name (the skill directory name), description (one line, Korean, what the skill gives the agent), roles (a list of role ids, e.g. roles: [technical, bear]), version: 1, source: "adapted from HKUDS/Vibe-Trading agent/src/skills/<name>/SKILL.md (MIT)", license: MIT. Then the body in English, at most 900 words, Markdown headings allowed, with: when to apply, the procedure/checklist the role should follow, concrete thresholds/formulas (numbers are fine), what NOT to do, and how to express the result through the role's output fields (name the schema fields). The body must be written in your own words: do NOT copy sentences from the original SKILL.md (formulas, numbers and single technical terms are fine). Strip everything that does not apply to a Binance spot long-only bot with only public data (options, A-shares, shorting, paid data, tool invocations, file paths, CLI commands of Vibe-Trading). The reader is an LLM inside one fixed role: write directly to it. No dates, no symbol names, no cycle numbers (the system prompt must stay byte-stable for prompt caching).`

const PROMPT_RULES = `
PROMPT FORMAT (SPEC §23): English Markdown system prompt for ONE role. Must contain, in this order: (1) Role and mission in 2-3 sentences; (2) The exact JSON input the role receives — list every top-level field of the payload builder for this role from SPEC §20.3 and what it means (packs are described in SPEC §17; use the same field names); (3) The decision procedure / rubric (concrete thresholds, ordered steps; reference the skills by name — the skills' text is appended after the prompt under a '# Skills' heading by the runner, so do not paste skill bodies); (4) Output contract: list every output field from schemas.py for this role with the allowed values and the rules for filling it, including that the model must answer ONLY with the structured output; (5) Hard rules: hard limits restated verbatim (10% per symbol, max 3 positions, stop-loss <= 2%, take-profit <= 4%, confidence >= 0.65 floor that code may raise to 0.75), cite input field names in every evidence/claim, never invent numbers, 'empty feed is not a calm market', spot-only wording, abstain policy (if inputs are missing or contradictory, produce the conservative output and say why in the relevant text field), Korean for all free-text fields; (6) Two short worked examples of correct reasoning (as prose, not JSON) — one that leads to a conservative output and one that leads to an actionable output. Never include dates, symbol names, cycle numbers, or anything that changes per request. Target 700-1400 words.`

const ROLE_REQS = {
  technical: 'per-symbol call; fixed rubric: trend / mean-reversion / volume vote, Ichimoku triple filter (4h regime + 15m trigger), candlestick composite, SMC structure + FVG filter, HV-percentile bands; direction long/flat/avoid; stop candidate must respect the 2% cap and the noise_check (1.5*ATR_1h% < 2%); for open positions fill exit_flag/exit_reason.',
  derivatives_flow: 'one batched call over all symbols in the payload; place each symbol on the capitulation..euphoria pendulum; threshold table: funding > +0.05%/8h extreme long crowding, < -0.02%/8h squeeze territory, OI +5% with funding > +0.03% = leveraged_long_buildup, nearest estimated long-liquidation cluster < 2% below = high cascade risk; estimated clusters are projections, use them only to block, never as a reason to enter; return block_new_longs with block_reason when crowding or cascade risk is high.',
  news_sentiment: 'score only NEW headlines on the fixed 7-step scale (-1.0, -0.66, -0.33, 0, +0.33, +0.66, +1.0), event_type enum, affected symbols via the alias map in the payload, <= 20-word Korean summary, stale flag (> 24h not intraday-actionable, > 7d stale), risk_event true for hack/exploit/delisting/regulatory action/exchange incident affecting a watchlist symbol; feed_health_note must say when the feed is empty or stale (empty feed != calm market).',
  bull: 'strongest honest long case from frozen evidence only; underreaction checklist (1h-4h momentum with volume confirmation, RSI(5) extreme rebound, capital-gain overhang cgo_60, abnormal volume); mandatory >= 2 disconfirming_evidence items from the packs; invalidation_level must be a price consistent with the technical stop candidate; never see or guess the bear case.',
  bear: 'independent adversary with a different procedure (Munger-style inversion rewritten for a 15m spot long): enumerate >= 3 death paths from different families each with an observable trigger from our data and a probability band; greed flags; one disconfirming item per bull-style positive in the packs; verdict survivable / survivable_if_monitored (with monitoring_conditions) / decline; strongest_point is quoted verbatim by the head when the bear loses.',
  risk_manager: 'one batched call; binding veto the head cannot override (it may only shrink further); per candidate approve/conditional/veto with max_size_pct <= 10 (halved when regime fused), stop_pct <= 2, take_profit_pct <= 4, rr_net_estimate, bull_point_reliability scores with pushback; per open position assumption_status per stored assumption, red_lines_triggered, health 1-10, action hold/trim/exit; global entries_paused with reason, confidence_floor >= 0.65 (0.75 when fused / decay_state decayed / bias severe), exposure_cap_pct <= 30; use code-computed numbers (VaR/CVaR, correlations, liquidity, impact, decay state, bias severities) and never recompute them.',
  head: 'one batched call only for approved/conditional candidates and flagged positions; BUY/SELL/HOLD per symbol with confidence, size_pct <= risk max_size_pct, stop_pct <= risk stop_pct, tp_pct <= risk take_profit_pct; thesis with 2-3 reasons, 2-4 falsifiable assumptions, structured red_lines (metric/op/value using only the allowed metrics), horizon_bars; stacking protocol: read bull and bear independently, classify the disagreement (horizon / caliber / risk_preference / information / none), quote the losing side strongest point verbatim, name the decisive factor; debias checklist booleans; hold_reasons for every non-trade; vetoed candidates in the payload are context only and must be HOLD; SELL only for open positions.',
  journal: 'two modes selected by payload.mode: post_trade (one closed trade with its thesis history and verdicts -> JournalPostTrade) and daily (code-computed metrics table -> JournalDaily); every number comes from the payload; lesson_tag enum; action_items only when closed_count >= 20 else abstain_reason and do_not_change_yet true; telegram_text <= 400 chars in Korean.',
}

const WRITE_SCHEMA = {
  type: 'object',
  properties: {
    files_written: { type: 'array', items: { type: 'string' } },
    word_counts: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, words: { type: 'integer' } }, required: ['file', 'words'], additionalProperties: false } },
    notes: { type: 'array', items: { type: 'string' } },
  },
  required: ['files_written', 'word_counts', 'notes'],
  additionalProperties: false,
}
const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      file: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] }, title: { type: 'string' }, detail: { type: 'string' }, confirmed: { type: 'boolean' },
    }, required: ['file', 'severity', 'title', 'detail', 'confirmed'], additionalProperties: false } },
    summary: { type: 'string' },
  },
  required: ['findings', 'summary'],
  additionalProperties: false,
}
const FIX_SCHEMA = {
  type: 'object',
  properties: { fixed: { type: 'array', items: { type: 'string' } }, not_fixed: { type: 'array', items: { type: 'string' } }, files_changed: { type: 'array', items: { type: 'string' } } },
  required: ['fixed', 'not_fixed', 'files_changed'],
  additionalProperties: false,
}

phase('Write')
const writeSkills = (b, i) => agent(
`Write these adapted skill files: ${b.map(s => `${REPO}/skills/${s}/SKILL.md (roles: [${(skillRoles[s] || []).join(', ')}])`).join('; ')}.
For each: read the ORIGINAL ${VIBE}/agent/src/skills/<name>/SKILL.md (and any references/ it points to, briefly) and the survey entry for it in ${REPO}/docs/research/vibe_skill_survey.md, then write our own adapted version.
${SKILL_RULES}
${CONTEXT}
Create the directories. Return the files written with word counts of the body (excluding frontmatter).`,
  { label: `skills:${i}`, phase: 'Write', schema: WRITE_SCHEMA, effort: 'medium' })

const writePrompt = (role) => agent(
`Write the system prompt ${REPO}/prompts/${role}.md for the role "${role}" (skills attached at runtime: ${ROLE_SKILLS[role].join(', ')}).
${PROMPT_RULES}
${CONTEXT}
Role-specific requirements from the roster (docs/research/agent_roster.md, section on ${role}) must be reflected: ${ROLE_REQS[role]}
Return the file written and its word count.`,
  { label: `prompt:${role}`, phase: 'Write', schema: WRITE_SCHEMA, effort: 'high' })

const writeNotices = () => agent(
`Write ${REPO}/THIRD_PARTY_NOTICES.md: a Korean introduction stating that the files under skills/ are adapted (rewritten) from HKUDS/Vibe-Trading (https://github.com/HKUDS/Vibe-Trading, MIT License) and list which skill names are adapted (${SKILLS.join(', ')}); then include the full MIT license text copied verbatim from ${VIBE}/LICENSE and the relevant attribution lines from ${VIBE}/NOTICE (only the parts about HKUDS; we do not bundle Qlib or alpha formulas). Also state that FinceptTerminal (AGPL-3.0) was consulted for ideas only and no code or text from it is included. ${CONTEXT}`,
  { label: 'notices', phase: 'Write', schema: WRITE_SCHEMA, effort: 'low' })

const reviewSkills = (res, b, i) => agent(
`Review these adapted skill files for license hygiene and quality: ${b.map(s => `${REPO}/skills/${s}/SKILL.md`).join(', ')} against the originals ${b.map(s => `${VIBE}/agent/src/skills/${s}/SKILL.md`).join(', ')}.
Checks (each failing check is a finding, confirmed=true only if you verified it): (a) any sentence of >= 8 consecutive words copied verbatim from the original (quote it); (b) frontmatter has exactly the keys name/description/roles/version/source/license with the right values and roles equal to ${rolesOf(b)}; (c) body <= 900 words; (d) content inapplicable to a Binance spot long-only bot with public data remains (options, shorting, A-shares, tool/CLI invocations, file paths); (e) the skill does not tell the agent to compute indicators from raw data or to fetch anything; (f) it names the role's output fields correctly per ${REPO}/tradingbot/agents/schemas.py; (g) anything that changes per request (dates, symbols). Use ${PY} to count words and to check verbatim overlaps mechanically (e.g. compare 8-grams). Do not edit files. ${CONTEXT}`,
  { label: `review-skills:${i}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'medium' })

const fixSkills = (rev, b, i) => agent(
`Apply the confirmed findings to ${b.map(s => `${REPO}/skills/${s}/SKILL.md`).join(', ')}: ${JSON.stringify(rev ? rev.findings.filter(f => f.confirmed) : [])}. Keep the rules: ${SKILL_RULES}. ${CONTEXT}`,
  { label: `fix-skills:${i}`, phase: 'Fix', schema: FIX_SCHEMA, effort: 'low' })

const skillsPipeline = pipeline(batches, (b, _r, i) => writeSkills(b, i), (res, b, i) => reviewSkills(res, b, i), (rev, b, i) => fixSkills(rev, b, i))
const promptsAll = parallel(ROLES.map(r => () => writePrompt(r)))
const noticesP = writeNotices()

const [skillsRes, promptsRes, noticesRes] = await Promise.all([skillsPipeline, promptsAll, noticesP])
log(`skills batches: ${skillsRes.filter(Boolean).length}/${batches.length}, prompts: ${promptsRes.filter(Boolean).length}/${ROLES.length}`)

phase('Review')
const promptReview = await agent(
`Cross-check all role prompts ${ROLES.map(r => `${REPO}/prompts/${r}.md`).join(', ')} against ${REPO}/tradingbot/agents/schemas.py, SPEC.md §20 and §23, DESIGN.md §1-3 and the skill files under ${REPO}/skills/. Findings (confirmed=true only when verified by reading): (a) an output field named in a prompt that does not exist in the schema, or a schema field the prompt never explains; (b) enum values that differ from the schema; (c) hard limits stated inconsistently across prompts; (d) a prompt that lets the model see or assume another analyst's output it is not given (technical/derivatives_flow/news_sentiment must be partitioned; bull and bear must not see each other); (e) per-request content (dates, symbols, cycle numbers); (f) missing abstain policy or missing 'Korean free text' rule; (g) a prompt that asks the model to compute indicators or fetch data; (h) a skill referenced by a prompt that is not in that role's skill list (${JSON.stringify(ROLE_SKILLS)}); (i) word count outside 700-1400. Do not edit. ${CONTEXT}`,
  { label: 'review-prompts', phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })

phase('Fix')
const promptFix = await agent(
`Apply the confirmed findings to the prompt files: ${JSON.stringify(promptReview ? promptReview.findings.filter(f => f.confirmed) : [])}. Keep ${PROMPT_RULES}. ${CONTEXT}`,
  { label: 'fix-prompts', phase: 'Fix', schema: FIX_SCHEMA, effort: 'medium' })

const verify = await agent(
`Verify the content set mechanically with ${PY}: (1) every directory under ${REPO}/skills/ has a SKILL.md whose frontmatter parses (keys name, description, roles, version, source, license; roles as a list) — write a small parser inline (no PyYAML); (2) the set of skill names equals ${JSON.stringify(SKILLS)}; (3) for each role in ${JSON.stringify(ROLE_SKILLS)} every listed skill exists and its roles list contains that role; (4) every prompt file exists for ${JSON.stringify(ROLES)}; (5) report word counts; (6) grep the skills and prompts for dates (20xx-), '/USDT', 'cycle ' numbers and list hits. Fix trivial frontmatter errors yourself (roles lists, missing keys) and report everything else. ${CONTEXT}`,
  { label: 'verify', phase: 'Fix', schema: REVIEW_SCHEMA, effort: 'low' })

return { skills: skillsRes, prompts: promptsRes, notices: noticesRes, promptReview, promptFix, verify }