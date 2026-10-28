"""scanner.py (SPEC §18) — 가짜 MarketData 로 게이트·랭킹·레짐·제외·펀딩창·격리·캐시 검증. 전부 오프라인."""
from __future__ import annotations

import json
import logging
import math
import random
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import formatdate

import pytest

from tradingbot import scanner as S
from tradingbot.config import load_settings
from tradingbot.exchange import ExchangeError
from tradingbot.features import FeaturePack, FlowPack, TechnicalLabels
from tradingbot.models import (
    BotState,
    Candle,
    DailyStats,
    FuturesContext,
    OrderBookSummary,
    Position,
    Ticker,
)

TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
SIGMA = {"15m": 0.002, "1h": 0.004, "4h": 0.008, "1d": 0.02}
SECRET = "SuperSecretKey123456"


def utc_ms(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


NOW = utc_ms(2026, 10, 28, 10, 0, 5)   # 펀딩창 밖


# ======================================================================= 가짜 MarketData
@dataclass
class Spec:
    p0: float = 100.0
    quote_volume_24h: float | None = 50_000_000.0
    spread_pct: float = 0.0002            # 2bps (소수)
    depth: float = 1_000_000.0
    change_24h: float | None = 0.01       # 소수(1%)
    drift: float = 0.0                    # 봉당 로그수익률 드리프트
    common: float = 0.0                   # 공통 인자 비중(0 독립, 1 완전 동일)
    last_1h_ret: float | None = None      # 마지막 1h 봉 수익률 강제
    last_vol_mult: float = 1.0            # 마지막 15m 봉 거래량 배수
    recent_vol_mult: float = 1.0          # 최근 96개 15m 봉 거래량 배수
    funding: float | None = 0.0001
    oi: float | None = 1e9
    ls: float | None = 1.2
    futures: bool = True
    fail: dict = field(default_factory=dict)   # method -> exception instance
    fail_tf: dict = field(default_factory=dict)  # tf -> exception instance
    too_few_15m: bool = False
    stale: bool = False


class FakeMarketData:
    def __init__(self, specs: dict[str, Spec], now_ms: int = NOW):
        self.specs = specs
        self.now_ms = now_ms
        self.calls: list[tuple] = []

    def _spec(self, symbol: str) -> Spec:
        if symbol not in self.specs:
            raise ExchangeError(f"unknown symbol {symbol}")
        return self.specs[symbol]

    def _walk(self, symbol: str, tf: str, n: int) -> list[Candle]:
        sp = self._spec(symbol)
        tf_ms = TF_MS[tf]
        end_ts = (self.now_ms // tf_ms) * tf_ms - tf_ms
        if sp.stale:
            end_ts -= 10 * tf_ms
        rng_i = random.Random(zlib.crc32(f"{symbol}|{tf}".encode()))
        rng_c = random.Random(zlib.crc32(f"common|{tf}".encode()))
        rng_v = random.Random(zlib.crc32(f"vol|{symbol}|{tf}".encode()))
        s = SIGMA[tf]
        rets = []
        for _ in range(n):
            c, i = rng_c.gauss(0, s), rng_i.gauss(0, s)
            rets.append(sp.common * c + (1 - sp.common) * i + sp.drift)
        if tf == "1h" and sp.last_1h_ret is not None:
            rets[-1] = math.log(1 + sp.last_1h_ret)
        out = []
        prev = sp.p0
        for k, r in enumerate(rets):
            close = prev * math.exp(r)
            vol = 1000.0 * (1 + 0.2 * rng_v.random())
            if tf == "15m":
                if k >= n - 96:
                    vol *= sp.recent_vol_mult
                if k == n - 1:
                    vol *= sp.last_vol_mult
            out.append(Candle(ts=end_ts - (n - 1 - k) * tf_ms, open=prev, high=max(prev, close) * (1 + 0.3 * s),
                              low=min(prev, close) * (1 - 0.3 * s), close=close, volume=vol))
            prev = close
        return out

    # --- MarketData 프로토콜
    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append(("ohlcv", symbol, timeframe))
        sp = self._spec(symbol)
        if "fetch_ohlcv" in sp.fail:
            raise sp.fail["fetch_ohlcv"]
        if timeframe in sp.fail_tf:
            raise sp.fail_tf[timeframe]
        n = 50 if (timeframe == "15m" and sp.too_few_15m) else limit
        return self._walk(symbol, timeframe, n)

    def fetch_ticker(self, symbol):
        self.calls.append(("ticker", symbol))
        sp = self._spec(symbol)
        if "fetch_ticker" in sp.fail:
            raise sp.fail["fetch_ticker"]
        last = self._walk(symbol, "15m", 300)[-1].close
        return Ticker(symbol=symbol, last=last, bid=last * (1 - sp.spread_pct / 2), ask=last * (1 + sp.spread_pct / 2),
                      quote_volume_24h=sp.quote_volume_24h, change_24h_pct=sp.change_24h, ts=self.now_ms)

    def fetch_orderbook_summary(self, symbol, depth=50):
        self.calls.append(("orderbook", symbol))
        sp = self._spec(symbol)
        if "fetch_orderbook_summary" in sp.fail:
            raise sp.fail["fetch_orderbook_summary"]
        last = self._walk(symbol, "15m", 300)[-1].close
        return OrderBookSummary(symbol=symbol, best_bid=last * (1 - sp.spread_pct / 2),
                                best_ask=last * (1 + sp.spread_pct / 2), spread_pct=sp.spread_pct,
                                bid_depth_usdt_1pct=sp.depth, ask_depth_usdt_1pct=sp.depth, ts=self.now_ms)

    def fetch_futures_context(self, symbol):
        self.calls.append(("futures", symbol))
        sp = self._spec(symbol)
        if "fetch_futures_context" in sp.fail:
            raise sp.fail["fetch_futures_context"]
        if not sp.futures:
            return None
        return FuturesContext(symbol=symbol, funding_rate=sp.funding, open_interest_usdt=sp.oi,
                              long_short_ratio=sp.ls, ts=self.now_ms)

    def server_time_ms(self):
        return self.now_ms


# ======================================================================= 도우미
SYMS = ("ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT")


def make_settings(symbols=SYMS, max_candidates=2, feeds="https://feeds.example.com/rss"):
    return load_settings({"AGENT_BACKEND": "mock", "SYMBOLS": ",".join(symbols),
                          "MAX_CANDIDATES_PER_CYCLE": str(max_candidates), "NEWS_FEEDS": feeds})


def make_state(cycle=10, positions=None, cooldown=None, loss_cooldown=None) -> BotState:
    return BotState(mode="paper", cycle=cycle, positions=positions or {}, paper_quote_balance=10_000.0,
                    peak_equity=10_000.0, daily=DailyStats(date="2026-10-28", start_equity=10_000.0),
                    cooldown_until_cycle=cooldown or {}, loss_cooldown_until_cycle=loss_cooldown or {},
                    kill_switch=False, kill_reason=None, llm_paused_until_ts=None, llm_pause_reason=None,
                    last_cycle_ts=None)


def make_position(symbol, entry=100.0) -> Position:
    return Position(symbol=symbol, base_amount=1.0, entry_price=entry, quote_spent=entry, stop_loss_price=entry * 0.98,
                    take_profit_price=entry * 1.04, opened_cycle=1, opened_ts=NOW - 3_600_000)


def default_specs(**overrides) -> dict[str, Spec]:
    specs = {s: Spec(p0=50.0 + 10 * i, drift=0.0002 * (i - 2)) for i, s in enumerate(SYMS)}
    specs["BTC/USDT"] = Spec(p0=60_000.0)
    for sym, sp in overrides.items():
        specs[sym] = sp
    return specs


def make_scanner(specs=None, settings=None, thresholds=None, **kw):
    market = FakeMarketData(specs or default_specs())
    sc = S.Scanner(settings or make_settings(), market, thresholds=thresholds or S.Thresholds(), **kw)
    return sc, market


def skipped_reason(result, symbol) -> list[str]:
    return [r["reason"] for r in result.skipped if r["symbol"] == symbol]


# ======================================================================= in_funding_window
@pytest.mark.parametrize("hms,expected", [
    ((7, 59, 30), True), ((8, 0, 30), True), ((8, 5, 0), False), ((0, 0, 0), True),
    ((16, 1, 0), True), ((16, 1, 1), False), ((15, 59, 0), True), ((15, 58, 59), False),
    ((12, 0, 0), False), ((23, 59, 30), True),
])
def test_in_funding_window(hms, expected):
    assert S.in_funding_window(utc_ms(2026, 10, 28, *hms)) is expected


def test_in_funding_window_custom_window():
    t = utc_ms(2026, 10, 28, 8, 4, 0)
    assert S.in_funding_window(t, 300) is True
    assert S.in_funding_window(t, 60) is False
    assert S.in_funding_window(utc_ms(2026, 10, 28, 8, 0, 0), 0) is True
    assert S.in_funding_window(utc_ms(2026, 10, 28, 8, 0, 1), 0) is False


# ======================================================================= zscores
def test_zscores_standard():
    z = S.zscores({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": None})
    assert z["e"] is None
    vals = [z[k] for k in "abcd"]
    assert sum(vals) == pytest.approx(0.0, abs=1e-12)
    assert math.sqrt(sum(v * v for v in vals) / 4) == pytest.approx(1.0)
    assert z["a"] < z["b"] < z["c"] < z["d"]


def test_zscores_rank_fallback_below_four():
    z = S.zscores({"a": 10.0, "b": -5.0, "c": 3.0, "d": None})
    assert z == {"a": 1.0, "b": -1.0, "c": 0.0, "d": None}
    assert S.zscores({"a": 1.0, "b": 2.0}) == {"a": -1.0, "b": 1.0}
    assert S.zscores({"a": 7.0}) == {"a": 0.0}
    assert S.zscores({}) == {}
    assert S.zscores({"a": None}) == {"a": None}


def test_zscores_rank_ties_average():
    z = S.zscores({"a": 1.0, "b": 1.0, "c": 2.0})
    assert z["a"] == z["b"] == pytest.approx(-0.5)
    assert z["c"] == 1.0


def test_zscores_constant_and_clip_and_nan():
    assert S.zscores({k: 5.0 for k in "abcde"}) == {k: 0.0 for k in "abcde"}
    vals = {f"s{i}": 0.0 for i in range(20)}
    vals["big"] = 1000.0
    z = S.zscores(vals)
    assert z["big"] == S.Z_CLIP
    z2 = S.zscores({"a": float("nan"), "b": 1.0, "c": 2.0, "d": 3.0, "e": 4.0})
    assert z2["a"] is None and z2["e"] > 0


# ======================================================================= 팩터 함수
def test_momentum_pct_skips_last_bar():
    closes = [100.0] * 5 + [110.0, 500.0]
    # 마지막 봉(500)은 빼고, 직전 2개 수익률: ln(110/100)
    assert S.momentum_pct(closes, 2, 1) == pytest.approx(math.log(1.1) * 100)
    assert S.momentum_pct(closes, 10, 1) is None
    assert S.momentum_pct([100.0, 0.0, 1.0, 2.0], 1, 1) is None
    assert S.momentum_pct(closes, 0) is None


def test_reversal_and_rv():
    closes = [100.0, 100.0, 101.0, 102.0, 103.0, 104.0]
    assert S.reversal_1h_pct(closes) == pytest.approx(-math.log(104 / 100) * 100)
    assert S.reversal_1h_pct(closes[:4]) is None
    flat = [100.0] * 30
    assert S.rv_24h_pct(flat) == pytest.approx(0.0)
    alt = [100.0 * (1.01 if i % 2 else 1.0) for i in range(30)]
    assert S.rv_24h_pct(alt) > 0
    assert S.rv_24h_pct(alt[:10]) is None


def test_funding_zscore():
    assert S.funding_zscore(0.0003, [0.0001, 0.0002, 0.0001, 0.0002]) == pytest.approx(
        (0.0003 - 0.00015) / 0.00005773502691896258)
    assert S.funding_zscore(0.0003, [0.0001, 0.0002]) is None      # 표본 부족
    assert S.funding_zscore(0.0003, [0.0001] * 5) is None          # 분산 0
    assert S.funding_zscore(None, [0.0001, 0.0002, 0.0003]) is None
    assert S.funding_zscore(1.0, [0.0, 0.0001, 0.0]) == 10.0        # 클립


def test_volume_multiple_and_spike():
    th = S.Thresholds()
    cs = [Candle(ts=i, open=1, high=1, low=1, close=1, volume=(3.0 if i >= 104 else 1.0)) for i in range(200)]
    assert S.volume_24h_multiple(cs) == pytest.approx(3.0)
    assert S.volume_24h_multiple(cs[:150]) is None
    assert S.is_unconfirmed_spike(20.0, None, th) is True
    assert S.is_unconfirmed_spike(20.0, 1.5, th) is True
    assert S.is_unconfirmed_spike(20.0, 2.0, th) is False
    assert S.is_unconfirmed_spike(15.0, None, th) is False          # 경계: 초과만
    assert S.is_unconfirmed_spike(-30.0, None, th) is False         # 급락은 급등 제외 대상 아님
    assert S.is_unconfirmed_spike(None, None, th) is False


def test_ls_percentile():
    assert S.ls_percentile(None, []) is None
    assert S.ls_percentile(0.8, []) == 0.0
    assert S.ls_percentile(2.0, []) == 100.0
    assert S.ls_percentile(1.4, []) == pytest.approx(50.0)
    assert S.ls_percentile(5.0, []) == 100.0
    hist = [1.0 + 0.1 * i for i in range(20)]   # 1.0..2.9
    assert S.ls_percentile(10.0, hist) == 100.0
    assert S.ls_percentile(0.5, hist) == 0.0
    assert S.ls_percentile(1.0, hist) == pytest.approx(2.5)


# ======================================================================= Thresholds
def test_thresholds_defaults_match_spec():
    th = S.Thresholds()
    assert th.MIN_QUOTE_VOLUME_24H == 20_000_000.0
    assert th.MAX_SPREAD_BPS == 10.0
    assert th.MIN_DEPTH_1PCT_USDT == 200_000.0
    assert th.MIN_VOL_RATIO_15M == 0.3
    assert th.MAX_SPIKE_24H_PCT == 15.0
    assert th.SPIKE_VOLUME_MULT == 2.0
    assert th.FUNDING_WINDOW_S == 60
    assert S.Thresholds.MAX_SPREAD_BPS == 10.0      # 클래스 속성으로도 접근
    assert S.FACTOR_WEIGHTS == {"momentum_4h": 0.25, "momentum_1d": 0.15, "reversal_1h": 0.10, "rv_24h": -0.10,
                                "volume_ratio": 0.10, "rs_vs_btc": 0.15, "funding_z": -0.10, "oi_change_24h": 0.05}


def test_thresholds_from_env():
    th = S.Thresholds.from_env({"SCANNER_MIN_QUOTE_VOLUME_24H": "5e6", "SCANNER_FUNDING_WINDOW_S": "120",
                                "SCANNER_MAX_SPREAD_BPS": "abc", "SCANNER_MIN_DEPTH_1PCT_USDT": "-1",
                                "SCANNER_MIN_VOL_RATIO_15M": "nan", "SCANNER_SPIKE_VOLUME_MULT": " "})
    assert th.MIN_QUOTE_VOLUME_24H == 5_000_000.0
    assert th.FUNDING_WINDOW_S == 120 and isinstance(th.FUNDING_WINDOW_S, int)
    assert th.MAX_SPREAD_BPS == 10.0 and th.MIN_DEPTH_1PCT_USDT == 200_000.0
    assert th.MIN_VOL_RATIO_15M == 0.3 and th.SPIKE_VOLUME_MULT == 2.0
    assert S.Thresholds.from_env({}) == S.Thresholds()


def test_scanner_reads_env_thresholds_at_construction(monkeypatch):
    monkeypatch.setenv("SCANNER_MAX_SPREAD_BPS", "3.5")
    sc = S.Scanner(make_settings(), FakeMarketData(default_specs()))
    assert sc.thresholds.MAX_SPREAD_BPS == 3.5
    explicit = S.Thresholds(MAX_SPREAD_BPS=7.0)
    assert S.Scanner(make_settings(), FakeMarketData(default_specs()), thresholds=explicit).thresholds is explicit


# ======================================================================= liquidity_gate
def _tk(qv=50e6, bid=99.99, ask=100.01):
    return Ticker(symbol="X/USDT", last=100.0, bid=bid, ask=ask, quote_volume_24h=qv, change_24h_pct=None, ts=0)


def _ob(spread_pct=0.0002, bid_depth=1e6, ask_depth=1e6):
    return OrderBookSummary(symbol="X/USDT", best_bid=99.99, best_ask=100.01, spread_pct=spread_pct,
                            bid_depth_usdt_1pct=bid_depth, ask_depth_usdt_1pct=ask_depth, ts=0)


def test_liquidity_gate_pass():
    g = S.liquidity_gate("X/USDT", _tk(), _ob(), 1.0, S.Thresholds())
    assert g.ok and g.reasons == []
    assert g.spread_bps == pytest.approx(2.0) and g.quote_volume_24h == 50e6 and g.depth_1pct_usdt == 1e6


def test_liquidity_gate_each_failure():
    th = S.Thresholds()
    g = S.liquidity_gate("X/USDT", _tk(qv=1e6), _ob(), 1.0, th)
    assert not g.ok and g.reasons[0].startswith("low_quote_volume:")
    g = S.liquidity_gate("X/USDT", _tk(), _ob(spread_pct=0.0015), 1.0, th)
    assert g.reasons == ["wide_spread:15.0bps>10"]
    g = S.liquidity_gate("X/USDT", _tk(), _ob(ask_depth=150_000.0), 1.0, th)
    assert g.reasons == ["thin_depth:150000<200000"] and g.depth_1pct_usdt == 150_000.0
    g = S.liquidity_gate("X/USDT", _tk(), _ob(), 0.1, th)
    assert g.reasons == ["low_volume_ratio_15m:0.10<0.3"]
    g = S.liquidity_gate("X/USDT", _tk(qv=None), None, None, th)
    assert set(g.reasons) == {"quote_volume_unknown", "orderbook_unavailable", "volume_ratio_15m_unknown"}
    # 호가 없을 때 스프레드는 티커 bid/ask 로
    assert g.spread_bps == pytest.approx(2.0)
    g = S.liquidity_gate("X/USDT", _tk(bid=None, ask=None), None, 1.0, th)
    assert "spread_unknown" in g.reasons


def test_liquidity_gate_boundaries_inclusive():
    th = S.Thresholds()
    g = S.liquidity_gate("X/USDT", _tk(qv=20_000_000.0), _ob(spread_pct=0.001, bid_depth=200_000.0,
                                                             ask_depth=200_000.0), 0.3, th)
    assert g.ok, g.reasons


# ======================================================================= rank_candidates
def _fp(symbol, vr=1.0, closes=None):
    return FeaturePack(symbol=symbol, ts=0, price=100.0, per_tf={"15m": TechnicalLabels(timeframe="15m",
                       volume_ratio_20=vr)}, last_closes_1h=closes or [])


def _trend(n, g, p0=100.0, step=TF_MS["4h"]):
    return [Candle(ts=i * step, open=p0 * g ** i, high=p0 * g ** i, low=p0 * g ** i, close=p0 * g ** i, volume=1.0)
            for i in range(n)]


def test_rank_candidates_momentum_order_and_rs():
    syms = ["A/USDT", "B/USDT", "C/USDT", "D/USDT"]
    growth = {"A/USDT": 1.01, "B/USDT": 1.0, "C/USDT": 0.99, "D/USDT": 1.005}
    candles = {s: {"4h": _trend(30, g)} for s, g in growth.items()}
    candles["BTC/USDT"] = {"4h": _trend(30, 1.002)}
    feats = {s: _fp(s) for s in syms}
    out = S.rank_candidates(feats, {}, {}, "BTC/USDT", set(), 4, candles=candles)
    assert [c.symbol for c in out] == ["A/USDT", "D/USDT", "B/USDT", "C/USDT"]
    assert [c.rank for c in out] == [1, 2, 3, 4]
    assert out[0].composite_z > out[1].composite_z > out[2].composite_z > out[3].composite_z
    a = out[0].factor_table
    assert set(a) == set(S.FACTOR_NAMES)
    assert a["momentum_4h"] == pytest.approx(24 * math.log(1.01) * 100)
    assert a["rs_vs_btc"] == pytest.approx(24 * (math.log(1.01) - math.log(1.002)) * 100)
    assert a["funding_z"] is None and a["oi_change_24h"] is None
    assert out[0].reasons[0] == "rank:1/4"
    assert any(r.startswith("momentum_4h:z=") for r in out[0].reasons)
    assert any(r.startswith("missing:") for r in out[0].reasons)


def test_rank_candidates_exclude_max_and_ties():
    feats = {s: _fp(s) for s in ["C/USDT", "A/USDT", "B/USDT"]}
    out = S.rank_candidates(feats, {}, {}, "BTC/USDT", set(), 5)
    # 모든 팩터가 같음 → 동점 → 심볼 오름차순
    assert [c.symbol for c in out] == ["A/USDT", "B/USDT", "C/USDT"]
    assert all(c.composite_z == 0.0 for c in out)
    assert [c.symbol for c in S.rank_candidates(feats, {}, {}, "BTC/USDT", {"A/USDT"}, 1)] == ["B/USDT"]
    assert S.rank_candidates(feats, {}, {}, "BTC/USDT", set(), 0) == []
    assert S.rank_candidates(feats, {}, {}, "BTC/USDT", set(feats), 3) == []


def test_rank_candidates_funding_weight_negative():
    syms = ["A/USDT", "B/USDT", "C/USDT", "D/USDT"]
    feats = {s: _fp(s) for s in syms}
    flows = {s: FlowPack(symbol=s, funding_now=f) for s, f in zip(syms, [0.001, 0.0001, 0.0002, 0.0003])}
    hist = {s: [0.0001, 0.0002, 0.0001, 0.0002] for s in syms}
    out = S.rank_candidates(feats, flows, {}, "BTC/USDT", set(), 4, funding_history=hist)
    # 펀딩 z 가 높을수록 감점(가중치 −0.10) → A 가 꼴찌
    assert out[-1].symbol == "A/USDT"
    assert out[-1].factor_table["funding_z"] > 0


def test_rank_candidates_pack_fallback_without_candles():
    closes = [100.0] * 16 + [100.0, 99.0, 98.0, 97.0]
    out = S.rank_candidates({"A/USDT": _fp("A/USDT", closes=closes)}, {}, {}, "BTC/USDT", set(), 1)
    ft = out[0].factor_table
    assert ft["reversal_1h"] == pytest.approx(-math.log(97 / 100) * 100)
    assert ft["volume_ratio"] == 1.0 and ft["momentum_4h"] is None


# ======================================================================= Scanner — 기본
def test_scan_basic_shape():
    sc, market = make_scanner()
    res = sc.scan(make_state(), NOW)
    assert isinstance(res, S.ScanResult)
    assert res.ts == NOW
    assert res.universe == list(SYMS)
    assert "BTC/USDT" not in res.universe
    # BTC 는 레짐용으로 항상 조회
    assert ("ticker", "BTC/USDT") in market.calls and "BTC/USDT" in res.marks
    assert res.regime.btc_regime_4h != "unknown" and res.regime.verdict == "normal"
    assert 0 < len(res.candidates) <= 2
    assert [c.rank for c in res.candidates] == list(range(1, len(res.candidates) + 1))
    for c in res.candidates:
        assert set(c.factor_table) == set(S.FACTOR_NAMES)
        assert c.symbol in res.feature_packs and c.symbol in res.flow_packs and c.symbol in res.risk_packs
    assert set(res.feature_packs) == set(res.flow_packs) == set(res.risk_packs) == {c.symbol for c in res.candidates}
    assert set(res.liquidity) == set(SYMS)
    assert all(g.ok for g in res.liquidity.values())
    assert set(res.quality) == set(SYMS) | {"BTC/USDT"}
    assert all(q.ok for q in res.quality.values())
    assert res.fetched["ETH/USDT"] == 300 + 200 + 200 + 120
    assert res.funding_window is False
    assert res.news == [] and res.news_available is False
    assert res.skipped == []
    # 결과는 직렬화 가능
    json.dumps(res.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def test_scan_candidates_sorted_by_composite():
    sc, _ = make_scanner(settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    zs = [c.composite_z for c in res.candidates]
    assert zs == sorted(zs, reverse=True)
    assert len(res.candidates) == 5


@pytest.mark.parametrize("mc", [1, 2, 3])
def test_scan_max_candidates_respected(mc):
    sc, _ = make_scanner(settings=make_settings(max_candidates=mc))
    res = sc.scan(make_state(), NOW)
    assert len(res.candidates) == mc
    assert len(res.feature_packs) == mc


def test_scan_deterministic():
    r1 = make_scanner()[0].scan(make_state(), NOW)
    r2 = make_scanner()[0].scan(make_state(), NOW)
    a = json.dumps(r1.model_dump(mode="json"), sort_keys=True)
    b = json.dumps(r2.model_dump(mode="json"), sort_keys=True)
    assert a == b


def test_scan_btc_in_universe_can_be_candidate():
    specs = default_specs()
    specs["BTC/USDT"] = Spec(p0=60_000.0, drift=0.002)
    settings = make_settings(symbols=("BTC/USDT", "ETH/USDT"), max_candidates=2)
    sc, market = make_scanner(specs=specs, settings=settings)
    res = sc.scan(make_state(), NOW)
    assert res.universe == ["BTC/USDT", "ETH/USDT"]
    assert sum(1 for c in market.calls if c == ("ticker", "BTC/USDT")) == 1
    assert "BTC/USDT" in [c.symbol for c in res.candidates]
    btc = next(c for c in res.candidates if c.symbol == "BTC/USDT")
    assert btc.factor_table["rs_vs_btc"] == pytest.approx(0.0)


# ======================================================================= 게이트·제외
def test_scan_low_volume_and_wide_spread_rejected():
    specs = default_specs(**{"SOL/USDT": Spec(quote_volume_24h=1_000_000.0),
                             "BNB/USDT": Spec(p0=70.0, spread_pct=0.0015)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    cands = {c.symbol for c in res.candidates}
    assert "SOL/USDT" not in cands and "BNB/USDT" not in cands
    assert res.liquidity["SOL/USDT"].ok is False
    assert any(r.startswith("low_quote_volume:") for r in res.liquidity["SOL/USDT"].reasons)
    assert skipped_reason(res, "SOL/USDT")[0].startswith("liquidity:low_quote_volume:")
    assert res.liquidity["BNB/USDT"].reasons == ["wide_spread:15.0bps>10"]
    assert skipped_reason(res, "BNB/USDT") == ["liquidity:wide_spread:15.0bps>10"]
    assert len(res.candidates) == 3


def test_scan_thin_depth_and_low_15m_volume_rejected():
    specs = default_specs(**{"XRP/USDT": Spec(depth=50_000.0), "ADA/USDT": Spec(last_vol_mult=0.05)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    assert skipped_reason(res, "XRP/USDT") == ["liquidity:thin_depth:50000<200000"]
    assert skipped_reason(res, "ADA/USDT")[0].startswith("liquidity:low_volume_ratio_15m:")
    assert {c.symbol for c in res.candidates} == {"ETH/USDT", "SOL/USDT", "BNB/USDT"}


def test_scan_custom_thresholds_loosen():
    specs = default_specs(**{"SOL/USDT": Spec(quote_volume_24h=1_000_000.0)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5),
                         thresholds=S.Thresholds(MIN_QUOTE_VOLUME_24H=500_000.0))
    res = sc.scan(make_state(), NOW)
    assert res.liquidity["SOL/USDT"].ok
    assert "SOL/USDT" in {c.symbol for c in res.candidates}


def test_scan_held_cooldown_revenge_excluded():
    state = make_state(cycle=10, positions={"ETH/USDT": make_position("ETH/USDT")},
                       cooldown={"SOL/USDT": 10, "XRP/USDT": 9}, loss_cooldown={"BNB/USDT": 15})
    sc, _ = make_scanner(settings=make_settings(max_candidates=5))
    res = sc.scan(state, NOW)
    cands = {c.symbol for c in res.candidates}
    assert cands == {"XRP/USDT", "ADA/USDT"}       # XRP 쿨다운은 cycle 9 로 끝남
    assert skipped_reason(res, "ETH/USDT") == ["held"]
    assert skipped_reason(res, "SOL/USDT") == ["cooldown:until_cycle=10"]
    assert skipped_reason(res, "BNB/USDT") == ["revenge_window:until_cycle=15"]
    # 보유 종목은 팩·마크가 있다(LLM 입력용) — 후보는 아님
    assert "ETH/USDT" in res.feature_packs and "ETH/USDT" in res.risk_packs and "ETH/USDT" in res.flow_packs
    assert "ETH/USDT" in res.marks
    assert set(res.feature_packs) == cands | {"ETH/USDT"}
    # 쿨다운 종목은 팩 없음
    assert "SOL/USDT" not in res.feature_packs


def test_scan_held_outside_whitelist_is_fetched():
    state = make_state(positions={"DOGE/USDT": make_position("DOGE/USDT", entry=0.2)})
    specs = default_specs(**{"DOGE/USDT": Spec(p0=0.2)})
    sc, market = make_scanner(specs=specs)
    res = sc.scan(state, NOW)
    assert res.universe == list(SYMS) + ["DOGE/USDT"]
    assert "DOGE/USDT" in res.marks and "DOGE/USDT" in res.feature_packs
    assert skipped_reason(res, "DOGE/USDT") == ["held"]
    assert "DOGE/USDT" not in {c.symbol for c in res.candidates}


def test_scan_held_risk_pack_corr_to_open():
    state = make_state(positions={"ETH/USDT": make_position("ETH/USDT"), "SOL/USDT": make_position("SOL/USDT")})
    sc, _ = make_scanner()
    res = sc.scan(state, NOW)
    cand = res.candidates[0].symbol
    assert set(res.risk_packs[cand].corr_to_open_positions) == {"ETH/USDT", "SOL/USDT"}
    assert set(res.risk_packs["ETH/USDT"].corr_to_open_positions) == {"SOL/USDT"}
    assert res.risk_packs[cand].corr_to_btc_1h is not None


def test_scan_spike_without_volume_excluded():
    specs = default_specs(**{"SOL/USDT": Spec(change_24h=0.20), "BNB/USDT": Spec(change_24h=0.20, recent_vol_mult=3.0)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    reasons = skipped_reason(res, "SOL/USDT")
    assert len(reasons) == 1 and reasons[0].startswith("spike_24h:20.0%>15")
    assert "BNB/USDT" in {c.symbol for c in res.candidates}      # 거래량 확인된 급등은 통과
    assert skipped_reason(res, "BNB/USDT") == []


def test_scan_name_alarm_excluded():
    specs = default_specs(**{"XRP/USDT": Spec(last_1h_ret=0.06), "BTC/USDT": Spec(p0=60_000.0, last_1h_ret=0.0)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    assert "XRP/USDT" in res.regime.name_alarm_symbols
    assert skipped_reason(res, "XRP/USDT") == ["name_alarm"]
    assert "XRP/USDT" not in {c.symbol for c in res.candidates}
    assert res.regime.verdict != "macro_alarm"


def test_scan_macro_alarm_empties_candidates():
    specs = {s: Spec(p0=50.0, common=1.0, last_1h_ret=0.06) for s in SYMS}
    specs["BTC/USDT"] = Spec(p0=60_000.0, common=1.0, last_1h_ret=0.06)
    state = make_state(positions={"ETH/USDT": make_position("ETH/USDT", entry=50.0)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=3))
    res = sc.scan(state, NOW)
    assert res.regime.fused is True and res.regime.btc_shock is True
    assert res.regime.verdict == "macro_alarm"
    assert res.candidates == []
    macro = [r for r in res.skipped if r["reason"] == "macro_alarm"]
    assert len(macro) == 3
    # 보유 종목 팩·마크는 남는다(청산 검토용)
    assert set(res.feature_packs) == {"ETH/USDT"} and "ETH/USDT" in res.marks


def test_scan_macro_alarm_with_no_rankable_symbols_records_marker():
    specs = {s: Spec(p0=50.0, common=1.0, last_1h_ret=0.06) for s in SYMS}
    specs["BTC/USDT"] = Spec(p0=60_000.0, common=1.0, last_1h_ret=0.06)
    state = make_state(cooldown={s: 99 for s in SYMS})
    sc, _ = make_scanner(specs=specs)
    res = sc.scan(state, NOW)
    assert res.regime.verdict == "macro_alarm" and res.candidates == []
    assert {"symbol": "*", "reason": "macro_alarm"} in res.skipped


# ======================================================================= 격리
def test_scan_failing_symbol_isolated():
    specs = default_specs(**{"SOL/USDT": Spec(fail_tf={"1h": ExchangeError("boom")}),
                             "BNB/USDT": Spec(fail={"fetch_ticker": RuntimeError("weird")}),
                             "XRP/USDT": Spec(fail={"fetch_orderbook_summary": ExchangeError("ob")}),
                             "ADA/USDT": Spec(fail={"fetch_futures_context": ValueError("fut")})})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    assert skipped_reason(res, "SOL/USDT") == ["fetch_error:ohlcv_1h:ExchangeError"]
    assert skipped_reason(res, "BNB/USDT") == ["fetch_error:ticker:RuntimeError"]
    assert "SOL/USDT" in res.marks          # 티커는 성공했으므로 마크 있음
    assert "BNB/USDT" not in res.marks
    assert res.fetched["SOL/USDT"] == 300   # 15m 만 받음
    assert "SOL/USDT" not in res.quality and "BNB/USDT" not in res.quality
    # 호가 실패 → 유동성 게이트에서 탈락(사유 남김), 선물 예외 → 플로우 팩 unknown 으로 계속
    assert "orderbook_unavailable" in res.liquidity["XRP/USDT"].reasons
    assert skipped_reason(res, "XRP/USDT")[0].startswith("liquidity:")
    assert {c.symbol for c in res.candidates} == {"ETH/USDT", "ADA/USDT"}
    assert res.flow_packs["ADA/USDT"].data_confidence == "none"


def test_scan_all_symbols_fail_returns_empty_result():
    specs = {s: Spec(fail={"fetch_ticker": ExchangeError("down")}) for s in list(SYMS) + ["BTC/USDT"]}
    sc, _ = make_scanner(specs=specs)
    res = sc.scan(make_state(), NOW)
    assert res.candidates == [] and res.marks == {} and res.feature_packs == {}
    assert len(res.skipped) == len(SYMS) + 1
    assert res.regime.btc_regime_4h == "unknown" and res.regime.verdict == "normal"
    assert res.sentiment.label == "unknown"


def test_scan_unknown_symbol_in_market_isolated():
    specs = default_specs()
    del specs["ADA/USDT"]
    sc, _ = make_scanner(specs=specs)
    res = sc.scan(make_state(), NOW)
    assert skipped_reason(res, "ADA/USDT") == ["fetch_error:ticker:ExchangeError"]
    assert len(res.candidates) == 2


def test_scan_btc_failure_regime_unknown():
    specs = default_specs(**{"BTC/USDT": Spec(fail={"fetch_ohlcv": ExchangeError("btc down")})})
    sc, _ = make_scanner(specs=specs)
    res = sc.scan(make_state(), NOW)
    assert res.regime.btc_regime_4h == "unknown"
    assert res.regime.btc_shock is False and res.regime.verdict != "macro_alarm"
    assert res.regime.name_alarm_symbols == []
    assert len(res.candidates) == 2
    assert all(c.factor_table["rs_vs_btc"] is None for c in res.candidates)


def test_scan_exception_text_never_leaks(caplog):
    specs = default_specs(**{"SOL/USDT": Spec(fail={"fetch_ohlcv": ExchangeError(f"apiKey={SECRET}")}),
                             "BNB/USDT": Spec(fail={"fetch_orderbook_summary": RuntimeError(SECRET)}),
                             "XRP/USDT": Spec(fail={"fetch_futures_context": RuntimeError(SECRET)})})
    sc, _ = make_scanner(specs=specs, fear_greed_fetcher=lambda: (_ for _ in ()).throw(RuntimeError(SECRET)))
    with caplog.at_level(logging.DEBUG, logger="tradingbot"):
        res = sc.scan(make_state(), NOW)
    assert SECRET not in json.dumps(res.model_dump(mode="json"))
    assert SECRET not in caplog.text


# ======================================================================= 품질
def test_scan_quality_failure_skipped():
    specs = default_specs(**{"SOL/USDT": Spec(stale=True)})
    sc, _ = make_scanner(specs=specs, settings=make_settings(max_candidates=5))
    res = sc.scan(make_state(), NOW)
    assert res.quality["SOL/USDT"].ok is False
    r = skipped_reason(res, "SOL/USDT")
    assert len(r) == 1 and r[0].startswith("quality:") and "stale_15m" in r[0]
    assert "SOL/USDT" not in res.liquidity and "SOL/USDT" not in res.feature_packs


def test_scan_marks_for_held_even_when_quality_fails():
    state = make_state(positions={"ETH/USDT": make_position("ETH/USDT"), "SOL/USDT": make_position("SOL/USDT")})
    specs = default_specs(**{"ETH/USDT": Spec(too_few_15m=True), "SOL/USDT": Spec(stale=True)})
    sc, market = make_scanner(specs=specs)
    res = sc.scan(state, NOW)
    for sym in ("ETH/USDT", "SOL/USDT"):
        assert sym in res.marks and res.marks[sym] > 0
        assert res.quality[sym].ok is False
        assert sym not in res.feature_packs and sym not in res.flow_packs and sym not in res.risk_packs
        assert skipped_reason(res, sym)[0].startswith("quality:")
    assert "too_few_15m:50<220" in res.quality["ETH/USDT"].reasons
    assert res.marks["ETH/USDT"] == pytest.approx(market.fetch_ticker("ETH/USDT").last)


# ======================================================================= 펀딩창
@pytest.mark.parametrize("hms,expected", [((7, 59, 30), True), ((8, 0, 30), True), ((8, 5, 0), False)])
def test_scan_funding_window_flag(hms, expected):
    now = utc_ms(2026, 10, 28, *hms)
    sc, market = make_scanner()
    market.now_ms = now
    res = sc.scan(make_state(), now)
    assert res.funding_window is expected
    assert len(res.candidates) == 2        # 후보는 남긴다(신규 주문 금지는 cycle 몫)


def test_scan_funding_window_uses_threshold():
    now = utc_ms(2026, 10, 28, 8, 4, 0)
    sc, market = make_scanner(thresholds=S.Thresholds(FUNDING_WINDOW_S=300))
    market.now_ms = now
    assert sc.scan(make_state(), now).funding_window is True


# ======================================================================= 캐시
def test_scan_caches_4h_1d_within_bucket():
    sc, market = make_scanner()
    sc.scan(make_state(), NOW)
    first = list(market.calls)
    market.calls.clear()
    later = NOW + 15 * 60_000          # 같은 4h·1d 구간 (10:15)
    market.now_ms = later
    sc.scan(make_state(), later)
    tfs = [c[2] for c in market.calls if c[0] == "ohlcv"]
    assert "4h" not in tfs and "1d" not in tfs
    assert tfs.count("15m") == len(SYMS) + 1 and tfs.count("1h") == len(SYMS) + 1
    assert sum(1 for c in first if c[0] == "ohlcv" and c[2] == "4h") == len(SYMS) + 1
    # 4h 구간이 바뀌면(12:00) 4h 만 다시 받고 1d 는 재사용
    market.calls.clear()
    t2 = utc_ms(2026, 10, 28, 12, 0, 5)
    market.now_ms = t2
    sc.scan(make_state(), t2)
    tfs = [c[2] for c in market.calls if c[0] == "ohlcv"]
    assert tfs.count("4h") == len(SYMS) + 1 and "1d" not in tfs
    # 날짜가 바뀌면 1d 도
    market.calls.clear()
    t3 = utc_ms(2026, 10, 29, 0, 0, 5)
    market.now_ms = t3
    sc.scan(make_state(), t3)
    tfs = [c[2] for c in market.calls if c[0] == "ohlcv"]
    assert tfs.count("1d") == len(SYMS) + 1


def test_scan_failed_fetch_not_cached():
    specs = default_specs(**{"ETH/USDT": Spec(fail_tf={"4h": ExchangeError("x")})})
    sc, market = make_scanner(specs=specs)
    sc.scan(make_state(), NOW)
    assert ("ETH/USDT", "4h") not in sc._tf_cache
    specs["ETH/USDT"].fail_tf.clear()
    market.calls.clear()
    res = sc.scan(make_state(), NOW + 60_000)
    assert ("ohlcv", "ETH/USDT", "4h") in market.calls
    assert skipped_reason(res, "ETH/USDT") == []


# ======================================================================= 심리·플로우 이력
def test_scan_sentiment_components():
    sc, _ = make_scanner(fear_greed_fetcher=lambda: 30)
    res = sc.scan(make_state(), NOW, headline_polarity=0.5)
    comp = res.sentiment.components
    assert comp["fear_greed"] == 30.0
    assert comp["headline_polarity"] == pytest.approx(75.0)
    assert comp["ls_pctile"] == pytest.approx((1.2 - 0.8) / 1.2 * 100)   # 이력 부족 → 선형 매핑
    assert comp["funding_z"] is None                                        # 이력 없음
    assert res.sentiment.score_0_100 is not None and res.sentiment.label != "unknown"


def test_scan_sentiment_without_inputs_and_bad_fetcher():
    specs = {s: Spec(futures=False) for s in list(SYMS) + ["BTC/USDT"]}
    sc, _ = make_scanner(specs=specs, fear_greed_fetcher=lambda: 150)
    res = sc.scan(make_state(), NOW)
    assert res.sentiment.score_0_100 is None and res.sentiment.label == "unknown"
    assert all(v is None for v in res.sentiment.components.values())
    sc2, _ = make_scanner(fear_greed_fetcher=lambda: (_ for _ in ()).throw(OSError("net")))
    assert sc2.scan(make_state(), NOW).sentiment.components["fear_greed"] is None


def test_scan_funding_history_builds_funding_z_and_mean():
    specs = default_specs()
    sc, market = make_scanner(specs=specs)
    t = NOW
    for i, f in enumerate([0.0001, 0.0002, 0.0001, 0.0002]):
        for s in SYMS:
            specs[s].funding = f
        market.now_ms = t + i * 900_000
        sc.scan(make_state(), market.now_ms)
    for s in SYMS:
        specs[s].funding = 0.0005
    market.now_ms = t + 4 * 900_000
    res = sc.scan(make_state(), market.now_ms)
    expected = S.funding_zscore(0.0005, [0.0001, 0.0002, 0.0001, 0.0002])
    assert res.sentiment.components["funding_z"] is not None
    for c in res.candidates:
        assert c.factor_table["funding_z"] == pytest.approx(expected)
        assert res.flow_packs[c.symbol].funding_7d_mean == pytest.approx((0.0006 + 0.0005) / 5)


def test_scan_oi_change_and_history_persistence(tmp_path):
    specs = default_specs()
    settings = make_settings()
    m1 = FakeMarketData(specs, NOW)
    sc1 = S.Scanner(settings, m1, cache_dir=tmp_path, thresholds=S.Thresholds())
    sc1.scan(make_state(), NOW)
    assert (tmp_path / S.HISTORY_FILE).exists()
    json.loads((tmp_path / S.HISTORY_FILE).read_text(encoding="utf-8"))
    # 새 인스턴스(재시작)가 이력을 읽어 24h 뒤 OI 변화 계산
    for s in SYMS:
        specs[s].oi = 1.1e9
    later = NOW + 24 * 3_600_000
    m2 = FakeMarketData(specs, later)
    sc2 = S.Scanner(settings, m2, cache_dir=tmp_path, thresholds=S.Thresholds())
    res = sc2.scan(make_state(), later)
    for c in res.candidates:
        assert res.flow_packs[c.symbol].oi_change_24h_pct == pytest.approx(10.0)
        assert c.factor_table["oi_change_24h"] == pytest.approx(10.0)


def test_scan_corrupt_history_ignored(tmp_path):
    (tmp_path / S.HISTORY_FILE).write_text("{not json", encoding="utf-8")
    sc = S.Scanner(make_settings(), FakeMarketData(default_specs()), cache_dir=tmp_path, thresholds=S.Thresholds())
    res = sc.scan(make_state(), NOW)
    assert len(res.candidates) == 2
    json.loads((tmp_path / S.HISTORY_FILE).read_text(encoding="utf-8"))


def test_scan_market_funding_history_hook():
    specs = default_specs()
    market = FakeMarketData(specs)
    market.fetch_funding_history = lambda symbol: [0.0001, 0.0002, 0.0001, 0.0002]   # type: ignore[attr-defined]
    for s in SYMS:
        specs[s].funding = 0.0005
    sc = S.Scanner(make_settings(), market, thresholds=S.Thresholds())
    res = sc.scan(make_state(), NOW)
    expected = S.funding_zscore(0.0005, [0.0001, 0.0002, 0.0001, 0.0002])
    assert all(c.factor_table["funding_z"] == pytest.approx(expected) for c in res.candidates)
    assert res.sentiment.components["funding_z"] is not None


def test_scan_ls_percentile_from_history():
    specs = default_specs()
    sc, market = make_scanner(specs=specs)
    for i in range(S.LS_MIN_HISTORY):
        for s in SYMS:
            specs[s].ls = 1.0 + 0.01 * i
        market.now_ms = NOW + i * 900_000
        sc.scan(make_state(), market.now_ms)
    for s in SYMS:
        specs[s].ls = 5.0
    market.now_ms = NOW + S.LS_MIN_HISTORY * 900_000
    res = sc.scan(make_state(), market.now_ms)
    assert res.sentiment.components["ls_pctile"] == 100.0


# ======================================================================= 뉴스
def _rss(title: str) -> str:
    pub = formatdate(time.time() - 3600, usegmt=True)
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            f'<item><title>{title}</title><link>https://www.coindesk.com/a/{abs(hash(title))}</link>'
            f'<pubDate>{pub}</pubDate></item></channel></rss>')


def test_scan_news_with_injected_fetcher():
    calls = []

    def fetcher(url):
        calls.append(url)
        return _rss("Ethereum rallies as ETH upgrade nears")

    sc, _ = make_scanner(news_fetcher=fetcher)
    res = sc.scan(make_state(), NOW)
    assert calls == ["https://feeds.example.com/rss"]
    assert res.news_available is True
    assert res.news[0].symbols == ["ETH"]


def test_scan_news_empty_feed_not_available():
    sc, _ = make_scanner(news_fetcher=lambda url: '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>')
    res = sc.scan(make_state(), NOW)
    assert res.news == [] and res.news_available is False
    sc2, _ = make_scanner(news_fetcher=lambda url: (_ for _ in ()).throw(OSError("down")))
    res2 = sc2.scan(make_state(), NOW)
    assert res2.news_available is False


def test_scan_no_news_fetcher_does_not_fetch(monkeypatch):
    from tradingbot import news as news_mod

    def boom(*a, **k):  # pragma: no cover - 호출되면 실패
        raise AssertionError("network fetch attempted")

    monkeypatch.setattr(news_mod, "default_fetcher", boom)
    sc, _ = make_scanner()
    res = sc.scan(make_state(), NOW)
    assert res.news_available is False


# ======================================================================= 리스크 팩 계획 주문액
def test_scan_planned_notional_override():
    sc, _ = make_scanner()
    r_default = sc.scan(make_state(), NOW)
    sym = r_default.candidates[0].symbol
    depth = r_default.risk_packs[sym].depth_taker_usdt
    # 기본: 추정 자본(10000) × 10% = 1000
    assert r_default.risk_packs[sym].impact_bps_est == pytest.approx(1000.0 / depth * 100)
    r2 = sc.scan(make_state(), NOW, planned_notional=5000.0)
    assert r2.risk_packs[sym].impact_bps_est == pytest.approx(5000.0 / depth * 100)
    assert r2.risk_packs[sym].exec_cost_pct is not None


def test_scan_payload_compactable():
    from tradingbot.features import compact
    sc, _ = make_scanner()
    res = sc.scan(make_state(), NOW)
    for c in res.candidates:
        d = compact(c)
        assert set(d) == {"symbol", "rank", "composite_z", "factor_table", "reasons"}
        compact(res.liquidity[c.symbol])
        compact(res.feature_packs[c.symbol])
    json.dumps(compact(res.regime), sort_keys=True, ensure_ascii=False)
