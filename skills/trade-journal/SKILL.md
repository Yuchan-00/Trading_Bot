---
name: trade-journal
description: 코드가 계산한 네 가지 행동 편향 심각도(처분효과·과매매·추격·앵커링)와 청산 기록을 읽어, 저널은 사후분석·일일 리뷰를 쓰고 리스크 관리자는 확신도 하한·진입 중단으로 반영하는 매매일지 규약
roles: [risk_manager, journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/trade-journal/SKILL.md (MIT)"
license: MIT
---
# Trade journal: reading the bot's own behaviour back to it

## Why this exists

The journal table turns closed trades into four behavioural diagnoses with severities so the two readers can react: the journal explains, the risk manager tightens. You never compute these numbers; they arrive as labelled fields and your job is to interpret them.

## The four diagnostics (code-computed, fixed definitions)

All four are computed over closed trades only; open positions never count. Severity enum: `none` / `mild` / `severe` / `insufficient_sample`.

| key | metric | mild | severe |
|---|---|---|---|
| `disposition` | mean hold time of losers ÷ mean hold time of winners | ≥ 1.2 | ≥ 1.5 |
| `overtrading` | closed trades per calendar day | > 8 | > 12 |
| `chasing` | share of entries placed after a 1h move of +3% or more | > 30% | > 50% |
| `anchoring` | share of exits within ±0.3% of the entry price | > 30% | — |

Fewer than 20 closed trades → every key is `insufficient_sample`. That value means "unknown", not "fine".

Reading them: `disposition` severe with many `time_stop` exits means the thesis review is too lenient; `overtrading` above 8 a day with max 3 positions means entries are recycled through cooldowns; `chasing` means the spike filters and the head's `debias.chasing_spike` are leaking; `anchoring` means positions are closed to get out flat rather than on the thesis.

## risk_manager: how the severities change your output

The severities come in `account_pack.bias_severities` (keys above) together with `account_pack.noise_trade_share`. Apply them once per call, before judging individual candidates:

1. `chasing` severe or `overtrading` severe → set `global.confidence_floor` to 0.75 (never below 0.65, never above 1.0) and say which key triggered it in `global.portfolio_note`.
2. Both severe at once → in addition set `global.entries_paused: true` with `pause_reason` naming both fields. Open positions are still reviewed as usual.
3. `chasing` mild → any candidate whose `technical.timing` is not `now`, or whose `bear.strongest_point` describes a spike already under way, is downgraded from `approve` to `conditional` with a condition in `conditions[]` that the entry waits for `technical.timing` = `now`. You receive no 24h change or volume figures; the scanner's spike exclusion handles those in code.
4. `disposition` severe → when scoring open positions, treat every `weakening` assumption as one notch worse in `positions[].assumption_status[]` and name the severity in `positions[].reason`. Do not invent broken assumptions; only tighten the ambiguity.
5. `anchoring` mild or severe → in `required_monitoring[]` of each approved candidate write that the exit must be judged on the thesis, not on `unrealized_pct` near zero.
6. `insufficient_sample` → change nothing; mention only that the sample is below 20 if the floor needs explaining.

Record what you applied in `risk_note` (Korean, ≤ 2 sentences).

## journal: post-trade review

For each closed trade you receive `trade` (entry, exit, fees, `pnl_pct`, `bars_held`, `reason` ∈ `stop_loss` / `take_profit` / `time_stop` / `head_sell` / `kill_switch` / `manual` / `partial`), the thesis snapshot, the cycle-by-cycle health history and the bull/bear/risk verdicts. Produce `JournalPostTrade`:

- `summary_bullets` (3 to 5, Korean): what the entry assumed, what happened, how it ended. Numbers come only from `trade`.
- `assumptions_at_exit[]`: one entry per stored assumption with `status` from the last health evaluation; use `unknown` when the history has no reading.
- `broken_assumption`: the first assumption whose status reached `broken`, or `null`.
- `near_miss_rules[]`: hard rules the trade brushed against (stop within 0.3% of being hit, `bars_held` within 4 bars of the 96-bar time stop). Empty if none.
- `lesson` (≤ 2 sentences) and `lesson_tag`: pick the enum from the evidence, not the P&L. A winner that exited on `time_stop` is `time_stop`, not `good_exit`. A loser whose entry followed a +3% hourly run is `chased_spike` even if the thesis also broke.
- `telegram_text` (≤ 400 characters, Korean): symbol, result, the tag, one line of lesson.

## journal: daily review

The daily call gives you `metrics_table` and `closed_count`. Produce `JournalDaily`:

- If `closed_count` < 20: `abstain_reason` set, `do_not_change_yet: true`, `action_items` empty, `risks` still ≥ 3.
- Otherwise `what_worked` / `what_failed` each cite a `metrics_table` key with its value, `risks` ≥ 3 and `action_items` ≥ 2 as `{param, from_value, to_value, reason}`. Only propose parameters that exist in the bot's config (cooldown cycles, confidence floor, candidates per cycle, time-stop bars, minimum volume ratio). Suggested mappings: `chasing` severe → raise the scanner's spike exclusion or require `timing: wait_pullback`; `overtrading` severe → extend the cooldown; `disposition` severe → shorten the time stop; `anchoring` → nothing to tune, flag it as a thesis-discipline issue.
- When every severity is `none`, or `by_exit_reason` shows the losses sitting in `kill_switch` (the code decided the day), propose no action items and say so in `abstain_reason`; `do_not_change_yet` then becomes true by the prompt's rule.
- `telegram_text` (Korean): P&L, hit rate, the worst severity, and whether action items exist.

## What not to do

- Do not compute win rate, hold ratios, chase shares or any statistic yourself; if a number is not in the input, write "자료 없음" rather than estimating.
- Do not read `insufficient_sample` as healthy, and do not call for parameter changes from it.
- Do not recommend shorting, hedging, adding to losers or widening the 2% stop / 4% take-profit; none of these exist in this bot.
- Do not let a profitable day soften a `severe` severity; the diagnosis is about process, not outcome.
