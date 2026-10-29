---
name: risk-analysis
description: 코드가 계산한 risk_packs(VaR/CVaR·실현변동성·첨도·역변동성/CVaR 사이즈)와 account_pack(낙폭 단계·일손익·연패·노출)을 손절 적합성·사이즈·거부권 판단으로 읽는 법 — 손실 양수 규약, 일관성 점검, 꼬리 위험 해석 (수치 규칙은 역할 프롬프트를 따름)
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/risk-analysis/SKILL.md (MIT)"
license: MIT
---
# Risk measurement for the veto

## Paths

risk_manager only: `risk_packs[symbol].var95_1h_pct`, `.var99_1h_pct`, `.cvar95_1h_pct`, `.rv_20_pct`, `.rv_60_pct`, `.daily_vol_pct`, `.hv_pct_rank`, `.kurtosis`, `.inverse_vol_size_pct`, `.cvar_size_pct`, `.stop_vs_atr_noise_ok`; `account_pack.daily_pnl_pct`, `.drawdown_pct`, `.drawdown_ladder_level`, `.exposure_pct`, `.position_count`, `.consecutive_losses`, `.decay_state`; `candidates[].technical.stop_pct`, `.tp_pct`, `.noise_check`; `positions[].unrealized_pct`.

Precedence: your role prompt governs. Its steps 1–3 (global), 4 (hard blocks), 6 (stop, target, R:R) and 7 (size) are the rules. This skill explains what the risk numbers mean and how to tell when they cannot be trusted. Never compute VaR, volatility or drawdown yourself; only the prompt's arithmetic is allowed.

## What the fields mean

- `var95_1h_pct`, `var99_1h_pct`, `cvar95_1h_pct`: historical one-hour loss quantiles over the last 200 hourly bars, in %. CVaR95 is the average loss on the worst 5% of hours.
- `rv_20_pct`, `rv_60_pct`: annualised realised volatility over short and longer windows; `daily_vol_pct`: one-day volatility from the last 96 hourly returns.
- `hv_pct_rank`: where current volatility sits in its own history (0–100); `kurtosis`: tail heaviness of hourly returns.
- `inverse_vol_size_pct` and `cvar_size_pct`: code-made size suggestions (both capped at 10). The prompt's starting size is the smaller of the two and 10.
- `stop_vs_atr_noise_ok`: the 1h ATR noise band fits inside the 2% stop ceiling.
- `account_pack.drawdown_ladder_level`: normal / caution / block / stop_all. The account pack has no position sizes or stop levels.

## Sign convention

Every VaR, CVaR and drawdown number is a loss magnitude: larger positive means larger loss. Never flip signs in your reasoning.

## Is the pack trustworthy?

- Ordering must hold: `cvar95_1h_pct` ≥ `var95_1h_pct` and `var99_1h_pct` ≥ `var95_1h_pct`. If not, the symbol's risk numbers are inconsistent; treat the item as contradictory under the prompt's abstain policy and name the fields.
- CVaR usually sits modestly above VaR95. A CVaR far above VaR means a few violent hours dominate the tail; one barely above VaR means the window is suspiciously quiet (new listing, dead market). Say which in `risk_note` or a condition.
- VaR is a quantile, not a ceiling: one hour in twenty loses more, without bound. Never size from VaR alone; the prompt sizes from the CVaR-based and volatility-based suggestions.
- `null` is unknown, never small. A null `cvar95_1h_pct` or `exec_cost_pct`, or a missing pack, is a hard veto (prompt step 4). Other nulls lower your `confidence`; never fill them with a normal-distribution guess or scale a 1h number to a day.

## Reading the tail against the stop

- The stop comes from the technical channel and is never widened. If `cvar95_1h_pct` is at or above the proposed `stop_pct`, an ordinary bad hour stops the trade out. You cannot fix that by widening; note it in `required_monitoring` or `risk_note`. The verdict itself follows the prompt's steps 6 and 9; this observation never changes it on its own.
- `var99_1h_pct` above 2 means the stop filters noise but gives no tail protection: one gap can exceed it. Say so in `required_monitoring`.
- Fat tails: crypto `kurtosis` is normally well above a normal distribution's. The prompt's haircut (`kurtosis` > 6 or `hv_pct_rank` > 70) is the only size change for it, and `hv_pct_rank` > 80 blocks stop tightening.
- `rv_20_pct` clearly above `rv_60_pct` means the recent window is wilder than the 200-bar history behind the VaR, so the VaR understates current risk. Note it in `required_monitoring`; no extra size rule.
- `stop_vs_atr_noise_ok` or `technical.noise_check` not true is a hard veto (step 4). Code gate G1 recomputes noise, so never claim a pass those fields do not show.

## Net reward

Net R:R = (`take_profit_pct` − 0.30) / (`stop_pct` + 0.30), with 0.30% for round-trip fees and slippage. At 1.5 or above it passes; below that, the only repair is tightening the stop as the prompt describes, otherwise veto.

## Account state

- The daily halt is at −3% `daily_pnl_pct`; the ladder runs normal → caution → block → stop_all. With `daily_pnl_pct` already negative, one more stop-out may reach the halt; mention that in `portfolio_note` instead of inventing a new size formula.
- Assume every holding falls together in a shock: crisis correlations go to one and stops gap. `account_pack.exposure_pct` is the honest measure of what a market-wide flash drop can cost; state it plainly when the book is near its cap.
- Low risk numbers permit size; they never create a reason to enter.

## Open positions

You see no stop for a holding, but none is wider than 2%. If the symbol's `cvar95_1h_pct` now exceeds what is left between `unrealized_pct` and a 2% loss, an ordinary bad hour clears any stop it can have: evidence that a risk-related assumption is `weakening`. Compute `health` and `action` with the prompt's formula and rules; a code `red_line_hits` entry goes into `red_lines_triggered` verbatim.

## Output

- `veto_reasons`, `conditions`, `required_monitoring`: Korean, each `"<field path>: <value> — <짧은 한국어 해석>"`, e.g. `"risk_packs[symbol].cvar95_1h_pct: 1.4 — 평범한 나쁜 1시간에도 손절 1.2% 도달 가능"`.
- `confidence` measures input completeness and follows the prompt's caps: ≤ 0.3 when every channel of a candidate is missing, otherwise ≤ 0.4 when any `risk_packs[symbol]`, `bull` or `bear` is null; the lower cap wins. Failed sanity checks lower it further.
- Never loosen a hard limit: stop ≤ 2, target ≤ 4, size ≤ 10, floor ≥ 0.65.
