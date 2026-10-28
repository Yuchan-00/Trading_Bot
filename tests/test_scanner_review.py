"""적대적 리뷰: tradingbot/scanner.py (SPEC §18). 각 테스트는 리뷰에서 찾은 결함 하나를 재현한다. 전부 오프라인."""
from __future__ import annotations

from test_scanner import (  # 같은 tests/ 디렉터리의 가짜 MarketData·도우미 재사용
    NOW,
    SYMS,
    FakeMarketData,
    Spec,
    default_specs,
    make_position,
    make_settings,
    make_state,
)

from tradingbot import scanner as S
from tradingbot.exchange import ExchangeError


class FlakyOnceMarket(FakeMarketData):
    """(symbol, tf) 의 첫 조회만 짧은(불완전) 응답을 돌려주고 이후는 정상인 가짜 거래소."""

    def __init__(self, specs, flaky: dict[tuple[str, str], int], now_ms: int = NOW):
        super().__init__(specs, now_ms)
        self.flaky = dict(flaky)

    def fetch_ohlcv(self, symbol, timeframe, limit):
        out = super().fetch_ohlcv(symbol, timeframe, limit)
        n = self.flaky.pop((symbol, timeframe), None)
        if n is not None:
            return out[-n:]
        return out


# ---------------------------------------------------------------- F1: 불완전 4h/1d 응답이 구간 내내 고정됨
def test_transient_incomplete_1d_response_is_not_pinned_for_the_whole_day():
    """1d 첫 응답이 일시적으로 10봉뿐(quality too_few_1d)이면, 거래소가 회복한 다음 사이클엔 다시 받아야 한다.
    현재는 품질 실패 데이터도 (symbol,'1d') 캐시에 넣어 같은 UTC 날짜 동안(최대 24h) 재사용 → 보유 종목이
    하루 종일 LLM 입력(technical 청산 판단)에서 빠진다."""
    state = make_state(positions={"ETH/USDT": make_position("ETH/USDT")})
    market = FlakyOnceMarket(default_specs(), {("ETH/USDT", "1d"): 10})
    sc = S.Scanner(make_settings(), market, thresholds=S.Thresholds())
    first = sc.scan(state, NOW)
    assert first.quality["ETH/USDT"].ok is False          # 첫 사이클 실패는 정상
    later = NOW + 15 * 60_000                             # 같은 4h·1d 구간
    market.now_ms = later
    res = sc.scan(state, later)
    assert res.quality["ETH/USDT"].ok is True, res.quality["ETH/USDT"].reasons
    assert "ETH/USDT" in res.feature_packs


# ---------------------------------------------------------------- F2: funding_z 분산 하한 없음 → 심리 고정
def test_float_noise_funding_change_does_not_peg_composite_sentiment():
    """펀딩이 사실상 일정(1e-12 단위 잡음)할 때 1e-9(=0.0000001%p) 변화가 z=+10(클립)으로 잡혀
    종합심리 funding 성분이 100, 라벨 extreme_greed 가 된다(G1 '종합심리>80' 차단을 촉발)."""
    specs = default_specs()
    for s in list(SYMS) + ["BTC/USDT"]:
        specs[s].ls = 1.7
    market = FakeMarketData(specs)
    sc = S.Scanner(make_settings(), market, thresholds=S.Thresholds())
    for k in range(4):
        for s in list(SYMS) + ["BTC/USDT"]:
            specs[s].funding = 0.0001 + k * 1e-12
        market.now_ms = NOW + k * 900_000
        sc.scan(make_state(), market.now_ms)
    for s in list(SYMS) + ["BTC/USDT"]:
        specs[s].funding = 0.0001 + 1e-9
    market.now_ms = NOW + 4 * 900_000
    res = sc.scan(make_state(), market.now_ms)
    fz = res.sentiment.components["funding_z"]
    assert fz is None or fz < 90.0, fz
    assert res.sentiment.label != "extreme_greed"


# ---------------------------------------------------------------- F3: BTC 실패 시 verdict
def test_btc_failure_verdict_is_normal_per_spec():
    """SPEC §18: 'BTC 데이터 실패 시 regime.verdict="normal" 에 unknown 라벨'. 현재는 알트 상관이 높으면 'fused'."""
    specs = default_specs()
    for s in SYMS:
        specs[s].common = 1.0
    specs["BTC/USDT"] = Spec(p0=60_000.0, fail={"fetch_ohlcv": ExchangeError("btc down")})
    sc = S.Scanner(make_settings(), FakeMarketData(specs), thresholds=S.Thresholds())
    res = sc.scan(make_state(), NOW)
    assert res.regime.btc_regime_4h == "unknown"
    assert res.regime.verdict == "normal", res.regime.verdict


# ---------------------------------------------------------------- F4: 보유 종목 품질 실패 → 상관 정보 소실
def test_held_symbol_with_partial_quality_failure_still_in_corr_to_open():
    """보유 SOL 의 15m 만 품질 실패(1h 캔들은 정상 조회됨)하면 후보 ETH 의 corr_to_open_positions 에서
    SOL 이 조용히 빠진다 → G1 '|상관|>0.8 보유 종목' 차단이 동작할 수 없어 사실상 같은 노출을 중복 진입."""
    specs = default_specs(**{"SOL/USDT": Spec(p0=60.0, common=1.0, too_few_15m=True),
                             "ETH/USDT": Spec(p0=50.0, common=1.0)})
    state = make_state(positions={"SOL/USDT": make_position("SOL/USDT", 60.0)})
    sc = S.Scanner(make_settings(max_candidates=5), FakeMarketData(specs), thresholds=S.Thresholds())
    res = sc.scan(state, NOW)
    assert res.quality["SOL/USDT"].ok is False
    assert "ETH/USDT" in res.risk_packs
    corr = res.risk_packs["ETH/USDT"].corr_to_open_positions
    assert "SOL/USDT" in corr, corr
