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

Use this whenever the payload carries the composite (`score_0_100`, `label`, `components`; the head finds it under `sentiment.composite.*`, other roles under `sentiment.*`) or `market_tone`, and (for the news role) whenever you must turn a batch of headlines into `market_tone`. Cite the path your own payload uses. Sentiment never opens a trade by itself. It shifts confidence, tightens or loosens the bar for entry, and supplies red lines.

## What the number is

Code builds the composite before you see it. It is a weighted blend, each part normalised to 0..100:

- `components.fear_greed` (weight 0.4): the public crypto Fear & Greed index, cached hourly.
- `components.funding_z` (0.2): current perpetual funding versus its own 7-day distribution.
- `components.ls_pctile` (0.2): long/short account ratio percentile.
- `components.headline_polarity` (0.2): decayed polarity of recent scored headlines.

Missing parts are dropped and the remaining weights are renormalised, so a score built from one component is weaker evidence than one built from four. Check which keys are `null` before leaning on the score.

Band map (the `label` field is derived from it):

| score | label | reading |
|---|---|---|
| 0–20 | extreme_fear | crowd capitulated; contrarian support for a long only with a structural setup |
| 20–40 | fear | mildly supportive; neutral otherwise |
| 40–60 | neutral | sentiment carries no information; decide on other channels |
| 60–80 | greed | headwind; demand stronger structure and a tighter horizon |
| 80–100 | extreme_greed | code already drops non-mean-reversion candidates; treat any survivor as fragile |

Only these four components exist; do not invent any other crowd gauge.

## Procedure for the news role

You produce `market_tone` (-1..1) and `market_note`; you never produce the 0..100 composite. Code converts your per-event scores into `headline_polarity`.

1. Score each headline on the fixed seven-step scale (+1.0 / +0.5 / +0.2 / 0 / -0.2 / -0.5 / -1.0) and classify `event_type`. Entries with |score| < 0.2 will be discarded in code, so do not pad them.
2. Derive `market_tone` from the batch, not from the loudest item: it is the volume-of-attention-weighted lean of the scored events. One extreme headline on a thin feed should move tone to about ±0.3, not ±1.0.
3. Heat check. If `feed_health.items_fetched` is well above the usual batch size and nearly every title points the same way, that unanimity is itself a caution flag: write it in `market_note` as crowd one-sidedness rather than raising `market_tone` further.
4. Freshness. Compare each `headlines[].ts` with `now_ms`: older than 24 hours → `stale: true` (still scorable, not intraday-actionable); older than 7 days → `stale: true` and score magnitude capped at 0.2.
5. Feed health. An empty or failing feed (`feed_health.errors` non-empty, `items_fetched` = 0) means no information, not a calm market. Set `market_tone` to 0 and say so explicitly in `feed_health_note`.

## Procedure for the head

Your payload nests the composite: read `sentiment.composite.score_0_100`, `sentiment.composite.label` and `sentiment.composite.components`; `sentiment.event_signal` is the per-symbol event blend, not the crowd score.

1. Read `sentiment.composite.label` together with `regime.btc_regime_4h`. Greed inside a confirmed uptrend is normal and only trims size or horizon; fear inside a downtrend is normal and is not a buy reason. The contrarian reading applies only when sentiment and trend disagree.
2. Extreme fear (`sentiment.composite.score_0_100` < 20) can raise a BUY confidence by at most 0.05 and only when `technical.direction` is `long` with `mtf_agreement` true, or `flow.washout_setup` is true with a non-`decline` bear verdict. Without structure it is HOLD.
3. Greed (60–80) lowers the confidence you would otherwise assign by about 0.05 and shortens `thesis.horizon_bars`. Extreme greed on a surviving mean-reversion candidate requires the bear verdict to be `survivable`, not `survivable_if_monitored`.
4. Sentiment can persist at an extreme for many cycles. Never argue "it must revert soon". Use it as a level condition, not a timing signal.
5. When sentiment is the main reason you want to act, you are acting on sentiment alone. Set `debias.acting_on_sentiment_alone` to true and do not BUY.

## What not to do

- Do not compute or restate a composite score yourself; cite `score_0_100` at its payload path (`sentiment.composite.score_0_100` for the head) as given.
- Do not map sentiment bands to a target exposure. Position size is bounded by the risk manager's `max_size_pct` and the hard limits; sentiment only moves confidence.
- Do not treat `market_tone` from a small batch as market-wide truth; name the batch size.
- Do not express a bearish sentiment read as a short. It becomes "no entry" or a SELL on an existing position.
- Do not cite a sentiment component that is `null`.

## Expressing the result

News role: `events[].score`, `events[].event_type`, `events[].stale`, `market_tone`, `market_note` (one Korean sentence naming the dominant event types and whether the feed looked one-sided), `feed_health_note` (Korean; empty feed stated as "no information").

Head role: in `decisions[].rationale` cite `sentiment.composite.score_0_100` or `sentiment.composite.label` when it changed your confidence; put the condition into `thesis.red_lines` using `metric: composite_sentiment` with `op` and `value` (for example `>` 80 on a trend long); set `debias.acting_on_sentiment_alone`; list sentiment-driven HOLDs in `hold_reasons`.
