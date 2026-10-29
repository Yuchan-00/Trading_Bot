---
name: correlation-analysis
description: 코드가 계산한 롤링 상관(corr_to_btc_1h·beta_btc·corr_to_open_positions)과 BTC 레짐으로 "세 번째 포지션이 새로운 베팅인가"를 판단해 중복 포지션 거부·사이즈 축소·상관 메모를 쓰는 법 (수치 규칙은 역할 프롬프트를 따름)
roles: [risk_manager]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/correlation-analysis/SKILL.md (MIT)"
license: MIT
---
# Rolling correlation: is the next position a new bet?

## Paths

risk_manager only: `risk_packs[symbol].corr_to_btc_1h`, `risk_packs[symbol].beta_btc`, `risk_packs[symbol].corr_to_open_positions` (holding symbol → corr); `regime.avg_pairwise_corr_1h`, `regime.btc_regime_4h`, `regime.fused`; `account_pack.position_count`; `news_risk_symbols`; `flow_blocks[symbol].*`.

Precedence: your role prompt governs. Step 4 (|corr to a holding| > 0.8 is a veto), step 7 (correlation haircuts) and step 12 (holding actions) are the rules. This skill explains what the correlation fields can and cannot tell you. It adds no thresholds of its own.

## When it matters

Whenever a candidate arrives while `account_pack.position_count` ≥ 1, and on every cycle with two or more holdings. Pair trading, cointegration and hedge ratios need a short leg; none exists in a spot long-only bot, so what remains is the snapshot correlation structure the code computes from 1h returns.

## What the fields mean

- `corr_to_btc_1h`: Pearson correlation of the symbol's hourly returns with BTC (the regime benchmark) over the recent window.
- `beta_btc`: slope of the symbol's returns on BTC's; above 1 it amplifies BTC moves, below 1 it mutes them.
- `corr_to_open_positions`: correlation of this symbol with each current holding.
- `regime.avg_pairwise_corr_1h`: the book-wide average; above 0.8 the regime is `fused` (see the correlation-regime skill).

You never compute a correlation. A `null` or missing entry means the relationship is unknown; never read it as low.

## Reading the candidate against the book

- Above 0.8 to any holding: the same trade in another ticker. Veto; code blocks it too, so your reason documents it.
- 0.6 to 0.8: strong co-movement. The prompt halves the size; holding sizes are not in the payload, so never subtract a guessed holding size.
- 0.3 to 0.6: partial overlap. Mention it in `correlation_note`; no extra rule.
- Below 0.3 or negative: genuine diversification on this window. Say so, but it earns no size bonus and is no hedge; on a long-only book a negatively correlated coin is just a separate bet with its own stop.
- High `corr_to_btc_1h` (the prompt uses > 0.85 outside a bull BTC regime) means the candidate is mostly BTC beta; several such positions are one market bet even when their pairwise numbers are missing.

Apply the prompt's lines exactly. A single Pearson estimate on hourly crypto returns is noisy and heavy-tailed, so a value just under a line is worth a sentence in `correlation_note`, not a different verdict.

## How many bets does the book hold?

Position count overstates diversification. Three holdings that all move together behave like little more than one position, while three loosely related ones behave like roughly two. Use `regime.avg_pairwise_corr_1h` and the candidate's own `corr_to_open_positions` values to describe this in words in `portfolio_note` ("보유 3종목이 사실상 하나의 BTC 베타 베팅"); do not compute a formula and do not invent a new cap from it.

## Beta and BTC

- `beta_btc` well above 1: an ordinary BTC hour becomes a larger move here, leaving less room inside the stop. Never widen the stop for it; add a `required_monitoring` item on BTC's hourly move.
- `beta_btc` below 1 with high `corr_to_btc_1h`: a muted follower; it rarely leads a recovery, so the move may take longer than the bull horizon suggests. Note it in `required_monitoring`.
- `corr_to_btc_1h` low: the coin trades on its own story. That is diversification, but also a hint that the move is idiosyncratic (news, listing, manipulation). Check `news_risk_symbols` and `flow_blocks[symbol]` before approving and mention the result in `correlation_note`.

## Correlations change with the regime

Measured correlations lag the market. In down-trends and high-volatility stretches crypto co-movement climbs toward one; in sideways markets it is lowest and most trustworthy.

- `regime.btc_regime_4h` bear: the snapshot probably understates how the coins will move together in a selloff. Never let a low reading argue for more size or a third correlated long.
- `regime.btc_regime_4h` range: measured values are most reliable; diversification claims are allowed.
- `regime.btc_regime_4h` bull with high average correlation: everything that rises together also falls together. The prompt's fused rules cover the extreme; below it, say so in `portfolio_note`.

## Holdings

When two holdings now move almost as one, that is evidence against any assumption that they are independent bets. Mark the assumption from that evidence and set `health` and `action` with the prompt's formula and step 12 (which trims under `regime.fused` with |`corr_to_btc_1h`| > 0.8). Cite the holding's own `risk_packs[symbol].corr_to_open_positions` entry, not only the book-wide average.

## What not to do

- Do not infer co-movement from prices, ticker names, sectors or narratives ("둘 다 레이어1"); only the fields above count.
- Do not propose spreads, hedges or pair trades; there is no short leg.
- Do not treat a short-window correlation as a long-run relationship.

## Output

- `correlation_note` (Korean): the highest `corr_to_open_positions` value and its holding, `corr_to_btc_1h`, `beta_btc`, in the format `"<field path>: <value> — <짧은 한국어 해석>"`, e.g. `"risk_packs[symbol].corr_to_open_positions: 0.72 — 보유 종목과 강한 동조, 사이즈 절반"`.
- `conditions` / `required_monitoring`: observable field tests in the same format.
- `portfolio_note`: the book-level "how many real bets" statement. Enum values exactly as the schema spells them.
