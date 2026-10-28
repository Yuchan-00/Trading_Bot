---
name: perp-funding-basis
description: 펀딩비·미결제약정·롱숏 라벨을 군집(크라우딩) 심리로 읽어, 파생 분석가는 현물 롱 차단·워시아웃 판정에, 약세 토론자는 crowded_unwind 사망경로에 쓰는 법 (판정 규칙은 역할 프롬프트를 따름)
roles: [derivatives_flow, bear]
version: 1
source: "adapted from HKUDS/Vibe-Trading agent/src/skills/perp-funding-basis/SKILL.md (MIT)"
license: MIT
---
# Funding and open interest as a crowding gauge

## Paths per role

- derivatives_flow: `flow_packs[symbol].funding_now`, `.funding_7d_mean`, `.funding_annualized_pct`, `.funding_regime`, `.oi_change_24h_pct`, `.oi_funding_label`, `.perp_premium_pct`, `.ls_ratio`, `.ls_label`, `.divergence_flags`, `.data_confidence`; cut-offs in `thresholds.*`.
- bear: the analyst's view only, `flow.posture`, `flow.crowding_risk`, `flow.squeeze_direction`, `flow.block_new_longs`, `flow.washout_setup`, `flow.flow_bias`, `flow.evidence`; scanner context `scanner_row.factor_table.funding_z`, `scanner_row.factor_table.oi_change_24h`. The bear never sees the raw funding pack.

Precedence: your role prompt governs. derivatives_flow steps 2-8 (posture by family count, crowding, squeeze, block, washout, bias, confidence) and bear steps 2-5 (death-path bands, greed flags, verdict) are the rules; this skill explains why the labels mean what they mean.

## How the labels are built (read, never recompute)

- Funding is a raw per-8h decimal. Positive funding means longs pay shorts to keep the perp near spot. `funding_annualized_pct` = rate × 3 × 365 × 100 expresses it as a yearly carry cost.
- `funding_regime` applies `thresholds.funding_*` to the 8h rate: `extreme_long` above 0.0005, `elevated_long` 0.0002 to 0.0005, `neutral` −0.0001 to 0.0002, `negative` −0.0002 to −0.0001, `squeeze` below −0.0002.
- `oi_funding_label`: `leveraged_long_buildup` = OI up at least `thresholds.oi_buildup_pct` with funding above `thresholds.oi_buildup_funding`; `short_buildup` = OI up with negative funding; `deleveraging` = OI down at least 5%.
- `ls_label`: `crowded_long` above 2.0, `crowded_short` below 0.8.
- `unknown` in any label is missing information. It never counts as `neutral` agreement and never as "calm".

## Why crowding matters to a spot buyer

High positive funding is not strength. It is a running fee the long crowd pays, and a crowd that pays to stay in is one small dip away from being forced out. Joining it means buying from people who will soon need to sell. The mirror case is the only contrarian long this channel may flag: after open interest has been flushed and the survivors are shorts paying to stay short, the forced sellers are gone. That is `washout_setup`; it says the selling pressure is exhausted, not that price will rise.

Persistence matters. A single hot print after a flat week is less crowded than a week-long elevated mean, so compare `funding_now` with `funding_7d_mean`: `elevated_long` with the current print below the mean is cooling and reads `neutral` (prompt step 2). When `funding_7d_mean` is `null`, skip the comparison and lean toward `neutral`.

## Derivatives_flow use

1. Funding is one family for posture; OI, long/short and premium are the others. Funding alone never makes `euphoria` or `capitulation` (step 2 needs three agreeing families, `greed`/`fear` two).
2. Crowding, squeeze direction and block come from steps 3, 4 and 6. Typical funding-driven blocks: `funding_regime` extreme_long, `oi_funding_label` leveraged_long_buildup, or `divergence_flags` containing `price_up_oi_down` while funding is elevated or extreme (price rising on shrinking participation while the remaining longs pay up).
3. Washout needs the full step 7 conjunction: `deleveraging`, funding `negative`/`squeeze`, no `crowded_long`, no nearby cluster, `cascade_risk` not high, `data_confidence` high. A negative funding print alone, or an OI drop alone, is not a washout.
4. `flow_bias` long is never allowed with `data_confidence` low; `none` yields the conservative row.
5. `block_reason` (Korean) names the deciding field. Evidence follows `<field path>: <value> — <짧은 한국어 해석>`, e.g. `flow_packs[symbol].funding_regime: extreme_long — 롱이 높은 펀딩 비용 부담`.
6. `market_wide_note` says whether crowding is broad (most symbols greed or euphoria) or confined to one name.

## Bear use

1. `crowded_unwind` is the family this skill feeds. The conditions and band come from bear step 2: `flow.posture` greed or euphoria, `flow.crowding_risk` high, `sentiment.score_0_100` > 80, `feature_extract.cgo_60` ≥ +3, `scanner_row.factor_table.funding_z` > 1, or a `corr_to_open` value above 0.8; high when two hold.
2. `description` (Korean) explains the mechanism: the long crowd pays to stay in and a modest dip forces liquidations. `observable_trigger` names a field you can see and a level, e.g. `flow.block_new_longs` turning true or `scanner_row.factor_table.funding_z` rising above 1.5.
3. Euphoric leverage (`flow.posture`, `flow.crowding_risk`, `flow.block_new_longs`) is one greed flag (bear step 3), not three; cite the strongest field.
4. `flow.washout_setup` true is a bullish item: step 4 requires a `disconfirming_evidence` entry naming a field that weakens it (a correlated holding, warming `regime.density`, thin `scanner_row.liquidity.depth_1pct_usdt`). If nothing weakens it, it seeds `what_would_disprove_bear`.
5. `monitoring_conditions` should be convertible into a `funding_rate` red line, e.g. "펀딩비 8h > 0.0005 이면 재평가".
6. A high `crowded_unwind` plus any second high path supports `decline` (step 5).

## What not to do

- Do not treat high positive funding as bullish confirmation.
- Do not infer funding direction from price; cite the labels.
- Do not re-derive labels from raw rates; code already applied `thresholds`.
- Do not express a short view; bearish readings become `flow_bias` avoid, `block_new_longs` true, or a bear death path.
