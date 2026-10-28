---
name: risk-analysis
description: 코드가 계산한 risk_pack(VaR/CVaR·실현변동성·첨도·CVaR 사이즈)과 account_pack(낙폭·일손익·연패)을 손절 거리·사이즈 상한·거부권으로 옮기는 손실 양수 규약, 스트레스 점검, 금지 규칙
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/risk-analysis/SKILL.md (MIT)"
license: MIT
---
# Risk measurement for the veto

## When to apply

Apply to every candidate and open position in the batch. All numbers are precomputed; you interpret and size against them. Per symbol the pack carries:

- `var95_1h_pct`, `var99_1h_pct`, `cvar95_1h_pct`: historical one-bar (1h) loss quantiles over the recent 200 bars, in percent
- `rv_20_pct`, `rv_60_pct`: annualised realised volatility; `daily_vol_pct`: one-day volatility from 96 hourly returns
- `hv_pct_rank`: where current volatility sits in its own history (0–100); `kurtosis` of hourly returns
- `inverse_vol_size_pct` = min(10, 0.5 / daily_vol_pct × 100); `cvar_size_pct` = min(10, 0.15 / cvar95_1h_pct × 100)
- `stop_vs_atr_noise_ok`: true when 1.5 × ATR% on 1h is still under the 2% stop ceiling

The `account_pack` gives `equity`, `daily_pnl_pct`, `drawdown_pct`, `drawdown_ladder_level`, `exposure_pct`, `position_count`, `consecutive_losses`, `cooldowns`, `decay_state`, `bias_severities`. Per-symbol packs sit under `risk_packs[<symbol>]`; `positions[]` carries no size or stop level.

## Sign convention

Read every VaR/CVaR/drawdown number as a loss magnitude: larger positive means larger loss. Never flip signs in your reasoning. Sanity checks before trusting a pack:

- `cvar95_1h_pct` ≥ `var95_1h_pct` and `var99_1h_pct` ≥ `var95_1h_pct` must hold; otherwise the symbol's risk numbers are unreliable and you `veto` citing the inconsistency, not a made-up figure.
- CVaR usually runs 1.3–1.8× VaR. Above 2, a few violent bars dominate the tail; near 1, the window is suspiciously quiet (fresh listing, dead market).

VaR is a quantile, not a ceiling: one bar in twenty loses more, unboundedly. Size against CVaR, never VaR.

## Procedure per candidate

1. **Stop versus tail.** The stop sits at most 2% below entry. If `cvar95_1h_pct` ≥ the proposed `stop_pct`, an ordinary bad hour stops the trade out; demand a wider stop up to the 2% cap or, if `stop_vs_atr_noise_ok` is false, `veto` ("stop inside noise"). If `var99_1h_pct` > 2%, say in `conditions` that the stop is a noise filter, not tail protection, and cut size.
2. **Size from the tail.** Start from `cvar_size_pct` (budget: a one-bar CVaR event costs ≤ 0.15% of equity). Cross-check with `inverse_vol_size_pct`; take the smaller, then apply external haircuts (fused regime ×0.5, `drawdown_ladder_level` at the caution step (the −3% rung) ×0.5, `decay_state == decayed` → consider 0). The result is `max_size_pct`; it can never exceed 10 and the code will clamp it again.
3. **Daily budget.** A stop hit costs about `size × stop_pct`. With `daily_pnl_pct` already negative, require `|daily_pnl_pct| + size × stop_pct / 100 ≤ 3` so a single loss cannot trigger the daily halt; otherwise shrink `max_size_pct` until it fits or `veto`.
4. **Fat tails.** `kurtosis` > 3 is the norm for crypto; above 8, or with `hv_pct_rank` > 90, treat `var95_1h_pct` as understated by roughly one-third and size from `var99_1h_pct` instead of `cvar95_1h_pct` if it is larger.
5. **Volatility trend.** `rv_20_pct` > 1.5 × `rv_60_pct` means the recent window is wilder than the 200-bar history that produced the VaR; the VaR is stale on the low side. Add a condition tied to `rv_20_pct` and lower size one notch.
6. **Net reward.** No fee field exists; use the fixed friction: `rr_net_estimate` = (`take_profit_pct` − 0.30) / (`stop_pct` + 0.30). Below 1.5, `conditional` at best; the code gate re-checks net R:R.

## Stress check for the book

No simulation and no per-position weights exist, so use the book figure `account_pack.exposure_pct` with three shocks: −10% in one hour (flash), −20% in a day, −40% liquidity vacuum. Worst-case loss (% of equity) ≈ `exposure_pct` × shock / 100, no credit for stops (they gap; at −40% they do not fill). Add it to `daily_pnl_pct` and compare with the −3% daily halt and the ladder (−3% caution: size halved, −4.5% no entries, −6% full stop). If the flash case crosses the next rung, set `global.exposure_cap_pct` to the current `exposure_pct` and say so in `portfolio_note`.

## Open positions

You do not see a holding's stop, but none is wider than the 2% ceiling: if the symbol's `cvar95_1h_pct` exceeds 2 (or 2 + `unrealized_pct` when under water), one ordinary bad hour clears any stop it can have: `assumption_status` includes `weakening`, `health` ≤ 5, `action: trim`. A `red_line_hits` entry or `assumption_status` containing `broken` from the code means `health` ≤ 3 and `action: exit`.

## What not to do

- Do not compute VaR, volatility or drawdown yourself from `last_closes_1h` or account history. If a number is `null`, say the metric is unavailable and size as if the tail were wide (`max_size_pct` ≤ 5) or `veto`.
- Do not assume normality to fill a missing figure, and do not scale 1h VaR to a day by √24; crypto hours are autocorrelated.
- Do not treat a low VaR as bullish; low risk numbers permit size, never create a reason to enter.
- Do not loosen anything the code has set: stop ≤ 2, TP ≤ 4, size ≤ 10, floor ≥ 0.65; your outputs can only tighten.

## Output fields

- `candidates[].verdict`: `veto` when steps 1 or 3 fail or the pack is inconsistent; `conditional` when any haircut applied; `approve` only when all six steps pass unchanged.
- `max_size_pct`, `stop_pct`, `take_profit_pct`, `rr_net_estimate` carry the numbers from the procedure, inside their ranges.
- `veto_reasons` / `conditions` / `required_monitoring`: Korean, each naming the field it rests on (e.g. `risk_packs[symbol].cvar95_1h_pct`, `account_pack.daily_pnl_pct`).
- `global.exposure_cap_pct`, `global.confidence_floor`, `global.entries_paused`: from the stress and ladder checks; `portfolio_note` and `risk_note` in Korean, two sentences at most.
- `confidence`: lower it when packs contain `null` fields or failed sanity checks; never claim certainty the numbers did not give.
