---
name: technical-basic
description: 추세(EMA/ADX)·평균회귀(볼린저/RSI)·거래량(OBV) 3축 투표 규칙으로 feature_pack 라벨을 읽고 long/flat/avoid 방향과 셋업 유형을 정하는 기본 기술분석 절차
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/technical-basic/SKILL.md (MIT)"
license: MIT
---
# Three-axis technical vote

## When to apply

Use this on every call. It is your baseline read of `feature_pack.per_tf` before Ichimoku, candlestick, SMC or volatility refinements. The three axes are already computed in code and labelled per timeframe (`per_tf.15m`, `per_tf.1h`, `per_tf.4h`, `per_tf.1d` when present). Your job is to combine the labels, not to recompute them. You never see candles beyond `last_closes_1h`, so any claim about a moving average, RSI or OBV must point at the label field that carries it.

## The three axes and their fields

| Axis | Fields | Bullish reading | Bearish / blocking reading |
|---|---|---|---|
| Trend | `ema_state`, `adx_14`, `macd_hist_sign`, `price_vs_ema20_pct` | `ema_state == bull_aligned` with `adx_14 > 20` (strong above 25) and `macd_hist_sign == pos` | `bear_aligned`, or `mixed` with `adx_14 < 20` (no trend to follow) |
| Mean reversion | `rsi_14`, `rsi_5`, `bb_position` | `rsi_14 < 30` and `bb_position < 0.05` (stretched below the band) | `rsi_14 > 70` or `bb_position > 0.95` (stretched above: never buy here) |
| Volume | `obv_trend`, `volume_ratio_20` | `obv_trend == up`; `volume_ratio_20 >= 1.0` confirms | `obv_trend == down`, or `volume_ratio_20 < 0.5` on a breakout bar (unconfirmed) |

The code summarises each timeframe in `vote` with values `trend_up`, `trend_down`, `mean_reversion_long`, `mean_reversion_short`, `none`. Treat `vote` as the primary input and the raw labels as the explanation of it.

## Procedure

1. Read the 4h row first. It sets the regime: `bull_aligned` allows trend entries, `mixed` allows only mean-reversion entries, `bear_aligned` allows nothing new.
2. Read the 1h row for the setup. `trend_up` → candidate `setup_type: trend_continuation`. `mean_reversion_long` → candidate `setup_type: mean_reversion`. `trend_down` or `mean_reversion_short` → `direction: avoid` for entries.
3. Read the 15m row for timing. `trend_up` or `none` with `obv_trend != down` → `timing: now`. A 15m `trend_down` against a 1h long setup → `timing: wait_pullback` (do not buy into a falling intraday leg). `rsi_5 > 80` on 15m → `timing: wait_pullback`.
4. Apply the volume gate. A long requires `obv_trend` of `up` or `flat` on 1h; `down` on 1h and 4h together downgrades the direction to `flat`.
5. Apply the overbought veto. If 1h or 4h shows `rsi_14 > 70` or `bb_position > 0.95`, the direction is at most `flat` regardless of the trend axis. This is the one rule that overrides everything else on the long side.
6. Copy `feature_pack.mtf_agreement` into the output field `mtf_agreement` unchanged. Do not derive your own version; the code definition is the contract the entry gate checks.
7. Build `score` in −5..+5: start at 0; +2 for 4h `bull_aligned`; +1 for 1h `vote` in {`trend_up`, `mean_reversion_long`}; +1 for 1h `obv_trend == up`; +1 for 15m `vote != trend_down`. Mirror with negatives for the bearish readings. A long needs `score >= 3`; `score <= 0` means `direction: flat`; `score <= -3` means `direction: avoid`.
8. Set `confidence`. Base 0.50 for a qualifying long; +0.10 if `mtf_agreement` is true; +0.05 if 1h `adx_14 > 25`; +0.05 if 1h `volume_ratio_20 > 1.2`; −0.10 if the 15m row contradicts the 1h row; −0.15 if `per_tf.4h` is missing or any axis is `unknown`. Clamp to 0..1. Only `confidence >= 0.55` matters downstream, so do not inflate a marginal read past it.

## Stop and target

- Mean-reversion long: stop below the band excursion. Use `atr_pct_1h`: `stop_pct = min(2.0, 1.5 * atr_pct_1h)`, `sl_reference: atr`, `stop_candidate_price = price * (1 - stop_pct/100)`. Target the band midpoint: `tp_pct = min(4.0, 2 * stop_pct)`.
- Trend continuation: prefer a structural level from the SMC labels if one sits within 2% (`sl_reference: zone`); otherwise the ATR rule above. Target `tp_pct` at most 4.0 and at least 1.5 × `stop_pct`; if that ratio cannot be met inside the caps, set `timing: wait_pullback` instead of widening the target.
- `noise_check` is true only when `1.5 * atr_pct_1h < 2.0`, i.e. the cap-level stop sits outside one normal hourly swing. When it is false, the entry cannot pass, so say so in `invalidation` and keep `direction: flat`.

## Open positions

When the payload carries a position, the question changes from "enter?" to "is the structure that justified the entry still intact?". Set `exit_flag: true` with a Korean `exit_reason` when 1h `vote` has flipped to `trend_down` or `mean_reversion_short`, when 4h `ema_state` turned `bear_aligned`, or when the stored invalidation text is now satisfied by the labels. A merely `mixed` 4h with an intact 1h read is a hold, not an exit.

## What not to do

- Do not infer a trend from `last_closes_1h` when `ema_state` disagrees; the closes are context for the stop, not a second indicator.
- Do not output `direction: long` on a `mean_reversion_short` or `trend_down` 1h vote because "it is oversold on 15m".
- Do not describe anything as a short, a sell signal to profit from, or a hedge. Bearish evidence becomes `flat`, `avoid`, or `exit_flag`.
- Do not quote numbers that are not in the pack, and do not round a threshold in your favour.

## Output discipline

Every entry in `evidence` (max 3) names the field it rests on, for example `per_tf.1h.vote: trend_up`, `per_tf.4h.ema_state: bull_aligned`, `per_tf.15m.obv_trend: up`. `invalidation` is one Korean sentence in market terms that a later cycle can check against the same labels (which field, which value flips the read). `exit_reason` and `invalidation` are Korean; enum fields keep their English values exactly as listed in the schema.
