export const meta = {
  name: 'tradingbot-content-fix',
  description: 'Align the 8 role prompts and 25 skills with SPEC v2.1 (payload contract) and the 16 confirmed cross-review findings, then cross-check and mechanically verify',
  phases: [
    { title: 'Fix', detail: '8 prompt fixers + 5 skill-batch aligners in parallel' },
    { title: 'Review', detail: 'cross-check prompts/skills vs SPEC v2.1' },
    { title: 'Verify', detail: 'apply confirmed findings, mechanical checks' },
  ],
}

const REPO = args.repo, PY = args.py, FINDINGS = args.findings
const ROLES = ['technical', 'derivatives_flow', 'news_sentiment', 'bull', 'bear', 'risk_manager', 'head', 'journal']
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
const SKILL_BATCHES = [
  ['technical-basic', 'ichimoku', 'candlestick', 'smc', 'minute-analysis'],
  ['volatility', 'perp-funding-basis', 'crypto-derivatives', 'liquidation-heatmap', 'market-microstructure'],
  ['investor-lenses', 'event-driven', 'sentiment-analysis', 'research-discipline', 'behavioral-finance'],
  ['correlation-regime', 'risk-analysis', 'correlation-analysis', 'asset-allocation', 'execution-model'],
  ['thesis-tracker', 'trade-journal', 'strategy-dev-manager', 'performance-attribution', 'report-generate'],
]
const SECTION = { technical: '20.3a.1', derivatives_flow: '20.3a.2', news_sentiment: '20.3a.3', bull: '20.3a.4', bear: '20.3a.4', risk_manager: '20.3a.5', head: '20.3a.6', journal: '20.3a.7' }
const skillRoles = {}
for (const [r, ss] of Object.entries(ROLE_SKILLS)) for (const s of ss) { (skillRoles[s] = skillRoles[s] || []).push(r) }

const DECISIONS = `
UNIFIED DECISIONS (apply everywhere; they override anything the current text says):
D1 cgo_60 is in % units. Thresholds: cgo_60 >= +3 = overhang / greed side (profit-taking pressure); cgo_60 <= -3 = many underwater holders (capitulation, rebound potential). Remove every other cgo_60 threshold (0.2, -0.3, 20, ...).
D2 Technical target: tp_pct = min(4.0, max(2*stop_pct, 1.5*stop_pct + 0.75)). Net R:R everywhere = (tp_pct - 0.30) / (stop_pct + 0.30) >= 1.5 (0.30% = round-trip fees + slippage).
D3 noise_check = feature_pack.noise_ok_2pct AND stop_pct >= feature_pack.noise_floor_stop_pct (both code-computed). noise_check false -> confidence <= 0.50 and timing wait_pullback. Code (G1) recomputes this, so never claim a pass the fields do not support.
D4 News score scale is exactly {-1.0, -0.66, -0.33, 0, +0.33, +0.66, +1.0}. "strong negative" = score <= -0.66, "negative" = score <= -0.33, "positive" = score >= +0.33. stale = age_h > 24; age_h > 168 -> |score| <= 0.33. Use the code-provided age_h; never do epoch arithmetic.
D5 derivatives_flow: no cascade-risk escalation from cluster spacing (the projected tiers are a fixed distance apart). Estimated liquidation clusters may only block or warn, never justify an entry.
D6 expected_horizon_bars (15m bars): trend continuation 16-48, RSI(5) / washout rebound 4-16, breakout 8-32. The same table must appear in prompts/bull.md and skills/behavioral-finance (or the skill must defer to the role prompt).
D7 Execution cost: use risk_packs[symbol].exec_cost_pct (code-computed = impact_bps_est/100 + spread_bps/200 + 0.20). Veto when exec_cost_pct > 0.25 * stop_pct. Never widen a stop beyond the technical stop candidate. If net R:R < 1.5 tighten the stop only; if that is impossible, veto.
D8 risk_manager confidence caps: if every input channel of a candidate is missing -> conservative output with confidence <= 0.3; otherwise if any of risk_packs[symbol], bull or bear is null -> confidence <= 0.4. The lower cap wins.
D9 evidence strings (technical, derivatives_flow, bull/bear evidence-like lists) use the format "<field path>: <value> — <짧은 한국어 해석>". All free-text fields are Korean; field names/enums are fixed by the schema.
D10 The technical payload carries no thesis and no confidence for a held position (SPEC v2.1 §20.3a.1); exit reads rest on the feature pack and the position numbers only.
D11 risk_manager positions[].prev_assumption_status is the previous risk_manager assessment (may be null); it is not a code pre-check. Re-assess every stored assumption now.
D12 Paths must be exactly the SPEC v2.1 §20.3a paths for the role (e.g. head: technical.labels.cgo_60, risk.confidence_floor, gate.confidence_floor, sentiment.composite.score_0_100, sentiment.event_signal[symbol].combined_score; bull/bear: feature_extract.*, news_events[], news_context.*, event_signal.*, scanner_row.factor_table.*, scanner_row.liquidity.*; bear: corr_to_open, hv_pct; risk: account_pack.*, risk_packs[symbol].*, flow_blocks[symbol].*, positions[].prev_assumption_status; journal: trade.thesis.*, thesis_history[], verdicts.*, metrics_table.*).
D13 journal: metrics_table.attribution_buckets = {noise_trades, stop_losses, take_profits, time_stops, head_sells}; gate_stats keys = candidates_seen, g1_dropped, g2_bear_decline, g3_risk_veto, g3_other, g4_downgrade, head_calls, bear_decline_rate, risk_veto_rate. Any value may be null.
D14 Prompt length 700-1900 words (system prompts are cached). Do not pad.
D15 No per-request content (dates, symbol names except BTC as the regime benchmark, cycle numbers) in prompts or skills.`

