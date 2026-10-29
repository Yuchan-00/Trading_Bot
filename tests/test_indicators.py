"""indicators.py 테스트 — 워밍업·결정성·손계산 값·경계."""
from __future__ import annotations

import json
import math
import random

import pytest

from tradingbot import indicators as ind
from tradingbot.indicators import WARMUP, adx, atr, bollinger, compute, ema, macd, rsi, sma, vwap
from tradingbot.models import Candle, IndicatorSet

DAY_MS = 86_400_000
TF_MS = 15 * 60 * 1000


# ---------------------------------------------------------------- 도우미
def mk_candle(i: int, close: float, *, high: float | None = None, low: float | None = None,
              volume: float = 1.0, ts: int | None = None, open_: float | None = None) -> Candle:
    h = close if high is None else high
    lo = close if low is None else low
    h = max(h, close)
    lo = min(lo, close)
    return Candle(
        ts=(i * TF_MS) if ts is None else ts,
        open=close if open_ is None else open_,
        high=h, low=lo, close=close, volume=volume,
    )


def series_candles(closes: list[float], *, spread: float = 0.0, volume: float = 1.0) -> list[Candle]:
    return [mk_candle(i, c, high=c + spread, low=c - spread, volume=volume) for i, c in enumerate(closes)]


def random_candles(n: int, seed: int = 7, start: float = 100.0) -> list[Candle]:
    rng = random.Random(seed)
    out = []
    price = start
    for i in range(n):
        o = price
        c = max(0.5, price * (1 + rng.uniform(-0.02, 0.02)))
        h = max(o, c) * (1 + rng.uniform(0, 0.01))
        lo = min(o, c) * (1 - rng.uniform(0, 0.01))
        v = rng.uniform(10, 100)
        out.append(Candle(ts=i * TF_MS, open=o, high=h, low=lo, close=c, volume=v))
        price = c
    return out


def assert_warmup(series, first_idx: int) -> None:
    """앞 first_idx 개는 None, 그 뒤는 전부 float."""
    assert all(v is None for v in series[:first_idx]), series[:first_idx]
    assert all(isinstance(v, float) for v in series[first_idx:]), series[first_idx:]


# ---------------------------------------------------------------- WARMUP 선언
def test_warmup_declaration_matches_spec():
    assert WARMUP == {
        "ema_20": 20, "ema_50": 50, "ema_200": 200, "rsi_14": 15, "macd": 35,
        "atr_14": 15, "bb": 20, "adx_14": 28, "volume_ratio_20": 21, "high_low_20": 20,
    }


def test_module_uses_logger_not_print():
    assert ind.log.name == "tradingbot.indicators"


# ---------------------------------------------------------------- SMA
class TestSMA:
    def test_hand_values(self):
        assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_n_one_is_identity(self):
        assert sma([3.0, 1.5, 2.0], 1) == [3.0, 1.5, 2.0]

    def test_length_and_warmup(self):
        out = sma(list(range(50)), 20)
        assert len(out) == 50
        assert_warmup(out, 19)

    def test_too_short_all_none(self):
        assert sma([1, 2, 3], 5) == [None, None, None]

    def test_empty(self):
        assert sma([], 3) == []

    def test_sliding_window_matches_naive(self):
        rng = random.Random(1)
        vals = [rng.uniform(1, 100) for _ in range(80)]
        out = sma(vals, 7)
        for i in range(6, 80):
            assert out[i] == pytest.approx(sum(vals[i - 6:i + 1]) / 7)

    @pytest.mark.parametrize("bad", [0, -1, 1.5, True])
    def test_bad_n(self, bad):
        with pytest.raises(ValueError):
            sma([1, 2, 3], bad)

    def test_does_not_mutate_input(self):
        vals = [1, 2, 3, 4]
        sma(vals, 2)
        assert vals == [1, 2, 3, 4]


