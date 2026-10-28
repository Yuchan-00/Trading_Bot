---
name: crypto-derivatives
description: 무기한 선물 프리미엄(베이시스)과 펀딩비의 일관성을 점검해 과열·역조(백워데이션) 상태를 판정하고 유지 불가능한 극단값을 역발상 경고로 쓰는 법
roles: [derivatives_flow]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/crypto-derivatives/SKILL.md (MIT)"
license: MIT
---
# Perp premium and funding sustainability

## When to apply

Apply when `flow_pack.perp_premium_pct` or `flow_pack.funding_annualized_pct` is present. This skill adds two things on top of the funding-band reading: a basis (premium) regime, and a sustainability test that flags funding levels no market can hold for long. Only the perpetual contract matters here; dated futures, options and arbitrage trades are outside this bot and must not appear in your output.

Inputs: `perp_premium_pct`, `funding_now`, `funding_7d_mean`, `funding_annualized_pct`, `funding_regime`, `oi_change_24h_pct`, `data_confidence`.

## Definitions

- `perp_premium_pct` = (perp mark − spot) / spot × 100. Positive is contango-like (the perp trades above spot), negative is backwardation-like.
- Funding is the mechanism that pulls the perp back to spot, so premium and funding normally agree in sign. Disagreement is information.
- `funding_annualized_pct` = 8h rate × 3 × 365 × 100. Treat it as a cost-of-carry figure: above roughly 50% per year the long crowd is paying more than any reasonable holding return, which historically resolves by funding collapsing, often with price.

## Basis regime from `perp_premium_pct`

| `perp_premium_pct` | regime | reading for a spot buyer |
|---|---|---|
| > +0.5 | hot contango | leveraged chasing; raise `crowding_risk` one notch |
| +0.1 to +0.5 | normal contango | no adjustment |
| −0.1 to +0.1 | flat | low conviction either way |
| < −0.1 | backwardation | forced or fearful selling in perps; supports `posture: "fear"`; combined with `oi_funding_label: deleveraging` it supports `washout_setup` |

## Sustainability test

1. Annualized funding above 50% (8h rate above about +0.046%) or `funding_regime == "extreme_long"`: label this unsustainable. The expected path is funding reverting toward zero. Set `squeeze_direction: "down"` and `block_new_longs: true` with a Korean `block_reason` citing `funding_annualized_pct`.
2. Annualized funding below −30% (8h rate below about −0.027%): equally unsustainable on the short side. Set `squeeze_direction: "up"`. This supports `washout_setup` only with the OI condition from the funding skill; on its own it raises `posture` to `fear` or `capitulation` and leaves `flow_bias` at `neutral`.
3. Acceleration: when `funding_now` exceeds 2 × `funding_7d_mean` and both are positive, the crowd is piling in faster than the weekly norm. Raise `crowding_risk` one notch even if the absolute band is only `elevated_long`.

## Consistency checks

- Premium positive but funding `negative` or `squeeze`: the perp is bid while shorts pay, which usually means a short squeeze in progress. Prefer `squeeze_direction: "up"` and lower `confidence` by 0.1 because the two gauges disagree.
- Premium negative but funding `elevated_long` or `extreme_long`: longs still pay while the perp trades under spot; this is late-stage crowding being unwound. Set `crowding_risk: "high"` and `block_new_longs: true`.
- Premium and funding both flat with `oi_change_24h_pct` within ±2: quiet market. `posture: "neutral"`, `flow_bias: "neutral"`, and keep `confidence` modest (≤ 0.6) because there is simply nothing to read.
- `perp_premium_pct` is `None`: skip this skill's adjustments entirely and say so in `evidence` (`perp_premium_pct: null`).

## What NOT to do

- Do not propose carry trades, hedges, futures shorts or option structures; the bot cannot execute any of them.
- Do not convert an unsustainable negative funding reading into `flow_bias: "long"` by itself; the washout requires OI to have fallen.
- Do not compute premium from `last_closes_1h`; the closes are spot context only.
- Do not let a hot premium raise `confidence` in a long; it only ever raises `crowding_risk`.
- Do not mention exchanges other than the one whose public data you are given, and do not reference cross-venue spreads.

## Output mapping summary

`crowding_risk` (notch adjustments), `squeeze_direction`, `block_new_longs`, `block_reason` (Korean, cites `funding_annualized_pct` or `perp_premium_pct`), `posture` (fear/capitulation under backwardation), `confidence` (−0.1 on gauge disagreement), `evidence` items such as `perp_premium_pct: -0.35` and `funding_annualized_pct: 62.1`.