const CONTEXT = `
PROJECT: ${REPO} — Binance SPOT long-only bot; Claude agents decide inside code-enforced hard limits. Read ${REPO}/SPEC.md: §17 (packs), §19 (gates), §20 (agents) and the final 'SPEC v2.1' part (payload field contract §20.3a and the 보완 사항 amendments) which overrides earlier text. Output models: ${REPO}/tradingbot/agents/schemas.py (frozen; do not edit). The confirmed cross-review findings are in ${FINDINGS} (JSON with 'findings'). Do NOT git commit. Edit only the files you are told to edit.`

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    files_changed: { type: 'array', items: { type: 'string' } },
    applied: { type: 'array', items: { type: 'string' } },
    not_applied: { type: 'array', items: { type: 'string' } },
    word_counts: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, words: { type: 'integer' } }, required: ['file', 'words'], additionalProperties: false } },
  },
  required: ['files_changed', 'applied', 'not_applied', 'word_counts'],
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

phase('Fix')
const fixPrompt = (role) => agent(
`Edit ${REPO}/prompts/${role}.md so that it matches SPEC v2.1 exactly and resolves every finding in ${FINDINGS} that concerns this file or applies to all prompts.
1) Rewrite section "2. Input you receive" so it lists exactly the payload keys of SPEC v2.1 §${SECTION[role]} for this role (${role === 'bull' || role === 'bear' ? 'bull and bear share §20.3a.4; bear additionally has corr_to_open and hv_pct; never mention the other side' : 'exact key names and nesting'}), with one line each on meaning, and the 'null means not available' rule.
2) Update every field path used in the procedure, output rules and worked examples to those keys.
3) Apply the unified decisions below.
Keep the structure required by SPEC §23 (role/mission, input, procedure, output contract for every schema field of this role, hard rules, two worked examples). Skills attached to this role at runtime: ${ROLE_SKILLS[role].join(', ')} — refer to them by name only.
${DECISIONS}
${CONTEXT}
Report word count (wc -w) of the final file.`,
  { label: `prompt:${role}`, phase: 'Fix', schema: FIX_SCHEMA, effort: 'medium' })

