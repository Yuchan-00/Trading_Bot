---
name: minute-analysis
description: 세션 VWAP 거리·직전 시간 대비 거래량·비정상 거래량 배수·cgo_60 같은 장중 라벨로 추격 매수를 걸러내고 진입 타이밍을 조정하는 법 (확신도·손절·노이즈 판정은 역할 프롬프트 규칙을 따름)
roles: [technical]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/minute-analysis/SKILL.md (MIT)"
license: MIT
---
# Intraday context: VWAP, volume of the hour, holder cost

## Purpose

The bot acts on 15-minute cycles, so intraday context decides when a setup found by the regime and structure reads may be acted on. It is mostly a brake: it catches chasing and unconfirmed breakouts. All values are code-computed in the feature pack:

- `feature_pack.vwap_distance_pct`: price versus the session VWAP built from 15m bars (UTC-day anchored), in %.
- `feature_pack.cgo_60`: price versus the 60-bar 1h VWAP, in %; a proxy for the average recent buyer's unrealized gain.
- `feature_pack.volume_vs_hour_avg`: last 15m volume over the mean of the previous 4 bars.
- `feature_pack.abnormal_volume_multiple`: last 15m volume over the median of the previous 96 bars.
- `feature_pack.per_tf.<tf>.volume_ratio_20`: volume versus its 20-bar mean on each timeframe.
- `feature_pack.change_24h_pct`, `feature_pack.last_closes_1h`: coarse context only.

Precedence: your role prompt (technical.md) governs. Its step 6 holds the chasing, breakout-volume and `cgo_60` rules with their confidence deductions; step 5 holds the noise test; step 8 the confidence recipe; step 10 the exits. This skill explains how to read the fields inside those steps and adds no confidence caps or score points of its own.

## Reading order

1. **Chasing (prompt step 6).** Any of `feature_pack.abnormal_volume_multiple` above 3, `feature_pack.change_24h_pct` above 15, or `feature_pack.vwap_distance_pct` above 1.5 marks the entry as a chase: `wait_pullback` and −0.1 confidence. A volume surge after a large daily move is late participation, not confirmation; it never supports `now`.
2. **Near fair value.** `feature_pack.vwap_distance_pct` roughly between −0.5 and +1.0 is a normal place to enter; it permits `now` when the prompt's step 3 trigger holds, and adds nothing beyond that.
3. **Below the session average.** `feature_pack.vwap_distance_pct` below −1.5 fits only a `mean_reversion` setup (1h vote mean_reversion_long). With a trend vote it describes a weak day: prefer `flat` or `wait_confirmation`.
4. **Breakout volume (prompt step 6).** `feature_pack.volume_vs_hour_avg` below 0.5 on a breakout → `wait_confirmation`. A breakout or continuation entry is better supported when `volume_vs_hour_avg` or `feature_pack.per_tf.15m.volume_ratio_20` is at least 1.2; without either, prefer `wait_confirmation` over `now`.
5. **Holder cost, `cgo_60` (prompt step 6).**
   - `cgo_60` at or above +3: overhang / greed side. Recent buyers sit on gains and profit-taking pressure is likely; −0.05 confidence and no `now` for `trend_continuation` or `breakout`.
   - `cgo_60` at or below −3: many underwater holders; capitulation may be near and a rebound is possible. It supports a `mean_reversion` read only when the 4h regime of step 1 holds, and is never an entry reason by itself.
   - Between −3 and +3: no adjustment.
6. **Stop fragility.** VWAP is never a stop reference (`sl_reference` allows only zone, kijun, atr, none). If `feature_pack.vwap_distance_pct` is positive and larger than the chosen `stop_pct`, price can reach the stop before even revisiting the session average. Mention this in `invalidation` and prefer `wait_pullback`, but do not change `noise_check`: it is exactly `feature_pack.noise_ok_2pct` true AND `stop_pct` at or above `feature_pack.noise_floor_stop_pct`, and gate G1 recomputes it from those fields.

## Open positions

The `position` block carries `entry_price`, `stop_loss_price`, `take_profit_price`, `last_mark`, `unrealized_pct`, `bars_held` and `opened_ts`; it has no thesis and no confidence. Intraday fields never set `exit_flag` on their own; only the prompt's step 10 conditions do. Use them as context:

- `feature_pack.vwap_distance_pct` below −1.0 with `feature_pack.volume_vs_hour_avg` at or above 1.5 is selling pressure: name it in `invalidation`, and in `exit_reason` when a step 10 condition also fires.
- `feature_pack.abnormal_volume_multiple` at or above 4 while `position.unrealized_pct` is falling suggests distribution; treat it the same way.

## What not to do

- Do not rebuild VWAP or a volume profile from `feature_pack.last_closes_1h`; those closes carry no volume.
- Do not use `feature_pack.change_24h_pct` as momentum; a large positive value is a chase warning, not a buy reason.
- Do not translate below-VWAP weakness into a short. It is `flat`, `avoid`, a `wait_*` timing, or a holding's invalidation note.
- Do not ask for more bars or tick data; the labels are all that exists.
- Do not read null intraday fields as neutral confirmation. If `vwap_distance_pct` or the volume fields are null, say so and avoid `now`.

## Evidence

Each item is `<field path>: <value> — <짧은 한국어 해석>`, for example `feature_pack.vwap_distance_pct: 1.8 — 세션 VWAP 대비 과열, 추격 매수` or `feature_pack.cgo_60: -3.6 — 손실 보유자 다수, 반등 여지`. Intraday items rarely deserve more than one of the three slots. `invalidation` examples: a 15m close back below the session VWAP, or `feature_pack.volume_vs_hour_avg` staying below 1.0 after a breakout. Free text is Korean; enum values stay exactly as the schema spells them.
