---
name: event-driven
description: 헤드라인을 고정 7단계 점수와 이벤트 유형으로 라벨링하고, 코드가 감쇠시킨 이벤트 신호를 강세 변론과 총괄 결정에서 촉매·위험으로만 쓰는 법
roles: [news_sentiment, bull, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/event-driven/SKILL.md (MIT)"
license: MIT
---
# Event-driven: label once, decay in code, never trade on news alone

The underlying idea is that the market absorbs news with a lag, so a scored event can add a little edge to a setup that already exists. Here the work is split strictly. The news role labels headlines. Code does every number afterwards: dedup memory, decay, the per-symbol `event_signal`, the headline share of the sentiment composite. Bull and head consume the decayed result as context. Nobody in the chain fetches text, computes an age or computes a decay.

## Shared vocabulary

- Score scale, fixed: −1.0, −0.66, −0.33, 0, +0.33, +0.66, +1.0. Strong negative means score ≤ −0.66, negative ≤ −0.33, positive ≥ +0.33. Zero is a valid score for noise or opinion.
- Freshness comes from the code-given `age_h`: `age_h` > 24 means `stale` (still scored, not intraday-actionable); `age_h` > 168 also caps |score| at 0.33. Never subtract timestamps yourself.
- Where the role prompt sets a cap or a level differently, the role prompt governs.

Paths per role:
- news_sentiment: `headlines[].id/ts/age_h/source/source_grade/title`, `aliases`, `feed_health.*`, `prev_events[]`.
- bull: `news_events[].score/event_type/stale/age_h/headline_id/summary`, `news_context.market_tone/risk_event_for_symbol/feed_health_note`, `event_signal.score/n_events`, plus `technical.*`, `feature_extract.*`, `flow.*`.
- head: per `approved[]` entry `news.events[]`, `news.market_tone`, `news.risk_event_for_symbol`; cycle-wide `sentiment.event_signal[symbol].score/n_events/combined_score`, `sentiment.composite.*`.

## news_sentiment

1. Skip any headline whose `headlines[].id` already appears in `prev_events[]`. A headline with the same symbol and `event_type` as a `prev_events[]` entry is a follow-up: keep it only if its title adds a new fact, else skip it. Never rescore old events and never compare `ts` values; freshness comes from `age_h`.
2. Score what the title states, not what you remember about the project. An extreme needs facts in the title (an amount, a venue, a decision), not adjectives. Catastrophic: exploit with lost funds, delisting, insolvency, stablecoin depeg. Clearly negative: enforcement, outage, large unlock. The positive side mirrors this: decisive approval or a major listing at the top, routine partnership news near +0.33. A `headlines[].source_grade` of `low` caps |score| at 0.33 unless a second independent source carries the same event.
3. `event_type` from the enum; `other` is for real but unclassifiable items, never a guess.
4. `symbols` only through `aliases`; a market-wide item carries an empty list and informs `market_tone`.
5. `summary` restates the title in Korean without adding facts.
6. `risk_event` follows the type-and-score rule of your role prompt and only ever removes trades. ETF decisions are the one type that qualifies in either direction (|score| ≥ 0.66): a decisive approval also makes the next hours gap-prone, and the 2% stop cannot absorb a gap.
7. `feed_health_note` says what arrived. An empty batch is "no information", never "calm".

## What code does afterwards

- `event_signal.score` = clip(Σ score × 0.5^(age_h / 12), −1, 1): a single 12-hour half-life, so an event's weight halves roughly every 48 bars. `score` is `null` when there are no events.
- For the head, `sentiment.event_signal[symbol].combined_score` = 0.6 × `technical.score`/5 + 0.4 × event score. It is reference information, not a decision rule.

## bull

Events are catalysts, never the thesis.

1. A `catalysts[]` entry needs a `news_events[]` item for this symbol with `score` ≥ +0.33 and `stale` false, cited by `headline_id` and `event_type`. A positive `event_signal.score` supports it; on its own it is not a catalyst.
2. Underreaction check: the event is still worth something only if the crowd has not already priced it. A large `feature_extract.change_24h_pct` or `feature_extract.abnormal_volume_multiple` above 3 says the crowd arrived first; use the levels in your role prompt.
3. Every non-stale negative item (`score` ≤ −0.33) and a clearly negative `news_context.market_tone` belong in `disconfirming_evidence`. A `news_context` of `null` is missing evidence, also disconfirming.
4. `thesis_points` rest on `technical.*`, `feature_extract.*`, `flow.*` or `scanner_row.*`; an event may support a point but never be the only one. A case built on headlines alone stays weak.
5. A catalyst never lengthens `expected_horizon_bars`; the horizon comes from the setup table in your role prompt.

## head

1. `news.risk_event_for_symbol` true → never `BUY`. A held symbol with a fresh risk event leads the SELL-or-HOLD discussion in `rationale` or `hold_reasons`.
2. A non-stale `news.events[]` item with `score` ≤ −0.66 disqualifies a candidate; one with `score` ≤ −0.33 lowers confidence as your role prompt says. If you still buy with a negative event present, `debias.ignored_bearish_news` must be true and `rationale` must say why it is irrelevant or priced.
3. If the only bullish channel is `sentiment.event_signal[symbol]` or `sentiment.composite`, set `debias.acting_on_sentiment_alone` true and do not buy.
4. When a catalyst matters to the thesis, write a checkable assumption ("`news.risk_event_for_symbol` stays false") and consider a `composite_sentiment` red line.
5. Stale or market-wide events may lower confidence; they never raise it.

## What not to do

- Do not fabricate events for a quiet feed, and do not read quiet as safe.
- Do not output a per-symbol aggregate; that is code's `event_signal`.
- Do not restate the 0.6/0.4 blend as a verdict.
- Do not treat a bearish headline as a short; the only bearish actions are no entry and exit.
