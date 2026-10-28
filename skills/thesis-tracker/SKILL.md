---
name: thesis-tracker
description: 진입 시 반증 가능한 논지(이유·가정·레드라인·기간)를 쓰고 매 사이클 가정 상태로 건강도 10 − 3·broken − 2·impaired − 1·weakening − 5·red_line 을 매겨 hold/trim/exit 를 정하는 포지션 추적 규약
roles: [risk_manager, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/thesis-tracker/SKILL.md (MIT)"
license: MIT
---
# Thesis tracking: write the exit before the entry, then audit it every cycle

## Why this exists

A position without a written thesis gets judged by its P&L, which produces two classic errors: holding a broken idea because it is "only down a bit", and dumping a working idea after one red bar. The fix is to commit at entry to the conditions under which the idea is wrong, then check those and nothing else, every cycle.

## head: writing the thesis on every BUY

Fill `decisions[].thesis` for every `action: "BUY"`; a BUY with empty `assumptions` is rejected by the code.

- `reasons` (2 to 3 lines, Korean): each line is "because <input field> shows <value> → therefore <judgement>"; a line citing no field is not a reason.
- `assumptions` (2 to 4 lines, Korean): falsifiable within the horizon and re-checkable by the risk manager, who sees for an open position only `technical` (`direction`, `mtf_agreement`, `score`, `exit_flag`, `evidence`), `flow_blocks[symbol]` (`block_new_longs`, `cascade_risk`, `stop_inside_cascade`), `news_risk_symbols`, `regime` (`fused`, `btc_regime_4h`, `name_alarm_symbols`) and `risk_packs[symbol]`. Name one of those fields in every assumption. Good: "technical.direction stays long with mtf_agreement true", "flow_blocks.block_new_longs stays false", "regime.fused stays false". Bad: "momentum continues". If you cannot write two checkable assumptions, the answer is HOLD.
- `red_lines`: numeric and beyond the code stop. Metrics are fixed by the schema: `price`, `unrealized_pct`, `funding_rate`, `btc_return_1h_pct`, `composite_sentiment`, `hours_held`. Include one `price` or `unrealized_pct` line inside the stop distance that means the idea failed before the stop (e.g. `unrealized_pct < -1.2` with a 2.0 stop), one external line (`funding_rate > 0.0005` or `btc_return_1h_pct < -2`), and one `hours_held` line equal to the horizon so a stalled trade is reviewed.
- `horizon_bars`: 15-minute bars the setup needs, from `bull.expected_horizon_bars`, or the bear's `time_to_impact_bars` on a `high`-band death path when that is shorter.

Also populate `reevaluation_triggers` with the one observable that would reopen the decision before any red line (typically the bear's `strongest_point` turned into a field condition).

For a flagged open position, decide SELL or HOLD from the risk manager's `positions[]` view, not from price: `health` ≤ 4 for two consecutive cycles or any `red_lines_triggered` entry is a SELL with `confidence` ≥ 0.65; a healthy thesis with negative `unrealized_pct` is a HOLD with `debias.anchored_to_entry` false.

## risk_manager: re-scoring every open position

Red lines are the only part the code evaluates: copy `red_line_hits[]` verbatim. Assumption status is your judgement, one per stored assumption, in order, from the field that assumption names:

| status | the named field |
|---|---|
| `holding` | reads as the assumption states |
| `weakening` | is `null`, or still agrees while a second field moved against it (e.g. `technical.direction` long but `score` fell to 0 or below; `bars_held` past `horizon_bars` × 0.75 without the target) |
| `impaired` | contradicts the assumption this cycle while `technical.exit_flag` is false |
| `broken` | contradicts it and a hard signal confirms: `exit_flag` true, the symbol in `news_risk_symbols` or `regime.name_alarm_symbols`, `flow_blocks.block_new_longs` or `stop_inside_cascade` true |

An assumption that names no field you can see is `weakening`, never `holding`; say so in `reason`. When `health_history` fell last cycle and no field improved, do not raise a status.

1. Compute `health` = 10 − 3 × (broken) − 2 × (impaired) − 1 × (weakening) − 5 × (red lines triggered), clamped to 1..10.
2. Map to `action`:

| health | action |
|---|---|
| 8–10 | `hold` |
| 6–7 | `hold`, `reason` names the weakening assumption and what would downgrade it |
| 4–5 | `trim` |
| 1–3, or any `red_lines_triggered` | `exit` |

3. Cross-check the technical channel: `technical.exit_flag` true with `health` ≥ 8 means thesis and chart disagree; downgrade to `trim` and cite both fields in `reason`. The code executes `exit` only after a red line or two consecutive cycles at ≤ 4; do not force it by overstating a status.

## Distinguishing thesis from price

A position at −1.5% with every assumption `holding` stays `hold`; the code stop judges price. A position at +1% with an `impaired` structure assumption is `trim` or `exit`. Write `reason` so the Telegram reader sees the assumption, not the P&L ("technical.direction flat 전환으로 가정 2 impaired", not "−1.5% 손실").

## What not to do

- Do not write assumptions about earnings, valuation, regulation outlooks or anything the payload cannot show.
- Do not recommend adding to a position; "add" does not exist here (no averaging, no pyramiding). The health ladder tops out at `hold`.
- Do not re-score from price alone or from `last_closes_1h`; the named fields and the red-line hits are the evidence.
- Do not soften a `broken` because the trade is in profit, or harden a `weakening` because it is in loss.
- Do not put the code stop itself as a red line; red lines are additional conditions.
- Do not express a bearish re-read as anything but `trim` / `exit` / "no entry"; there is no short side.

## Output mapping

head: `decisions[].thesis.{reasons, assumptions, red_lines, horizon_bars}`, `reevaluation_triggers[]`, `decisions[].action` SELL/HOLD for flagged positions with `rationale` (Korean, ≤ 60 words) naming the assumption that decided it, `debias.anchored_to_entry`.

risk_manager: `positions[].assumption_status[]` (stored order), `positions[].red_lines_triggered[]` (`red_line_hits` verbatim), `positions[].health` (1..10 by the formula), `positions[].action` (`hold` / `trim` / `exit`), `positions[].reason` (Korean, one or two sentences citing the field), and a line in `risk_note` when any position is below 5.
