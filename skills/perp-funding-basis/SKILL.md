---
name: perp-funding-basis
description: 펀딩비·미결제약정·프리미엄 라벨을 군집(크라우딩) 심리로 읽어 현물 롱 진입 차단·워시아웃 셋업·약세 사망경로로 변환하는 법
roles: [derivatives_flow, bear]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/perp-funding-basis/SKILL.md (MIT)"
license: MIT
---
# Funding rate and open interest as a crowding gauge

## When to apply

Apply whenever the flow pack carries funding data. Everything here is computed in code; interpret labels, do not derive them. The bot only buys spot, so the questions are: is the crowd already leaning long (do not join), has it just been flushed (the one allowed contrarian setup), or is there nothing to say (neutral).

Inputs for the derivatives_flow role (per symbol in `flow_pack`): `funding_now`, `funding_7d_mean`, `funding_annualized_pct`, `funding_regime`, `oi_change_24h_pct`, `oi_funding_label`, `perp_premium_pct`, `ls_ratio`, `ls_label`, `divergence_flags`, `data_confidence`.

Inputs for the bear role: the frozen `derivatives_flow` view for the symbol (`posture`, `crowding_risk`, `squeeze_direction`, `block_new_longs`, `washout_setup`, `flow_bias`, `evidence`). The bear never sees the raw pack; it cites the analyst's labels.

## Definitions (read only, never recompute)

- Funding is a per-8h decimal; `funding_annualized_pct` = rate × 3 × 365 × 100.
- Positive funding means long holders pay. Large positive funding is a running cost for the long crowd, so it reads as crowding, not strength.
- `funding_regime` bands on the 8h rate: `extreme_long` above +0.05%, `elevated_long` +0.02% to +0.05%, `neutral` −0.01% to +0.02%, `negative` −0.02% to −0.01%, `squeeze` below −0.02%.
- `oi_funding_label`: `leveraged_long_buildup` = OI up more than 5% in 24h with funding above +0.03%; `short_buildup` = OI up more than 5% with negative funding; `deleveraging` = OI down more than 5%.
- `ls_label`: `crowded_long` when `ls_ratio` > 2.0, `crowded_short` when < 0.8.

## Mapping funding to `posture`

| `funding_regime` | persistence check | `posture` |
|---|---|---|
| `extreme_long` | any | `euphoria` |
| `elevated_long` | `funding_now` ≥ `funding_7d_mean` | `greed` |
| `elevated_long` | `funding_now` < `funding_7d_mean` | `neutral` (cooling) |
| `neutral` | any | `neutral` |
| `negative` | any | `fear` |
| `squeeze` | `funding_now` ≤ `funding_7d_mean` | `capitulation` |
| `squeeze` | `funding_now` > `funding_7d_mean` | `fear` (recovering) |

Persistence matters: one hot print after a flat week is less crowded than a week-long elevated mean. When `funding_7d_mean` is `None`, skip the check and lean one notch toward `neutral`. An `unknown` value of `funding_regime`, `oi_funding_label` or `ls_label` counts as `neutral` and triggers no adjustment anywhere below.

## Derivatives_flow procedure

1. Set `posture` from the table.
2. `crowding_risk`: `high` when `funding_regime == "extreme_long"` or `oi_funding_label == "leveraged_long_buildup"` or `ls_label == "crowded_long"` with `funding_regime` in {`elevated_long`, `extreme_long`}; `moderate` when exactly one of `elevated_long`, `crowded_long`, or `perp_premium_pct` > 0.3 holds; otherwise `low`.
3. `squeeze_direction`: `down` when `crowding_risk == "high"` (the crowded side is long, so the forced move is down); `up` when `funding_regime == "squeeze"` or `oi_funding_label == "short_buildup"` or `ls_label == "crowded_short"`; else `none`.
4. `block_new_longs`: true when `crowding_risk == "high"`, or when `divergence_flags` contains `price_up_oi_down` together with `funding_regime` in {`elevated_long`, `extreme_long`} (price rising on shrinking participation while the remaining longs pay up). Write `block_reason` in Korean and name the deciding field.
5. `washout_setup`: true only when all hold: `oi_funding_label == "deleveraging"`, `funding_regime` in {`negative`, `squeeze`}, `ls_label != "crowded_long"`, and `data_confidence == "high"`. This is the only contrarian long the channel may flag; it means forced sellers are gone, not that price will rise.
6. `flow_bias`: `avoid` when `block_new_longs`; `long` only when `washout_setup` or (`posture` in {`fear`, `neutral`} and `oi_funding_label` in {`neutral`, `short_buildup`} and `crowding_risk == "low"`); else `neutral`.
7. `confidence`: start at 0.7 with `data_confidence == "high"`, 0.4 with `low`. Add 0.1 when funding, OI label and `ls_label` all point the same way; subtract 0.15 when `divergence_flags` is non-empty and conflicts with `posture`. With `data_confidence == "none"` emit `posture: "neutral"`, `flow_bias: "neutral"`, `confidence` ≤ 0.2 and a single evidence item `data_confidence: none`.
8. `evidence`: up to three strings of the form `field: value`, for example `funding_regime: extreme_long`, `oi_change_24h_pct: 7.2`.
9. `market_wide_note` (Korean, one line): say whether crowding is broad (most symbols `greed`/`euphoria`) or symbol-specific. Top-level `data_confidence` is `low` if any symbol in the batch is below `high`.

## Bear procedure

1. If the flow view shows `crowding_risk: "high"` or `posture` in {`greed`, `euphoria`}, open a `death_paths` entry with `family: "crowded_unwind"`. `description` (Korean) explains that the long crowd pays to stay in and a small dip forces them out. `observable_trigger` names a visible field and level, for example funding turning from positive to negative within the holding horizon, or `oi_change_24h_pct` dropping below −5. `probability_band`: `high` when `squeeze_direction == "down"`, `mid` for `greed` alone.
2. Add each of `posture: greed/euphoria`, `crowding_risk: high`, `block_new_longs: true` to `greed_flags`.
3. When the flow view is `washout_setup: true`, put it in `disconfirming_evidence` (it argues against the bear) and in `what_would_disprove_bear`: funding staying negative while OI rebuilds.
4. `monitoring_conditions` (when `verdict == "survivable_if_monitored"`): phrase them so the head can turn them into a `funding_rate` red line, for example "펀딩비 8h > 0.0005 이면 재평가".
5. A `crowded_unwind` path with `probability_band: "high"` plus any second `high` path justifies `verdict: "decline"`.

## What NOT to do

- Do not treat high positive funding as bullish confirmation; it is a cost borne by the crowd you would be joining.
- Do not call `washout_setup` on funding alone; it needs the OI drop.
- Do not infer funding direction from `last_closes_1h` or any price series; cite the labels.
- Do not express a short view; bearish readings become `flow_bias: "avoid"`, `block_new_longs: true`, or a bear death path.
- Do not emit `flow_bias: "long"` when `data_confidence` is `low`.

## Output mapping summary

Derivatives_flow: `posture`, `crowding_risk`, `squeeze_direction`, `block_new_longs`, `block_reason`, `washout_setup`, `flow_bias`, `confidence`, `evidence`, `market_wide_note`, `data_confidence`.
Bear: `death_paths` (`crowded_unwind`), `greed_flags`, `disconfirming_evidence`, `what_would_disprove_bear`, `monitoring_conditions`, `verdict`.
