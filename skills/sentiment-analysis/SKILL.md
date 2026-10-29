---
name: sentiment-analysis
description: 코드가 계산한 종합심리(공포·탐욕/펀딩 z/롱숏 백분위/헤드라인 극성)를 역발상 지표로 읽어 뉴스 톤 산출과 총괄의 확신도 보정에만 쓰는 법
roles: [news_sentiment, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/sentiment-analysis/SKILL.md (MIT)"
license: MIT
---
# Crowd sentiment as a contrarian modifier

## When to apply

Sentiment never opens a trade. It can lower confidence, support a red line and explain a HOLD; it cannot create a BUY and it cannot raise a confidence. Where a number here differs from your role prompt, the role prompt governs.

Paths per role:
- news_sentiment: you do not receive the composite. You read `headlines[].*` and `feed_health.*` and produce `market_tone` and `market_note`; code turns your event scores into the `headline_polarity` component later.
- head: `sentiment.composite.score_0_100`, `sentiment.composite.label`, `sentiment.composite.components.*`; per symbol `sentiment.event_signal[symbol].*` (the event blend, not the crowd score).

## What the composite is

Code builds it as a weighted blend of four parts, each normalised to 0..100:

- `fear_greed` (weight 0.4): the public crypto Fear & Greed index.
- `funding_z` (0.2): current perpetual funding against its own seven-day history.
- `ls_pctile` (0.2): long/short account-ratio percentile.
- `headline_polarity` (0.2): decayed polarity of recently scored headlines.

A missing part is dropped and the other weights are renormalised, so a score resting on one component is weaker evidence than one resting on four. Check which `components` are `null` before leaning on the score, and never cite a `null` component. No other crowd gauge exists.

`label` bands, computed by code: below 20 `extreme_fear`, below 40 `fear`, below 60 `neutral`, below 80 `greed`, otherwise `extreme_greed`; `null` score gives `unknown`. Code (G1) already removes candidates above 80 unless the setup is mean reversion, so any survivor in that zone is fragile.

## news_sentiment: producing market_tone

1. Score each headline on the fixed scale (−1.0, −0.66, −0.33, 0, +0.33, +0.66, +1.0); zero is a valid score, never pad a weak item.
2. `market_tone` (−1..1) is the lean of the batch weighted by how many independent sources agree, not the loudest title. One extreme item on a thin batch moves tone to roughly ±0.3.
3. Heat check: when `feed_health.items_fetched` is unusually large and nearly every title leans the same way, the unanimity is itself a caution. Write it into `market_note` as one-sidedness instead of pushing the tone further.
4. Freshness from `headlines[].age_h` only: above 24 is `stale`, above 168 is also capped at |score| 0.33. Never compute ages from timestamps.
5. An empty or failing feed (`feed_health.items_fetched` 0, or `feed_health.errors` with nothing usable) is no information, not a calm market: `market_tone` 0 and say so in `feed_health_note`.
6. Name the batch size in `market_note`; a tone from five titles is not market-wide truth.

## head: reading the composite

1. Read `sentiment.composite.label` together with `regime.btc_regime_4h`. Greed inside a confirmed uptrend is normal; fear inside a downtrend is normal and is not a buy reason. The contrarian reading is meaningful only when sentiment and trend disagree, and even then it is context, not a trigger.
2. Extreme fear never adds confidence. It may make a structural setup (`technical.direction` long with `technical.mtf_agreement` true, or `flow.washout_setup` true) easier to accept as reasoning; the number still comes from your role prompt.
3. Greed costs confidence through the deduction in your role prompt (it uses `sentiment.composite.score_0_100` above 70). Do not apply a second, separate sentiment penalty on top.
4. Sentiment can sit at an extreme for many cycles. Never argue "it must revert soon"; treat it as a level condition, not a timing signal.
5. If sentiment is the main reason you want to act, you are acting on sentiment alone: set `debias.acting_on_sentiment_alone` true and do not buy.
6. When a thesis depends on the crowd staying calm, express it as a red line on `composite_sentiment` (for example `>` 80 on a trend long).

## What not to do

- Do not compute or restate a composite yourself; cite `sentiment.composite.score_0_100` as given.
- Do not map sentiment bands to a target exposure. Size is bounded by `risk.max_size_pct`, `gate.size_multiplier` and the hard limits.
- Do not express a bearish read as a short. It becomes no entry, or SELL on a held position.

## Expressing the result

- news_sentiment: `events[].score`, `events[].stale`, `market_tone`, `market_note` (one Korean sentence naming the dominant event types, the batch size and whether the feed looked one-sided), `feed_health_note` (Korean; an empty feed stated as missing information).
- head: cite `sentiment.composite.score_0_100` or `sentiment.composite.label` in `rationale` when it changed your confidence, add a `composite_sentiment` entry to `thesis.red_lines` where the thesis depends on tone, set `debias.acting_on_sentiment_alone`, and list sentiment-driven HOLDs in `hold_reasons`.
