"""오프라인 체험·테스트용 부품 (키·네트워크 불필요).

- :class:`FakeMarketData` — 결정적 합성 시세. ``MarketData`` 프로토콜(§4)을 그대로 구현한다.
  가격은 **타임스탬프의 결정적 함수**(사인파 + 해시 잡음)라서 타임프레임·사이클이 바뀌어도 서로 일관되고,
  같은 시각이면 언제나 같은 값이 나온다. 마지막 캔들은 *완료된* 봉(ccxt 경로와 동일).
- :func:`scenario_responses` / :func:`make_mock_backend` — ``MockBackend`` 시나리오 팩토리.
  ``conservative``(기본): 스키마의 ``conservative_example`` → 진입 없음(HOLD).
  ``approve``: 기술 롱·플로우 중립·강세/약세 생존·리스크 승인·총괄 BUY → paper 진입.
- :func:`mock_backend_from_env` — ``MOCK_SCENARIO`` 환경변수로 시나리오를 고른다.

``main run --offline-fixture`` 가 ``CcxtMarketData`` 대신 :class:`FakeMarketData` 를 쓰고 뉴스 조회를 끈다.
"""
from __future__ import annotations

import inspect
import logging
import math
import os
import time
import zlib
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    DeathPath,
    Debias,
    FlowOutput,
    FlowSymbolView,
    HeadDecision,
    HeadOutput,
    RedLine,
    RiskCandidateVerdict,
    RiskGlobal,
    RiskOutput,
    RiskPositionView,
    TechnicalOutput,
    Thesis,
    ThesisPoint,
)
from tradingbot.config import HardLimits
from tradingbot.exchange import ExchangeError, split_symbol
from tradingbot.llm.backend import MockBackend
from tradingbot.models import Candle, FuturesContext, OrderBookSummary, Ticker

log = logging.getLogger("tradingbot.testing")

__all__ = [
    "FakeMarketData", "TF_MS", "SCENARIOS", "DEFAULT_SCENARIO",
    "scenario_responses", "conservative_factory", "make_mock_backend", "mock_backend_from_env",
]

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_BASE_PRICES = {"BTC": 60_000.0, "ETH": 3_000.0, "SOL": 150.0, "BNB": 600.0, "XRP": 0.6, "ADA": 0.4,
                "DOGE": 0.15, "AVAX": 30.0, "LINK": 15.0, "DOT": 6.0}
# (주기 ms, 진폭 로그) — 느린 추세 + 일중 파동 + 단기 파동
_WAVES = ((6 * 86_400_000, 0.04), (86_400_000, 0.012), (6 * 3_600_000, 0.005))
_NOISE = 0.0012


def _h01(key: str) -> float:
    """문자열 → [0, 1) 결정적 해시."""
    return (zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF) / 4294967296.0


