---
name: technical-basic
description: 추세(EMA/ADX/MACD)·평균회귀(RSI/볼린저)·거래량(OBV) 3축 라벨을 시간대별로 읽어 레짐·셋업·타이밍을 정하는 기본 기술분석 절차 (점수·확신도·손절·목표는 역할 프롬프트 규칙을 따름)
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/technical-basic/SKILL.md (MIT)"
license: MIT
---
# Three-axis label read

## Purpose

Baseline read before the other technical skills. Every indicator is already computed and labelled by code in `feature_pack.per_tf.<tf>` (keys `15m`, `1h`, `4h`, `1d`, only the ones available). You interpret labels; you never rebuild an indicator from `feature_pack.last_closes_1h`.

Precedence: your role prompt (technical.md) governs. The score recipe (step 7), confidence recipe (step 8), stop/target formulas (step 9), noise test (step 5) and exit conditions (step 10) live there and are not restated or changed here. This skill explains the labels and how to weigh them.

## The three axes

Each `feature_pack.per_tf.<tf>` row carries:

| Axis | Fields | Supports a long | Argues against |
|---|---|---|---|
| Trend | `ema_state`, `adx_14`, `macd_hist_sign`, `price_vs_ema20_pct` | `ema_state` bull_aligned, `adx_14` above 20, `macd_hist_sign` pos | `ema_state` bear_aligned; mixed with `adx_14` below 20 is "no trend" |
| Mean reversion | `rsi_14`, `rsi_5`, `bb_position` | `rsi_14` below 30 with `bb_position` below 0.05 (stretched under the lower band) | `rsi_14` above 70 or `bb_position` above 0.95 (stretched near or over the upper band) |
| Volume | `obv_trend`, `volume_ratio_20` | `obv_trend` up; `volume_ratio_20` at or above 1.0 | `obv_trend` down; a breakout bar with `volume_ratio_20` below 0.5 |

The code condenses each row into `vote` (trend_up / trend_down / mean_reversion_long / mean_reversion_short / none) with conjunctive rules: trend_up needs bull_aligned, `adx_14` above 20 and `macd_hist_sign` pos (trend_down mirrors it); mean_reversion_long needs `rsi_14` below 30 and `bb_position` below 0.05; mean_reversion_short needs `rsi_14` above 70 and `bb_position` above 0.95. Treat `vote` as the primary fact and the raw labels as its explanation.

## How the timeframes divide the work

- **4h = regime.** `feature_pack.per_tf.4h.ema_state` bull_aligned permits a new long; mixed permits one only when `feature_pack.per_tf.1d.ema_state` is bull_aligned (the same condition code uses for `mtf_agreement`); bear_aligned permits nothing new. The Ichimoku cloud condition of prompt step 1 must also hold.
- **1h = setup.** `feature_pack.per_tf.1h.vote` maps to `setup_type` as in prompt step 2: trend_up → trend_continuation, mean_reversion_long → mean_reversion, trend_down or mean_reversion_short → no new entry. Breakout is decided by the SMC rule in step 2, not by this skill.
- **15m = entry bar.** `feature_pack.per_tf.15m.vote` trend_down against a 1h long setup means do not buy into a falling intraday leg: no `now`, use `wait_pullback`. A 15m `rsi_5` above 80 is a stretch worth naming in `invalidation`; prompt steps 3–6 set `timing`.

## Weighing rules inside the prompt steps

1. **Volume gate.** A long wants `feature_pack.per_tf.1h.obv_trend` up or flat. When 1h and 4h `obv_trend` are both down, the volume axis contradicts the trend axis; keep the step 7 score unchanged, name the contradiction in `invalidation` and prefer `flat` or a `wait_*` timing.
2. **Overbought caution.** On 1h, `rsi_14` above 70 or `bb_position` above 0.95 while the vote is still trend_up means the move is extended: no `now` (use `wait_pullback`). If both conditions hold the code vote is already mean_reversion_short and prompt step 2 forbids an entry.
3. **Trend strength.** `adx_14` and `volume_ratio_20` shape timing and your trust in the vote; they add no confidence increment of their own. Confidence comes only from the prompt's step 8 recipe.
4. **mtf_agreement.** Copy `feature_pack.mtf_agreement` unchanged into the output. Never derive your own version; the gate checks the code definition.
5. **Missing rows.** A missing `feature_pack.per_tf.4h` or an `unknown` `ema_state` triggers the prompt's data check (step 0). Missing is never neutral.

## Stops and noise

Use the prompt's step 9 formulas only: zone stop when the SMC demand zone qualifies, otherwise `stop_pct` = min(1.5 × `feature_pack.atr_pct_1h`, 2.0) with `sl_reference` atr, and `tp_pct` = min(4.0, max(2 × `stop_pct`, 1.5 × `stop_pct` + 0.75)). Net R:R = (`tp_pct` − 0.30) / (`stop_pct` + 0.30) must be at least 1.5; if a closer target breaks it, tighten the stop only, never widen it.

`noise_check` is `feature_pack.noise_ok_2pct` true AND `stop_pct` at or above `feature_pack.noise_floor_stop_pct`. Do not substitute your own ATR arithmetic: code gate G1 recomputes the same test, so a pass the fields do not support is simply wrong. When it is false, confidence is at most 0.50 and timing is `wait_pullback`.

## Open positions

The payload's `position` block has `entry_price`, `stop_loss_price`, `take_profit_price`, `last_mark`, `unrealized_pct`, `bars_held`, `opened_ts` and nothing else: no thesis, no confidence. Your hold/exit read rests on the feature pack and those numbers.

Set `exit_flag` true only on the prompt's step 10 conditions. Three-axis deterioration that falls short of them (1h vote flipping to trend_down while the 4h cloud still holds, 4h `ema_state` turning mixed or bear_aligned) is not an exit by itself: name it in `invalidation` as the condition that would become one. A mixed 4h with an intact 1h read is a hold.

## What not to do

- Do not infer a trend from `feature_pack.last_closes_1h` when `ema_state` disagrees; the closes are context only.
- Do not call a long on a trend_down or mean_reversion_short 1h vote because the 15m looks oversold.
- Do not describe anything as a short or a hedge. Bearish evidence becomes `flat`, `avoid`, a `wait_*` timing, or `exit_flag`.
- Do not quote a number absent from the payload or round a threshold in your favour.

## Evidence

At most three items, each `<field path>: <value> — <짧은 한국어 해석>`, with the full path, for example `feature_pack.per_tf.1h.vote: trend_up — 1시간 추세 상승 투표` or `feature_pack.per_tf.4h.ema_state: bull_aligned — 4시간 정배열`. `invalidation` is one Korean sentence naming the field and value whose change voids the read. All free text is Korean; enum values stay exactly as the schema spells them.
