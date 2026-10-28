---
name: correlation-regime
description: 코드가 계산한 레짐 스냅샷(상관 밀도·fused·btc_shock·macro_alarm·name_alarm)을 "시장이 한 덩어리인가, 누가 먼저 깨졌는가"로 읽어 약세 변론가는 regime_flip 경로로, 리스크 관리자는 진입 중단·사이즈·확신도 하한·보유 재평가로 옮기는 법 (수치 규칙은 역할 프롬프트를 따름)
roles: [bear, risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/correlation-regime/SKILL.md (MIT)"
license: MIT
---
# Correlation regime: one bloc or many?

## Paths per role

- bear: `regime.btc_regime_4h`, `regime.fused`, `regime.density`, `regime.verdict`, `regime.btc_shock`, `regime.name_alarm_symbols`; `corr_to_open` (holding → corr); `hv_pct`. The bear payload has no `avg_pairwise_corr_1h` and no `corr_to_btc_1h`.
- risk_manager: `regime.*` (the same six plus `regime.avg_pairwise_corr_1h`); `risk_packs[symbol].corr_to_open_positions`, `risk_packs[symbol].corr_to_btc_1h`, `risk_packs[symbol].hv_pct_rank`.

Precedence: your role prompt governs. The bear's step 2 (`regime_flip` bands) and step 5 (verdict), and the risk manager's steps 1–3 (pause, floor, exposure cap), 7 (size) and 10–12 (holdings) are the rules. This skill explains what the fields mean and how to read them together; it adds no extra numbers.

## What the fields mean

All values are code-computed from the watchlist's 1h returns. You never compute a correlation and never infer one from which coins rose together.

- `density`: share of symbol pairs whose 1h correlation is above 0.7.
- `avg_pairwise_corr_1h` (risk only): mean pairwise correlation; `fused` is true when it is above 0.8.
- `btc_regime_4h`: bull / bear / range / unknown, from BTC's 4h trend (BTC is the regime benchmark).
- `btc_shock`: BTC's last 1h move exceeded twice its own 1h ATR%.
- `name_alarm_symbols`: symbols whose last 1h move exceeded three times their own ATR% while BTC moved less than one ATR%: a break specific to that coin.
- `verdict`: normal / fused / macro_alarm; macro_alarm means `btc_shock` and `fused` at once. Under macro_alarm the scanner already empties the candidate list, and name-alarm symbols are already excluded from entry.

## Why it matters for a long-only book

Three spot positions diversify only while they move independently. When density rises, the book behaves like one bet on the market, and a stop on one holding is effectively a stop on all of them. When one coin breaks alone while BTC is quiet, the story is that coin, not the market.

Read the state, not a single number:

- `fused` false and `density` below 0.45: pairwise independence is usable.
- `density` 0.45 or higher without `fused`: warming. A sub-group may already move as one; look at the candidate's own correlation to holdings.
- `fused` true: the book is one position. De-gross; do not liquidate on this alone.
- `verdict` macro_alarm: crisis; no new entries, and every holding is reviewed.

A value just under a line is not calm; mention it, but apply the prompt's thresholds exactly as written.

## Who broke first

Prefer no attribution to a wrong one.

- `btc_shock` and `fused` both true: a market shock. No single culprit; the whole book is exposed.
- `name_alarm_symbols` non-empty with `btc_shock` false: an idiosyncratic break. As a holding it is evidence against that thesis; anything strongly correlated with it is contaminated.
- `btc_shock` true, `fused` false: a BTC move that has not yet spread; treat alts as "wait", not calm.
- Nothing set: abstain. The absence of an alarm is not evidence of safety, and `verdict` normal says nothing about news, funding or liquidity.

The 3×ATR rule is coarse; read a name alarm as "watch or worse", not proof of collapse.

## What a fused regime is not

Fusion is confirmed after the first leg down, so it cannot time a top, and exiting every holding on its onset tends to lose to the plain code stop. The sound response is to cut new exposure and let the stops work. A full exit needs `macro_alarm` or a symbol-level reason, never `fused` alone.

## Bear use

- A `regime_flip` path is mandatory when `regime.fused` is true, `regime.density` ≥ 0.45 or `regime.btc_regime_4h` is bear. Take `probability_band` and `time_to_impact_bars` from your prompt's step 2; `observable_trigger` names the regime field and level, e.g. `regime.btc_shock` true.
- `hv_pct` above 80 means gaps through the stop are more likely; say so in the path description.
- Any `corr_to_open` value above 0.8 seeds a `crowded_unwind` path: a stop on that holding is a stop on this entry.
- Verdict follows your step 5: macro_alarm is a hard veto; under `fused` the best verdict is `survivable_if_monitored` with a monitoring condition on `regime.verdict`.
- Warming `regime.density` is a valid counter in `disconfirming_evidence` against a bullish technical or momentum item.
- Never turn a bear regime into a short or a hedge.

## Risk manager use

- Global: macro_alarm pauses entries; `fused` raises the confidence floor and lowers the exposure cap; a bear `btc_regime_4h` raises the floor. Use the exact values in your prompt's steps 1–3 and name the regime field in `pause_reason` or `portfolio_note`.
- Size: halve under `fused`, then apply the prompt's correlation haircuts from `risk_packs[symbol].corr_to_open_positions` and `corr_to_btc_1h` (step 7). Any |corr to a holding| above 0.8 is a veto (step 4).
- Warming density without `fused`: add a `required_monitoring` item on `regime.density` and say in `portfolio_note` that diversification is thinner than the position count suggests.
- Holdings: a held symbol in `regime.name_alarm_symbols`, or macro_alarm for every holding, is evidence against the stored assumptions. Mark the affected assumptions from that evidence, then compute `health` and `action` with the prompt's formula and rules (steps 10–12). The holding's stop level is not in the payload; do not reason about it.
- Never loosen a code gate (empty candidates on macro_alarm, half size on fused); your layer only tightens.

## Writing it down

Every claim cites a field: `"<field path>: <value> — <짧은 한국어 해석>"`, e.g. `"regime.density: 0.58 — 종목 간 동조화가 진행 중"`. Free text is Korean; enum values stay exactly as the schema spells them.
