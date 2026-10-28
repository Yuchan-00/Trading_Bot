"""규칙 전용 기준선 백테스트 (SPEC §13). LLM 호출 없음.

목적
- 에이전트 파이프라인을 붙이기 전에 "코드가 강제하는 리스크 규칙 + 단순 추세 규칙"만으로
  어느 정도 성과가 나는지 기준선을 만든다. 결과가 ``VALIDATED`` 가 아니면 규칙 자체가 아니라
  데이터/비용/표본 수부터 의심해야 한다.

선행편향 금지(불변식)
- 신호는 봉 t **종가** 기준 지표로만 만든다(봉 t 까지의 캔들만 입력). 상위 시간대(1h/4h)는
  봉 t 종료 시각 이전에 **완료된** 봉만 쓴다.
- 체결은 봉 t+1 **시가** × (1+slippage). 봉 t+1 의 다른 값은 체결가에 영향을 주지 않는다.
- 손절/익절은 봉의 저가/고가로 판정하고, 한 봉에서 둘 다 닿으면 **손절 우선**(보수적).
  시가가 손절 아래로 갭이면 시가에 체결된 것으로 본다(더 나쁜 쪽). 익절 갭은 익절가로만 인정.
- 시간손절은 ``HardLimits.TIME_STOP_CYCLES``(96봉). 종목당 1포지션, 동시 최대 ``MAX_POSITIONS``(3).
- 자본은 매 봉 종가로 평가(mark)한다.

손절/익절/사이징은 ``risk.default_stops`` → ``clamp_stops`` → ``enforce_net_rr`` → ``size_position``
을 그대로 쓴다(실거래와 같은 경로). 청산 사유 분류는 ``risk.check_exits`` 를 저가/고가에 각각 적용한다.

단위: 금액 USDT float, 비율은 소수(0.02 = 2%). 시각은 epoch ms.
지표 창: 실거래 스캐너와 같게 기본 시간대 300봉, 1h/4h 200봉(``WINDOW_BARS``).

데이터 품질
- 비유한(NaN/inf) 값이 있는 봉은 **죽이지 않는다**: 그 봉에서는 신호 HOLD·체결/청산 판정 건너뜀·
  직전 유한 종가로 평가(mark)한다. 결과 카드는 항상 표준 JSON(NaN/Infinity 없음)이다.
- 거래소가 빈 목록을 돌려주면 캐시에 저장하지 않고, 빈 캐시 파일은 캐시 미스로 본다(일시 장애가 영구화되지 않게).
- 실제 데이터 기간(``metrics.covered_days``)이 요청 ``days`` 의 ``VERDICT_MIN_COVERAGE_RATIO`` 미만이면
  판정 사유 ``data_coverage`` 를 남긴다(짧은 표본의 VALIDATED 를 90일 검증으로 오해하지 않게).
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import random
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from tradingbot import indicators
from tradingbot.config import HardLimits
from tradingbot.models import Candle, ClosedTrade, ExitReason, IndicatorSet, Position
from tradingbot.risk import check_exits, clamp_stops, default_stops, enforce_net_rr, size_position

log = logging.getLogger("tradingbot.backtest")

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "SegmentResult",
    "WINDOW_BARS",
    "RULE_CONFIDENCE",
    "MC_ITERATIONS",
    "MC_SEED",
    "VERDICT_MIN_TRADES",
    "VERDICT_MIN_SEGMENT_PF",
    "VERDICT_MAX_MDD",
    "VERDICT_MIN_STRESS_PF",
    "VERDICT_MIN_COVERAGE_RATIO",
    "rule_signal",
    "signal_at",
    "run",
    "resample",
    "timeframe_ms",
    "compute_metrics",
    "max_drawdown",
    "sharpe_from_curve",
    "monte_carlo",
]

Action = Literal["BUY", "HOLD"]

#: 기본 규칙의 확신도(사이징 티어 0.65~0.75 → 상한의 40%).
RULE_CONFIDENCE: float = 0.7
#: 지표 계산 창(봉 수). 실거래 스캐너의 조회 수와 같다. 기본 시간대는 "base" 키.
WINDOW_BARS: dict[str, int] = {"base": 300, "1h": 200, "4h": 200}
#: 상위 시간대(신호·ATR 손절에 사용).
HIGHER_TIMEFRAMES: tuple[str, ...] = ("1h", "4h")
#: 몬테카를로 설정.
MC_ITERATIONS: int = 1000
MC_SEED: int = 42
#: 판정 기준.
VERDICT_MIN_TRADES: int = 30
VERDICT_MIN_SEGMENT_PF: float = 1.1
VERDICT_MAX_MDD: float = 0.10
VERDICT_MIN_STRESS_PF: float = 1.0
#: 실제 데이터 기간 / 요청 days 가 이 비율 미만이면 판정 사유(data_coverage).
VERDICT_MIN_COVERAGE_RATIO: float = 0.5
WALK_FORWARD_SEGMENTS: int = 3
#: 손실이 0인데 이익이 있을 때의 PF 표기값(무한대 대신; JSON 호환).
PF_CAP: float = 999.0

#: 1h ATR% 허용 범위(기본 규칙).
ATR_PCT_MIN: float = 0.003
ATR_PCT_MAX: float = 0.03
RSI_MIN: float = 45.0
RSI_MAX: float = 65.0

_TF_RE = re.compile(r"^(\d+)([mhdw])$")
_TF_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_MS_PER_DAY = 86_400_000


# ---------------------------------------------------------------- 설정/결과
@dataclass
class BacktestConfig:
    symbols: list[str]
    timeframe: str = "15m"
    days: int = 90
    fee_pct: float = 0.001
    slippage_pct: float = 0.0005
    start_balance: float = 10000.0
    cache_dir: Path = Path("backtest_cache")
    out_dir: Path = Path("backtest_results")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cache_dir"] = str(self.cache_dir)
        d["out_dir"] = str(self.out_dir)
        return d


@dataclass
class SegmentResult:
    """워크포워드 구간(또는 전체) 성과."""

    index: int
    start_ts: int
    end_ts: int
    bars: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    net_return: float
    max_drawdown: float
    start_equity: float
    end_equity: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    run_ts: int
    config: dict
    verdict: Literal["VALIDATED", "NOT VALIDATED"]
    reasons: list[str]
    metrics: dict
    buy_and_hold: dict[str, float]
    walk_forward: dict
    monte_carlo: dict
    stress: dict
    trades: list[ClosedTrade]
    equity_curve: list[tuple[int, float]]
    json_path: Path | None = None
    md_path: Path | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_ts": self.run_ts,
            "config": self.config,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "buy_and_hold": dict(self.buy_and_hold),
            "walk_forward": self.walk_forward,
            "monte_carlo": dict(self.monte_carlo),
            "stress": dict(self.stress),
            "trades": [t.model_dump() for t in self.trades],
            "equity_curve": [[int(ts), float(eq)] for ts, eq in self.equity_curve],
            "notes": list(self.notes),
            "json_path": str(self.json_path) if self.json_path else None,
            "md_path": str(self.md_path) if self.md_path else None,
        }


# ---------------------------------------------------------------- 시간대 도우미
def timeframe_ms(timeframe: str) -> int:
    """'15m'/'1h'/'4h'/'1d' → ms. 형식이 다르면 ValueError."""
    m = _TF_RE.match(str(timeframe).strip())
    if not m:
        raise ValueError(f"알 수 없는 시간대: {timeframe!r}")
    n = int(m.group(1))
    if n <= 0:
        raise ValueError(f"알 수 없는 시간대: {timeframe!r}")
    return n * _TF_UNIT_MS[m.group(2)]


def resample(candles: Sequence[Candle], timeframe: str) -> list[Candle]:
    """하위 시간대 캔들을 ``timeframe`` 으로 합친다. **완료된** 버킷만 돌려준다.

    버킷 = ts // tf_ms. 마지막 하위 봉의 종료 시각이 버킷 종료와 같아야 완료로 본다.
    입력은 ts 오름차순·중복 없음이어야 한다(정렬해서 쓴다).
    """
    tf = timeframe_ms(timeframe)
    if not candles:
        return []
    src = sorted(candles, key=lambda c: c.ts)
    # 하위 봉 길이 추정: 최소 양의 ts 간격
    gaps = [b.ts - a.ts for a, b in zip(src, src[1:]) if b.ts > a.ts]
    sub_ms = min(gaps) if gaps else tf
    if sub_ms > tf:
        raise ValueError(f"입력 봉({sub_ms}ms)이 목표 시간대({timeframe})보다 길다")
    out: list[Candle] = []
    bucket: int | None = None
    o = h = l = c = 0.0
    v = 0.0
    last_end = 0
    for cd in src:
        b = int(cd.ts) // tf
        if b != bucket:
            if bucket is not None and last_end >= (bucket + 1) * tf:
                out.append(Candle(ts=bucket * tf, open=o, high=h, low=l, close=c, volume=v))
            bucket = b
            o, h, l, c, v = cd.open, cd.high, cd.low, cd.close, cd.volume
        else:
            h = max(h, cd.high)
            l = min(l, cd.low)
            c = cd.close
            v += cd.volume
        last_end = int(cd.ts) + sub_ms
    if bucket is not None and last_end >= (bucket + 1) * tf:
        out.append(Candle(ts=bucket * tf, open=o, high=h, low=l, close=c, volume=v))
    return out


def _sorted_unique(candles: Sequence[Candle]) -> list[Candle]:
    seen: dict[int, Candle] = {}
    for c in candles:
        seen[int(c.ts)] = c
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------- 규칙 신호
def rule_signal(
    ind15: IndicatorSet, ind1h: IndicatorSet | None, ind4h: IndicatorSet | None
) -> tuple[Action, float, str]:
    """기준선 규칙. 모든 조건을 만족하면 ("BUY", 0.7, 사유), 아니면 ("HOLD", 0.0, 사유).

    - 4h: ema_50 > ema_200 (추세)
    - 15m: close > ema_20, 45 ≤ rsi_14 ≤ 65, macd_hist > 0, volume_ratio_20 > 1.0
    - 1h: 0.3% ≤ atr_pct ≤ 3%
    지표가 하나라도 None 이면 HOLD. 비유한 값도 None 과 같이 취급한다.
    """
    if ind15 is None:
        return "HOLD", 0.0, "15m 지표 없음 (no_ind15)"
    if ind4h is None:
        return "HOLD", 0.0, "4h 지표 없음 (no_ind4h)"
    if ind1h is None:
        return "HOLD", 0.0, "1h 지표 없음 (no_ind1h)"

    needed = {
        "4h.ema_50": ind4h.ema_50,
        "4h.ema_200": ind4h.ema_200,
        "15m.close": ind15.close,
        "15m.ema_20": ind15.ema_20,
        "15m.rsi_14": ind15.rsi_14,
        "15m.macd_hist": ind15.macd_hist,
        "15m.volume_ratio_20": ind15.volume_ratio_20,
        "1h.atr_pct": ind1h.atr_pct,
    }
    missing = [k for k, v in needed.items() if v is None or not _finite(v)]
    if missing:
        return "HOLD", 0.0, f"지표 미계산 (warmup): {', '.join(missing)}"

    if not ind4h.ema_50 > ind4h.ema_200:  # type: ignore[operator]
        return "HOLD", 0.0, "4h 추세 아님 (4h ema_50 ≤ ema_200)"
    if not ind15.close > ind15.ema_20:  # type: ignore[operator]
        return "HOLD", 0.0, "15m 종가 ≤ ema_20"
    rsi = float(ind15.rsi_14)  # type: ignore[arg-type]
    if not (RSI_MIN <= rsi <= RSI_MAX):
        return "HOLD", 0.0, f"15m rsi_14 {rsi:.1f} 범위 밖 ({RSI_MIN:.0f}~{RSI_MAX:.0f})"
    if not ind15.macd_hist > 0:  # type: ignore[operator]
        return "HOLD", 0.0, "15m macd_hist ≤ 0"
    if not ind15.volume_ratio_20 > 1.0:  # type: ignore[operator]
        return "HOLD", 0.0, f"15m volume_ratio_20 {ind15.volume_ratio_20:.2f} ≤ 1.0"
    atr_pct = float(ind1h.atr_pct)  # type: ignore[arg-type]
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return "HOLD", 0.0, f"1h atr_pct {atr_pct:.4f} 범위 밖 ({ATR_PCT_MIN}~{ATR_PCT_MAX})"
    return (
        "BUY",
        RULE_CONFIDENCE,
        f"4h 추세↑, 15m 종가>ema20, rsi {rsi:.1f}, macd_hist>0, 거래량비 {ind15.volume_ratio_20:.2f}, "
        f"1h atr {atr_pct:.3%} (rule_buy)",
    )


def _finite(x: Any) -> bool:
    try:
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _candle_ok(c: Candle) -> bool:
    """시가/고가/저가/종가가 모두 유한한 양수인 봉만 체결·청산·평가에 쓴다."""
    return all(_finite(v) and v > 0 for v in (c.open, c.high, c.low, c.close))


# ---------------------------------------------------------------- 종목 데이터(선행편향 차단)
class _SymbolData:
    """한 종목의 기본/상위 시간대 캔들과 "봉 k 시점" 지표·신호(메모이즈)."""

    def __init__(self, symbol: str, timeframe: str, candles_by_tf: dict[str, list[Candle]]):
        self.symbol = symbol
        self.timeframe = timeframe
        self.tf_ms = timeframe_ms(timeframe)
        base = candles_by_tf.get(timeframe)
        if not base:
            raise ValueError(f"{symbol}: 기본 시간대 {timeframe} 캔들이 없음")
        self.base: list[Candle] = _sorted_unique(base)
        self.index_by_ts: dict[int, int] = {int(c.ts): i for i, c in enumerate(self.base)}
        self.higher: dict[str, list[Candle]] = {}
        self.higher_ends: dict[str, list[int]] = {}
        for tf in HIGHER_TIMEFRAMES:
            src = candles_by_tf.get(tf)
            if src:
                cs = _sorted_unique(src)
            else:
                cs = resample(self.base, tf)
                log.info("%s: %s 캔들이 없어 %s 에서 합성 (%d봉)", symbol, tf, timeframe, len(cs))
            self.higher[tf] = cs
            ms = timeframe_ms(tf)
            self.higher_ends[tf] = [int(c.ts) + ms for c in cs]
        self._ind_cache: dict[tuple[str, int], IndicatorSet | None] = {}
        self._base_cache: dict[int, IndicatorSet | None] = {}
        self._signal_cache: dict[int, tuple[Action, float, str, float | None]] = {}
        self.bad_bars: int = sum(1 for c in self.base if not _candle_ok(c))
        if self.bad_bars:
            log.warning("%s: 비유한/비양수 값이 있는 %s 봉 %d개 — 해당 봉은 HOLD·건너뜀 처리", symbol, timeframe, self.bad_bars)

    # -- 지표
    def base_indicators(self, i: int) -> IndicatorSet | None:
        """기본 시간대 봉 i 종가 기준 지표 (봉 0..i 중 마지막 WINDOW 봉). 종가가 비유한이면 None."""
        if i in self._base_cache:
            return self._base_cache[i]
        w = WINDOW_BARS.get("base", 300)
        window = self.base[max(0, i - w + 1): i + 1]
        try:
            ind: IndicatorSet | None = indicators.compute(self.symbol, self.timeframe, window)
        except ValueError as e:
            log.debug("%s: 봉 %d 지표 계산 불가 → HOLD: %s", self.symbol, i, e)
            ind = None
        self._base_cache[i] = ind
        return ind

    def higher_indicators(self, tf: str, i: int) -> IndicatorSet | None:
        """봉 i 종료 시각 이전에 **완료된** 상위 봉만으로 계산한 지표. 없으면 None."""
        close_time = int(self.base[i].ts) + self.tf_ms
        ends = self.higher_ends[tf]
        n = bisect.bisect_right(ends, close_time)
        if n == 0:
            return None
        key = (tf, n)
        if key in self._ind_cache:
            return self._ind_cache[key]
        w = WINDOW_BARS.get(tf, 200)
        window = self.higher[tf][max(0, n - w): n]
        try:
            ind: IndicatorSet | None = indicators.compute(self.symbol, tf, window)
        except ValueError as e:
            log.debug("%s: %s 봉 %d 지표 계산 불가 → None: %s", self.symbol, tf, n - 1, e)
            ind = None
        self._ind_cache[key] = ind
        return ind

    def indicators_at(self, i: int) -> tuple[IndicatorSet | None, IndicatorSet | None, IndicatorSet | None]:
        return (
            self.base_indicators(i),
            self.higher_indicators("1h", i),
            self.higher_indicators("4h", i),
        )

    # -- 신호
    def signal_at(self, i: int) -> tuple[Action, float, str, float | None]:
        """봉 i 종가 기준 (action, confidence, reason, atr_1h). 비용과 무관하므로 메모이즈."""
        cached = self._signal_cache.get(i)
        if cached is not None:
            return cached
        ind15, ind1h, ind4h = self.indicators_at(i)
        if ind15 is None:
            out: tuple[Action, float, str, float | None] = ("HOLD", 0.0, "15m 봉 값 비유한 (bad_candle)", None)
            self._signal_cache[i] = out
            return out
        action, conf, reason = rule_signal(ind15, ind1h, ind4h)
        atr_1h = ind1h.atr_14 if ind1h is not None else None
        if atr_1h is not None and not _finite(atr_1h):
            atr_1h = None
        out = (action, conf, reason, atr_1h)
        self._signal_cache[i] = out
        return out


def signal_at(
    candles_by_tf: dict[str, list[Candle]], index: int, timeframe: str = "15m", symbol: str = "X/USDT"
) -> tuple[Action, float, str]:
    """기본 시간대 봉 ``index`` 종가 기준 규칙 신호. 봉 index 이후의 데이터는 보지 않는다(테스트용 공개 함수)."""
    data = _SymbolData(symbol, timeframe, candles_by_tf)
    if index < 0 or index >= len(data.base):
        raise IndexError(f"index {index} 범위 밖 (봉 {len(data.base)}개)")
    action, conf, reason, _ = data.signal_at(index)
    return action, conf, reason


# ---------------------------------------------------------------- 시뮬레이션
@dataclass
class _OpenPos:
    position: Position
    entry_index: int
    base_amount: float
    quote_spent: float
    fee_buy: float
    equity_before: float


@dataclass
class _SimOutput:
    trades: list[ClosedTrade]
    equity_curve: list[tuple[int, float]]
    trade_returns: list[float]  # 거래별 pnl / 진입 직전 자본
    timeline: list[int]
    skipped: dict[str, int]


def _simulate(
    data: dict[str, _SymbolData],
    timeline: list[int],
    *,
    start_balance: float,
    fee_pct: float,
    slippage_pct: float,
) -> _SimOutput:
    """공통 타임라인 위에서 종목들을 함께 시뮬레이션한다(현금·동시 포지션 수 공유)."""
    cash = float(start_balance)
    positions: dict[str, _OpenPos] = {}
    pending: dict[str, tuple[float, str, float | None]] = {}
    last_close: dict[str, float] = {}
    trades: list[ClosedTrade] = []
    curve: list[tuple[int, float]] = []
    trade_returns: list[float] = []
    skipped: dict[str, int] = {"rr_impossible": 0, "size_zero": 0, "max_positions": 0, "bad_candle": 0}
    equity_prev = cash
    max_pos = HardLimits.MAX_POSITIONS
    symbols = list(data.keys())

    for k, ts in enumerate(timeline):
        # a) 전 봉 신호 → 이번 봉 시가 체결
        for sym in symbols:
            if sym not in pending:
                continue
            sd = data[sym]
            i = sd.index_by_ts.get(ts)
            if i is None:
                continue  # 이 종목은 이번 시각에 봉이 없음 → 다음 봉에서 체결
            conf, reason, atr_1h = pending.pop(sym)
            if sym in positions:
                continue
            if len(positions) >= max_pos:
                skipped["max_positions"] += 1
                continue
            c = sd.base[i]
            if not _candle_ok(c):
                skipped["bad_candle"] += 1
                continue
            entry = float(c.open) * (1.0 + slippage_pct)
            if not _finite(entry) or entry <= 0:
                continue
            sl, tp = default_stops(entry, atr_1h)
            sl, tp = clamp_stops(entry, sl, tp)
            sl, tp, note = enforce_net_rr(entry, sl, tp)
            if "rr_impossible" in note:
                skipped["rr_impossible"] += 1
                continue
            free_quote = cash / (1.0 + fee_pct)
            quote, _notes = size_position(equity_prev, entry, sl, conf, 1.0, free_quote)
            if quote <= 0:
                skipped["size_zero"] += 1
                continue
            fee_buy = quote * fee_pct
            cash -= quote + fee_buy
            base_amount = quote / entry
            pos = Position(
                symbol=sym,
                base_amount=base_amount,
                entry_price=entry,
                quote_spent=quote,
                fee_quote=fee_buy,
                stop_loss_price=sl,
                take_profit_price=tp,
                opened_cycle=k,
                opened_ts=int(c.ts),
                thesis=reason,
                confidence=conf,
                entry_order_id=None,
                last_mark=entry,
            )
            positions[sym] = _OpenPos(pos, k, base_amount, quote, fee_buy, equity_prev)

        # b) 청산 판정: 저가 → 손절 우선, 고가 → 익절, 보유 봉 수 → 시간손절
        for sym in list(positions):
            op = positions[sym]
            sd = data[sym]
            i = sd.index_by_ts.get(ts)
            if i is None:
                continue
            c = sd.base[i]
            if not _candle_ok(c):
                continue  # 손상 봉: 가격 판정 불가 → 다음 봉에서 판정(시간손절 포함)
            pos = op.position
            reason: ExitReason | None = None
            r_low = check_exits(pos, float(c.low), k)
            if r_low == "stop_loss":
                reason = "stop_loss"
                exit_px = min(pos.stop_loss_price, float(c.open)) * (1.0 - slippage_pct)
            else:
                r_high = check_exits(pos, float(c.high), k)
                if r_low == "take_profit" or r_high == "take_profit":
                    reason = "take_profit"
                    exit_px = pos.take_profit_price * (1.0 - slippage_pct)
                elif r_low == "time_stop" or r_high == "time_stop":
                    reason = "time_stop"
                    exit_px = float(c.close) * (1.0 - slippage_pct)
            if reason is None:
                continue
            proceeds = op.base_amount * exit_px
            fee_sell = proceeds * fee_pct
            cash += proceeds - fee_sell
            received = proceeds - fee_sell
            pnl = received - (op.quote_spent + op.fee_buy)
            trade = ClosedTrade(
                symbol=sym,
                base_amount=op.base_amount,
                entry_price=pos.entry_price,
                exit_price=exit_px,
                quote_spent=op.quote_spent,
                quote_received=received,
                fee_quote_total=op.fee_buy + fee_sell,
                pnl_quote=pnl,
                pnl_pct=pnl / op.quote_spent if op.quote_spent > 0 else 0.0,
                reason=reason,
                opened_cycle=op.entry_index,
                closed_cycle=k,
                opened_ts=pos.opened_ts,
                closed_ts=int(c.ts),
                thesis=pos.thesis,
                confidence=pos.confidence,
                exit_order_id=None,
            )
            trades.append(trade)
            trade_returns.append(pnl / op.equity_before if op.equity_before > 0 else 0.0)
            del positions[sym]

        # c) 종가 평가 (비유한 종가는 직전 유한 종가 유지)
        for sym in symbols:
            i = data[sym].index_by_ts.get(ts)
            if i is not None:
                close = data[sym].base[i].close
                if _finite(close) and close > 0:
                    last_close[sym] = float(close)
        equity = cash + sum(op.base_amount * last_close.get(sym, op.position.entry_price) for sym, op in positions.items())
        curve.append((int(ts), equity))
        equity_prev = equity

        # d) 봉 종가 기준 신호 → 다음 봉 체결 예약
        if len(positions) + len(pending) >= max_pos:
            continue
        for sym in symbols:
            if sym in positions or sym in pending:
                continue
            sd = data[sym]
            i = sd.index_by_ts.get(ts)
            if i is None:
                continue
            action, conf, reason, atr_1h = sd.signal_at(i)
            if action == "BUY":
                pending[sym] = (conf, reason, atr_1h)
                if len(positions) + len(pending) >= max_pos:
                    break

    return _SimOutput(trades, curve, trade_returns, timeline, skipped)


# ---------------------------------------------------------------- 지표(성과)
def max_drawdown(curve: Sequence[float]) -> float:
    """최고점 대비 최대 낙폭(소수). 빈 입력은 0."""
    peak = -math.inf
    mdd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def sharpe_from_curve(curve: Sequence[tuple[int, float]], periods_per_year: float = 365.0) -> float:
    """일봉 근사 샤프: UTC 일 마지막 자본으로 일수익률을 만들고 mean/std*sqrt(365). 표본 부족·분산 0이면 0."""
    if not curve:
        return 0.0
    daily: dict[int, float] = {}
    for ts, eq in curve:
        daily[int(ts) // _MS_PER_DAY] = float(eq)
    eqs = [daily[d] for d in sorted(daily)]
    rets = [b / a - 1.0 for a, b in zip(eqs, eqs[1:]) if a > 0]
    if len(rets) < 2:
        return 0.0
    mean = statistics.fmean(rets)
    std = statistics.stdev(rets)
    if std <= 0 or not math.isfinite(std):
        return 0.0
    return mean / std * math.sqrt(periods_per_year)


def _profit_factor(trades: Sequence[ClosedTrade]) -> float:
    gp = sum(t.pnl_quote for t in trades if t.pnl_quote > 0)
    gl = -sum(t.pnl_quote for t in trades if t.pnl_quote < 0)
    if gl > 0:
        return gp / gl
    return PF_CAP if gp > 0 else 0.0


def compute_metrics(
    trades: Sequence[ClosedTrade], curve: Sequence[tuple[int, float]], start_balance: float
) -> dict:
    """전체 성과 지표. 비율은 소수."""
    wins = [t for t in trades if t.pnl_quote > 0]
    losses = [t for t in trades if t.pnl_quote <= 0]
    final = curve[-1][1] if curve else float(start_balance)
    eq_values = [eq for _, eq in curve]
    n = len(trades)
    return {
        "start_balance": float(start_balance),
        "final_equity": float(final),
        "net_return": (final / start_balance - 1.0) if start_balance > 0 else 0.0,
        "net_pnl_quote": float(final - start_balance),
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else 0.0,
        "profit_factor": _profit_factor(trades),
        "max_drawdown": max_drawdown(eq_values),
        "sharpe_daily": sharpe_from_curve(curve),
        "avg_pnl_pct": (statistics.fmean(t.pnl_pct for t in trades) if n else 0.0),
        "avg_bars_held": (statistics.fmean(t.closed_cycle - t.opened_cycle for t in trades) if n else 0.0),
        "total_fees_quote": float(sum(t.fee_quote_total for t in trades)),
        "bars": len(curve),
        "exit_reasons": _count_reasons(trades),
    }


def _count_reasons(trades: Sequence[ClosedTrade]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        out[t.reason] = out.get(t.reason, 0) + 1
    return dict(sorted(out.items()))


def _buy_and_hold(data: dict[str, _SymbolData], timeline: list[int], fee_pct: float, slippage_pct: float) -> dict[str, float]:
    """종목별 바이앤홀드 수익률: 첫 봉 시가 매수(+슬리피지·수수료) → 마지막 봉 종가 매도(−슬리피지·수수료)."""
    out: dict[str, float] = {}
    if not timeline:
        return out
    t0, t1 = timeline[0], timeline[-1]
    for sym, sd in data.items():
        first = next((c for c in sd.base if int(c.ts) >= t0 and _candle_ok(c)), None)
        last = next((c for c in reversed(sd.base) if int(c.ts) <= t1 and _candle_ok(c)), None)
        if first is None or last is None or first.open <= 0:
            out[sym] = 0.0
            continue
        buy = float(first.open) * (1.0 + slippage_pct) * (1.0 + fee_pct)
        sell = float(last.close) * (1.0 - slippage_pct) * (1.0 - fee_pct)
        out[sym] = sell / buy - 1.0
    return out


def _segments(sim: _SimOutput, start_balance: float, n_seg: int = WALK_FORWARD_SEGMENTS) -> list[SegmentResult]:
    """타임라인을 n 등분해 구간별 성과(거래는 청산 봉 기준으로 귀속)."""
    curve = sim.equity_curve
    total = len(curve)
    out: list[SegmentResult] = []
    if total == 0:
        return out
    bounds: list[tuple[int, int]] = []
    for s in range(n_seg):
        a = total * s // n_seg
        b = total * (s + 1) // n_seg
        if b > a:
            bounds.append((a, b))
    for idx, (a, b) in enumerate(bounds):
        seg_trades = [t for t in sim.trades if a <= t.closed_cycle < b]
        seg_curve = [eq for _, eq in curve[a:b]]
        start_eq = curve[a - 1][1] if a > 0 else float(start_balance)
        end_eq = curve[b - 1][1]
        wins = sum(1 for t in seg_trades if t.pnl_quote > 0)
        n = len(seg_trades)
        out.append(
            SegmentResult(
                index=idx,
                start_ts=curve[a][0],
                end_ts=curve[b - 1][0],
                bars=b - a,
                trades=n,
                wins=wins,
                losses=n - wins,
                win_rate=(wins / n) if n else 0.0,
                profit_factor=_profit_factor(seg_trades),
                net_return=(end_eq / start_eq - 1.0) if start_eq > 0 else 0.0,
                max_drawdown=max_drawdown([start_eq] + seg_curve),
                start_equity=start_eq,
                end_equity=end_eq,
            )
        )
    return out


def monte_carlo(
    trade_returns: Sequence[float], iterations: int = MC_ITERATIONS, seed: int = MC_SEED
) -> dict:
    """거래 순서 셔플 몬테카를로. 거래별 수익률(자본 대비)을 복리로 이어 붙인다.

    동시 보유(최대 3)로 겹치는 거래를 순차로 근사한다(보수적 근사; 문서화).
    반환: 5% 분위 수익률, 95% 분위 MDD, 중앙값들, 반복 수.
    """
    rets = [float(r) for r in trade_returns if _finite(r)]
    if not rets:
        return {
            "iterations": 0,
            "trades": 0,
            "return_p5": 0.0,
            "return_p50": 0.0,
            "mdd_p50": 0.0,
            "mdd_p95": 0.0,
            "seed": seed,
        }
    rng = random.Random(seed)
    finals: list[float] = []
    mdds: list[float] = []
    order = list(rets)
    for _ in range(int(iterations)):
        rng.shuffle(order)
        eq = 1.0
        peak = 1.0
        mdd = 0.0
        for r in order:
            eq *= 1.0 + r
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > mdd:
                mdd = dd
        finals.append(eq - 1.0)
        mdds.append(mdd)
    finals.sort()
    mdds.sort()
    return {
        "iterations": int(iterations),
        "trades": len(rets),
        "return_p5": _percentile(finals, 0.05),
        "return_p50": _percentile(finals, 0.50),
        "mdd_p50": _percentile(mdds, 0.50),
        "mdd_p95": _percentile(mdds, 0.95),
        "seed": seed,
    }


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """선형 보간 분위수(정렬된 입력)."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac)