const alignSkills = (batch, i) => agent(
`Align these skill files with SPEC v2.1 and the unified decisions: ${batch.map(s => `${REPO}/skills/${s}/SKILL.md (roles: ${(skillRoles[s] || []).join(', ')})`).join('; ')}.
For each skill: (a) every field path it cites must exist in the payload of EACH role that uses it (SPEC v2.1 §20.3a); when a skill serves several roles with different paths, give the path per role in one short line; (b) remove or reconcile any threshold or table that conflicts with the unified decisions or with the role prompt (prompts govern; a skill may say 'use the table in your role prompt'); (c) keep the frontmatter keys exactly (name, description, roles, version, source, license) and roles exactly ${JSON.stringify(Object.fromEntries(batch.map(s => [s, skillRoles[s] || []])))}; (d) body <= 900 words, own words (no copying from the original Vibe-Trading text), no per-request content.
${DECISIONS}
${CONTEXT}
Report word counts of the bodies.`,
  { label: `skills:${i}`, phase: 'Fix', schema: FIX_SCHEMA, effort: 'medium' })

const fixed = await parallel([
  ...ROLES.map(r => () => fixPrompt(r)),
  ...SKILL_BATCHES.map((b, i) => () => alignSkills(b, i)),
])
log(`fix stage: ${fixed.filter(Boolean).length}/${ROLES.length + SKILL_BATCHES.length} done`)

phase('Review')
const review = await agent(
`Cross-check ALL prompts (${ROLES.map(r => `prompts/${r}.md`).join(', ')}) and ALL skills under ${REPO}/skills/ against SPEC v2.1 (§20.3a payload contract + 보완 사항), ${REPO}/tradingbot/agents/schemas.py and the unified decisions below. Mechanically extract every backticked field path from each prompt and check it against that role's §20.3a key list (and the schema output fields); list unknown paths. Check: thresholds consistent across prompts and skills (cgo_60, news scale, horizon bands, R:R, exec cost, confidence caps, noise_check); bull/bear do not mention each other's output as an input; technical never uses a thesis; enums match the schema; abstain policy and Korean free-text rule present in every prompt; no per-request content; word counts (prompts 700-1900, skill bodies <= 900). confirmed=true only when verified. Do not edit.
${DECISIONS}
${CONTEXT}`,
  { label: 'review', phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })

phase('Verify')
const confirmed = review ? review.findings.filter(f => f.confirmed) : []
log(`review: ${review ? review.findings.length : 0} findings, ${confirmed.length} confirmed`)
let applied = null
if (confirmed.length) {
  applied = await agent(
`Apply these confirmed findings to the prompt/skill files: ${JSON.stringify(confirmed)}.
${DECISIONS}
${CONTEXT}`,
    { label: 'apply', phase: 'Verify', schema: FIX_SCHEMA, effort: 'medium' })
}
const verify = await agent(
`Mechanical verification with ${PY} (write small inline scripts, no PyYAML): (1) every ${REPO}/skills/*/SKILL.md frontmatter parses with keys name, description, roles (list), version, source, license and name == directory name; (2) skill set == ${JSON.stringify(Object.keys(skillRoles).sort())}; (3) for each role ${JSON.stringify(ROLE_SKILLS)} each skill exists and its roles list contains the role (and no extra roles); (4) all prompts exist: ${ROLES.join(', ')}; (5) word counts (prompts 700-1900; skill bodies <= 900); (6) grep prompts and skills for dates (20[0-9][0-9]-), '/USDT', 'cycle [0-9]'; (7) load the real registry: cd ${REPO} && ${PY} -c "from pathlib import Path; from tradingbot.skills import SkillRegistry; r=SkillRegistry(Path('skills')); import sys; [print(role, len(r.for_role(role)), len(r.render(role, max_tokens=24000))//4) for role in ${JSON.stringify(ROLES)}]" and confirm every role renders all its skills within 24000 tokens (compare counts with ROLE_SKILLS). Fix trivial frontmatter problems yourself; report the rest as findings (confirmed=true when verified).
${CONTEXT}`,
  { label: 'verify', phase: 'Verify', schema: REVIEW_SCHEMA, effort: 'low' })

return { fixed, review, applied, verify }
