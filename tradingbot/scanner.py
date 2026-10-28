"""코드 전용 스캐너 (SPEC §18 + v2.1).

LLM 돈을 쓰기 전에 도는 결정적 데이터·피처 계층이다. 한 사이클에 한 번 ``Scanner.scan`` 을 부른다.

흐름(``Scanner.scan``)
1. 유니버스 = ``settings.symbols`` (+ 화이트리스트 밖이어도 보유 중인 종목). ``BTC/USDT`` 는 레짐 계산용으로
   유니버스에 없어도 항상 조회한다(이 경우 후보가 되지 않는다).
2. 캔들 15m 300 / 1h 200 / 4h 200 / 1d 120. 4h·1d 는 현재 봉 구간(``now_ms // tf``)이 바뀌지 않았으면
   메모리 캐시를 재사용한다(키: ``(symbol, tf)`` → ``(bucket, candles)``).
3. ``quality.check`` 실패 종목은 ``skipped`` — 보유 종목이면 ``marks`` 만 갱신하고 팩(LLM 입력)에서 뺀다.
4. 유동성 게이트, 보유/쿨다운/복수매매 창/무거래량 급등 제외. 펀딩 정산 창이면 ``funding_window=True``
   (후보는 남기고 신규 주문 금지는 cycle 이 한다).
5. 팩 생성(최종 후보 + 보유 종목), 레짐, 종합심리.
6. 레짐 게이트: ``macro_alarm`` → 후보 없음(skipped 에 사유), ``name_alarm_symbols`` 종목 제외.

격리 정책: 종목 단위로 모든 예외(``ExchangeError`` 포함)를 잡아 ``skipped`` 에 사유를 남기고 계속한다.
사유·로그에는 예외 **타입 이름만** 넣는다(메시지에 비밀값이 섞일 가능성을 원천 차단).

단위: factor_table 의 모멘텀·반전·rs·rv·OI 변화는 **% 단위**(로그수익률×100), ``funding_z`` 는 z 값,
``volume_ratio`` 는 배수.

부가 상태(메모리, ``cache_dir`` 가 있으면 ``scanner_history.json`` 에 원자적으로 저장):
- 종목별 선물 관측 이력(펀딩·OI·롱숏비) 7일 — ``funding_7d_mean``·``funding_z``·``oi_change_24h`` 계산용
  (``MarketData`` 에는 펀딩 이력 API 가 없다. ``market.fetch_funding_history(symbol)`` 이 있으면 그것을 쓴다).
- 유니버스 평균 롱숏비 이력 — 종합심리 ``ls_pctile`` 용.

뉴스: ``news_fetcher`` 가 None 이면 **뉴스를 조회하지 않는다**(오프라인 안전). 실운용은
``news_fetcher=news.default_fetcher`` 를 넘긴다. 피드에서 항목이 하나도 없으면 ``news_available=False``
("빈 피드 ≠ 조용한 시장").
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradingbot import indicators as ind_mod
from tradingbot import news as news_mod
from tradingbot import quality as quality_mod
from tradingbot.config import HardLimits, Settings
from tradingbot.exchange import MarketData
from tradingbot.features import (
    FeaturePack,
    FlowPack,
    RegimePack,
    RiskPack,
    SentimentComposite,
    build_feature_pack,
    build_flow_pack,
    build_regime_pack,
    build_risk_pack,
    composite_sentiment,
)
from tradingbot.models import (
    BotState,
    Candle,
    FuturesContext,
    IndicatorSet,
    NewsItem,
    OrderBookSummary,
    QualityReport,
    Ticker,
)

log = logging.getLogger("tradingbot.scanner")

__all__ = [
    "Thresholds", "LiquidityGate", "Candidate", "ScanResult", "Scanner",
    "FACTOR_WEIGHTS", "FACTOR_NAMES", "BTC_SYMBOL", "CANDLE_LIMITS", "CACHED_TIMEFRAMES",
    "in_funding_window", "zscores", "rank_candidates", "compute_factors", "liquidity_gate",
    "momentum_pct", "reversal_1h_pct", "rv_24h_pct", "funding_zscore", "volume_24h_multiple",
    "is_unconfirmed_spike", "ls_percentile",
]

BTC_SYMBOL = "BTC/USDT"
CANDLE_LIMITS: dict[str, int] = {"15m": 300, "1h": 200, "4h": 200, "1d": 120}
CACHED_TIMEFRAMES: frozenset[str] = frozenset({"4h", "1d"})
TF_MS: dict[str, int] = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

FACTOR_WEIGHTS: dict[str, float] = {
    "momentum_4h": 0.25, "momentum_1d": 0.15, "reversal_1h": 0.10, "rv_24h": -0.10,
    "volume_ratio": 0.10, "rs_vs_btc": 0.15, "funding_z": -0.10, "oi_change_24h": 0.05,
}
FACTOR_NAMES: tuple[str, ...] = tuple(FACTOR_WEIGHTS)

ZSCORE_MIN_SAMPLES = 4       # 이보다 적으면 순위 기반(−1..1)
Z_CLIP = 3.0                 # z 윈저라이즈
FUNDING_Z_MIN_HISTORY = 3    # funding_z 계산에 필요한 최소 과거 관측 수
FUNDING_Z_MIN_SD = 1e-6      # funding_z 분모 하한(펀딩률 단위, 0.0001%p) — 부동소수 잡음이 z 를 폭주시키지 않게
LS_MIN_HISTORY = 12          # ls_pctile 을 이력 백분위로 계산하는 최소 관측 수(미만이면 임계값 선형 매핑)
HISTORY_KEEP_MS = 7 * 86_400_000 + 3_600_000
PREV_OI_TARGET_MS = 24 * 3_600_000
PREV_OI_MIN_AGE_MS = 20 * 3_600_000
PREV_OI_MAX_AGE_MS = 28 * 3_600_000
FUNDING_PERIOD_S = 8 * 3600
HISTORY_FILE = "scanner_history.json"
NEWS_CACHE_FILE = "news_cache.json"


# ======================================================================= 임계값
@dataclass(frozen=True)
class Thresholds:
    """스캐너 기본 임계값. 환경변수 ``SCANNER_<필드명>`` 으로 덮어쓸 수 있다(:meth:`from_env`).

    하드 리밋이 아니라 후보 선별용 설정이다. 잘못된 값(숫자 아님·음수·비유한)은 경고 후 기본값.
    """

    MIN_QUOTE_VOLUME_24H: float = 20_000_000.0
    MAX_SPREAD_BPS: float = 10.0
    MIN_DEPTH_1PCT_USDT: float = 200_000.0
    MIN_VOL_RATIO_15M: float = 0.3
    MAX_SPIKE_24H_PCT: float = 15.0
    SPIKE_VOLUME_MULT: float = 2.0
    FUNDING_WINDOW_S: int = 60

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Thresholds:
        """``env``(기본 ``os.environ``)의 ``SCANNER_*`` 값을 읽어 만든다. 없는 키는 기본값."""
        src = os.environ if env is None else env
        overrides: dict[str, Any] = {}
        for f in fields(cls):
            key = f"SCANNER_{f.name}"
            raw = src.get(key)
            if raw is None or not str(raw).strip():
                continue
            try:
                val = float(str(raw).strip())
            except ValueError:
                log.warning("%s 값이 숫자가 아님 → 기본값 사용", key)
                continue
            if not math.isfinite(val) or val < 0:
                log.warning("%s 값이 유효 범위 밖(음수/비유한) → 기본값 사용", key)
                continue
            overrides[f.name] = int(val) if f.type in (int, "int") else val
        return replace(cls(), **overrides) if overrides else cls()


# ======================================================================= 모델
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiquidityGate(_Model):
    symbol: str
    ok: bool
    reasons: list[str] = Field(default_factory=list)
    spread_bps: float | None = None
    quote_volume_24h: float | None = None
    depth_1pct_usdt: float | None = None


class Candidate(_Model):
    symbol: str
    rank: int
    composite_z: float
    factor_table: dict[str, float | None] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class ScanResult(_Model):
    ts: int
    universe: list[str]
    candidates: list[Candidate]
    skipped: list[dict]                       # {"symbol","reason"}
    regime: RegimePack
    sentiment: SentimentComposite
    funding_window: bool
    feature_packs: dict[str, FeaturePack]
    flow_packs: dict[str, FlowPack]
    risk_packs: dict[str, RiskPack]
    liquidity: dict[str, LiquidityGate]
    quality: dict[str, QualityReport]
    news: list[NewsItem]
    news_available: bool
    marks: dict[str, float]                   # 심볼별 현재가(보유 포지션 마크에도 사용)
    fetched: dict[str, int]                   # 심볼별 캔들 수 합계(디버그)


# ======================================================================= 순수 도우미
def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pos(x: Any) -> float | None:
    v = _num(x)
    return v if v is not None and v > 0 else None


def _closes(candles: Sequence[Candle] | None) -> list[float]:
    return [float(c.close) for c in (candles or [])]


def in_funding_window(now_ms: int, window_s: int = 60) -> bool:
    """UTC 00:00/08:00/16:00 정산 시각 ± ``window_s`` 초 안이면 True (경계 포함)."""
    sec = (int(now_ms) // 1000) % FUNDING_PERIOD_S
    dist = min(sec, FUNDING_PERIOD_S - sec)
    return dist <= max(0, int(window_s))


def zscores(values: Mapping[str, float | None]) -> dict[str, float | None]:
    """교차 z-점수. 유효 표본 ≥ 4 이면 (x−평균)/모표준편차(±3 윈저라이즈, 분산 0이면 0),
    아니면 순위 기반 −1..1(동순위는 평균 순위, 표본 1개면 0). None 은 None 유지."""
    valid = {k: v for k, v in ((k, _num(v)) for k, v in values.items()) if v is not None}
    out: dict[str, float | None] = {k: None for k in values}
    n = len(valid)
    if n == 0:
        return out
    if n >= ZSCORE_MIN_SAMPLES:
        mean = sum(valid.values()) / n
        var = sum((v - mean) ** 2 for v in valid.values()) / n
        sd = math.sqrt(var)
        for k, v in valid.items():
            if sd <= 1e-15 * max(1.0, abs(mean)):
                out[k] = 0.0
            else:
                out[k] = max(-Z_CLIP, min(Z_CLIP, (v - mean) / sd))
        return out
    if n == 1:
        for k in valid:
            out[k] = 0.0
        return out
    ordered = sorted(valid.items(), key=lambda kv: kv[1])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0
        for m in range(i, j + 1):
            out[ordered[m][0]] = -1.0 + 2.0 * avg_rank / (n - 1)
        i = j + 1
    return out


def momentum_pct(closes: Sequence[float], n: int, skip: int = 1) -> float | None:
    """마지막 ``skip`` 봉을 뺀 직전 ``n`` 개 로그수익률 합(%) = ln(c[−1−skip]/c[−1−skip−n])×100."""
    if n <= 0 or len(closes) < n + skip + 1:
        return None
    a, b = _pos(closes[-1 - skip - n]), _pos(closes[-1 - skip])
    if a is None or b is None:
        return None
    return math.log(b / a) * 100.0


def reversal_1h_pct(closes_1h: Sequence[float], n: int = 4) -> float | None:
    """−(최근 ``n`` 개 1h 로그수익률 합)(%)."""
    if len(closes_1h) < n + 1:
        return None
    a, b = _pos(closes_1h[-1 - n]), _pos(closes_1h[-1])
    if a is None or b is None:
        return None
    return -math.log(b / a) * 100.0


def rv_24h_pct(closes_1h: Sequence[float], n: int = 24) -> float | None:
    """최근 ``n`` 개 1h 로그수익률의 표본표준편차×√24×100 (일간 실현변동성, %)."""
    if len(closes_1h) < n + 1:
        return None
    tail = list(closes_1h[-(n + 1):])
    rets: list[float] = []
    for a, b in zip(tail, tail[1:]):
        pa, pb = _pos(a), _pos(b)
        if pa is None or pb is None:
            return None
        rets.append(math.log(pb / pa))
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0)) * math.sqrt(24.0) * 100.0


def funding_zscore(funding_now: float | None, history: Iterable[float] | None) -> float | None:
    """현재 펀딩의 과거(7d) 대비 z. 과거 관측 < 3 이거나 표준편차 0 이면 None.
    표준편차는 ``FUNDING_Z_MIN_SD`` 미만이면 하한으로 올려 쓴다(잡음 분산 → z 폭주 방지)."""
    f = _num(funding_now)
    hist = [v for v in (_num(x) for x in (history or [])) if v is not None]
    if f is None or len(hist) < FUNDING_Z_MIN_HISTORY:
        return None
    m = sum(hist) / len(hist)
    var = sum((x - m) ** 2 for x in hist) / (len(hist) - 1)
    sd = math.sqrt(max(var, 0.0))
    if sd <= 0:
        return None
    # 사실상 일정한 이력(잡음 수준 분산)에서 미세 변화가 z=±10 으로 튀지 않게 분모 하한을 둔다
    return max(-10.0, min(10.0, (f - m) / max(sd, FUNDING_Z_MIN_SD)))


def volume_24h_multiple(candles_15m: Sequence[Candle], n: int = 96) -> float | None:
    """최근 24h(15m ``n`` 봉) 평균 거래량 / 그 이전 봉들 평균 거래량. 이전 봉이 ``n`` 개 미만이면 None."""
    c = list(candles_15m or [])
    if len(c) < 2 * n:
        return None
    vols = [_num(x.volume) for x in c]
    if any(v is None for v in vols):
        return None
    recent, prior = vols[-n:], vols[:-n]
    pm = sum(prior) / len(prior)  # type: ignore[arg-type]
    if pm <= 0:
        return None
    return (sum(recent) / n) / pm  # type: ignore[arg-type]


def is_unconfirmed_spike(change_24h_pct: float | None, volume_multiple: float | None,
                         thresholds: Thresholds) -> bool:
    """24h +``MAX_SPIKE_24H_PCT``% 초과 급등인데 거래량 확인(배수 ≥ ``SPIKE_VOLUME_MULT``)이 없으면 True."""
    ch = _num(change_24h_pct)
    if ch is None or ch <= thresholds.MAX_SPIKE_24H_PCT:
        return False
    vm = _num(volume_multiple)
    return vm is None or vm < thresholds.SPIKE_VOLUME_MULT


def ls_percentile(current: float | None, history: Sequence[float]) -> float | None:
    """유니버스 평균 롱숏비의 백분위(0..100).

    이력이 ``LS_MIN_HISTORY`` 이상이면 이력 내 경험적 백분위(같은 값은 절반 계산),
    미만이면 임계값 선형 매핑(0.8 → 0, 2.0 → 100, 클립). 값이 없으면 None."""
    cur = _pos(current)
    if cur is None:
        return None
    hist = [v for v in (_pos(x) for x in history) if v is not None]
    if len(hist) >= LS_MIN_HISTORY:
        below = sum(1 for v in hist if v < cur)
        equal = sum(1 for v in hist if v == cur)
        return 100.0 * (below + 0.5 * equal) / len(hist)
    lo, hi = 0.8, 2.0
    return max(0.0, min(100.0, (cur - lo) / (hi - lo) * 100.0))


def liquidity_gate(symbol: str, ticker: Ticker | None, orderbook: OrderBookSummary | None,
                   volume_ratio_15m: float | None, thresholds: Thresholds) -> LiquidityGate:
    """유동성 게이트: 24h 거래대금·스프레드(bps)·1% 호가 깊이(양쪽 중 작은 쪽)·15m 거래량 비율.

    값을 모르면 통과시키지 않는다(보수적). reasons 는 기계 판독용 코드 문자열."""
    th = thresholds
    reasons: list[str] = []
    qv = _num(ticker.quote_volume_24h) if ticker is not None else None
    if qv is None:
        reasons.append("quote_volume_unknown")
    elif qv < th.MIN_QUOTE_VOLUME_24H:
        reasons.append(f"low_quote_volume:{qv:.0f}<{th.MIN_QUOTE_VOLUME_24H:.0f}")

    sp: float | None = None
    if orderbook is not None:
        s = _num(orderbook.spread_pct)
        if s is not None and s >= 0:
            sp = s * 1e4
    if sp is None and ticker is not None:
        b, a = _pos(ticker.bid), _pos(ticker.ask)
        if b is not None and a is not None and a >= b:
            sp = (a - b) / ((a + b) / 2.0) * 1e4
    if sp is None:
        reasons.append("spread_unknown")
    elif sp > th.MAX_SPREAD_BPS:
        reasons.append(f"wide_spread:{sp:.1f}bps>{th.MAX_SPREAD_BPS:g}")

    depth: float | None = None
    if orderbook is None:
        reasons.append("orderbook_unavailable")
    else:
        bd, ad = _num(orderbook.bid_depth_usdt_1pct), _num(orderbook.ask_depth_usdt_1pct)
        if bd is None or ad is None:
            reasons.append("depth_unknown")
        else:
            depth = min(bd, ad)
            if depth < th.MIN_DEPTH_1PCT_USDT:
                reasons.append(f"thin_depth:{depth:.0f}<{th.MIN_DEPTH_1PCT_USDT:.0f}")

    vr = _num(volume_ratio_15m)
    if vr is None:
        reasons.append("volume_ratio_15m_unknown")
    elif vr < th.MIN_VOL_RATIO_15M:
        reasons.append(f"low_volume_ratio_15m:{vr:.2f}<{th.MIN_VOL_RATIO_15M:g}")

    return LiquidityGate(symbol=symbol, ok=not reasons, reasons=reasons, spread_bps=sp,
                         quote_volume_24h=qv, depth_1pct_usdt=depth)


# ======================================================================= 팩터·랭킹
def compute_factors(symbol: str, fp: FeaturePack | None, flow: FlowPack | None, risk: RiskPack | None,
                    candles_by_tf: Mapping[str, Sequence[Candle]] | None = None,
                    btc_momentum_4h: float | None = None,
                    funding_history: Sequence[float] | None = None) -> dict[str, float | None]:
    """한 종목의 원시 팩터(키 8개 고정, 없는 값은 None).

    캔들이 있으면 캔들로 계산하고, 없으면 팩에서 가능한 것만 쓴다(reversal_1h ← last_closes_1h,
    rv_24h ← risk.daily_vol_pct)."""
    cb = candles_by_tf or {}
    c4h, c1d, c1h = _closes(cb.get("4h")), _closes(cb.get("1d")), _closes(cb.get("1h"))
    mom4 = momentum_pct(c4h, 24, 1) if c4h else None
    mom1d = momentum_pct(c1d, 7, 1) if c1d else None
    if c1h:
        rev = reversal_1h_pct(c1h)
        rv = rv_24h_pct(c1h)
    else:
        rev = reversal_1h_pct(fp.last_closes_1h) if fp is not None else None
        rv = None
    if rv is None and risk is not None:
        rv = _num(risk.daily_vol_pct)
    vr = None
    if fp is not None and "15m" in fp.per_tf:
        vr = _num(fp.per_tf["15m"].volume_ratio_20)
    btc_m = _num(btc_momentum_4h)
    rs = mom4 - btc_m if (mom4 is not None and btc_m is not None) else None
    fz = funding_zscore(flow.funding_now if flow is not None else None, funding_history)
    oi = _num(flow.oi_change_24h_pct) if flow is not None else None
    return {"momentum_4h": mom4, "momentum_1d": mom1d, "reversal_1h": rev, "rv_24h": rv,
            "volume_ratio": vr, "rs_vs_btc": rs, "funding_z": fz, "oi_change_24h": oi}


def rank_candidates(features: Mapping[str, FeaturePack], flows: Mapping[str, FlowPack],
                    risks: Mapping[str, RiskPack], btc_symbol: str, exclude: set[str],
                    max_candidates: int, *,
                    candles: Mapping[str, Mapping[str, Sequence[Candle]]] | None = None,
                    funding_history: Mapping[str, Sequence[float]] | None = None) -> list[Candidate]:
    """``features`` 의 종목 중 ``exclude`` 밖을 교차 z-점수 가중합(``FACTOR_WEIGHTS``)으로 순위화한다.

    - z 는 풀(제외 후 종목) 안에서 팩터별로 계산(표본 < 4 면 순위 기반). 없는 팩터는 기여 0.
    - ``candles``(심볼→tf→캔들)가 있으면 모멘텀·반전·rv 를 캔들로 계산, ``btc_symbol`` 4h 캔들로 rs_vs_btc.
    - 정렬: composite_z 내림차순, 동점이면 심볼 오름차순. 상위 ``max_candidates`` 개.
    """
    if max_candidates <= 0:
        return []
    pool = sorted(s for s in features if s not in exclude)
    if not pool:
        return []
    cands = candles or {}
    fh = funding_history or {}
    btc_c4 = _closes((cands.get(btc_symbol) or {}).get("4h"))
    btc_mom = momentum_pct(btc_c4, 24, 1) if btc_c4 else None
    raw = {s: compute_factors(s, features.get(s), flows.get(s), risks.get(s), cands.get(s),
                              btc_mom, fh.get(s)) for s in pool}
    z_by_factor = {f: zscores({s: raw[s][f] for s in pool}) for f in FACTOR_NAMES}
    scored: list[tuple[float, str, list[tuple[float, str, float]]]] = []
    for s in pool:
        contribs: list[tuple[float, str, float]] = []
        total = 0.0
        for f, w in FACTOR_WEIGHTS.items():
            z = z_by_factor[f][s]
            if z is None:
                continue
            total += w * z
            contribs.append((w * z, f, z))
        scored.append((total, s, contribs))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out: list[Candidate] = []
    n_pool = len(pool)
    for i, (total, s, contribs) in enumerate(scored[:max_candidates]):
        top = sorted(contribs, key=lambda c: (-abs(c[0]), c[1]))[:3]
        reasons = [f"rank:{i + 1}/{n_pool}"]
        reasons += [f"{f}:z={z:+.2f}:contrib={c:+.3f}" for c, f, z in top]
        missing = [f for f in FACTOR_NAMES if raw[s][f] is None]
        if missing:
            reasons.append("missing:" + ",".join(missing))
        out.append(Candidate(symbol=s, rank=i + 1, composite_z=total, factor_table=dict(raw[s]),
                             reasons=reasons))
    return out


# ======================================================================= 스캐너
@dataclass
class _SymData:
    symbol: str
    ticker: Ticker | None = None
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    orderbook: OrderBookSummary | None = None
    futures: FuturesContext | None = None
    indicators: dict[str, IndicatorSet] = field(default_factory=dict)
    quality: QualityReport | None = None
    feature_pack: FeaturePack | None = None
    flow_pack: FlowPack | None = None
    funding_prior: list[float] = field(default_factory=list)
    ok: bool = False


class Scanner:
    """코드 전용 스캐너. 상태는 4h/1d 캔들 캐시와 선물 관측 이력뿐이다."""

    def __init__(self, settings: Settings, market: MarketData, *, news_fetcher: Callable[[str], str] | None = None,
                 fear_greed_fetcher: Callable[[], int | None] | None = None, cache_dir: Path | None = None,
                 thresholds: Thresholds | None = None) -> None:
        self.settings = settings
        self.market = market
        self.news_fetcher = news_fetcher
        self.fear_greed_fetcher = fear_greed_fetcher
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.thresholds = thresholds if thresholds is not None else Thresholds.from_env()
        self._tf_cache: dict[tuple[str, str], tuple[int, list[Candle]]] = {}
        self._flow_hist: dict[str, list[dict[str, Any]]] = {}
        self._ls_hist: list[dict[str, Any]] = []
        self._load_history()

    # ------------------------------------------------------------ 이력 저장/로드
    def _history_path(self) -> Path | None:
        return self.cache_dir / HISTORY_FILE if self.cache_dir is not None else None

    def _load_history(self) -> None:
        path = self._history_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            flow = data.get("flow", {}) if isinstance(data, dict) else {}
            for sym, rows in (flow.items() if isinstance(flow, dict) else []):
                if isinstance(sym, str) and isinstance(rows, list):
                    self._flow_hist[sym] = [r for r in rows if isinstance(r, dict) and isinstance(r.get("ts"), int)]
            ls = data.get("ls", []) if isinstance(data, dict) else []
            if isinstance(ls, list):
                self._ls_hist = [r for r in ls if isinstance(r, dict) and isinstance(r.get("ts"), int)]
        except (OSError, ValueError, TypeError) as e:
            log.warning("스캐너 이력 로드 실패(무시): %s", type(e).__name__)
            self._flow_hist, self._ls_hist = {}, []

    def _save_history(self) -> None:
        path = self._history_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"flow": self._flow_hist, "ls": self._ls_hist},
                                 sort_keys=True, ensure_ascii=False)
            fd, tmp = tempfile.mkstemp(prefix=".scanner_history.", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError) as e:
            log.warning("스캐너 이력 저장 실패(무시): %s", type(e).__name__)

    # ------------------------------------------------------------ 조회
    def _fetch_candles(self, symbol: str, tf: str, now_ms: int) -> list[Candle]:
        limit = CANDLE_LIMITS[tf]
        if tf in CACHED_TIMEFRAMES:
            bucket = int(now_ms) // TF_MS[tf]
            hit = self._tf_cache.get((symbol, tf))
            if hit is not None and hit[0] == bucket:
                return hit[1]
            candles = list(self.market.fetch_ohlcv(symbol, tf, limit))
            if candles:
                self._tf_cache[(symbol, tf)] = (bucket, candles)
            return candles
        return list(self.market.fetch_ohlcv(symbol, tf, limit))

    def _evict_bad_cache(self, symbol: str, candles: Mapping[str, Sequence[Candle]], now_ms: int) -> None:
        """품질 실패 시, 자체 검사에서 문제가 있는 캐시 tf(4h/1d) 항목을 지워 다음 사이클에 다시 받게 한다."""
        for tf in CACHED_TIMEFRAMES:
            key = (symbol, tf)
            if key not in self._tf_cache:
                continue
            reasons, _ = quality_mod._check_candles(tf, list(candles.get(tf) or []), now_ms)
            if reasons:
                self._tf_cache.pop(key, None)

    def _fetch_symbol(self, symbol: str, now_ms: int, marks: dict[str, float],
                      fetched: dict[str, int], skipped: list[dict]) -> _SymData | None:
        """티커·캔들·호가·선물을 조회한다. 티커/캔들 실패면 skipped 에 기록하고 None."""
        d = _SymData(symbol=symbol)
        stage = "ticker"
        try:
            d.ticker = self.market.fetch_ticker(symbol)
            mark = _pos(d.ticker.last)
            if mark is not None:
                marks[symbol] = mark
            for tf in CANDLE_LIMITS:
                stage = f"ohlcv_{tf}"
                d.candles[tf] = self._fetch_candles(symbol, tf, now_ms)
        except Exception as e:  # 종목 단위 격리 (ExchangeError 포함)
            log.warning("%s 조회 실패(%s): %s", symbol, stage, type(e).__name__)
            skipped.append({"symbol": symbol, "reason": f"fetch_error:{stage}:{type(e).__name__}"})
            if d.candles:
                fetched[symbol] = sum(len(c) for c in d.candles.values())
            return None
        fetched[symbol] = sum(len(c) for c in d.candles.values())
        try:
            d.orderbook = self.market.fetch_orderbook_summary(symbol)
        except Exception as e:
            log.info("%s 호가 조회 실패: %s", symbol, type(e).__name__)
            d.orderbook = None
        try:
            fut = self.market.fetch_futures_context(symbol)
            d.futures = fut if isinstance(fut, FuturesContext) else None
        except Exception as e:  # 프로토콜상 예외 금지지만 방어
            log.info("%s 선물 컨텍스트 실패: %s", symbol, type(e).__name__)
            d.futures = None
        return d

    # ------------------------------------------------------------ 선물 이력
    def _record_flow(self, symbol: str, fut: FuturesContext | None, now_ms: int) -> None:
        rows = [r for r in self._flow_hist.get(symbol, [])
                if now_ms - HISTORY_KEEP_MS <= r["ts"] and r["ts"] != now_ms]
        if fut is not None:
            rows.append({"ts": int(now_ms), "funding": _num(fut.funding_rate),
                         "oi": _pos(fut.open_interest_usdt), "ls": _pos(fut.long_short_ratio)})
        rows.sort(key=lambda r: r["ts"])
        if rows:
            self._flow_hist[symbol] = rows
        else:
            self._flow_hist.pop(symbol, None)

    def _funding_prior(self, symbol: str, now_ms: int) -> list[float]:
        fn = getattr(self.market, "fetch_funding_history", None)
        if callable(fn):
            try:
                vals = [v for v in (_num(x) for x in (fn(symbol) or [])) if v is not None]
                if vals:
                    return vals
            except Exception as e:
                log.info("%s 펀딩 이력 조회 실패: %s", symbol, type(e).__name__)
        return [r["funding"] for r in self._flow_hist.get(symbol, [])
                if r["ts"] < now_ms and _num(r.get("funding")) is not None]

    def _prev_oi(self, symbol: str, now_ms: int) -> float | None:
        best: tuple[int, float] | None = None
        for r in self._flow_hist.get(symbol, []):
            age = now_ms - r["ts"]
            oi = _pos(r.get("oi"))
            if oi is None or age < PREV_OI_MIN_AGE_MS or age > PREV_OI_MAX_AGE_MS:
                continue
            dist = abs(age - PREV_OI_TARGET_MS)
            if best is None or dist < best[0]:
                best = (dist, oi)
        return best[1] if best is not None else None

    # ------------------------------------------------------------ 보조 계산
    def _equity_estimate(self, state: BotState, marks: Mapping[str, float]) -> float:
        eq = _num(state.paper_quote_balance) or 0.0
        for sym, pos in state.positions.items():
            px = marks.get(sym) or _pos(pos.last_mark) or _pos(pos.entry_price) or 0.0
            eq += max(0.0, _num(pos.base_amount) or 0.0) * px
        if eq <= 0:
            eq = float(self.settings.paper_start_balance)
        return eq

    def _fear_greed(self) -> int | None:
        if self.fear_greed_fetcher is None:
            return None
        try:
            v = _num(self.fear_greed_fetcher())
        except Exception as e:
            log.info("공포·탐욕 지수 조회 실패: %s", type(e).__name__)
            return None
        if v is None or not 0 <= v <= 100:
            return None
        return int(round(v))

    def _fetch_news(self, symbols: Sequence[str]) -> list[NewsItem]:
        if self.news_fetcher is None:
            log.debug("news_fetcher 없음 → 뉴스 조회 생략")
            return []
        cache = self.cache_dir / NEWS_CACHE_FILE if self.cache_dir is not None else None
        try:
            return list(news_mod.fetch_news(self.settings.news_feeds, symbols,
                                            fetcher=self.news_fetcher, cache_path=cache))
        except Exception as e:  # fetch_news 는 예외를 던지지 않지만 방어
            log.warning("뉴스 조회 실패: %s", type(e).__name__)
            return []

    # ------------------------------------------------------------ 본체
    def scan(self, state: BotState, now_ms: int, *, headline_polarity: float | None = None,
             planned_notional: float | None = None) -> ScanResult:
        """한 사이클 스캔. 어떤 종목 실패도 전파하지 않는다.

        ``headline_polarity``: news_sentiment 결과(−1..1). 없으면 None(종합심리에서 빠짐).
        ``planned_notional``: 리스크 팩 충격 추정용 계획 주문액. 없으면 추정 자본×MAX_POSITION_PCT.
        """
        now_ms = int(now_ms)
        th = self.thresholds
        whitelist = list(self.settings.symbols)
        held = set(state.positions)
        universe = whitelist + sorted(s for s in held if s not in whitelist)
        fetch_list = universe + ([BTC_SYMBOL] if BTC_SYMBOL not in universe else [])

        skipped: list[dict] = []
        marks: dict[str, float] = {}
        fetched: dict[str, int] = {}
        qualities: dict[str, QualityReport] = {}
        ok: dict[str, _SymData] = {}
        # 품질 전체는 실패했지만 1h 캔들 자체는 정상인 보유 종목(상관 계산용; G1 상관 차단이 조용히 빠지지 않게)
        held_valid_1h: dict[str, list[Candle]] = {}

        # 1~3) 조회·품질·피처팩
        for sym in fetch_list:
            d = self._fetch_symbol(sym, now_ms, marks, fetched, skipped)
            if d is None:
                continue
            try:
                q = quality_mod.check(sym, d.candles, d.ticker, now_ms, self.settings.cycle_minutes)  # type: ignore[arg-type]
            except Exception as e:  # pragma: no cover - quality.check 는 예외를 던지지 않음
                q = QualityReport(symbol=sym, ok=False, reasons=[f"quality_error:{type(e).__name__}"])
            qualities[sym] = q
            d.quality = q
            if not q.ok:
                skipped.append({"symbol": sym, "reason": "quality:" + ";".join(q.reasons)})
                self._evict_bad_cache(sym, d.candles, now_ms)
                if sym in held:
                    c1h = d.candles.get("1h") or []
                    if c1h and not quality_mod._check_candles("1h", list(c1h), now_ms)[0]:
                        held_valid_1h[sym] = list(c1h)
                continue
            try:
                for tf, cs in d.candles.items():
                    d.indicators[tf] = ind_mod.compute(sym, tf, cs)
                d.feature_pack = build_feature_pack(sym, d.candles, d.indicators, d.ticker)  # type: ignore[arg-type]
                self._record_flow(sym, d.futures, now_ms)
                d.funding_prior = self._funding_prior(sym, now_ms)
                hist_now = d.funding_prior + ([_num(d.futures.funding_rate)] if d.futures is not None
                                              and _num(d.futures.funding_rate) is not None else [])
                d.flow_pack = build_flow_pack(sym, d.futures, hist_now, d.orderbook, d.ticker,  # type: ignore[arg-type]
                                              None, d.candles.get("1h", []), self._prev_oi(sym, now_ms))
            except Exception as e:
                log.warning("%s 피처 계산 실패: %s", sym, type(e).__name__)
                skipped.append({"symbol": sym, "reason": f"feature_error:{type(e).__name__}"})
                continue
            d.ok = True
            ok[sym] = d

        # 레짐 (BTC 실패 시 btc 라벨 unknown, macro_alarm 불가)
        btc = ok.get(BTC_SYMBOL)
        regime = build_regime_pack({s: d.candles.get("1h", []) for s, d in ok.items()}, BTC_SYMBOL,
                                   btc.indicators.get("4h") if btc else None,
                                   btc.indicators.get("1h") if btc else None)
        if btc is None:
            # SPEC §18: BTC 데이터 실패 시 verdict="normal" + unknown 라벨
            if regime.verdict != "normal":
                regime = regime.model_copy(update={"verdict": "normal"})
            log.warning("BTC 데이터 없음 → 레짐 BTC 라벨 unknown, verdict normal")

        # 4) 유동성·제외
        liquidity: dict[str, LiquidityGate] = {}
        exclude: set[str] = set()
        name_alarms = set(regime.name_alarm_symbols)
        for sym in universe:
            d = ok.get(sym)
            if d is None:
                continue
            fp = d.feature_pack
            vr = fp.per_tf["15m"].volume_ratio_20 if fp is not None and "15m" in fp.per_tf else None
            gate = liquidity_gate(sym, d.ticker, d.orderbook, vr, th)
            liquidity[sym] = gate
            reason = self._exclusion_reason(state, sym, whitelist, gate, d, name_alarms)
            if reason is not None:
                exclude.add(sym)
                skipped.append({"symbol": sym, "reason": reason})

        funding_window = in_funding_window(now_ms, th.FUNDING_WINDOW_S)

        # 랭킹
        pool_syms = [s for s in universe if s in ok]
        candidates = rank_candidates(
            {s: ok[s].feature_pack for s in pool_syms},  # type: ignore[misc]
            {s: ok[s].flow_pack for s in pool_syms},  # type: ignore[misc]
            {}, BTC_SYMBOL, exclude, int(self.settings.max_candidates),
            candles={s: d.candles for s, d in ok.items()},
            funding_history={s: d.funding_prior for s, d in ok.items()},
        )

        # 6) 레짐 게이트
        if regime.verdict == "macro_alarm":
            log.warning("레짐 macro_alarm → 후보 없음")
            dropped = [c.symbol for c in candidates] or [s for s in pool_syms if s not in exclude]
            for s in dropped:
                skipped.append({"symbol": s, "reason": "macro_alarm"})
            if not dropped:
                skipped.append({"symbol": "*", "reason": "macro_alarm"})
            candidates = []

        # 5) 팩(최종 후보 + 보유)
        pack_syms = [c.symbol for c in candidates]
        pack_syms += [s for s in universe if s in held and s in ok and s not in pack_syms]
        planned = _pos(planned_notional)
        if planned is None:
            planned = self._equity_estimate(state, marks) * HardLimits.MAX_POSITION_PCT
        btc_1h = btc.candles.get("1h", []) if btc else []
        open_1h = {s: (ok[s].candles.get("1h", []) if s in ok else held_valid_1h[s])
                   for s in sorted(held) if s in ok or s in held_valid_1h}
        feature_packs: dict[str, FeaturePack] = {}
        flow_packs: dict[str, FlowPack] = {}
        risk_packs: dict[str, RiskPack] = {}
        for s in pack_syms:
            d = ok[s]
            assert d.feature_pack is not None and d.flow_pack is not None
            try:
                rp = build_risk_pack(s, d.candles.get("1h", []), btc_1h, open_1h, d.orderbook, d.ticker,  # type: ignore[arg-type]
                                     planned, d.feature_pack.atr_pct_1h)
            except Exception as e:
                log.warning("%s 리스크 팩 실패: %s", s, type(e).__name__)
                skipped.append({"symbol": s, "reason": f"risk_pack_error:{type(e).__name__}"})
                candidates = [c for c in candidates if c.symbol != s]
                continue
            feature_packs[s] = d.feature_pack
            flow_packs[s] = d.flow_pack
            risk_packs[s] = rp
        # 랭크는 최종 후보 순서대로 1..n 로 다시 매긴다(제외로 빈 자리 없음)
        candidates = [c.model_copy(update={"rank": i + 1}) for i, c in enumerate(candidates)]

        # 종합심리
        sentiment = self._sentiment(ok, universe, now_ms, headline_polarity)

        # 뉴스
        news_items = self._fetch_news(universe)

        self._save_history()

        result = ScanResult(
            ts=now_ms, universe=universe, candidates=candidates, skipped=skipped, regime=regime,
            sentiment=sentiment, funding_window=funding_window, feature_packs=feature_packs,
            flow_packs=flow_packs, risk_packs=risk_packs, liquidity=liquidity, quality=qualities,
            news=news_items, news_available=bool(news_items), marks=marks, fetched=fetched,
        )
        log.info("스캔 완료: 유니버스 %d, 정상 %d, 후보 %s, 제외 %d, 레짐 %s, 펀딩창 %s, 뉴스 %d",
                 len(universe), len(ok), [c.symbol for c in candidates], len(skipped), regime.verdict,
                 funding_window, len(news_items))
        return result

    def _exclusion_reason(self, state: BotState, sym: str, whitelist: Sequence[str], gate: LiquidityGate,
                          d: _SymData, name_alarms: set[str]) -> str | None:
        """후보 제외 사유(첫 번째). 없으면 None. 쿨다운·복수매매 창은 ``state.cycle <= until`` 동안."""
        if sym in state.positions:
            return "held"
        if sym not in whitelist:
            return "not_whitelisted"
        until = state.cooldown_until_cycle.get(sym)
        if until is not None and state.cycle <= until:
            return f"cooldown:until_cycle={until}"
        loss_until = state.loss_cooldown_until_cycle.get(sym)
        if loss_until is not None and state.cycle <= loss_until:
            return f"revenge_window:until_cycle={loss_until}"
        if not gate.ok:
            return "liquidity:" + ";".join(gate.reasons)
        fp = d.feature_pack
        ch = fp.change_24h_pct if fp is not None else None
        vm = volume_24h_multiple(d.candles.get("15m", []))
        if is_unconfirmed_spike(ch, vm, self.thresholds):
            vm_txt = f"{vm:.2f}" if vm is not None else "unknown"
            return f"spike_24h:{ch:.1f}%>{self.thresholds.MAX_SPIKE_24H_PCT:g}:volume_mult={vm_txt}"
        if sym in name_alarms:
            return "name_alarm"
        return None

    def _sentiment(self, ok: Mapping[str, _SymData], universe: Sequence[str], now_ms: int,
                   headline_polarity: float | None) -> SentimentComposite:
        """종합심리: fear_greed(선택) + 유니버스 펀딩 z 평균 + 유니버스 평균 롱숏비 백분위 + 헤드라인 극성."""
        zs: list[float] = []
        ls_vals: list[float] = []
        for sym in universe:
            d = ok.get(sym)
            if d is None or d.futures is None:
                continue
            z = funding_zscore(d.futures.funding_rate, d.funding_prior)
            if z is not None:
                zs.append(z)
            ls = _pos(d.futures.long_short_ratio)
            if ls is not None:
                ls_vals.append(ls)
        funding_z = sum(zs) / len(zs) if zs else None
        ls_now = sum(ls_vals) / len(ls_vals) if ls_vals else None
        prior_ls = [r["ls"] for r in self._ls_hist if r["ts"] < now_ms and _pos(r.get("ls")) is not None]
        ls_pctile = ls_percentile(ls_now, prior_ls)
        self._ls_hist = [r for r in self._ls_hist if now_ms - HISTORY_KEEP_MS <= r["ts"] and r["ts"] != now_ms]
        if ls_now is not None:
            self._ls_hist.append({"ts": now_ms, "ls": ls_now})
        self._ls_hist.sort(key=lambda r: r["ts"])
        hp = _num(headline_polarity)
        if hp is not None:
            hp = max(-1.0, min(1.0, hp))
        return composite_sentiment(self._fear_greed(), funding_z, ls_pctile, hp)
