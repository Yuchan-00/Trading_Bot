---
name: market-microstructure
description: 코드가 계산한 스프레드·호가 깊이·주문 불균형(OIR)·충격 추정치를 읽어 시장가 주문이 안전한지, 사이즈를 줄일지, 거부할지 판단하는 법
roles: [derivatives_flow, risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/market-microstructure/SKILL.md (MIT)"
license: MIT
---
# Microstructure — can a market order get in and out cleanly?

The bot sends small spot market orders and nothing else. The only levers are the size and the right to refuse. Every microstructure number is computed in code from a public order-book snapshot and 24h ticker; you read it, you never estimate it from price.

## Definitions (interpretation only)

- `spread_bps` = (ask − bid) / mid × 10 000. The scanner already rejects symbols above 10 bps; what you see passed that gate.
- `spread_vs_24h_median` = current spread / its 24h median. The ratio, not the level, is the stress signal.
- `oir_1pct` = (bid depth − ask depth) / (bid depth + ask depth) within 1% of mid; range −1..+1.
- `depth_taker_usdt` = quote volume resting on the ask side within 1%: what a buy market order eats.
- `impact_bps_est` = planned notional / `depth_taker_usdt` × 100: a rough walk-the-book cost for the planned order.
- `order_notional_vs_15m_volume_share` = planned notional / (24h quote volume / 96): participation in one bar.

These are the only liquidity fields; nothing else exists to cite.

## risk_manager procedure (per candidate)

Walk in this order; the first veto ends the walk.

1. Spread stress: `risk_pack.spread_vs_24h_median` > 3 → `verdict: "veto"`, `veto_reasons` cites the field. Between 2 and 3 → at most `conditional` with a `conditions` entry requiring the ratio to fall under 2 before entry.
2. Depth versus size: `risk_pack.depth_taker_usdt` < 5 × planned notional → `veto`. Between 5× and 10× → cut `max_size_pct` so the notional is ≤ depth / 10.
3. Impact: `risk_pack.impact_bps_est` > 20 → `veto`; 10..20 → halve `max_size_pct`; ≤ 10 → no change.
4. Participation: `risk_pack.order_notional_vs_15m_volume_share` > 0.05 → shrink `max_size_pct` proportionally so the share is ≤ 0.05; if that leaves less than 1% of equity, `veto`.
5. Cost against the stop: round-trip cost in bps = 20 (two fees) + 2 × `impact_bps_est`. It must be ≤ 25% of the stop distance, i.e. ≤ 0.25 × `stop_pct` × 100. A 2% stop tolerates 50 bps, so `impact_bps_est` above 15 fails here; do not fix this by widening `stop_pct` beyond 2.0 — tighten size or veto.
6. Net reward-to-risk: `rr_net_estimate` = (`take_profit_pct` − c) / (`stop_pct` + c) where c = max(0.30, round-trip cost in %). Below 1.5 → `conditional` at best, and only if a tighter stop that still satisfies `risk_pack.stop_vs_atr_noise_ok` reaches 1.5; otherwise `veto`.
7. Thin-book exit risk: if `risk_pack.spread_bps` > 6 and `flow_pack.oir_1pct` < −0.3, note in `required_monitoring` that the stop may fill worse than 2%, and keep `max_size_pct` ≤ 5.

Record the binding rule by field name in `veto_reasons` (veto) or `conditions` (conditional), and summarise any veto in `risk_note` so the Telegram message explains why nothing traded.

For open positions, a `spread_vs_24h_median` above 3 or `depth_taker_usdt` collapsing below 5× the position notional counts as `weakening` for the liquidity assumption; prefer `trim` over `exit` unless a red line also fired, because an exit into a vacuum is itself the cost you are avoiding.

## derivatives_flow procedure

`oir_1pct` and `spread_bps` are supporting evidence only. Order-book imbalance predicts the next few minutes with roughly 55–60% accuracy and resting size can be pulled instantly, so it never decides `flow_bias` by itself.

- `oir_1pct` > +0.3 with `funding_regime` in {`negative`, `squeeze`} strengthens a `washout_setup` reading; cite both fields in `evidence`.
- `oir_1pct` < −0.3 with `cascade_risk` at least `moderate` and `nearest_long_liq_below_pct` ≤ 3: the book is thin exactly where forced selling would land. You may raise `cascade_risk` one notch (never lower it) and set `stop_inside_cascade: true` when the cluster sits inside 2%.
- `spread_bps` widening together with a negative `oir_1pct` is the signature of a liquidity vacuum: `crowding_risk` cannot be `low`, and `confidence` on any `flow_bias: "long"` is capped at 0.6.
- A single OIR reading with everything else neutral → `posture: "neutral"`, `flow_bias: "neutral"`, and a note that the book, not positioning, is the only signal.

## What not to do

- Use only the fields above; no trade-tape statistics and no execution scheduling or order-type advice. Execution is a market order at the next bar open; funding-settlement minutes are already blocked in code.
- Do not recompute impact from `last_closes_1h` or from the ticker; use `impact_bps_est` as given and say "unknown" when it is `None` (an unknown impact is a reason to shrink, not to approve).
- Do not treat high 24h volume as proof of depth; the two fields are separate for a reason.
- Do not express any of this as "price will go up/down". Microstructure answers whether the trade can be executed, not whether it should be.

## Output mapping summary

| Observation | risk_manager field | derivatives_flow field |
|---|---|---|
| spread ratio > 3 | `verdict: "veto"`, `veto_reasons` | — |
| depth < 5× notional | `verdict: "veto"` | — |
| impact 10..20 bps | `max_size_pct` halved, `conditions` | — |
| OIR < −0.3 near cluster | `required_monitoring` | `cascade_risk` up, `stop_inside_cascade` |
| OIR > +0.3 after funding reset | — | supports `washout_setup`, `evidence` |
| any value `None` | shrink, state unknown in `risk_note` | `confidence` ≤ 0.5 |
