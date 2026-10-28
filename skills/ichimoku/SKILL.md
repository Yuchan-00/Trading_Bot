---
name: ichimoku
description: 일목균형표 라벨(전환/기준선 교차·구름 위치·구름 방향·교차 강도)을 4h 레짐 + 1h 트리거로 읽어 롱 진입 허용, 타이밍, 보유 청산 신호를 정하는 절차 (점수·확신도·손절·목표는 역할 프롬프트 규칙을 따름)
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/ichimoku/SKILL.md (MIT)"
license: MIT
---
# Ichimoku: cloud regime and cross trigger

## Purpose

Use whenever `feature_pack.ichimoku_4h` or `feature_pack.ichimoku_1h` is not null. Code has already built the lines (conversion 9, base 26, leading spans shifted 26 ahead, the second span from a 52-bar midpoint) and reduced each timeframe to four labels:

- `tk_cross`: bullish / bearish / none. Only a conversion-base cross within the last 3 bars counts; an old cross reads none.
- `price_vs_cloud`: above / inside / below / unknown.
- `cloud_bullish`: true when the first leading span is above the second at the current bar; null if not computable.
- `cross_strength`: strong / medium / weak / none. strong means the cross happened on the favourable side of the cloud and the cloud direction agrees.

A null block or an `unknown` label means warm-up was not met: the Ichimoku filter is absent (no trigger, no `now`), never assumed favourable.

Precedence: your role prompt (technical.md) governs. Its step 1 (4h regime), step 3 (trigger), step 7 (score), step 8 (confidence), step 9 (stop/target) and step 10 (exits) are the rules; this skill explains how to read the labels inside them.

## Core idea

The bullish case is three conditions together: a fresh bullish cross, price above the cloud, and a bullish cloud. Any two without the third is no signal, not a weak signal. A long-running bullish structure without a fresh cross supports holding, not a new entry.

## Reading order

1. **4h regime (prompt step 1).** A new long needs `feature_pack.ichimoku_4h.price_vs_cloud` above together with the 4h EMA condition. above with `cloud_bullish` false is a thinner regime: trust it less and prefer `wait_confirmation`. inside with `cloud_bullish` true allows at most `wait_confirmation`; inside with a bearish cloud permits no new long. below means `avoid` for entries.
2. **1h trigger (prompt step 3).** The trigger is `feature_pack.ichimoku_1h.tk_cross` bullish with `cross_strength` strong or medium, plus the 15m conditions the prompt lists. A bullish cross with `cross_strength` weak (it happened below or inside the cloud) is not a trigger: `wait_confirmation`. No fresh cross but 1h price above a bullish cloud means the structure is fine and the entry bar has not arrived: `wait_pullback` for a trend setup.
3. **Bearish cross.** `feature_pack.ichimoku_1h.tk_cross` bearish means there is no trigger (prompt step 3), so never `now`; direction and timing still come from the prompt's steps 3 and 7.
4. **Score and confidence.** The trigger is one of the +1/−1 votes in prompt step 7. The only Ichimoku confidence increment is the +0.10 in step 8 for `cross_strength` strong. Add nothing of your own.
5. **Setup type.** A fresh strong or medium 1h cross with the 4h above a bullish cloud fits `trend_continuation`. Setup type still follows the prompt's step 2 mapping from `feature_pack.per_tf.1h.vote` and the SMC breakout rule; Ichimoku does not create a breakout label on its own.

## Stops and targets

A base-line stop would be natural, but the payload carries relationships only, never line prices. So `sl_reference` kijun is not used, and you never estimate a base-line value from `feature_pack.last_closes_1h`. Use the prompt's step 9: a qualifying SMC demand zone (`zone`) or `stop_pct` = min(1.5 × `feature_pack.atr_pct_1h`, 2.0) (`atr`), then `tp_pct` = min(4.0, max(2 × `stop_pct`, 1.5 × `stop_pct` + 0.75)) and net R:R = (`tp_pct` − 0.30) / (`stop_pct` + 0.30) of at least 1.5. The far cloud edge is not a number in the pack; do not cite it as a target.

`noise_check` comes only from `feature_pack.noise_ok_2pct` and `feature_pack.noise_floor_stop_pct` (prompt step 5); a strong cross does not rescue a failing noise test.

## Open positions

The `position` block carries prices, `unrealized_pct` and `bars_held` but no thesis or confidence. The Ichimoku exits are exactly those of prompt step 10:

1. `feature_pack.ichimoku_1h.tk_cross` bearish with `cross_strength` strong → `exit_flag` true, Korean `exit_reason` naming the cross.
2. `feature_pack.ichimoku_4h.price_vs_cloud` below together with `feature_pack.per_tf.1h.vote` trend_down → `exit_flag` true.

Weaker deterioration is not an exit by itself: 4h below with an intact 1h vote, 4h inside with a bearish cloud, or a medium/weak bearish 1h cross. Record it in `invalidation` as the condition that would become an exit, and give no `now` timing.

## What not to do

- Do not treat a bearish filter as a short setup; it means `avoid` for entries and an exit cue for holdings.
- Do not read a bullish cloud alone as bullish; it is one condition of three.
- Do not promote `cross_strength` weak to medium because other labels look good.
- Do not cite the lagging span: the pack does not expose it.

## Evidence

Each item is `<field path>: <value> — <짧은 한국어 해석>`, for example `feature_pack.ichimoku_4h.price_vs_cloud: above — 4시간 구름 위` or `feature_pack.ichimoku_1h.cross_strength: strong — 구름 위 강한 교차`. `invalidation` is one Korean sentence in label terms, such as a 1h bearish cross or the 4h price moving below the cloud. `mtf_agreement` is copied from `feature_pack.mtf_agreement`, never re-derived from Ichimoku.
