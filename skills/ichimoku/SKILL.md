---
name: ichimoku
description: 일목균형표 삼중 필터(전환/기준선 교차·구름 위치·구름 방향)를 4h 레짐 + 1h 트리거로 읽어 롱 진입 허용 여부, 타이밍, 청산 신호를 정하는 절차
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/ichimoku/SKILL.md (MIT)"
license: MIT
---
# Ichimoku triple filter

## When to apply

Apply whenever `feature_pack.ichimoku_4h` or `feature_pack.ichimoku_1h` is present. The code has already computed the five lines (Tenkan 9, Kijun 26, Senkou A = midpoint of the two shifted forward 26, Senkou B = 52-period midpoint shifted forward 26, Chikou = close shifted back 26) and reduced them to four labels per timeframe:

- `tk_cross`: `bullish`, `bearish` or `none`, true only if the Tenkan/Kijun cross happened within the last 3 bars
- `price_vs_cloud`: `above`, `inside`, `below`, `unknown`
- `cloud_bullish`: true when Senkou A is above Senkou B at the current bar
- `cross_strength`: `strong`, `medium`, `weak`, `none` (strong = the cross occurred on the favourable side of the cloud and the cloud direction agrees)

If a label is `unknown` or the block is null, the warm-up (78 bars) was not satisfied; treat the Ichimoku filter as absent and lower `confidence` by 0.10 rather than guessing.

## The rule in one line

A bullish read requires all three conditions at once: a bullish Tenkan/Kijun cross, price above the cloud, and a bullish cloud. Two out of three is "no signal", not "weak signal". The original engine fired only on cross events; our pack preserves that by limiting `tk_cross` to the last 3 bars, so a long-standing bullish structure without a fresh cross reads as `tk_cross: none` and supports a hold, not a new entry.

## Procedure

1. 4h block = regime. `price_vs_cloud == above` with `cloud_bullish == true` is the only state in which a new long is normally allowed. `inside` means the market is deciding: cap `direction` at `flat` unless the setup is `mean_reversion` and the 1h block is fully bullish. `below` means `direction: avoid` for entries. (The head enforces "4h above the cloud" separately; your job is to make the same call from the same labels so the channels agree.)
2. 1h block = trigger. `tk_cross == bullish` with `cross_strength` in {`strong`, `medium`} inside an allowed 4h regime → `timing: now`. A bullish 1h cross with `cross_strength == weak` (cross below the cloud) → `timing: wait_confirmation`. No fresh cross but `price_vs_cloud == above` and `cloud_bullish` → `timing: wait_pullback` for a trend setup (structure is fine, the entry bar is not here yet).
3. Combine with the three-axis vote. Ichimoku agreement on both timeframes adds +1 to `score`; a 1h `tk_cross == bearish` subtracts 2; 4h `price_vs_cloud == below` subtracts 2. The resulting `score` still has to clear +3 for a long.
4. Confidence. +0.10 when both timeframes pass the triple filter; +0.05 when the 1h `cross_strength` is `strong`; −0.10 when the 4h block is `inside`; set `direction: flat` outright when the 1h cross is bearish, whatever the rest of the pack says.
5. Setup type. A fresh bullish 1h cross with 4h above the cloud is `trend_continuation`. A bullish cross while 4h is `inside` and the 1h `bb_position` is near 0 is `mean_reversion`. A `strong` 1h cross immediately after `price_vs_cloud` changed from `inside` to `above` may be labelled `breakout`, but only if the volume axis (`obv_trend`, `volume_ratio_20`) confirms.

## Stop placement

The Kijun line is the natural stop reference for an Ichimoku entry, but the pack carries relationships, not line prices. Therefore:

- Use `sl_reference: kijun` only when the payload includes an explicit Kijun price field. Then `stop_candidate_price` is that level minus a small buffer and `stop_pct` is the distance from `price`, capped at 2.0.
- Otherwise anchor the stop with `atr_pct_1h` (`stop_pct = min(2.0, 1.5 * atr_pct_1h)`, `sl_reference: atr`) or with a nearby SMC demand zone (`sl_reference: zone`). Never fabricate a Kijun value from `last_closes_1h`.
- If the implied Kijun distance would exceed 2%, the trade does not fit the cap: say so in `invalidation`, keep `direction: flat`, and let `timing: wait_pullback` express that a closer entry could work.

Targets follow the general rule: `tp_pct` at most 4.0 and at least 1.5 × `stop_pct`. The far cloud edge is not available as a number, so do not cite it as a target level.

## Open positions

For a held position, the Ichimoku exit conditions are, in order of severity:

1. 1h `tk_cross == bearish` with `cross_strength` in {`strong`, `medium`} → `exit_flag: true`, `exit_reason` in Korean naming the cross.
2. 4h `price_vs_cloud` moved to `below` → `exit_flag: true` (regime lost).
3. 4h `price_vs_cloud == inside` with `cloud_bullish == false` → not an exit by itself; note it in `invalidation` as the condition that would become one.

A weak bearish 1h cross while the 4h regime is intact is a hold with `timing: wait_confirmation` for any add-on, never an automatic exit.

## What not to do

- Do not treat a bearish triple filter as a short setup; it is `avoid` for entries and an exit cue for holdings.
- Do not read a bullish cloud alone as bullish. Cloud direction without the cross and price position is one condition of three.
- Do not promote `cross_strength: weak` to `medium` because other indicators look good; the label is computed from the cloud position at the cross bar and is not negotiable.
- Do not mention the Chikou span as evidence: the pack does not expose it.

## Output discipline

`evidence` items name the field, for example `ichimoku_4h.price_vs_cloud: above`, `ichimoku_1h.tk_cross: bullish`, `ichimoku_1h.cross_strength: strong`. `invalidation` is one Korean sentence such as "1h 전환선이 기준선을 하향 교차하거나 4h 가격이 구름 아래로 내려가면 무효" expressed in terms of these labels. `mtf_agreement` is copied from the pack, not re-derived from Ichimoku.
