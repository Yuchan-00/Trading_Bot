---
name: crypto-derivatives
description: 무기한 선물 프리미엄(베이시스)과 펀딩비의 부호 일관성·지속 가능성을 점검해 과열·역조(백워데이션)를 해석하고, 역할 프롬프트의 군집·스퀴즈·확신도 규칙에 근거로 넣는 법
roles: [derivatives_flow]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/crypto-derivatives/SKILL.md (MIT)"
license: MIT
---
# Perp premium and funding sustainability

## Paths

derivatives_flow only: `flow_packs[symbol].perp_premium_pct`, `.funding_now`, `.funding_7d_mean`, `.funding_annualized_pct`, `.funding_regime`, `.oi_change_24h_pct`, `.oi_funding_label`, `.divergence_flags`, `.data_confidence`; cut-offs in `thresholds.*`.

Precedence: the derivatives_flow prompt governs. Posture (step 2), crowding (step 3), squeeze direction (step 4), block (step 6), washout (step 7) and confidence (step 8) are decided there. This skill explains the premium gauge and how to read it next to funding; it adds no extra notch, cap or block rule.

## The two gauges

- `perp_premium_pct` = (perp mark − spot) / spot × 100. Positive: the perpetual trades above spot, buyers in perps are paying up. Negative: the perp trades below spot, perp sellers are pressing harder than spot sellers.
- Funding is the mechanism that drags the perp back toward spot, so in a normal market premium and funding share a sign. When they disagree, one side of the market is being forced, and that disagreement is the information.
- `funding_annualized_pct` turns the 8h rate into a yearly carry cost. Very large positive values mean the long crowd pays more per year than almost any holding return could justify; such levels have historically been temporary and usually unwind with funding falling, often together with price.

## Reading the premium

| `perp_premium_pct` | reading | where it enters the prompt |
|---|---|---|
| above +0.3 | perp bid hard, leveraged chasing | one of the "exactly one" conditions for `crowding_risk` moderate (step 3) |
| about 0 to +0.3 | ordinary contango | premium family neutral |
| below 0 | perp under spot, fearful or forced perp selling | premium family leans to the fear side for posture (step 2); with `deleveraging` and negative funding it is consistent with a washout, but step 7 decides |
| `null` | not available | the premium family is missing, not neutral; cite `flow_packs[symbol].perp_premium_pct: null` if it mattered |

## Consistency checks between premium and funding

1. Premium positive while `funding_regime` is `negative` or `squeeze`: perps are bid while shorts pay. That is the profile of a short squeeze in progress. Step 4 gives `squeeze_direction` up, and step 8 takes 0.1 off confidence because the two gauges disagree in sign.
2. Premium negative while funding is `elevated_long` or `extreme_long`: longs are still paying even though the perp trades under spot. This is late-stage crowding starting to unwind. Step 3 makes `crowding_risk` high, which makes `squeeze_direction` down and `block_new_longs` true.
3. Both gauges flat and `oi_change_24h_pct` small: a quiet market with nothing to read. Expect `posture` neutral and `flow_bias` neutral; do not manufacture a lean.
4. `divergence_flags` containing `funding_up_price_flat`: longs are paying more without price following, so the crowd is growing faster than the move. Use it as evidence for the hot side and apply the step 8 deduction when it conflicts with the posture you chose; do not compute your own ratio of `funding_now` to `funding_7d_mean`.

## Sustainability reading

- Extreme positive carry (`funding_regime` extreme_long, roughly 55% a year or more) is the case step 3 already treats as high crowding. Name `flow_packs[symbol].funding_annualized_pct` in `block_reason` so the reader sees the cost.
- High carry inside `elevated_long` is worth an evidence line but does not by itself block; the block conditions are the ones in step 6.
- Deeply negative carry (`squeeze`) means shorts pay heavily; step 4 already gives `squeeze_direction` up. On its own it never yields `flow_bias` long: a washout also needs open interest to have fallen (`deleveraging`).

## Evidence

Format `<field path>: <value> — <짧은 한국어 해석>`, e.g. `flow_packs[symbol].perp_premium_pct: -0.35 — 선물이 현물보다 낮음, 공포성 매도` or `flow_packs[symbol].funding_annualized_pct: 62.1 — 롱 보유 비용 과다, 지속 불가`. At most three items; free text in Korean.

## What not to do

- Do not propose carry trades, hedges, futures shorts or option structures; the bot buys spot with market orders and nothing else.
- Do not let a hot premium raise confidence in a long; it only ever argues for crowding.
- Do not compute premium or funding from `last_closes_1h` or any price series.
- Do not cite other venues or cross-exchange spreads; you only have the data in `flow_packs`.
- Do not add notch adjustments, caps or blocks beyond the prompt's steps.
