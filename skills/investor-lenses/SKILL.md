---
name: investor-lenses
description: 세 가지 사고 절차 — 파생흐름의 탐욕·공포 진자(posture), 약세 변론가의 역산(death_paths), 총괄의 스태킹·불일치 분류 — 를 현물 롱 전용 15분 봇에 맞게 적용하는 법
roles: [derivatives_flow, bear, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/investor-lenses/SKILL.md (MIT)"
license: MIT
---
# Reasoning lenses: pendulum, inversion, stacking

A lens is a fixed order of questions applied after the evidence is frozen. Only three lenses fit a bot that sees prices, derivatives data and headlines but no financial statements. No investor names appear in any output. Every number, band and veto level lives in your role prompt; this skill supplies the order of thought, and where the two differ the role prompt governs.

## Shared rules

1. The evidence is the labelled JSON you received; never turn it into numbers that are not there.
2. A hard veto is not traded off against positives. When one fires, stop weighing and report it.
3. Unknown is not low. A band you could not assess is `unknown`.
4. Never average two verdicts or two confidences. A disagreement is information; locate it.
5. Every claim names the payload path it rests on, written as `"<field path>: <value> — <짧은 한국어 해석>"` wherever the schema has an evidence-like list. A claim with no path is dropped.

Paths per role, in one line each:
- derivatives_flow: `flow_packs[symbol].*` and `thresholds.*`.
- bear: `technical.*`, `feature_extract.*`, `flow.*`, `news_events[]`, `news_context.*`, `event_signal.*`, `sentiment.*`, `scanner_row.factor_table.*`, `scanner_row.liquidity.*`, `regime.*`, `position.*`, `corr_to_open`, `hv_pct`.
- head: per `approved[]` entry `technical.*` (feature labels under `technical.labels.*`), `flow.*`, `news.*`, `bull.*`, `bear.*`, `risk.*`, `gate.*`; cycle-wide `sentiment.composite.*`, `sentiment.event_signal[symbol].*`, `regime.*`, `account_pack.*`, `abstained_channels`.

## derivatives_flow: the pendulum

You read the temperature of positioning; you never forecast the turn.

1. Count families pointing the same way. The families are the four your role prompt names (funding, open interest, long/short ratio, premium), read from `flow_packs[symbol].funding_regime`, `.oi_funding_label`, `.ls_label` and `.perp_premium_pct`. Use the agreement counts in your role prompt to choose between `neutral`, `greed`/`fear` and `euphoria`/`capitulation`.
2. Posture is a level, not a clock. Crowds can stay at an extreme for many cycles; an `euphoria` reading is a reason to refuse longs, never a claim that price falls next bar. Say so in `market_wide_note` when most of the batch is hot.
3. Posture does not set the flags by itself. `crowding_risk`, `block_new_longs` and `washout_setup` follow the explicit conditions in your role prompt; the pendulum only tells you which side of them you are probably on.
4. The one bullish reading is the flushed crowd, and it is expressed only through `washout_setup`, which rests on funding and open interest, never on clusters.
5. Estimated liquidation clusters (`.est_liq_clusters`, `.nearest_long_liq_below_pct`) are projections at fixed distances from a VWAP. They may block or warn; they never justify an entry, and their spacing never escalates `cascade_risk` (copy the pack value as your prompt says).

Do not infer posture from `.oir_1pct` or `.spread_bps` alone; they describe the book, not positioning.

## bear: inversion

Assume the long was entered and ask how it dies before its horizon ends. Ordinary noise inside the stop is not a death; a jump through the stop, or a stop-out before the thesis had time, is.

1. State the implied thesis in one falsifiable sentence from `technical.setup_type`, `technical.invalidation` and `flow.flow_bias`. If you cannot, that failure is itself the first path.
2. Walk the families in turn and look for the field that would carry each death: `liquidation_cascade` (`flow.stop_inside_cascade`, `flow.nearest_long_liq_cluster_pct_below`, `flow.cascade_risk`); `news_shock` (`news_context.risk_event_for_symbol`, `news_events[].score` with `stale` false, `news_context.feed_health_note`); `regime_flip` (`regime.verdict`, `regime.btc_regime_4h`, `regime.fused`, `regime.density`, `regime.btc_shock`, `hv_pct`); `crowded_unwind` (`flow.posture`, `flow.crowding_risk`, `sentiment.score_0_100`, `feature_extract.cgo_60` ≥ +3, `scanner_row.factor_table.funding_z`, `corr_to_open`); `data_incident` (any `null` channel, `flow.confidence`, `technical.confidence`, `technical.noise_check`, `scanner_row.liquidity.ok`). Band levels and bar ranges are in your role prompt.
3. Each path needs an observable trigger: a field and a level the bot can watch. A worry with no trigger is not a path.
4. Distinct families, not variants. Five phrasings of "funding is hot" are one path.
5. Turn every visible positive against itself: for each one, name the field that weakens it. If none exists, that positive is what would disprove you.
6. A bearish verdict means "no entry" or "exit". There is no short.

Do not decline because "crypto is risky", and do not rate a path `low` because you did not look at it.

## head: stacking and disagreement

Bull, bear and risk arrive independently. Treat them as three lenses that are allowed to disagree.

1. Read each alone and note its strongest point before comparing.
2. Classify the bull-versus-bear conflict into one `disagreement_class`: `horizon` (same facts, different clocks: compare `bull.expected_horizon_bars` with `bear.death_paths[].time_to_impact_bars`); `caliber` (they measure different things, e.g. `technical.labels.cgo_60` against `flow.posture`); `risk_preference` (same expected path, different tolerance for a stop-out); `information` (one side relies on a field that is `null` or abstained). Use `none` only when the verdicts agree.
3. Quote the losing side's single best sentence verbatim in `losing_side_strongest_point`. If you cannot state it fairly, do not trade.
4. `decisive_factor` names the field that settled the class, never the side with more arguments. A two-to-one count is not evidence.
5. Confidence comes from your role prompt (it starts from min(`bull.confidence`, `risk.risk_confidence`) + 0.05 and must clear max(`risk.confidence_floor`, `gate.confidence_floor`)). A risk veto or a bear `decline` never reaches you as a buy; `news.risk_event_for_symbol` true is never `BUY`.
6. On a `BUY`, the bear's watchable triggers become `reevaluation_triggers`, and the thesis assumptions are checkable sentences tied to the allowed `red_lines` metrics.

Do not pick the side whose action you prefer and then rationalise it, do not blend confidences, and do not count two channels reading the same candles as independent confirmation.
