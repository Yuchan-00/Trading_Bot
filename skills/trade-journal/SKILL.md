---
name: trade-journal
description: 코드가 계산한 네 가지 행동 편향 심각도(처분효과·과매매·추격·앵커링)와 청산 기록을 읽어, 저널은 사후분석·일일 리뷰를 쓰고 리스크 관리자는 역할 프롬프트의 확신도 하한·진입 중단 규칙 안에서 반영하는 매매일지 규약
roles: [risk_manager, journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/trade-journal/SKILL.md (MIT)"
license: MIT
---
# Trade journal: feeding the bot's own behaviour back to it

## Purpose

Code turns closed trades into four behaviour diagnoses, each with a severity. The journal explains them and the risk manager tightens on them. You never compute these numbers. You read the labels and interpret them. Your role prompt holds the binding rules: floors, pause conditions, the lesson-tag order and the action-item list. This skill adds the reading.

## Where the severities sit (SPEC v2.1 §20.3a)

- risk_manager: `account_pack.bias_severities.{disposition, overtrading, chasing, anchoring}` and `account_pack.noise_trade_share` (null when fewer than 5 trades).
- journal (daily): `metrics_table.bias_severities.*`, `metrics_table.attribution_buckets.*`, `metrics_table.by_exit_reason.*`.
- journal (post_trade): there are no severities. Use `trade.*`, `trade.thesis.*`, `thesis_history[]` and `verdicts.*`.

## The four diagnoses (fixed code definitions)

Code computes them over closed trades only. Each takes one of `none`, `mild`, `severe` or `insufficient_sample`.

| key | what code measures | mild | severe |
|---|---|---|---|
| `disposition` | average hold time of losing trades divided by that of winning trades | ≥ 1.2 | ≥ 1.5 |
| `overtrading` | closed trades per active day | > 8 | > 12 |
| `chasing` | share of entries made after a 1h return of +3% or more | > 30% | > 50% |
| `anchoring` | share of exits within ±0.3% of the entry price | > 30% | > 50% |

With fewer than 20 closed trades, or too little data for one key, that key reads `insufficient_sample`. Read it as unknown, never as fine.

What each one says about the process:
- `disposition`: losers are held longer than winners, so the thesis review is lenient.
- `overtrading`: entries are being recycled through cooldowns.
- `chasing`: spike filters and the head's `debias.chasing_spike` are leaking.
- `anchoring`: exits are being made to get out flat rather than on the thesis.

## risk_manager: how the severities feed your output

Apply them once per call, before the candidates. The prompt's section 3A sets the numbers:
- `chasing` or `overtrading` severe gives a `confidence_floor` of 0.75.
- `decay_state` = decayed together with `overtrading` severe pauses entries.
- No other severity or decay combination pauses entries; the prompt's other pause triggers (regime, drawdown, losses, exposure) still apply.

Gate G3 applies the same inputs again in code.

Beyond those numbers, the severities shape your wording and conditions:

1. `chasing` mild or severe: a candidate whose `candidates[].bear.strongest_point` describes a spike already under way gets an entry in `conditions[]` saying "candidates[].technical.timing: now 확인 후 진입". You have no 24h change figure, so do not cite one.
2. `disposition` severe: when you re-assess open positions, an ambiguous or null field never resolves to `holding`; it is at least `weakening`. Name the severity in `positions[].reason`. Do not invent broken assumptions.
3. `anchoring` mild or severe: in each approved candidate's `required_monitoring[]`, write that the exit is judged on the thesis, not on `unrealized_pct` near zero.
4. `account_pack.noise_trade_share` is high: stops are sitting inside noise. Say so in `risk_note` and keep the noise checks strict. Never widen a stop beyond `technical.stop_pct`.
5. `insufficient_sample`: change nothing, and mention the small sample only if a floor needs explaining.

Record what you applied in `risk_note` (Korean, at most 2 sentences), in the format "<field path>: <value> — <짧은 한국어 해석>".

## journal: post-trade review

Follow the prompt's steps 1 to 6, which set the assumption order, the `broken_assumption` rule, the `near_miss_rules` list and the `lesson_tag` order, where the first match wins. Reading notes:

- A winner that closed on `time_stop` is tagged `time_stop`, not `good_exit`.
- The chasing evidence in a post-trade review is `verdicts.head.debias_chasing_spike`, or a spike named in `verdicts.bear.strongest_point`. No pre-entry return field is present, so do not claim one.
- A loss where every `thesis_history[].assumption_status` stayed `holding` points to the stop or to noise, not to the thesis.
- Take numbers only from `trade.*` and `thesis_history[]`.

## journal: daily review

- Cite the severities as `metrics_table.bias_severities.<key>`, and pair each one with the bucket that shows its effect:
  - `chasing` with `metrics_table.attribution_buckets.stop_losses`;
  - `disposition` with `metrics_table.attribution_buckets.time_stops`;
  - `anchoring` with `metrics_table.attribution_buckets.noise_trades`.
- Action items follow the prompt's step 10: `closed_count` ≥ 20, `decay_state` ≠ `insufficient_sample`, tightening only, and only on the prompt's configurable list. Use these mappings:
  - `chasing` severe → raise the confidence floor or lower the number of candidates per cycle;
  - `overtrading` severe → more cooldown cycles;
  - `disposition` severe → no parameter to tune; report it as a thesis-discipline risk;
  - `anchoring` → same, no parameter.
- If every severity is `none` and `metrics_table.by_exit_reason.kill_switch` holds the losses, the code decided the day. Propose nothing and say so in `abstain_reason`.

## Do not

- Do not compute win rates, hold ratios, chase shares or any other statistic. Write "자료 없음" when a number is absent.
- Do not treat `insufficient_sample` as healthy, and do not tune parameters from it.
- Do not suggest shorting, hedging, averaging down, or widening the 2% stop or the 4% take-profit.
- Do not let a profitable day soften a `severe` reading. The diagnosis is about process, not outcome.
