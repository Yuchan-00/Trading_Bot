"""backtest.py 테스트 (SPEC §13).

합성 캔들만 사용(오프라인). 고정하는 불변식:
- 선행편향 없음: 봉 t 신호는 봉 t+1 이후 데이터를 바꿔도 변하지 않는다. 체결은 봉 t+1 시가×(1+슬리피지).
- 손절/익절은 저가/고가 판정, 한 봉에서 둘 다 닿으면 손절 우선. 시간손절 96봉.
- 종목당 1포지션, 동시 최대 3. 자본은 종가 평가.
- 무작위 데이터는 NOT VALIDATED. 결과 카드(json+md)가 out_dir 에 저장된다.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from tradingbot import backtest as bt
from tradingbot.config import HardLimits
from tradingbot.models import Candle, ClosedTrade, IndicatorSet
from tradingbot.risk import clamp_stops, default_stops, enforce_net_rr

TF = 900_000  # 15m
H1 = 3_600_000
H4 = 14_400_000
SYM = "BTC/USDT"


# ---------------------------------------------------------------- 합성 캔들
def flat_series(n: int, price: float = 100.0, wick: float = 0.05, start_ts: int = 0) -> list[Candle]:
    """시가=종가=price, 고저 ±wick 인 평평한 봉."""
    return [
        Candle(ts=start_ts + i * TF, open=price, high=price + wick, low=price - wick, close=price, volume=100.0)
        for i in range(n)
    ]


def trend_series(
    n: int, *, drift: float = 0.0004, amp: float = 0.004, noise: float = 0.0015, seed: int = 7, start_ts: int = 0
) -> list[Candle]:
    """완만한 상승 추세 + 파동 + 작은 잡음. 거래량은 3봉마다 튄다(volume_ratio>1 구간을 만들기 위해)."""
    r = random.Random(seed)
    out: list[Candle] = []
    p = 100.0
    for i in range(n):
        o = p
        c = o * (1 + drift + amp * math.sin(i / 6.0) + r.gauss(0, noise))
        h = max(o, c) * (1 + abs(r.gauss(0, 0.0008)))
        lo = min(o, c) * (1 - abs(r.gauss(0, 0.0008)))
        v = 100 + (80 if i % 3 == 0 else 0) + r.random() * 20
        out.append(Candle(ts=start_ts + i * TF, open=o, high=h, low=lo, close=c, volume=v))
        p = c
    return out


def random_walk(n: int, seed: int = 123, start_ts: int = 0) -> list[Candle]:
    r = random.Random(seed)
    out: list[Candle] = []
    p = 100.0
    for i in range(n):
        o = p
        c = o * (1 + r.gauss(0, 0.004))
        h = max(o, c) * (1 + abs(r.gauss(0, 0.001)))
        lo = min(o, c) * (1 - abs(r.gauss(0, 0.001)))
        out.append(Candle(ts=start_ts + i * TF, open=o, high=h, low=lo, close=c, volume=100 + r.random() * 100))
        p = c
    return out


def with_higher(base: list[Candle], full: list[Candle] | None = None) -> dict[str, list[Candle]]:
    """기본 15m + (전체 이력에서 합성한) 1h/4h."""
    src = full if full is not None else base
    return {"15m": base, "1h": bt.resample(src, "1h"), "4h": bt.resample(src, "4h")}


def no_atr_higher(base: list[Candle]) -> dict[str, list[Candle]]:
    """1h/4h 를 2봉만 줘서 ATR 이 None(→ 기본 손절 1%/익절 2%)이 되게 한다."""
    t0 = base[0].ts
    h1 = [Candle(ts=t0 - 2 * H1 + k * H1, open=100, high=100.1, low=99.9, close=100, volume=1) for k in range(2)]
    h4 = [Candle(ts=t0 - 2 * H4 + k * H4, open=100, high=100.1, low=99.9, close=100, volume=1) for k in range(2)]
    return {"15m": base, "1h": h1, "4h": h4}


def replace(series: list[Candle], i: int, **fields) -> list[Candle]:
    out = list(series)
    out[i] = series[i].model_copy(update=fields)
    return out


def ind(**kw) -> IndicatorSet:
    base = dict(symbol=SYM, timeframe="15m", ts=0, close=100.0)
    base.update(kw)
    return IndicatorSet(**base)


def good_inputs() -> tuple[IndicatorSet, IndicatorSet, IndicatorSet]:
    i15 = ind(close=101.0, ema_20=100.0, rsi_14=55.0, macd_hist=0.1, volume_ratio_20=1.3, warmup_ok=True)
    i1h = ind(timeframe="1h", atr_14=1.0, atr_pct=0.01)
    i4h = ind(timeframe="4h", ema_50=100.0, ema_200=95.0)
    return i15, i1h, i4h


def buy_at(bars: set[int], conf: float = 0.7):
    """봉 인덱스(ts//TF)가 bars 에 들면 BUY 하는 가짜 규칙."""

    def rule(ind15, ind1h, ind4h):
        if ind15.ts // TF in bars:
            return "BUY", conf, "test_buy"
        return "HOLD", 0.0, "test_hold"

    return rule


def always_buy(ind15, ind1h, ind4h):
    return "BUY", 0.7, "always"


@pytest.fixture
def small_windows(monkeypatch):
    """가짜 규칙 테스트에서 지표 계산 비용을 줄인다."""
    monkeypatch.setattr(bt, "WINDOW_BARS", {"base": 30, "1h": 30, "4h": 30})


@pytest.fixture
def cfg(tmp_path: Path) -> bt.BacktestConfig:
    return bt.BacktestConfig(symbols=[SYM], out_dir=tmp_path / "out", cache_dir=tmp_path / "cache")


def expected_stops(entry: float, atr_1h: float | None = None) -> tuple[float, float]:
    sl, tp = default_stops(entry, atr_1h)
    sl, tp = clamp_stops(entry, sl, tp)
    sl, tp, note = enforce_net_rr(entry, sl, tp)
    assert "rr_impossible" not in note
    return sl, tp


# ================================================================ 설정/도우미
class TestConfigAndHelpers:
    def test_config_defaults_match_spec(self):
        c = bt.BacktestConfig(symbols=["BTC/USDT"])
        assert c.timeframe == "15m"
        assert c.days == 90
        assert c.fee_pct == 0.001
        assert c.slippage_pct == 0.0005
        assert c.start_balance == 10000.0
        assert c.cache_dir == Path("backtest_cache")
        assert c.out_dir == Path("backtest_results")
        d = c.to_dict()
        assert d["cache_dir"] == "backtest_cache" and d["symbols"] == ["BTC/USDT"]

    @pytest.mark.parametrize("tf,ms", [("15m", TF), ("1h", H1), ("4h", H4), ("1d", 86_400_000), ("1w", 604_800_000)])
    def test_timeframe_ms(self, tf, ms):
        assert bt.timeframe_ms(tf) == ms

    @pytest.mark.parametrize("bad", ["", "15", "m15", "0m", "1x", "abc"])
    def test_timeframe_ms_invalid(self, bad):
        with pytest.raises(ValueError):
            bt.timeframe_ms(bad)

    def test_resample_aggregates_ohlcv_and_drops_incomplete(self):
        base = [
            Candle(ts=0, open=10, high=12, low=9, close=11, volume=1),
            Candle(ts=TF, open=11, high=15, low=10, close=14, volume=2),
            Candle(ts=2 * TF, open=14, high=14, low=8, close=9, volume=3),
            Candle(ts=3 * TF, open=9, high=10, low=7, close=10, volume=4),
            Candle(ts=4 * TF, open=10, high=11, low=9, close=10.5, volume=5),  # 두 번째 버킷(미완)
        ]
        out = bt.resample(base, "1h")
        assert len(out) == 1
        c = out[0]
        assert (c.ts, c.open, c.high, c.low, c.close, c.volume) == (0, 10, 15, 7, 10, 10)

    def test_resample_complete_second_bucket(self):
        base = flat_series(8)
        out = bt.resample(base, "1h")
        assert [c.ts for c in out] == [0, H1]
        assert out[1].volume == 400.0

    def test_resample_empty_and_coarser_input(self):
        assert bt.resample([], "1h") == []
        h4 = [Candle(ts=k * H4, open=1, high=1, low=1, close=1, volume=1) for k in range(3)]
        with pytest.raises(ValueError):
            bt.resample(h4, "1h")

    def test_resample_unsorted_input_is_sorted(self):
        base = list(reversed(flat_series(4)))
        out = bt.resample(base, "1h")
        assert len(out) == 1 and out[0].ts == 0


# ================================================================ 규칙 신호
class TestRuleSignal:
    def test_all_conditions_buy(self):
        action, conf, reason = bt.rule_signal(*good_inputs())
        assert action == "BUY"
        assert conf == pytest.approx(0.7)
        assert "rule_buy" in reason

    def test_missing_higher_tf_hold(self):
        i15, i1h, i4h = good_inputs()
        assert bt.rule_signal(i15, None, i4h)[0] == "HOLD"
        assert bt.rule_signal(i15, i1h, None)[0] == "HOLD"
        assert bt.rule_signal(None, i1h, i4h)[0] == "HOLD"  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "which,field",
        [("15m", "ema_20"), ("15m", "rsi_14"), ("15m", "macd_hist"), ("15m", "volume_ratio_20"),
         ("1h", "atr_pct"), ("4h", "ema_50"), ("4h", "ema_200")],
    )
    def test_none_indicator_hold(self, which, field):
        i15, i1h, i4h = good_inputs()
        target = {"15m": i15, "1h": i1h, "4h": i4h}[which]
        patched = target.model_copy(update={field: None})
        args = {"15m": i15, "1h": i1h, "4h": i4h}
        args[which] = patched
        action, conf, reason = bt.rule_signal(args["15m"], args["1h"], args["4h"])
        assert action == "HOLD" and conf == 0.0
        assert field in reason

    def test_nan_indicator_hold(self):
        i15, i1h, i4h = good_inputs()
        bad = i15.model_copy(update={"rsi_14": float("nan")})
        assert bt.rule_signal(bad, i1h, i4h)[0] == "HOLD"

    @pytest.mark.parametrize(
        "which,field,value",
        [
            ("4h", "ema_50", 94.0),           # 추세 아님
            ("15m", "close", 99.0),           # 종가 ≤ ema20
            ("15m", "rsi_14", 44.9),          # rsi 하한 밖
            ("15m", "rsi_14", 65.1),          # rsi 상한 밖
            ("15m", "macd_hist", 0.0),        # macd_hist ≤ 0
            ("15m", "macd_hist", -0.1),
            ("15m", "volume_ratio_20", 1.0),  # 거래량비 ≤ 1
            ("1h", "atr_pct", 0.0029),        # atr 하한 밖
            ("1h", "atr_pct", 0.0301),        # atr 상한 밖
        ],
    )
    def test_each_condition_hold(self, which, field, value):
        i15, i1h, i4h = good_inputs()
        args = {"15m": i15, "1h": i1h, "4h": i4h}
        args[which] = args[which].model_copy(update={field: value})
        action, conf, reason = bt.rule_signal(args["15m"], args["1h"], args["4h"])
        assert action == "HOLD" and conf == 0.0 and reason

    def test_boundaries_inclusive(self):
        i15, i1h, i4h = good_inputs()
        assert bt.rule_signal(i15.model_copy(update={"rsi_14": 45.0}), i1h, i4h)[0] == "BUY"
        assert bt.rule_signal(i15.model_copy(update={"rsi_14": 65.0}), i1h, i4h)[0] == "BUY"
        assert bt.rule_signal(i15, i1h.model_copy(update={"atr_pct": 0.003}), i4h)[0] == "BUY"
        assert bt.rule_signal(i15, i1h.model_copy(update={"atr_pct": 0.03}), i4h)[0] == "BUY"


