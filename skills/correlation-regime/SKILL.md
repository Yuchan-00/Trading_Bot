---
name: correlation-regime
description: 코드가 계산한 regime_pack(상관 밀도·fused·macro_alarm·name_alarm)을 "시장이 한 덩어리인가, 누가 먼저 깨졌는가"로 읽어 진입 중단·사이즈 축소·확신도 하한 상향으로 옮기는 절차와 금지 규칙
roles: [bear, risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/correlation-regime/SKILL.md (MIT)"
license: MIT
---
# Correlation regime: one bloc or many?

## When to apply

Apply on every call that carries the regime snapshot, computed in code from the watchlist's 1h returns; you never compute correlations yourself. This text says `regime_pack`; cite your payload's key (`regime` for the risk_manager; per-symbol fields under `risk_packs[<symbol>]`). Its fields:

- `avg_pairwise_corr_1h`: mean of all pairwise 1h return correlations over the recent window
- `density`: share of pairs whose correlation clears 0.7 (an "edge")
- `fused`: true when `avg_pairwise_corr_1h` > 0.8
- `btc_regime_4h`: `bull` / `bear` / `range` / `unknown` from the reference coin's 4h trend labels
- `btc_shock`: the reference coin's last 1h move exceeded 2× its own 1h ATR
- `name_alarm_symbols`: symbols whose last 1h move exceeded 3× their own ATR while the reference coin moved less than 1× its ATR (idiosyncratic break)
- `verdict`: `normal` / `fused` / `macro_alarm` (macro_alarm = `btc_shock` ∧ `fused`)

Diversification across three spot positions exists only while pairs move independently. When density climbs, the book becomes one bet on the market and a stop on one position is effectively a stop on all. When one asset breaks alone while the reference coin is quiet, that asset is the story, not the market.

## Reading the state

Read with hysteresis in mind: a value just under a line is not "calm".

| `density` / `avg_pairwise_corr_1h` | State | Meaning for a long-only book |
|---|---|---|
| density < 0.45 and avg corr < 0.65 | defused | pairwise independence usable; normal sizing |
| density 0.45–0.65 or avg corr 0.65–0.8 | warming (dead band) | do not add a third correlated position; treat as fused if `btc_regime_4h == bear` |
| `fused == true` | fused | the book is one position; halve size, raise the confidence floor |
| `verdict == macro_alarm` | crisis | no new entries; every holding is reviewed for exit |

High density with a moderate average means a sub-cluster is fused while the rest is loose; apply the fused rule to any candidate whose `corr_to_open_positions` entries exceed 0.8.

## Attribution: who broke first

The code does a crude first-mover test for you. Read it with the abstention discipline of the original method: prefer no name to a wrong name.

- `btc_shock` true and `fused` true → macro shock. Nobody is "the culprit"; the whole book is exposed.
- `name_alarm_symbols` non-empty and `btc_shock` false → idiosyncratic break. The scanner already blocks the named symbol from entry; as a holding it is an exit candidate; anything correlated > 0.8 with it is contaminated.
- `btc_shock` true, `fused` false → a reference-coin move not yet propagated; expect it within a few bars, so alts are in a "wait" state, not a calm one.
- Nothing set → abstain from attribution; absence of an alarm is not evidence of safety.

The 3×ATR rule is coarse: read `name_alarm_symbols` as "watch or worse", not proof of collapse.

## What a fused regime is NOT

The regime state cannot time a top: fusion is confirmed after the first leg down, and the source found that exiting on regime onset lost to a plain price stop. The defensible response is de-grossing: cut new exposure and let the code stops work. Liquidating every holding needs `macro_alarm` or a symbol-level reason, never `fused` alone.

## What not to do

- Do not derive correlation from `last_closes_1h` or from which symbols rose together; only `regime_pack`, `corr_to_open_positions` and `corr_to_btc_1h` are evidence.
- Do not call a market "calm" because `verdict == normal`; the pack says nothing about news, funding or liquidity.
- Do not turn `btc_regime_4h == bear` into a short or a hedge. The only expressions are no entry, smaller size, higher floor, or exit.
- Do not loosen the code gates (empty candidate list on `macro_alarm`, half size on `fused`); your layer only tightens.

## Output: bear role

- Add a `death_paths` entry with `family: regime_flip` whenever `fused` is true, `density` ≥ 0.45, or `btc_regime_4h == bear`. `observable_trigger` cites the field (`regime_pack.density`, `regime_pack.btc_shock`) and the confirming level; `probability_band` is `high` under `macro_alarm`, `mid` under `fused`, `low` in the dead band; `time_to_impact_bars` 1–8 for shocks, `null` for slow fusion.
- A candidate with `corr_to_open` > 0.8 to any holding gets a `crowded_unwind` path: a stop on the holding is a stop on this entry too.
- `verdict` is `decline` under `macro_alarm` and at most `survivable_if_monitored` under `fused`, with a `monitoring_conditions` line naming `regime_pack.verdict` returning to `normal`.
- Put the regime read in `disconfirming_evidence` only when it contradicts a bullish technical label.

## Output: risk_manager role

- `global.entries_paused: true` with `pause_reason` citing `regime_pack.verdict` under `macro_alarm`.
- Under `fused`: `global.confidence_floor` ≥ 0.75, each candidate's `max_size_pct` ≤ 5, `global.exposure_cap_pct` no higher than `account_pack.exposure_pct` plus one half-sized slot; `portfolio_note` (Korean) says the holdings move as one position.
- In the dead band: `conditional` verdicts with a `conditions` entry tied to `regime_pack.density` < 0.45, and `correlation_note` naming the highest `corr_to_open_positions` value.
- Holdings: a symbol in `name_alarm_symbols` gets `assumption_status` containing `broken`, `health` ≤ 3, `action: exit`; other holdings under `macro_alarm` get at least `weakening`, `health` ≤ 5, `trim` (the holding's stop level is not in the payload; do not reason about it).
- Every claim in `veto_reasons`, `conditions`, `required_monitoring` and `reason` names a field under the payload's regime key or `risk_packs[<symbol>]`; free text in Korean, enum values exactly as the schema spells them.
