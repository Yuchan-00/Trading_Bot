---
name: candlestick
description: 코드가 라벨한 캔들 패턴 목록과 점수(candles_15m/candles_1h)를 추세·구조 맥락 안에서만 타이밍·확신도 보정에 쓰는 절차와 금지 규칙
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/candlestick/SKILL.md (MIT)"
license: MIT
---
# Candlestick pattern reading

## When to apply

Apply after the three-axis vote and the Ichimoku filter have set a provisional `direction`. Candlestick labels are a timing and confidence modifier, never the reason for a direction on their own. The code inspects only the last two bars of each timeframe and hands you:

- `candles_15m.patterns`, `candles_1h.patterns`: a list drawn from `bullish_engulfing`, `bearish_engulfing`, `hammer`, `shooting_star`, `doji`, `pin_bar_bull`, `pin_bar_bear`, `inside_bar`, `outside_bar`
- `candles_15m.score`, `candles_1h.score`: bullish +1 / bearish −1 per pattern, summed and clipped to −3..+3

The detectors behind the labels use the classic geometry: a doji is a body under 10% of the bar range; a hammer or pin bar has a wick at least 2× the body on one side and almost none on the other; an engulfing bar's body covers the prior bar's body with opposite colour. You do not re-check these; the list is the fact.

## How much weight a pattern carries

| Context (from `per_tf` and Ichimoku) | Bullish pattern on 1h | Bearish pattern on 1h |
|---|---|---|
| 4h `bull_aligned`, 1h `vote == trend_up` | confirms `timing: now`, +0.05 confidence | downgrade `timing` to `wait_pullback`; no direction change |
| 4h `mixed`, 1h `mean_reversion_long` (`rsi_14 < 30`, `bb_position < 0.05`) | the trigger for `timing: now`; without one, `wait_confirmation` | hold at `flat`; reversal not started |
| 4h `bear_aligned` or 4h `price_vs_cloud == below` | ignore (counter-trend bounce bait) | reinforces `avoid` |
| Open position | neutral | `score <= -2` on 1h plus a broken 1h vote → `exit_flag` consideration |

Rules of thumb:

- A reversal pattern needs something to reverse. A `hammer` or `pin_bar_bull` counts only after a decline is visible in the labels (1h `rsi_14 < 40` or `bb_position < 0.2`). A `shooting_star` or `pin_bar_bear` counts only after a rise (1h `rsi_14 > 60` or `bb_position > 0.8`). Otherwise the pattern is noise and contributes nothing.
- `doji`, `inside_bar` and `outside_bar` are indecision or range labels. They never move `direction`; they can move `timing` to `wait_confirmation` when they appear on the bar you would otherwise enter on.
- `bullish_engulfing` on 1h with `volume_ratio_20 > 1.2` is the strongest single bullish pattern in this pack; `bearish_engulfing` on 1h is the strongest bearish one.
- 15m patterns matter only for the entry bar: a bearish 15m pattern on an otherwise valid 1h long means `timing: wait_pullback`; a bullish 15m pattern never upgrades a `flat` read to `long`.

## Scoring contribution

Add `candles_1h.score` to your working `score` only when its sign agrees with the provisional direction from the trend and Ichimoku reads; when it disagrees, subtract 1 and move `timing` one step more cautious (`now` → `wait_pullback` → `wait_confirmation`). Ignore `candles_15m.score` for `score`; use it for `timing` only. The final `score` must still respect the −5..+5 range and the +3 threshold for a long.

Confidence: +0.05 for an agreeing 1h pattern in the right context, −0.05 for a disagreeing one, 0 for indecision labels. A pattern can never lift `confidence` above what the trend and multi-timeframe reads justify.

## Stops from patterns

A reversal bar gives a natural stop: the low of a `hammer` or `pin_bar_bull`, or the low of the engulfed range. The pack does not carry bar lows, so you cannot cite that price. Anchor the stop with `atr_pct_1h` instead (`stop_pct = min(2.0, 1.5 * atr_pct_1h)`, `sl_reference: atr`) or with an SMC demand zone (`sl_reference: zone`), and state in `invalidation` that a 1h close below the pattern bar's low (as it will be visible through `bb_position`/`vote` flipping) voids the read. Do not set `sl_reference: zone` or quote a `stop_candidate_price` derived from an imagined wick.

## What not to do

- Do not spot patterns yourself from `last_closes_1h`. Closes alone cannot show wicks or bodies, and the schema wants claims that name a pack field, not a visual impression.
- Do not infer star patterns, three-soldier sequences or gap-based formations: the market trades around the clock, true gaps are rare on short bars, and the code does not label them. If it is not in `patterns`, it did not happen for the purposes of this call.
- Do not let a single pattern override an overbought veto (`rsi_14 > 70`, `bb_position > 0.95`) or a bearish 4h regime.
- Do not express a bearish pattern as a short. It is `flat`, `avoid`, a more cautious `timing`, or an `exit_flag` on a holding.
- Do not report a pattern on the 4h timeframe: the pack has none, so such a claim would be fabricated.

## Output discipline

Cite patterns by field, e.g. `candles_1h.patterns: [bullish_engulfing]` or `candles_1h.score: -2`, as at most one of the three `evidence` slots; the other slots belong to the trend and structure reads. Pattern reasoning may appear in the Korean `invalidation` or `exit_reason` text, but the primary invalidation condition should be a label that a later cycle can verify (a `vote` or `ema_state` flip), not "the pattern failed". Enum values stay exactly as the schema spells them.
