"""indicators.py 적대적 리뷰 테스트 (SPEC §5, §0 공통 규칙).

여기 있는 테스트는 현재 구현에서 **실패**하도록 작성됐고, 결함이 고쳐지면 통과해야 한다.
구현자 파일(tradingbot/indicators.py, tests/test_indicators.py)은 건드리지 않는다.

결함 R1 — 비유한(NaN/inf) 캔들 값이 걸러지지 않고 지표로 새어 나간다.
  - `compute()` 가 ``ema_20 = nan`` 인 채로 ``warmup_ok = True`` 를 돌려준다. SPEC §2/§5 의
    ``warmup_ok`` 정의("모든 필수 지표 계산 가능 여부")와 모순이다.
  - `rsi()` 는 NaN 변화량을 ``d > 0`` / ``d < 0`` 모두 False 로 보고 "변화 없음"으로 조용히 흡수해
    유한하지만 틀린 값을 낸다(None 도 NaN 도 아니다). 같은 NaN 입력에 대해 `sma()` 는 롤링 합이
    영구 오염되어 창이 지나가도 NaN 이고, `bollinger()` 의 middle 은 창이 지나가면 회복된다.
    즉 세 도우미가 같은 입력에 서로 다른 답을 내므로 "같은 입력이면 같은 출력" 이전에 정의 자체가
    일관되지 않다.
  - 결과 IndicatorSet 은 ``json.dumps(..., allow_nan=False)`` 에서 ValueError 를 내고, 기본
    ``json.dumps`` 는 RFC 8259 비준수 토큰 ``NaN`` 을 LLM 입력/리포트에 흘린다.
"""
from __future__ import annotations

import json
import math

import pytest

from tradingbot import indicators as ind
from tradingbot.indicators import bollinger, compute, rsi, sma
from tradingbot.models import Candle, IndicatorSet

TF_MS = 15 * 60 * 1000


def _candles(closes: list[float], *, volume: float = 1.0) -> list[Candle]:
    out = []
    for i, c in enumerate(closes):
        h = c if math.isfinite(c) else c
        out.append(Candle(ts=i * TF_MS, open=c, high=h + 1.0, low=c - 1.0, close=c, volume=volume))
    return out


def _wavy(n: int) -> list[float]:
    return [100.0 + (i % 5) - 2.0 for i in range(n)]


def _float_fields(s: IndicatorSet) -> dict[str, float]:
    return {k: v for k, v in s.model_dump().items() if isinstance(v, float)}


# ---------------------------------------------------------------- R1: NaN/inf 누출
class TestNonFiniteInput:
    def test_compute_nan_close_in_window_is_not_warmup_ok(self):
        """ema_20 이 NaN 이면 '핵심 지표가 계산됨' 이 아니다 -> warmup_ok 는 False 여야 한다."""
        closes = _wavy(40)
        closes[30] = float("nan")
        out = compute("BTC/USDT", "15m", _candles(closes))
        assert not (out.ema_20 is not None and math.isnan(out.ema_20)) or out.warmup_ok is False, (
            f"ema_20={out.ema_20!r} 인데 warmup_ok={out.warmup_ok}"
        )
        assert out.warmup_ok is False

    def test_compute_never_emits_nan(self):
        """IndicatorSet 의 모든 float 필드는 None 이거나 유한해야 한다 (NaN 캔들 입력)."""
        closes = _wavy(60)
        closes[45] = float("nan")
        out = compute("BTC/USDT", "15m", _candles(closes))
        bad = {k: v for k, v in _float_fields(out).items() if not math.isfinite(v)}
        assert bad == {}, bad

    def test_compute_never_emits_nan_on_inf_close(self):
        closes = _wavy(60)
        closes[45] = float("inf")
        out = compute("BTC/USDT", "15m", _candles(closes))
        bad = {k: v for k, v in _float_fields(out).items() if not math.isfinite(v)}
        assert bad == {}, bad

    def test_compute_nan_volume_never_emits_nan(self):
        candles = _candles(_wavy(60))
        candles[-3] = candles[-3].model_copy(update={"volume": float("nan")})
        out = compute("BTC/USDT", "15m", candles)
        bad = {k: v for k, v in _float_fields(out).items() if not math.isfinite(v)}
        assert bad == {}, bad

    def test_compute_result_is_strict_json_serializable(self):
        """§0 결정적 JSON: 결과는 RFC 준수 JSON 이어야 한다 (NaN 토큰 금지)."""
        closes = _wavy(40)
        closes[30] = float("nan")
        out = compute("BTC/USDT", "15m", _candles(closes))
        json.dumps(out.model_dump(), sort_keys=True, ensure_ascii=False, allow_nan=False)

    def test_rsi_does_not_silently_treat_nan_change_as_flat(self):
        """NaN 이 들어간 뒤 RSI 가 '유한한 그럴듯한 값' 을 내면 안 된다 (None 또는 비유한이어야)."""
        vals = _wavy(30) + [float("nan")] + _wavy(10)
        out = rsi(vals, 14)
        nan_idx = 30
        # NaN 봉과 바로 다음 봉은 NaN 변화량을 포함한다 -> 유한한 RSI 가 나올 수 없다.
        for i in (nan_idx, nan_idx + 1):
            v = out[i]
            assert v is None or not math.isfinite(v), f"rsi[{i}]={v!r} (NaN 변화량을 0 으로 흡수함)"

    def test_sma_and_bollinger_middle_agree_after_nan_leaves_window(self):
        """같은 입력·같은 창에서 sma 와 bollinger middle 은 같은 값이어야 한다.

        현재 sma 는 롤링 합이 NaN 으로 영구 오염되고 bollinger 는 창 단위로 다시 계산해 회복한다.
        """
        vals = _wavy(10) + [float("nan")] + _wavy(20)
        n = 5
        s = sma(vals, n)
        _, mid, _, _ = bollinger(vals, n, 2.0)
        i = len(vals) - 1  # NaN 은 창 밖
        assert mid[i] is not None and math.isfinite(mid[i])
        assert s[i] is not None and math.isfinite(s[i]), f"sma[{i}]={s[i]!r} vs bollinger middle {mid[i]!r}"
        assert s[i] == pytest.approx(mid[i])


# ---------------------------------------------------------------- 참고: 통과해야 하는 보조 확인
def test_warmup_declaration_unchanged():
    """리뷰 테스트가 전제하는 WARMUP 값(회귀 방지용, 현재 통과)."""
    assert ind.WARMUP["ema_20"] == 20 and ind.WARMUP["rsi_14"] == 15
