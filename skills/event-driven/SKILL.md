---
name: event-driven
description: 헤드라인을 고정 7단계 점수와 이벤트 유형으로 라벨링하고, 코드가 감쇠시킨 이벤트 신호를 강세 변론과 총괄 결정에서 촉매·위험으로만 쓰는 법
roles: [news_sentiment, bull, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/event-driven/SKILL.md (MIT)"
license: MIT
---
# Event-driven — label once, decay in code, never trade on news alone

The original strategy let a model score news, stored the scores with a knowable date, decayed them over time and blended the result with a technical signal. Here the division of labour is strict: the news role produces labels, code does every number (decay, dedup filter, per-symbol `event_signal`, the headline share of `composite_sentiment`), and the bull and head roles consume the decayed result as context. Nobody in this chain fetches text or computes a decay.

## news_sentiment procedure

You receive `headlines[]` (title only, `id`, `ts`, `source`, `source_grade`), the watchlist with an alias map, the current time, `feed_health`, and `prev_events` already scored. Produce one `events[]` entry per headline you keep.

1. Skip anything whose `id` or whose (symbol, event type, hour) already appears in `prev_events`; do not rescore it.
2. `score` on the fixed scale only: +1.0 extreme positive (major listing on a top venue, decisive regulatory approval), +0.5 clearly positive, +0.2 mildly positive, 0.0 noise, −0.2 mildly negative, −0.5 clearly negative (enforcement action, outage, large unlock), −1.0 catastrophic (exploit with lost funds, delisting, insolvency, stablecoin depeg). Intermediate values are allowed but stay honest: code drops |score| < 0.2, so do not inflate a 0.1 to keep it alive.
3. `event_type` from the enum; `other` is for headlines that are real but unclassifiable, not for guesses.
4. `symbols` only from the alias map; a market-wide item carries an empty list and feeds `market_tone`.
5. `summary` ≤ 20 words, Korean, restating the title without adding facts that are not in it.
6. `stale: true` when `ts` is more than 24h before the current time (the same cutoff the news prompt uses: older than 24h is not intraday-actionable); such events still get a score (code decays them) but cannot drive `risk_event`. Past 7 days, additionally cap |score| at 0.33.
7. `risk_event: true` and the symbol in `risk_event_symbols` when a non-stale headline is `hack`, `delisting`, `exchange` (incident at a venue holding the pair), `regulatory` with enforcement wording, a stablecoin depeg, or an `etf` decision, and the symbol is on the watchlist or held. This flag only ever removes trades.
8. `market_tone` is your read of market-wide items (−1..1), not an average of every score; `market_note` is one Korean sentence.
9. `feed_health_note` must say what arrived: items fetched, newest and oldest timestamps, errors. An empty batch is "no information", never "calm".

Source weighting: `source_grade` low → cap |score| at 0.5 unless two independent sources carry the same event.

## What code does afterwards (so you do not)

- Half-life by type, in hours: `sentiment` and `technical_break` 6–12; `listing`, `exchange`, `hack` ~24; `macro`, `etf` ~48; `regulatory`, `protocol` ~72. Decayed value = score × exp(−λ × hours), λ = ln2 / half-life.
- Per-symbol `event_signal` = clipped sum of decayed scores in [−1, 1]; the top-3 decayed summaries travel with it.
- A suggested blend 0.6 × technical + 0.4 × event is shown to the head as information only; it is not a decision rule.

## bull procedure

Use events as catalysts, never as the thesis.

1. A `catalysts[]` entry needs a non-stale event with `score` ≥ +0.5 for this symbol, or `event_signal` ≥ +0.3; cite the summary and the field.
2. Underreaction check: a positive event is a catalyst only if `feature.change_24h_pct` has not already run past 15% and `feature.abnormal_volume_multiple` shows participation (≥ 1.5). A big move on no volume is the crowd pricing it, not underreaction; say so in `underreaction_evidence`.
3. Every negative event with `score` ≤ −0.2 on the symbol, and any `market_tone` below −0.3, goes into `disconfirming_evidence`.
4. `thesis_points[]` must rest on `technical.*` or `flow.*` fields; an event may support a point via `evidence_field` but cannot be the only point. A case built on headlines alone caps `case_strength` at 2 and `confidence` at 0.5.
5. `expected_horizon_bars` should not exceed the catalyst's decay: roughly 24–48 bars for `sentiment`, up to 96 for `listing`/`exchange`, longer only when the technical setup, not the news, justifies it.

## head procedure

1. `news.risk_event` on a symbol → `action` is never `BUY`; for a held symbol, decide between `SELL` and `HOLD` with the event as the lead item in `hold_reasons` (or in `rationale` if you sell).
2. If the only bullish channel is `event_signal` or `composite_sentiment`, set `debias.acting_on_sentiment_alone: true` and do not buy. If a negative event exists and the decision still buys, `debias.ignored_bearish_news` must be true and `rationale` must say why the event is already priced or irrelevant.
3. Where a catalyst matters to the thesis, write the assumption as a checkable sentence ("no new negative event on the symbol") and add a `red_lines` entry on `composite_sentiment` when the thesis depends on tone.
4. Stale or market-wide events may lower `confidence`; they never raise it.

## What not to do

- Do not score from memory of a project or coin; only the title in front of you.
- Do not fabricate events to fill a quiet feed, and do not read quiet as safe.
- Do not output an aggregate number for a symbol (that is code's `event_signal`) or restate the 0.6/0.4 blend as a verdict.
- Do not treat a bearish headline as a short signal; the only bearish actions are no entry and exit.
- Do not let one source's phrasing set a ±1.0; extremes need facts in the title (amount lost, venue name, decision wording), not adjectives.
