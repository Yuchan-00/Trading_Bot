---
name: behavioral-finance
description: 과소반응(모멘텀)·과잉반응(반전)·처분효과·군집 심리를 피처팩 필드로 읽는 강세/약세 체크리스트와 리스크·총괄용 탈편향 규칙
roles: [bull, bear, risk_manager, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/behavioral-finance/SKILL.md (MIT)"
license: MIT
---
# Crowd behaviour read from labelled features

## When to apply

Apply when the payload contains the behavioural extract (`feature_extract.cgo_60`, `rsi_5`, `abnormal_volume_multiple`, `change_24h_pct`; the head gets `cgo_60`, `abnormal_volume_multiple`, `change_24h_pct` under `technical.*`), the flow view (`flow.posture`, `flow.crowding_risk`, `flow.squeeze_direction`), `scanner_row.factor_table.funding_z`, the composite `sentiment` (`sentiment.score_0_100`; head: `sentiment.composite.score_0_100`), or, for risk and head, `account_pack.bias_severities`. Cite the path your own payload uses; an absent field is "unavailable", never "passed". The ideas: crowds underreact to clear information (so trends continue for a while) and overreact to ambiguous information (so extremes revert), and the decision maker is itself a crowd member with known biases.

## Two mechanisms, one horizon rule

- Underreaction → continuation. Information is absorbed slowly; price drifts in the direction of the news. On this bot's clock that is a 2h–12h window, i.e. 8–48 bars of 15 minutes.
- Overreaction → reversal. Fear or euphoria pushes price past what the information supports; the snap-back happens on the very short horizon (a few bars to one day) after an extreme.

A single candidate cannot be both at once. Decide which mechanism the packs describe, then pick its horizon. Attention shortens everything: when `abnormal_volume_multiple` > 3, halve any continuation horizon.

Capital-gain overhang: `cgo_60` = price / 60-bar hourly VWAP − 1, in percent; use ±3% as the working band on this window. Above +3 the average recent buyer is in profit and disposition selling is likely on strength; below −3 most recent buyers are under water, selling pressure is often exhausted, and a base can form.

## Fear-side checklist (bull)

Count the items that hold. Fewer than 3 means the behavioural case is weak and `case_strength` should not exceed 2.

1. Continuation with confirmation: `technical.setup_type` is `trend_continuation` or `breakout`, `technical.mtf_agreement` is true, `scanner_row.factor_table.momentum_4h` > 0, and `scanner_row.factor_table.volume_ratio` ≥ 1.0 or `feature_extract.volume_vs_hour_avg` ≥ 1.0.
2. Short-term exhaustion rebound: `feature_extract.rsi_5` (1h) < 10, with `technical.direction` not `avoid`.
3. Overhang relief: `feature_extract.cgo_60` ≤ −3.
4. Contrarian positioning: `sentiment.label` is `extreme_fear`, or `scanner_row.factor_table.funding_z` < −1, or `flow.squeeze_direction` is `up`, or `flow.posture` is `capitulation`.
5. Flush already happened: `flow.washout_setup` is true.

Write the matched items into `underreaction_evidence` and the pack-field citations into `thesis_points[].evidence_field`. Set `expected_horizon_bars` from the mechanism (continuation 8–48, rebound 4–16), halved under abnormal volume.

## Greed-side checklist (bear)

Each item that holds becomes a `greed_flags` entry; 3 or more should push the verdict to `decline` unless `what_would_disprove_bear` is already visible in the packs.

1. Chase: `feature_extract.change_24h_pct` > 15 without `feature_extract.abnormal_volume_multiple` ≥ 2.
2. Disposition overhang: `feature_extract.cgo_60` ≥ +3 while `feature_extract.obv_trend` (1h) is not `up`.
3. Euphoric leverage: `scanner_row.factor_table.funding_z` > 1, or `flow.posture` is `euphoria`, or `flow.crowding_risk` is `high`, or `flow.block_new_longs` is true.
4. Divergence: `flow.evidence` names price rising against falling open interest, or funding rising while price is flat.
5. Stretched: `feature_extract.rsi_14` > 70 and `feature_extract.bb_position` > 0.95.
6. Crowd extreme: `sentiment.score_0_100` > 80.
7. Attention spike: `feature_extract.abnormal_volume_multiple` > 3 (also halves any surviving horizon).

Turn the dominant flag into a `death_paths` entry: euphoric leverage → family `crowded_unwind` or `liquidation_cascade`; chase or stretched → `regime_flip` with a trigger such as "1h close back below the 15m session VWAP". Give `time_to_impact_bars`, and use `probability_band: unknown` rather than `low` when the packs are silent.

## Decision-maker biases (risk_manager and head)

The journal measures four biases on the bot's own trades and passes severities in `account_pack.bias_severities` (`none` / `mild` / `severe` / `insufficient_sample`):

| Bias | Signal in the account pack | Rule |
|---|---|---|
| Disposition | losing trades held ≥ 1.2× (mild) or ≥ 1.5× (severe) as long as winners | risk: no `hold` on a position whose `assumption_status` contains `broken`; recommend `exit`, do not wait for the stop |
| Overtrading | > 8 entries per day mild, > 12 severe | risk: `confidence_floor` 0.75 on severe; `entries_paused` if combined with `decay_state` `decayed` |
| Chasing | > 30% of entries after a +3% 1h run-up (mild), > 50% (severe) | risk: `confidence_floor` 0.75; head: `debias.chasing_spike` true blocks BUY |
| Anchoring | > 30% of exits within ±0.3% of entry | head: never cite the entry price as a reason to hold; `debias.anchored_to_entry` true on any such thought |

`insufficient_sample` means report, do not tighten. Judge recency over the full window code gives you, never by the last trade. Reason in percent-of-equity terms, never in raw quote-currency P&L.

## What not to do

- Do not estimate crowd gauges that are not in the packs; only the fields named above exist.
- Do not translate a sentiment band into a target exposure; sizing is the risk manager's `max_size_pct` inside the hard limits.
- Do not treat one checklist item as a trigger; three is the minimum on either side.
- Do not express a greed read as a short; it is "no entry" or "exit".
- Do not average bull and bear confidence; the head classifies the disagreement instead.

## Expressing the result

- bull: `case_strength` (1–5, checklist count capped as above), `underreaction_evidence`, `expected_horizon_bars`, `disconfirming_evidence` (the greed items you could not rule out), `confidence`.
- bear: `greed_flags`, `death_paths[]` with `family`, `observable_trigger`, `probability_band`, `time_to_impact_bars`; `verdict`; `monitoring_conditions` when `survivable_if_monitored`.
- risk_manager: `global.confidence_floor`, `global.entries_paused`, `global.pause_reason`; per candidate `conditions` and `required_monitoring` (for example a funding ceiling); per position `action` and `reason`; `bull_point_reliability[].pushback` naming the greed flag that weakens a bull point.
- head: all five `debias` booleans answered from this checklist (overhang and attention read from `technical.*`, crowd score from `sentiment.composite.score_0_100`); `thesis.horizon_bars` consistent with the mechanism; `thesis.red_lines` using `funding_rate`, `composite_sentiment` or `unrealized_pct`; `reevaluation_triggers` for the attention and overhang conditions.
