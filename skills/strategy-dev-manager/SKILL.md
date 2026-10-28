---
name: strategy-dev-manager
description: 코드가 최근 청산 20건의 손익비로 매기는 전략 쇠퇴 상태(healthy/warning/decayed/insufficient_sample)를 읽어 리스크 관리자는 역할 프롬프트의 확신도 하한·진입 중단 규칙으로, 저널은 일일 리뷰의 건강도 서술로 반영하는 규약
roles: [risk_manager, journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/strategy-dev-manager/SKILL.md (MIT)"
license: MIT
---
# Strategy decay: is the edge still there?

## Purpose

Of the original strategy lifecycle, only the monitoring step is kept. Code scores the bot's own closed trades and hands you a decay label. You never backtest and never see candles. You read the label and decide how defensive to be. The binding numbers live in your role prompt; this skill explains what the label means and how to write about it.

## Where the label sits (SPEC v2.1 §20.3a)

- risk_manager: `account_pack.decay_state`, read together with `account_pack.bias_severities.*`.
- journal (daily): `metrics_table.decay_state`, read together with `metrics_table.trade_stats.{profit_factor, sharpe, sortino, calmar, win_rate}`, `metrics_table.returns.max_drawdown_pct` and `closed_count`.
- journal (post_trade): there is no decay field, so do not mention decay there.

## How code assigns it

Code looks at the profit factor (gross profit ÷ gross loss, after fees) over the last 20 closed trades:

| profit factor | `decay_state` |
|---|---|
| ≥ 1.1 | `healthy` |
| 0.9 up to 1.1 | `warning` |
| < 0.9 | `decayed` |
| profit factor undefined (no wins and no losses) | `warning` |
| fewer than 20 closed trades | `insufficient_sample` |

The window rolls and has no memory. One trade entering or leaving it can flip the state, and nothing smooths it. A trade window with no losing trade has an infinite profit factor; the daily table shows that as `trade_stats.profit_factor: null`, while the label still reads `healthy`.

The supporting ratios cover the same trades from other angles. A ratio may drive a change only once it has enough sample: profit factor and Sharpe need 30 closed trades, win rate needs 50. With less, report the number with "표본 부족". When a ratio disagrees with the label, the label governs and the worse number is reported as a risk.

## risk_manager: reacting to the label

Read `account_pack.decay_state` once, before any verdict, and apply the prompt's section 3A:
- `warning` sets `global.confidence_floor` to at least 0.70.
- `decayed` sets it to 0.75.
- `decayed` together with `account_pack.bias_severities.overtrading` = severe sets `global.entries_paused`.

The prompt's floors and sizing formula are complete, so add no extra size caps of your own. Gate G3 applies decay in code, independently of you.

What this skill adds is judgement and wording:

1. `healthy`: no change. Leave it unmentioned unless a verdict depends on it. A healthy label never raises a size above the prompt formula, and never lowers the floor below 0.65.
2. `warning`: write the label into `global.portfolio_note`. Where a candidate is borderline for `conditional`, the warning is a valid reason to stay `conditional`. Cite it as "account_pack.decay_state: warning — 최근 20건 손익비 경계".
3. `decayed`: write the label and the 0.75 floor into `portfolio_note`. Open positions are still scored on their assumptions. Decay is no reason to dump a thesis that is still holding.
4. `insufficient_sample`: treat it as `healthy` for sizing. If a floor needs explaining, say that fewer than 20 trades have closed.

When decay and a bias severity both raise the floor, the higher floor applies. Name both fields in `portfolio_note`.

## journal: writing about decay in the daily review

- `summary_bullets`: state `metrics_table.decay_state` together with `metrics_table.trade_stats.profit_factor` and `closed_count`.
- `what_failed`: under `warning` or `decayed`, name the number that moved the state, with its path and value. That is usually `metrics_table.trade_stats.profit_factor`, plus `metrics_table.returns.max_drawdown_pct` when it is large.
- `risks`:
  - `warning`: the window has no smoothing, and the next few closes decide whether the floor rises to 0.75.
  - `decayed`: the 0.75 floor is active, and entries pause if `overtrading` turns severe.
- `action_items`: follow the prompt's step 10. When `closed_count` is under 30, the profit factor is below its minimum sample, so an item may not rest on it alone. Base the reason on a bias severity or a gate statistic, or emit none and explain in `abstain_reason`. Under `decayed`, items tighten only and only on the prompt's configurable list (confidence floor, candidates per cycle, cooldown cycles, scanner liquidity, FUSED thresholds). Never propose loosening a hard limit.
- `insufficient_sample`: follow the prompt's abstain rule, so there are no items, `do_not_change_yet` is true and `abstain_reason` is set.
- `telegram_text`: name the state in one word.

## Do not

- Do not derive a decay state yourself. If the label is missing, say so in `risk_note` or `abstain_reason`.
- Do not switch interpretations within one call. Apply the label you were given, and note any disagreement with the ratios.
- Do not propose new strategies, parameter sweeps or re-optimisation. Action items are proposals for a human.
- Do not suggest shorting or hedging. On a spot long-only book, being defensive means fewer or smaller entries.
