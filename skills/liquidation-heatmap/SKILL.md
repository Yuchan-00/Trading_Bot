---
name: liquidation-heatmap
description: 코드가 추정한 청산 군집(est_liq_clusters)·최근접 거리·연쇄청산 라벨을 읽어 손절이 청산 구간 안에 놓이는 진입을 차단·경고하고, 플러시 완료와 진행 중을 구분하는 법 (군집은 진입 근거가 될 수 없음)
roles: [derivatives_flow]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/liquidation-heatmap/SKILL.md (MIT)"
license: MIT
---
# Estimated liquidation clusters: block or warn, never buy

## Paths

derivatives_flow only: `flow_packs[symbol].est_liq_clusters` (each `{level, side, leverage, estimated}`), `.nearest_long_liq_below_pct`, `.cascade_risk`, `.oi_change_24h_pct`, `.oi_funding_label`, `.funding_regime`, `.ls_label`, `.data_confidence`; `thresholds.cascade_high_distance_pct`, `thresholds.cascade_moderate_distance_pct`; `hard_limits.max_stop_pct`.

Precedence: the derivatives_flow prompt governs, in particular step 5 (cascade copy, stop inside cascade), step 6 (block), step 7 (washout) and step 8 (confidence). This skill explains what the cluster fields are and why they matter.

## What code gives you

The bot has no paid heatmap and no forced-order feed. Code projects where leveraged longs opened near the 24h VWAP would be force-closed, for three fixed leverage tiers (25x, 50x, 100x) with a small maintenance margin. That puts the tiers roughly 3.5%, 1.5% and 0.5% below the reference price.

- `nearest_long_liq_below_pct`: distance in % from current price down to the closest projected long tier.
- `cascade_risk` (code): high when the nearest tier is within `thresholds.cascade_high_distance_pct` (2%) and `oi_funding_label` is leveraged_long_buildup; moderate when within 3% or `funding_regime` is extreme_long; otherwise low; `unknown` when it cannot be computed.
- Every cluster carries `estimated: true`. These are statistical zones that drift as positions open and close, never exact prices.

Because the tiers are a fixed distance from the same reference, the spacing between clusters is a property of the formula, not of the market. It carries no information and never escalates `cascade_risk`.

## Why a spot buyer cares

A pool of over-leveraged longs just under price is a stack of forced market sells waiting for a small dip. If the bot's stop (at most `hard_limits.max_stop_pct` = 2%) sits inside that zone, it is likely to be hit exactly when forced selling peaks. So a nearby cluster backed by real leverage (code `cascade_risk` high, which needs leveraged_long_buildup) is a reason to stay out. A tier near price without that label only says price sits near its 24h VWAP: a warning, not a block. The converse does not hold: a cluster that has been flushed may later act as support, but an estimated projection is never evidence for entering.

## Procedure inside the prompt steps

1. `cascade_risk`: copy the pack value. The output enum has no `unknown`, so pack `unknown` becomes `moderate` and you cite `flow_packs[symbol].cascade_risk: unknown`. Never lower it, never raise it.
2. `nearest_long_liq_cluster_pct_below`: the same number as `nearest_long_liq_below_pct`; `null` stays `null`.
3. `stop_inside_cascade`: true only when the pack `cascade_risk` is high (a tier within 2% AND leveraged_long_buildup). Distance alone never sets it: every tier is a fixed fraction of the 24h VWAP, so a tier within 2% mostly means price sits near its VWAP, not that leverage is stacked. Cite such a distance as a warning in `evidence`.
4. `block_new_longs`: true when `stop_inside_cascade` is true (plus the other step 6 conditions). `block_reason` in Korean cites the distance and tier, e.g. "50x 추정 청산 군집이 1.4% 아래, 손절이 강제청산 구간 안".
5. Washout: clusters never make one. The step 7 conjunction rests on funding and OI; the cluster part only requires that no tier is in stop range (`nearest_long_liq_below_pct` null or ≥ 3.0) and `cascade_risk` is not high.
6. Confidence: cap 0.6 when `data_confidence` is low; cap 0.3 when `est_liq_clusters` is empty and the pack `cascade_risk` is unknown; clusters never add confidence.
7. `market_wide_note` may mention a one-sided magnet when projected clusters exist only below price. It is a warning, not a bias.

## Flush done or flush in progress

Read open interest through its code label and the funding regime, not through raw cut-offs of your own.

| `oi_funding_label` | `funding_regime` | state | consequence (prompt step) |
|---|---|---|---|
| leveraged_long_buildup | any positive band | leverage still building | `crowding_risk` high, block (3, 6) |
| deleveraging | elevated_long or extreme_long | flush unfinished; survivors still long and paying | block as unfinished flush, `flow_bias` avoid (6, 8) |
| deleveraging | negative or squeeze | flush done; forced sellers gone | washout candidate if every step 7 condition holds |
| neutral | neutral | quiet | no cluster-based adjustment |

An unfinished flush leaves `squeeze_direction` to step 4; do not set it from the clusters.

## Evidence

Format `<field path>: <value> — <짧은 한국어 해석>`, e.g. `flow_packs[symbol].nearest_long_liq_below_pct: 1.4 — 손절 범위 안 추정 청산 군집`, `flow_packs[symbol].cascade_risk: high — 연쇄청산 위험`. Cite distances and labels, never raw cluster prices.

## What not to do

- Do not invent liquidation volumes or 24h liquidation totals; no such field exists.
- Do not quote cluster levels as support or resistance for anyone.
- Do not use cluster spacing, density or count to change `cascade_risk`.
- Do not set `washout_setup` on a negative funding print alone or an OI drop alone.
- Do not suggest widening the stop past 2% to clear a cluster; the answer is no entry.
- Do not express a short view; a high `cascade_risk` yields `avoid` and `block_new_longs`, nothing more.