# ---------------------------------------------------------------- EMA
class TestEMA:
    def test_hand_values_sma_seed(self):
        # n=3: seed = mean(1,2,3)=2 ; k=0.5 ; 4->3.0 ; 5->4.0
        assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_hand_values_n2(self):
        # n=2: seed = 1.5 ; k=2/3 ; 10 -> (10-1.5)*2/3+1.5 = 7.1666...
        out = ema([1, 2, 10], 2)
        assert out[0] is None
        assert out[1] == 1.5
        assert out[2] == pytest.approx(1.5 + (10 - 1.5) * 2 / 3)

    def test_constant_series(self):
        out = ema([5.0] * 30, 10)
        assert_warmup(out, 9)
        assert all(v == pytest.approx(5.0) for v in out[9:])

    def test_warmup_and_length(self):
        out = ema(list(range(100)), 20)
        assert len(out) == 100
        assert_warmup(out, 19)

    def test_too_short(self):
        assert ema([1.0] * 19, 20) == [None] * 19

    def test_empty(self):
        assert ema([], 20) == []

    def test_n_one_is_identity(self):
        assert ema([3.0, 1.0, 2.0], 1) == [3.0, 1.0, 2.0]

    def test_bad_n(self):
        with pytest.raises(ValueError):
            ema([1, 2], 0)

    def test_ema_reacts_faster_than_sma_to_step(self):
        # 0 이 20개 뒤에 100 으로 계단: 직후 몇 봉은 EMA 가 SMA 보다 가격에 더 가깝다
        vals = [0.0] * 20 + [100.0] * 5
        e = ema(vals, 10)
        s = sma(vals, 10)
        for i in range(20, 25):
            assert s[i] < e[i] < 100.0

    def test_linear_ramp_lag_equals_sma(self):
        # 선형 추세에서는 EMA 와 SMA 의 지연이 (n-1)/2 로 같다 -> 수렴 후 일치
        vals = [float(i) for i in range(1, 61)]
        e = ema(vals, 10)
        s = sma(vals, 10)
        assert e[-1] == pytest.approx(s[-1], rel=1e-6)
        assert e[-1] < vals[-1]


# ---------------------------------------------------------------- RSI
class TestRSI:
    def test_hand_values_n2(self):
        # 변화: +1 +1 -1 -1
        # idx2: gain 1, loss 0 -> 100
        # idx3: gain (1*1+0)/2=0.5, loss (0+1)/2=0.5 -> RS 1 -> 50
        # idx4: gain 0.25, loss (0.5+1)/2=0.75 -> RS 1/3 -> 25
        out = rsi([1, 2, 3, 2, 1], 2)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(100.0)
        assert out[3] == pytest.approx(50.0)
        assert out[4] == pytest.approx(25.0)

    def test_constant_series_is_50(self):
        out = rsi([10.0] * 40, 14)
        assert_warmup(out, 14)
        assert all(v == 50.0 for v in out[14:])

    def test_monotone_up_is_100(self):
        out = rsi([float(i) for i in range(40)], 14)
        assert_warmup(out, 14)
        assert all(v == pytest.approx(100.0) for v in out[14:])

    def test_monotone_down_is_0(self):
        out = rsi([float(40 - i) for i in range(40)], 14)
        assert_warmup(out, 14)
        assert all(v == pytest.approx(0.0) for v in out[14:])

    def test_range_0_100(self):
        closes = [c.close for c in random_candles(300)]
        out = rsi(closes, 14)
        assert_warmup(out, 14)
        assert all(0.0 <= v <= 100.0 for v in out[14:])

    def test_warmup_exact(self):
        assert rsi([1.0] * 14, 14) == [None] * 14
        out = rsi([1.0] * 15, 14)
        assert out[:14] == [None] * 14 and out[14] == 50.0

    def test_empty_and_single(self):
        assert rsi([], 14) == []
        assert rsi([5.0], 14) == [None]

    def test_matches_naive_wilder(self):
        closes = [c.close for c in random_candles(120, seed=3)]
        n = 14
        out = rsi(closes, n)
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]
        ag = sum(gains[:n]) / n
        al = sum(losses[:n]) / n
        expected = [None] * n + [100 - 100 / (1 + ag / al)]
        for i in range(n, len(deltas)):
            ag = (ag * (n - 1) + gains[i]) / n
            al = (al * (n - 1) + losses[i]) / n
            expected.append(100 - 100 / (1 + ag / al))
        for a, b in zip(out, expected):
            if b is None:
                assert a is None
            else:
                assert a == pytest.approx(b)


