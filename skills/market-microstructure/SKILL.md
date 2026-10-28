---
name: market-microstructure
description: 코드가 계산한 스프레드·호가 깊이·주문 불균형(OIR)·충격·실행비용(exec_cost_pct)을 읽어, 리스크 관리자는 시장가 주문의 체결 가능성·사이즈·거부를, 파생 분석가는 보조 증거만 판단하는 법 (수치 규칙은 역할 프롬프트를 따름)
roles: [derivatives_flow, risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/market-microstructure/SKILL.md (MIT)"
license: MIT
---
# Microstructure: can a market order get in and out cleanly?

## Paths per role

- derivatives_flow: `flow_packs[symbol].oir_1pct`, `flow_packs[symbol].spread_bps`, next to `.funding_regime`, `.cascade_risk`, `.nearest_long_liq_below_pct`.
- risk_manager: `risk_packs[symbol].spread_bps`, `.depth_taker_usdt`, `.impact_bps_est`, `.order_notional_vs_15m_volume_share`, `.exec_cost_pct`; `candidates[].planned_notional_usdt`; `candidates[].technical.stop_pct` / `.tp_pct`. The risk payload has no order-book imbalance.

Precedence: your role prompt governs. risk_manager steps 5 (liquidity) and 6 (stop, target, R:R, execution cost) and derivatives_flow steps 3, 5 and 7 are the rules; this skill explains the fields and how they interact. It adds no separate veto table.

The bot sends small spot market orders and nothing else, executed at the next bar open; minutes around funding settlement are already blocked in code. Its only levers are size and the right to refuse. Every number below is computed in code from an order-book snapshot and the 24h ticker; read it, never estimate it from price.

## What the fields mean

- `spread_bps`: (ask − bid) / mid in basis points. The scanner rejects symbols above 10 bps, so what reaches you passed that filter at scan time.
- `depth_taker_usdt`: quote value resting on the ask side within 1% of mid, i.e. what a buy market order can eat before moving price 1%.
- `impact_bps_est`: planned notional / `depth_taker_usdt` × 100, a rough walk-the-book cost for this order.
- `order_notional_vs_15m_volume_share`: planned notional as a share of one average 15m bar of volume.
- `exec_cost_pct` (risk only): impact_bps_est/100 + spread_bps/200 + 0.20, i.e. impact plus half the spread plus round-trip fees, in %. It is the one execution-cost number to use; do not build your own from bps.
- `oir_1pct` (flow only): (bid depth − ask depth) / (bid depth + ask depth) within 1%, from −1 to +1.

These are the only liquidity fields. There is no trade tape, no spread history and no queue data to cite.

## Risk manager use

1. Liquidity (step 5): veto when `risk_packs[symbol].spread_bps` > 10 or `depth_taker_usdt` < 5 × `planned_notional_usdt`. When `order_notional_vs_15m_volume_share` > 0.05, shrink `max_size_pct` in proportion; veto if the result falls below 2.
2. Execution cost (step 6): veto when `risk_packs[symbol].exec_cost_pct` > 0.25 × the final `stop_pct`. Check again after any stop tightening, because tightening makes this test stricter. Gate G3 recomputes it.
3. The stop is never widened beyond `candidates[].technical.stop_pct` to make cost fit. Net R:R = (`take_profit_pct` − 0.30) / (`stop_pct` + 0.30); below 1.5 tighten the stop only, and if that is impossible or breaks the cost test, veto.
4. `null` in `exec_cost_pct` (or a missing risk pack) is a hard veto under step 4; an unknown cost is never approved.
5. High 24h volume is not depth; the two fields are separate for a reason.
6. Open positions: a liquidity-related assumption is `weakening` when `risk_packs[symbol].spread_bps` now exceeds 10 or `exec_cost_pct` has become large relative to the remaining stop distance. Exiting into a thin book is itself a cost; the action still follows the prompt's health and red-line rules.
7. Format every veto reason or condition as `<field path>: <value> — <짧은 한국어 해석>`, e.g. `risk_packs[symbol].exec_cost_pct: 0.41 — 실행비용이 손절폭의 25% 초과`, and summarize a liquidity veto in `risk_note` so the Telegram message explains why nothing traded.

## Derivatives_flow use

Order-book imbalance and spread are supporting evidence only. Resting size can be pulled instantly and imbalance predicts minutes, not hours, so these fields never decide `flow_bias`, never change `cascade_risk` and never set `stop_inside_cascade` (that comes only from the code `cascade_risk`, step 5).

- `oir_1pct` > +0.3 together with `funding_regime` negative or squeeze strengthens a washout read that step 7 has already established; cite both fields.
- A wide `spread_bps` together with `oir_1pct` < −0.3 is the signature of a liquidity vacuum: per step 3 `crowding_risk` cannot be `low`. If a projected long cluster is also near, mention the thin book in `evidence` or `block_reason` as a warning.
- A lone imbalance reading with everything else neutral leaves `posture` neutral and `flow_bias` neutral; note that only the book, not positioning, moved.
- Evidence example: `flow_packs[symbol].oir_1pct: 0.35 — 매수 호가 우위, 워시아웃 보조 근거`.

## What not to do

- Do not recompute impact or cost from `last_closes_1h` or the ticker; use the given fields.
- Do not give order-type, scheduling or slicing advice; execution is fixed by code.
- Do not express microstructure as "price will go up or down". It answers whether the trade can be executed, not whether it should be.
- Do not widen a stop or the size to absorb a cost; shrink or veto.
