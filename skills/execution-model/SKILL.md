---
name: execution-model
description: 시장가 주문의 체결 비용 모델 — 유동성 등급별 보수적 슬리피지 가정, 코드 산출 임팩트, 다음 봉 시가 체결 가정, 왕복 마찰 0.30%를 순손익비·손절 거리·회전율 부담에 반영하는 절차
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/execution-model/SKILL.md (MIT)"
license: MIT
---
# Execution cost model for small spot market orders

## When to apply

Apply to every candidate before filling `stop_pct`, `take_profit_pct` and `rr_net_estimate`, and to every open position when judging whether an `exit` costs more than it saves. The bot sends one market buy at the next bar open and one market sell at the stop, target, time stop or decision; no limit orders, no slicing. Price that round trip and refuse trades whose edge it eats. Liquidity vetoes (spread stress, depth, participation) belong to the microstructure skill; this skill covers what the fill costs and what that does to the reward-to-risk arithmetic.

Inputs: `risk_pack.impact_bps_est`, `risk_pack.spread_bps`, `risk_pack.order_notional_vs_15m_volume_share`, `risk_pack.depth_taker_usdt`, `risk_pack.daily_vol_pct`, `risk_pack.stop_vs_atr_noise_ok`, the technical `stop_pct` / `tp_pct` proposals, `technical.timing`, the volume items in `technical.evidence` (`volume_vs_hour_avg`, `abnormal_volume_multiple`), and `bull.expected_horizon_bars`.

## Step 1: price the fill

Expected one-way slippage is the larger of two estimates:

1. **Liquidity tier.** The paper simulator fills at last price ± a flat 5 bps; assume more, by `risk_pack.depth_taker_usdt`: above 2,000,000 USDT 5 bps, 500,000 to 2,000,000 15 bps, below that 30 bps. This is your conservative assumption, not the simulator's rule.
2. **Impact.** Use `impact_bps_est` as given. `order_notional_vs_15m_volume_share` ≥ 0.05 is already cut or blocked by the code gate; if such a candidate still reaches you, `veto` naming the field. Above 20 bps of impact the order is too large for the venue regardless of the chart: shrink `max_size_pct` until it is under 20 or `veto`.

Add half of `spread_bps` (a market order crosses half the spread). Call the total `slip_bps`.

## Step 2: the round-trip friction

friction (%) = 2 × 0.10 (taker fee each way) + 2 × `slip_bps` / 100, floored at 0.30. The 0.30% floor is what the code assumes in gate G4; raise it for a thin book, never lower it. State the number used in `conditions` for the journal.

## Step 3: friction against the stop and the target

- The stop must not be an execution artefact: friction ≤ 25% of `stop_pct` (a 2% stop tolerates 0.50%; a 1.2% stop only the 0.30% floor). If the stop is tighter than 4 × friction, widen it toward 2.0 only while `stop_vs_atr_noise_ok` is true, otherwise `veto`; never widen the target instead.
- Net reward-to-risk: `rr_net_estimate` = (`take_profit_pct` − friction) / (`stop_pct` + friction). Below 1.5 at best `conditional`; below 1.2 `veto`. At 0.30% friction the ratio binds through the target, not the 2% cap: a 4.0 target clears 1.5 with any permitted stop, a 3.0 target needs a stop ≤ 1.5, a 2.5 target ≤ 1.15. Tighten the stop to meet the ratio, or decline.
- Check the target against the horizon: the move needed is `take_profit_pct` + friction. When `bull.expected_horizon_bars` × 0.25 is under 24 hours and that move exceeds `risk_pack.daily_vol_pct`, the target asks more than a typical day delivers: cut `take_profit_pct` to `daily_vol_pct` (≤ 4.0), recompute the ratio, and let it decide.

## Step 4: fill timing and the stale-signal risk

The decision is made on a closed 15-minute bar and the fill happens at the next open, so entry lags the signal by one bar. If `technical.evidence` carries `volume_vs_hour_avg` > 2 or `abnormal_volume_multiple` > 3, the open is likely to gap and the stop distance from the actual fill may be shorter than planned: add a `required_monitoring` item noting the stop is re-anchored to the fill (the code does this) and keep `max_size_pct` one notch lower. Never use this to argue about "now" or "later"; timing belongs to the technical channel.

## Step 5: turnover drag over the book

Each round trip costs at least 0.30% of notional: at 10% size that is 0.03% of equity per trade, 0.24% for eight trades a day, before any edge. When `account_pack.bias_severities.overtrading` is `mild` or `severe`, or a symbol in `cooldowns` would be re-entered the same day, raise `global.confidence_floor` to 0.75 and name friction as the reason in `portfolio_note`.

## Open positions

An `exit` into a thin book is itself a cost: if `spread_bps` has doubled or `depth_taker_usdt` is below 5 × position notional, prefer `trim` and say so in `reason`; after a red line, cost is irrelevant and the answer is `exit`.

## What not to do

- Do not recommend VWAP, TWAP, limit or iceberg orders, or "wait for a quieter session"; none exist here and funding-settlement minutes are already blocked by code.
- Do not estimate slippage or impact from price action, 24h volume or memory of past fills; use the pack fields and treat `null` as unknown, which shrinks size.
- Do not lower the 0.30% floor, the 10 bps fee or the 25%-of-stop rule; only tighten them.
- Do not fix a failing ratio by raising `take_profit_pct` above what the horizon supports or above 4.0.
- Low cost permits a trade; it never recommends one.

## Output mapping

- `candidates[].stop_pct` (≤ 2.0), `take_profit_pct` (≤ 4.0): Step 3 values, never looser than the technical proposal.
- `candidates[].rr_net_estimate`: the Step 3 ratio; `null` only when both `impact_bps_est` and `spread_bps` are missing.
- `candidates[].max_size_pct`: after the Step 1 impact shrink and the Step 4 notch.
- `candidates[].verdict`: `conditional` when friction forced any change, `veto` when the ratio is under 1.2 or impact stays above 20 bps.
- `conditions`, `veto_reasons`, `required_monitoring` (Korean): cite `risk_pack.impact_bps_est`, `risk_pack.spread_bps` and the friction used.
- `global.confidence_floor`, `portfolio_note` (Korean): turnover drag from Step 5.
- `positions[].action` / `reason` (Korean): the trim-versus-exit judgement.
