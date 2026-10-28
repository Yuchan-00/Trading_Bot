---
name: performance-attribution
description: 코드가 계산한 손익·BTC 바이앤홀드 대비 성과·위험조정 지표·심볼별/셋업별/청산사유별 기여도 표를 읽어 "왜 벌었는지/잃었는지"를 일일 리뷰로 설명하는 라이트 성과 귀속 규약
roles: [journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/performance-attribution/SKILL.md (MIT)"
license: MIT
---
# Performance attribution: explaining the P&L, not just reporting it

## Why this exists

A daily return tells the operator nothing about whether the process works. Attribution splits the result into sources so that a lucky spike and a sound process meeting a kill switch are described differently. This is the light version a three-position spot book allows: one benchmark, risk-adjusted ratios and contribution buckets, all code-computed in `metrics_table`.

## What the table contains (keys written as they appear)

- `returns`: `day_pnl_pct`, `cum_pnl_pct`, `equity`, `max_drawdown_pct`, `btc_buy_hold_pct` (BTC buy-and-hold over the same window) and `excess_vs_btc_pct` (bot minus benchmark).
- `trade_stats`: `closed_today`, `win_rate`, `avg_win_pct`, `avg_loss_pct`, `profit_factor`, `avg_bars_held`, `sharpe`, `sortino`, `calmar` (ratios already annualised).
- `by_exit_reason`: one bucket per `stop_loss` / `take_profit` / `time_stop` / `head_sell` / `kill_switch` / `manual` / `partial`, each with trade count and summed P&L. `by_symbol` and `by_setup` (`trend_continuation` / `mean_reversion` / `breakout`) use the same shape.
- `attribution_buckets`: `noise_trades`, `early_exits`, `late_exits`, `overtrading`, `missed_signals`, the code's own process diagnosis.
- `bias_severities`, `decay_state`: covered by the trade-journal and strategy-dev-manager skills.
- `gate_stats`: G1 to G4 rejection counts, `bear_decline_rate`, `risk_veto_rate`, `counterfactual_declined_pnl_pct`.
- `llm`: `calls`, `cost_usd`, `budget_usd`, `abstain_count`. `halts`: code halts of the day.

Any entry can be `null`; a `null` bucket is unavailable, not zero.

Rating bands for the ratios (adapt your wording to them, do not restate them as facts about the day):

| metric | strong | acceptable | weak |
|---|---|---|---|
| `sharpe` | > 1.5 | 1.0 to 1.5 | < 0.5 |
| `sortino` | > 2.0 | 1.5 to 2.0 | < 1.0 |
| `calmar` | > 1.0 | 0.5 to 1.0 | < 0.2 |

All ratios are meaningless below 30 closed trades; before that, describe the raw numbers and say the ratio is not yet informative.

## Procedure for the daily review

1. Lead with `returns.day_pnl_pct` against `returns.btc_buy_hold_pct`, using `excess_vs_btc_pct` as given. A positive excess in a falling BTC window is the strongest thing this bot can show; a positive absolute return with a negative excess in a rising window is not a process success.
2. Rank the `by_symbol`, `by_setup` and `by_exit_reason` buckets by the absolute value of their summed P&L and name the top one per dimension. When one `by_symbol` bucket holds nearly all of the summed P&L across `by_symbol`, or `trade_stats.closed_today` is 1, say that the day rests on one name: concentration is a risk, not a skill.
3. Read `by_exit_reason` as a process signal: a large `time_stop` count means theses are not playing out within their horizon; a large `kill_switch` count means the code decided the day and the agents were not tested; `manual` and `partial` counts are operator actions and say nothing about the agents.
4. Read `attribution_buckets` as the herd-error detector: `noise_trades` and `overtrading` point at the entry side (scanner and cooldown), `early_exits` and `late_exits` at the thesis review, `missed_signals` at gate starvation. Quote the bucket with its value in `what_failed`.
5. A `returns.max_drawdown_pct` close to the −6% ladder stop together with a dominant `stop_loss` bucket means stops are being hit in BTC-driven drops; the correlation gate deserves an action item.
6. Turn `gate_stats` into one risk line: `bear_decline_rate` above 0.7 or `risk_veto_rate` above 0.6 with a positive `counterfactual_declined_pnl_pct` is over-conservatism; a rate below 0.1 with losses is rubber-stamping.
7. When `llm.cost_usd` exceeds `llm.budget_usd` for several days, the action item is on candidates per cycle, not on the strategy.

## Mapping to `JournalDaily`

- `summary_bullets` (3 to 5, Korean): return versus benchmark, drawdown, closed count, dominant contribution bucket, decay state if present.
- `what_worked[]` / `what_failed[]`: every item contains one key of `metrics_table` written as it appears (for example `by_exit_reason.take_profit`, `attribution_buckets.early_exits`) and its value; prose without a key is not allowed here.
- `risks[]` (≥ 3): concentration, drawdown near the ladder, gate starvation or rubber-stamping, sample size, LLM cost, `null` metrics, as applicable.
- `action_items[]` (≥ 2 when `closed_count` ≥ 20 and `decay_state` is not `insufficient_sample`): `{param, from_value, to_value, reason}` with `param` an existing config key; `reason` must cite the bucket or ratio that motivates it.
- `abstain_reason` set and `action_items` empty (so `do_not_change_yet` is true) when `closed_count` < 20, when step 2 found the day resting on one name or one trade, or when the `kill_switch` bucket or `halts` show the code made the decisions.
- `telegram_text` (Korean, ≤ 400 characters): return versus BTC, worst ratio, top contributor, and "변경 없음" or the count of action items.

## What not to do

- Do not compute any ratio, return or share yourself; comparing two table values is allowed, dividing them is not. If the table lacks a field, write that it is unavailable.
- Do not treat a short window as evidence: attribution describes the past sample, and a few trades describe luck.
- Do not attribute P&L to agents by name or "good calls"; attribute only to the table's buckets (symbol, setup, exit reason, attribution bucket).
- Do not compare against anything but the BTC buy-and-hold series in the table; there is no index, risk-free adjustment or factor model here.
- Do not recommend leverage, shorting, hedging or averaging down; the only levers are fewer entries, smaller sizes, tighter filters and cooldowns.
- Do not let a strong `sharpe` over 20 trades justify loosening any hard limit; hard limits are not tunable from the review.