# ================================================================ 선행편향
class TestNoLookAhead:
    def test_future_base_candles_do_not_change_signal(self):
        full = trend_series(2200, seed=3)
        base = full[-500:]
        a = with_higher(base, full)
        t = 420
        sig_a = [bt.signal_at(a, i) for i in range(t - 5, t + 1)]
        # 봉 t+1 이후를 전부 망가뜨린다(종가 10배, 거래량 50배, 고저 뒤집기 수준의 변화)
        mutated = list(base)
        for j in range(t + 1, len(base)):
            c = base[j]
            mutated[j] = c.model_copy(update={"close": c.close * 10, "high": c.high * 20, "low": c.low * 0.1, "volume": c.volume * 50})
        b = {"15m": mutated, "1h": a["1h"], "4h": a["4h"]}
        sig_b = [bt.signal_at(b, i) for i in range(t - 5, t + 1)]
        assert sig_a == sig_b

    def test_changing_candle_t_plus_1_close_keeps_signal_at_t(self):
        full = trend_series(2200, seed=5)
        base = full[-400:]
        a = with_higher(base, full)
        for t in (250, 300, 398):
            b = {"15m": replace(base, t + 1, close=base[t + 1].close * 3), "1h": a["1h"], "4h": a["4h"]}
            assert bt.signal_at(a, t) == bt.signal_at(b, t)

    def test_signal_at_index_bounds(self):
        d = with_higher(flat_series(50))
        with pytest.raises(IndexError):
            bt.signal_at(d, 50)
        with pytest.raises(IndexError):
            bt.signal_at(d, -1)

    def test_higher_tf_uses_only_completed_candles(self):
        base = flat_series(20, start_ts=10 * H4)  # 기본 봉은 t=40h 부터
        h4 = [Candle(ts=k * H4, open=100, high=101, low=99, close=100, volume=1) for k in range(12)]  # 0..44h
        sd = bt._SymbolData(SYM, "15m", {"15m": base, "1h": [], "4h": h4})
        # 봉 0 종료 시각 = 40h15m → 완료된 4h 봉은 ts ≤ 36h(끝 40h)까지 → 마지막 ts = 9*H4
        i4 = sd.higher_indicators("4h", 0)
        assert i4 is not None and i4.ts == 9 * H4
        # 봉 16 종료 시각 = 44h15m → ts=10*H4 봉(끝 44h) 포함, ts=11*H4(끝 48h) 제외
        i4 = sd.higher_indicators("4h", 16)
        assert i4 is not None and i4.ts == 10 * H4
        # 아직 완료된 상위 봉이 없으면 None
        sd2 = bt._SymbolData(SYM, "15m", {"15m": flat_series(5), "1h": [], "4h": h4})
        assert sd2.higher_indicators("4h", 0) is None

    def test_engine_fill_independent_of_future(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(150)
        a = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        mutated = replace(base, 11, close=100.4, high=100.6, volume=900.0)
        mutated = replace(mutated, 12, close=90.0, low=89.0)
        b = bt.run(cfg, candles={SYM: no_atr_higher(mutated)})
        assert len(a.trades) == 1 and len(b.trades) == 1
        assert a.trades[0].reason == "time_stop" and b.trades[0].reason == "stop_loss"
        assert a.trades[0].entry_price == b.trades[0].entry_price
        assert a.trades[0].quote_spent == b.trades[0].quote_spent
        assert a.trades[0].opened_ts == b.trades[0].opened_ts == base[11].ts


# ================================================================ 체결/청산 엔진
class TestEngine:
    def test_fill_at_next_open_with_slippage(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = replace(flat_series(60), 11, open=101.0, high=101.3, low=100.6, close=101.0)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert len(res.trades) == 1
        t = res.trades[0]
        assert t.entry_price == pytest.approx(101.0 * (1 + cfg.slippage_pct))
        assert t.opened_ts == base[11].ts
        assert t.opened_cycle == 11
        assert t.base_amount == pytest.approx(t.quote_spent / t.entry_price)

    def test_stop_priority_when_both_touched(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(60)
        entry = base[11].open * (1 + cfg.slippage_pct)
        sl, tp = expected_stops(entry, None)
        assert sl < entry < tp
        base = replace(base, 12, low=sl * 0.999, high=tp * 1.001)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert len(res.trades) == 1
        t = res.trades[0]
        assert t.reason == "stop_loss"
        assert t.closed_cycle == 12
        assert t.exit_price == pytest.approx(sl * (1 - cfg.slippage_pct))
        assert t.pnl_quote < 0

    def test_gap_down_fills_at_open(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(60)
        entry = base[11].open * (1 + cfg.slippage_pct)
        sl, _tp = expected_stops(entry, None)
        gap_open = sl * 0.98
        base = replace(base, 12, open=gap_open, high=gap_open + 0.1, low=gap_open - 0.1, close=gap_open)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        t = res.trades[0]
        assert t.reason == "stop_loss"
        assert t.exit_price == pytest.approx(gap_open * (1 - cfg.slippage_pct))

    def test_take_profit_on_high(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(60)
        entry = base[11].open * (1 + cfg.slippage_pct)
        sl, tp = expected_stops(entry, None)
        base = replace(base, 13, high=tp * 1.001)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert len(res.trades) == 1
        t = res.trades[0]
        assert t.reason == "take_profit"
        assert t.closed_cycle == 13
        assert t.exit_price == pytest.approx(tp * (1 - cfg.slippage_pct))
        assert t.pnl_quote > 0
        # 손익 계산: 받은 금액 − (지출 + 매수 수수료)
        fee_buy = t.quote_spent * cfg.fee_pct
        proceeds = t.base_amount * t.exit_price
        assert t.quote_received == pytest.approx(proceeds * (1 - cfg.fee_pct))
        assert t.pnl_quote == pytest.approx(t.quote_received - t.quote_spent - fee_buy)
        assert t.fee_quote_total == pytest.approx(fee_buy + proceeds * cfg.fee_pct)
        assert t.pnl_pct == pytest.approx(t.pnl_quote / t.quote_spent)

    def test_exit_checked_on_entry_bar(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(60)
        entry = base[11].open * (1 + cfg.slippage_pct)
        sl, _ = expected_stops(entry, None)
        base = replace(base, 11, low=sl * 0.999)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        t = res.trades[0]
        assert t.reason == "stop_loss" and t.closed_cycle == 11

    def test_time_stop_after_96_bars(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(150)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert len(res.trades) == 1
        t = res.trades[0]
        assert t.reason == "time_stop"
        assert t.closed_cycle - t.opened_cycle == HardLimits.TIME_STOP_CYCLES == 96
        assert t.exit_price == pytest.approx(base[11 + 96].close * (1 - cfg.slippage_pct))

    def test_one_position_per_symbol_and_signal_waits(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        base = flat_series(260)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        # 시간손절(96봉) → 다음 봉 신호 → 그 다음 봉 체결: 포지션은 겹치지 않는다
        assert len(res.trades) >= 2
        for a, b in zip(res.trades, res.trades[1:]):
            assert b.opened_cycle > a.closed_cycle
        assert all(t.reason == "time_stop" for t in res.trades)

    def test_max_three_concurrent_positions(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        syms = [f"S{i}/USDT" for i in range(5)]
        cfg.symbols = syms
        base = flat_series(230)
        candles = {s: no_atr_higher(base) for s in syms}
        res = bt.run(cfg, candles=candles)
        assert res.trades
        # 어느 봉에서도 동시 보유 ≤ 3
        for k in range(230):
            open_now = sum(1 for t in res.trades if t.opened_cycle <= k <= t.closed_cycle)
            assert open_now <= HardLimits.MAX_POSITIONS
        assert max(sum(1 for t in res.trades if t.opened_cycle <= k <= t.closed_cycle) for k in range(230)) == 3
        assert res.metrics["skipped"]["max_positions"] >= 0

    def test_position_size_respects_hard_limits(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        base = flat_series(120)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        t = res.trades[0]
        assert 0 < t.quote_spent <= cfg.start_balance * HardLimits.MAX_POSITION_PCT + 1e-9
        # 확신도 0.7 → 상한의 40%
        assert t.quote_spent <= cfg.start_balance * HardLimits.MAX_POSITION_PCT * 0.40 + 1e-9
        assert t.confidence == pytest.approx(0.7)

    def test_equity_accounting_closed_positions(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10, 150}))
        base = flat_series(300)
        base = replace(base, 160, low=90.0)  # 두 번째 거래 손절
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert [t.reason for t in res.trades] == ["time_stop", "stop_loss"]
        final = res.equity_curve[-1][1]
        assert final == pytest.approx(cfg.start_balance + sum(t.pnl_quote for t in res.trades))
        assert res.metrics["final_equity"] == pytest.approx(final)
        assert res.metrics["net_pnl_quote"] == pytest.approx(sum(t.pnl_quote for t in res.trades))
        assert len(res.equity_curve) == 300
        assert [ts for ts, _ in res.equity_curve] == [c.ts for c in base]

    def test_equity_marked_at_close_while_open(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(40)
        base = replace(base, 20, close=100.5, high=100.6)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert not res.trades  # 아직 보유 중(40봉 < 96)
        eq_19 = res.equity_curve[19][1]
        eq_20 = res.equity_curve[20][1]
        quote = cfg.start_balance * 0.04  # 400 USDT 진입
        entry = 100.0 * (1 + cfg.slippage_pct)
        base_amt = quote / entry
        assert eq_20 - eq_19 == pytest.approx(base_amt * 0.5)

    def test_no_trades_on_hold_only(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        res = bt.run(cfg, candles={SYM: no_atr_higher(flat_series(50))})
        assert res.trades == []
        assert res.metrics["trades"] == 0 and res.metrics["win_rate"] == 0.0 and res.metrics["profit_factor"] == 0.0
        assert res.verdict == "NOT VALIDATED"
        assert all(eq == pytest.approx(cfg.start_balance) for _, eq in res.equity_curve)

    def test_deterministic(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        candles = {SYM: with_higher(random_walk(300, seed=9))}
        a = bt.run(cfg, candles=candles)
        b = bt.run(cfg, candles=candles)
        assert [t.model_dump() for t in a.trades] == [t.model_dump() for t in b.trades]
        assert a.equity_curve == b.equity_curve
        assert a.monte_carlo == b.monte_carlo

    def test_missing_higher_tf_is_resampled(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        res = bt.run(cfg, candles={SYM: {"15m": flat_series(120)}})
        assert len(res.trades) == 1


# ================================================================ 성과 지표
def _trade(pnl: float, spent: float = 400.0, opened: int = 0, closed: int = 1) -> ClosedTrade:
    return ClosedTrade(
        symbol=SYM, base_amount=4.0, entry_price=100.0, exit_price=100.0 + pnl / 4.0, quote_spent=spent,
        quote_received=spent + pnl, fee_quote_total=0.8, pnl_quote=pnl, pnl_pct=pnl / spent, reason="take_profit" if pnl > 0 else "stop_loss",
        opened_cycle=opened, closed_cycle=closed, opened_ts=opened * TF, closed_ts=closed * TF,
    )


class TestMetrics:
    def test_max_drawdown(self):
        assert bt.max_drawdown([]) == 0.0
        assert bt.max_drawdown([100, 110, 120]) == 0.0
        assert bt.max_drawdown([100, 80, 90, 120, 60]) == pytest.approx(0.5)
        assert bt.max_drawdown([100, 90, 95, 85]) == pytest.approx(0.15)

    def test_sharpe_from_curve(self):
        assert bt.sharpe_from_curve([]) == 0.0
        flat = [(d * 86_400_000, 100.0) for d in range(10)]
        assert bt.sharpe_from_curve(flat) == 0.0
        rising = [(d * 86_400_000, 100.0 * 1.01 ** d) for d in range(10)]
        # 일수익률이 거의 일정 → std≈0 → 0 또는 매우 큰 값. 부호는 양수여야 한다.
        assert bt.sharpe_from_curve(rising) >= 0.0
        mixed = [(d * 86_400_000, v) for d, v in enumerate([100, 102, 101, 104, 103, 106])]
        rets = [102 / 100 - 1, 101 / 102 - 1, 104 / 101 - 1, 103 / 104 - 1, 106 / 103 - 1]
        import statistics

        expected = statistics.fmean(rets) / statistics.stdev(rets) * math.sqrt(365)
        assert bt.sharpe_from_curve(mixed) == pytest.approx(expected)
        # 하루 안의 여러 봉은 마지막 값만 쓴다
        intraday = [(0, 100.0), (TF, 50.0), (2 * TF, 100.0), (86_400_000, 102.0), (2 * 86_400_000, 101.0), (3 * 86_400_000, 104.0)]
        assert bt.sharpe_from_curve(intraday) == bt.sharpe_from_curve([(0, 100.0), (86_400_000, 102.0), (2 * 86_400_000, 101.0), (3 * 86_400_000, 104.0)])

    def test_compute_metrics(self):
        trades = [_trade(10, closed=1), _trade(-5, closed=2), _trade(20, closed=3), _trade(-10, closed=4)]
        curve = [(0, 10000.0), (TF, 10010.0), (2 * TF, 10005.0), (3 * TF, 10025.0), (4 * TF, 10015.0)]
        m = bt.compute_metrics(trades, curve, 10000.0)
        assert m["trades"] == 4 and m["wins"] == 2 and m["losses"] == 2
        assert m["win_rate"] == pytest.approx(0.5)
        assert m["profit_factor"] == pytest.approx(30 / 15)
        assert m["net_return"] == pytest.approx(0.0015)
        assert m["max_drawdown"] == pytest.approx(10 / 10025)
        assert m["avg_bars_held"] == pytest.approx((1 + 2 + 3 + 4) / 4)
        assert m["exit_reasons"] == {"stop_loss": 2, "take_profit": 2}
        assert m["total_fees_quote"] == pytest.approx(3.2)

    def test_profit_factor_edge_cases(self):
        assert bt._profit_factor([]) == 0.0
        assert bt._profit_factor([_trade(5), _trade(3)]) == bt.PF_CAP
        assert bt._profit_factor([_trade(-5)]) == 0.0
        m = bt.compute_metrics([], [], 10000.0)
        assert m["final_equity"] == 10000.0 and m["net_return"] == 0.0 and m["sharpe_daily"] == 0.0

    def test_monte_carlo_seeded_and_percentiles(self):
        rets = [0.01, -0.005, 0.02, -0.01, 0.015, -0.002, 0.008, -0.012]
        a = bt.monte_carlo(rets, iterations=200)
        b = bt.monte_carlo(rets, iterations=200)
        assert a == b
        assert a["iterations"] == 200 and a["trades"] == 8 and a["seed"] == bt.MC_SEED
        assert a["return_p5"] <= a["return_p50"]
        assert a["mdd_p50"] <= a["mdd_p95"]
        # 순서만 바뀌므로 최종 수익률은 모든 반복에서 같다(복리 곱은 가환)
        total = 1.0
        for r in rets:
            total *= 1 + r
        assert a["return_p5"] == pytest.approx(total - 1) and a["return_p50"] == pytest.approx(total - 1)
        assert a["mdd_p95"] > 0.0
        c = bt.monte_carlo(rets, iterations=200, seed=1)
        assert c["mdd_p95"] >= 0.0

    def test_monte_carlo_empty_and_all_positive(self):
        e = bt.monte_carlo([])
        assert e["iterations"] == 0 and e["trades"] == 0 and e["mdd_p95"] == 0.0 and e["return_p5"] == 0.0
        p = bt.monte_carlo([0.01, 0.02, 0.03], iterations=50)
        assert p["mdd_p95"] == 0.0 and p["return_p5"] > 0
        n = bt.monte_carlo([float("nan"), 0.01], iterations=10)
        assert n["trades"] == 1

    def test_percentile(self):
        assert bt._percentile([], 0.5) == 0.0
        assert bt._percentile([3.0], 0.95) == 3.0
        assert bt._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
        assert bt._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.05) == pytest.approx(1.2)
        assert bt._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == pytest.approx(4.8)


# ================================================================ 워크포워드/판정/스트레스
class TestWalkForwardAndVerdict:
    def test_segments_partition_timeline_and_trades(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        base = trend_series(400, drift=0.002, amp=0.0, noise=0.0, seed=1)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        segs = res.walk_forward["segments"]
        assert len(segs) == 3
        assert sum(s["bars"] for s in segs) == 400
        assert sum(s["trades"] for s in segs) == len(res.trades)
        assert segs[0]["start_ts"] == base[0].ts and segs[-1]["end_ts"] == base[-1].ts
        assert segs[0]["start_equity"] == pytest.approx(cfg.start_balance)
        for a, b in zip(segs, segs[1:]):
            assert b["start_equity"] == pytest.approx(a["end_equity"])
            assert b["start_ts"] > a["end_ts"]
        assert segs[-1]["end_equity"] == pytest.approx(res.equity_curve[-1][1])

    def test_validated_on_steady_uptrend(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        base = trend_series(1000, drift=0.002, amp=0.0, noise=0.0, seed=1)  # ≈ 10.4 일
        cfg.days = 10  # 요청 기간이 데이터 기간과 맞아야 VALIDATED 가능(data_coverage 사유 방지)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert res.metrics["covered_days"] >= cfg.days * bt.VERDICT_MIN_COVERAGE_RATIO
        assert res.metrics["trades"] >= 30
        assert all(t.reason == "take_profit" for t in res.trades)
        assert res.verdict == "VALIDATED"
        assert res.reasons == []
        assert res.walk_forward["all_pf_above_threshold"] is True
        assert res.stress["profit_factor"] > 1.0
        assert res.metrics["max_drawdown"] < 0.10

    def test_not_validated_on_random_data(self, cfg):
        full = random_walk(1500, seed=11)
        base = full[-400:]
        res = bt.run(cfg, candles={SYM: with_higher(base, full)})
        assert res.verdict == "NOT VALIDATED"
        assert res.reasons
        if res.metrics["trades"] < bt.VERDICT_MIN_TRADES:
            assert any("too_few_trades" in r for r in res.reasons)

    def test_not_validated_too_few_trades_reason(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        res = bt.run(cfg, candles={SYM: no_atr_higher(flat_series(150))})
        assert res.verdict == "NOT VALIDATED"
        assert any("too_few_trades" in r for r in res.reasons)

    def test_verdict_rules_directly(self):
        seg = lambda pf, i=0: bt.SegmentResult(i, 0, 1, 10, 12, 8, 4, 0.66, pf, 0.01, 0.01, 100.0, 101.0)  # noqa: E731
        good_m = {"trades": 40, "max_drawdown": 0.05}
        v, r = bt._verdict(good_m, [seg(1.5, 0), seg(1.2, 1), seg(1.11, 2)], {"profit_factor": 1.05})
        assert v == "VALIDATED" and r == []
        v, r = bt._verdict({"trades": 29, "max_drawdown": 0.05}, [seg(1.5, 0), seg(1.2, 1), seg(1.11, 2)], {"profit_factor": 1.05})
        assert v == "NOT VALIDATED" and any("too_few_trades" in x for x in r)
        v, r = bt._verdict(good_m, [seg(1.5, 0), seg(1.1, 1), seg(1.3, 2)], {"profit_factor": 1.05})
        assert v == "NOT VALIDATED" and any("segment_pf" in x and "구간 2" in x for x in r)
        v, r = bt._verdict({"trades": 40, "max_drawdown": 0.10}, [seg(1.5, 0), seg(1.2, 1), seg(1.3, 2)], {"profit_factor": 1.05})
        assert v == "NOT VALIDATED" and any("(mdd)" in x for x in r)
        v, r = bt._verdict(good_m, [seg(1.5, 0), seg(1.2, 1), seg(1.3, 2)], {"profit_factor": 1.0})
        assert v == "NOT VALIDATED" and any("stress_pf" in x for x in r)
        v, r = bt._verdict(good_m, [seg(1.5, 0), seg(1.2, 1)], {"profit_factor": 1.5})
        assert v == "NOT VALIDATED" and any("too_few_segments" in x for x in r)

    def test_stress_doubles_costs(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", always_buy)
        base = trend_series(400, drift=0.002, amp=0.0, noise=0.0, seed=1)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert res.stress["fee_pct"] == pytest.approx(cfg.fee_pct * 2)
        assert res.stress["slippage_pct"] == pytest.approx(cfg.slippage_pct * 2)
        assert res.stress["trades"] > 0
        assert res.stress["net_return"] < res.metrics["net_return"]
        assert res.stress["profit_factor"] <= res.metrics["profit_factor"]

    def test_buy_and_hold(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        base = trend_series(200, drift=0.001, amp=0.0, noise=0.0, seed=1)
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        buy = base[0].open * (1 + cfg.slippage_pct) * (1 + cfg.fee_pct)
        sell = base[-1].close * (1 - cfg.slippage_pct) * (1 - cfg.fee_pct)
        assert res.buy_and_hold[SYM] == pytest.approx(sell / buy - 1)
        assert res.buy_and_hold[SYM] > 0
        assert res.metrics["vs_buy_and_hold_best"] == pytest.approx(0.0 - res.buy_and_hold[SYM])


# ================================================================ 결과 저장/조회
class TestRunCardAndFetch:
    def test_run_card_files_written(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        res = bt.run(cfg, candles={SYM: no_atr_higher(flat_series(150))})
        assert res.json_path is not None and res.md_path is not None
        assert res.json_path.parent == cfg.out_dir and res.json_path.name.startswith("run_")
        assert res.json_path.suffix == ".json" and res.md_path.suffix == ".md"
        assert res.md_path.exists()
        raw = res.json_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data["verdict"] == "NOT VALIDATED"
        assert data["metrics"]["trades"] == 1
        assert len(data["trades"]) == 1 and data["trades"][0]["reason"] == "time_stop"
        # 대시보드가 읽는 키들
        for key in ("metrics", "stress", "monte_carlo", "walk_forward", "buy_and_hold", "trades", "config"):
            assert key in data
        # 결정적 직렬화(sort_keys)
        assert raw == json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2)
        md = res.md_path.read_text(encoding="utf-8")
        assert "NOT VALIDATED" in md and "too_few_trades" in md and "워크포워드" in md
        assert "NaN" not in raw and "Infinity" not in raw

    def test_run_card_unique_names(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        monkeypatch.setattr(bt.time, "time", lambda: 1700000000.0)
        candles = {SYM: no_atr_higher(flat_series(40))}
        a = bt.run(cfg, candles=candles)
        b = bt.run(cfg, candles=candles)
        assert a.json_path != b.json_path and a.json_path.exists() and b.json_path.exists()
        assert a.run_ts == b.run_ts == 1700000000000
        assert len(list(cfg.out_dir.glob("run_*.json"))) == 2

    def test_to_dict_roundtrip_json(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        res = bt.run(cfg, candles={SYM: no_atr_higher(flat_series(150))})
        d = res.to_dict()
        json.dumps(d, sort_keys=True, ensure_ascii=False, allow_nan=False)
        assert d["equity_curve"][0] == [0, pytest.approx(cfg.start_balance)]

    def test_fetch_with_cache(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        cfg.days = 1
        calls: list[tuple[str, str, int]] = []
        base = flat_series(200)

        class FakeMarket:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                calls.append((symbol, timeframe, limit))
                if timeframe == "15m":
                    return base[-limit:]
                return bt.resample(base, timeframe)

            def fetch_ticker(self, symbol):  # pragma: no cover - 프로토콜 형식만
                raise NotImplementedError

            def fetch_orderbook_summary(self, symbol, depth=50):  # pragma: no cover
                raise NotImplementedError

            def fetch_futures_context(self, symbol):  # pragma: no cover
                return None

            def server_time_ms(self):  # pragma: no cover
                return 0

        res = bt.run(cfg, market=FakeMarket())
        assert res.metrics["bars"] > 0
        tfs = sorted(tf for _, tf, _ in calls)
        assert tfs == ["15m", "1h", "4h"]
        limit_15m = next(lim for _, tf, lim in calls if tf == "15m")
        assert limit_15m == 1 * 96 + bt.WINDOW_BARS["base"]
        cache_files = sorted(p.name for p in cfg.cache_dir.glob("*.json"))
        assert len(cache_files) == 3 and all(n.startswith("BTC_USDT_") for n in cache_files)
        # 두 번째 실행은 캐시만 사용
        calls.clear()
        res2 = bt.run(cfg, market=FakeMarket())
        assert calls == []
        assert res2.metrics["bars"] == res.metrics["bars"]

    def test_corrupt_cache_is_refetched(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        cfg.days = 1
        cfg.cache_dir.mkdir(parents=True)
        base = flat_series(120)
        calls = []

        class M:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                calls.append(timeframe)
                return base if timeframe == "15m" else bt.resample(base, timeframe)

        limit = 96 + bt.WINDOW_BARS["base"]
        (cfg.cache_dir / bt._cache_name(SYM, "15m", limit)).write_text("{not json", encoding="utf-8")
        bt.run(cfg, market=M())
        assert "15m" in calls

    def test_errors(self, cfg):
        with pytest.raises(ValueError):
            bt.run(bt.BacktestConfig(symbols=[], out_dir=cfg.out_dir), candles={})
        with pytest.raises(ValueError):
            bt.run(cfg)  # market 도 candles 도 없음
        with pytest.raises(ValueError):
            bt.run(cfg, candles={"ETH/USDT": no_atr_higher(flat_series(10))})
        with pytest.raises(ValueError):
            bt.run(cfg, candles={SYM: {"1h": flat_series(10)}})  # 기본 시간대 없음
        bad = bt.BacktestConfig(symbols=[SYM], start_balance=0.0, out_dir=cfg.out_dir)
        with pytest.raises(ValueError):
            bt.run(bad, candles={SYM: no_atr_higher(flat_series(10))})
        bad2 = bt.BacktestConfig(symbols=[SYM], fee_pct=-0.1, out_dir=cfg.out_dir)
        with pytest.raises(ValueError):
            bt.run(bad2, candles={SYM: no_atr_higher(flat_series(10))})

    def test_days_trims_timeline(self, cfg, small_windows, monkeypatch):
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        cfg.days = 1
        base = flat_series(96 * 3)  # 3일
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert res.metrics["bars"] == 96
        assert res.equity_curve[0][0] == base[-96].ts


# ================================================================ 실제 규칙 통합
class TestIntegrationRealRule:
    def test_trending_series_trades_and_invariants(self, cfg):
        full = trend_series(4500, seed=7)
        base = full[-600:]
        res = bt.run(cfg, candles={SYM: with_higher(base, full)})
        assert len(res.trades) >= 3
        for t in res.trades:
            assert t.reason in ("stop_loss", "take_profit", "time_stop")
            assert t.closed_cycle > t.opened_cycle or t.closed_cycle == t.opened_cycle
            assert t.closed_cycle - t.opened_cycle <= HardLimits.TIME_STOP_CYCLES
            assert 0 < t.quote_spent <= cfg.start_balance * HardLimits.MAX_POSITION_PCT * 1.5
            assert t.thesis and "rule_buy" in t.thesis
            # 손절 거리 ≤ 2% + 슬리피지, 익절 ≤ 4%: 손실은 -2.3% 보다 나쁘지 않고 이익은 4% 이하
            assert t.pnl_pct > -(HardLimits.MAX_STOP_LOSS_PCT + 0.004)
            assert t.pnl_pct < HardLimits.MAX_TAKE_PROFIT_PCT
        assert res.metrics["bars"] == 600
        assert res.metrics["trades"] == len(res.trades)
        assert res.monte_carlo["trades"] == len(res.trades)
        assert res.json_path.exists()
        md = res.md_path.read_text(encoding="utf-8")
        assert res.verdict in md

    def test_signal_memoized_across_stress_run(self, cfg, monkeypatch):
        """비용 2배 스트레스는 지표/신호를 다시 계산하지 않는다(같은 데이터 객체 재사용)."""
        full = trend_series(2000, seed=2)
        base = full[-200:]
        calls = {"n": 0}
        orig = bt.indicators.compute

        def counting(symbol, timeframe, candles):
            calls["n"] += 1
            return orig(symbol, timeframe, candles)

        monkeypatch.setattr(bt.indicators, "compute", counting)
        bt.run(cfg, candles={SYM: with_higher(base, full)})
        # 기본 200봉 + 1h(최대 50) + 4h(최대 13) ≤ 263 — 스트레스 재실행이 추가 계산을 만들면 2배가 된다
        assert calls["n"] <= 200 + 50 + 13 + 5
