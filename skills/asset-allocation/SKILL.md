---
name: asset-allocation
description: 최대 3개 현물 포지션 책을 위한 배분 규약 — 역변동성·CVaR 사이즈 중 작은 값 채택, 상관 0.8 초과 보유 종목은 한 베팅으로 간주, 총노출 30% 예산을 전체 포트폴리오 관점에서 배분하는 절차
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/asset-allocation/SKILL.md (MIT)"
license: MIT
---
# Allocation for a three-slot spot book

## When to apply

Apply once per batch, after judging each candidate on its own merits and before writing `max_size_pct`. The question is not "is this trade good" but "how much of the book may it occupy, given what is held and what else is proposed this cycle". With at most 3 positions of 10% each there is no optimizer; allocation reduces to three checks.

Relevant inputs: `risk_pack.inverse_vol_size_pct`, `risk_pack.cvar_size_pct`, `risk_pack.daily_vol_pct`, `risk_pack.corr_to_btc_1h`, `risk_pack.corr_to_open_positions`, `risk_pack.beta_btc`, `regime_pack.fused`, `regime_pack.avg_pairwise_corr_1h`, `regime_pack.density`, `account_pack.exposure_pct`, `account_pack.position_count`, `account_pack.drawdown_ladder_level`, and the planned sizes of the other candidates in the same batch.

## Check 1: volatility-scaled slot size

The code already turned volatility into a size suggestion with the inverse-volatility rule `w_i ∝ 1/σ_i`, expressed as `inverse_vol_size_pct = min(10, 0.5 / daily_vol_pct × 100)`, and a tail-budget rule `cvar_size_pct = min(10, 0.15 / risk_pack.cvar95_1h_pct × 100)`. Your starting size is the smaller of the two. The quieter candidate gets the larger slot; never give two candidates the same size just because both passed. If both fields are `null`, start from 5 and say in `conditions` that volatility is unmeasured.

Reduce the starting size by these multipliers, in this order, and round down to a half-percent:

| Condition | Multiplier |
|---|---|
| `regime_pack.fused` true | 0.5 |
| `account_pack.drawdown_ladder_level` ≥ 1 | 0.5 |
| `risk_pack.rv_20_pct` > 1.5 × `risk_pack.rv_60_pct` (volatility expanding) | 0.75 |
| second or third position opened this same cycle | 0.75 |

The result is the per-slot ceiling; it can only go down from here.

## Check 2: correlation makes positions merge

Realised correlation between crypto pairs jumps toward 1 exactly when it hurts, so the book is judged by its effective number of independent bets, not by its position count. Read `risk_pack.corr_to_open_positions` (1h-return correlation between the candidate and each open position):

- any value with |corr| > 0.8: the candidate and that position are one bet. The code gate already drops such candidates; if one still reaches you, `veto` with `correlation_note` citing the field.
- 0.6 < |corr| ≤ 0.8: half a bet. Cap the candidate so that candidate size + correlated position size ≤ 10%, and write the shared exposure in `correlation_note`.
- |corr| ≤ 0.6: independent for sizing purposes.

Do the same for two candidates proposed in the same batch using their individual `corr_to_btc_1h` and `beta_btc`: two candidates with `corr_to_btc_1h` > 0.7 each are correlated with each other through the market factor even if no pairwise number is given. Approve at most one at full size; the other is `conditional` with a size that keeps their sum ≤ 10%, or `veto` if the stronger candidate already fills the slot.

When `regime_pack.density` > 0.6 or `avg_pairwise_corr_1h` > 0.7 even without `fused`, state in `portfolio_note` that diversification is currently nominal and set `global.exposure_cap_pct` to at most 20.

## Check 3: the exposure budget

Total exposure is capped at 30% by code. You hand out the remaining budget: `remaining = min(30, global.exposure_cap_pct) − account_pack.exposure_pct`. Sum the sizes you intend to approve; if the sum exceeds `remaining`, cut the lowest-quality candidate first (lowest `rr_net_estimate`, then highest `daily_vol_pct`) rather than shaving everything proportionally. A 1% slot is not worth monitoring; below 2% prefer `veto` with the reason "budget exhausted".

If `account_pack.position_count` is 2 and two candidates survive, only one can be approved: the one with the lower correlation to the existing book, not the better chart.

## Rebalancing stance

There is no periodic rebalance and no "add"; the only portfolio adjustments are `trim` and `exit` on `positions[]`. Suggest `trim` when a position has grown past 12% of equity through appreciation (`unrealized_pct` applied to its entry size) or when its correlation to a newly approved candidate would push the merged bet above 10%; otherwise leave SL/TP to the code.

## What not to do

- Do not run or describe mean-variance, Black-Litterman, risk parity or all-weather logic; with three slots the result is meaningless and invents inputs you do not have.
- Do not compute correlation or volatility from `last_closes_1h`; use the pack fields and say "unmeasured" when they are `null`.
- Do not size up a candidate because it diversifies; diversification only ever permits the starting size, it never adds to it.
- Do not read a low correlation as safety. Crisis correlation goes to 1; the stress figures in `portfolio_note` must assume all positions fall together.
- Do not propose weights that sum above the remaining budget; the code will clamp, but the journal records that you tried.

## Output mapping

- `candidates[].max_size_pct`: the per-slot ceiling from Check 1 after the correlation caps of Check 2 and the budget cut of Check 3; always ≤ 10.
- `candidates[].verdict`: `conditional` whenever a correlation or budget cap changed the size; `veto` when the slot is unavailable (merged bet, position count, budget < 2%).
- `candidates[].correlation_note` (Korean): the binding correlation field and the merged exposure, e.g. "risk_pack.corr_to_open_positions 0.72 → 합산 9%".
- `candidates[].conditions` / `veto_reasons` (Korean): each item names the field it rests on.
- `global.exposure_cap_pct`: lower than 30 when density or correlation says the book is one bet.
- `global.portfolio_note` (Korean, ≤ 2 sentences): effective number of bets and the remaining budget.
- `positions[].action`: `trim` with `reason` citing the oversized or merged exposure; never an "add".
