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

Run this once at the start of every call, before you form a view, and once more just before you emit the output. It needs no extra data; it only changes the order in which you look at the fields. A model handed a labelled JSON packet tends to lock onto the first coherent story and then read every remaining field as support. This procedure breaks that habit. Where a count or level here differs from your role prompt, the role prompt governs.

Paths per role, for the checks below:
- news_sentiment: `headlines[].age_h`, `headlines[].source_grade`, `aliases`, `feed_health.*`, `prev_events[]`.
- bull and bear: `technical.*`, `feature_extract.*`, `flow.*`, `news_events[].age_h/stale/score`, `news_context.*`, `event_signal.*`, `sentiment.*`, `scanner_row.factor_table.*`, `scanner_row.liquidity.*`, `regime.*`; bear also `corr_to_open`, `hv_pct`.
- head: per `approved[]` entry `technical.*` (with `technical.labels.*`), `flow.*`, `news.events[].age_h/stale/score`, `bull.*`, `bear.*`, `risk.*`, `gate.*`; plus `sentiment.composite.*`, `regime.*`, `account_pack.*`, `abstained_channels`.

## The five biases in this bot

| Bias | How it shows up | Correction |
|---|---|---|
| Confirmation | A long setup in `technical` is found first, and every other channel is then read only for agreement. | For each supporting field, look for one that argues against it. A conclusion with zero disconfirming citations is unfinished. |
| Recency | An old headline or a slow label is treated as current. | Use the code-given `age_h` (above 24 is stale, above 168 capped at 0.33); never compute ages. Prefer the timeframe closest to the decision; a daily label alone does not justify a 15-minute entry. |
| Narrative | An event type or a vivid title stands in for what happened. A "listing" may be a minor venue; a "protocol" item may be a roadmap post. | Score and cite what the title actually states and whom it touches. Map symbols only through `aliases`. |
| Large-cap comfort | The biggest names feel safer, so the same evidence earns a looser read. | Confidence is set by fields, not by the ticker. The hard limits and your rubric are identical for every symbol. |
| Attention | A spike in volume or 24h change (`feature_extract.abnormal_volume_multiple`, `feature_extract.change_24h_pct` for bull and bear; `technical.labels.*` for the head) makes a symbol feel important. | Attention shortens the horizon and invites the question "who already bought?"; it is not a reason to enter. The scanner already removes a 24h move above 15% without volume confirmation; do not argue it back in. |

## Procedure

1. Write the thesis you are testing in one sentence, internally, then in the field the schema gives for it.
2. Walk the five rows and ask "am I about to do this?" for each.
3. Read the channels in reverse order of preference: start with the one most likely to contradict you.
4. Collect the disconfirming fields first, then the supporting ones.
5. Final check: does every claim carry a payload path? Is any cited fact older than the horizon it supports? Have you stated the strongest point of the side you reject?

## Rules of thumb

- Minimum disconfirming citations: bull at least 2; bear one per positive it attacks; head at least the losing side's strongest point on every BUY.
- A claim whose only support is a symbol's size, popularity or a label word is discarded, not downgraded.
- If fewer than two independent channels are present (not `null`, not abstained), the honest output is low confidence, not a confident view either way.
- Evidence-like strings use the format `"<field path>: <value> — <짧은 한국어 해석>"`. Free text is Korean; field names and enums stay as the schema has them.

## What not to do

- Do not assume facts outside the payload; there is nothing else to look up.
- Do not relabel a supporting field as disconfirming to meet the count.
- Do not let a missing channel count as silent agreement. The head sees it in `abstained_channels`; bull and bear see `technical`, `flow` or `news_context` as `null`; the news role sees an empty batch or `feed_health.errors`.
- Do not write a bearish conclusion as a short; it is "no entry" or "exit".

## Expressing the result

- news_sentiment: `events[].stale` from `headlines[].age_h`; `events[].symbols` only from `aliases`; `events[].summary` describing the actual event in at most 20 Korean words; `feed_health_note` saying when the feed, not the market, is quiet.
- bull: `disconfirming_evidence` (at least 2, each naming a path such as `flow.crowding_risk` or `feature_extract.rsi_5`), `main_risk_to_bull_case`, `what_would_disprove` (an observable trigger), and every `thesis_points[].evidence_field` a real payload path.
- bear: `disconfirming_evidence` (one per positive found), `what_would_disprove_bear`, a `strongest_point` the head can quote verbatim, and each `death_paths[].observable_trigger` tied to a field.
- head: `disagreement_class`, `losing_side_strongest_point` copied verbatim from bull or bear, `debias.ignored_bearish_news` and `debias.chasing_spike` answered honestly, and `decisive_factor` naming the field that settled it.