# ---------------------------------------------------------------- MACD
class TestMACD:
    def test_returns_three_lists_same_length(self):
        closes = [c.close for c in random_candles(100)]
        m, s, h = macd(closes)
        assert len(m) == len(s) == len(h) == 100

    def test_warmup_indices(self):
        closes = [c.close for c in random_candles(100)]
        m, s, h = macd(closes)
        assert_warmup(m, 25)   # slow-1
        assert_warmup(s, 33)   # slow-1 + signal-1
        assert_warmup(h, 33)

    def test_hist_equals_macd_minus_signal(self):
        closes = [c.close for c in random_candles(200, seed=11)]
        m, s, h = macd(closes)
        for i in range(33, 200):
            assert h[i] == pytest.approx(m[i] - s[i])

    def test_matches_ema_composition(self):
        closes = [c.close for c in random_candles(150, seed=5)]
        m, s, h = macd(closes)
        e12 = ema(closes, 12)
        e26 = ema(closes, 26)
        for i in range(25, 150):
            assert m[i] == pytest.approx(e12[i] - e26[i])
        # signal = EMA9(macd 선, 25부터 SMA 시드)
        mv = [m[i] for i in range(25, 150)]
        sig = ema(mv, 9)
        for j, i in enumerate(range(25, 150)):
            if sig[j] is None:
                assert s[i] is None
            else:
                assert s[i] == pytest.approx(sig[j])

    def test_constant_series_zero(self):
        m, s, h = macd([42.0] * 60)
        assert all(v == pytest.approx(0.0) for v in m[25:])
        assert all(v == pytest.approx(0.0) for v in s[33:])
        assert all(v == pytest.approx(0.0) for v in h[33:])

    def test_uptrend_positive(self):
        m, s, h = macd([float(i) for i in range(1, 101)])
        assert m[-1] > 0

    def test_too_short(self):
        m, s, h = macd([1.0] * 10)
        assert m == [None] * 10 and s == [None] * 10 and h == [None] * 10

    def test_empty(self):
        assert macd([]) == ([], [], [])

    def test_small_hand_params(self):
        # fast=1, slow=2, signal=1: macd = price - EMA2 ; signal = macd (EMA1)
        vals = [1.0, 2.0, 3.0]
        m, s, h = macd(vals, 1, 2, 1)
        e2 = ema(vals, 2)
        assert m[0] is None
        assert m[1] == pytest.approx(2.0 - e2[1])
        assert m[2] == pytest.approx(3.0 - e2[2])
        assert s[1] == pytest.approx(m[1]) and s[2] == pytest.approx(m[2])
        assert h[1] == pytest.approx(0.0) and h[2] == pytest.approx(0.0)

    def test_fast_must_be_less_than_slow(self):
        with pytest.raises(ValueError):
            macd([1.0] * 50, 26, 12, 9)


