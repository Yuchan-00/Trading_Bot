---
name: volatility
description: 실현변동성 백분위(hv_pct_rank)와 ATR%를 읽어 변동성 레짐별 진입 허용·사이즈 축소·손절 노이즈 점검을 하는 법
roles: [technical, risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/volatility/SKILL.md (MIT)"
license: MIT
---
# Volatility regime — percentile bands, not direction

## When to apply

Apply on every call where `hv_pct_rank` is present (`feature_pack.hv_pct_rank` for the technical role, `risk_pack.hv_pct_rank` for the risk manager). The number is computed in code: realized volatility of 1h log returns over 24 bars, ranked as a percentile against at least 100 prior windows. `None` means the window is not yet filled; treat the regime as unknown and give no volatility-based credit or penalty.

Underlying definitions (for interpretation only; never recompute):
- realized vol = std(log returns, 24 bars) × sqrt(bars per year); the bot uses 365 days × 24 for 1h data because crypto trades continuously.
- `hv_pct_rank` = percentile of the current window within the lookback, 0..100.
- `atr_pct_1h` = ATR(14) on 1h as % of price; `risk_pack.daily_vol_pct` = std(1h returns, 96 bars) × sqrt(24) × 100.

## Core idea

Volatility clusters and mean-reverts. A low percentile says expansion is statistically more likely soon; a high percentile says contraction is more likely. Neither says which way price goes. A low-vol reading is a permission slip for a breakout setup, never a buy signal on its own; a high-vol reading is a reason to shrink or skip, never a reason to sell short (spot-only bot).

## Bands

| `hv_pct_rank` | Regime | Technical role | Risk manager |
|---|---|---|---|
| < 20 | compressed | breakout watch; `setup_type: "breakout"` allowed if structure or trend vote agrees | normal size; note that the first expansion bar can be violent |
| 20–50 | calm | no adjustment | no adjustment |
| 50–80 | active | require `mtf_agreement` for `direction: "long"`; `timing` defaults to `wait_pullback` | `max_size_pct` ≤ 7.5 |
| > 80 | stressed | no new `long` unless `setup_type == "mean_reversion"` with `per_tf.1h.rsi_14` < 30; subtract 0.10 from `confidence` | `max_size_pct` ≤ 5; add a condition requiring `hv_pct_rank` to fall below 80 before a second entry in the same symbol; prefer `conditional` over `approve` |
| > 95 | extreme | `direction: "avoid"` for new entries; open positions: tighten `invalidation` and set `exit_flag` only if structure also breaks | `veto` unless `rr_net_estimate` ≥ 2.0 and `stop_vs_atr_noise_ok` is true |

## Technical role procedure

1. Read `hv_pct_rank` and `atr_pct_1h` from the feature pack.
2. Noise check for the 2% stop: `noise_check` is true only when `atr_pct_1h × 1.5 < 2.0`. If false, the hard stop sits inside ordinary hourly swings; say so in `invalidation`, set `confidence` no higher than 0.50, and prefer `timing: "wait_pullback"` so the entry moves closer to the structural stop. Do not try to fix this by widening `stop_pct` past 2.0.
3. If no structural stop is available, use an ATR stop: `stop_pct = min(2.0, 1.5 × atr_pct_1h)` with `sl_reference: "atr"`. When 1.5 × ATR already exceeds 2.0, the ATR stop is truncated, which is another reason to lower `confidence`.
4. Target sanity: `tp_pct` should be reachable within the holding horizon. As a rule of thumb, `tp_pct` ≤ 3 × `atr_pct_1h` and ≤ 4.0; if `tp_pct` < 1.5 × `stop_pct` after these caps, downgrade to `direction: "flat"`.
5. Score contribution: +1 when `hv_pct_rank` < 20 and a breakout structure exists; −1 when `hv_pct_rank` > 80; 0 otherwise.
6. Evidence items: `hv_pct_rank: 17`, `atr_pct_1h: 0.9`, and the structure field that justified the breakout.

## Risk manager procedure

1. Compare the three size suggestions in the risk pack: `inverse_vol_size_pct` (0.5 / daily_vol_pct × 100, capped at 10), `cvar_size_pct` (0.15 / cvar95_1h_pct × 100, capped at 10), and the band cap from the table above. `max_size_pct` = the minimum of these, then halved when `regime_pack.fused` is true. Never exceed 10.
2. Stop: when `stop_vs_atr_noise_ok` is false, either set `stop_pct` to the largest value that the net R:R rule still allows and record a `conditions` entry that the entry be taken on a pullback, or `veto` with the reason naming `stop_vs_atr_noise_ok`.
3. Net R:R: `rr_net_estimate = (take_profit_pct − 0.30) / (stop_pct + 0.30)` using the 0.30% round-trip cost assumption. Below 1.5 → `veto` or tighten `stop_pct` (never widen `take_profit_pct`).
4. Trend of vol: if `rv_20_pct` > 1.3 × `rv_60_pct`, volatility is expanding right now. Add `required_monitoring` such as "1h realized vol stops rising" and prefer `conditional`.
5. Portfolio level: when two or more candidates or open positions have `hv_pct_rank` > 80 at the same time, lower `global.exposure_cap_pct` (for example to 20) and say why in `portfolio_note`. Raise `global.confidence_floor` to 0.75 if the stressed symbol is also `fused`.
6. Open positions: a jump of `hv_pct_rank` from below 50 to above 80 since entry is a `weakening` assumption, not `broken` by itself; combine with red lines before recommending `exit`.

## What NOT to do

- Do not read a low percentile as bullish or a high one as bearish.
- Do not compute percentiles, standard deviations or ATR from `last_closes_1h`; the 20 closes are context, not a dataset.
- Do not widen `stop_pct` above 2.0 or `take_profit_pct` above 4.0 because volatility is high; shrink size or skip instead.
- Do not emit a short view. The bearish outputs are `flat`, `avoid`, `veto`, `trim` and `exit`.
- Do not give volatility credit when `hv_pct_rank` is `None`.

## Output mapping summary

Technical: `noise_check`, `sl_reference` ("atr" fallback), `stop_pct`, `tp_pct`, `setup_type` ("breakout" in compressed regime), `confidence` adjustment, `score` ±1, `evidence`.
Risk manager: `max_size_pct`, `stop_pct`, `take_profit_pct`, `rr_net_estimate`, `conditions`, `required_monitoring`, `verdict`, `global.exposure_cap_pct`, `global.confidence_floor`, `portfolio_note`, and `assumption_status` for positions.