def _verdict(
    metrics: dict, segments: Sequence[SegmentResult], stress: dict
) -> tuple[Literal["VALIDATED", "NOT VALIDATED"], list[str]]:
    reasons: list[str] = []
    if metrics["trades"] < VERDICT_MIN_TRADES:
        reasons.append(f"거래 수 {metrics['trades']} < {VERDICT_MIN_TRADES} (too_few_trades)")
    if len(segments) < WALK_FORWARD_SEGMENTS:
        reasons.append(f"워크포워드 구간 {len(segments)} < {WALK_FORWARD_SEGMENTS} (too_few_segments)")
    for s in segments:
        if not s.profit_factor > VERDICT_MIN_SEGMENT_PF:
            reasons.append(
                f"구간 {s.index + 1} PF {s.profit_factor:.2f} ≤ {VERDICT_MIN_SEGMENT_PF} "
                f"(거래 {s.trades}) (segment_pf)"
            )
    if not metrics["max_drawdown"] < VERDICT_MAX_MDD:
        reasons.append(f"MDD {metrics['max_drawdown']:.2%} ≥ {VERDICT_MAX_MDD:.0%} (mdd)")
    if not stress.get("profit_factor", 0.0) > VERDICT_MIN_STRESS_PF:
        reasons.append(f"비용 2배 PF {stress.get('profit_factor', 0.0):.2f} ≤ {VERDICT_MIN_STRESS_PF} (stress_pf)")
    covered = metrics.get("covered_days")
    requested = metrics.get("requested_days")
    if _finite(covered) and _finite(requested) and requested > 0:
        if covered < requested * VERDICT_MIN_COVERAGE_RATIO:
            reasons.append(
                f"데이터 기간 {covered:.1f}일 < 요청 {requested}일 × {VERDICT_MIN_COVERAGE_RATIO:.0%} "
                f"(data_coverage)"
            )
    if reasons:
        return "NOT VALIDATED", reasons
    return "VALIDATED", []