# ---------------------------------------------------------------- ATR
class TestATR:
    def test_hand_values_n2(self):
        candles = [
            Candle(ts=0, open=9, high=10, low=8, close=9, volume=1),
            Candle(ts=1, open=9, high=12, low=9, close=11, volume=1),       # TR=max(3,3,0)=3
            Candle(ts=2, open=11, high=11, low=10, close=10.5, volume=1),   # TR=max(1,0,1)=1
            Candle(ts=3, open=10.5, high=14, low=10, close=13, volume=1),   # TR=max(4,3.5,0.5)=4
        ]
        out = atr(candles, 2)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(2.0)          # (3+1)/2
        assert out[3] == pytest.approx(3.0)          # (2*1+4)/2

    def test_gap_uses_prev_close(self):
        # 갭 상승: 두 번째 봉 high-low 작지만 |high - prev_close| 큼
        candles = [
            Candle(ts=0, open=10, high=10, low=10, close=10, volume=1),
            Candle(ts=1, open=20, high=20.5, low=20, close=20, volume=1),  # TR = 10.5
        ]
        out = atr(candles, 1)
        assert out[1] == pytest.approx(10.5)

    def test_warmup_and_length(self):
        candles = random_candles(60)
        out = atr(candles, 14)
        assert len(out) == 60
        assert_warmup(out, 14)

    def test_constant_candles_zero(self):
        candles = series_candles([5.0] * 30)
        out = atr(candles, 14)
        assert all(v == 0.0 for v in out[14:])

    def test_constant_range(self):
        candles = series_candles([5.0] * 30, spread=1.0)  # TR = 2 매 봉
        out = atr(candles, 14)
        assert all(v == pytest.approx(2.0) for v in out[14:])

    def test_positive(self):
        out = atr(random_candles(200), 14)
        assert all(v > 0 for v in out[14:])

    def test_too_short_and_empty(self):
        assert atr(random_candles(14), 14) == [None] * 14
        assert atr([], 14) == []


# ---------------------------------------------------------------- Bollinger
class TestBollinger:
    def test_hand_values_n3(self):
        up, mid, lo, w = bollinger([1, 2, 3, 4, 5], 3, 2.0)
        std = math.sqrt(2.0 / 3.0)  # 모집단 표준편차 of (1,2,3)
        assert up[:2] == [None, None]
        assert mid[2] == pytest.approx(2.0)
        assert up[2] == pytest.approx(2.0 + 2 * std)
        assert lo[2] == pytest.approx(2.0 - 2 * std)
        assert w[2] == pytest.approx(4 * std / 2.0)
        assert mid[4] == pytest.approx(4.0)
        assert w[4] == pytest.approx(4 * std / 4.0)

    def test_constant_series_zero_width(self):
        up, mid, lo, w = bollinger([7.0] * 30, 20, 2.0)
        assert_warmup(up, 19)
        for i in range(19, 30):
            assert up[i] == 7.0 and lo[i] == 7.0 and mid[i] == 7.0 and w[i] == 0.0

    def test_middle_equals_sma(self):
        closes = [c.close for c in random_candles(80)]
        _, mid, _, _ = bollinger(closes, 20, 2.0)
        s = sma(closes, 20)
        for a, b in zip(mid, s):
            if b is None:
                assert a is None
            else:
                assert a == pytest.approx(b)

    def test_width_formula_and_ordering(self):
        closes = [c.close for c in random_candles(80, seed=9)]
        up, mid, lo, w = bollinger(closes, 20, 2.0)
        for i in range(19, 80):
            assert lo[i] <= mid[i] <= up[i]
            assert w[i] == pytest.approx((up[i] - lo[i]) / mid[i])
            assert w[i] >= 0

    def test_zero_middle_width_none(self):
        up, mid, lo, w = bollinger([-1.0, 0.0, 1.0], 3, 2.0)
        assert mid[2] == 0.0
        assert w[2] is None
        assert up[2] is not None

    def test_lengths_and_too_short(self):
        res = bollinger([1.0] * 10, 20, 2.0)
        assert all(len(r) == 10 and r == [None] * 10 for r in res)
        assert bollinger([], 20) == ([], [], [], [])

    def test_bad_k(self):
        with pytest.raises(ValueError):
            bollinger([1.0] * 30, 20, -1.0)
        with pytest.raises(ValueError):
            bollinger([1.0] * 30, 20, float("nan"))


