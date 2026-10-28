---
name: research-discipline
description: 추론을 시작하기 전 60초 자기편향 점검(확증·최신성·서사·대형주·주목 편향)을 거쳐 반증 증거 인용과 신선도 확인을 강제하는 사고 위생 절차
roles: [news_sentiment, bull, bear, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/research-discipline/SKILL.md (MIT)"
license: MIT
---
# Reasoning hygiene before you write a verdict

## When to apply

Run this at the very start of every call, before you form an opinion from the packs, and once more just before you emit the output. It costs no extra tool or data; it only changes the order in which you look at the fields. The reason it exists: a language model handed a labelled JSON packet tends to lock onto the first coherent story, then reads every remaining field as support for it.

## The five biases, rewritten for this bot

| Bias | How it shows up here | Correction |
|---|---|---|
| Confirmation | You find a long setup in `technical` and then read `flow`, `news` and the scanner row only for agreement. | Before concluding, deliberately hunt the opposite: for every supporting field, name one field that argues against it. A conclusion with zero disconfirming citations is not finished. |
| Recency / staleness | A headline, a funding print or a 24h change that is hours or days old is treated as current. | Check timestamps and windows. Headlines: `ts` versus `now_ms`, 24h and 7d cut-offs. Features: prefer the labelled timeframe closest to the decision horizon; a 1d label does not justify a 15m entry on its own. |
| Narrative | An `event_type` label or a catchy title stands in for the event. A "listing" headline may describe a minor venue; a "protocol" item may be a roadmap post. | Score what actually happened and whom it touches. Fill `symbols` only with names the title really concerns through the alias map, not with everything in the watchlist. |
| Large-cap default | The two largest names feel safer, so the same evidence earns a higher confidence or a looser read for them. | Confidence is set by the fields, not by the ticker. The hard limits are identical for every symbol; so is your rubric. |
| Attention | A symbol with a spike in `abnormal_volume_multiple` or `change_24h_pct` (under `feature_extract` for bull/bear, under `technical` for the head) feels important and the analysis drifts toward justifying a trade in it. | Attention is a reason to shorten the horizon and to look for the crowd, not a reason to enter. The scanner already drops a > 15% 24h move without volume confirmation; do not reintroduce it. |

## Procedure

1. Write the thesis you are about to test in one sentence (internally, then in the field the schema provides for it).
2. Walk the five rows and answer "am I about to do this?" for each.
3. Read the packs in reverse order of your preference: start with the channel most likely to contradict the thesis.
4. Collect the disconfirming fields first, then the supporting ones. Count them.
5. Re-check at the end: is every claim tied to an input field name? Is any cited fact older than the horizon it supports? Did you name the strongest point of the side you reject?

## Thresholds and rules of thumb

- Minimum disconfirming citations: bull ≥ 2, bear ≥ 1 per positive it attacks, head ≥ 1 per BUY (the losing side's strongest point).
- Headline freshness: > 24 hours not intraday-actionable, > 7 days stale with capped score.
- A claim whose only evidence is the symbol's size, popularity or a label word is discarded, not downgraded.
- If the packs contain fewer than two independent channels that are not `null` or abstained, the honest verdict is low confidence, not a confident one in either direction.

## What not to do

- Do not search for or assume facts outside the payload; there is nothing else to search.
- Do not rephrase a supporting field as if it were disconfirming to satisfy the count.
- Do not let an abstained channel count as silent agreement; it is missing evidence. The head sees it in `abstained_channels`; bull and bear see it as `technical` or `flow` being `null`; the news role sees it as an empty feed with `feed_health.errors`.
- Do not write a bearish conclusion as a short; it is "no entry" or "exit".

## Expressing the result

- news_sentiment: `events[].stale` from the timestamp check; `events[].symbols` only from the alias map; `events[].summary` describing the actual event in ≤ 20 Korean words; `feed_health_note` stating when the feed, not the market, is quiet.
- bull: `disconfirming_evidence` (≥ 2 items, each naming a pack field such as `flow.crowding_risk` or `feature_extract.rsi_5`), `main_risk_to_bull_case`, `what_would_disprove` (an observable trigger, in Korean), and every `thesis_points[].evidence_field` filled with a real field path.
- bear: `disconfirming_evidence` (one item per positive you found in the packs), `what_would_disprove_bear`, `strongest_point` written so it can be quoted verbatim by the head, `death_paths[].observable_trigger` tied to a field.
- head: `disagreement_class` (horizon / caliber / risk_preference / information / none), `losing_side_strongest_point` copied verbatim from the bull or bear output, `debias.ignored_bearish_news` and `debias.chasing_spike` answered honestly, and `decisive_factor` naming the field that settled it.
