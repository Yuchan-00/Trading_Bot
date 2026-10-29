---
name: execution-model
description: 시장가 주문의 체결 비용을 코드 산출 exec_cost_pct(충격+스프레드 절반+왕복 수수료)로 읽고, 손절 대비 비용 25% 거부·왕복 마찰 0.30% 순손익비·다음 봉 시가 체결 지연을 판단하는 법 — 손절은 넓히지 않고 좁히기만 함 (수치 규칙은 역할 프롬프트를 따름)
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/execution-model/SKILL.md (MIT)"
license: MIT
---
# Execution cost for small spot market orders

## Paths

risk_manager only: `risk_packs[symbol].exec_cost_pct`, `.impact_bps_est`, `.spread_bps`, `.order_notional_vs_15m_volume_share`, `.daily_vol_pct`, `.hv_pct_rank`; `candidates[].planned_notional_usdt`; `candidates[].technical.stop_pct`, `.tp_pct`, `.timing`, `.evidence`; `candidates[].bull.expected_horizon_bars`; `account_pack.bias_severities.overtrading`, `account_pack.trades_today`, `account_pack.cooldowns`.

Precedence: your role prompt governs. Step 5 (liquidity) and step 6 (stop, target, R:R, execution cost) are the rules; liquidity vetoes are explained in the market-microstructure skill. This skill explains what a fill costs and what that does to the reward-to-risk arithmetic. It adds no cost model of its own.

## How the bot trades

One market buy at the next 15-minute bar open, and one market sell at the stop, the target, the time stop or a decision. No limit orders, no slicing, no choice of session. Minutes around funding settlement are already blocked in code. The only levers are size and the right to refuse.

## The cost number

`exec_cost_pct` = `impact_bps_est`/100 + `spread_bps`/200 + 0.20, computed in code: estimated book impact for this order, plus half the spread, plus round-trip taker fees, in %. It is the one execution-cost figure to use. Do not build your own from liquidity tiers, bps or memory of past fills. A `null` `exec_cost_pct` is a hard veto (prompt step 4): an unknown cost is never approved.

The net R:R uses a separate fixed friction: 0.30% for round-trip fees plus slippage. Both numbers apply; neither replaces the other.

## Step 1: cost against the stop

Veto when `exec_cost_pct` > 0.25 × the final `stop_pct`. A wide stop tolerates more cost than a tight one, so check again after any stop tightening; tightening makes this test stricter. Code gate G3 recomputes it, so leniency gains nothing.

Never widen the stop to make cost fit. The stop can only stay at the technical candidate (capped at 2) or move tighter.

## Step 2: net reward-to-risk

- `stop_pct` = min(`technical.stop_pct`, 2); `take_profit_pct` = min(`technical.tp_pct`, 4).
- Net R:R = (`take_profit_pct` − 0.30) / (`stop_pct` + 0.30). At 1.5 or above it passes.
- Below 1.5, tighten the stop only, as far as the prompt's formula allows. If the tightened stop is too small or volatility too high (the prompt's limits), or the cost test of Step 1 then fails, veto.
- Never repair the ratio by raising the target. The technical target already follows tp_pct = min(4.0, max(2 × stop_pct, 1.5 × stop_pct + 0.75)), which is built to clear 1.5 after friction.

## Step 3: order size versus the venue

- `order_notional_vs_15m_volume_share` above 0.05: the order is a visible share of a normal 15-minute bar. The prompt shrinks `max_size_pct` in proportion and vetoes below 2.
- A large `impact_bps_est` already shows up inside `exec_cost_pct`; do not count it twice with an extra rule.
- High 24h volume is not depth; read the pack, never the ticker.

## Step 4: the one-bar delay

The decision is made on a closed bar and the fill comes at the next open, so entry lags the signal by one bar. When `technical.evidence` cites elevated `feature_pack.volume_vs_hour_avg` or `feature_pack.abnormal_volume_multiple`, the open may gap and the real stop distance from the fill may differ from the plan. The code re-anchors the stop to the fill; add a `required_monitoring` item noting this. Timing itself belongs to the technical channel (`technical.timing`); do not argue "now" or "later" from cost.

## Step 5: target versus horizon

The move needed is the target plus friction. If `bull.expected_horizon_bars` (15-minute bars) is short and that move is large relative to `risk_packs[symbol].daily_vol_pct`, the target asks more than a typical day delivers. Do not cut the target yourself; say so in `conditions` or `required_monitoring` and let the prompt's verdict rules decide.

## Step 6: turnover drag

Every round trip costs at least the fixed 0.30% of notional before any edge. Frequent trading compounds it. When `account_pack.bias_severities.overtrading` is elevated, or a symbol just left `account_pack.cooldowns`, name friction in `portfolio_note`. The floor changes only by the prompt's step 2 (0.75 when overtrading is severe).

## Open positions

You see no holding's notional, so do not estimate exit cost per position. A sharply worse `spread_bps` or a large `exec_cost_pct` for a held symbol is evidence that a liquidity assumption is `weakening`; the action still follows the prompt's health and red-line rules. After a red line, cost is irrelevant and the answer is the prompt's `exit`.

## What not to do

- Do not recommend VWAP, TWAP, limit, iceberg orders or "wait for a quieter session"; none exist here.
- Do not estimate slippage or impact from price action or 24h volume; use the pack and treat `null` as unknown.
- Do not lower the 0.30% friction or the 25%-of-stop rule; you may only tighten.
- Low cost permits a trade; it never recommends one.

## Output

- `stop_pct` (≤ 2, never wider than the technical stop) and `take_profit_pct` (≤ 4): from Step 2.
- `rr_net_estimate`: the Step 2 ratio; `null` only on a veto.
- `max_size_pct`: after the prompt's size rules and the Step 3 shrink.
- `veto_reasons`, `conditions`, `required_monitoring` (Korean): `"<field path>: <value> — <짧은 한국어 해석>"`, e.g. `"risk_packs[symbol].exec_cost_pct: 0.41 — 손절폭 1.2%의 25% 초과, 비용이 우위를 잠식"`.
- `portfolio_note` (Korean): turnover drag from Step 6. Enum values exactly as the schema spells them.