# ---------------------------------------------------------------- ADX
class TestADX:
    def test_pure_uptrend_is_100(self):
        # h=10+i, l=9+i: +DM=1, -DM=0 매 봉 -> DX=100 -> ADX=100
        candles = [Candle(ts=i, open=9.5 + i, high=10 + i, low=9 + i, close=9.5 + i, volume=1) for i in range(40)]
        out = adx(candles, 14)
        assert_warmup(out, 27)
        assert all(v == pytest.approx(100.0) for v in out[27:])

    def test_pure_downtrend_is_100(self):
        candles = [Candle(ts=i, open=100 - i, high=100.5 - i, low=99.5 - i, close=100 - i, volume=1) for i in range(40)]
        out = adx(candles, 14)
        assert all(v == pytest.approx(100.0) for v in out[27:])

    def test_flat_is_zero(self):
        candles = series_candles([5.0] * 40, spread=1.0)  # DM 모두 0
        out = adx(candles, 14)
        assert_warmup(out, 27)
        assert all(v == 0.0 for v in out[27:])

    def test_constant_no_range_no_nan(self):
        candles = series_candles([5.0] * 40)  # TR 0 -> 0 나눗셈 보호
        out = adx(candles, 14)
        assert all(v == 0.0 for v in out[27:])

    def test_range_and_length(self):
        candles = random_candles(300, seed=21)
        out = adx(candles, 14)
        assert len(out) == 300
        assert_warmup(out, 27)
        assert all(0.0 <= v <= 100.0 for v in out[27:])

    def test_small_n_hand(self):
        # n=1: 평활 없음. DX_i = 100*|+DM - -DM|/(+DM + -DM) ; ADX(n=1) = DX (index 1 부터)
        candles = [
            Candle(ts=0, open=10, high=10, low=9, close=10, volume=1),
            Candle(ts=1, open=10, high=12, low=9.5, close=11, volume=1),  # +DM=2, -DM=0 -> DX 100
            Candle(ts=2, open=11, high=12, low=8, close=9, volume=1),     # +DM=0, -DM=1.5 -> DX 100
            Candle(ts=3, open=9, high=12, low=8, close=9, volume=1),      # +DM=0, -DM=0 -> DX 0
        ]
        out = adx(candles, 1)
        assert out[0] is None
        assert out[1] == pytest.approx(100.0)
        assert out[2] == pytest.approx(100.0)
        assert out[3] == pytest.approx(0.0)

    def test_too_short_and_empty(self):
        assert adx(random_candles(27), 14) == [None] * 27
        assert adx([], 14) == []

    def test_matches_naive_wilder(self):
        candles = random_candles(120, seed=33)
        n = 14
        out = adx(candles, n)
        L = len(candles)
        tr = [candles[0].high - candles[0].low]
        pdm = [0.0]
        mdm = [0.0]
        for i in range(1, L):
            c, p = candles[i], candles[i - 1]
            tr.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
            up, dn = c.high - p.high, p.low - c.low
            pdm.append(up if (up > dn and up > 0) else 0.0)
            mdm.append(dn if (dn > up and dn > 0) else 0.0)
        s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])

        def dx(t, p, m):
            pdi, mdi = 100 * p / t, 100 * m / t
            return 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0

        dxs = [dx(s_tr, s_p, s_m)]
        for i in range(n + 1, L):
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
            dxs.append(dx(s_tr, s_p, s_m))
        expected = [None] * (2 * n - 1)
        a = sum(dxs[:n]) / n
        expected.append(a)
        for j in range(n, len(dxs)):
            a = (a * (n - 1) + dxs[j]) / n
            expected.append(a)
        assert len(expected) == L
        for got, exp in zip(out, expected):
            if exp is None:
                assert got is None
            else:
                assert got == pytest.approx(exp)


