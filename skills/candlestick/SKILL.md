---
name: candlestick
description: 코드가 라벨한 캔들 패턴 목록과 점수(candles_15m/candles_1h)를 추세·구조 맥락 안에서만 타이밍 보정과 보유 청산 판단에 쓰는 절차와 금지 규칙 (점수·확신도·손절은 역할 프롬프트 규칙을 따름)
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/candlestick/SKILL.md (MIT)"
license: MIT
---
# Candlestick labels as a timing modifier

## Purpose

Apply after the regime and setup reads have produced a provisional direction. Patterns adjust timing and can trigger a holding's exit; they never create a direction on their own. Code inspects only the last two bars of each timeframe and supplies:

- `feature_pack.candles_15m.patterns`, `feature_pack.candles_1h.patterns`: labels from bullish_engulfing, bearish_engulfing, hammer, shooting_star, doji, pin_bar_bull, pin_bar_bear, inside_bar, outside_bar.
- `feature_pack.candles_15m.score`, `feature_pack.candles_1h.score`: +1 per bullish and −1 per bearish pattern, summed and clipped to −3..3.

The list is the fact. You do not re-check body or wick geometry, and there is no 4h pattern field.

Precedence: your role prompt (technical.md) governs. Its step 3 (trigger), step 7 (score), step 8 (confidence), step 9 (stop) and step 10 (exits) are the rules; this skill only explains how to read patterns inside them.

## Where patterns enter the prompt steps

- **Trigger (step 3).** A confirmed trigger needs `feature_pack.candles_15m.score` at or above 0. Either `candles_15m.score` or `candles_1h.score` at or below −2 forces `wait_confirmation`.
- **Score (step 7).** Only the sign of `feature_pack.candles_1h.score` counts, as one +1 / −1 / 0 vote. Do not add the raw score, and `candles_15m.score` does not vote.
- **Confidence (step 8).** Patterns add no increment. They can never lift confidence above what regime and structure justify.

## Context decides what a pattern means

| Context | Bullish 1h pattern | Bearish 1h pattern |
|---|---|---|
| 4h regime passes step 1, 1h vote trend_up | supports `now` if the step 3 trigger also holds | no `now`; `wait_pullback`, direction unchanged unless score falls |
| 1h vote mean_reversion_long | useful confirmation that a turn has started; without one prefer `wait_confirmation` | stay `flat`; the turn has not begun |
| 4h `ema_state` bear_aligned or `ichimoku_4h.price_vs_cloud` below | ignore (counter-trend bounce) | reinforces `avoid` |

Paths in the table are under `feature_pack.` (`feature_pack.per_tf.1h.vote`, `feature_pack.per_tf.4h.ema_state`, `feature_pack.ichimoku_4h.price_vs_cloud`).

Reading rules:

- A reversal label needs something to reverse. hammer or pin_bar_bull counts only after visible weakness (`feature_pack.per_tf.1h.rsi_14` below 40 or `feature_pack.per_tf.1h.bb_position` below 0.2). shooting_star or pin_bar_bear counts only after a rise (`rsi_14` above 60 or `bb_position` above 0.8). Out of context the label is noise; note it, give it no weight.
- doji, inside_bar and outside_bar are indecision. They never move direction; on the bar you would enter they argue for `wait_confirmation`.
- bullish_engulfing on 1h with `feature_pack.per_tf.1h.volume_ratio_20` above 1.2 is the strongest bullish label here; bearish_engulfing on 1h is the strongest bearish one.
- 15m labels matter for the entry bar only. A negative `candles_15m.score` on an otherwise valid 1h long removes `now` (use `wait_pullback`); a bullish 15m label never turns `flat` into `long`.

## Stops

A reversal bar implies a natural stop under its low, but the pack carries no bar highs or lows. Never quote a price derived from an imagined wick. Use the prompt's step 9 only (a qualifying SMC zone or the ATR rule, then `tp_pct` = min(4.0, max(2 × `stop_pct`, 1.5 × `stop_pct` + 0.75)) and net R:R of at least 1.5). A pattern never changes `noise_check`, which comes only from `feature_pack.noise_ok_2pct` and `feature_pack.noise_floor_stop_pct`.

## Open positions

The `position` block has no thesis or confidence; read it with the feature pack. The pattern exit is exactly prompt step 10: `feature_pack.candles_1h.score` at or below −2 while `position.unrealized_pct` is below 0 → `exit_flag` true with a Korean `exit_reason` naming the score. The same score with the position in profit is not an exit by itself; put it in `invalidation`. Bullish labels on a holding are neutral.

## What not to do

- Do not spot patterns from `feature_pack.last_closes_1h`; closes show no wicks or bodies.
- Do not claim star formations, three-bar sequences or gap patterns: the code does not label them, and the market trades around the clock. If it is not in `patterns`, it did not happen for this call.
- Do not let one pattern override a bearish 4h regime, a mean_reversion_short vote or an extended 1h reading.
- Do not express a bearish pattern as a short. It becomes `flat`, `avoid`, a more cautious timing, or `exit_flag` on a holding.

## Evidence

Use at most one of the three evidence slots for patterns, in the format `<field path>: <value> — <짧은 한국어 해석>`, for example `feature_pack.candles_1h.patterns: [bullish_engulfing] — 1시간 상승 장악형` or `feature_pack.candles_1h.score: -2 — 약세 패턴 누적`. The other slots belong to regime and structure. The primary `invalidation` condition should be a label a later cycle can verify (a `vote`, `ema_state` or structure change), not "the pattern failed". Free text is Korean; enum values stay as the schema spells them.
