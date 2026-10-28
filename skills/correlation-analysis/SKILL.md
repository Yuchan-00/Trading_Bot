---
name: correlation-analysis
description: 코드가 계산한 롤링 상관(corr_to_btc_1h·beta_btc·corr_to_open_positions)과 기준코인 레짐으로 "세 포지션이 사실상 하나인가"를 판정해 중복 포지션 차단·사이즈 축소·상관 메모를 쓰는 절차
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/correlation-analysis/SKILL.md (MIT)"
license: MIT
---
# Rolling correlation: is the third position a new bet?

## When to apply

Apply whenever a candidate is evaluated while `account_pack.position_count` ≥ 1, and on every cycle with two or more holdings. Pair-trading, cointegration and hedge-ratio machinery needs a short leg; none exists here. What survives is the snapshot correlation structure the code computes from 1h returns per symbol:

- `risk_pack.corr_to_btc_1h`: Pearson correlation of the symbol's hourly returns with the reference coin over the recent window
- `risk_pack.beta_btc`: slope of the symbol's returns on the reference coin's returns (how much it amplifies a reference move)
- `risk_pack.corr_to_open_positions`: map of holding symbol → correlation with this candidate
- `regime_pack.avg_pairwise_corr_1h`, `regime_pack.btc_regime_4h`: book-wide context (see the correlation-regime skill for the fused/alarm logic)

You never compute a correlation; if a field is `null`, the relationship is unknown and is treated as high for sizing purposes. This text writes `risk_pack.*`; cite the payload key `risk_packs[<symbol>].*` (and `regime.*` for the book-wide fields).

## Screening thresholds

| `corr_to_open_positions` value (any holding) | Reading | Action |
|---|---|---|
| > 0.8 | same trade in a different ticker | `veto`; the code gate blocks this too, so your veto reason just documents it |
| 0.6–0.8 | strong co-movement | `conditional`: halve `max_size_pct` (≤ 5); holding sizes are not in the payload, never subtract a guessed one |
| 0.3–0.6 | partial overlap | approve on other grounds; mention in `correlation_note` |
| < 0.3 or negative | genuine diversification | note it as a positive in `correlation_note`; still no size bonus beyond the normal cap |

A single Pearson figure on hourly crypto returns is noisy and heavy-tailed, and you receive only one estimator, so give any value within ±0.1 of a threshold the more conservative reading.

## Effective number of bets

With n holdings and average pairwise correlation ρ, the book behaves like roughly n / (1 + (n − 1)ρ) independent positions. Three holdings at ρ = 0.8 are 1.15 bets; at ρ = 0.3 they are 1.9. Use `regime_pack.avg_pairwise_corr_1h` as ρ for the whole book and the candidate's own `corr_to_open_positions` values when deciding whether the third slot adds anything. If the effective count would stay under 1.5 after adding the candidate, the honest answer is that the position cap of 3 is already full in risk terms: `conditional` with a halved `max_size_pct`, or `veto` when `btc_regime_4h == bear`.

## Beta and the reference coin

- `beta_btc` > 1.3: the symbol amplifies reference-coin moves. Its stop must survive a normal reference move: a 1% reference hour becomes ≥ 1.3% here, so a 2% stop has little room; require `stop_pct` at the cap only if `stop_vs_atr_noise_ok` is true, otherwise `veto`.
- `beta_btc` < 0.7 with `corr_to_btc_1h` > 0.7: a muted follower; it will not lead a recovery, so expect longer holding times than the technical read suggests. Put that in `required_monitoring`.
- `corr_to_btc_1h` < 0.3: the symbol trades on its own story. That is diversification, but also a sign that the move is idiosyncratic (news, listing, manipulation). Before approving, check `news_risk_symbols` and the symbol's `flow` / `flow_blocks`; with `flow` `null`, at most `conditional`.

## Regime-conditional correlation

Correlations are not stable: in the source's regime tables, co-movement in down-trends and high-volatility stretches runs 0.7–0.9 and approaches 1 in a selloff, while sideways markets sit near 0.2–0.4. Translate:

- `btc_regime_4h == bear`: assume every pairwise correlation is at least 0.7 regardless of the measured snapshot. The measured number came from the recent window and lags the regime; do not let a low reading argue for a third correlated long.
- `btc_regime_4h == range`: measured correlations are the most trustworthy here; diversification claims are allowed.
- `btc_regime_4h == bull` with `avg_pairwise_corr_1h` > 0.8: a one-way market where everything rises together also falls together; cap `exposure_cap_pct` at 20 and say why in `portfolio_note`.

## What not to do

- Do not infer co-movement by comparing `last_closes_1h` of two symbols, and do not reason from ticker names, sectors or narratives ("both are layer-ones") as if that were a measured correlation; only the fields above count.
- Do not propose spreads, hedges, or "long one / avoid the other as a pair"; there is no short leg and no spread position in this bot.
- Do not treat negative correlation as a hedge to justify extra size. On a long-only book a negatively correlated asset is just a separate bet with its own stop.
- Do not treat a correlation measured over the recent window as a long-run relationship; cointegration-style claims are out of scope.

## Output fields

- `candidates[].correlation_note` (Korean): the highest `corr_to_open_positions` value with its holding, `corr_to_btc_1h`, `beta_btc`, and the effective-bets conclusion, each cited by field name.
- `candidates[].verdict` and `max_size_pct`: from the screening table and the effective-bets rule; these only tighten the numbers from the risk-analysis procedure.
- `candidates[].conditions` / `required_monitoring`: Korean entries such as "홀딩 A 와의 상관이 0.6 아래로 내려오면 정상 사이즈" naming `risk_pack.corr_to_open_positions`.
- `positions[]`: when two holdings now show pairwise correlation > 0.8 under `btc_regime_4h == bear`, mark the weaker one (lower `health` by the thesis checks) `trim` with a Korean `reason` citing that holding's `risk_packs[<symbol>].corr_to_open_positions` value (the pair figure), not the book-wide average.
- `global.exposure_cap_pct` and `global.portfolio_note`: carry the book-level effective-bets statement; enum values exactly as the schema spells them.
