---
name: minute-analysis
description: 세션 VWAP 거리·시간대 대비 거래량·비정상 거래량 배수 같은 장중 라벨로 진입 타이밍과 확신도를 조정하는 법
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/minute-analysis/SKILL.md (MIT)"
license: MIT
---
# Intraday context — VWAP and volume-of-day labels

## When to apply

Use on every technical call. The bot runs on 15-minute cycles, so intraday price-versus-volume context decides *when* to act on a setup the other channels have identified. All values are precomputed; read them from the feature pack:

- `vwap_distance_pct` — current price versus the session VWAP built from 15m bars (anchored to the UTC day), in %. VWAP = Σ(typical price × volume) / Σ(volume), typical price = (high + low + close) / 3.
- `cgo_60` — price / 60-bar 1h VWAP − 1, in %. A proxy for the average holder's unrealized gain.
- `volume_vs_hour_avg` — last 15m volume divided by the mean of the previous 4 bars.
- `abnormal_volume_multiple` — last 15m volume divided by the median of the previous 96 bars (one day).
- `per_tf.<tf>.volume_ratio_20` — volume relative to its 20-bar average on each timeframe.
- `change_24h_pct` and `last_closes_1h` (20 values) for coarse context only.

## Procedure

1. **Locate price against VWAP.**
   - `vwap_distance_pct` between −0.5 and +1.0 → price is near fair value for the session; `timing: "now"` is acceptable if the structure channel agrees.
   - `vwap_distance_pct` > +1.5 → price is extended above the session average. Set `timing: "wait_pullback"` and add `vwap_distance_pct` to `evidence`. Above +3.0 with `abnormal_volume_multiple` < 2 → this is a thin spike; `direction` cannot be `long`.
   - `vwap_distance_pct` < −1.5 → price is below the session average. Allowed for `setup_type: "mean_reversion"` only when `per_tf.15m.vote` or `per_tf.1h.vote` is `mean_reversion_long`; otherwise treat as a downtrend day and keep `direction: "flat"`.
2. **Check volume confirmation.**
   - A breakout or trend-continuation setup needs `volume_vs_hour_avg` ≥ 1.2 or `per_tf.15m.volume_ratio_20` ≥ 1.2. Without either, downgrade `timing` to `wait_confirmation` and cap `confidence` at 0.55.
   - `abnormal_volume_multiple` ≥ 3.0 is a two-sided signal: participation is real, but the move is being chased. Combine with `change_24h_pct`: if the 24h change is already above +10, do not enter now (`timing: "wait_pullback"`, `confidence` − 0.10). If the 24h change is modest (< +5) and the structure is bullish, the volume surge supports `timing: "now"`.
   - `volume_vs_hour_avg` < 0.5 → the current bar is quiet relative to the hour; treat any breakout as unconfirmed.
3. **Read holder positioning via `cgo_60`.**
   - `cgo_60` > +20 → most recent buyers sit on large gains and may sell into strength; subtract 1 from `score` for continuation setups.
   - `cgo_60` < −30 → selling pressure from trapped holders is likely exhausted; add 1 to `score` for mean-reversion setups when volume confirms.
   - Values in between carry no adjustment.
4. **Sanity-check the stop against intraday rotation.** Do not build a stop from VWAP; `sl_reference` only allows `zone`, `kijun`, `atr` or `none`, so the stop always comes from the structure, Kijun or ATR channels. Use VWAP only as a fragility test: if `vwap_distance_pct` is positive and larger than the chosen `stop_pct`, price can hit the stop before it even revisits the session average, which means the stop sits inside ordinary intraday rotation. In that case keep the structural stop, set `noise_check: false`, mention the fragility in `invalidation`, and prefer `timing: "wait_pullback"` so the entry moves closer to the stop.
5. **Write the invalidation line** in market terms, for example a 15m close back below the session VWAP, or `volume_vs_hour_avg` staying below 1.0 for the next 4 bars after a breakout.

## Score contribution (into `score`)

| Condition | Points |
|---|---|
| `vwap_distance_pct` in [−0.5, +1.0] with bullish structure | +1 |
| `volume_vs_hour_avg` ≥ 1.2 on a breakout/continuation setup | +1 |
| `abnormal_volume_multiple` ≥ 3 and `change_24h_pct` > 10 | −1 |
| `vwap_distance_pct` > 3.0 | −1 |
| `cgo_60` > 20 (continuation) | −1 |
| `cgo_60` < −30 (mean reversion, volume confirmed) | +1 |

## Open positions

- Price has fallen more than 1.0% below the session VWAP (`vwap_distance_pct` < −1.0) after an entry made above it, with `volume_vs_hour_avg` ≥ 1.5 → report the deterioration in `evidence`; set `exit_flag: true` only if the `position.thesis` text you receive mentions VWAP or a structure label (`smc_1h.structure`, `per_tf.1h.vote`) also turned bearish.
- A spike in `abnormal_volume_multiple` ≥ 4 on a bar closing lower is a distribution warning; mention it in `exit_reason` when combined with a structure break.

## What NOT to do

- Do not reconstruct VWAP, TWAP or hourly volume profiles from `last_closes_1h`; those closes have no volume attached and are for shape only.
- Do not use `change_24h_pct` as a momentum signal by itself; a large positive value is a chase warning, not a reason to buy.
- Do not translate "below VWAP" into a short. Below-VWAP weakness is `flat`, `avoid` or `exit_flag`.
- Do not request more bars, other intervals or tick data; the labels are all that exists.
- Do not treat `None` labels as neutral confirmation; if `vwap_distance_pct` or the volume fields are missing, say so in `evidence` and keep `timing: "wait_confirmation"`.

## Output mapping summary

`timing` (now / wait_pullback / wait_confirmation), `confidence` caps and adjustments, `score` ±1 per row above, `direction` downgrade on thin spikes, `noise_check: false` when the stop sits inside VWAP rotation, `invalidation` phrasing, `exit_flag` / `exit_reason` for positions, and `evidence` items such as `vwap_distance_pct: 0.4`, `volume_vs_hour_avg: 1.6`, `abnormal_volume_multiple: 3.4`, `cgo_60: -12`.