class FakeMarketData:
    """결정적 합성 ``MarketData``. 네트워크 없음.

    ``clock_ms`` 로 현재 시각을 주입할 수 있다(기본 실제 시계). 알 수 없는 기호는 기호 해시로 기준가를 만든다.
    ``quote_volume_24h``·``spread_pct``·``depth_usdt`` 는 스캐너 유동성 게이트를 넉넉히 통과하는 기본값.
    """

    def __init__(self, *, clock_ms: Callable[[], int] | None = None, quote_volume_24h: float = 500_000_000.0,
                 spread_pct: float = 0.0002, depth_usdt: float = 2_000_000.0,
                 funding_rate: float = 0.0001, open_interest_usdt: float = 1e9, long_short_ratio: float = 1.1) -> None:
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self.quote_volume_24h = float(quote_volume_24h)
        self.spread_pct = float(spread_pct)
        self.depth_usdt = float(depth_usdt)
        self.funding_rate = funding_rate
        self.open_interest_usdt = open_interest_usdt
        self.long_short_ratio = long_short_ratio
        self.calls: list[tuple] = []

    def __repr__(self) -> str:
        return "FakeMarketData(synthetic, offline)"

    # ------------------------------------------------------------------ 가격 모형
    @staticmethod
    def _base(symbol: str) -> float:
        try:
            base, quote = split_symbol(symbol)
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"잘못된 기호: {symbol!r}") from exc
        if quote.upper() != "USDT":
            raise ExchangeError(f"{symbol}: USDT 페어만 지원")
        b = base.upper()
        if b in _BASE_PRICES:
            return _BASE_PRICES[b]
        return 1.0 + 99.0 * _h01(f"p0|{b}")

    def price_at(self, symbol: str, ts_ms: int) -> float:
        """시각 ``ts_ms`` 의 합성 가격(결정적)."""
        p0 = self._base(symbol)
        lp = 0.0
        for i, (period, amp) in enumerate(_WAVES):
            phase = 2 * math.pi * _h01(f"phase|{symbol}|{i}")
            lp += amp * math.sin(2 * math.pi * (ts_ms % period) / period + phase)
        # 1분 격자 해시 잡음(같은 분이면 같은 값)
        minute = int(ts_ms // 60_000)
        lp += _NOISE * (2.0 * _h01(f"n|{symbol}|{minute}") - 1.0)
        return p0 * math.exp(lp)

    def _volume(self, symbol: str, tf: str, ts: int) -> float:
        per_bar = self.quote_volume_24h * TF_MS[tf] / 86_400_000.0
        price = self.price_at(symbol, ts)
        return per_bar * (0.8 + 0.4 * _h01(f"v|{symbol}|{tf}|{ts}")) / max(price, 1e-12)

    # ------------------------------------------------------------------ MarketData
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        self.calls.append(("ohlcv", symbol, timeframe, int(limit)))
        if timeframe not in TF_MS:
            raise ExchangeError(f"지원하지 않는 타임프레임: {timeframe!r}")
        n = max(0, int(limit))
        tf = TF_MS[timeframe]
        now = int(self._clock())
        last_open = (now // tf) * tf - tf          # 마지막 *완료* 봉의 시작 시각
        out: list[Candle] = []
        for k in range(n):
            ts = last_open - (n - 1 - k) * tf
            o = self.price_at(symbol, ts)
            c = self.price_at(symbol, ts + tf - 60_000)
            mid = self.price_at(symbol, ts + tf // 2)
            hi = max(o, c, mid) * 1.0008
            lo = min(o, c, mid) * 0.9992
            out.append(Candle(ts=ts, open=o, high=hi, low=lo, close=c, volume=self._volume(symbol, timeframe, ts)))
        return out

    def fetch_ticker(self, symbol: str) -> Ticker:
        self.calls.append(("ticker", symbol))
        now = int(self._clock())
        last = self.price_at(symbol, now)
        prev = self.price_at(symbol, now - 86_400_000)
        half = self.spread_pct / 2.0
        return Ticker(symbol=symbol, last=last, bid=last * (1 - half), ask=last * (1 + half),
                      quote_volume_24h=self.quote_volume_24h, change_24h_pct=(last / prev - 1.0) * 100.0, ts=now)

    def fetch_orderbook_summary(self, symbol: str, depth: int = 50) -> OrderBookSummary:
        self.calls.append(("orderbook", symbol))
        now = int(self._clock())
        last = self.price_at(symbol, now)
        half = self.spread_pct / 2.0
        return OrderBookSummary(symbol=symbol, best_bid=last * (1 - half), best_ask=last * (1 + half),
                                spread_pct=self.spread_pct, bid_depth_usdt_1pct=self.depth_usdt,
                                ask_depth_usdt_1pct=self.depth_usdt, ts=now)

    def fetch_futures_context(self, symbol: str) -> FuturesContext | None:
        self.calls.append(("futures", symbol))
        self._base(symbol)
        return FuturesContext(symbol=symbol, funding_rate=self.funding_rate,
                              open_interest_usdt=self.open_interest_usdt,
                              long_short_ratio=self.long_short_ratio, ts=int(self._clock()))

    def server_time_ms(self) -> int:
        return int(self._clock())


# ======================================================================= MockBackend 시나리오
SCENARIOS = ("conservative", "approve")
DEFAULT_SCENARIO = "conservative"


def _price_from(payload: Mapping[str, Any]) -> float | None:
    fp = payload.get("feature_pack")
    if isinstance(fp, Mapping):
        p = fp.get("price")
        if isinstance(p, (int, float)) and math.isfinite(p) and p > 0:
            return float(p)
    return None


def _stop_pct_from(payload: Mapping[str, Any]) -> float:
    """노이즈 바닥보다 넉넉하되 하드 리밋(2%) 이하인 손절 %."""
    fp = payload.get("feature_pack")
    floor = None
    if isinstance(fp, Mapping):
        floor = fp.get("noise_floor_stop_pct")
    base = 1.0
    if isinstance(floor, (int, float)) and math.isfinite(floor) and floor > 0:
        base = max(base, float(floor) * 1.2)
    return round(min(base, HardLimits.MAX_STOP_LOSS_PCT * 100.0 * 0.9), 4)


def _tp_pct(stop_pct: float) -> float:
    return round(min(stop_pct * 2.25, HardLimits.MAX_TAKE_PROFIT_PCT * 100.0 * 0.95), 4)


def _approve_technical(p: dict) -> TechnicalOutput:
    sym = str(p.get("symbol"))
    price = _price_from(p) or 100.0
    stop_pct = _stop_pct_from(p)
    tp_pct = _tp_pct(stop_pct)
    return TechnicalOutput(
        symbol=sym, direction="long", setup_type="trend_continuation", timing="now", mtf_agreement=True,
        score=4, confidence=0.8, stop_candidate_price=round(price * (1 - stop_pct / 100.0), 8), stop_pct=stop_pct,
        sl_reference="atr", target_candidate_price=round(price * (1 + tp_pct / 100.0), 8), tp_pct=tp_pct,
        invalidation="1h 종가가 기준선 아래", noise_check=True, exit_flag=False, exit_reason=None,
        evidence=["feature_pack.mtf_agreement: true"])


def _approve_flow(p: dict) -> FlowOutput:
    views = [FlowSymbolView(symbol=s, posture="neutral", crowding_risk="low", squeeze_direction="none",
                            cascade_risk="low", nearest_long_liq_cluster_pct_below=8.0, stop_inside_cascade=False,
                            washout_setup=False, block_new_longs=False, block_reason=None, flow_bias="long",
                            confidence=0.6, evidence=["flow_packs.funding_regime: neutral"])
             for s in p.get("symbols") or []]
    return FlowOutput(per_symbol=views, market_wide_note="합성 시세 — 중립", data_confidence="high")


def _approve_bull(p: dict) -> BullOutput:
    return BullOutput(symbol=str(p.get("symbol")), case_strength=4,
                      thesis_points=[ThesisPoint(claim="추세 지속", evidence_field="technical.setup_type")],
                      catalysts=["합성 시나리오"], underreaction_evidence="feature_extract 기준",
                      entry_trigger="지금", expected_horizon_bars=16, invalidation_level=None,
                      disconfirming_evidence=["BTC 약세 전환", "거래량 감소"], main_risk_to_bull_case="BTC 급락",
                      what_would_disprove="1h 종가가 손절선 아래", confidence=0.7)


def _approve_bear(p: dict) -> BearOutput:
    paths = [DeathPath(family=f, description="합성 시나리오", observable_trigger="btc_return_1h_pct < -1",
                       probability_band="low", time_to_impact_bars=None)
             for f in ("liquidation_cascade", "news_shock", "regime_flip")]
    return BearOutput(symbol=str(p.get("symbol")), death_paths=paths, greed_flags=[],
                      disconfirming_evidence=["추세 유지"], strongest_point="BTC 의존도",
                      what_would_disprove_bear="거래량 증가", verdict="survivable",
                      monitoring_conditions=["btc_return_1h_pct < -1"], confidence=0.6)


def _approve_risk(p: dict) -> RiskOutput:
    cands = []
    for c in p.get("candidates") or []:
        tech = c.get("technical") if isinstance(c, Mapping) else None
        stop_pct = 1.0
        if isinstance(tech, Mapping) and isinstance(tech.get("stop_pct"), (int, float)):
            stop_pct = float(tech["stop_pct"])
        cands.append(RiskCandidateVerdict(
            symbol=c["symbol"], verdict="approve", veto_reasons=[], conditions=["BTC 감시"], max_size_pct=8.0,
            stop_pct=stop_pct, take_profit_pct=_tp_pct(stop_pct), rr_net_estimate=1.8, bull_point_reliability=[],
            required_monitoring=["btc"], correlation_note="낮음"))
    poss = [RiskPositionView(symbol=x["symbol"], assumption_status=["holding"], red_lines_triggered=[], health=7,
                             action="hold", reason="합성 시나리오") for x in p.get("positions") or []]
    return RiskOutput(candidates=cands, positions=poss,
                      **{"global": RiskGlobal(entries_paused=False, pause_reason=None, confidence_floor=0.65,
                                              exposure_cap_pct=30.0, portfolio_note="정상")},
                      risk_note="리스크 정상(합성)", confidence=0.7)


def _approve_head(p: dict) -> HeadOutput:
    decisions = []
    for a in p.get("approved") or []:
        risk = a.get("risk") if isinstance(a, Mapping) else None
        stop_pct = 1.0
        if isinstance(risk, Mapping) and isinstance(risk.get("stop_pct"), (int, float)):
            stop_pct = float(risk["stop_pct"])
        decisions.append(HeadDecision(
            symbol=a["symbol"], action="BUY", confidence=0.8, size_pct=5.0, stop_pct=stop_pct,
            tp_pct=_tp_pct(stop_pct),
            thesis=Thesis(reasons=["다중 시간대 추세 지속(합성)"], assumptions=["BTC 안정", "거래량 유지"],
                          red_lines=[RedLine(metric="btc_return_1h_pct", op="<", value=-2.0)], horizon_bars=16),
            disagreement_class="horizon", losing_side_strongest_point="BTC 의존", decisive_factor="mtf",
            rationale="합성 승인 시나리오"))
    return HeadOutput(decisions=decisions,
                      debias=Debias(anchored_to_entry=False, ignored_bearish_news=False, overtrading=False,
                                    chasing_spike=False, acting_on_sentiment_alone=False),
                      reevaluation_triggers=[], hold_reasons=[], cycle_note="합성 승인 시나리오")


def _payload_symbols(role: str, p: Mapping[str, Any]) -> tuple[str, ...]:
    def syms(items: Any) -> list[str]:
        return [str(x.get("symbol")) for x in items or [] if isinstance(x, Mapping) and x.get("symbol")]

    if role == "derivatives_flow":
        return tuple(str(s) for s in p.get("symbols") or [])
    if role == "risk_manager":
        return tuple(syms(p.get("candidates")))
    if role == "head":
        return tuple(dict.fromkeys(syms(p.get("approved")) + syms(p.get("flagged_positions"))))
    return ()


def conservative_factory(role: str, output_model: type[BaseModel], payload: dict) -> BaseModel | None:
    """``conservative_example`` 을 페이로드의 심볼로 채워 돌려준다(심볼 불일치 기권 대신 '관망' 출력).

    ``symbol`` 인자를 받는 예시는 ``payload["symbol"]``, ``symbols`` 인자를 받는 예시는 역할별 심볼 목록.
    """
    example = getattr(output_model, "conservative_example", None)
    if not callable(example):
        return MockBackend._default_output(output_model)
    try:
        params = inspect.signature(example).parameters
    except (TypeError, ValueError):
        params = {}
    sym = payload.get("symbol")
    if "symbol" in params and isinstance(sym, str) and sym:
        return example(sym)
    if "symbols" in params:
        return example(_payload_symbols(role, payload))
    return example()


def scenario_responses(name: str = DEFAULT_SCENARIO) -> dict[str, Callable[[dict], BaseModel]]:
    """시나리오 이름 → ``MockBackend(responses=...)`` 용 역할별 팩토리. ``conservative`` 는 빈 dict."""
    key = (name or DEFAULT_SCENARIO).strip().lower()
    if key == "conservative":
        return {}
    if key == "approve":
        return {"technical": _approve_technical, "derivatives_flow": _approve_flow, "bull": _approve_bull,
                "bear": _approve_bear, "risk_manager": _approve_risk, "head": _approve_head}
    raise ValueError(f"알 수 없는 MOCK_SCENARIO: {name!r} (허용: {', '.join(SCENARIOS)})")


def make_mock_backend(name: str = DEFAULT_SCENARIO) -> MockBackend:
    """시나리오 MockBackend. 시나리오에 없는 역할(news·journal 등)은 :func:`conservative_factory`."""
    return MockBackend(scenario_responses(name), default_factory=conservative_factory)


def mock_backend_from_env(env: Mapping[str, str] | None = None) -> MockBackend:
    """``MOCK_SCENARIO``(기본 conservative). 알 수 없는 값은 경고 후 conservative(가장 보수적)."""
    src = os.environ if env is None else env
    name = (src.get("MOCK_SCENARIO") or DEFAULT_SCENARIO).strip().lower() or DEFAULT_SCENARIO
    if name not in SCENARIOS:
        log.warning("알 수 없는 MOCK_SCENARIO=%r → conservative 사용", name)
        name = DEFAULT_SCENARIO
    log.info("MockBackend 시나리오: %s", name)
    return make_mock_backend(name)
