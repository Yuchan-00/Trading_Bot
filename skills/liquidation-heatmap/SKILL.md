---
name: liquidation-heatmap
description: 코드가 추정한 청산 군집(est_liq_clusters)과 연쇄청산 위험 라벨을 읽어 손절이 청산대 안에 놓이는 진입을 차단하고 워시아웃 후 셋업을 식별하는 법
roles: [derivatives_flow]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/liquidation-heatmap/SKILL.md (MIT)"
license: MIT
---
# Estimated liquidation clusters and cascade risk

## When to apply

Apply whenever `flow_pack.est_liq_clusters` is non-empty or `cascade_risk` is not `unknown`. The bot has no paid heatmap. Code projects where leveraged longs would be force-closed from the 24h VWAP using the standard formula and three leverage tiers, then measures how far the nearest projected long cluster sits below price. Your task is to decide whether a 2% spot stop would be sitting inside that forced-selling zone, and whether a wipe has already happened.

Inputs: `est_liq_clusters` (each `{level, side, leverage, estimated}`), `nearest_long_liq_below_pct`, `cascade_risk`, `oi_change_24h_pct`, `oi_funding_label`, `funding_regime`, `ls_label`, `divergence_flags`, `data_confidence`.

## Definitions (interpretation only)

- Long liquidation level ≈ entry × (1 − 1/leverage + maintenance margin). With 0.5% maintenance, 25x sits about 3.5% below the reference price, 50x about 1.5%, 100x about 0.5%.
- `nearest_long_liq_below_pct` is the distance in percent from current price down to the closest projected long cluster.
- `cascade_risk` as set by code: `high` when nearest cluster < 2% and `oi_funding_label == "leveraged_long_buildup"`; `moderate` when nearest < 3% or `funding_regime == "extreme_long"`; otherwise `low`.
- Every cluster is `estimated: true`. These are statistical projections that shift as positions open and close, so treat them as zones, never as exact prices.

## Why it matters for a spot buyer

A cluster of over-leveraged longs just below price is a pool of forced market sells waiting for a small dip. Price tends to be pulled toward such pools, and when one is hit the selling can push into the next. A spot position with a 2% stop inside that zone is very likely to be stopped at the worst moment. Conversely, once a pool has been flushed, the weak hands are gone and the level often turns into support.

## Procedure

1. Copy `cascade_risk` from the pack into the output field of the same name. The output enum has no `unknown`: when the pack says `unknown`, emit `moderate` (the conservative default) and cite `cascade_risk: unknown` in `evidence`. Never lower the code value, and do not raise it on your own reading of cluster spacing; the projected tiers are always a fixed distance apart, so spacing carries no information.
2. Copy `nearest_long_liq_below_pct` into `nearest_long_liq_cluster_pct_below` (same number, `None` stays `None`).
3. `stop_inside_cascade`: true when `nearest_long_liq_below_pct` ≤ 2.0, because the hard stop limit is 2% and the stop would be placed at or beyond the cluster. Also true when `cascade_risk == "high"` regardless of distance.
4. `block_new_longs`: true when `stop_inside_cascade` is true; `block_reason` in Korean citing `nearest_long_liq_below_pct` and the cluster leverage, for example "50x 추정 청산 군집이 1.4% 아래에 있어 손절이 연쇄청산 구간 안에 놓임".
5. Washout recognition. All of the following must hold to set `washout_setup: true`:
   - `oi_funding_label == "deleveraging"` (OI fell more than 5% in 24h; the leverage has been removed),
   - `funding_regime` in {`negative`, `squeeze`} (the survivors are shorts paying up),
   - `ls_label != "crowded_long"`,
   - `nearest_long_liq_below_pct` is `None` or ≥ 3.0 (the nearest remaining pool is not in stop range),
   - `data_confidence == "high"`.
   When set, `posture` is `capitulation` or `fear`, `squeeze_direction` is `up`, `flow_bias` may be `long`, and `cascade_risk` should read `low`.
6. During an active flush, meaning `oi_change_24h_pct` < −5 but `funding_regime` still `elevated_long` or `extreme_long`, the unwind is incomplete. Set `flow_bias: "avoid"`, `squeeze_direction: "down"`, and do not flag a washout; there is no reason to be the first buyer.
7. Magnet asymmetry (optional note): if clusters exist only on the long side below price and none above, mention in `market_wide_note` (Korean) that the path of least resistance is down until those pools clear. Do not turn this into a bias on its own; it only supports `avoid` when crowding is also present.
8. `confidence`: cap at 0.6 whenever `data_confidence == "low"` and at 0.3 when the cluster list is empty with pack `cascade_risk == "unknown"`. Cluster projections never add more than 0.1 to confidence.
9. `evidence`: cite `nearest_long_liq_below_pct: 1.4`, `cascade_risk: high`, `oi_funding_label: deleveraging` and similar; do not list raw cluster prices.

## Reading OI changes around a flush

| `oi_change_24h_pct` | `funding_regime` | state | action |
|---|---|---|---|
| > +5 | `elevated_long` or `extreme_long` | leverage building | `crowding_risk: "high"`, block |
| < −5 | still positive bands | flush in progress | `flow_bias: "avoid"` |
| < −5 | `negative` or `squeeze` | flush done | washout candidate (step 5) |
| within ±2 | `neutral` | quiet | no cluster-based adjustment |

## What NOT to do

- Do not invent liquidation volumes in USD; the bot has no forced-order feed, so there is no 24h liquidation total to cite.
- Do not quote cluster levels as support or resistance prices for the technical channel; you do not output price levels, only distances and flags.
- Do not lower `cascade_risk` below the code value.
- Do not set `washout_setup` on a negative funding print alone or on an OI drop alone.
- Do not propose widening the stop past 2% to get outside the cluster; the answer is no entry.
- Do not express a short view; a dense long cluster below price yields `avoid` and `block_new_longs`, nothing more.

## Output mapping summary

`cascade_risk`, `nearest_long_liq_cluster_pct_below`, `stop_inside_cascade`, `block_new_longs`, `block_reason`, `washout_setup`, `squeeze_direction`, `posture`, `flow_bias`, `confidence`, `evidence`, `market_wide_note`.
