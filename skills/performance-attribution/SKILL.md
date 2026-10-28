---
name: performance-attribution
description: 코드가 계산한 손익·BTC 바이앤홀드 대비 성과·위험조정 지표·심볼별/셋업별/청산사유별 기여도·게이트 통계 표를 읽어 "왜 벌었는지/잃었는지"를 일일 리뷰로 설명하는 라이트 성과 귀속 규약
roles: [journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/performance-attribution/SKILL.md (MIT)"
license: MIT
---
# Performance attribution: explain the P&L, don't just report it

## Purpose

A daily return on its own says nothing about whether the process works. Attribution breaks the result into sources, so a lucky spike reads differently from a sound process that hit a kill switch. A three-position spot book supports only a light version of this: one benchmark, a few risk-adjusted ratios and contribution buckets. Code computes all of them under `metrics_table`. The daily procedure, the abstain rule and the Telegram order are set in your role prompt. This skill covers how to read the table.

## Table paths (SPEC v2.1 §20.3a.7, daily mode)

- `metrics_table.returns`: `day_pnl_pct`, `cum_pnl_pct`, `equity`, `max_drawdown_pct`, `btc_buy_hold_pct`, `excess_vs_btc_pct`.
- `metrics_table.trade_stats`: `closed_today`, `win_rate`, `avg_win_pct`, `avg_loss_pct`, `profit_factor`, `avg_bars_held`, `sharpe`, `sortino`, `calmar`.
- `metrics_table.by_exit_reason`, `metrics_table.by_symbol`, `metrics_table.by_setup`: each maps a key to `{count, pnl_pct_sum}`.
  - Exit-reason keys: `stop_loss`, `take_profit`, `time_stop`, `head_sell`, `kill_switch`, `manual`, `partial`.
  - Setup keys: `trend_continuation`, `mean_reversion`, `breakout`.
- `metrics_table.attribution_buckets`: `noise_trades` (|pnl_pct| < 0.3), `stop_losses`, `take_profits`, `time_stops`, `head_sells`. These are counts.
- `metrics_table.gate_stats`: `candidates_seen`, `g1_dropped`, `g2_bear_decline`, `g3_risk_veto`, `g3_other`, `g4_downgrade`, `head_calls`, `bear_decline_rate`, `risk_veto_rate`.
- `metrics_table.llm`: `calls`, `cost_usd`, `budget_usd`, `abstain_count`.
- `metrics_table.halts[]`.
- `metrics_table.bias_severities` and `metrics_table.decay_state`: see the trade-journal and strategy-dev-manager skills.

Every value can be `null`. A `null` value means unavailable, not zero. A `null` `profit_factor` may mean there were no losses at all. Say that it is undefined rather than guessing.

## Ratio bands

Use these words only once the sample supports them. Sharpe, Sortino, Calmar and profit factor need `closed_count` ≥ 30. Win rate needs ≥ 50. Below that, quote the raw number with "표본 부족".

| metric | strong | acceptable | watch | weak |
|---|---|---|---|---|
| `trade_stats.sharpe` | > 1.5 | 1.0–1.5 | 0.5–1.0 | < 0.5 |
| `trade_stats.sortino` | > 2.0 | 1.5–2.0 | 1.0–1.5 | < 1.0 |
| `trade_stats.calmar` | > 1.0 | 0.5–1.0 | 0.2–0.5 | < 0.2 |

These bands describe the window. They never justify loosening a limit.

## Reading order

1. **Benchmark first.** Compare `returns.day_pnl_pct` with `returns.btc_buy_hold_pct` and quote `returns.excess_vs_btc_pct` as given.
   - A positive excess while BTC fell is the best result this bot can show.
   - A positive return with a negative excess while BTC rose is not a process win.
2. **Top contributors.** In each of `by_symbol`, `by_setup` and `by_exit_reason`, name the bucket with the largest absolute `pnl_pct_sum`. If one symbol carries nearly all of it, or `trade_stats.closed_today` is 1, the day rests on one name or one trade. That is a concentration risk, not skill.
3. **Exit reasons as process signals.**
   - Many `time_stop` exits mean theses are not playing out within their horizon.
   - `kill_switch` means code decided the day and the agents were not tested.
   - `manual` and `partial` are operator actions and say nothing about the agents.
4. **Attribution buckets.**
   - A high `noise_trades` count means stops sit inside noise or the edge is too thin to clear the 0.30% round-trip cost. That points at the entry side: scanner thresholds, cooldowns and the confidence floor.
   - When `stop_losses` outnumbers `take_profits`, the target is rarely reached.
   - A large `time_stops` count repeats signal 3.
   - `head_sells` shows how often thesis reviews ended trades early.
   Quote the bucket and its value.
5. **Drawdown.** If `returns.max_drawdown_pct` is near a drawdown-ladder rung (3, 4.5 or 6%) and `by_exit_reason.stop_loss` dominates, stops are being hit in BTC-driven drops. The FUSED thresholds deserve an action item.
6. **Gate statistics.** Turn them into one risk line, using the prompt's bands:
   - `gate_stats.bear_decline_rate` or `gate_stats.risk_veto_rate` above 0.7 means the bot is starved of trades;
   - either below 0.1 together with losses means the gates are rubber-stamping;
   - a large `g1_dropped` compared with `candidates_seen` means the scanner feeds weak candidates.
   No counterfactual P&L exists in the table, so do not claim what a declined trade would have earned.
7. **LLM cost.** If `llm.cost_usd` exceeds `llm.budget_usd`, the item targets candidates per cycle, not the strategy.

## How this maps to `JournalDaily`

- `what_worked` and `what_failed`: each item holds one full `metrics_table.*` path and its value, for example `metrics_table.by_exit_reason.take_profit.pnl_pct_sum`. Prose without a path does not belong here.
- `risks` (≥ 3), as they apply: concentration, drawdown near a rung, gate starvation or rubber-stamping, sample size, LLM cost, `null` metrics.
- `action_items` follow the prompt's step 10. Each `reason` cites the bucket or ratio behind it.
  - If step 2 found a one-name or one-trade day, prefer abstaining and say why in `abstain_reason`.
  - Do the same when `by_exit_reason.kill_switch` or `halts` show that code made the decisions.

## Do not

- Do not compute ratios, returns or shares. You may compare two values in the table; do not derive new numbers from them.
- Do not treat a short window as evidence. A few trades describe luck.
- Do not credit agents by name. Attribute only to the table's buckets.
- Do not benchmark against anything but `returns.btc_buy_hold_pct`.
- Do not suggest leverage, shorting, hedging or averaging down.
- Do not let a strong ratio loosen any hard limit.
