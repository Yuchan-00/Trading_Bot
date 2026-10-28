"""에이전트 파이프라인 — Step 2~4 + G1~G3 (SPEC §20.5, v2.1 §20.3a).

순서(코드가 강제)
1. 분석가 3종을 **한 번의** ``run_many`` 로 동시에:
   - ``technical``: 후보 ∪ 보유(품질 통과·피처팩 있는 종목) 심볼마다 1회.
   - ``derivatives_flow``: 1회 일괄. 대상 심볼의 flow_pack 중 ``data_confidence != "none"`` 인 것이
     하나도 없으면 호출하지 않고 기권(``derivatives_flow:*:no_data``).
   - ``news_sentiment``: ``news_store.new_headlines(scan.news)`` 가 비어 있지 않을 때만 1회.
2. 뉴스 저장소 갱신(이벤트 24h 만료) → ``news_risk_symbols`` 계산.
3. G1(후보별) → 4. 생존 후보의 bull·bear 를 **한 번의** ``run_many`` 로 → 5. G2(약세 킬).
6. ``risk_manager`` 1회(생존 후보 또는 보유 종목이 있을 때) → 7. G3(코드가 다시 강제).
8. ``head`` 1회(승인/조건부 후보 ≥1 또는 플래그 포지션 ≥1 일 때만). 아니면 ``head_skipped_reason``.

원칙
- 어떤 역할 출력도 다른 **분석가**의 입력이 되지 않는다(bull/bear/risk/head 만 상위 출력을 본다).
- ``ok=False`` 결과는 출력 None + ``abstained`` 에 ``"<role>:<symbol>:<error>"`` (일괄 역할은 symbol ``*``).
  기권은 절대 강세 신호가 아니다: technical 기권 → G1 탈락, risk 기권 → G3 탈락, head 기권 → HOLD.
- ``BudgetExceeded`` 는 어느 단계에서든 잡아서 그 시점까지의 결과로 HOLD 처리한다:
  ``abstained`` 에 ``"budget"``, 남은 단계 출력은 None, ``head`` None, ``budget_exceeded=True``.
  (cycle 이 이 플래그를 보고 LLM 을 다음 UTC 자정까지 일시정지한다.) 비용 원장 기록 실패(OSError)는 전파한다.
- LLM 이 쓴 심볼 표기('ETH')는 base 티커로 유니버스 심볼('ETH/USDT')에 맞춘다(뉴스 위험 심볼·flow 뷰).
  요청한 심볼의 flow 뷰가 없으면 ``derivatives_flow:<sym>:missing_view`` 기권.
- 승인 후보는 후보 순위대로 남은 포지션 슬롯(MAX_POSITIONS − 보유)과 노출 상한
  (min(30%, risk global.exposure_cap_pct))으로 코드가 자른다. head BUY 도 같은 슬롯·남은 노출로 다시 자른다.
- 뉴스: 실제로 payload 에 실린 헤드라인만 seen 처리한다. 새 헤드라인이 없는 사이클에도 head 의 ``news`` 블록은
  저장소 이벤트로 채운다(market_tone 만 null). news 채널이 기권한 사이클에만 null.
- head 출력은 코드가 정리한다: 승인되지 않은 심볼의 BUY, 보유하지 않은(플래그되지 않은) 심볼의 SELL 은
  HOLD 로 강등하고, 이번 사이클 대상이 아닌 심볼의 결정은 버린다.

``NewsStore`` 는 이 모듈에 둔다: 본 헤드라인 id 와 이벤트(24h 만료)·뉴스 위험 심볼을 JSON 으로 원자 저장한다.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradingbot import gates as _gates
from tradingbot.agents import payloads as P
from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    FlowOutput,
    FlowSymbolView,
    HeadDecision,
    HeadOutput,
    NewsEvent,
    NewsOutput,
    RiskCandidateVerdict,
    RiskGlobal,
    RiskOutput,
    RiskPositionView,
    TechnicalOutput,
)
from tradingbot.config import HardLimits
from tradingbot.features import RiskPack
from tradingbot.gates import GateDecision
from tradingbot.journal import JournalState, evaluate_red_lines
from tradingbot.llm.backend import AgentResult
from tradingbot.llm.cost import BudgetExceeded
from tradingbot.models import BotState, NewsItem, Position
from tradingbot.scanner import Candidate, LiquidityGate, ScanResult

log = logging.getLogger("tradingbot.agents.pipeline")

__all__ = [
    "CandidateContext", "PositionContext", "PipelineResult", "NewsStore", "run_pipeline",
    "effective_gate", "flag_position", "BTC_SYMBOL", "HEALTH_FLAG_MAX", "NEWS_EVENT_TTL_H",
]

BTC_SYMBOL = "BTC/USDT"
HEALTH_FLAG_MAX = 4               # 건강도 ≤4 가 2사이클 연속이면 총괄에게 청산 결정을 묻는다
NEWS_EVENT_TTL_H = 24.0           # 이벤트·뉴스 위험 심볼 만료(시간)
NEWS_SEEN_TTL_H = 7 * 24.0        # 본 헤드라인 id 보관 기간(시간)
NEWS_SEEN_CAP = 5000              # 본 헤드라인 id 상한
NEWS_EVENTS_CAP = 500             # 저장 이벤트 상한
NEWS_RISK_SCORE = -0.66           # 이 점수 이하 이벤트의 심볼도 뉴스 위험으로 본다(7단계 척도의 '강한 악재')

_STAGE_G1 = "g1"
_STAGE_BEAR = "bear_decline"
_STAGE_RISK = "risk_veto"
_STAGE_G3 = "g3"
_STAGE_BUDGET = "budget"
_STAGE_DEADLINE = "cycle_deadline"   # 사이클 기한 초과로 남은 LLM 단계를 건너뜀(HOLD)
_MAX_SIZE_PCT = HardLimits.MAX_POSITION_PCT * 100.0                    # 10.0 (%)
_MAX_EXPOSURE_PCT = HardLimits.MAX_POSITIONS * _MAX_SIZE_PCT           # 30.0 (%) — gates 와 같은 정의


# ======================================================================= 결과 모델
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class CandidateContext(_Model):
    symbol: str
    candidate: Candidate
    technical: TechnicalOutput | None = None
    flow: FlowSymbolView | None = None
    bull: BullOutput | None = None
    bear: BearOutput | None = None
    risk: RiskCandidateVerdict | None = None
    gates: list[GateDecision] = Field(default_factory=list)
    dropped_at: str | None = None        # "g1" | "bear_decline" | "risk_veto" | "g3" | "budget" | "cycle_deadline" | None(생존)


class PositionContext(_Model):
    symbol: str
    technical: TechnicalOutput | None = None
    risk: RiskPositionView | None = None
    red_line_hits: list[str] = Field(default_factory=list)
    health_history: list[int] = Field(default_factory=list)
    flagged_for_head: bool = False
    flag_reason: str | None = None


class PipelineResult(_Model):
    candidates: list[CandidateContext] = Field(default_factory=list)
    positions: list[PositionContext] = Field(default_factory=list)
    news: NewsOutput | None = None
    flow: FlowOutput | None = None
    risk_global: RiskGlobal | None = None
    head: HeadOutput | None = None
    abstained: list[str] = Field(default_factory=list)
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    gate_log: list[GateDecision] = Field(default_factory=list)
    head_skipped_reason: str | None = None
    # --- SPEC 외 추가(cycle 편의) ---
    budget_exceeded: bool = False                        # BudgetExceeded 로 중단됨 → cycle 이 LLM 일시정지
    news_risk_symbols: list[str] = Field(default_factory=list)
    risk_note: str | None = None                         # RiskOutput.risk_note (entry_verdicts 용)
    risk_confidence: float | None = None                 # RiskOutput.confidence
    gate_counts: dict[str, int] = Field(default_factory=dict)   # journal.record_gate 용 카운터
    # _sanitize_head 가 총괄 BUY 를 HOLD 로 강등한 기록 [{symbol, reason}] → cycle 이 gate_downgrade 알림(SPEC v2 §7)
    head_downgrades: list[dict[str, str]] = Field(default_factory=list)

    def approved(self) -> list[CandidateContext]:
        """G3 까지 살아남은(승인/조건부) 후보."""
        return [c for c in self.candidates if c.dropped_at is None and c.risk is not None
                and c.risk.verdict in ("approve", "conditional")]

    def decision_for(self, symbol: str) -> HeadDecision | None:
        if self.head is None:
            return None
        for d in self.head.decisions:
            if d.symbol == symbol:
                return d
        return None


# ======================================================================= NewsStore
def _finite(x: Any) -> bool:
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


class NewsStore:
    """본 헤드라인 id·뉴스 이벤트·뉴스 위험 심볼의 영속 저장소(JSON, 원자 저장).

    - ``new_headlines(items)``: 아직 처리하지 않은 헤드라인만(목록 안 중복도 제거). id = ``payloads.headline_id``.
    - ``mark_seen(items, now_ms)``: 뉴스 분석이 **성공한 뒤** 호출(실패하면 다음 사이클에 다시 시도).
    - ``add_events(events, now_ms, ts_by_id)``: NewsEvent 저장. 만료 = 헤드라인 시각(없으면 now) + 24h.
    - ``add_risk_symbols(symbols, now_ms)``: risk_event 심볼을 24h 동안 기억(새 헤드라인이 없는 사이클에도 G1 차단 유지).
    - ``active_events(now_ms)`` / ``risk_symbols(now_ms)``: 만료 전 항목만.
    - ``path`` 가 None 이면 메모리 전용(테스트). 손상 파일은 경고 후 빈 상태로 시작한다(캐시이므로).
    """

    VERSION = 1

    def __init__(self, path: Path | str | None = None, *, event_ttl_h: float = NEWS_EVENT_TTL_H,
                 seen_ttl_h: float = NEWS_SEEN_TTL_H) -> None:
        self.path = Path(path) if path is not None else None
        self.event_ttl_ms = int(float(event_ttl_h) * 3_600_000)
        self.seen_ttl_ms = int(float(seen_ttl_h) * 3_600_000)
        self.seen: dict[str, int] = {}                 # headline_id -> 처음 본 시각(ms)
        self.events: dict[str, dict] = {}              # headline_id -> {headline_id, score, event_type, symbols, summary, stale, ts, expires_ts}
        self.risk: dict[str, int] = {}                 # symbol -> 만료 시각(ms)
        if self.path is not None:
            self._load()

    # ------------------------------------------------------------ 영속화
    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("루트가 객체가 아님")
            seen = data.get("seen") or {}
            events = data.get("events") or {}
            risk = data.get("risk") or {}
            self.seen = {str(k): int(v) for k, v in seen.items() if _finite(v)}
            self.events = {str(k): dict(v) for k, v in events.items() if isinstance(v, Mapping)
                           and _finite(v.get("expires_ts"))}
            self.risk = {str(k): int(v) for k, v in risk.items() if _finite(v)}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("뉴스 저장소 손상/읽기 실패 — 빈 상태로 시작 path=%s type=%s", self.path, type(exc).__name__)
            self.seen, self.events, self.risk = {}, {}, {}

    def to_dict(self) -> dict:
        return {"version": self.VERSION, "seen": dict(sorted(self.seen.items())),
                "events": dict(sorted(self.events.items())), "risk": dict(sorted(self.risk.items()))}

    def save(self) -> Path | None:
        """원자 저장(tmp → fsync → replace). 메모리 전용이면 None."""
        if self.path is None:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, indent=1)
        fd, tmp = tempfile.mkstemp(prefix=".news_store.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path

    # ------------------------------------------------------------ 조회·갱신
    @staticmethod
    def headline_id(item: Any) -> str:
        return P.headline_id(item)

    def new_headlines(self, items: Iterable[NewsItem] | None) -> list[NewsItem]:
        out: list[NewsItem] = []
        ids: set[str] = set()
        for it in items or []:
            hid = self.headline_id(it)
            if hid in self.seen or hid in ids:
                continue
            ids.add(hid)
            out.append(it)
        return out

    def mark_seen(self, items: Iterable[Any] | None, now_ms: int) -> None:
        for it in items or []:
            self.seen.setdefault(self.headline_id(it), int(now_ms))

    def add_events(self, events: Iterable[NewsEvent | Mapping[str, Any]] | None, now_ms: int,
                   ts_by_id: Mapping[str, int | None] | None = None) -> list[dict]:
        """이벤트 저장(같은 headline_id 는 최신 평가로 덮는다). 저장한 레코드 목록을 돌려준다."""
        ts_by_id = ts_by_id or {}
        added: list[dict] = []
        for ev in events or []:
            d = ev.model_dump(mode="json") if isinstance(ev, BaseModel) else dict(ev)
            hid = str(d.get("headline_id") or "")
            if not hid:
                continue
            ts = ts_by_id.get(hid)
            if not _finite(ts):
                prev = self.events.get(hid)
                ts = prev.get("ts") if prev and _finite(prev.get("ts")) else int(now_ms)
            ts = int(ts)
            score = d.get("score")
            rec = {"headline_id": hid, "score": float(score) if _finite(score) else None,
                   "event_type": d.get("event_type"), "symbols": [str(s) for s in d.get("symbols") or []],
                   "summary": d.get("summary"), "stale": bool(d.get("stale")), "ts": ts,
                   "expires_ts": ts + self.event_ttl_ms}
            self.events[hid] = rec
            added.append(rec)
        return added

    def add_risk_symbols(self, symbols: Iterable[str] | None, now_ms: int) -> None:
        exp = int(now_ms) + self.event_ttl_ms
        for s in symbols or []:
            s = str(s)
            if s:
                self.risk[s] = max(self.risk.get(s, 0), exp)

    def prune(self, now_ms: int) -> None:
        now = int(now_ms)
        self.events = {k: v for k, v in self.events.items() if int(v.get("expires_ts", 0)) > now}
        self.risk = {k: v for k, v in self.risk.items() if v > now}
        self.seen = {k: v for k, v in self.seen.items() if now - v < self.seen_ttl_ms}
        if len(self.seen) > NEWS_SEEN_CAP:
            keep = sorted(self.seen.items(), key=lambda kv: (-kv[1], kv[0]))[:NEWS_SEEN_CAP]
            self.seen = dict(keep)
        if len(self.events) > NEWS_EVENTS_CAP:
            keep_ev = sorted(self.events.items(), key=lambda kv: (-int(kv[1].get("ts", 0)), kv[0]))[:NEWS_EVENTS_CAP]
            self.events = dict(keep_ev)

    def active_events(self, now_ms: int) -> list[dict]:
        """만료 전 이벤트(최신순). 각 레코드에 ``ts`` 가 있어 payload 빌더가 나이를 다시 계산한다."""
        now = int(now_ms)
        evs = [dict(v) for v in self.events.values() if int(v.get("expires_ts", 0)) > now]
        evs.sort(key=lambda e: (-int(e.get("ts", 0)), e.get("headline_id", "")))
        for e in evs:
            e.pop("expires_ts", None)
        return evs

    def risk_symbols(self, now_ms: int) -> set[str]:
        now = int(now_ms)
        return {s for s, exp in self.risk.items() if exp > now}

    def __repr__(self) -> str:
        return f"NewsStore(path={str(self.path) if self.path else None!r}, seen={len(self.seen)}, events={len(self.events)})"


# ======================================================================= 도우미
def _abst(role: str, symbol: str, error: str | None) -> str:
    return f"{role}:{symbol}:{error or 'unknown'}"


def _call_gate(fn: Any, *args: Any, **kwargs: Any) -> GateDecision:
    """게이트 호출. 게이트가 받지 않는 키워드 인자는 빼고 호출한다(시그니처 진화 관용)."""
    try:
        params = inspect.signature(fn).parameters
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return fn(*args, **kwargs)


def _gate(name: str, passed: bool, reasons: list[str], **kw: Any) -> GateDecision:
    return GateDecision(gate=name, passed=passed, reasons=reasons, **kw)


def effective_gate(ctx: CandidateContext, risk_global: RiskGlobal | None = None) -> dict:
    """후보의 실효 게이트(head payload ``gate`` 블록 / G4 입력).

    ``confidence_floor`` = max(하드 최소, 각 게이트 하한, 리스크 전역 하한), ``size_multiplier`` = 각 게이트 배수의 곱
    (0..1 로 클램프), ``notes`` = 합집합(순서 유지). 조이기만 한다.
    """
    floor = float(HardLimits.MIN_CONFIDENCE)
    mult = 1.0
    notes: list[str] = []
    for g in ctx.gates:
        if _finite(g.confidence_floor):
            floor = max(floor, float(g.confidence_floor))
        if _finite(g.size_multiplier):
            mult *= float(g.size_multiplier)
        for n in g.notes:
            if n not in notes:
                notes.append(n)
    if risk_global is not None and _finite(risk_global.confidence_floor):
        floor = max(floor, float(risk_global.confidence_floor))
    mult = max(0.0, min(1.0, mult))
    return {"confidence_floor": min(1.0, floor), "size_multiplier": mult, "notes": notes}


def flag_position(risk_view: RiskPositionView | None, health_history: Sequence[int], technical: TechnicalOutput | None,
                  red_line_hits: Sequence[str]) -> tuple[bool, str | None]:
    """보유 포지션을 총괄에게 올릴지(SPEC DESIGN §6). ``health_history`` 는 이번 사이클 평가를 포함한 기록."""
    reasons: list[str] = []
    if risk_view is not None and risk_view.action in ("trim", "exit"):
        reasons.append(f"risk_action:{risk_view.action}")
    hh = [int(h) for h in health_history if _finite(h)]
    if len(hh) >= 2 and hh[-1] <= HEALTH_FLAG_MAX and hh[-2] <= HEALTH_FLAG_MAX:
        reasons.append(f"health_le_{HEALTH_FLAG_MAX}_x2:{hh[-2]},{hh[-1]}")
    if technical is not None and technical.exit_flag:
        reasons.append("technical_exit_flag" + (f":{technical.exit_reason}" if technical.exit_reason else ""))
    if red_line_hits:
        reasons.append("red_line:" + "; ".join(red_line_hits))
    return (bool(reasons), "; ".join(reasons) if reasons else None)


def _btc_return_1h_pct(scan: ScanResult) -> float | None:
    fp = scan.feature_packs.get(BTC_SYMBOL)
    if fp is None or len(fp.last_closes_1h) < 2:
        return None
    a, b = fp.last_closes_1h[-2], fp.last_closes_1h[-1]
    if not (_finite(a) and _finite(b)) or a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def _mark(scan: ScanResult, pos: Position) -> float:
    m = scan.marks.get(pos.symbol)
    if _finite(m) and m > 0:
        return float(m)
    if _finite(pos.last_mark) and (pos.last_mark or 0) > 0:
        return float(pos.last_mark)  # type: ignore[arg-type]
    return float(pos.entry_price)


def _budget_remaining(runner: Any) -> float | None:
    ledger = getattr(getattr(runner, "backend", None), "ledger", None)
    fn = getattr(ledger, "remaining_today", None)
    if not callable(fn):
        return None
    try:
        v = fn()
        return float(v) if _finite(v) else None
    except Exception:  # noqa: BLE001 - 참고 정보일 뿐
        return None


def _llm_eligible(scan: ScanResult, symbol: str) -> bool:
    """LLM 입력 대상인가: 피처팩이 있고 품질 실패가 아니어야 한다."""
    if symbol not in scan.feature_packs:
        return False
    q = scan.quality.get(symbol)
    return not (q is not None and not q.ok)


def _norm_symbols(raw: Iterable[Any] | None, universe: Sequence[str]) -> set[str]:
    """LLM 이 쓴 심볼 표기를 유니버스 심볼로 정규화한다('ETH' → 'ETH/USDT'). 정확 일치 우선, 아니면 base 티커 일치
    전부. 유니버스에 없는 표기는 원문 그대로 둔다(조이기만: 차단 집합을 줄이지 않는다)."""
    out: set[str] = set()
    uni = list(dict.fromkeys(str(u) for u in universe))
    for r in raw or []:
        r = str(r).strip()
        if not r:
            continue
        out.add(r)
        if r in uni:
            continue
        b = P._base(r)
        out.update(u for u in uni if P._base(u) == b)
    return out


def _match_view(symbol: str, views: Sequence[FlowSymbolView]) -> FlowSymbolView | None:
    """심볼의 flow 뷰: 정확 일치 우선, 없으면 base 티커 일치(payloads._symbol_in 과 같은 규칙, 심볼은 정규화)."""
    for v in views:
        if v.symbol == symbol:
            return v
    b = P._base(symbol)
    for v in views:
        if P._base(v.symbol) == b:
            return v.model_copy(update={"symbol": symbol})     # 표기 정규화(G1 의 심볼 일치 검사 통과)
    return None


def _first_failed_reason(gate: GateDecision) -> str:
    return "; ".join(gate.reasons) if gate.reasons else gate.gate


class _Acc:
    """실행 중 누적(호출 수·비용·기권)."""

    def __init__(self) -> None:
        self.calls = 0
        self.cost = 0.0
        self.abstained: list[str] = []
        self.budget = False
        self.deadline: str | None = None   # 기한 초과로 멈춘 단계 이름(None = 기한 내)

    def take(self, results: Sequence[AgentResult]) -> None:
        for r in results:
            self.calls += 1
            if _finite(r.cost_usd):
                self.cost += float(r.cost_usd)

    def abstain(self, entry: str) -> None:
        if entry not in self.abstained:
            self.abstained.append(entry)

    def budget_hit(self, stage: str) -> None:
        log.warning("LLM 예산 초과 — 단계 %s 에서 중단, 남은 단계는 HOLD", stage)
        self.budget = True
        self.abstain("budget")


def _output(result: AgentResult | None, model: type[BaseModel]) -> Any:
    if result is None or not result.ok or not isinstance(result.output, model):
        return None
    return result.output


# ======================================================================= 파이프라인
def run_pipeline(scan: ScanResult, state: BotState, equity: float, runner: Any, journal_state: JournalState,
                 now_ms: int, *, news_store: NewsStore,
                 should_stop: Callable[[str], bool] | None = None) -> PipelineResult:
    """SPEC §20.5. ``runner`` 는 ``AgentRunner``(``run_many`` 필요).

    ``should_stop(stage)`` (선택, cycle 이 사이클 기한 검사로 넘긴다)이 단계 경계(bull/bear·risk_manager·head 앞)에서
    True 를 돌려주면 남은 LLM 단계를 부르지 않고 HOLD 로 끝낸다(``head_skipped_reason="cycle_deadline"``, 살아 있던
    후보는 ``dropped_at="cycle_deadline"``, 보유 플래그 해제). 예산 초과와 달리 LLM 일시정지는 걸지 않는다.
    """
    acc = _Acc()

    def _stop(stage: str) -> bool:
        if should_stop is None:
            return False
        try:
            hit = bool(should_stop(stage))
        except Exception as exc:  # noqa: BLE001 - 기한 콜백 실패는 기한 내로 본다(조이기만 하는 장치)
            log.warning("기한 콜백 실패 stage=%s type=%s", stage, type(exc).__name__)
            return False
        if hit:
            log.warning("사이클 기한 초과 — 단계 %s 부터 LLM 호출 생략, 남은 단계는 HOLD", stage)
            acc.deadline = stage
            acc.abstain(_STAGE_DEADLINE)
        return hit
    gate_log: list[GateDecision] = []
    counts: dict[str, int] = {k: 0 for k in ("candidates_seen", "g1_dropped", "g2_bear_decline",
                                             "g3_risk_veto", "g3_other", "head_calls")}
    cycle = int(state.cycle)
    sentiment = scan.sentiment
    regime = scan.regime
    marks = dict(scan.marks)

    # ---------------------------------------------------------------- 대상 심볼
    cand_list: list[Candidate] = []
    seen_c: set[str] = set()
    for c in scan.candidates:
        if c.symbol in seen_c:
            continue
        seen_c.add(c.symbol)
        cand_list.append(c)
    counts["candidates_seen"] = len(cand_list)
    held = sorted(state.positions)
    held_llm = [s for s in held if s not in seen_c and _llm_eligible(scan, s)]
    for s in held:
        if s not in held_llm and s not in seen_c:
            acc.abstain(_abst("technical", s, "quality_excluded"))
    cand_syms = [c.symbol for c in cand_list]
    tech_syms = [s for s in cand_syms if _llm_eligible(scan, s)] + held_llm
    for s in cand_syms:
        if s not in tech_syms:
            acc.abstain(_abst("technical", s, "no_feature_pack"))
    flow_syms = list(tech_syms)

    ctxs: dict[str, CandidateContext] = {c.symbol: CandidateContext(symbol=c.symbol, candidate=c) for c in cand_list}
    pos_ctx: dict[str, PositionContext] = {s: PositionContext(symbol=s) for s in held}
    account_pack = P.make_account_pack(state, equity, marks=marks, journal_state=journal_state,
                                       llm_budget_remaining_usd=_budget_remaining(runner))
    exposure_pct = account_pack.get("exposure_pct")      # None(자본 무효) → G1 이 보수적으로 탈락시킨다

    news_out: NewsOutput | None = None
    flow_out: FlowOutput | None = None
    risk_out: RiskOutput | None = None
    head_out: HeadOutput | None = None
    head_skipped: str | None = None
    head_downgrades: list[dict[str, str]] = []
    vetoed: list[dict] = []

    def _finish(reason_if_budget: str = "budget_exceeded") -> PipelineResult:
        nonlocal head_skipped
        if acc.budget:
            for c in ctxs.values():
                if c.dropped_at is None:
                    c.dropped_at = _STAGE_BUDGET
            for p in pos_ctx.values():
                p.flagged_for_head = False
            head_skipped = reason_if_budget
        elif acc.deadline is not None:
            for c in ctxs.values():
                if c.dropped_at is None:
                    c.dropped_at = _STAGE_DEADLINE
            for p in pos_ctx.values():
                p.flagged_for_head = False
            head_skipped = _STAGE_DEADLINE
        try:
            news_store.prune(now_ms)
            news_store.save()
        except OSError as exc:
            log.error("뉴스 저장소 저장 실패(계속 진행) type=%s", type(exc).__name__)
        return PipelineResult(
            candidates=[ctxs[s] for s in cand_syms], positions=[pos_ctx[s] for s in held], news=news_out,
            flow=flow_out, risk_global=risk_out.global_ if risk_out is not None else None,
            head=None if (acc.budget or acc.deadline is not None) else head_out, abstained=list(acc.abstained), llm_calls=acc.calls,
            llm_cost_usd=round(acc.cost, 6), gate_log=gate_log, head_skipped_reason=head_skipped,
            budget_exceeded=acc.budget, news_risk_symbols=sorted(news_risk), risk_note=None if risk_out is None
            else risk_out.risk_note, risk_confidence=None if risk_out is None else risk_out.confidence,
            gate_counts=dict(counts), head_downgrades=[] if acc.budget else list(head_downgrades),
        )

    news_risk: set[str] = news_store.risk_symbols(now_ms)

    # ================================================================ 1. 분석가
    calls: list[tuple[str, dict]] = []
    tags: list[tuple[str, str]] = []           # (role, symbol)
    for s in tech_syms:
        pos = state.positions.get(s)
        payload = P.build_technical(s, scan.feature_packs[s], pos, _mark(scan, pos) if pos else None, cycle)
        calls.append(("technical", payload))
        tags.append(("technical", s))

    flow_has_data = any((scan.flow_packs.get(s) is not None and scan.flow_packs[s].data_confidence != "none")
                        for s in flow_syms)
    if flow_syms and flow_has_data:
        calls.append(("derivatives_flow", P.build_flow(scan.flow_packs, flow_syms)))
        tags.append(("derivatives_flow", "*"))
    elif flow_syms:
        acc.abstain(_abst("derivatives_flow", "*", "no_data"))

    new_heads = news_store.new_headlines(scan.news)
    sent_heads: list[NewsItem] = []
    risk_universe = list(dict.fromkeys([*scan.universe, *cand_syms, *held]))
    if new_heads:
        universe = list(dict.fromkeys([*scan.universe, *cand_syms, *held]))
        fh = P.feed_health_from(scan.news, [] if scan.news_available else ["news_unavailable"])
        news_payload = P.build_news(new_heads, P.build_aliases(universe), int(now_ms), fh,
                                    news_store.active_events(now_ms))
        sent_ids = {str(h.get("id")) for h in news_payload.get("headlines") or [] if isinstance(h, Mapping)}
        sent_heads = [h for h in new_heads if news_store.headline_id(h) in sent_ids]
        calls.append(("news_sentiment", news_payload))
        tags.append(("news_sentiment", "*"))
    elif not scan.news_available:
        acc.abstain(_abst("news_sentiment", "*", "feed_unavailable"))

    results: list[AgentResult] = []
    if calls:
        try:
            results = runner.run_many(calls)
        except BudgetExceeded:
            acc.budget_hit("analysts")
            return _finish()
    acc.take(results)
    tech_out: dict[str, TechnicalOutput | None] = {}
    tech_abst: set[str] = set()
    for (role, sym), res in zip(tags, results):
        if role == "technical":
            out = _output(res, TechnicalOutput)
            if out is not None and out.symbol != sym:
                log.warning("technical 출력 심볼 불일치 expected=%s got=%s — 기권 처리", sym, out.symbol)
                out = None
                acc.abstain(_abst("technical", sym, "symbol_mismatch"))
            elif out is None:
                acc.abstain(_abst("technical", sym, res.error))
            if out is None:
                tech_abst.add(sym)
            tech_out[sym] = out
        elif role == "derivatives_flow":
            flow_out = _output(res, FlowOutput)
            if flow_out is None:
                acc.abstain(_abst("derivatives_flow", "*", res.error))
        elif role == "news_sentiment":
            news_out = _output(res, NewsOutput)
            if news_out is None:
                acc.abstain(_abst("news_sentiment", "*", res.error))
    for s in tech_syms:
        tech_out.setdefault(s, None)
    flow_views: dict[str, FlowSymbolView] = {}
    if flow_out is not None:
        for s in flow_syms:
            v = _match_view(s, flow_out.per_symbol)
            if v is None:
                acc.abstain(_abst("derivatives_flow", s, "missing_view"))
            else:
                flow_views[s] = v

    # ================================================================ 2. 뉴스 저장소
    if news_out is not None:
        ts_by_id = {news_store.headline_id(h): h.published_ts for h in new_heads}
        news_store.add_events(news_out.events, now_ms, ts_by_id)
        news_store.mark_seen(sent_heads, now_ms)        # 페이로드에서 잘린 헤드라인은 다음 사이클에 다시
        raw_risk: list[str] = list(news_out.risk_event_symbols)
        for ev in news_out.events:
            if ev.score <= NEWS_RISK_SCORE and not ev.stale:
                raw_risk.extend(ev.symbols)
        news_store.add_risk_symbols(sorted(_norm_symbols(raw_risk, risk_universe)), now_ms)
    news_risk = news_store.risk_symbols(now_ms)
    news_events = news_store.active_events(now_ms)

    for s, c in ctxs.items():
        c.technical = tech_out.get(s)
        c.flow = flow_views.get(s)

    # ================================================================ 3. G1
    survivors: list[str] = []
    for s in cand_syms:
        c = ctxs[s]
        rp = scan.risk_packs.get(s)
        corr = dict(rp.corr_to_open_positions) if rp is not None else {}
        g = _call_gate(_gates.g1_candidate, s, c.technical, c.flow, set(news_risk), sentiment, len(state.positions),
                       exposure_pct, corr, s in tech_abst or c.technical is None, fp=scan.feature_packs.get(s))
        c.gates.append(g)
        gate_log.append(g)
        if g.passed:
            survivors.append(s)
        else:
            c.dropped_at = _STAGE_G1
            counts["g1_dropped"] += 1
            vetoed.append({"symbol": s, "stage": _STAGE_G1, "reason": _first_failed_reason(g)})

    # ================================================================ 4. bull + bear (한 번의 run_many)
    if survivors and _stop("debate"):
        return _finish()
    if survivors:
        # §20.3a.4: news_context 는 뉴스 채널이 기권했을 때만 null. 새 헤드라인이 없어 news_sentiment 를 부르지 않은
        # 평상 사이클은 기권이 아니다 → 총괄 news 블록(_head_news_ctx)과 같은 상태: market_tone null + 저장소 위험 심볼.
        debate_news: Any = news_out
        if news_out is None and not any(a.startswith("news_sentiment:") for a in acc.abstained):
            debate_news = {"market_tone": None, "risk_event_symbols": sorted(news_risk),
                           "feed_health_note": "새 헤드라인 없음(news_sentiment 미호출, 기권 아님)"}
        calls = []
        for s in survivors:
            c = ctxs[s]
            fp = scan.feature_packs.get(s)
            rp = scan.risk_packs.get(s)
            common = dict(feature_pack=fp, news=debate_news, liquidity=scan.liquidity.get(s), now_ms=int(now_ms))
            calls.append(("bull", P.build_bull(s, c.technical, c.flow, news_events, sentiment, c.candidate, regime,
                                               None, **common)))
            calls.append(("bear", P.build_bear(s, c.technical, c.flow, news_events, sentiment, c.candidate, regime,
                                               None, dict(rp.corr_to_open_positions) if rp else {},
                                               fp.hv_pct_rank if fp else None, **common)))
        try:
            results = runner.run_many(calls)
        except BudgetExceeded:
            acc.budget_hit("debate")
            return _finish()
        acc.take(results)
        for i, s in enumerate(survivors):
            c = ctxs[s]
            rb, rr = results[2 * i], results[2 * i + 1]
            c.bull = _output(rb, BullOutput)
            if c.bull is not None and c.bull.symbol != s:
                c.bull = None
                acc.abstain(_abst("bull", s, "symbol_mismatch"))
            elif c.bull is None:
                acc.abstain(_abst("bull", s, rb.error))
            c.bear = _output(rr, BearOutput)
            if c.bear is not None and c.bear.symbol != s:
                c.bear = None
                acc.abstain(_abst("bear", s, "symbol_mismatch"))
            elif c.bear is None:
                acc.abstain(_abst("bear", s, rr.error))

    # ================================================================ 5. G2
    survivors2: list[str] = []
    for s in survivors:
        c = ctxs[s]
        g = _gates.g2_bear(c.bear)
        c.gates.append(g)
        gate_log.append(g)
        if g.passed:
            survivors2.append(s)
        else:
            c.dropped_at = _STAGE_BEAR
            counts["g2_bear_decline"] += 1
            vetoed.append({"symbol": s, "stage": _STAGE_BEAR, "reason": _first_failed_reason(g)})

    # ---------------------------------------------------------------- 보유 포지션 코드 평가(레드라인)
    btc_ret = _btc_return_1h_pct(scan)
    sent_score = sentiment.score_0_100 if sentiment is not None else None
    for s in held:
        pos = state.positions[s]
        pc = pos_ctx[s]
        pc.technical = tech_out.get(s)
        thesis = journal_state.thesis.get(s)
        hits: list[str] = []
        if thesis is not None:
            fpk = scan.flow_packs.get(s)
            try:
                hits = evaluate_red_lines(thesis, _mark(scan, pos), pos, fpk.funding_now if fpk else None, btc_ret,
                                          sent_score, int(now_ms))
            except Exception as exc:  # noqa: BLE001 - 평가 실패는 적중 없음 + 경고
                log.warning("레드라인 평가 실패 symbol=%s type=%s", s, type(exc).__name__)
        pc.red_line_hits = hits
        pc.health_history = list(journal_state.health_history.get(s, []))

    planned_notional = max(0.0, float(equity) * HardLimits.MAX_POSITION_PCT) if _finite(equity) else 0.0

    # ================================================================ 6. risk_manager
    if (survivors2 or held_llm) and _stop("risk_manager"):
        return _finish()
    if survivors2 or held_llm:
        cands_payload = [{"symbol": s, "candidate": ctxs[s].candidate, "technical": ctxs[s].technical,
                          "flow": ctxs[s].flow, "bull": ctxs[s].bull, "bear": ctxs[s].bear,
                          "planned_notional_usdt": planned_notional} for s in survivors2]
        pos_payload = []
        for s in held_llm:
            pos = state.positions[s]
            pc = pos_ctx[s]
            pos_payload.append({"symbol": s, "position": pos, "mark": _mark(scan, pos), "cycle": cycle,
                                "thesis": journal_state.thesis.get(s),
                                "prev_assumption_status": journal_state.prev_assumption_status.get(s),
                                "red_line_hits": pc.red_line_hits, "health_history": pc.health_history,
                                "technical": pc.technical})
        payload = P.build_risk(cands_payload, pos_payload, scan.risk_packs, regime, account_pack, sorted(news_risk),
                               flow_out if flow_out is not None else {})
        try:
            results = runner.run_many([("risk_manager", payload)])
        except BudgetExceeded:
            acc.budget_hit("risk_manager")
            return _finish()
        acc.take(results)
        risk_out = _output(results[0] if results else None, RiskOutput)
        if risk_out is None:
            acc.abstain(_abst("risk_manager", "*", results[0].error if results else "no_result"))

    verdicts: dict[str, RiskCandidateVerdict] = {}
    views: dict[str, RiskPositionView] = {}
    if risk_out is not None:
        for v in risk_out.candidates:
            verdicts.setdefault(v.symbol, v)
        for pv in risk_out.positions:
            views.setdefault(pv.symbol, pv)
    risk_global = risk_out.global_ if risk_out is not None else None

    # ================================================================ 7. G3
    approved: list[str] = []
    for s in survivors2:
        c = ctxs[s]
        c.risk = verdicts.get(s)
        if risk_out is not None and c.risk is None:
            acc.abstain(_abst("risk_manager", s, "missing_verdict"))
        rp = scan.risk_packs.get(s) or RiskPack(symbol=s)
        liq = scan.liquidity.get(s) or LiquidityGate(symbol=s, ok=False, reasons=["유동성 자료 없음"])
        stops = [x for x in (c.risk.stop_pct if c.risk else None,
                             c.technical.stop_pct if c.technical else None, HardLimits.MAX_STOP_LOSS_PCT * 100)
                 if _finite(x) and x > 0]
        stop_pct = float(min(stops))
        g = _call_gate(_gates.g3_risk, s, c.risk, regime, journal_state.decay_state, dict(journal_state.bias), rp,
                       liq, planned_notional, stop_pct)
        c.gates.append(g)
        gate_log.append(g)
        if not g.passed:
            is_veto = c.risk is not None and c.risk.verdict == "veto"
            c.dropped_at = _STAGE_RISK if is_veto else _STAGE_G3
            counts["g3_risk_veto" if is_veto else "g3_other"] += 1
            vetoed.append({"symbol": s, "stage": c.dropped_at, "reason": _first_failed_reason(g)})
            continue
        if risk_global is not None and risk_global.entries_paused:
            g2 = _gate("G3", False, [f"리스크 관리자 신규 진입 일시중단: {risk_global.pause_reason or '사유 없음'}"])
            c.gates.append(g2)
            gate_log.append(g2)
            c.dropped_at = _STAGE_G3
            counts["g3_other"] += 1
            vetoed.append({"symbol": s, "stage": _STAGE_G3, "reason": _first_failed_reason(g2)})
            continue
        if c.risk is None or c.risk.verdict not in ("approve", "conditional"):
            c.dropped_at = _STAGE_RISK
            counts["g3_risk_veto"] += 1
            vetoed.append({"symbol": s, "stage": _STAGE_RISK, "reason": "리스크 승인 없음"})
            continue
        approved.append(s)

    # ---------------------------------------------------------------- 슬롯·노출 한도(코드 강제, 조이기만)
    exposure_cap = _MAX_EXPOSURE_PCT
    if risk_global is not None and _finite(risk_global.exposure_cap_pct):
        exposure_cap = min(exposure_cap, float(risk_global.exposure_cap_pct))
    slots = max(0, HardLimits.MAX_POSITIONS - len(state.positions))
    exp_now = float(exposure_pct) if _finite(exposure_pct) else None
    kept: list[str] = []
    for s in approved:                       # 후보 순위(scan 순서) 유지
        c = ctxs[s]
        reason = None
        if exp_now is None or exp_now >= exposure_cap - 1e-9:
            reason = f"총 노출 {exp_now!r}% ≥ 노출 상한 {exposure_cap:g}% — 신규 진입 불가"
        elif len(kept) >= slots:
            reason = f"남은 포지션 슬롯 없음(보유 {len(state.positions)} + 승인 {len(kept)} ≥ {HardLimits.MAX_POSITIONS})"
        if reason is None:
            kept.append(s)
            continue
        g = _gate("G3", False, [reason])
        c.gates.append(g)
        gate_log.append(g)
        c.dropped_at = _STAGE_G3
        counts["g3_other"] += 1
        vetoed.append({"symbol": s, "stage": _STAGE_G3, "reason": reason})
    approved = kept
    exposure_headroom = max(0.0, exposure_cap - exp_now) if exp_now is not None else 0.0

    # ---------------------------------------------------------------- 포지션 플래그
    flagged: list[str] = []
    for s in held:
        pc = pos_ctx[s]
        if s not in held_llm:
            continue
        pc.risk = views.get(s)
        if pc.risk is not None:
            pc.health_history = [*pc.health_history, int(pc.risk.health)]
        if risk_out is not None and pc.risk is None:
            acc.abstain(_abst("risk_manager", s, "missing_position_view"))
        pc.flagged_for_head, pc.flag_reason = flag_position(pc.risk, pc.health_history, pc.technical,
                                                            pc.red_line_hits)
        if pc.flagged_for_head:
            flagged.append(s)

    # ================================================================ 8. head
    if not approved and not flagged:
        head_skipped = "no_actionable" if (cand_syms or held) else "no_candidates_or_positions"
        log.info("총괄 호출 생략: %s", head_skipped)
        return _finish()

    if _stop("head"):
        return _finish()

    news_abstained = any(a.startswith("news_sentiment:") for a in acc.abstained)

    def _head_news_ctx(sym: str) -> dict:
        if news_out is not None:
            return {"news_output": news_out, "news_events": news_events,
                    "news": {"events": news_events, "market_tone": news_out.market_tone,
                             "risk_event_for_symbol": P._symbol_in(sym, news_risk)}}
        if news_abstained:
            return {"news_output": None, "news_events": news_events}       # → news: null (기권)
        # 새 헤드라인 없는 평상 사이클: 저장소 이벤트 + 저장된 위험 심볼로 블록 구성(market_tone 만 null)
        return {"news": {"events": news_events, "market_tone": None,
                         "risk_event_for_symbol": P._symbol_in(sym, news_risk)}, "news_events": news_events}

    approved_ctx = []
    for s in approved:
        c = ctxs[s]
        approved_ctx.append({
            "symbol": s, "candidate": c.candidate, "technical": c.technical, "feature_pack": scan.feature_packs.get(s),
            "flow": c.flow, "bull": c.bull, "bear": c.bear, "risk": c.risk, "risk_global": risk_global,
            "risk_confidence": risk_out.confidence if risk_out is not None else None,
            "gate": effective_gate(c, risk_global), **_head_news_ctx(s),
        })
    flagged_ctx = []
    for s in flagged:
        pos = state.positions[s]
        pc = pos_ctx[s]
        flagged_ctx.append({
            "symbol": s, "position": pos, "mark": _mark(scan, pos), "cycle": cycle,
            "thesis": journal_state.thesis.get(s), "red_line_hits": pc.red_line_hits,
            "health_history": pc.health_history, "risk": pc.risk, "technical": pc.technical,
            "flag_reason": pc.flag_reason, "news_events": news_events,
        })
    head_payload = P.build_head(approved_ctx, flagged_ctx, regime, account_pack, sentiment, vetoed,
                                P.hard_limits(), list(acc.abstained), now_ms=int(now_ms))
    counts["head_calls"] += 1
    try:
        results = runner.run_many([("head", head_payload)])
    except BudgetExceeded:
        acc.budget_hit("head")
        return _finish()
    acc.take(results)
    head_out = _output(results[0] if results else None, HeadOutput)
    if head_out is None:
        acc.abstain(_abst("head", "*", results[0].error if results else "no_result"))
    else:
        caps = {s: (float(ctxs[s].risk.max_size_pct) if ctxs[s].risk is not None and _finite(ctxs[s].risk.max_size_pct)
                    else _MAX_SIZE_PCT) for s in approved}
        head_out = _sanitize_head(head_out, set(approved), set(flagged), slots=slots,
                                  exposure_headroom_pct=exposure_headroom, size_caps=caps,
                                  downgrades=head_downgrades)
    return _finish()


def _sanitize_head(head: HeadOutput, approved: set[str], flagged: set[str], *, slots: int | None = None,
                   exposure_headroom_pct: float | None = None,
                   size_caps: Mapping[str, float] | None = None,
                   downgrades: list[dict[str, str]] | None = None) -> HeadOutput:
    """총괄 결정 정리(조이기만): 대상 밖 심볼 결정은 버리고, 승인 안 된 BUY·플래그 안 된 SELL 은 HOLD 로.

    ``slots``/``exposure_headroom_pct`` 가 주어지면 BUY 를 결정 순서대로 남은 포지션 슬롯 수와 남은 노출 한도
    (BUY 비중 = min(head size_pct, risk max_size_pct, 10) 의 누적합)로 제한하고 넘치는 BUY 는 HOLD 로 강등한다.

    ``downgrades`` 리스트가 주어지면 HOLD 로 강등된 총괄 BUY 마다 ``{"symbol", "reason"}`` 을 덧붙인다
    (cycle 이 notify(order_rejected, "gate_downgrade: ...") 로 알린다 — SPEC v2 §7).
    """
    out: list[HeadDecision] = []
    seen: set[str] = set()
    buys = 0
    used = 0.0
    for d in head.decisions:
        if d.symbol in seen:
            log.warning("총괄 중복 결정 무시 symbol=%s", d.symbol)
            continue
        if d.symbol not in approved and d.symbol not in flagged:
            log.warning("총괄이 대상 밖 심볼 결정 symbol=%s action=%s — 무시", d.symbol, d.action)
            continue
        seen.add(d.symbol)
        was_buy = d.action == "BUY"
        if d.action == "BUY" and d.symbol not in approved:
            log.warning("승인되지 않은 BUY → HOLD symbol=%s", d.symbol)
            d = HeadDecision.hold(d.symbol, "코드 강등: 승인되지 않은 후보의 BUY")
        elif d.action == "SELL" and d.symbol not in flagged:
            log.warning("보유/플래그 아닌 SELL → HOLD symbol=%s", d.symbol)
            d = HeadDecision.hold(d.symbol, "코드 강등: 플래그되지 않은 심볼의 SELL")
        if d.action == "BUY":
            size = min(float(d.size_pct), float((size_caps or {}).get(d.symbol, _MAX_SIZE_PCT)), _MAX_SIZE_PCT)
            if slots is not None and buys >= slots:
                log.warning("남은 포지션 슬롯 초과 BUY → HOLD symbol=%s", d.symbol)
                d = HeadDecision.hold(d.symbol, "코드 강등: 남은 포지션 슬롯 없음")
            elif exposure_headroom_pct is not None and used + size > exposure_headroom_pct + 1e-9:
                log.warning("노출 한도 초과 BUY → HOLD symbol=%s", d.symbol)
                d = HeadDecision.hold(d.symbol, "코드 강등: 총 노출 한도 초과")
            else:
                buys += 1
                used += size
        if was_buy and d.action != "BUY" and downgrades is not None:
            downgrades.append({"symbol": d.symbol, "reason": d.rationale})
        out.append(d)
    return head.model_copy(update={"decisions": out})
