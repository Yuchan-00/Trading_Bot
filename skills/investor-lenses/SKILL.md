---
name: investor-lenses
description: 세 가지 사고 절차 — 파생흐름의 탐욕·공포 진자(posture), 약세 변론가의 역산(death_paths), 총괄의 스태킹·불일치 분류 — 를 현물 롱 전용 15분 봇에 맞게 적용하는 법
roles: [derivatives_flow, bear, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/investor-lenses/SKILL.md (MIT)"
license: MIT
---
# Reasoning lenses — pendulum, inversion, stacking

A lens is a fixed order of questions plus veto rules, applied *after* the evidence is frozen. Only three lenses survive here, because the other nine need financial statements the bot never sees. No investor names appear in any output; a verdict is "under this procedure", never "someone would buy this".

## Rules shared by every role

1. The evidence is the labelled JSON you received. Do not reinterpret it into numbers that are not there.
2. A hard veto is not weighed against positives. When one fires, stop scoring and report it.
3. "Unknown is not low." A probability you cannot assess is `unknown`, never `low`.
4. Never average two verdicts or two confidences. Disagreement is information; locate it.
5. Every claim names the input field it rests on. A claim without a field is dropped.

## derivatives_flow: pendulum (posture)

Apply to each symbol in the batch. You take the temperature of positioning, never forecast a turn.

Observable families, each from `flow_pack`:
- Funding: `funding_regime` (`extreme_long` means > +0.05%/8h, `squeeze` means < −0.02%/8h), `funding_annualized_pct`.
- Leverage build: `oi_change_24h_pct`, `oi_funding_label` (`leveraged_long_buildup` = OI +5% with funding > +0.03%).
- Positioning: `ls_label` (`crowded_long` > 2.0, `crowded_short` < 0.8), `perp_premium_pct`.
- Behaviour: `divergence_flags`, `cascade_risk`, `nearest_long_liq_below_pct`.

Procedure:
1. Count how many families point the same way. `posture` may be `euphoria` or `capitulation` only when at least three families agree; `greed`/`fear` needs two; one family alone yields `neutral` with `confidence` ≤ 0.5.
2. Map posture to action, not timing: `euphoria` → `crowding_risk: "high"`, `flow_bias: "avoid"`, and `block_new_longs: true` with `block_reason` naming the fields; `greed` → `crowding_risk` at least `moderate`; `fear` → `flow_bias: "neutral"` unless a washout is present; `capitulation` → the only bullish reading, expressed through `washout_setup`.
3. `washout_setup: true` requires all of: `oi_funding_label == "deleveraging"`, `funding_regime` in {`negative`, `squeeze`}, `cascade_risk != "high"`, and `data_confidence == "high"`. Otherwise it is false even if the posture is `capitulation`.
4. Extremes persist. A `euphoria` reading is a reason to refuse new longs, never a claim that price falls next bar; say so in `market_wide_note`.

Do not: infer posture from `oir_1pct` or `spread_bps` alone (they are not positioning), set `washout_setup` on `data_confidence: "low"`, or let a bullish posture override `stop_inside_cascade`.

## bear: inversion (death paths)

You are a veto layer, not a mirror of the bull. The question is how this long dies within the time-stop window (the next 96 bars), given that the hard stop bounds loss at 2% *unless* price jumps through it.

Procedure:
1. Restate the implied thesis as one falsifiable sentence from `technical.setup_type`, `technical.invalidation` and `flow.flow_bias`. If it cannot be stated, that is the first path (`family: "other"`).
2. Enumerate at least three `death_paths` from *different* `family` values. Standard mappings:
   - `liquidation_cascade`: `flow.nearest_long_liq_cluster_pct_below` ≤ 2, `flow.cascade_risk`, `flow.stop_inside_cascade`.
   - `news_shock`: `news.risk_event`, negative `news.events[].score` ≤ −0.5, `feed_health_note` showing a broken feed.
   - `regime_flip`: `regime_pack.btc_regime_4h`, `regime_pack.fused`, `corr_to_btc_1h` > 0.8, `hv_pct` > 80.
   - `crowded_unwind`: `flow.posture` in {`greed`, `euphoria`}, `flow.crowding_risk`, `composite_sentiment` > 80, `feature.cgo_60` > 20 (percent above the 60-bar VWAP).
   - `data_incident`: any channel that abstained, `flow.data_confidence: "low"`, a `technical.confidence` below 0.55.
3. Each path carries `observable_trigger` (a field and a level), `probability_band`, and `time_to_impact_bars` (`None` only when truly unknowable). A worry without an observable trigger is not a path; name it in `strongest_point` only if it is the real objection.
4. Hard vetoes → `verdict: "decline"`: a path with gap-through-stop potential (cascade within 2% or live `risk_event`) rated `high`; a `high` path with no trigger the bot can watch; or a thesis that cannot be stated.
5. Otherwise choose `survivable` when every `high`/`mid` path has a trigger already listed in `monitoring_conditions`, else `survivable_if_monitored` and fill `monitoring_conditions` with those triggers.
6. `disconfirming_evidence` must contain one entry per positive you can see in the packs (a trend vote, a washout flag, a catalyst). `what_would_disprove_bear` names the observation that would retire your strongest path.

Do not: list five variants of one worry, decline because "crypto is risky", confuse ordinary volatility (covered by the 2% stop) with gap risk, or rate a path `low` because you did not examine it.

## head: stacking and disagreement

You receive bull, bear and risk independently. Treat them as three lenses that are allowed to disagree.

Procedure:
1. Read each in isolation; note its strongest point before comparing.
2. Classify every bull-versus-bear conflict into exactly one `disagreement_class`: `horizon` (same facts, different bar count — compare `bull.expected_horizon_bars` with `bear.death_paths[].time_to_impact_bars`), `caliber` (they measure different things, e.g. `feature.cgo_60` versus `flow.posture`), `risk_preference` (same expected outcome, different tolerance for a 2% stop-out), `information` (one side needs a field that is missing or abstained). Use `none` only when verdicts agree.
3. Quote the losing side's single best sentence verbatim in `losing_side_strongest_point`. If you cannot state it fairly, do not trade.
4. `decisive_factor` names the field that settled the class, not the side that "won on points". A 2-to-1 count of arguments is not evidence.
5. `confidence` ≤ min(`bull.confidence`, `risk.confidence`) + 0.05, and never above the cycle's `confidence_floor` rule. A risk `veto` or `bear.verdict: "decline"` is final; a `news.risk_event` symbol is never `BUY`.
6. On `BUY`, write `thesis.assumptions` as checkable market sentences tied to `red_lines` metrics (`price`, `unrealized_pct`, `funding_rate`, `btc_return_1h_pct`, `composite_sentiment`, `hours_held`); the bear's monitored triggers belong in `reevaluation_triggers`.

Do not: pick the side whose action you prefer and then rationalise (lens shopping), blend confidences, or treat two channels agreeing on the same candles as independent confirmation.
