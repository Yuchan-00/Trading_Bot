---
name: smc
description: 코드가 계산한 SMC 구조 라벨(BOS/ChoCH/FVG/수요·공급 존)을 읽어 진입 허용·타이밍·구조적 손절 후보·무효화 조건·보유 청산을 정하는 법 (점수·확신도·손절·목표 공식은 역할 프롬프트 규칙을 따름)
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/smc/SKILL.md (MIT)"
license: MIT
---
# Market structure read for a spot long

## Purpose

Use whenever `feature_pack.smc_1h` or `feature_pack.smc_4h` is not null and its `structure` is not unknown. Code derives the labels from 5-bar swing fractals and 3-bar gaps; you interpret them and never re-derive breaks, gaps or zones from `feature_pack.last_closes_1h`. If both blocks are null the structure channel is silent: no zone stop, no structural credit.

Precedence: your role prompt (technical.md) governs. Steps 1, 2 and 4 (structure filters), step 7 (score), step 8 (confidence), step 9 (stop/target) and step 10 (exits) are the rules; this skill explains the labels inside them.

## Label meanings

- `structure`
  - bos_up: a close through the prior swing high within an existing uptrend (continuation).
  - choch_up: a close through the prior swing high after a downtrend (first sign of a turn).
  - bos_down / choch_down: the bearish mirrors. For a spot long they only mean no entry or exit.
  - range: no swing break in the window. It is the absence of a signal, not mildly bullish.
- `fvg_dir`: direction of the latest unfilled 3-bar gap in the last 20 bars. bullish supports a long; bearish is an unfilled gap below that price may revisit.
- `nearest_demand_zone` `[low, high]`: range of the last down bar before the latest bullish break; a retest often finds buyers. `nearest_supply_zone` is the mirror above price.
- `distance_to_demand_pct` / `distance_to_supply_pct`: % distance from `feature_pack.price` to the zone.

## Reading order

1. **4h bias (prompt step 1).** `feature_pack.smc_4h.structure` choch_down → `avoid`. bos_down → timing at most `wait_confirmation` (no `now`). bos_up or choch_up supports the regime; range is neutral and leaves the decision to the EMA and cloud conditions.
2. **1h structure (prompt steps 2 and 4).** `feature_pack.smc_1h.structure` bos_up or choch_up supports a long and is one +1 vote in step 7; bos_down or choch_down is −1, and choch_down blocks any entry. bos_up with `feature_pack.per_tf.1h.volume_ratio_20` above 1.5 is the step 2 `breakout` setup; otherwise the setup type follows the 1h vote.
3. **Timing (prompt step 4).** `feature_pack.smc_1h.fvg_dir` bearish, or `feature_pack.smc_1h.distance_to_supply_pct` below 1.0 (supply overhead), → `wait_pullback`. Bullish structure with price far above the demand zone also argues for `wait_pullback`: a retest is the better fill. `now` still needs the step 3 trigger.
4. **Confidence (prompt step 8).** The only SMC increment is +0.05 for `fvg_dir` bullish together with bos_up on 1h. Add nothing else.

## Stop from the zone (prompt step 9)

- Use `sl_reference` zone when `feature_pack.smc_1h.distance_to_demand_pct` is at most 1.8 and at least `feature_pack.noise_floor_stop_pct`. Place `stop_candidate_price` just below `feature_pack.smc_1h.nearest_demand_zone[0]` and take `stop_pct` from that price; it must stay at or below 2.0.
- Otherwise use the ATR rule: `stop_pct` = min(1.5 × `feature_pack.atr_pct_1h`, 2.0), `sl_reference` atr. kijun is not available (no line price in the pack). Never stretch a stop beyond 2.0 to reach a zone; a zone that is too far means `wait_pullback`.
- `noise_check` is `feature_pack.noise_ok_2pct` true AND `stop_pct` at or above `feature_pack.noise_floor_stop_pct`. A zone that sits closer than the noise floor is inside ordinary hourly swing and fails it. When false: confidence at most 0.50, `wait_pullback`.

## Target from supply

`tp_pct` = min(4.0, max(2 × `stop_pct`, 1.5 × `stop_pct` + 0.75)). If `feature_pack.smc_1h.distance_to_supply_pct` is smaller, lower `tp_pct` to it and keep `target_candidate_price` consistent. Then check net R:R = (`tp_pct` − 0.30) / (`stop_pct` + 0.30) at or above 1.5. If the supply cap breaks it, tighten the stop only; if the tightened stop would fall below `feature_pack.noise_floor_stop_pct`, downgrade to `wait_pullback` or `flat` and say why. Never widen a stop to make the ratio work.

## Open positions

The `position` block has prices, `unrealized_pct` and `bars_held`, no thesis or confidence. The structure exit is prompt step 10: `feature_pack.smc_1h.structure` choch_down → `exit_flag` true, Korean `exit_reason` naming the field. A 1h bos_down, or a 4h bos_down / choch_down while the 1h has not flipped, is not an exit by itself: state it in `invalidation` as the trigger to watch and give no add-on timing.

## What not to do

- Do not infer breaks, gaps or zones yourself; cite only labelled fields.
- Do not express bearish structure as a short; the only bearish outputs are `flat`, `avoid` and `exit_flag`.
- Do not exceed 2.0 for `stop_pct` or 4.0 for `tp_pct`.
- Do not cite a zone that is null.

## Evidence

At most three items, each `<field path>: <value> — <짧은 한국어 해석>`, for example `feature_pack.smc_1h.structure: choch_up — 1시간 추세 전환 신호` or `feature_pack.smc_1h.distance_to_demand_pct: 0.6 — 수요 존 근접`. Prefer the items that decided direction and `sl_reference`. `invalidation` is one Korean sentence naming the zone or structure label, such as a 1h close back below the demand-zone low or a new choch_down on 1h.
