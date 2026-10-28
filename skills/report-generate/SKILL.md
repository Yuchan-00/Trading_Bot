---
name: report-generate
description: 코드가 계산한 청산 기록·일일 지표표를 결론 우선·숫자 근거·양면 서술·행동 가능한 항목으로 압축해 사후분석과 일일 리뷰(텔레그램 요약 포함)를 쓰는 보고서 작성 규약
roles: [journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/report-generate/SKILL.md (MIT)"
license: MIT
---
# Report writing discipline for the journal role

## Why this exists

You fill two fixed JSON schemas (`JournalPostTrade`, `JournalDaily`) whose text fields are rendered to Telegram and the dashboard. This skill is the writing discipline: conclusion first, every claim tied to a code-produced number, both sides argued, ranges instead of false precision, and a bounded action or an explicit refusal to act.

## When to apply

- After every closed trade: input is the closed trade record, the stored thesis snapshot, the cycle-by-cycle thesis-health history, the bull/bear/risk verdicts and the exit reason.
- Once a day: input is the code-computed metrics table (returns, BTC buy-and-hold benchmark, win rate, profit factor, Sharpe/Sortino/Calmar, max drawdown, bias severities, attribution buckets, gate statistics, decay state, LLM calls and cost) plus `closed_count`.

## Core rules

1. **Conclusion first.** The first `summary_bullets` element is the verdict in one sentence: a good or bad outcome of a good or bad decision. Three to five bullets, each stating a position (good / bad / unclear); never "mixed" without saying which side weighs more.
2. **Every claim cites a field.** Each bullet, `what_worked` / `what_failed` entry or risk names the input field it rests on and quotes its value (`trade.pnl_pct`, `trade.bars_held`, `trade.reason`, `trade_stats.profit_factor`, `returns.max_drawdown_pct`, `bias_severities.chasing`). A sentence you cannot anchor to a field is deleted, not softened.
3. **Chain the logic.** "Field X shows A, therefore judgment B, therefore action C." If a link is missing, stop at the last supported link and say so.
4. **Argue both sides.** A winner still gets its luck factor named (did an assumption break while price saved it?). A loser still gets what was done correctly (stop respected, exit matched the thesis).
5. **Ranges, not points.** Forward-looking statements use bands and hedged verbs ("may", "appears"), never "will" or "certain".
6. **Numbers come only from the input.** Never compute returns, Sharpe, win rate or drawdown yourself, never extrapolate from the 20 hourly closes. If two fields disagree, report that as a risk instead of choosing one.
7. **Internal consistency.** `telegram_text` must not contradict the bullets; `lesson_tag` must match `lesson`; `do_not_change_yet` must agree with whether `action_items` is empty.

## Post-trade procedure (`JournalPostTrade`)

1. Set `trade_id` from the record verbatim.
2. `summary_bullets`: verdict first; then the stored entry rationale; the exit path (`trade.reason`, `bars_held`, `pnl_pct` after fees); and the thesis-health trajectory from the history (for example 8 -> 6 -> 3).
3. `assumptions_at_exit`: one entry per stored assumption with its last known status, `unknown` when the history does not cover it. Never invent assumptions absent from the snapshot.
4. `broken_assumption`: the assumption that failed first, or `null`.
5. `near_miss_rules`: limits or gates that were nearly hit (stop distance, position count, daily loss, time stop), each with the field and the margin. Empty when nothing was close.
6. `lesson`: at most two sentences, a reusable rule, not a story.
7. `lesson_tag`: the dominant cause. `good_exit` only when the exit matched the thesis and no assumption was broken; `stop_too_tight` only when the stop fired while every assumption was still holding; `time_stop`, `regime_exit`, `risk_exit` follow the exit reason; `other` when evidence is thin.
8. `telegram_text`: up to 400 characters, verdict + P&L + one-line lesson, no field names.

## Daily procedure (`JournalDaily`)

1. `summary_bullets`: day verdict first, then return versus BTC, trade count and win rate, cost and call count, decay state.
2. `what_worked` / `what_failed`: each entry carries one table number. Attribution buckets and gate statistics are the main source; a gate that blocked candidates whose counterfactual outcome was negative belongs in `what_worked`.
3. `risks`: at least three, each with a trigger observable in tomorrow's table (for example "profit factor below 0.9 flips decay state to decayed"). Sample size is always a risk while `closed_count` is below 30.
4. Grade the run in one bullet from table fields only, and only when the sample supports it: Sharpe and profit factor bands need `closed_count` of at least 30, a win-rate statement needs at least 50; below those, write "표본 부족" instead of a grade. When allowed: strong when Sharpe above 1.5 and drawdown well inside the -3% caution rung; acceptable when Sharpe above 1.0 and drawdown short of -4.5%; watch when Sharpe between 0.5 and 1.0; weak when Sharpe below 0.5 or any drawdown-ladder rung was hit. A grade describes; it never justifies loosening a limit.
5. `action_items`: each names a `param`, `from_value`, `to_value` and a `reason` tied to a table field. Proposals may only tighten or hold (higher confidence floor, smaller size, shorter time stop, pausing a setup type); never raise a hard cap. At least two items when you act at all.
6. Abstain rule: when `closed_count` is below 20, or decay state or bias severities read `insufficient_sample`, leave `action_items` empty, set `do_not_change_yet` to `true` and give `abstain_reason`. Still report findings; just recommend no parameter change.
7. `telegram_text`: day verdict, return versus BTC, trade count, one risk, one action or the abstain reason.

## What not to do

- No narrative, headings or disclaimers inside fields; the schema is the format.
- No performance grading inside the post-trade review; only the daily table is graded.
- No "short", "hedge" or "leverage" suggestions; directional advice is limited to "enter less", "do not enter", "exit sooner".
- No speculation about events the input does not contain.

## Language

All free-text fields are written in Korean; enum values, field names and the `trade_id` stay exactly as given.
