---
name: asset-allocation
description: 최대 3개 현물 포지션 책의 배분 관점 — 역변동성·CVaR 사이즈 중 작은 값에서 출발, 고상관 보유 종목은 한 베팅, 같은 배치 후보 간 노출 한도 배분과 우선순위를 정하는 법 (수치 규칙은 역할 프롬프트를 따름)
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/asset-allocation/SKILL.md (MIT)"
license: MIT
---
# Allocation for a three-slot spot book

## Paths

risk_manager only: `risk_packs[symbol].inverse_vol_size_pct`, `.cvar_size_pct`, `.daily_vol_pct`, `.corr_to_btc_1h`, `.beta_btc`, `.corr_to_open_positions`; `regime.fused`, `regime.density`, `regime.avg_pairwise_corr_1h`; `account_pack.exposure_pct`, `.position_count`, `.drawdown_ladder_level`; `candidates[].planned_notional_usdt`; your own `max_size_pct`, `rr_net_estimate` and `global.exposure_cap_pct`.

Precedence: your role prompt governs. Step 3 (exposure cap), step 7 (size and haircuts) and step 9 (verdict) are the rules. This skill adds the book view: how candidates in the same batch share the room left, and which one wins when they cannot all fit. It adds no multipliers and no caps of its own.

## The question

Apply once per batch, after each candidate has been judged on its own and before writing the final `max_size_pct`. The question is not "is this trade good" but "how much of the book may it occupy, given what is held and what else is proposed now". With at most three positions of at most 10% each, there is no optimizer; allocation is three checks.

## Check 1: the quieter coin gets the larger slot

The code already turned volatility and tail risk into size suggestions: `inverse_vol_size_pct` (smaller when daily volatility is high) and `cvar_size_pct` (smaller when the one-hour tail is heavy). The prompt starts from the smaller of the two and 10, then applies its haircuts (fused, correlation to holdings or BTC, kurtosis or `hv_pct_rank`). Do not add other multipliers.

Do not give two candidates the same size merely because both passed; their packs differ, so their sizes should too. If both suggestions are `null`, the candidate cannot be sized from data: treat it under the prompt's missing-input rules.

## Check 2: correlated positions merge

Crypto correlations jump toward one exactly when it hurts, so judge the book by its independent bets, not its position count.

- A candidate above 0.8 to a holding is one bet with it: veto (prompt step 4; code blocks it too).
- 0.6 to 0.8: half a bet; the prompt halves the size. Holding sizes are not in the payload, so never reason about a combined size.
- Two candidates in the same batch with high `corr_to_btc_1h` are tied to each other through BTC even without a pairwise figure. Prefer approving one; the other at most `conditional`, or veto when the room is gone.
- When `regime.density` is high without `fused`, say in `portfolio_note` that diversification is mostly nominal.

## Check 3: share the room that is left

Total exposure is capped at 30% by code and by your `global.exposure_cap_pct` (the prompt sets 30, 15 under `fused` or a caution ladder step, or the current exposure when paused). The room left is that cap minus `account_pack.exposure_pct`. If the sizes you intend to approve would not fit, do not shave everything evenly: keep the stronger candidate and veto or shrink the weakest, ranked by lower `rr_net_estimate`, then higher `risk_packs[symbol].daily_vol_pct`, then higher correlation to the book.

- A size below 2 cannot be `approve` (prompt step 9); prefer veto with the reason that the budget is used up.
- With `account_pack.position_count` 2 and two survivors, only one slot remains: choose the one with the lower correlation to the existing book, not the better chart.
- The code clamps anything above the cap, but the journal records what you tried; do not propose sizes that together exceed the room.

## Holdings

There is no periodic rebalance and no "add". The only adjustments are `trim` and `exit`, decided by the prompt's health formula and step 12 (for example, trim under `fused` with high BTC correlation). Position sizes are not visible, so never argue from a holding's weight.

## What not to do

- Do not run mean-variance, risk parity, Black-Litterman or all-weather logic; with three slots it invents inputs you do not have.
- Do not compute volatility or correlation from prices; say "unmeasured" when a field is `null`.
- Do not size up because a candidate diversifies; diversification only allows the normal starting size.
- Do not read low correlation as safety; in a crash all holdings fall together.

## Output

- `max_size_pct`: the prompt's step 7 size, reduced further only by the room left; always ≤ 10.
- `verdict`: `conditional` whenever correlation or the budget changed the size; `veto` when the slot is unavailable.
- `correlation_note`, `conditions`, `veto_reasons` (Korean): `"<field path>: <value> — <짧은 한국어 해석>"`, e.g. `"account_pack.exposure_pct: 24 — 남은 노출 한도 6%, 후보 하나만 수용"`.
- `portfolio_note` (Korean, two sentences at most): how many real bets the book holds and how much room is left. Enum values exactly as the schema spells them.
