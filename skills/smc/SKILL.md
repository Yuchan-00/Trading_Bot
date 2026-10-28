---
name: smc
description: 코드가 계산한 SMC 구조 라벨(BOS/ChoCH/FVG/수요·공급 존)을 읽어 진입 방향·구조적 손절 후보·무효화 조건을 정하는 법
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/smc/SKILL.md (MIT)"
license: MIT
---
# Smart Money Concepts — structure read for a spot long

## When to apply

Use this skill whenever `feature_pack.smc_1h` or `feature_pack.smc_4h` is present and not `unknown`. The structure labels are already computed in code from swing points (5-bar fractals) and 3-bar gaps; your job is to interpret them, not to re-derive them from `last_closes_1h`. If both SMC blocks are `None`, treat the structure channel as silent and rely on the other feature labels.

## What the labels mean

- `structure`:
  - `bos_up` — close pushed through the previous swing high while the trend was already up: continuation.
  - `choch_up` — close pushed through the previous swing high while the trend was down: the first sign of a reversal upward. Weighted above a plain BOS.
  - `bos_down` / `choch_down` — the mirrored bearish events. For this bot they only ever mean "no entry" or "exit a held long".
  - `range` — no swing break inside the window; structure gives no direction.
- `fvg_dir`: the most recent unfilled 3-candle imbalance in the last 20 bars. A `bullish` gap (bar-3 low above bar-1 high) acts as a same-direction filter for longs; a `bearish` gap is a magnet below price that may be revisited.
- `nearest_demand_zone` `[low, high]`: the last down candle immediately before a bullish structure break. Price often reacts here on a retest. `nearest_supply_zone` is the mirror image above price.
- `distance_to_demand_pct` / `distance_to_supply_pct`: percent distance from the current `price` to the zone edge.

## Procedure

1. Read the higher timeframe first. `smc_4h.structure` sets the bias:
   - `bos_up` or `choch_up` → long bias allowed.
   - `range` → long only if `per_tf.1h.vote` and `smc_1h` both agree on an upward structure.
   - `bos_down` or `choch_down` → `direction` cannot be `long`; use `flat` (or `avoid` if `smc_1h` is also bearish).
2. Read the 1h block for timing:
   - `smc_1h.structure` in {`bos_up`, `choch_up`} and `smc_1h.fvg_dir == "bullish"` → structure supports entry; `timing: "now"` if `distance_to_demand_pct` ≤ 1.0, otherwise `timing: "wait_pullback"` (the retest of the demand zone or gap is the better fill).
   - Bullish structure but `fvg_dir == "none"` → weaker confirmation; cap `score` at +2 from this channel and set `timing: "wait_confirmation"`.
   - Bullish structure but `fvg_dir == "bearish"` → conflicting; do not add points from SMC.
3. Pick `setup_type`:
   - `choch_up` on 1h with 4h `range` or `choch_up` → `breakout` (early reversal).
   - `bos_up` on both timeframes → `trend_continuation`.
   - Price sitting on `nearest_demand_zone` with 1h `bos_up` intact → `mean_reversion` is acceptable only if `per_tf.1h.rsi_14` < 40.
4. Build the stop from the zone. The structural stop is just below the demand zone: `stop_candidate_price = nearest_demand_zone[0] * (1 - 0.001)`. Compute `stop_pct = (price - stop_candidate_price) / price * 100`. The hard cap is 2.0:
   - `stop_pct` ≤ 2.0 → `sl_reference: "zone"`.
   - `stop_pct` > 2.0 → the structural stop is too wide for this bot. Either (a) set `timing: "wait_pullback"` so the entry moves closer to the zone, or (b) fall back to an ATR or Kijun stop (`sl_reference: "atr"` / `"kijun"`) and explicitly lower `confidence` by 0.10, because the stop no longer sits behind structure. Never emit `stop_pct` above 2.0.
5. Build the target from supply. `target_candidate_price = nearest_supply_zone[0]` when it exists, else price × (1 + 2 × stop_pct / 100). `tp_pct` is capped at 4.0; if the supply zone is further away than 4%, cap `tp_pct` at 4.0 and keep `target_candidate_price` consistent with it. Require `tp_pct ≥ 1.5 × stop_pct` or drop the setup to `direction: "flat"`.
6. Write `invalidation` as a price-level statement in Korean referring to the zone, for example the close falling back below the demand-zone low or a new `choch_down` on 1h.

## Score contribution (into `score`, −5..+5 overall)

| Condition | Points |
|---|---|
| `smc_4h.structure` ∈ {bos_up, choch_up} | +1 |
| `smc_1h.structure == "choch_up"` | +2 |
| `smc_1h.structure == "bos_up"` | +1 |
| `smc_1h.fvg_dir == "bullish"` | +1 |
| `distance_to_demand_pct` ≤ 1.0 (price at the zone) | +1 |
| `smc_1h.structure` ∈ {bos_down, choch_down} | −2 |
| `smc_4h.structure == "choch_down"` | −2 |
| `smc_1h.fvg_dir == "bearish"` while bias is long | −1 |

Combine with the other technical channels; SMC alone should never carry the whole score.

## Open positions

When a position is supplied, check for `exit_flag`:
- `smc_1h.structure == "choch_down"` → `exit_flag: true`, `exit_reason` names the field (structure flipped against the long).
- `smc_4h.structure == "bos_down"` → `exit_flag: true`.
- `bos_down` on 1h only, with 4h still bullish → keep `exit_flag: false` but mention the weakening in `evidence` and tighten the `invalidation` line.

## What NOT to do

- Do not infer BOS, ChoCH, gaps or zones yourself from `last_closes_1h`; use only the labelled fields.
- Do not express a bearish structure as a short. The only bearish outputs are `direction: "flat"` / `"avoid"` and `exit_flag`.
- Do not stretch `stop_pct` beyond 2.0 or `tp_pct` beyond 4.0 to "fit" a zone.
- Do not treat a `range` label as neutral-bullish; it is the absence of a signal.
- Do not cite a zone that is `None`.

## Evidence format

Each `evidence` item must name the field, e.g. `smc_1h.structure: choch_up`, `smc_1h.fvg_dir: bullish`, `smc_1h.distance_to_demand_pct: 0.6`. Keep at most three items; prefer the ones that decided `direction` and `sl_reference`.
