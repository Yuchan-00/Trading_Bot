---
name: report-generate
description: 코드가 계산한 청산 기록·일일 지표표를 결론 우선·필드 근거·양면 서술·행동 가능한 항목으로 압축해 사후분석과 일일 리뷰(텔레그램 요약 포함)를 쓰는 보고서 작성 규약(절차·태그 순서는 역할 프롬프트 우선)
roles: [journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/report-generate/SKILL.md (MIT)"
license: MIT
---
# Report writing discipline for the journal role

## Purpose

You fill two fixed schemas, `JournalPostTrade` and `JournalDaily`. Their text goes to Telegram and the dashboard. Your role prompt sets the order of steps, the lesson-tag order, the abstain rule and the layout of `telegram_text`. This skill sets how the text is written: conclusion first, every claim tied to a code-computed field, both sides argued, hedged forward statements, and either a bounded action or an explicit refusal to act.

## Inputs by mode (SPEC v2.1 §20.3a.7)

- post_trade:
  - `trade.*`, including `trade.trade_id`, `trade.reason`, `trade.pnl_pct`, `trade.bars_held` and `trade.confidence`;
  - `trade.thesis.{reasons, assumptions, red_lines, horizon_bars}`;
  - `thesis_history[]` with `{cycle, health, assumption_status, red_line_hits, action, exit_flag, unrealized_pct}`;
  - `verdicts.{technical, bull, bear, risk, head, channel_agreement, news_risk_after_entry}`.
- daily: `closed_count` and `metrics_table.*` (returns, trade_stats, by_exit_reason, by_symbol, by_setup, attribution_buckets, bias_severities, decay_state, gate_stats, llm, halts).

Do not cite a field from the other mode. The daily table does not exist in a post-trade call, and the reverse is also true.

## Writing rules

1. **Conclusion first.** The first `summary_bullets` item is a one-sentence verdict. It classifies the trade as a good or bad decision with a good or bad outcome. Every bullet takes a side. "Mixed" is allowed only when you say which side weighs more.
2. **Every claim cites a field.** Bullets, `what_worked`/`what_failed` items and risks each name their path and value. Examples: "trade.bars_held: 11 — 기간의 절반에 손절", "metrics_table.trade_stats.profit_factor: 0.94 — 경계 구간". If a sentence has no field behind it, delete it rather than soften it.
3. **Chain the logic.** Field shows A, so judgement B, so action C. Where a link is missing, stop at the last supported step and say so.
4. **Argue both sides.** A winner still names its luck: did an assumption break while price rescued the trade? A loser still names what went right: the stop was respected, or the exit matched the thesis.
5. **Hedge the future.** Write "할 수 있다" or "보인다" about what comes next, never "반드시". Give bands, not point forecasts.
6. **Numbers come only from the input.** Never compute returns, ratios, win rates, drawdowns or times. Never do timestamp arithmetic; quote `trade.bars_held`. When two fields disagree, report the disagreement as a risk.
7. **Stay consistent.**
   - `telegram_text` must not contradict the bullets.
   - `lesson_tag` must match `lesson`.
   - `do_not_change_yet` and `abstain_reason` must agree with whether `action_items` is empty, as prompt step 11 requires.

## Post-trade writing

- `trade_id`: copy it verbatim.
- `summary_bullets`:
  - the verdict;
  - what the entry rested on (`verdicts.bull.thesis_points`, `verdicts.head.decisive_factor`);
  - the bear warning and whether it happened (`verdicts.bear.strongest_point`);
  - the health path from the first to the last `thesis_history[].health`, for example 9 → 7 → 5;
  - the exit (`trade.reason`, `trade.pnl_pct`, `trade.bars_held`).
- `assumptions_at_exit`: one entry per stored assumption, taken from the last `thesis_history[].assumption_status`. Use `unknown` when there is no reading, and never add an assumption that is not stored.
- `broken_assumption`, `near_miss_rules` and `lesson_tag`: apply the prompt's rules in its order. Write the reason for the tag into `lesson`.
- `lesson`: at most two sentences. Give a rule that carries over to future trades, and name the field that would have caught the problem. It is a rule, not a story.
- No performance grade here; grades belong only to the daily table.

## Daily writing

- `summary_bullets`:
  - the day's verdict;
  - `metrics_table.returns.day_pnl_pct` against `btc_buy_hold_pct`;
  - `trade_stats.closed_today` with `win_rate`;
  - `returns.max_drawdown_pct`;
  - `llm.cost_usd` against `budget_usd`;
  - `decay_state`.
- `what_worked` / `what_failed`: one table number each.
  - Attribution buckets and gate statistics are the main sources.
  - The table has no counterfactual P&L, so do not praise a gate for losses it avoided.
- `risks`: at least three. Each risk carries a trigger that tomorrow's table can show, for example "metrics_table.trade_stats.profit_factor 0.9 미만이면 decay_state decayed 전환 가능". While `closed_count` is below 30, the small sample is always one of the risks.
- **Grade.** Give one bullet, built only from table fields and only when the sample allows:
  - use the performance-attribution bands, which need `closed_count` ≥ 30 for ratios and ≥ 50 for win rate;
  - below that, write "표본 부족";
  - a drawdown at or past a ladder rung (`returns.max_drawdown_pct` 3, 4.5 or 6) caps the grade at "watch".
- `action_items`: follow the prompt's step 10. Items tighten only, use the configurable list only, and each `reason` cites a table path. If you act, aim for at least two items, as the prompt says; if you cannot, give none and fill `abstain_reason`.

## Telegram

Use the line order the prompt gives for each mode. Keep it to 400 characters or fewer, use plain Korean, and do not paste raw field paths into the phone text. Values with their meaning are enough.

## Do not

- Do not put headings, narrative or disclaimers inside fields. The schema is the format.
- Do not suggest shorting, hedging or leverage. Directional advice is limited to entering less, not entering, or exiting sooner.
- Do not speculate about events the input does not contain.

## Language

Write every free-text field in Korean. Keep enum values, field names and `trade_id` exactly as given.
