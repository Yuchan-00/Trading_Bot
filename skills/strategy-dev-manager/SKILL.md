---
name: strategy-dev-manager
description: 코드가 최근 청산 20건의 손익비로 매기는 전략 쇠퇴 상태(healthy/warning/decayed/insufficient_sample)를 읽어 리스크 관리자는 확신도 하한·진입 중단으로, 저널은 일일 리뷰의 건강도 판단으로 반영하는 규약
roles: [risk_manager, journal]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/strategy-dev-manager/SKILL.md (MIT)"
license: MIT
---
# Strategy decay: is the edge still there?

## Why this exists

Only the monitoring layer of the original lifecycle manager survives here: the code scores the bot's own closed trades, assigns a decay state, and hands it to you. You never run a backtest or see candles; you read one label and a few metrics and decide how defensive the next cycle must be.

## The decay state (code-computed)

`decay_state` enum: `insufficient_sample` / `healthy` / `warning` / `decayed`.

Single rule, over the most recent 20 closed trades:

| profit factor (gross wins ÷ gross losses, after fees) | state |
|---|---|
| ≥ 1.1 | `healthy` |
| 0.9 to 1.1 | `warning` |
| < 0.9 | `decayed` |
| fewer than 20 closed trades | `insufficient_sample` |

The state is recomputed from that rolling window on every call and has no memory: one trade entering or leaving the window can move it, and nothing in the code smooths it. A fresh `warning` is a reason to tighten, `decayed` a reason to stop adding risk.

Supporting metrics in the daily `metrics_table` (`trade_stats.sharpe`, `sortino`, `calmar`, `win_rate`, `profit_factor`, `returns.max_drawdown_pct`) describe the same window at different angles. Minimum samples before one of them may drive a change: Sharpe and profit factor 30 trades, win rate 50; below that the number is reported with "표본 부족" and must not move a threshold on its own. When they disagree with `decay_state`, the label wins for sizing and the worst metric is reported as a risk.

## risk_manager: reacting to the state

Read `account_pack.decay_state` once per call, before any candidate verdict. The floors below are the ones gate G3 applies to head confidence at G4; you cannot see head confidence, so do not test `bull.confidence` against them.

1. `healthy` → no change. Do not mention it unless a candidate verdict depends on it.
2. `warning` → `global.confidence_floor` at least 0.70 and the state written in `global.portfolio_note`. `max_size_pct` for every candidate is at most 7.5 (three quarters of the cap) unless the CVaR size is already smaller. An `approve` stays `approve`; a candidate that needed any shrink is `conditional` with the condition citing `decay_state`.
3. `decayed` → `global.confidence_floor` 0.75 and the state in `portfolio_note`. No candidate is `approve`: downgrade to `conditional` with a condition that `decay_state` must leave `decayed` before the next add, or `veto` when the candidate already carries another shrink. `global.entries_paused: true` only when `account_pack.bias_severities.overtrading` is also `severe`, `pause_reason` naming both fields. Open positions are still scored normally; decay does not justify dumping a holding thesis, but it downgrades `hold` to `trim` when `health` is 5 or lower.
4. `insufficient_sample` → treat as `healthy` for sizing, but write in `risk_note` that the sample is below 20 if you are asked to raise the floor for another reason.

Gate G3 enforces the floor and the pause independently of you; your added value is deciding which `conditional` entries still make sense and lowering position `health` only where an assumption is actually weakening.

Combine with the bias severities from the trade-journal skill: `warning` plus `chasing` severe is a 0.75 floor even though neither alone requires it. State both fields in `portfolio_note`.

## journal: writing about decay in the daily review

The daily `metrics_table` carries `decay_state` and its inputs. In `JournalDaily`:

- Put the state and the `trade_stats.profit_factor` it rests on into `summary_bullets`, with `closed_count`.
- `what_failed` must name which number moved the state when it is `warning` or `decayed`: `trade_stats.profit_factor`, `returns.max_drawdown_pct` or `trade_stats.sharpe`, each with its value from the table.
- `risks[]`: in `warning`, include that the window has no smoothing and the next closes decide whether the floor rises to 0.75; in `decayed`, include that the 0.75 floor is active and a pause follows if overtrading turns severe.
- `action_items[]`: in `warning` with `closed_count` 20 to 29, profit factor is below its minimum sample, so emit no action item and say so in `abstain_reason` (`do_not_change_yet` is then true by the prompt's rule). At 30 or more, propose at most one tightening (confidence floor, candidates per cycle, cooldown) and set `do_not_change_yet: false`. In `decayed`, propose a review of the scanner factor weights and a comparison against the rules-only baseline, `param` naming the config key; never propose loosening a hard limit.
- `insufficient_sample`: `abstain_reason` set, `do_not_change_yet: true`, no action items.
- `telegram_text` (Korean) names the state in one word so a reader skimming the phone sees it.

In the post-trade review the state is context only: if the trade closed while `decayed`, add one bullet saying so. Decay never changes the `lesson_tag`.

## What not to do

- Do not derive a decay state from the equity numbers yourself; the state is a labelled input. If it is missing, say so in `risk_note` or `abstain_reason`.
- Do not flip between `healthy` and `decayed` interpretations inside one call; apply the label you were given and note in `risk_note` when it differs from the supporting metrics.
- Do not propose a new strategy, a parameter sweep or a re-optimisation; action items are proposals for a human, never instructions to the bot.
- Do not recommend shorting or hedging; on a spot long-only book, defensive means fewer or smaller entries.
- Do not let `healthy` raise a size above the risk-pack suggestion or lower the floor below 0.65; a healthy strategy earns normal limits, never looser ones.
