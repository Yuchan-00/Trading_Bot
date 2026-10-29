---
name: thesis-tracker
description: 진입 시 반증 가능한 논지(이유·가정·레드라인·기간)를 쓰고, 매 사이클 리스크 관리자가 자기 payload 의 필드로 가정을 다시 판정해 건강도와 hold/trim/exit 를 정하는 포지션 추적 규약(공식·행동표는 역할 프롬프트 우선)
roles: [risk_manager, head]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/thesis-tracker/SKILL.md (MIT)"
license: MIT
---
# Thesis tracking: commit to the exit before the entry

## Purpose

Without a written thesis, P&L ends up judging the position, which leads to two mistakes: keeping a dead idea because the loss is small, and dropping a live idea after one bad bar. The cure is to write down at entry what would prove the idea wrong, then check exactly that every cycle. Price is the code stop's job. The thesis is yours.

Formulas, thresholds and action tables live in your role prompt, which wins over this skill.

## Who sees what (SPEC v2.1 §20.3a)

The head writes the thesis using its own paths. A later cycle re-checks it with different ones. Write each assumption so that the risk manager can still test it.

- head (candidates): `approved[].technical.*` (including `technical.labels.*`), `approved[].flow.*`, `approved[].news.*`, `approved[].risk.*`, `approved[].gate.*`, `regime.*`, `sentiment.composite.score_0_100`, `sentiment.event_signal[symbol].combined_score`.
- head (held): `flagged_positions[].{thesis, red_line_hits, health_history, risk, technical, flag_reason}`. Its `risk` is the risk manager's `RiskPositionView`, and its `technical` is only `{exit_flag, exit_reason}`.
- risk_manager (held): `positions[].{mark, unrealized_pct, bars_held, thesis, prev_assumption_status, red_line_hits, health_history, technical}` plus the book-level `regime.*`, `risk_packs[symbol].*`, `flow_blocks[symbol].*` and `news_risk_symbols`. A held position comes with no technical direction, score, thesis or confidence; `positions[].technical` carries only `exit_flag` and `exit_reason`.

Once a position is open, the head's paths map to the risk manager's like this:

| written by head | re-checked by risk_manager |
|---|---|
| `technical.*` (mtf_agreement, direction, timing) | `positions[].technical.exit_flag` / `exit_reason` |
| `flow.block_new_longs`, `flow.stop_inside_cascade`, `flow.cascade_risk` | `flow_blocks[symbol].*`, same keys |
| `flow.crowding_risk`, `flow.posture` | no direct field; judge it from `flow_blocks[symbol].*` |
| `news.risk_event_for_symbol` | is the symbol in `news_risk_symbols` |
| `regime.*` | `regime.*`, same keys |
| `risk.*` sizing context | `risk_packs[symbol].*` |

## head: writing the thesis on a BUY

An empty `assumptions` list leaves nothing to re-assess; write the 2–4 the prompt asks for. Field counts, the red-line rules and the horizon clamp come from your prompt, steps 9 and 10.

- `reasons`: each one uses the form "<field path>: <value> — <짧은 한국어 해석>". A reason that cites no field is not a reason.
- `assumptions`: each must be falsifiable within the horizon and must name a field from the table above. Prefer fields the risk manager sees under the same name: `regime.btc_regime_4h` stays bull, `regime.fused` stays false, `flow.block_new_longs` stays false, `news.risk_event_for_symbol` stays false. An assumption about `technical.mtf_agreement` can later be judged only through `exit_flag`, so pair it with one that has a direct field. If you cannot write two checkable assumptions, HOLD.
- `red_lines`: use the schema metrics only, and make each one stricter than the code stop. Follow the prompt: a `price` line sits above the stop price, `hours_held` > `horizon_bars` / 4 is the time line, and `funding_rate` is the raw 8h decimal. Never use the code stop itself as a red line.
- `horizon_bars`: take it from the prompt's setup table (15-minute bars), clamped and halved as step 9 says.
- `reevaluation_triggers`: the bear's and the risk manager's monitoring items, written as field tests.

For a flagged position, apply the prompt's SELL list to `flagged_positions[].red_line_hits`, `risk.action`, `health_history` and `risk.assumption_status`. Never decide from `unrealized_pct`. A healthy thesis showing a loss is a HOLD with `debias.anchored_to_entry` false.

## risk_manager: judging each stored assumption again

`prev_assumption_status` is your own assessment from the previous cycle, and it may be null. It is not a code result. Judge every assumption now, keep the stored order, and use the mapping table above.

| status | what the mapped field shows this cycle |
|---|---|
| `holding` | it reads as the assumption states |
| `weakening` | it is null, or it agrees while a related field moved against it, or the assumption names no field you can see (say so in `reason`) |
| `impaired` | it contradicts the assumption and no hard signal confirms |
| `broken` | it contradicts the assumption and a hard signal confirms: `positions[].technical.exit_flag` true, the symbol in `news_risk_symbols` or `regime.name_alarm_symbols`, or `flow_blocks[symbol].block_new_longs` / `stop_inside_cascade` true |

Upgrade a status only when a cited field now supports it. If `health_history` fell last cycle and no field improved, do not raise any status.

For `red_lines_triggered`, copy `red_line_hits` verbatim. Compute `health` and choose `action` with the prompt's formula and table (section 3C). Do not inflate a status to force an exit.

## Thesis versus price

At −1.5% with every assumption holding, nothing changes; the code stop handles price. At +1% with a broken assumption, trim or exit. In `reason`, name the assumption and its field ("positions[].technical.exit_flag: true — 가정 2 broken"), not the P&L.

## Do not

- Do not write assumptions the payloads cannot show (earnings, valuation, regulation).
- Do not suggest adding to a position. There is no averaging or pyramiding, so the ladder stops at `hold`.
- Do not re-score from price alone.
- Do not soften a broken assumption because the trade is in profit, or harden a weakening one because it is in loss.
- Do not express a bearish read as anything except trim, exit or no entry. This is spot only, so there is no short.

## Output mapping

- head: `decisions[].thesis.{reasons, assumptions, red_lines, horizon_bars}`, `reevaluation_triggers[]`, SELL/HOLD on flagged symbols with a Korean `rationale` that names the deciding assumption, and `debias.anchored_to_entry`.
- risk_manager: `positions[].assumption_status[]` (in stored order), `red_lines_triggered[]`, `health`, `action`, and a Korean `reason` that cites the field.
