---
name: volatility
description: 실현변동성 백분위(hv_pct_rank)와 ATR%를 방향이 아닌 레짐 정보로 읽어, 기술분석은 타이밍·노이즈 점검에, 리스크 관리자는 사이즈 축소·모니터링에 쓰는 법 (수치 규칙은 역할 프롬프트를 따름)
roles: [technical, risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/volatility/SKILL.md (MIT)"
license: MIT
---
# Volatility as regime, never as direction

## Paths per role

- technical: `feature_pack.hv_pct_rank`, `feature_pack.atr_pct_1h`, `feature_pack.noise_ok_2pct`, `feature_pack.noise_floor_stop_pct`, `feature_pack.per_tf.<tf>.atr_pct`.
- risk_manager: `risk_packs[symbol].hv_pct_rank`, `.rv_20_pct`, `.rv_60_pct`, `.daily_vol_pct`, `.kurtosis`, `.inverse_vol_size_pct`, `.cvar_size_pct`, `.stop_vs_atr_noise_ok`; the technical stop as `candidates[].technical.stop_pct` / `.noise_check`; `regime.fused`.

Precedence: your role prompt governs every number (technical steps 5 and 9; risk_manager steps 4, 6 and 7). This skill explains what the volatility fields mean and how to use them inside those steps; it adds no separate size table, confidence deduction or score vote.

## What the numbers are

All of them are computed in code; read them, never rebuild them from `last_closes_1h` (twenty closes are context, not a sample).

- `hv_pct_rank` (0..100): where the current 24-bar realized volatility of 1h log returns sits among at least 100 earlier windows. `null` means the history is too short: the regime is unknown, so give neither credit nor relief.
- `atr_pct_1h`: 1h ATR(14) as % of price, the size of an ordinary hourly swing.
- `noise_floor_stop_pct` (= `atr_pct_1h`): the smallest stop distance that is not inside routine noise.
- `noise_ok_2pct`: code test 1.5 × `atr_pct_1h` < 2.0, i.e. whether the 2% hard stop clears normal swings at all.
- `rv_20_pct` / `rv_60_pct`: annualized realized volatility over short and longer windows; `daily_vol_pct`: the 1-day equivalent used by inverse-vol sizing.
- `stop_vs_atr_noise_ok`: the risk-side copy of the same noise test.

## Core idea

Volatility clusters and then reverts: a low percentile makes a larger move more likely soon, a high one makes calmer swings more likely later. Neither tells you direction. Low volatility can make a breakout read more credible; it never creates a long by itself. High volatility is a reason to wait, shrink or skip; on a spot-only bot it is never a reason to "go short".

## Reading the percentile

| `hv_pct_rank` | regime | what it means for you |
|---|---|---|
| below 20 | compressed | expansion likely; a breakout setup that the prompt already identified gains plausibility, and the first expansion bar can be violent |
| 20 to 70 | normal | no volatility adjustment |
| above 70 | active | risk_manager applies the × 0.75 size factor of its step 7 |
| above 80 | stressed | technical: no `now` entry and the confidence cap of step 5; risk_manager: a stop that must be tightened for R:R cannot be, so veto per step 6 |
| `null` | unknown | no credit, no relief; say so if it matters |

## Technical role

1. Noise check is a code fact. `noise_check` = `feature_pack.noise_ok_2pct` is true AND your `stop_pct` ≥ `feature_pack.noise_floor_stop_pct`. Either field `null`, or no stop, makes it false. When false: `confidence` ≤ 0.50, `timing` wait_pullback, and say in `invalidation` that the stop sits inside ordinary hourly swings. Gate G1 recomputes the same test, so never report a pass the fields do not show.
2. Never solve a noise problem by widening the stop past 2.0 or the target past 4.0. A stop that cannot be both ≥ the noise floor and ≤ 2 means no long now.
3. Stop and target come only from step 9 of the prompt: ATR fallback `stop_pct` = min(1.5 × `atr_pct_1h`, 2.0), `tp_pct` = min(4.0, max(2 × `stop_pct`, 1.5 × `stop_pct` + 0.75)), net R:R = (`tp_pct` − 0.30) / (`stop_pct` + 0.30) ≥ 1.5. If the ATR stop was truncated at 2.0, that is worth naming in `invalidation`.
4. Open position: a high `hv_pct_rank` alone never sets `exit_flag`; the exit conditions of step 10 are structural. You see no thesis and no confidence for the position, only the feature pack and the position numbers.
5. Evidence example: `feature_pack.hv_pct_rank: 17 — 변동성 압축, 확장 임박 가능`, `feature_pack.noise_ok_2pct: false — 2% 손절이 1시간 정상 변동 안에 위치`.

## Risk manager role

1. Sizing follows step 7 only: `max_size_pct` = min(10, `inverse_vol_size_pct`, `cvar_size_pct`), then its halving and × 0.75 factors (`hv_pct_rank` > 70 or `kurtosis` > 6). Inverse-vol and CVaR sizes already shrink as volatility rises; do not stack an extra percentile table on top.
2. `risk_packs[symbol].stop_vs_atr_noise_ok` or `candidates[].technical.noise_check` not true is a hard veto (step 4). Do not rescue it with a different stop.
3. Never widen the stop beyond `candidates[].technical.stop_pct`. If net R:R < 1.5, tighten the stop only; with `hv_pct_rank` > 80 a tighter stop lands inside noise, so veto and name the field.
4. Expanding volatility: when `rv_20_pct` is clearly above `rv_60_pct` (for example 1.3×), swings are widening right now. Add a Korean `required_monitoring` entry such as "risk_packs[symbol].rv_20_pct: 상승 멈춤 확인". This informs monitoring; the verdict still follows step 9.
5. Open positions: when a stored assumption depends on calm conditions, re-assess it now against `risk_packs[symbol].hv_pct_rank`; a stressed reading by itself is `weakening`, not `broken`. `prev_assumption_status` is only your earlier view, not a code check. Action follows the prompt's health rules.
6. Every reason uses the format `<field path>: <value> — <짧은 한국어 해석>`, e.g. `risk_packs[symbol].hv_pct_rank: 86 — 변동성 과열, 손절 축소 불가`.

## What not to do

- Do not read a low percentile as bullish or a high one as bearish.
- Do not compute percentiles, standard deviations or ATR yourself.
- Do not give any volatility credit when `hv_pct_rank` is `null`.
- Do not widen a stop or target because volatility is high; wait, shrink or skip.
- Do not emit a short view; bearish outcomes are `flat`, `avoid`, `wait_pullback`, `veto`, `trim` and `exit`.
