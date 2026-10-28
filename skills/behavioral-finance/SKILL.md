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

Use this whenever you weigh what the crowd has already done: the behavioural extract, the flow view, the sentiment composite, or the bot's own measured biases. Two ideas drive it. Crowds absorb clear information slowly, so moves continue for a while; they overshoot on ambiguous information, so extremes snap back. And the decision maker is itself a crowd member with measurable habits. The checklists, counts and levels that score these ideas are in your role prompt and govern; this skill explains what they mean.

Paths per role:
- bull and bear: `feature_extract.cgo_60/rsi_5/rsi_14/bb_position/obv_trend/abnormal_volume_multiple/volume_vs_hour_avg/change_24h_pct` (the rsi, band and OBV values are 1h), `flow.*`, `sentiment.score_0_100`, `sentiment.label`, `scanner_row.factor_table.*`, `technical.*`.
- risk_manager: `account_pack.bias_severities.*`, `account_pack.decay_state`, `account_pack.noise_trade_share`, `candidates[].bull.thesis_points[]`, `candidates[].bear.death_paths[]`, `positions[].*`.
- head: `technical.labels.cgo_60/abnormal_volume_multiple/change_24h_pct`, `sentiment.composite.score_0_100`, `account_pack.bias_severities.*`, `bull.*`, `bear.*`.

An absent or `null` field is "unavailable", never "passed".

## Two mechanisms

- Underreaction → continuation. Price drifts with information the crowd has not finished pricing.
- Overreaction → reversal. Fear or euphoria pushes price past what the information supports; the snap-back is short.

One candidate cannot be both. Decide which mechanism the input describes and take its horizon from this table (15-minute bars), the same one your role prompt uses:

| Setup | `expected_horizon_bars` |
|---|---|
| Trend continuation | 16–48 |
| RSI(5) or washout rebound | 4–16 |
| Breakout | 8–32 |

Attention shortens everything: halve the horizon when the abnormal-volume multiple is above 3. Never exceed the 96-bar code time stop.

Capital-gain overhang: `cgo_60` is price against the 60-bar 1h VWAP, in percent. At +3 or more the average recent buyer is in profit and selling into strength is likely (overhang, greed side). At −3 or less many recent buyers are under water; selling pressure is often spent and a rebound can start (capitulation side). Between the two it carries no signal.

## bull: fear-side reading

The underreaction items in your role prompt are this skill's ideas in scored form: volume-confirmed momentum, a 1h RSI(5) extreme, overhang relief (`feature_extract.cgo_60` ≤ −3), attention with participation but before the crowd fully arrives, and a contrarian set (extreme fear, cold funding, capitulation posture, upward squeeze, washout). Write the matched items into `underreaction_evidence` and the paths into `thesis_points[].evidence_field`. Momentum without volume is half an item, and you must say so. A rebound thesis needs evidence the selling is done, not only that price fell.

## bear: greed-side reading

The greed flags in your role prompt are the mirror image: chasing a big 24h move without volume, overhang (`feature_extract.cgo_60` ≥ +3), euphoric leverage in `flow.*`, a divergence between price and positioning or OBV, stretched momentum oscillators, a crowd extreme in `sentiment.score_0_100`, and an attention spike. Each flag that holds is one `greed_flags` entry; the dominant one also becomes a death path (leverage → `crowded_unwind` or `liquidation_cascade`; chase or stretch → `regime_flip` with a reversal trigger). Use `probability_band: unknown`, not `low`, when the input is silent.

## risk_manager and head: the bot's own biases

The journal measures four habits on the bot's own trades and passes `none`, `mild`, `severe` or `insufficient_sample` in `account_pack.bias_severities`:

- Disposition: losers held much longer than winners.
- Overtrading: too many entries per day.
- Chasing: entries right after a sharp 1h run-up.
- Anchoring: exits clustered near the entry price.

The severity levels are computed in code; do not recompute them. What each severity does to `global.confidence_floor`, `global.entries_paused` or a position `action` is set in your role prompt. `insufficient_sample` means report, do not tighten. Reason in percent of equity, never in raw quote-currency P&L, and judge recency over the full window given, not the last trade.

For the head, the matching `debias` flags are behavioural checks on this cycle's decision: `anchored_to_entry` (the entry price used as a reason to hold), `chasing_spike` (buying a move the labels say already ran), `acting_on_sentiment_alone`, `overtrading`, `ignored_bearish_news`. Set them after deciding; a true `chasing_spike` or `acting_on_sentiment_alone` makes code downgrade the BUY, so fix the decision rather than the flag.

## What not to do

- Do not estimate crowd gauges that are not in the input.
- Do not turn a sentiment band into a target exposure; sizing belongs to the risk manager inside the hard limits.
- Do not express a greed read as a short; it is "no entry" or "exit".
- Do not average bull and bear confidence; the head classifies the disagreement instead.

## Expressing the result

Evidence-like strings use `"<field path>: <value> — <짧은 한국어 해석>"`, free text in Korean.
- bull: `underreaction_evidence`, `expected_horizon_bars` from the table, `disconfirming_evidence` with the greed items you could not rule out.
- bear: `greed_flags`, matching `death_paths[]`.
- risk_manager: `bull_point_reliability[].pushback` naming the greed item that weakens a bull point; floor and pause choices citing `account_pack.bias_severities.*`.
- head: all five `debias` booleans, `thesis.horizon_bars` consistent with the mechanism, and `reevaluation_triggers` for the overhang and attention conditions.
