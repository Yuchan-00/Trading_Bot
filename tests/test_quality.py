"""tradingbot.quality 테스트 (SPEC §6). 전부 오프라인·결정적."""
from __future__ import annotations

import math

import pytest

from tradingbot import quality
from tradingbot.models import Candle, QualityReport, Ticker

NOW = 1_700_000_000_000  # 고정 now (epoch ms)
TF15 = 900_000
TF1H = 3_600_000
TF4H = 14_400_000
TF1D = 86_400_000
SYMBOL = "BTC/USDT"


def make_candles(tf_ms: int, n: int, *, now_ms: int = NOW, price: float = 100.0, close_lag: int = 0) -> list[Candle]:
    """now 직전에 완료된 봉 n개(오름차순). close_lag 봉만큼 더 오래된 시리즈로 만든다."""
    last_close = (now_ms // tf_ms) * tf_ms - close_lag * tf_ms   # 마지막 완료 봉의 종료 시각
    last_ts = last_close - tf_ms
    out = []
    for i in range(n - 1, -1, -1):
        ts = last_ts - i * tf_ms
        out.append(Candle(ts=ts, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=10.0))
    return out


def good_ticker(**kw) -> Ticker:
    base = dict(symbol=SYMBOL, last=100.0, bid=99.99, ask=100.01, quote_volume_24h=1e9, change_24h_pct=1.0, ts=NOW)
    base.update(kw)
    return Ticker(**base)


def full_set() -> dict[str, list[Candle]]:
    return {
        "15m": make_candles(TF15, 230),
        "1h": make_candles(TF1H, 70),
        "4h": make_candles(TF4H, 70),
        "1d": make_candles(TF1D, 40),
    }


def check(candles=None, ticker=None, now_ms=NOW, symbol=SYMBOL, cycle_minutes=15) -> QualityReport:
    return quality.check(symbol, candles if candles is not None else full_set(), ticker or good_ticker(), now_ms, cycle_minutes)


# ---------------------------------------------------------------- 기본
def test_clean_data_is_ok():
    rep = check()
    assert isinstance(rep, QualityReport)
    assert rep.ok is True
    assert rep.reasons == []
    assert rep.stale_seconds is None
    assert rep.symbol == SYMBOL


def test_only_15m_is_ok_when_enough():
    rep = check({"15m": make_candles(TF15, 220)})
    assert rep.ok, rep.reasons


def test_timeframe_seconds_map():
    assert quality.TF_SECONDS == {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def test_deterministic():
    a = check()
    b = check()
    assert a == b
    assert a.model_dump() == b.model_dump()


def test_does_not_mutate_input():
    cs = full_set()
    copy = {k: list(v) for k, v in cs.items()}
    check(cs)
    assert cs == copy


# ---------------------------------------------------------------- too_few
@pytest.mark.parametrize("tf,tf_ms,minimum", [("15m", TF15, 220), ("1h", TF1H, 60), ("4h", TF4H, 60), ("1d", TF1D, 30)])
def test_too_few_per_timeframe(tf, tf_ms, minimum):
    cs = full_set()
    cs[tf] = make_candles(tf_ms, minimum - 1)
    rep = check(cs)
    assert not rep.ok
    assert f"too_few_{tf}:{minimum - 1}<{minimum}" in rep.reasons


@pytest.mark.parametrize("tf,tf_ms,minimum", [("15m", TF15, 220), ("1h", TF1H, 60), ("4h", TF4H, 60), ("1d", TF1D, 30)])
def test_exactly_minimum_is_enough(tf, tf_ms, minimum):
    cs = full_set()
    cs[tf] = make_candles(tf_ms, minimum)
    rep = check(cs)
    assert not any(r.startswith("too_few") for r in rep.reasons), rep.reasons


def test_empty_candle_list():
    rep = check({"15m": []})
    assert not rep.ok
    assert "too_few_15m:0<220" in rep.reasons
    # 빈 리스트에서 stale/gap 등으로 예외가 나면 안 된다
    assert rep.stale_seconds is None


def test_missing_optional_timeframes_not_required():
    rep = check({"15m": make_candles(TF15, 230), "1h": make_candles(TF1H, 60)})
    assert rep.ok, rep.reasons


def test_unknown_timeframe_reported_but_no_crash():
    cs = full_set()
    cs["2w"] = make_candles(14 * TF1D, 5)
    rep = check(cs)
    assert "unknown_tf_2w" in rep.reasons
    assert not rep.ok


def test_short_tf_1m_5m_no_minimum_but_checked_for_staleness():
    cs = full_set()
    cs["1m"] = make_candles(60_000, 5)
    cs["5m"] = make_candles(300_000, 5)
    assert check(cs).ok
    cs["1m"] = make_candles(60_000, 5, close_lag=3)
    rep = check(cs)
    assert any(r.startswith("stale_1m:") for r in rep.reasons)


# ---------------------------------------------------------------- stale
def test_stale_15m_exact_threshold_boundary():
    # close_lag=2 -> 종료 = now_aligned - 2*tf : "더 오래됨"이 아니라 같음 -> 통과 (now가 정렬된 경우)
    now = (NOW // TF15) * TF15
    cs = {"15m": make_candles(TF15, 230, now_ms=now, close_lag=2)}
    assert check(cs, now_ms=now).ok
    # 1ms 더 지나면 stale
    rep = check(cs, now_ms=now + 1)
    assert not rep.ok
    assert any(r.startswith("stale_15m:") for r in rep.reasons)


def test_stale_reason_format_and_stale_seconds():
    now = (NOW // TF15) * TF15
    cs = {"15m": make_candles(TF15, 230, now_ms=now, close_lag=4)}  # 종료가 now-4tf -> 지연 3600s
    rep = check(cs, now_ms=now)
    assert "stale_15m:3600s" in rep.reasons
    assert rep.stale_seconds == 3600


def test_stale_seconds_is_max_over_timeframes():
    now = (NOW // TF1H) * TF1H
    cs = {
        "15m": make_candles(TF15, 230, now_ms=now, close_lag=4),   # 3600s
        "1h": make_candles(TF1H, 70, now_ms=now, close_lag=5),     # 5h = 18000s
    }
    rep = check(cs, now_ms=now)
    assert rep.stale_seconds == 18_000
    assert "stale_15m:3600s" in rep.reasons
    assert "stale_1h:18000s" in rep.reasons


def test_last_1d_candle_closed_yesterday_is_fresh():
    """일봉은 마지막 완료 봉이 최대 24h 전 종료 -> 2*tf 안이면 정상."""
    now = (NOW // TF1D) * TF1D + TF1D - 1  # 하루의 거의 끝
    cs = {"15m": make_candles(TF15, 230, now_ms=now), "1d": make_candles(TF1D, 40, now_ms=now)}
    assert check(cs, ticker=good_ticker(ts=now), now_ms=now).ok


def test_future_candle_flagged():
    cs = full_set()
    cs["15m"] = cs["15m"] + [Candle(ts=NOW + 5 * TF15, open=100, high=101, low=99, close=100, volume=1)]
    rep = check(cs)
    assert "future_15m" in rep.reasons


# ---------------------------------------------------------------- gap / 정렬
def test_gap_single_missing_candle():
    cs = full_set()
    c = cs["15m"]
    cs["15m"] = c[:100] + c[101:]  # 하나 누락
    rep = check(cs)
    assert "gap_15m:1" in rep.reasons
    assert not rep.ok


def test_gap_counts_multiple_missing():
    cs = full_set()
    c = cs["15m"]
    cs["15m"] = c[:50] + c[53:120] + c[122:]  # 3개 + 2개 누락
    rep = check(cs)
    assert "gap_15m:5" in rep.reasons


def test_gap_in_1h_only():
    cs = full_set()
    c = cs["1h"]
    cs["1h"] = c[:10] + c[12:]
    rep = check(cs)
    assert "gap_1h:2" in rep.reasons
    assert not any(r.startswith("gap_15m") for r in rep.reasons)


def test_duplicate_ts_is_unsorted():
    cs = full_set()
    c = cs["15m"]
    cs["15m"] = c[:100] + [c[99]] + c[100:]
    rep = check(cs)
    assert "unsorted_15m" in rep.reasons


def test_descending_order_is_unsorted():
    cs = full_set()
    cs["15m"] = list(reversed(cs["15m"]))
    rep = check(cs)
    assert "unsorted_15m" in rep.reasons
    # 최신 봉 기준으로 stale은 아니어야 한다
    assert not any(r.startswith("stale_15m") for r in rep.reasons)


# ---------------------------------------------------------------- nan / non_positive
@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_nan_in_any_field(field):
    cs = full_set()
    c = cs["15m"][50]
    cs["15m"][50] = c.model_copy(update={field: float("nan")})
    rep = check(cs)
    assert "nan" in rep.reasons
    assert not rep.ok


def test_inf_is_nan_reason():
    cs = full_set()
    cs["1h"][3] = cs["1h"][3].model_copy(update={"close": float("inf")})
    assert "nan" in check(cs).reasons


@pytest.mark.parametrize("field,value", [("close", 0.0), ("open", -1.0), ("low", 0.0), ("high", -0.5), ("volume", -1.0)])
def test_non_positive(field, value):
    cs = full_set()
    cs["15m"][10] = cs["15m"][10].model_copy(update={field: value})
    rep = check(cs)
    assert "non_positive" in rep.reasons


def test_zero_volume_is_allowed():
    cs = full_set()
    cs["15m"][10] = cs["15m"][10].model_copy(update={"volume": 0.0})
    assert check(cs).ok


def test_nan_reason_deduplicated_across_timeframes():
    cs = full_set()
    cs["15m"][1] = cs["15m"][1].model_copy(update={"close": float("nan")})
    cs["1h"][1] = cs["1h"][1].model_copy(update={"close": float("nan")})
    rep = check(cs, ticker=good_ticker(last=float("nan")))
    assert rep.reasons.count("nan") == 1


def test_nan_close_does_not_trigger_outlier_or_crash():
    cs = full_set()
    cs["15m"][100] = cs["15m"][100].model_copy(update={"close": float("nan")})
    rep = check(cs)
    assert "nan" in rep.reasons
    assert "outlier_15m" not in rep.reasons


# ---------------------------------------------------------------- outlier
def test_outlier_above_30pct():
    cs = full_set()
    cs["15m"][100] = cs["15m"][100].model_copy(update={"close": 131.0})  # +31%
    rep = check(cs)
    assert "outlier_15m" in rep.reasons


def test_outlier_down_move():
    cs = full_set()
    cs["4h"][5] = cs["4h"][5].model_copy(update={"close": 69.0})  # -31%
    rep = check(cs)
    assert "outlier_4h" in rep.reasons
    assert "outlier_15m" not in rep.reasons


def test_exactly_30pct_is_not_outlier():
    """정확히 +30% (100 -> 130)는 경계 포함이 아니므로 통과해야 한다 (부동소수 오차 포함)."""
    cs = full_set()
    cs["15m"][100] = cs["15m"][100].model_copy(update={"close": 130.0})
    cs["15m"][101] = cs["15m"][101].model_copy(update={"close": 130.0})
    cs["15m"][102] = cs["15m"][102].model_copy(update={"close": 100.0})  # 130 -> 100 은 -23%
    rep = check(cs)
    assert "outlier_15m" not in rep.reasons
    # 130 -> 91 은 -30% 정확히 -> 통과, 130 -> 90.9 는 초과
    cs["15m"][102] = cs["15m"][102].model_copy(update={"close": 91.0})
    cs["15m"][103] = cs["15m"][103].model_copy(update={"close": 91.0})
    assert "outlier_15m" not in check(cs).reasons
    cs["15m"][102] = cs["15m"][102].model_copy(update={"close": 90.9})
    assert "outlier_15m" in check(cs).reasons


def test_normal_volatility_is_not_outlier():
    cs = full_set()
    price = 100.0
    out = []
    for i, c in enumerate(cs["15m"]):
        price *= 1.05 if i % 2 == 0 else 0.96
        out.append(c.model_copy(update={"close": price, "open": price, "high": price * 1.01, "low": price * 0.99}))
    cs["15m"] = out
    assert "outlier_15m" not in check(cs).reasons


# ---------------------------------------------------------------- 티커
def test_ticker_stale_after_5_minutes():
    rep = check(ticker=good_ticker(ts=NOW - 5 * 60 * 1000 - 1))
    assert "ticker_stale" in rep.reasons
    assert not rep.ok


def test_ticker_exactly_5_minutes_is_fresh():
    assert check(ticker=good_ticker(ts=NOW - 5 * 60 * 1000)).ok


def test_spread_too_wide():
    rep = check(ticker=good_ticker(bid=99.6, ask=100.4))  # 0.8%
    assert "spread_too_wide:0.8%" in rep.reasons
    assert not rep.ok


def test_spread_within_limit():
    assert check(ticker=good_ticker(bid=99.8, ask=100.2)).ok  # 0.4%


def test_spread_exactly_half_percent_passes():
    rep = check(ticker=good_ticker(bid=99.75, ask=100.25))  # 정확히 0.5%
    assert not any(r.startswith("spread_too_wide") for r in rep.reasons)


def test_spread_reason_formatting_two_decimals():
    rep = check(ticker=good_ticker(bid=99.0, ask=101.0))  # 2%
    assert "spread_too_wide:2%" in rep.reasons
    rep = check(ticker=good_ticker(bid=99.2, ask=100.0))  # ~0.803%
    assert any(r.startswith("spread_too_wide:0.8") for r in rep.reasons)


def test_missing_bid_ask_skips_spread_check():
    assert check(ticker=good_ticker(bid=None, ask=None)).ok


def test_crossed_book():
    rep = check(ticker=good_ticker(bid=100.5, ask=100.0))
    assert "crossed_book" in rep.reasons


def test_ticker_nan_and_non_positive():
    assert "nan" in check(ticker=good_ticker(last=float("nan"))).reasons
    assert "nan" in check(ticker=good_ticker(bid=float("inf"))).reasons
    assert "non_positive" in check(ticker=good_ticker(last=0.0)).reasons
    assert "non_positive" in check(ticker=good_ticker(ask=-1.0)).reasons


def test_symbol_mismatch():
    rep = check(ticker=good_ticker(symbol="ETH/USDT"))
    assert "symbol_mismatch" in rep.reasons


# ---------------------------------------------------------------- 복합/순서
def test_multiple_reasons_collected_in_deterministic_order():
    cs = {
        "1h": make_candles(TF1H, 50, close_lag=5),
        "15m": make_candles(TF15, 100),
    }
    rep = check(cs, ticker=good_ticker(bid=99.0, ask=101.0, ts=NOW - 10**7))
    assert not rep.ok
    # 15m 먼저, 그 다음 1h, 그 다음 티커
    idx = {r.split(":")[0]: i for i, r in enumerate(rep.reasons)}
    assert idx["too_few_15m"] < idx["too_few_1h"] < idx["stale_1h"] < idx["spread_too_wide"] < idx["ticker_stale"]
    assert rep.stale_seconds is not None and rep.stale_seconds > 0


def test_ok_false_has_at_least_one_reason_and_vice_versa():
    for rep in (check(), check({"15m": []}), check(ticker=good_ticker(ts=0))):
        assert rep.ok == (len(rep.reasons) == 0)


def test_cycle_minutes_does_not_change_verdict():
    for cm in (1, 5, 15, 60, 240):
        assert check(cycle_minutes=cm).ok
        assert not check({"15m": make_candles(TF15, 230, close_lag=10)}, cycle_minutes=cm).ok


def test_report_is_json_serializable_sorted():
    import json

    rep = check({"15m": make_candles(TF15, 10)})
    s = json.dumps(rep.model_dump(), sort_keys=True, ensure_ascii=False)
    assert '"ok": false' in s and "too_few_15m" in s


def test_now_far_in_future_everything_stale():
    rep = check(now_ms=NOW + 30 * TF1D)
    for tf in ("15m", "1h", "4h", "1d"):
        assert any(r.startswith(f"stale_{tf}:") for r in rep.reasons), rep.reasons
    assert "ticker_stale" in rep.reasons
    assert rep.stale_seconds is not None and rep.stale_seconds >= 29 * 86_400


def test_large_series_fast():
    import time as _t

    cs = {"15m": make_candles(TF15, 5000), "1h": make_candles(TF1H, 2000)}
    t0 = _t.perf_counter()
    assert check(cs).ok
    assert _t.perf_counter() - t0 < 0.2