# ---------------------------------------------------------------- VWAP
class TestVWAP:
    def test_hand_values_same_day(self):
        candles = [
            Candle(ts=0, open=10, high=11, low=9, close=10, volume=1),       # tp 10
            Candle(ts=TF_MS, open=20, high=21, low=19, close=20, volume=3),  # tp 20
        ]
        out = vwap(candles)
        assert out[0] == pytest.approx(10.0)
        assert out[1] == pytest.approx((10 * 1 + 20 * 3) / 4)  # 17.5

    def test_resets_on_utc_day_boundary(self):
        candles = [
            Candle(ts=DAY_MS - TF_MS, open=10, high=11, low=9, close=10, volume=5),  # 전날 마지막 봉
            Candle(ts=DAY_MS, open=30, high=31, low=29, close=30, volume=1),         # 새 날 00:00
            Candle(ts=DAY_MS + TF_MS, open=40, high=41, low=39, close=40, volume=1),
        ]
        out = vwap(candles)
        assert out[0] == pytest.approx(10.0)
        assert out[1] == pytest.approx(30.0)     # 리셋됨 (전날 봉 무시)
        assert out[2] == pytest.approx(35.0)

    def test_last_second_of_day_still_same_day(self):
        candles = [
            Candle(ts=0, open=10, high=10, low=10, close=10, volume=1),
            Candle(ts=DAY_MS - 1, open=20, high=20, low=20, close=20, volume=1),
        ]
        assert vwap(candles)[1] == pytest.approx(15.0)

    def test_zero_volume_none_then_value(self):
        candles = [
            Candle(ts=0, open=10, high=10, low=10, close=10, volume=0),
            Candle(ts=TF_MS, open=12, high=12, low=12, close=12, volume=2),
        ]
        out = vwap(candles)
        assert out[0] is None
        assert out[1] == pytest.approx(12.0)

    def test_length_and_empty(self):
        assert len(vwap(random_candles(50))) == 50
        assert vwap([]) == []

    def test_within_high_low_of_session(self):
        candles = random_candles(96, seed=2)  # 모두 같은 UTC 날 (96*15m = 24h, ts 0..)
        out = vwap(candles)
        lo = min(c.low for c in candles)
        hi = max(c.high for c in candles)
        assert all(lo <= v <= hi for v in out)

    def test_negative_ts_day_floor(self):
        # 음수 ts 도 바닥 나눗셈으로 날짜가 갈린다
        candles = [
            Candle(ts=-1, open=10, high=10, low=10, close=10, volume=1),
            Candle(ts=0, open=20, high=20, low=20, close=20, volume=1),
        ]
        out = vwap(candles)
        assert out[0] == 10.0 and out[1] == 20.0


