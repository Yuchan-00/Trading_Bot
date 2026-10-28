"""포지션 논지 추적·편향 진단·성과 지표·저널 리뷰 (SPEC §21, v2.1 §20.3a.7·보완 사항).

구성
- ``JournalState``: 저널이 사이클 사이에 들고 다니는 상태(``<data_dir>/journal_state.json``, 원자 저장).
  SPEC v2.1 추가 필드(``entry_verdicts``·``thesis_history``·``prev_assumption_status``·
  ``news_risk_after_entry``·``gate_stats_today``·``halts_today``) 포함. ``health_history`` 는
  ``thesis_history`` 에서 파생되어 함께 유지된다.
- ``evaluate_red_lines``: 총괄이 BUY 때 남긴 구조화 레드라인을 코드가 매 사이클 평가한다.
- ``bias_severities`` / ``decay_state``: 편향 심각도·전략 쇠퇴 상태(청산 20건 미만이면 표본 부족).
- ``code_metrics``: 승률·PF·평균손익·기대값·샤프/소르티노/칼마·MDD·BTC 바이앤홀드 대비.
- ``build_journal_post_trade`` / ``build_journal_daily``: journal 역할 payload(§20.3a.7 키 정확히).
- ``post_trade_review`` / ``daily_review``: ``runner.run("journal", ...)`` 호출. 실패하면 None
  (호출자는 ``code_post_trade_summary`` / ``code_daily_summary`` 로 대체한다).

단위
- ``ClosedTrade.pnl_pct`` 는 소수(0.02 = 2%)다. 저널 지표와 LLM payload 의 ``*_pct`` 는 전부 **% 단위**
  (2.0 = 2%)로 바꿔 쓴다(SPEC v2.1: "퍼센트는 % 단위").
- LLM payload 의 숫자는 4유효자리로 반올림한다(``features.compact`` 와 같은 규칙).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    Debias,
    FlowSymbolView,
    HeadDecision,
    JournalDaily,
    JournalPostTrade,
    RedLine,
    RiskCandidateVerdict,
    TechnicalOutput,
    Thesis,
)
from tradingbot.models import ClosedTrade, Position

log = logging.getLogger("tradingbot.journal")

# ---------------------------------------------------------------- 상수
JOURNAL_STATE_FILE = "journal_state.json"
JOURNAL_LOG_FILE = "journal.jsonl"           # <data_dir>/logs/journal.jsonl

MIN_SAMPLE = 20                               # 편향·쇠퇴 판정 최소 청산 수
DECAY_WINDOW = 20                             # 쇠퇴 판정: 최근 20건 PF
DECAY_HEALTHY_PF = 1.1
DECAY_WARNING_PF = 0.9

DISPOSITION_MILD = 1.2                        # 손실 보유시간 / 이익 보유시간 (이상)
DISPOSITION_SEVERE = 1.5
OVERTRADING_MILD = 8                          # 일 거래수 (초과)
OVERTRADING_SEVERE = 12
OVERTRADING_WINDOW_DAYS = 7                   # 마지막 거래일 기준 최근 7일의 활동일 평균
CHASING_RETURN_PCT = 3.0                      # 진입 직전 1h 수익률 ≥ +3%
CHASING_MILD = 0.30                           # 비율 (초과)
CHASING_SEVERE = 0.50
ANCHORING_BAND = 0.003                        # 진입가 ±0.3% 내 청산
ANCHORING_MILD = 0.30                         # 비율 (초과)
ANCHORING_SEVERE = 0.50                       # SPEC 은 단일 임계(30%)만 정함 → 추격과 같은 50% 를 severe 로 둔다

NOISE_PNL_PCT = 0.3                           # |pnl_pct| < 0.3% → 노이즈 거래
NOISE_SHARE_WINDOW = 20
NOISE_SHARE_MIN = 5

PERIODS_PER_YEAR = 365.0                      # 일 수익률 연율화
MIN_CALMAR_DAYS = 30                          # calmar(연율화 수익률/MDD)는 일수익률 표본이 이만큼 쌓이기 전에는 None
PF_INFINITE_CAP = 999.0                       # 손실 없는 PF(무한대)를 daily payload 에 싣는 상한값(null=데이터 없음과 구분)
DEFAULT_CYCLE_MINUTES = 15                    # daily_review 가 state 미갱신 시 통계를 직접 계산할 때 쓰는 사이클 길이
TELEGRAM_MAX_CHARS = 400
HISTORY_CAP = 400                             # 심볼별 thesis_history 최대 보관 수
ENTRY_CONTEXT_CAP = 500                       # entry_context 최대 보관 수(오래된 것부터 버림)

BIAS_KEYS: tuple[str, ...] = ("disposition", "overtrading", "chasing", "anchoring")
BiasLevel = Literal["none", "mild", "severe", "insufficient_sample"]
DecayState = Literal["insufficient_sample", "healthy", "warning", "decayed"]
DECAY_STATES: tuple[str, ...] = ("insufficient_sample", "healthy", "warning", "decayed")
BIAS_LEVELS: tuple[str, ...] = ("none", "mild", "severe", "insufficient_sample")

HARD_LIMITS_PAYLOAD: dict[str, float] = {
    "max_position_pct": 10, "max_positions": 3, "max_stop_pct": 2, "max_tp_pct": 4, "min_confidence": 0.65,
}

# §20.3a.7 키 계약 -------------------------------------------------------------
THESIS_HISTORY_KEYS: tuple[str, ...] = (
    "cycle", "health", "assumption_status", "red_line_hits", "action", "exit_flag", "unrealized_pct",
)
ENTRY_VERDICT_KEYS: dict[str, tuple[str, ...]] = {
    "technical": ("setup_type", "confidence"),
    "bull": ("case_strength", "thesis_points", "confidence"),
    "bear": ("verdict", "strongest_point", "death_paths"),
    "risk": ("verdict", "conditions", "required_monitoring", "rr_net_estimate", "risk_note"),
    "head": ("disagreement_class", "decisive_factor", "rationale", "debias_chasing_spike"),
    "channel_agreement": ("technical", "flow", "news"),
}
RETURNS_KEYS: tuple[str, ...] = (
    "day_pnl_pct", "cum_pnl_pct", "equity", "max_drawdown_pct", "btc_buy_hold_pct", "excess_vs_btc_pct",
)
TRADE_STATS_KEYS: tuple[str, ...] = (
    "closed_today", "win_rate", "avg_win_pct", "avg_loss_pct", "profit_factor", "avg_bars_held",
    "sharpe", "sortino", "calmar",
)
ATTRIBUTION_KEYS: tuple[str, ...] = ("noise_trades", "stop_losses", "take_profits", "time_stops", "head_sells")
GATE_COUNTER_KEYS: tuple[str, ...] = (
    "candidates_seen", "g1_dropped", "g2_bear_decline", "g3_risk_veto", "g3_other", "g4_downgrade", "head_calls",
)
GATE_STATS_KEYS: tuple[str, ...] = GATE_COUNTER_KEYS + ("bear_decline_rate", "risk_veto_rate")
LLM_KEYS: tuple[str, ...] = ("calls", "cost_usd", "budget_usd", "abstain_count")
METRICS_TABLE_KEYS: tuple[str, ...] = (
    "returns", "trade_stats", "by_exit_reason", "by_symbol", "by_setup", "attribution_buckets",
    "bias_severities", "decay_state", "gate_stats", "llm", "halts",
)

RED_LINE_LABELS: dict[str, str] = {
    "price": "가격",
    "unrealized_pct": "미실현손익(%)",
    "funding_rate": "펀딩비(8h)",
    "btc_return_1h_pct": "BTC 1h 수익률(%)",
    "composite_sentiment": "종합심리(0~100)",
    "hours_held": "보유시간(h)",
}


def _default_bias() -> dict[str, str]:
    return {k: "insufficient_sample" for k in BIAS_KEYS}


# ---------------------------------------------------------------- 상태 모델
class JournalState(BaseModel):
    """저널 상태. 모든 필드에 기본값이 있어 ``JournalState()`` 가 빈 상태다."""

    model_config = ConfigDict(extra="forbid")

    closed_count: int = 0
    decay_state: DecayState = "insufficient_sample"
    bias: dict[str, BiasLevel] = Field(default_factory=_default_bias)
    gate_stats: dict[str, int] = Field(default_factory=dict)            # 누적 게이트 카운터
    health_history: dict[str, list[int]] = Field(default_factory=dict)  # thesis_history 에서 파생
    thesis: dict[str, Thesis] = Field(default_factory=dict)
    last_daily_review_date: str | None = None
    # --- SPEC v2.1 추가 필드
    entry_verdicts: dict[str, dict] = Field(default_factory=dict)
    thesis_history: dict[str, list[dict]] = Field(default_factory=dict)
    prev_assumption_status: dict[str, list[str]] = Field(default_factory=dict)
    news_risk_after_entry: dict[str, list[str]] = Field(default_factory=dict)
    gate_stats_today: dict[str, int] = Field(default_factory=dict)
    halts_today: list[str] = Field(default_factory=list)
    # --- 구현 보조(청산 뒤에도 남아야 하는 진입 맥락: 추격 편향·by_setup 집계용)
    #     키 = entry_key(symbol, opened_ts) ; 값 = {"setup_type": str|None, "return_1h_pct": float|None}
    entry_context: dict[str, dict] = Field(default_factory=dict)

    # 편의 메서드(모듈 함수 위임)
    @classmethod
    def load(cls, data_dir: Path | str) -> "JournalState":
        return load_journal_state(data_dir)

    def save(self, data_dir: Path | str) -> Path:
        return save_journal_state(self, data_dir)


def journal_state_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / JOURNAL_STATE_FILE


def load_journal_state(data_dir: Path | str) -> JournalState:
    """``<data_dir>/journal_state.json`` 을 읽는다.

    파일이 없으면 빈 상태. 손상(JSON/스키마 오류)이면 ``.corrupt-<ms>`` 로 격리하고 빈 상태로 시작한다
    (저널 상태는 거래 안전과 무관한 보조 상태이므로 사이클을 멈추지 않는다).
    """
    p = journal_state_path(data_dir)
    if not p.exists():
        return JournalState()
    try:
        raw = p.read_text(encoding="utf-8")
        return JournalState.model_validate(json.loads(raw))
    except (OSError, ValueError, ValidationError) as exc:
        target = p.with_name(f"{p.name}.corrupt-{int(time.time() * 1000)}")
        try:
            os.replace(p, target)
        except OSError:
            target = p
        log.error("저널 상태 손상 — 격리 후 새 상태로 시작: %s (%s)", target, type(exc).__name__)
        return JournalState()


def save_journal_state(state: JournalState, data_dir: Path | str) -> Path:
    """tmp 파일 → fsync → ``os.replace`` 원자 저장. 실패는 ``OSError`` 로 전파한다."""
    p = journal_state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, indent=1)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        log.error("저널 상태 저장 실패: %s", p)
        raise
    return p


def append_journal_log(data_dir: Path | str, record: Mapping[str, Any]) -> Path:
    """``<data_dir>/logs/journal.jsonl`` 에 한 줄 추가(결정적 JSON)."""
    p = Path(data_dir) / "logs" / JOURNAL_LOG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_jsonable(record), sort_keys=True, ensure_ascii=False)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return p


# ---------------------------------------------------------------- 숫자 유틸
def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _round_sig(x: float, sig: int = 4) -> float | None:
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    v = float(f"{x:.{sig}g}")
    return 0.0 if v == 0 else v


def _jsonable(obj: Any) -> Any:
    """BaseModel/Mapping/Sequence 를 JSON 가능한 값으로(반올림 없음, NaN/inf → None)."""
    if isinstance(obj, BaseModel):
        return _jsonable(obj.model_dump(mode="json", by_alias=True))
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _compact_value(obj: Any) -> Any:
    """로컬 반올림(``features.compact`` 와 같은 규칙): float 4유효자리, NaN/inf → None, 키 정렬."""
    if isinstance(obj, BaseModel):
        return _compact_value(obj.model_dump(mode="python", by_alias=True))
    if isinstance(obj, Mapping):
        return {str(k): _compact_value(obj[k]) for k in sorted(obj, key=lambda k: str(k))}
    if isinstance(obj, (list, tuple)):
        return [_compact_value(v) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return _round_sig(obj)
    return obj


def compact_payload(obj: Any) -> Any:
    """payload 반올림: ``features.compact`` 를 쓰고, 그 모듈을 쓸 수 없으면 같은 규칙의 로컬 구현으로 대체."""
    try:
        from tradingbot.features import compact
    except Exception:  # noqa: BLE001 - features 가 없거나 import 실패해도 저널은 동작해야 한다
        return _compact_value(obj)
    try:
        return compact(obj)
    except Exception:  # noqa: BLE001
        log.debug("features.compact 실패 — 로컬 반올림 사용")
        return _compact_value(obj)


def entry_key(symbol: str, opened_ts: int) -> str:
    """진입 단위 식별자(청산 뒤에도 남는 entry_context 의 키)."""
    return f"{symbol}-{int(opened_ts)}"


def trade_id(trade: ClosedTrade) -> str:
    """SPEC §20.3a.7: ``f"{symbol}-{opened_ts}-{closed_ts}"``."""
    return f"{trade.symbol}-{trade.opened_ts}-{trade.closed_ts}"


def _pnl_pct(trade: ClosedTrade) -> float | None:
    """소수 → % 단위. 알 수 없으면(NaN/inf) None — 본전(0.0)으로 바꾸지 않는다(SPEC v2.1: 없는 값은 null)."""
    return float(trade.pnl_pct) * 100.0 if _finite(trade.pnl_pct) else None


def _known(xs: Sequence[float | None]) -> list[float]:
    return [float(x) for x in xs if x is not None]


def _bars_held(trade: ClosedTrade) -> int:
    return max(int(trade.closed_cycle) - int(trade.opened_cycle), 0)


def _hold_minutes(trade: ClosedTrade, cycle_minutes: int) -> float:
    if trade.closed_ts > trade.opened_ts:
        return (trade.closed_ts - trade.opened_ts) / 60_000.0
    return _bars_held(trade) * float(max(cycle_minutes, 0))


def _utc_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _sorted_trades(trades: Sequence[ClosedTrade]) -> list[ClosedTrade]:
    return sorted(trades, key=lambda t: (t.closed_ts, t.opened_ts))


# ---------------------------------------------------------------- 레드라인
def red_line_metrics(
    mark: float,
    position: Position,
    funding: float | None,
    btc_return_1h_pct: float | None,
    sentiment: float | None,
    now_ms: int,
) -> dict[str, float | None]:
    """레드라인 지표의 현재값. 계산할 수 없으면 None."""
    price = float(mark) if _finite(mark) and mark > 0 else None
    unreal: float | None = None
    if price is not None and _finite(position.entry_price) and position.entry_price > 0:
        unreal = (price / position.entry_price - 1.0) * 100.0
    hours: float | None = None
    if _finite(now_ms) and _finite(position.opened_ts) and position.opened_ts > 0 and now_ms >= position.opened_ts:
        hours = (now_ms - position.opened_ts) / 3_600_000.0
    return {
        "price": price,
        "unrealized_pct": unreal,
        "funding_rate": float(funding) if _finite(funding) else None,
        "btc_return_1h_pct": float(btc_return_1h_pct) if _finite(btc_return_1h_pct) else None,
        "composite_sentiment": float(sentiment) if _finite(sentiment) else None,
        "hours_held": hours,
    }


def _fmt_num(x: float) -> str:
    return f"{x:.6g}"


def evaluate_red_lines(
    thesis: Thesis,
    mark: float,
    position: Position,
    funding: float | None,
    btc_return_1h_pct: float | None,
    sentiment: float | None,
    now_ms: int,
) -> list[str]:
    """적중한 레드라인을 사람이 읽을 한국어 문자열로 돌려준다(입력 순서 유지).

    비교는 엄격(``<``/``>``). 지표값이 없으면(None/NaN) 적중으로 보지 않고 로그에만 남긴다.
    """
    if thesis is None or not thesis.red_lines:
        return []
    values = red_line_metrics(mark, position, funding, btc_return_1h_pct, sentiment, now_ms)
    hits: list[str] = []
    missing: list[str] = []
    for rl in thesis.red_lines:
        if not isinstance(rl, RedLine):
            try:
                rl = RedLine.model_validate(rl)
            except ValidationError:
                log.warning("레드라인 형식 오류 symbol=%s — 무시", position.symbol)
                continue
        cur = values.get(rl.metric)
        if cur is None or not _finite(rl.value):
            missing.append(rl.metric)
            continue
        hit = cur < rl.value if rl.op == "<" else cur > rl.value
        if hit:
            label = RED_LINE_LABELS.get(rl.metric, rl.metric)
            hits.append(
                f"레드라인 적중: {rl.metric} {rl.op} {_fmt_num(rl.value)} — 현재 {_fmt_num(_round_sig(cur) or 0.0)} ({label})"
            )
    if missing:
        log.info("레드라인 지표 없음(미적중 처리) symbol=%s metrics=%s", position.symbol, ",".join(missing))
    return hits


# ---------------------------------------------------------------- 편향·쇠퇴
def _level(value: float, mild: float, severe: float, *, inclusive: bool) -> str:
    if inclusive:
        if value >= severe:
            return "severe"
        if value >= mild:
            return "mild"
        return "none"
    if value > severe:
        return "severe"
    if value > mild:
        return "mild"
    return "none"


def disposition_ratio(trades: Sequence[ClosedTrade], cycle_minutes: int) -> float | None:
    """평균 손실 보유시간 / 평균 이익 보유시간. 손실이 없으면 0(효과 없음), 이익이 없으면 None."""
    wins = [_hold_minutes(t, cycle_minutes) for t in trades if t.pnl_quote > 0]
    losses = [_hold_minutes(t, cycle_minutes) for t in trades if t.pnl_quote < 0]
    if not losses:
        return 0.0
    if not wins:
        return None
    avg_w = sum(wins) / len(wins)
    avg_l = sum(losses) / len(losses)
    if avg_w <= 0:
        return math.inf if avg_l > 0 else 1.0
    return avg_l / avg_w


def trades_per_day(trades: Sequence[ClosedTrade]) -> float | None:
    """마지막 진입일 기준 최근 7일 중 거래가 있었던 날의 평균 진입 수(UTC, opened_ts 기준)."""
    if not trades:
        return None
    last_ts = max(t.opened_ts for t in trades)
    window_start = last_ts - OVERTRADING_WINDOW_DAYS * 86_400_000
    # 부분 청산으로 한 진입이 ClosedTrade 여러 건이 될 수 있으므로 entry_key(symbol, opened_ts) 로 중복 제거
    entries: dict[str, set[str]] = {}
    for t in trades:
        if t.opened_ts > window_start:
            entries.setdefault(_utc_date(t.opened_ts), set()).add(entry_key(t.symbol, t.opened_ts))
    if not entries:
        return None
    return sum(len(v) for v in entries.values()) / len(entries)


def _entry_return(trade: ClosedTrade, returns: Mapping[str, Any] | None) -> float | None:
    if not returns:
        return None
    for key in (entry_key(trade.symbol, trade.opened_ts), trade_id(trade)):
        if key in returns:
            v = returns[key]
            if isinstance(v, Mapping):
                v = v.get("return_1h_pct")
            return float(v) if _finite(v) else None
    return None


def bias_severities(
    trades: list[ClosedTrade],
    cycle_minutes: int,
    *,
    entry_returns_1h_pct: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """편향 심각도 {disposition, overtrading, chasing, anchoring} → none|mild|severe|insufficient_sample.

    - 청산 20건 미만이면 전부 ``insufficient_sample``.
    - disposition: 평균 손실 보유시간 / 평균 이익 보유시간 ≥1.2 mild, ≥1.5 severe(이익 거래가 없으면 표본 부족).
    - overtrading: 일 거래수(최근 7일 활동일 평균) > 8 mild, > 12 severe.
    - chasing: 진입 직전 1h 수익률 ≥ +3% 인 진입 비율 > 30% mild, > 50% severe.
      ``ClosedTrade`` 에는 진입 직전 수익률이 없으므로 ``entry_returns_1h_pct``
      (키 ``entry_key(symbol, opened_ts)`` 또는 trade_id, 값 float 또는 ``{"return_1h_pct": ..}``)로 받는다.
      값이 있는 거래가 20건 미만이면 ``insufficient_sample``.
    - anchoring: 청산가가 진입가 ±0.3% 이내인 비율 > 30% mild, > 50% severe.
    """
    trades = list(trades or [])
    if len(trades) < MIN_SAMPLE:
        return _default_bias()
    out: dict[str, str] = {}

    ratio = disposition_ratio(trades, cycle_minutes)
    out["disposition"] = "insufficient_sample" if ratio is None else _level(
        ratio, DISPOSITION_MILD, DISPOSITION_SEVERE, inclusive=True
    )

    per_day = trades_per_day(trades)
    out["overtrading"] = "insufficient_sample" if per_day is None else _level(
        per_day, OVERTRADING_MILD, OVERTRADING_SEVERE, inclusive=False
    )

    # 추격 비율도 진입 단위(부분 청산 레코드 중복 제거)로 센다
    seen_entries: set[str] = set()
    rets: list[float] = []
    for t in trades:
        ek = entry_key(t.symbol, t.opened_ts)
        if ek in seen_entries:
            continue
        seen_entries.add(ek)
        r = _entry_return(t, entry_returns_1h_pct)
        if r is not None:
            rets.append(r)
    if len(rets) < MIN_SAMPLE:
        out["chasing"] = "insufficient_sample"
    else:
        share = sum(1 for r in rets if r >= CHASING_RETURN_PCT) / len(rets)
        out["chasing"] = _level(share, CHASING_MILD, CHASING_SEVERE, inclusive=False)

    valid = [t for t in trades if _finite(t.entry_price) and t.entry_price > 0 and _finite(t.exit_price)]
    if len(valid) < MIN_SAMPLE:
        out["anchoring"] = "insufficient_sample"
    else:
        near = sum(1 for t in valid if abs(t.exit_price / t.entry_price - 1.0) <= ANCHORING_BAND + 1e-12)
        out["anchoring"] = _level(near / len(valid), ANCHORING_MILD, ANCHORING_SEVERE, inclusive=False)
    return out


def profit_factor(trades: Sequence[ClosedTrade]) -> float | None:
    """총이익 / 총손실(quote). 손실이 없으면 이익이 있을 때 inf, 둘 다 없으면 None."""
    gp = sum(t.pnl_quote for t in trades if _finite(t.pnl_quote) and t.pnl_quote > 0)
    gl = -sum(t.pnl_quote for t in trades if _finite(t.pnl_quote) and t.pnl_quote < 0)
    if gl <= 0:
        return math.inf if gp > 0 else None
    return gp / gl


def decay_state(trades: list[ClosedTrade]) -> str:
    """최근 20건 PF: ≥1.1 healthy, 0.9~1.1 warning, <0.9 decayed ; 20건 미만 insufficient_sample.

    손익이 전부 0 이라 PF 를 정할 수 없으면 warning(개선 근거 없음).
    """
    trades = list(trades or [])
    if len(trades) < MIN_SAMPLE:
        return "insufficient_sample"
    recent = _sorted_trades(trades)[-DECAY_WINDOW:]
    pf = profit_factor(recent)
    if pf is None:
        return "warning"
    if pf >= DECAY_HEALTHY_PF:
        return "healthy"
    if pf >= DECAY_WARNING_PF:
        return "warning"
    return "decayed"


def noise_trade_share(trades: Sequence[ClosedTrade]) -> float | None:
    """최근 20건 중 |pnl_pct| < 0.3% 비율. 5건 미만이면 None (ACCOUNT_PACK 용).

    손익을 알 수 없는 거래(pnl_pct NaN)는 노이즈로 세지 않고 표본에서도 뺀다.
    """
    recent = _sorted_trades(trades)[-NOISE_SHARE_WINDOW:]
    known = _known([_pnl_pct(t) for t in recent])
    if len(known) < NOISE_SHARE_MIN:
        return None
    return sum(1 for p in known if abs(p) < NOISE_PNL_PCT) / len(known)


def refresh_stats(state: JournalState, trades: Sequence[ClosedTrade], cycle_minutes: int) -> JournalState:
    """청산 내역으로 closed_count·decay_state·bias 를 다시 계산해 state 를 갱신(제자리)하고 돌려준다."""
    trades = list(trades or [])
    state.closed_count = len(trades)
    state.decay_state = decay_state(trades)  # type: ignore[assignment]
    returns = {k: v.get("return_1h_pct") for k, v in state.entry_context.items() if isinstance(v, Mapping)}
    state.bias = bias_severities(trades, cycle_minutes, entry_returns_1h_pct=returns)  # type: ignore[assignment]
    return state


# ---------------------------------------------------------------- 성과 지표
def _returns(curve: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(curve, curve[1:]):
        if _finite(prev) and _finite(cur) and prev > 0:
            out.append(cur / prev - 1.0)
    return out


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def max_drawdown_pct(curve: Sequence[float]) -> float | None:
    """최고점 대비 최대 낙폭(% 단위, 양수). 유효 값이 없으면 None."""
    vals = [float(v) for v in curve if _finite(v)]
    if not vals:
        return None
    peak = -math.inf
    mdd = 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd * 100.0


def sharpe_ratio(daily_returns: Sequence[float]) -> float | None:
    if len(daily_returns) < 2:
        return None
    m = sum(daily_returns) / len(daily_returns)
    var = sum((r - m) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    sd = math.sqrt(var)
    if sd <= 1e-15:
        return None
    return m / sd * math.sqrt(PERIODS_PER_YEAR)


def sortino_ratio(daily_returns: Sequence[float]) -> float | None:
    if len(daily_returns) < 2:
        return None
    m = sum(daily_returns) / len(daily_returns)
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily_returns) / len(daily_returns))
    if dd <= 1e-15:
        return None
    return m / dd * math.sqrt(PERIODS_PER_YEAR)


def _group(trades: Sequence[ClosedTrade], key) -> dict[str, dict]:  # noqa: ANN001
    out: dict[str, dict] = {}
    for t in trades:
        k = str(key(t))
        g = out.setdefault(k, {"count": 0, "pnl_pct_sum": 0.0})
        g["count"] += 1
        p = _pnl_pct(t)
        if p is not None:                     # 손익 불명 거래는 합계에 넣지 않는다(개수만 셈)
            g["pnl_pct_sum"] += p
    return dict(sorted(out.items()))


def _setup_of(trade: ClosedTrade, setups: Mapping[str, Any] | None) -> str:
    if setups:
        for k in (entry_key(trade.symbol, trade.opened_ts), trade_id(trade)):
            if k in setups:
                v = setups[k]
                if isinstance(v, Mapping):
                    v = v.get("setup_type")
                if isinstance(v, str) and v:
                    return v
    return "unknown"


def code_metrics(
    trades: list[ClosedTrade],
    equity_curve: list[float],
    btc_curve: list[float] | None,
    *,
    setups: Mapping[str, Any] | None = None,
) -> dict:
    """코드가 계산하는 성과 지표(반올림 없음, % 단위는 ``*_pct``).

    - 거래: n_trades, wins, losses, win_rate(0..1), profit_factor(손실 없으면 None, ``profit_factor_infinite``),
      avg_win_pct, avg_loss_pct(음수), expectancy_pct(=평균 pnl_pct), expectancy_quote, total_pnl_quote,
      avg_bars_held, by_exit_reason/by_symbol/by_setup({"count","pnl_pct_sum"}), attribution_buckets, noise_trade_share.
    - 자본곡선(일 단위 표본 가정): total_return_pct, max_drawdown_pct(양수 %), sharpe·sortino(일수익률 × √365),
      calmar(연율화 수익률 / MDD — 일수익률 ``MIN_CALMAR_DAYS`` 개 미만이면 None: 며칠치를 ^365 로 연율화하면
      −338 같은 무의미한 값이 된다), n_days.
    - BTC: ``btc_curve`` 가 같은 길이일 때 btc_buy_hold_pct, excess_vs_btc_pct, btc_max_drawdown_pct. 아니면 None.
    """
    trades = list(trades or [])
    n = len(trades)
    wins = [t for t in trades if t.pnl_quote > 0]
    losses = [t for t in trades if t.pnl_quote < 0]
    pf = profit_factor(trades)
    pnl_pcts = _known([_pnl_pct(t) for t in trades])
    m: dict[str, Any] = {
        "n_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "profit_factor": pf if (pf is not None and math.isfinite(pf)) else None,
        "profit_factor_infinite": bool(pf is not None and math.isinf(pf)),
        "avg_win_pct": _mean(_known([_pnl_pct(t) for t in wins])),
        "avg_loss_pct": _mean(_known([_pnl_pct(t) for t in losses])),
        "expectancy_pct": _mean(pnl_pcts),
        "expectancy_quote": _mean([float(t.pnl_quote) for t in trades]),
        "total_pnl_quote": float(sum(t.pnl_quote for t in trades)),
        "avg_bars_held": _mean([float(_bars_held(t)) for t in trades]),
        "by_exit_reason": _group(trades, lambda t: t.reason),
        "by_symbol": _group(trades, lambda t: t.symbol),
        "by_setup": _group(trades, lambda t: _setup_of(t, setups)),
        "attribution_buckets": {
            "noise_trades": sum(1 for p in pnl_pcts if abs(p) < NOISE_PNL_PCT),
            "stop_losses": sum(1 for t in trades if t.reason == "stop_loss"),
            "take_profits": sum(1 for t in trades if t.reason == "take_profit"),
            "time_stops": sum(1 for t in trades if t.reason == "time_stop"),
            "head_sells": sum(1 for t in trades if t.reason == "head_sell"),
        },
        "noise_trade_share": noise_trade_share(trades),
    }

    curve = [float(v) for v in (equity_curve or []) if _finite(v)]
    rets = _returns(curve)
    total_ret = (curve[-1] / curve[0] - 1.0) * 100.0 if len(curve) >= 2 and curve[0] > 0 else None
    mdd = max_drawdown_pct(curve) if len(curve) >= 2 else None
    calmar: float | None = None
    if total_ret is not None and mdd is not None and mdd > 0 and len(rets) >= MIN_CALMAR_DAYS:
        growth = curve[-1] / curve[0]
        if growth > 0:
            ann = growth ** (PERIODS_PER_YEAR / len(rets)) - 1.0
            calmar = ann / (mdd / 100.0) if math.isfinite(ann) else None
    day_pnl = rets[-1] * 100.0 if rets and len(curve) >= 2 and curve[-2] > 0 else None
    m.update(
        {
            "n_days": len(rets),
            "day_pnl_pct": day_pnl,               # 자본곡선 마지막 구간(=당일) 수익률
            "equity_start": curve[0] if curve else None,
            "equity_end": curve[-1] if curve else None,
            "total_return_pct": total_ret,
            "max_drawdown_pct": mdd,
            "sharpe": sharpe_ratio(rets),
            "sortino": sortino_ratio(rets),
            "calmar": calmar,
        }
    )

    btc_ret = btc_mdd = excess = None
    if btc_curve is not None:
        raw_btc = list(btc_curve)
        if len(raw_btc) != len(equity_curve or []):
            log.warning("btc_curve 길이(%d) ≠ equity_curve 길이(%d) — BTC 비교 생략", len(raw_btc), len(equity_curve or []))
        else:
            # 두 곡선 모두 유한한 시점만 짝지어 같은 구간을 비교한다(각자 따로 거르면 구간이 어긋남)
            pairs = [(float(e), float(b)) for e, b in zip(equity_curve or [], raw_btc) if _finite(e) and _finite(b)]
            if len(pairs) >= 2 and pairs[0][1] > 0:
                bvals = [b for _, b in pairs]
                btc_ret = (bvals[-1] / bvals[0] - 1.0) * 100.0
                btc_mdd = max_drawdown_pct(bvals)
                e0, e1 = pairs[0][0], pairs[-1][0]
                if e0 > 0:
                    excess = (e1 / e0 - 1.0) * 100.0 - btc_ret
    m.update({"btc_buy_hold_pct": btc_ret, "excess_vs_btc_pct": excess, "btc_max_drawdown_pct": btc_mdd})
    return m


# ---------------------------------------------------------------- 상태 기록 헬퍼
def _dump_or_none(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True)
    return obj


def make_entry_verdicts(
    *,
    technical: TechnicalOutput | None,
    flow: FlowSymbolView | None,
    bull: BullOutput | None,
    bear: BearOutput | None,
    risk: RiskCandidateVerdict | None,
    head: HeadDecision | None,
    debias: Debias | None = None,
    risk_note: str | None = None,
    event_signal_score: float | None = None,
) -> dict:
    """BUY 체결 시 저장할 ``entry_verdicts[symbol]`` (SPEC v2.1 §20.3a.7 모양). 기권 채널은 None."""
    return {
        "technical": None if technical is None else {
            "setup_type": technical.setup_type, "confidence": technical.confidence},
        "bull": None if bull is None else {
            "case_strength": bull.case_strength,
            "thesis_points": [_dump_or_none(p) for p in bull.thesis_points],
            "confidence": bull.confidence},
        "bear": None if bear is None else {
            "verdict": bear.verdict, "strongest_point": bear.strongest_point,
            "death_paths": [_dump_or_none(p) for p in bear.death_paths]},
        "risk": None if risk is None else {
            "verdict": risk.verdict, "conditions": list(risk.conditions),
            "required_monitoring": list(risk.required_monitoring),
            "rr_net_estimate": risk.rr_net_estimate, "risk_note": risk_note},
        "head": None if head is None else {
            "disagreement_class": head.disagreement_class, "decisive_factor": head.decisive_factor,
            "rationale": head.rationale,
            "debias_chasing_spike": bool(debias.chasing_spike) if debias is not None else None},
        "channel_agreement": {
            "technical": bool(technical is not None and technical.direction == "long"),
            "flow": bool(flow is not None and flow.flow_bias == "long"),
            "news": bool(_finite(event_signal_score) and float(event_signal_score) > 0),
        },
    }


def normalize_entry_verdicts(verdicts: Mapping[str, Any] | None) -> dict:
    """entry_verdicts 를 정확한 키 집합으로 맞춘다(없는 채널/키는 None, 추가 키는 버림)."""
    src = _jsonable(verdicts) if verdicts else {}
    out: dict[str, Any] = {}
    for ch, keys in ENTRY_VERDICT_KEYS.items():
        sub = src.get(ch) if isinstance(src, Mapping) else None
        if ch == "channel_agreement":
            sub = sub if isinstance(sub, Mapping) else {}
            # 모르는 값은 null(SPEC v2.1) — False 로 채우면 '진입 시 롱이 아니었다'는 사실을 날조한다
            out[ch] = {k: bool(sub.get(k)) if sub.get(k) is not None else None for k in keys}
        elif isinstance(sub, Mapping):
            out[ch] = {k: sub.get(k) for k in keys}
        else:
            out[ch] = None
    return out


def record_entry_verdicts(
    state: JournalState,
    symbol: str,
    verdicts: Mapping[str, Any] | None,
    *,
    thesis: Thesis | None = None,
    opened_ts: int | None = None,
    entry_return_1h_pct: float | None = None,
) -> None:
    """BUY 체결 시 호출: 진입 판단을 동결 저장하고 심볼의 논지 추적 기록을 새로 시작한다."""
    v = normalize_entry_verdicts(verdicts)
    state.entry_verdicts[symbol] = v
    state.thesis_history[symbol] = []
    state.health_history[symbol] = []
    state.news_risk_after_entry[symbol] = []
    state.prev_assumption_status.pop(symbol, None)
    if thesis is not None:
        state.thesis[symbol] = thesis if isinstance(thesis, Thesis) else Thesis.model_validate(thesis)
    if opened_ts is not None:
        setup = (v.get("technical") or {}).get("setup_type") if v.get("technical") else None
        state.entry_context[entry_key(symbol, opened_ts)] = {
            "setup_type": setup,
            "return_1h_pct": float(entry_return_1h_pct) if _finite(entry_return_1h_pct) else None,
        }
        if len(state.entry_context) > ENTRY_CONTEXT_CAP:
            # 저장 후 다시 읽으면 키가 정렬되므로 삽입 순서가 아니라 opened_ts(키 접미사) 기준으로 오래된 것부터 버린다
            def _ts_of(k: str) -> int:
                tail = k.rsplit("-", 1)[-1]
                return int(tail) if tail.lstrip("-").isdigit() else 0

            for k in sorted(state.entry_context, key=_ts_of)[: len(state.entry_context) - ENTRY_CONTEXT_CAP]:
                state.entry_context.pop(k, None)


def set_thesis(state: JournalState, symbol: str, thesis: Thesis | Mapping[str, Any]) -> None:
    state.thesis[symbol] = thesis if isinstance(thesis, Thesis) else Thesis.model_validate(thesis)


def normalize_history_entry(entry: Mapping[str, Any]) -> dict:
    """thesis_history 한 항목을 정확한 키 집합으로(없는 값 None, 리스트 키는 [])."""
    e = dict(entry or {})
    out: dict[str, Any] = {}
    for k in THESIS_HISTORY_KEYS:
        v = e.get(k)
        if k in ("assumption_status", "red_line_hits"):
            v = [str(x) for x in v] if isinstance(v, (list, tuple)) else []
        elif k in ("cycle", "health"):
            v = int(v) if _finite(v) else None
        elif k == "exit_flag":
            v = bool(v) if v is not None else None
        elif k == "unrealized_pct":
            v = float(v) if _finite(v) else None
        elif k == "action":
            v = str(v) if v is not None else None
        out[k] = v
    return out


def append_thesis_history(
    state: JournalState,
    symbol: str,
    entry: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict:
    """보유 심볼의 사이클 평가 1건을 추가한다(``entry`` dict 또는 키워드).

    ``health_history[symbol]`` 과 ``prev_assumption_status[symbol]``(비어 있지 않을 때)도 함께 갱신한다.
    """
    merged = dict(entry or {})
    merged.update(fields)
    unknown = set(merged) - set(THESIS_HISTORY_KEYS)
    if unknown:
        log.debug("thesis_history 알 수 없는 키 무시: %s", sorted(unknown))
    item = normalize_history_entry(merged)
    hist = state.thesis_history.setdefault(symbol, [])
    hist.append(item)
    if len(hist) > HISTORY_CAP:
        del hist[: len(hist) - HISTORY_CAP]
    state.health_history[symbol] = [int(h["health"]) for h in hist if h.get("health") is not None]
    if item["assumption_status"]:
        state.prev_assumption_status[symbol] = list(item["assumption_status"])
    return item


def record_news_risk(state: JournalState, symbol: str, note: str) -> None:
    """보유 중 이 심볼에 뉴스 risk_event 가 났을 때 기록(중복 제거)."""
    lst = state.news_risk_after_entry.setdefault(symbol, [])
    if note not in lst:
        lst.append(str(note))


def record_gate(state: JournalState, key: str, n: int = 1) -> None:
    """게이트 카운터 증가(당일 ``gate_stats_today`` 와 누적 ``gate_stats`` 동시)."""
    if key not in GATE_COUNTER_KEYS:
        log.warning("알 수 없는 게이트 카운터 키: %s (그대로 기록)", key)
    n = int(n)
    state.gate_stats_today[key] = state.gate_stats_today.get(key, 0) + n
    state.gate_stats[key] = state.gate_stats.get(key, 0) + n


def record_halt(state: JournalState, reason: str) -> None:
    """당일 중단 사유(일일 한도·연속손실·낙폭·킬스위치·LLM 일시정지 등) 기록(중복 제거)."""
    if reason and reason not in state.halts_today:
        state.halts_today.append(str(reason))


def start_new_day(state: JournalState, review_date: str | None = None) -> None:
    """일일 리뷰 후 당일 카운터 초기화. ``review_date`` 가 있으면 last_daily_review_date 로 기록."""
    state.gate_stats_today = {}
    state.halts_today = []
    if review_date is not None:
        state.last_daily_review_date = review_date


def clear_symbol(state: JournalState, symbol: str) -> None:
    """청산 후(저널 호출 뒤) 심볼 항목 정리. entry_context 는 지표 집계용으로 남긴다."""
    for d in (state.entry_verdicts, state.thesis_history, state.prev_assumption_status,
              state.news_risk_after_entry, state.health_history, state.thesis):
        d.pop(symbol, None)


# ---------------------------------------------------------------- payload 빌더
def _trade_payload(trade: ClosedTrade, thesis: Thesis | Mapping[str, Any] | None) -> dict:
    d = trade.model_dump(mode="json")
    d["pnl_pct"] = _pnl_pct(trade)                      # % 단위
    d["trade_id"] = trade_id(trade)
    d["bars_held"] = _bars_held(trade)
    if thesis is None:
        d["thesis"] = None
    elif isinstance(thesis, Thesis):
        d["thesis"] = thesis.model_dump(mode="json")
    else:
        try:
            d["thesis"] = Thesis.model_validate(thesis).model_dump(mode="json")
        except ValidationError:
            log.warning("thesis 형식 오류 trade=%s — null 로 전달", d["trade_id"])
            d["thesis"] = None
    return d


def build_journal_post_trade(
    trade: ClosedTrade,
    thesis: Thesis | Mapping[str, Any] | None,
    thesis_history: Sequence[Mapping[str, Any]] | None,
    verdicts: Mapping[str, Any] | None,
    news_risk_after_entry: Sequence[str] | None = None,
) -> dict:
    """SPEC v2.1 §20.3a.7 post_trade payload.

    ``verdicts`` 는 ``JournalState.entry_verdicts[symbol]``(없으면 None). ``news_risk_after_entry`` 를 따로
    주지 않으면 ``verdicts["news_risk_after_entry"]`` 를 쓴다.
    """
    v = normalize_entry_verdicts(verdicts)
    if news_risk_after_entry is None and verdicts and isinstance(verdicts.get("news_risk_after_entry"), (list, tuple)):
        news_risk_after_entry = verdicts.get("news_risk_after_entry")  # type: ignore[assignment]
    v["news_risk_after_entry"] = [str(x) for x in (news_risk_after_entry or [])]
    payload = {
        "role": "journal",
        "mode": "post_trade",
        "trade": _trade_payload(trade, thesis),
        "thesis_history": [normalize_history_entry(e) for e in (thesis_history or [])],
        "verdicts": v,
        "hard_limits": dict(HARD_LIMITS_PAYLOAD),
    }
    return compact_payload(payload)


def _num(x: Any) -> float | None:
    return float(x) if _finite(x) else None


def _int(x: Any) -> int | None:
    return int(x) if _finite(x) else None


def _norm_groups(groups: Any) -> dict:
    out: dict[str, dict] = {}
    if isinstance(groups, Mapping):
        for k, g in groups.items():
            g = g if isinstance(g, Mapping) else {}
            out[str(k)] = {"count": _int(g.get("count")) or 0, "pnl_pct_sum": _num(g.get("pnl_pct_sum"))}
    return dict(sorted(out.items()))


def _pick(src: Any, keys: Sequence[str], conv=_num) -> dict:  # noqa: ANN001
    src = src if isinstance(src, Mapping) else {}
    return {k: conv(src.get(k)) for k in keys}


def normalize_metrics_table(table: Mapping[str, Any] | None) -> dict:
    """metrics_table 을 §20.3a.7 키 집합으로 정확히 맞춘다(없는 값 null, 추가 키 버림)."""
    t = table if isinstance(table, Mapping) else {}
    bias_src = t.get("bias_severities") if isinstance(t.get("bias_severities"), Mapping) else {}
    bias = {}
    for k in BIAS_KEYS:
        b = bias_src.get(k)
        bias[k] = b if b in BIAS_LEVELS else "insufficient_sample"
    ds = t.get("decay_state")
    gate_src = t.get("gate_stats") if isinstance(t.get("gate_stats"), Mapping) else {}
    gate = {k: (_int(gate_src.get(k)) or 0) for k in GATE_COUNTER_KEYS}
    gate.update({k: _num(gate_src.get(k)) for k in ("bear_decline_rate", "risk_veto_rate")})
    trade_stats = _pick(t.get("trade_stats"), TRADE_STATS_KEYS)
    trade_stats["closed_today"] = _int(trade_stats["closed_today"])
    halts = t.get("halts")
    return {
        "returns": _pick(t.get("returns"), RETURNS_KEYS),
        "trade_stats": trade_stats,
        "by_exit_reason": _norm_groups(t.get("by_exit_reason")),
        "by_symbol": _norm_groups(t.get("by_symbol")),
        "by_setup": _norm_groups(t.get("by_setup")),
        "attribution_buckets": _pick(t.get("attribution_buckets"), ATTRIBUTION_KEYS, _int),
        "bias_severities": bias,
        "decay_state": ds if ds in DECAY_STATES else "insufficient_sample",
        "gate_stats": gate,
        "llm": {
            "calls": _int((t.get("llm") or {}).get("calls")) if isinstance(t.get("llm"), Mapping) else None,
            "cost_usd": _num((t.get("llm") or {}).get("cost_usd")) if isinstance(t.get("llm"), Mapping) else None,
            "budget_usd": _num((t.get("llm") or {}).get("budget_usd")) if isinstance(t.get("llm"), Mapping) else None,
            "abstain_count": _int((t.get("llm") or {}).get("abstain_count")) if isinstance(t.get("llm"), Mapping) else None,
        },
        "halts": [str(h) for h in halts] if isinstance(halts, (list, tuple)) else [],
    }


def build_journal_daily(metrics_table: Mapping[str, Any], closed_count: int) -> dict:
    """SPEC v2.1 §20.3a.7 daily payload."""
    payload = {
        "role": "journal",
        "mode": "daily",
        "closed_count": int(closed_count) if _finite(closed_count) else 0,
        "hard_limits": dict(HARD_LIMITS_PAYLOAD),
        "metrics_table": normalize_metrics_table(metrics_table),
    }
    return compact_payload(payload)


def gate_stats_table(counters: Mapping[str, int] | None) -> dict:
    """게이트 카운터 + 비율. bear_decline_rate = g2/(seen−g1), risk_veto_rate = g3_veto/(seen−g1−g2)."""
    c = {k: int((counters or {}).get(k, 0) or 0) for k in GATE_COUNTER_KEYS}
    to_bear = c["candidates_seen"] - c["g1_dropped"]
    to_risk = to_bear - c["g2_bear_decline"]
    out: dict[str, Any] = dict(c)
    out["bear_decline_rate"] = c["g2_bear_decline"] / to_bear if to_bear > 0 else None
    out["risk_veto_rate"] = c["g3_risk_veto"] / to_risk if to_risk > 0 else None
    return out


def build_metrics_table(
    metrics: Mapping[str, Any],
    state: JournalState,
    *,
    closed_today: int | None = None,
    day_pnl_pct: float | None = None,
    cum_pnl_pct: float | None = None,
    equity: float | None = None,
    llm: Mapping[str, Any] | None = None,
) -> dict:
    """``code_metrics`` 결과 + JournalState 로 daily ``metrics_table`` 을 만든다.

    키워드가 없으면 ``metrics`` 의 같은 이름 키(``closed_today``, ``day_pnl_pct``, ``cum_pnl_pct``,
    ``equity``, ``llm``)를 쓰고, ``cum_pnl_pct`` 는 ``total_return_pct``, ``equity`` 는 ``equity_end``,
    ``closed_today`` 는 ``n_trades`` 로 대체한다.
    """
    m = metrics or {}
    pf = m.get("profit_factor")
    if pf is None and m.get("profit_factor_infinite"):
        pf = PF_INFINITE_CAP                  # 손실 없음 = 알려진 값(무한대). null(데이터 없음)과 구분한다

    def pick(val: Any, *keys: str) -> Any:
        if val is not None:
            return val
        for k in keys:
            if m.get(k) is not None:
                return m.get(k)
        return None

    table = {
        "returns": {
            "day_pnl_pct": pick(day_pnl_pct, "day_pnl_pct"),
            "cum_pnl_pct": pick(cum_pnl_pct, "cum_pnl_pct", "total_return_pct"),
            "equity": pick(equity, "equity", "equity_end"),
            "max_drawdown_pct": m.get("max_drawdown_pct"),
            "btc_buy_hold_pct": m.get("btc_buy_hold_pct"),
            "excess_vs_btc_pct": m.get("excess_vs_btc_pct"),
        },
        "trade_stats": {
            "closed_today": pick(closed_today, "closed_today", "n_trades"),
            **{k: m.get(k) for k in TRADE_STATS_KEYS if k not in ("closed_today", "profit_factor")},
            "profit_factor": pf,
        },
        "by_exit_reason": m.get("by_exit_reason") or {},
        "by_symbol": m.get("by_symbol") or {},
        "by_setup": m.get("by_setup") or {},
        "attribution_buckets": m.get("attribution_buckets") or {},
        "bias_severities": dict(state.bias),
        "decay_state": state.decay_state,
        "gate_stats": gate_stats_table(state.gate_stats_today),
        "llm": dict(pick(llm, "llm") or {}),
        "halts": list(state.halts_today),
    }
    return normalize_metrics_table(table)


# ---------------------------------------------------------------- 리뷰 호출
def _clip_telegram(text: str) -> str:
    return text if len(text) <= TELEGRAM_MAX_CHARS else text[: TELEGRAM_MAX_CHARS - 1] + "…"


def _run_journal(runner: Any, payload: dict, model: type[BaseModel]) -> BaseModel | None:
    try:
        result = runner.run("journal", payload, output_model=model)
    except Exception as exc:  # noqa: BLE001 - 저널 실패는 사이클을 막지 않는다(BudgetExceeded 포함)
        log.warning("저널 호출 실패 mode=%s type=%s", payload.get("mode"), type(exc).__name__)
        return None
    if result is None or not getattr(result, "ok", False):
        log.warning("저널 기권 mode=%s error=%s", payload.get("mode"), getattr(result, "error", None))
        return None
    out = getattr(result, "output", None)
    if isinstance(out, model):
        return out
    if isinstance(out, Mapping):
        try:
            return model.model_validate(out)
        except ValidationError:
            pass
    log.warning("저널 출력 형식 불일치 mode=%s type=%s", payload.get("mode"), type(out).__name__)
    return None


def post_trade_review(runner: Any, trade: ClosedTrade, ctx: Mapping[str, Any] | None) -> JournalPostTrade | None:
    """청산 직후 사후분석. 실패(기권·예외·형식 오류)면 None → 호출자는 ``code_post_trade_summary`` 사용.

    ``ctx`` 키(모두 선택): ``journal_state``(JournalState — 아래 값의 기본 출처), ``thesis``,
    ``thesis_history``, ``verdicts``, ``news_risk_after_entry``. 명시 키가 journal_state 보다 우선한다.
    """
    ctx = dict(ctx or {})
    js = ctx.get("journal_state")
    sym = trade.symbol
    thesis = ctx.get("thesis")
    history = ctx.get("thesis_history")
    verdicts = ctx.get("verdicts")
    news = ctx.get("news_risk_after_entry")
    if isinstance(js, JournalState):
        thesis = thesis if thesis is not None else js.thesis.get(sym)
        history = history if history is not None else js.thesis_history.get(sym, [])
        verdicts = verdicts if verdicts is not None else js.entry_verdicts.get(sym)
        news = news if news is not None else js.news_risk_after_entry.get(sym, [])
    payload = build_journal_post_trade(trade, thesis, history or [], verdicts, news or [])
    out = _run_journal(runner, payload, JournalPostTrade)
    if out is None:
        return None
    assert isinstance(out, JournalPostTrade)
    tid = payload["trade"]["trade_id"]
    update: dict[str, Any] = {}
    if out.trade_id != tid:
        log.warning("저널 trade_id 불일치 — 코드 값으로 교정 (%s)", tid)
        update["trade_id"] = tid
    if len(out.telegram_text) > TELEGRAM_MAX_CHARS:
        update["telegram_text"] = _clip_telegram(out.telegram_text)
    return out.model_copy(update=update) if update else out


def daily_review(runner: Any, state: JournalState, trades: Sequence[ClosedTrade], metrics: Mapping[str, Any]) -> JournalDaily | None:
    """일일 리뷰. ``metrics`` 가 이미 metrics_table(``returns``·``trade_stats`` 키 보유)이면 그대로 정규화해 쓰고,
    아니면 ``code_metrics`` 결과로 보고 ``build_metrics_table(metrics, state)`` 로 만든다.
    ``closed_count`` 는 ``state.closed_count``(0 이면 ``len(trades)``). state 가 갱신되지 않아(closed_count=0)
    ``trades`` 로 대체하는 경우 decay_state·bias 도 같은 ``trades`` 로 계산해 payload 가 자기모순이 되지 않게 한다
    (state 는 바꾸지 않는다).

    결정적 규칙(DESIGN §6, prompts/journal.md 10·11단계)은 코드가 강제한다: ``closed_count`` < 20 이거나
    ``decay_state`` 가 insufficient_sample 이면 ``action_items=[]``·``do_not_change_yet=True``,
    ``action_items`` 가 비면 ``do_not_change_yet=True`` 이고 ``abstain_reason`` 을 채운다. 실패하면 None.
    """
    m = metrics or {}
    trades = list(trades or [])
    stats_state = state
    if not state.closed_count and trades:
        stats_state = refresh_stats(state.model_copy(deep=True), trades, DEFAULT_CYCLE_MINUTES)
    if isinstance(m, Mapping) and "returns" in m and "trade_stats" in m:
        table = normalize_metrics_table(m)
    else:
        table = build_metrics_table(m, stats_state)
    closed_count = state.closed_count or len(trades)
    payload = build_journal_daily(table, closed_count)
    out = _run_journal(runner, payload, JournalDaily)
    if out is None:
        return None
    assert isinstance(out, JournalDaily)
    update: dict[str, Any] = {}
    cc = payload["closed_count"]
    ds = payload["metrics_table"]["decay_state"]
    if cc < MIN_SAMPLE or ds == "insufficient_sample":
        if out.action_items:
            log.warning("저널 일일 리뷰: 표본 부족(closed_count=%d, decay=%s)인데 action_items %d건 — 제거",
                        cc, ds, len(out.action_items))
            update["action_items"] = []
            # 텔레그램 문구가 버려진 변경안을 알릴 수 있으므로 코드 요약으로 바꾼다
            update["telegram_text"] = code_daily_summary(payload["metrics_table"], cc)
        if not out.do_not_change_yet:
            update["do_not_change_yet"] = True
        if not out.abstain_reason:
            update["abstain_reason"] = f"표본 부족(청산 {cc}건, 쇠퇴 상태 {ds}) — 보고만"
    elif not out.action_items:
        if not out.do_not_change_yet:
            update["do_not_change_yet"] = True
        if not out.abstain_reason:
            update["abstain_reason"] = "변경 제안 없음"
    text = update.get("telegram_text", out.telegram_text)
    if len(text) > TELEGRAM_MAX_CHARS:
        update["telegram_text"] = _clip_telegram(text)
    return out.model_copy(update=update) if update else out


# ---------------------------------------------------------------- 코드 요약(LLM 실패 대체)
def _fmt_opt(x: Any, spec: str = ".2f", suffix: str = "") -> str:
    return f"{x:{spec}}{suffix}" if _finite(x) else "n/a"


def code_post_trade_summary(trade: ClosedTrade) -> str:
    """LLM 사후분석 실패 시 텔레그램/로그용 코드 요약(≤400자)."""
    text = (
        f"사후분석(코드 요약) {trade.symbol} — 사유 {trade.reason}, 손익 {_fmt_opt(_pnl_pct(trade), '+.2f', '%')}"
        f" ({_fmt_opt(trade.pnl_quote, '+.2f')} USDT), 보유 {_bars_held(trade)}사이클, "
        f"진입 {_fmt_opt(trade.entry_price, '.6g')} → 청산 {_fmt_opt(trade.exit_price, '.6g')}"
    )
    return _clip_telegram(text)


def code_daily_summary(metrics_table: Mapping[str, Any], closed_count: int) -> str:
    """LLM 일일 리뷰 실패 시 코드 요약(≤400자)."""
    t = normalize_metrics_table(metrics_table)
    r, s = t["returns"], t["trade_stats"]
    bias_flags = [k for k, v in t["bias_severities"].items() if v in ("mild", "severe")]
    text = (
        f"일일 리뷰(코드 요약) 손익 {_fmt_opt(r['day_pnl_pct'], '+.2f', '%')}, BTC {_fmt_opt(r['btc_buy_hold_pct'], '+.2f', '%')}, "
        f"청산 {s['closed_today'] if s['closed_today'] is not None else 'n/a'}건, 승률 {_fmt_opt(s['win_rate'], '.0%')}, "
        f"MDD {_fmt_opt(r['max_drawdown_pct'], '.2f', '%')}, 쇠퇴 {t['decay_state']}, "
        f"편향 {','.join(bias_flags) if bias_flags else '없음'}, 누적 청산 {int(closed_count)}건"
    )
    return _clip_telegram(text)


__all__ = [
    "JournalState", "JOURNAL_STATE_FILE", "HARD_LIMITS_PAYLOAD", "MIN_SAMPLE", "MIN_CALMAR_DAYS",
    "journal_state_path", "load_journal_state", "save_journal_state", "append_journal_log",
    "evaluate_red_lines", "red_line_metrics", "bias_severities", "decay_state", "profit_factor",
    "disposition_ratio", "trades_per_day", "noise_trade_share", "refresh_stats", "code_metrics",
    "max_drawdown_pct", "sharpe_ratio", "sortino_ratio", "make_entry_verdicts", "normalize_entry_verdicts",
    "record_entry_verdicts", "set_thesis", "append_thesis_history", "normalize_history_entry", "record_news_risk",
    "record_gate", "record_halt", "start_new_day", "clear_symbol", "build_journal_post_trade",
    "build_journal_daily", "build_metrics_table", "normalize_metrics_table", "gate_stats_table",
    "post_trade_review", "daily_review", "code_post_trade_summary", "code_daily_summary",
    "compact_payload", "entry_key", "trade_id",
]