# ---------------------------------------------------------------- 캔들 조회(캐시)
def _bars_per_day(timeframe: str) -> int:
    return max(1, _MS_PER_DAY // timeframe_ms(timeframe))


def _cache_name(symbol: str, timeframe: str, limit: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_")
    return f"{safe}_{timeframe}_{limit}.json"


def _load_cache(path: Path) -> list[Candle] | None:
    """캐시 파일 → 캔들. 손상·빈 목록이면 None(캐시 미스로 취급해 거래소를 다시 묻는다)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            return None
        return [Candle(**row) for row in raw]
    except (OSError, ValueError, TypeError) as e:
        log.warning("캐시 읽기 실패 %s: %s", path, e)
        return None


def _save_cache(path: Path, candles: Sequence[Candle]) -> None:
    """캔들을 원자적으로 저장한다. 빈 목록은 저장하지 않는다(일시 장애의 영구화 방지)."""
    if not candles:
        log.warning("빈 캔들 목록은 캐시하지 않음: %s", path.name)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([c.model_dump() for c in candles], sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as e:
        log.warning("캐시 쓰기 실패 %s: %s", path, e)


def _fetch_candles(cfg: BacktestConfig, market: Any) -> dict[str, dict[str, list[Candle]]]:
    """종목·시간대별 캔들을 캐시(JSON) 우선으로 가져온다. 거래소 limit 상한으로 부족하면 경고."""
    out: dict[str, dict[str, list[Candle]]] = {}
    cache_dir = Path(cfg.cache_dir)
    tfs = (cfg.timeframe, *[tf for tf in HIGHER_TIMEFRAMES if tf != cfg.timeframe])
    for sym in cfg.symbols:
        out[sym] = {}
        for tf in tfs:
            window = WINDOW_BARS["base"] if tf == cfg.timeframe else WINDOW_BARS.get(tf, 200)
            limit = int(cfg.days) * _bars_per_day(tf) + window
            path = cache_dir / _cache_name(sym, tf, limit)
            candles = _load_cache(path) if path.exists() else None
            if candles is None:
                candles = list(market.fetch_ohlcv(sym, tf, limit))
                _save_cache(path, candles)
                log.info("%s %s: %d봉 조회 → 캐시 %s", sym, tf, len(candles), path.name)
            else:
                log.info("%s %s: 캐시 %s (%d봉)", sym, tf, path.name, len(candles))
            if len(candles) < limit:
                log.warning(
                    "%s %s: 요청 %d봉 중 %d봉만 받음 (거래소 limit 상한일 수 있음 — 기간이 짧아진다)",
                    sym, tf, limit, len(candles),
                )
            out[sym][tf] = candles
    return out


# ---------------------------------------------------------------- 실행
def run(
    cfg: BacktestConfig,
    market: Any | None = None,
    candles: dict[str, dict[str, list[Candle]]] | None = None,
) -> BacktestResult:
    """백테스트 실행. ``candles``(종목→시간대→캔들)가 있으면 그대로, 없으면 ``market`` 에서 조회(캐시).

    결과를 ``cfg.out_dir/run_<ts>.json`` + ``.md`` 로 저장한다. 비율은 소수.
    """
    if not cfg.symbols:
        raise ValueError("symbols 가 비어 있음")
    if candles is None:
        if market is None:
            raise ValueError("market 또는 candles 중 하나는 필요")
        candles = _fetch_candles(cfg, market)
    if not (cfg.start_balance > 0 and _finite(cfg.start_balance)):
        raise ValueError(f"start_balance 는 양수여야 함: {cfg.start_balance!r}")
    if cfg.fee_pct < 0 or cfg.slippage_pct < 0:
        raise ValueError("fee_pct/slippage_pct 는 0 이상이어야 함")

    t_start = time.perf_counter()
    data: dict[str, _SymbolData] = {}
    for sym in cfg.symbols:
        by_tf = candles.get(sym)
        if not by_tf:
            raise ValueError(f"{sym}: 캔들이 없음")
        data[sym] = _SymbolData(sym, cfg.timeframe, by_tf)
    timeline = sorted({int(c.ts) for sd in data.values() for c in sd.base})
    if cfg.days > 0 and timeline:
        cutoff = timeline[-1] - int(cfg.days) * _MS_PER_DAY
        trimmed = [ts for ts in timeline if ts > cutoff]
        if trimmed:
            timeline = trimmed

    sim = _simulate(
        data, timeline, start_balance=cfg.start_balance, fee_pct=cfg.fee_pct, slippage_pct=cfg.slippage_pct
    )
    metrics = compute_metrics(sim.trades, sim.equity_curve, cfg.start_balance)
    metrics["skipped"] = dict(sim.skipped)
    # 실제 데이터 기간(요청 days 와 비교해 판정 사유·카드에 남긴다)
    tf_ms = timeframe_ms(cfg.timeframe)
    covered_start = int(timeline[0]) if timeline else 0
    covered_end = int(timeline[-1]) + tf_ms if timeline else 0
    metrics["covered_start_ts"] = covered_start
    metrics["covered_end_ts"] = covered_end
    metrics["covered_days"] = round((covered_end - covered_start) / _MS_PER_DAY, 3)
    metrics["requested_days"] = int(cfg.days)
    metrics["bad_candles"] = {sym: sd.bad_bars for sym, sd in data.items() if sd.bad_bars}
    bnh = _buy_and_hold(data, timeline, cfg.fee_pct, cfg.slippage_pct)
    segments = _segments(sim, cfg.start_balance)
    mc = monte_carlo(sim.trade_returns)

    stress_sim = _simulate(
        data,
        timeline,
        start_balance=cfg.start_balance,
        fee_pct=cfg.fee_pct * 2.0,
        slippage_pct=cfg.slippage_pct * 2.0,
    )
    stress_metrics = compute_metrics(stress_sim.trades, stress_sim.equity_curve, cfg.start_balance)
    stress = {
        "fee_pct": cfg.fee_pct * 2.0,
        "slippage_pct": cfg.slippage_pct * 2.0,
        "trades": stress_metrics["trades"],
        "profit_factor": stress_metrics["profit_factor"],
        "net_return": stress_metrics["net_return"],
        "max_drawdown": stress_metrics["max_drawdown"],
        "win_rate": stress_metrics["win_rate"],
    }

    verdict, reasons = _verdict(metrics, segments, stress)
    walk_forward = {
        "segments": [s.to_dict() for s in segments],
        "all_pf_above_threshold": bool(segments) and all(s.profit_factor > VERDICT_MIN_SEGMENT_PF for s in segments),
        "threshold_pf": VERDICT_MIN_SEGMENT_PF,
    }
    best_bnh = max(bnh.values()) if bnh else 0.0
    metrics["vs_buy_and_hold_best"] = metrics["net_return"] - best_bnh
    metrics["vs_buy_and_hold_mean"] = metrics["net_return"] - (statistics.fmean(bnh.values()) if bnh else 0.0)
    metrics["elapsed_s"] = round(time.perf_counter() - t_start, 3)

    result = BacktestResult(
        run_ts=int(time.time() * 1000),
        config=cfg.to_dict(),
        verdict=verdict,
        reasons=reasons,
        metrics=metrics,
        buy_and_hold=bnh,
        walk_forward=walk_forward,
        monte_carlo=mc,
        stress=stress,
        trades=list(sim.trades),
        equity_curve=list(sim.equity_curve),
        notes=[
            "신호=봉 t 종가, 체결=봉 t+1 시가×(1+슬리피지); 손절/익절은 저가/고가 판정, 동시 접촉 시 손절 우선",
            "몬테카를로는 거래를 순차로 근사(동시 보유 겹침 무시)",
            f"PF {PF_CAP} 는 손실 0·이익 >0 의 표기값",
        ],
    )
    try:
        result.json_path, result.md_path = _write_run_card(cfg.out_dir, result)
    except OSError as e:
        log.error("백테스트 결과 저장 실패: %s", e)
    log.info(
        "백테스트 %s: 거래 %d, 수익률 %.2f%%, PF %.2f, MDD %.2f%%, 판정 %s",
        ",".join(cfg.symbols), metrics["trades"], metrics["net_return"] * 100,
        metrics["profit_factor"], metrics["max_drawdown"] * 100, verdict,
    )
    return result


# ---------------------------------------------------------------- 저장
def _write_run_card(out_dir: Path, result: BacktestResult) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"run_{result.run_ts}"
    json_path = out_dir / f"{stem}.json"
    n = 1
    while json_path.exists():
        json_path = out_dir / f"{stem}_{n}.json"
        n += 1
    md_path = json_path.with_suffix(".md")
    payload = result.to_dict()
    payload["json_path"] = str(json_path)
    payload["md_path"] = str(md_path)
    # 표준 JSON 보장(NaN/Infinity 금지) + 원자적 저장(.tmp → replace). md 를 먼저 써서
    # json 이 보이는 시점에는 md 도 반드시 존재한다.
    json_text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
    md_text = render_markdown(result)
    _atomic_write_text(md_path, md_text)
    _atomic_write_text(json_path, json_text)
    return json_path, md_path


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def render_markdown(result: BacktestResult) -> str:
    m = result.metrics
    cfg = result.config
    lines = [
        f"# 백테스트 결과 — {result.verdict}",
        "",
        f"- 종목: {', '.join(cfg.get('symbols', []))} / 시간대 {cfg.get('timeframe')} / 요청 {cfg.get('days')}일",
        f"- 실제 데이터 기간: {m.get('covered_days', 0.0):.1f}일 "
        f"({m.get('covered_start_ts', 0)} ~ {m.get('covered_end_ts', 0)} ms, {m.get('bars', 0)}봉)",
        f"- 비용: 수수료 {cfg.get('fee_pct')} 편도, 슬리피지 {cfg.get('slippage_pct')} / 시작 자본 {cfg.get('start_balance')} USDT",
        f"- 실행 시각(ms): {result.run_ts}",
        "",
        "## 요약",
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 순수익률 | {m['net_return']:.2%} |",
        f"| 최종 자본 | {m['final_equity']:.2f} USDT |",
        f"| 거래 수 | {m['trades']} (승 {m['wins']} / 패 {m['losses']}) |",
        f"| 승률 | {m['win_rate']:.1%} |",
        f"| Profit Factor | {m['profit_factor']:.2f} |",
        f"| MDD | {m['max_drawdown']:.2%} |",
        f"| 샤프(일봉 근사) | {m['sharpe_daily']:.2f} |",
        f"| 평균 보유 봉 수 | {m['avg_bars_held']:.1f} |",
        "",
        "## 바이앤홀드 대비",
        "",
        "| 종목 | 바이앤홀드 수익률 |",
        "|---|---|",
    ]
    for sym, r in result.buy_and_hold.items():
        lines.append(f"| {sym} | {r:.2%} |")
    lines += [
        f"| (전략 − 최고 종목) | {m.get('vs_buy_and_hold_best', 0.0):+.2%} |",
        "",
        "## 워크포워드 (3구간)",
        "",
        "| 구간 | 거래 | 승률 | PF | 수익률 | MDD |",
        "|---|---|---|---|---|---|",
    ]
    for s in result.walk_forward.get("segments", []):
        lines.append(
            f"| {s['index'] + 1} | {s['trades']} | {s['win_rate']:.1%} | {s['profit_factor']:.2f} | "
            f"{s['net_return']:.2%} | {s['max_drawdown']:.2%} |"
        )
    mc = result.monte_carlo
    st = result.stress
    lines += [
        "",
        "## 몬테카를로 (거래 순서 셔플)",
        "",
        f"- 반복 {mc.get('iterations', 0)}회, 거래 {mc.get('trades', 0)}건",
        f"- 수익률 5% 분위: {mc.get('return_p5', 0.0):.2%} (중앙값 {mc.get('return_p50', 0.0):.2%})",
        f"- MDD 95% 분위: {mc.get('mdd_p95', 0.0):.2%} (중앙값 {mc.get('mdd_p50', 0.0):.2%})",
        "",
        "## 비용 2배 스트레스",
        "",
        f"- 수수료 {st.get('fee_pct')} / 슬리피지 {st.get('slippage_pct')}",
        f"- 거래 {st.get('trades', 0)}건, PF {st.get('profit_factor', 0.0):.2f}, 수익률 {st.get('net_return', 0.0):.2%}, MDD {st.get('max_drawdown', 0.0):.2%}",
        "",
        f"## 판정: {result.verdict}",
        "",
    ]
    if result.reasons:
        lines += [f"- {r}" for r in result.reasons]
    else:
        lines.append(
            f"- 모든 구간 PF > {VERDICT_MIN_SEGMENT_PF}, 거래 ≥ {VERDICT_MIN_TRADES}, "
            f"MDD < {VERDICT_MAX_MDD:.0%}, 비용 2배 PF > {VERDICT_MIN_STRESS_PF}"
        )
    if result.notes:
        lines += ["", "## 비고", ""] + [f"- {n}" for n in result.notes]
    lines.append("")
    return "\n".join(lines)