# ---------------------------------------------------------------- compute()
class TestCompute:
    def test_empty_raises(self):
        with pytest.raises(ValueError):
            compute("BTC/USDT", "15m", [])

    def test_single_candle(self):
        c = Candle(ts=123, open=1, high=2, low=0.5, close=1.5, volume=3)
        out = compute("BTC/USDT", "15m", [c])
        assert isinstance(out, IndicatorSet)
        assert out.symbol == "BTC/USDT" and out.timeframe == "15m"
        assert out.ts == 123 and out.close == 1.5
        assert out.warmup_ok is False
        assert out.vwap_session == pytest.approx((2 + 0.5 + 1.5) / 3)
        for f in ("ema_20", "ema_50", "ema_200", "sma_20", "rsi_14", "macd", "macd_signal", "macd_hist",
                  "atr_14", "atr_pct", "bb_upper", "bb_lower", "bb_width_pct", "adx_14",
                  "volume_ratio_20", "return_1", "return_4", "return_24", "high_20", "low_20"):
            assert getattr(out, f) is None, f

    def test_two_candles_return_1_only(self):
        candles = series_candles([100.0, 110.0])
        out = compute("ETH/USDT", "1h", candles)
        assert out.return_1 == pytest.approx(0.10)
        assert out.return_4 is None and out.return_24 is None
        assert out.warmup_ok is False

    def test_returns(self):
        closes = [float(100 + i) for i in range(30)]  # 100..129
        out = compute("X/USDT", "15m", series_candles(closes))
        assert out.return_1 == pytest.approx(129 / 128 - 1)
        assert out.return_4 == pytest.approx(129 / 125 - 1)
        assert out.return_24 == pytest.approx(129 / 105 - 1)

    def test_return_24_needs_25_candles(self):
        c24 = series_candles([1.0 + i for i in range(24)])
        assert compute("X/USDT", "15m", c24).return_24 is None
        c25 = series_candles([1.0 + i for i in range(25)])
        assert compute("X/USDT", "15m", c25).return_24 == pytest.approx(25.0 / 1.0 - 1)

    def test_return_zero_base_is_none(self):
        candles = series_candles([0.0, 5.0])
        assert compute("X/USDT", "15m", candles).return_1 is None

    def test_high_low_20(self):
        candles = [mk_candle(i, 100.0, high=100 + i, low=100 - i) for i in range(25)]
        out = compute("X/USDT", "15m", candles)
        assert out.high_20 == pytest.approx(124.0)   # i=24
        assert out.low_20 == pytest.approx(100 - 24)
        # 19봉이면 None
        out19 = compute("X/USDT", "15m", candles[:19])
        assert out19.high_20 is None and out19.low_20 is None
        out20 = compute("X/USDT", "15m", candles[:20])
        assert out20.high_20 == pytest.approx(119.0) and out20.low_20 == pytest.approx(81.0)

    def test_volume_ratio_excludes_last(self):
        vols = [1.0] * 20 + [5.0]
        candles = [mk_candle(i, 10.0, volume=v) for i, v in enumerate(vols)]
        out = compute("X/USDT", "15m", candles)
        assert out.volume_ratio_20 == pytest.approx(5.0)
        # 20봉이면 None (이전 20봉이 없음)
        assert compute("X/USDT", "15m", candles[:20]).volume_ratio_20 is None

    def test_volume_ratio_uses_previous_20_only(self):
        vols = [100.0] * 5 + [2.0] * 20 + [4.0]
        candles = [mk_candle(i, 10.0, volume=v) for i, v in enumerate(vols)]
        assert compute("X/USDT", "15m", candles).volume_ratio_20 == pytest.approx(2.0)

    def test_volume_ratio_zero_mean_none(self):
        candles = [mk_candle(i, 10.0, volume=0.0) for i in range(21)]
        assert compute("X/USDT", "15m", candles).volume_ratio_20 is None

    @pytest.mark.parametrize("n,field", [
        (19, "ema_20"), (49, "ema_50"), (199, "ema_200"), (19, "sma_20"), (14, "rsi_14"),
        (34, "macd"), (34, "macd_signal"), (34, "macd_hist"), (14, "atr_14"), (14, "atr_pct"),
        (19, "bb_upper"), (19, "bb_lower"), (19, "bb_width_pct"), (27, "adx_14"),
        (20, "volume_ratio_20"), (19, "high_20"), (19, "low_20"),
    ])
    def test_field_none_below_warmup(self, n, field):
        out = compute("X/USDT", "15m", random_candles(n))
        assert getattr(out, field) is None

    @pytest.mark.parametrize("n,field", [
        (20, "ema_20"), (50, "ema_50"), (200, "ema_200"), (20, "sma_20"), (15, "rsi_14"),
        (35, "macd"), (35, "macd_signal"), (35, "macd_hist"), (15, "atr_14"), (15, "atr_pct"),
        (20, "bb_upper"), (20, "bb_lower"), (20, "bb_width_pct"), (28, "adx_14"),
        (21, "volume_ratio_20"), (20, "high_20"), (20, "low_20"),
    ])
    def test_field_present_at_warmup(self, n, field):
        out = compute("X/USDT", "15m", random_candles(n))
        assert isinstance(getattr(out, field), float)

    def test_warmup_ok_threshold(self):
        # 핵심: ema_20(20), rsi_14(15), atr_14(15), bb(20) -> 20봉부터 True
        assert compute("X/USDT", "15m", random_candles(19)).warmup_ok is False
        out20 = compute("X/USDT", "15m", random_candles(20))
        assert out20.warmup_ok is True
        assert out20.ema_50 is None and out20.macd is None  # 비핵심은 None 이어도 ok

    def test_full_series_all_fields(self):
        out = compute("BTC/USDT", "15m", random_candles(250))
        assert out.warmup_ok is True
        for name, value in out.model_dump().items():
            if name in ("symbol", "timeframe", "warmup_ok"):
                continue
            assert value is not None, name
            if isinstance(value, float):
                assert math.isfinite(value), name

    def test_values_match_helpers(self):
        candles = random_candles(250, seed=42)
        closes = [c.close for c in candles]
        out = compute("BTC/USDT", "15m", candles)
        assert out.ema_20 == ema(closes, 20)[-1]
        assert out.ema_50 == ema(closes, 50)[-1]
        assert out.ema_200 == ema(closes, 200)[-1]
        assert out.sma_20 == sma(closes, 20)[-1]
        assert out.rsi_14 == rsi(closes, 14)[-1]
        m, s, h = macd(closes)
        assert out.macd == m[-1] and out.macd_signal == s[-1] and out.macd_hist == h[-1]
        a = atr(candles, 14)[-1]
        assert out.atr_14 == a
        assert out.atr_pct == pytest.approx(a / closes[-1])
        up, _, lo, w = bollinger(closes, 20, 2.0)
        assert out.bb_upper == up[-1] and out.bb_lower == lo[-1] and out.bb_width_pct == w[-1]
        assert out.adx_14 == adx(candles, 14)[-1]
        assert out.vwap_session == vwap(candles)[-1]
        assert out.high_20 == max(c.high for c in candles[-20:])
        assert out.low_20 == min(c.low for c in candles[-20:])
        assert out.volume_ratio_20 == pytest.approx(candles[-1].volume / (sum(c.volume for c in candles[-21:-1]) / 20))

    def test_constant_series(self):
        candles = series_candles([50.0] * 250, volume=2.0)
        out = compute("X/USDT", "15m", candles)
        assert out.rsi_14 == 50.0
        assert out.ema_20 == pytest.approx(50.0) and out.ema_200 == pytest.approx(50.0)
        assert out.macd == pytest.approx(0.0) and out.macd_hist == pytest.approx(0.0)
        assert out.atr_14 == 0.0 and out.atr_pct == 0.0
        assert out.bb_width_pct == 0.0
        assert out.adx_14 == 0.0
        assert out.volume_ratio_20 == pytest.approx(1.0)
        assert out.return_1 == 0.0 and out.return_24 == 0.0
        assert out.high_20 == 50.0 and out.low_20 == 50.0
        assert out.warmup_ok is True

    def test_vwap_session_uses_last_candle_day(self):
        prev = [Candle(ts=DAY_MS - (3 - i) * TF_MS, open=10, high=10, low=10, close=10, volume=100) for i in range(3)]
        today = [Candle(ts=DAY_MS + i * TF_MS, open=20, high=20, low=20, close=20, volume=1) for i in range(2)]
        out = compute("X/USDT", "15m", prev + today)
        assert out.vwap_session == pytest.approx(20.0)

    def test_deterministic(self):
        candles = random_candles(300, seed=99)
        a = compute("BTC/USDT", "1h", candles)
        b = compute("BTC/USDT", "1h", [Candle(**c.model_dump()) for c in candles])
        assert a == b
        ja = json.dumps(a.model_dump(), sort_keys=True, ensure_ascii=False)
        jb = json.dumps(b.model_dump(), sort_keys=True, ensure_ascii=False)
        assert ja == jb

    def test_does_not_mutate_candles(self):
        candles = random_candles(60)
        before = [c.model_dump() for c in candles]
        compute("X/USDT", "15m", candles)
        assert [c.model_dump() for c in candles] == before

    def test_atr_pct_zero_close_none(self):
        candles = series_candles([1.0] * 19 + [0.0], spread=0.1)
        out = compute("X/USDT", "15m", candles)
        assert out.atr_14 is not None
        assert out.atr_pct is None

    def test_fast_enough(self):
        import time

        candles = random_candles(1000)
        t0 = time.perf_counter()
        compute("X/USDT", "15m", candles)
        assert time.perf_counter() - t0 < 1.0
