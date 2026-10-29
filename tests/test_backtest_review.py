"""backtest.py 적대적 리뷰 테스트 (SPEC §13).

이 파일의 테스트는 **현재 구현에서 실패**하도록 작성됐다. 각 테스트는 리뷰에서 찾은 결함 하나를
고정하며, 결함이 고쳐지면 통과해야 한다. 구현 파일(tradingbot/backtest.py, tests/test_backtest.py)은
수정하지 않는다.

결함 요약
- R1 캐시 오염: 거래소가 빈 목록을 돌려주면 그 빈 결과가 캐시에 저장되고, 이후 모든 실행이
  거래소를 다시 묻지 않은 채 같은 ValueError 로 실패한다(캐시를 손으로 지워야 복구).
- R2 비유한 캔들: 종가가 NaN 인 봉 하나가 (보유 중이면) 결과 JSON 에 NaN 을 흘리고,
  (미보유면) indicators.compute 의 ValueError 로 백테스트 전체를 중단시킨다.
- R3 기간 부족 미표시: 요청한 days 보다 훨씬 짧은 데이터만 있어도 결과 카드는 요청 일수를 그대로
  적고 판정 사유에도 나타나지 않는다(로그 WARNING 뿐). 90일 검증을 기대한 사람이 12일 표본의
  VALIDATED 를 그대로 믿게 된다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingbot import backtest as bt
from tradingbot.models import Candle

TF = 900_000  # 15m
H1 = 3_600_000
H4 = 14_400_000
MS_PER_DAY = 86_400_000
SYM = "BTC/USDT"


def flat_series(n: int, price: float = 100.0, wick: float = 0.05, start_ts: int = 0) -> list[Candle]:
    return [
        Candle(ts=start_ts + i * TF, open=price, high=price + wick, low=price - wick, close=price, volume=100.0)
        for i in range(n)
    ]


def no_atr_higher(base: list[Candle]) -> dict[str, list[Candle]]:
    t0 = base[0].ts
    h1 = [Candle(ts=t0 - 2 * H1 + k * H1, open=100, high=100.1, low=99.9, close=100, volume=1) for k in range(2)]
    h4 = [Candle(ts=t0 - 2 * H4 + k * H4, open=100, high=100.1, low=99.9, close=100, volume=1) for k in range(2)]
    return {"15m": base, "1h": h1, "4h": h4}


def buy_at(bars: set[int]):
    def rule(ind15, ind1h, ind4h):
        if ind15.ts // TF in bars:
            return "BUY", 0.7, "test_buy"
        return "HOLD", 0.0, "test_hold"

    return rule


def _reject_constant(name: str):
    raise ValueError(f"표준 JSON 이 아닌 상수: {name}")


@pytest.fixture
def small_windows(monkeypatch):
    monkeypatch.setattr(bt, "WINDOW_BARS", {"base": 30, "1h": 30, "4h": 30})


@pytest.fixture
def cfg(tmp_path: Path) -> bt.BacktestConfig:
    return bt.BacktestConfig(symbols=[SYM], out_dir=tmp_path / "out", cache_dir=tmp_path / "cache")


class _Market:
    """fetch_ohlcv 만 쓰는 가짜 시장 데이터. ``data(tf)`` 가 캔들 목록을 돌려준다."""

    def __init__(self, data, calls: list[str]):
        self._data = data
        self._calls = calls

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        self._calls.append(timeframe)
        return list(self._data(timeframe))


# ================================================================ R1 캐시 오염
class TestCachePoisoning:
    def test_empty_fetch_result_is_not_cached_forever(self, cfg, small_windows, monkeypatch):
        """거래소가 빈 목록을 돌려준 실행 뒤, 다음 실행은 거래소를 다시 물어야 한다.

        현재: 빈 목록이 ``<sym>_<tf>_<limit>.json`` 에 ``[]`` 로 저장되고, ``_load_cache`` 가
        ``[]`` (None 아님) 을 돌려주므로 두 번째 실행은 fetch_ohlcv 를 한 번도 부르지 않고
        같은 ValueError 로 끝난다. 일시적 거래소 장애가 영구 장애가 된다.
        """
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        cfg.days = 1
        calls: list[str] = []

        with pytest.raises(ValueError):
            bt.run(cfg, market=_Market(lambda tf: [], calls))  # 1차: 빈 응답 → 실패는 정상
        assert calls, "1차 실행은 거래소를 물어야 한다"

        base = flat_series(200)
        good = _Market(lambda tf: base if tf == "15m" else bt.resample(base, tf), calls)
        calls.clear()
        res = bt.run(cfg, market=good)  # 2차: 정상 응답이면 성공해야 한다
        assert "15m" in calls, "빈 캐시를 신뢰하지 말고 거래소를 다시 물어야 한다"
        assert res.metrics["bars"] > 0


# ================================================================ R2 비유한 캔들
class TestNonFiniteCandles:
    def test_nan_close_while_holding_does_not_leak_nan_into_json(self, cfg, small_windows, monkeypatch):
        """보유 중에 종가 NaN 봉을 만나면 자본 곡선·지표에 NaN 이 들어가고 결과 JSON 이
        비표준(``NaN`` 리터럴)이 된다. 결과 카드는 항상 표준 JSON 이어야 한다
        (구현 테스트도 정상 데이터에 대해 'NaN' 부재를 고정한다)."""
        monkeypatch.setattr(bt, "rule_signal", buy_at({10}))
        base = flat_series(60)
        base[20] = base[20].model_copy(update={"close": float("nan")})
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert res.json_path is not None
        raw = res.json_path.read_text(encoding="utf-8")
        json.loads(raw, parse_constant=_reject_constant)  # NaN/Infinity 가 있으면 ValueError
        json.dumps(res.to_dict(), allow_nan=False)
        for _, eq in res.equity_curve:
            assert eq == eq, "자본 곡선에 NaN"

    def test_nan_close_while_flat_does_not_abort_the_whole_run(self, cfg, small_windows, monkeypatch):
        """미보유 상태에서 종가 NaN 봉을 만나면 indicators.compute 의 ValueError 가 그대로 전파되어
        백테스트 전체가 중단된다. 봉 하나의 손상은 그 봉을 HOLD/건너뜀 처리하고 계속 진행해야
        한다(실거래 사이클의 '데이터 오류는 죽이지 않는다' 정책과 같은 방향)."""
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        base = flat_series(60)
        base[20] = base[20].model_copy(update={"close": float("nan")})
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        assert res.metrics["bars"] == 60
        json.dumps(res.to_dict(), allow_nan=False)


# ================================================================ R3 기간 부족 미표시
class TestDataCoverageShortfall:
    def test_short_coverage_is_a_verdict_reason(self, cfg, small_windows, monkeypatch):
        """days=90 을 요청했는데 데이터가 2일치뿐이면(거래소 limit 상한, 캐시 등) 판정 사유에
        기간 부족이 드러나야 한다. 현재는 로그 WARNING(그것도 market 경로에서만)뿐이고,
        결과 카드는 '90일' 이라고 적는다."""
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        cfg.days = 90
        base = flat_series(200)  # ≈ 2.08 일
        res = bt.run(cfg, candles={SYM: no_atr_higher(base)})
        covered_days = (base[-1].ts + TF - base[0].ts) / MS_PER_DAY
        assert covered_days < cfg.days * 0.5
        assert res.verdict == "NOT VALIDATED"
        assert any(("coverage" in r) or ("기간" in r) for r in res.reasons), res.reasons

    def test_short_coverage_is_reported_from_market_path_too(self, cfg, small_windows, monkeypatch):
        """market 경로에서 요청 봉 수보다 적게 받았을 때도 결과(사유/지표)에 남아야 한다.
        로그만 남기면 대시보드·카드를 읽는 사람은 알 수 없다."""
        monkeypatch.setattr(bt, "rule_signal", lambda a, b, c: ("HOLD", 0.0, "x"))
        cfg.days = 90
        base = flat_series(200)
        calls: list[str] = []
        res = bt.run(cfg, market=_Market(lambda tf: base if tf == "15m" else bt.resample(base, tf), calls))
        assert calls
        assert any(("coverage" in r) or ("기간" in r) for r in res.reasons), res.reasons
