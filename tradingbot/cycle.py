"""한 사이클 실행기 (SPEC §15 골격 + §22 확정 순서, v2.1).

``Cycle.run_once()`` 순서(고정)
1. 잠금(``bot.lock``) 확인·갱신, ``control/STOP`` → kill, ``control/CLEAR`` → kill 해제(파일 삭제).
2. 상태(메모리의 Portfolio) 사이클 증가, 날짜 롤오버(전일 ``daily_summary`` notify) +
   저널 일일 리뷰(00:05 UTC 이후 첫 사이클).
3. ``Scanner.scan`` → 마크 갱신, equity·peak 갱신, 낙폭 사다리(−6% 이면 ``set_kill("drawdown_stop_all")`` + notify).
4. **코드 청산**: 보유마다 ``risk.check_exits`` → market_sell → ``close_position`` → ``save`` → notify(exit)
   → 저널 사후분석(실패 무시).
5. G0. 실패면 결정 로그·보고서 쓰고 종료(LLM 0회).
6. ``run_pipeline``. ``BudgetExceeded``(→ ``PipelineResult.budget_exceeded``) → ``pause_llm(다음 UTC 자정)`` +
   notify(budget_exceeded); 인증 오류 기권 다수 → ``pause_llm(60분)`` + notify(llm_paused).
   risk_manager 가 돈 뒤 보유 종목의 ``thesis_history``·``prev_assumption_status`` 를 저널 상태에 기록한다.
7. head 결정 처리: SELL(보유) → 매도(risk action ``trim`` 이면 50%, 그 외 전량)·저장·notify(exit, head_sell);
   BUY → G4 → OrderPlan → market_buy → ``open_position``(논지 JSON) + 저널에 구조화 논지·진입 판단 스냅샷 →
   ``save`` → notify(entry). G4 강등은 notify(order_rejected, "gate_downgrade: ..."). HOLD 는 로그만.
8. 결정 로그(``logs/decisions.jsonl``), 사이클 보고서(``logs/cycles/``), 상태 저장, ``CycleSummary`` 반환.

에러 정책
- 모든 단계는 ``try/except`` 로 감싸 로그 + ``error`` 이벤트 notify 후 다음 단계로 간다.
- 예외: ``CorruptStateError``·``ConfigError``·``LockLostError``·상태 저장 실패(``StateSaveError``, ``OSError``)
  는 전파한다(사이클을 멈춘다). 파이프라인 안의 비용 원장 기록 실패(``OSError``)도 상태 저장 실패로 보고 전파한다.
- 사이클 기한(기본 300초): 단계가 기한을 넘겨 시작되면 남은 LLM 단계(파이프라인·저널)와 신규 진입은
  HOLD 로 건너뛰고(사유 ``cycle_deadline``) notify(error) 한다. 청산(코드 청산·총괄 SELL)은 위험을 줄이므로
  기한 뒤에도 수행한다.
"""
from __future__ import annotations

import json
import logging
import math
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradingbot import gates as G
from tradingbot import journal as J
from tradingbot import reports
from tradingbot import risk
from tradingbot.agents import payloads as P
from tradingbot.agents.pipeline import CandidateContext, NewsStore, PipelineResult, effective_gate, run_pipeline
from tradingbot.agents.runner import AgentRunner, _raised_from_cost_ledger
from tradingbot.agents.schemas import HeadDecision
from tradingbot.config import ConfigError, HardLimits, Settings, mask_secret
from tradingbot.exchange import Broker, MarketData
from tradingbot.journal import JournalState
from tradingbot.llm.backend import AgentBackend
from tradingbot.llm.cost import BudgetExceeded, CostLedger
from tradingbot.models import ClosedTrade, ExitReason, OrderRequest, Position, TradeEvent
from tradingbot.notify import Notifier
from tradingbot.portfolio import CorruptStateError, Portfolio
from tradingbot.scanner import ScanResult, Scanner
from tradingbot.skills import SkillRegistry

log = logging.getLogger("tradingbot.cycle")

__all__ = [
    "Cycle", "CycleSummary", "LockLostError", "StateSaveError",
    "CYCLE_DEADLINE_S", "AUTH_PAUSE_MINUTES", "TRIM_FRACTION", "DEFAULT_PROMPTS_DIR",
    "STATE_FILENAME", "CONTROL_DIRNAME", "STOP_FILENAME", "CLEAR_FILENAME", "LOCK_FILENAME",
    "next_utc_midnight_ms", "read_control_file",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"

STATE_FILENAME = "state.json"
LOCK_FILENAME = "bot.lock"
CONTROL_DIRNAME = "control"
STOP_FILENAME = "STOP"
CLEAR_FILENAME = "CLEAR"
LOGS_DIRNAME = "logs"
CYCLES_DIRNAME = "cycles"
DECISIONS_FILENAME = "decisions.jsonl"
JOURNAL_FILENAME = "journal.jsonl"
NEWS_STORE_FILENAME = "news_store.json"
CACHE_DIRNAME = "cache"

CYCLE_DEADLINE_S = 300.0                 # 사이클 기한(5분)
AUTH_PAUSE_MINUTES = 60                  # 인증 오류 → LLM 60분 일시정지
TRIM_FRACTION = 0.5                      # risk action "trim" + head SELL → 50% 매도
DAILY_REVIEW_AFTER = dtime(0, 5)         # 일일 리뷰는 00:05 UTC 이후 첫 사이클
DEADLINE_REASON = "cycle_deadline"
MAX_ERROR_TEXT = 200


# ======================================================================= 예외·요약
class LockLostError(RuntimeError):
    """``bot.lock`` 을 다른 프로세스가 가져갔다(단일 인스턴스 위반) — 사이클을 멈춘다."""


class StateSaveError(OSError):
    """상태(state.json) 저장 최종 실패 — 사이클을 멈춘다(``OSError`` 하위)."""


_FATAL: tuple[type[BaseException], ...] = (CorruptStateError, ConfigError, LockLostError, StateSaveError)


class CycleSummary(BaseModel):
    """``run_once`` 결과 요약(로그·CLI·테스트용)."""

    model_config = ConfigDict(extra="forbid")

    cycle: int
    ts: int
    mode: str
    equity: float | None = None
    peak_equity: float | None = None
    drawdown_level: str | None = None
    g0_passed: bool | None = None
    g0_reasons: list[str] = Field(default_factory=list)
    pipeline_ran: bool = False
    head_called: bool = False
    head_skipped_reason: str | None = None
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    abstained: list[str] = Field(default_factory=list)
    entries: list[dict] = Field(default_factory=list)       # {"symbol","quote_amount","price","stop_loss_price","take_profit_price"}
    exits: list[dict] = Field(default_factory=list)         # {"symbol","reason","pnl_quote","pnl_pct","partial"}
    downgrades: list[dict] = Field(default_factory=list)    # {"symbol","reason"}
    decisions: list[dict] = Field(default_factory=list)     # 심볼별 {"symbol","action","executed","reason"}
    errors: list[str] = Field(default_factory=list)
    skipped_stages: list[str] = Field(default_factory=list)
    deadline_hit: bool = False
    budget_exceeded: bool = False
    llm_paused: bool = False
    kill_switch: bool = False
    kill_reason: str | None = None
    daily_rollover: str | None = None                       # 끝난 날짜(롤오버가 있었으면)
    daily_review: str | None = None                         # 리뷰한 날짜
    duration_s: float = 0.0


# ======================================================================= 도우미
def _finite(x: Any) -> bool:
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _pos(x: Any) -> float | None:
    return float(x) if _finite(x) and float(x) > 0 else None


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(_utc(dt).timestamp() * 1000)


def next_utc_midnight_ms(now: datetime) -> int:
    """다음 UTC 자정(epoch ms)."""
    d = _utc(now)
    nxt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
    return _ms(nxt)


def _utc_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def read_control_file(path: Path) -> dict | None:
    """``control/STOP``·``control/CLEAR`` 내용({"reason","ts"}). 없으면 None. JSON 이 아니면 본문을 사유로."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        raw = ""
    data: dict = {}
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, Mapping):
                data = dict(obj)
            else:
                data = {"reason": str(obj)}
        except ValueError:
            data = {"reason": raw[:500]}
    ts = data.get("ts")
    if not _finite(ts):
        try:
            ts = int(p.stat().st_mtime * 1000)
        except OSError:
            ts = 0
    reason = data.get("reason")
    return {"reason": str(reason)[:500] if reason else None, "ts": int(ts)}


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True)
    return obj


def _role_summaries(ctx: CandidateContext | None, decision: HeadDecision | None) -> dict:
    """대시보드용 역할별 요약(결정 로그)."""
    out: dict[str, Any] = {}
    if ctx is not None:
        t = ctx.technical
        if t is not None:
            out["technical"] = {"direction": t.direction, "setup_type": t.setup_type, "score": t.score,
                                "confidence": t.confidence, "stop_pct": t.stop_pct, "tp_pct": t.tp_pct}
        f = ctx.flow
        if f is not None:
            out["derivatives_flow"] = {"flow_bias": f.flow_bias, "block_new_longs": f.block_new_longs,
                                       "cascade_risk": f.cascade_risk, "confidence": f.confidence}
        b = ctx.bull
        if b is not None:
            out["bull"] = {"case_strength": b.case_strength, "confidence": b.confidence,
                           "summary": b.entry_trigger}
        br = ctx.bear
        if br is not None:
            out["bear"] = {"verdict": br.verdict, "strongest_point": br.strongest_point, "confidence": br.confidence}
        r = ctx.risk
        if r is not None:
            out["risk_manager"] = {"verdict": r.verdict, "max_size_pct": r.max_size_pct, "stop_pct": r.stop_pct,
                                   "take_profit_pct": r.take_profit_pct, "reasons": list(r.veto_reasons)}
    if decision is not None:
        out["head"] = {"action": decision.action, "confidence": decision.confidence, "size_pct": decision.size_pct,
                       "reason": decision.rationale, "decisive_factor": decision.decisive_factor}
    return out


class _Run:
    """한 사이클 실행 중 누적 상태."""

    def __init__(self, now: datetime, start_mono: float) -> None:
        self.now = _utc(now)
        self.now_ms = _ms(now)
        self.start_mono = start_mono
        self.errors: list[str] = []
        self.skipped: list[str] = []
        self.deadline_hit = False
        self.scan: ScanResult | None = None
        self.marks: dict[str, float] = {}
        self.equity: float = float("nan")
        self.dd_level: str | None = None
        self.g0: G.GateDecision | None = None
        self.result: PipelineResult | None = None
        self.entries: list[dict] = []
        self.exits: list[dict] = []
        self.downgrades: list[dict] = []
        self.actions: dict[str, dict] = {}          # symbol -> {"action","executed","reason"}
        self.reset_peak = False
        self.rollover: str | None = None
        self.review: str | None = None
        self.budget_exceeded = False


# ======================================================================= Cycle
class Cycle:
    """SPEC §15/§22 사이클. 객체는 ``main.build_components`` 가 만들어 넘긴다(테스트는 가짜 주입)."""

    def __init__(self, settings: Settings, market: MarketData, broker: Broker, portfolio: Portfolio,
                 backend: AgentBackend, ledger: CostLedger, notifier: Notifier, skills: SkillRegistry, *,
                 now: Callable[[], datetime] | None = None, scanner: Scanner | None = None,
                 runner: AgentRunner | None = None, news_store: NewsStore | None = None,
                 journal_state: JournalState | None = None, prompts_dir: Path | None = None,
                 lock: Any | None = None, monotonic: Callable[[], float] | None = None,
                 deadline_s: float = CYCLE_DEADLINE_S) -> None:
        self.settings = settings
        self.market = market
        self.broker = broker
        self.portfolio = portfolio
        self.backend = backend
        self.ledger = ledger
        self.notifier = notifier
        self.skills = skills
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._mono = monotonic or time.monotonic
        self.deadline_s = float(deadline_s)
        self.lock = lock

        self.data_dir = Path(settings.data_dir)
        self.logs_dir = self.data_dir / LOGS_DIRNAME
        self.control_dir = self.data_dir / CONTROL_DIRNAME
        self.cycles_dir = self.logs_dir / CYCLES_DIRNAME
        self.decisions_path = self.logs_dir / DECISIONS_FILENAME
        self.journal_path = self.logs_dir / JOURNAL_FILENAME

        self.scanner = scanner if scanner is not None else Scanner(
            settings, market, cache_dir=self.data_dir / CACHE_DIRNAME)
        self.runner = runner if runner is not None else AgentRunner(
            settings, backend, skills, Path(prompts_dir) if prompts_dir is not None else DEFAULT_PROMPTS_DIR)
        self.news_store = news_store if news_store is not None else NewsStore(self.data_dir / NEWS_STORE_FILENAME)
        self.journal_state = journal_state if journal_state is not None else J.load_journal_state(self.data_dir)
        self._last_market_tone: float | None = None
        self._finished_day: Any = None            # 마지막 롤오버로 끝난 DailyStats(일일 리뷰용)
        self._secrets = tuple(s for s in (settings.anthropic_api_key, settings.binance_api_key,
                                          settings.binance_api_secret, settings.telegram_bot_token) if s)

    # ------------------------------------------------------------ 공용 도우미
    @property
    def state(self):  # noqa: ANN201 - BotState
        return self.portfolio.state

    def _scrub(self, text: Any) -> str:
        s = str(text)
        for sec in self._secrets:
            if sec and sec in s:
                s = s.replace(sec, mask_secret(sec))
        return s

    def _elapsed(self, run: _Run) -> float:
        return float(self._mono()) - run.start_mono

    def _event(self, kind: str, run: _Run, **kw: Any) -> TradeEvent:
        return TradeEvent(kind=kind, ts=run.now_ms, mode=self.settings.mode, **kw)  # type: ignore[arg-type]

    def _notify(self, event: TradeEvent) -> None:
        try:
            self.notifier.notify(event)
        except Exception as exc:  # noqa: BLE001 - Notifier 는 던지지 않지만 방어
            log.error("알림 호출 실패 kind=%s type=%s", event.kind, type(exc).__name__)

    def _record_error(self, run: _Run, stage: str, exc: BaseException) -> None:
        msg = self._scrub(exc)[:MAX_ERROR_TEXT]
        text = f"{stage}: {type(exc).__name__}" + (f": {msg}" if msg else "")
        run.errors.append(text)
        log.error("사이클 단계 실패 stage=%s type=%s msg=%s", stage, type(exc).__name__, msg)
        if log.isEnabledFor(logging.DEBUG):
            # 트레이스백도 스크럽한다(원 예외 메시지에 비밀값이 섞일 수 있다)
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            log.debug("단계 실패 상세 stage=%s\n%s", stage, self._scrub(tb))
        self._notify(self._event("error", run, reason=text, extra={"stage": stage}))

    def _stage(self, run: _Run, stage: str, fn: Callable[[], Any], *, default: Any = None,
               fatal_oserror: bool = False) -> Any:
        try:
            return fn()
        except _FATAL:
            raise
        except OSError as exc:
            if fatal_oserror and self._fatal_oserror(exc):
                raise
            self._record_error(run, stage, exc)
            return default
        except Exception as exc:  # noqa: BLE001 - 단계 실패는 사이클을 죽이지 않는다
            self._record_error(run, stage, exc)
            return default

    @staticmethod
    def _fatal_oserror(exc: OSError) -> bool:
        """파이프라인 OSError 가 치명(상태 저장 실패)인가.

        비용 원장 기록 실패는 치명. 파일 없음·디렉터리 오류(예: 프롬프트 파일 누락)는 상태 저장 실패가 아니므로
        기록·알림 후 HOLD 로 계속한다(SPEC §15). 그 밖의 OSError 는 보수적으로 치명으로 본다.
        """
        if _raised_from_cost_ledger(exc):
            return True
        return not isinstance(exc, (FileNotFoundError, IsADirectoryError, NotADirectoryError))

    def _deadline_passed(self, run: _Run, stage: str) -> bool:
        """``stage`` 시작 시 기한을 넘겼으면 True(처음 넘었을 때 notify(error "cycle_deadline"))."""
        if self._elapsed(run) <= self.deadline_s:
            return False
        if not run.deadline_hit:
            run.deadline_hit = True
            text = f"{DEADLINE_REASON}: 사이클 기한 {self.deadline_s:.0f}s 초과 — 남은 LLM 단계·신규 진입 HOLD"
            log.warning("%s (stage=%s, elapsed=%.1fs)", text, stage, self._elapsed(run))
            self._notify(self._event("error", run, reason=text, extra={"stage": stage, "code": DEADLINE_REASON}))
        run.skipped.append(f"{stage}:{DEADLINE_REASON}")
        return True

    def _save(self) -> None:
        """상태 저장. 최종 실패는 ``StateSaveError``(``OSError``)로 전파한다."""
        try:
            self.portfolio.save()
        except OSError as exc:
            log.error("상태 저장 최종 실패 type=%s", type(exc).__name__)
            raise StateSaveError(exc.errno, f"상태 저장 실패: {type(exc).__name__}") from exc

    def _save_journal(self, run: _Run) -> None:
        try:
            J.save_journal_state(self.journal_state, self.data_dir)
        except OSError as exc:
            # 저널 상태는 보조 상태 — 사이클을 멈추지 않는다
            run.errors.append(f"journal_save: {type(exc).__name__}")
            log.error("저널 상태 저장 실패 type=%s", type(exc).__name__)

    def _llm_available(self, run: _Run) -> bool:
        if self.portfolio.llm_paused(run.now_ms):
            return False
        try:
            return bool(self.ledger.budget_ok())
        except Exception:  # noqa: BLE001
            return False

    def _equity(self, marks: Mapping[str, float]) -> float:
        if self.settings.mode == "paper":
            return float(self.portfolio.equity(marks))
        return float(self.portfolio.equity(marks, quote_balance=float(self.broker.free_quote())))

    def _sync_paper_broker(self) -> None:
        """paper 모드: 브로커 메모리 잔고를 상태(진실 원천)에 맞춘다."""
        if self.settings.mode != "paper" or not callable(getattr(self.broker, "set_balances", None)):
            return
        bal: dict[str, float] = {"USDT": max(0.0, float(self.state.paper_quote_balance))}
        for sym, pos in self.state.positions.items():
            base = sym.split("/", 1)[0].upper()
            bal[base] = bal.get(base, 0.0) + max(0.0, float(pos.base_amount))
        self.broker.set_balances(bal)  # type: ignore[attr-defined]

    # ================================================================ 본체
    def run_once(self) -> CycleSummary:
        run = _Run(self._now(), float(self._mono()))
        js = self.journal_state

        # 1. 잠금·control 파일
        self._stage(run, "lock", self._check_lock)
        self._stage(run, "control", lambda: self._process_control(run))

        # 2. 사이클 증가·롤오버·일일 리뷰
        self.state.cycle = int(self.state.cycle) + 1
        cycle = self.state.cycle
        self._stage(run, "sync_broker", self._sync_paper_broker)
        self._stage(run, "rollover", lambda: self._rollover(run))

        # 3. 스캔·마크·equity·peak·낙폭 사다리
        run.scan = self._stage(run, "scan", lambda: self.scanner.scan(
            self.state, run.now_ms, headline_polarity=self._last_market_tone))
        self._stage(run, "marks", lambda: self._update_marks(run))
        self._stage(run, "daily_review", lambda: self._maybe_daily_review(run))

        # 4. 코드 청산
        self._stage(run, "code_exits", lambda: self._code_exits(run))

        # 5. G0
        g0 = self._stage(run, "g0", lambda: self._g0(run))
        run.g0 = g0
        review_only = self._positions_only_block(g0) and run.scan is not None
        if review_only:
            # 포지션 수 한도만 걸림 → 신규 진입만 막고 보유 포지션 검토(레드라인·risk_manager·총괄 SELL)는 계속
            try:
                for code in G.reason_codes(g0):
                    J.record_halt(js, code)
            except Exception:  # noqa: BLE001
                pass
            log.info("G0: 포지션 수 한도 — 후보 없이 보유 검토만 진행: %s", "; ".join(g0.reasons))
        if run.scan is not None and g0 is not None and (g0.passed or review_only):
            # 6. 파이프라인
            if not self._deadline_passed(run, "pipeline"):
                run.result = self._stage(run, "pipeline", lambda: self._pipeline(run, no_candidates=review_only),
                                         fatal_oserror=True)
            if run.result is not None:
                self._stage(run, "pipeline_post", lambda: self._after_pipeline(run))
                # 7. head 결정
                self._execute_decisions(run)
        elif g0 is not None and not g0.passed:
            try:
                for code in G.reason_codes(g0):
                    J.record_halt(js, code)
            except Exception:  # noqa: BLE001
                pass
            log.info("G0 차단 — LLM 0회: %s", "; ".join(g0.reasons))
        elif run.scan is None:
            run.skipped.append("pipeline:no_scan")

        # 8. 결정 로그·보고서·저장
        self.state.last_cycle_ts = run.now_ms
        try:
            self.state.daily.llm_cost_usd = float(self.ledger.spent_today())
        except Exception:  # noqa: BLE001
            pass
        self._stage(run, "decision_log", lambda: self._write_decisions(run))
        summary = self._summary(run, cycle)
        self._stage(run, "cycle_report", lambda: self._write_report(run, cycle, summary))
        self._save()
        self._save_journal(run)
        summary.errors = list(run.errors)
        summary.duration_s = round(self._elapsed(run), 3)
        log.info("사이클 %d 완료: equity=%.2f entries=%d exits=%d llm_calls=%d errors=%d",
                 cycle, summary.equity if summary.equity is not None else float("nan"),
                 len(summary.entries), len(summary.exits), summary.llm_calls, len(summary.errors))
        return summary

    # ================================================================ 1. 잠금·control
    def _check_lock(self) -> None:
        if self.lock is None:
            return
        fn = getattr(self.lock, "refresh", None)
        if callable(fn):
            fn()        # LockLostError 는 전파

    def _process_control(self, run: _Run) -> None:
        stop_p = self.control_dir / STOP_FILENAME
        clear_p = self.control_dir / CLEAR_FILENAME
        events: list[tuple[int, int, str, dict]] = []
        stop = read_control_file(stop_p)
        if stop is not None:
            events.append((stop["ts"], 0, "stop", stop))
        clr = read_control_file(clear_p)
        if clr is not None:
            events.append((clr["ts"], 1, "clear", clr))
        events.sort()
        pending: list[TradeEvent] = []
        for _, _, kind, info in events:
            if kind == "stop":
                reason = f"manual_stop: {info.get('reason') or '사유 없음'}"
                if not self.state.kill_switch or self.state.kill_reason != reason:
                    self.portfolio.set_kill(reason)
                    pending.append(self._event("kill_switch", run, reason=reason))
            else:
                prev = self.state.kill_reason or ""
                was = self.state.kill_switch
                self.portfolio.clear_kill()
                if prev.startswith("drawdown"):
                    # 사람이 낙폭 정지를 해제 → 최고자본을 현재 자본으로 재설정(아니면 다음 사이클에 다시 발동)
                    run.reset_peak = True
                if was:
                    pending.append(self._event(
                        "startup", run, reason=f"킬스위치 해제 — 신규 진입 재개 (이전 사유: {prev or '-'}; "
                                               f"요청: {info.get('reason') or '-'})",
                        extra={"event": "kill_cleared"}))
        if not events:
            return
        # 상태 저장 → control 파일 삭제 → 알림 (저장 실패 시 파일은 남아 재시작 후 다시 처리된다)
        self._save()
        for _, _, kind, _info in events:
            (stop_p if kind == "stop" else clear_p).unlink(missing_ok=True)
        for ev in pending:
            self._notify(ev)

    # ================================================================ 2. 롤오버·일일 리뷰
    def _rollover(self, run: _Run) -> None:
        marks = {s: p.last_mark for s, p in self.state.positions.items() if _pos(p.last_mark)}
        try:
            eq = self._equity(marks)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - 자본 계산 실패면 시작자본 0(다음 사이클에 채움)
            log.warning("롤오버용 자본 계산 실패 type=%s", type(exc).__name__)
            eq = 0.0
        finished = self.portfolio.ensure_day(run.now, eq)
        if finished is None:
            return
        self._finished_day = finished
        run.rollover = finished.date
        prev_state = self.state.model_copy(update={"daily": finished})
        ev = reports.daily_summary(prev_state, eq, float(finished.llm_cost_usd))
        self._save()            # 상태 저장 → 알림
        self._notify(ev.model_copy(update={"ts": run.now_ms}))

    def _maybe_daily_review(self, run: _Run) -> None:
        js = self.journal_state
        if run.now.time() < DAILY_REVIEW_AFTER:
            return
        yesterday = (run.now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        last = js.last_daily_review_date
        if last is not None and last >= yesterday:
            return
        trades = list(self.state.closed_trades)
        if last is None and not trades:
            J.start_new_day(js, yesterday)        # 첫 가동: 리뷰할 것이 없다
            self._save_journal(run)
            return
        J.refresh_stats(js, trades, self.settings.cycle_minutes)
        day_trades = [t for t in trades if _utc_date(t.closed_ts) == yesterday]
        fin = self._finished_day if (self._finished_day is not None and self._finished_day.date == yesterday) else None
        start_eq = float(fin.start_equity) if fin is not None and _pos(fin.start_equity) else None
        eq_now = run.equity if _finite(run.equity) else None
        curve = [x for x in (start_eq, eq_now) if x is not None]
        metrics = J.code_metrics(trades, curve, None, setups=js.entry_context)
        day_pnl = sum(t.pnl_quote for t in day_trades)
        table = J.build_metrics_table(
            metrics, js, closed_today=len(day_trades),
            day_pnl_pct=(day_pnl / start_eq * 100.0) if start_eq else None,
            equity=eq_now,
            llm={"calls": None, "cost_usd": float(fin.llm_cost_usd) if fin is not None else None,
                 "budget_usd": float(self.settings.llm_daily_budget_usd), "abstain_count": None})
        review = None
        if self._llm_available(run) and not self._deadline_passed(run, "daily_review"):
            review = J.daily_review(self.runner, js, trades, table)
        text = review.telegram_text if review is not None else J.code_daily_summary(table, js.closed_count)
        try:
            J.append_journal_log(self.data_dir, {"kind": "daily_review", "date": yesterday, "ts": run.now_ms,
                                                 "review": _dump(review), "summary": text, "llm": review is not None})
        except OSError as exc:
            run.errors.append(f"daily_review_log: {type(exc).__name__}")
        self._notify(self._event("daily_summary", run, reason=f"{yesterday} 일일 리뷰: {text}",
                                 extra={"review_date": yesterday, "llm_review": review is not None}))
        J.start_new_day(js, yesterday)
        run.review = yesterday
        self._save_journal(run)

    # ================================================================ 3. 마크·자본
    def _fallback_marks(self) -> dict[str, float]:
        marks: dict[str, float] = {}
        for sym in sorted(self.state.positions):
            try:
                t = self.market.fetch_ticker(sym)
                if _pos(t.last):
                    marks[sym] = float(t.last)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 보조 시세 조회 실패 type=%s", sym, type(exc).__name__)
        return marks

    def _update_marks(self, run: _Run) -> None:
        marks: dict[str, float] = {}
        if run.scan is not None:
            marks.update({k: float(v) for k, v in run.scan.marks.items() if _pos(v)})
        missing = [s for s in self.state.positions if s not in marks]
        if missing:
            marks.update({k: v for k, v in self._fallback_marks().items() if k in missing})
        run.marks = marks
        self.portfolio.update_marks(marks)
        eq = self._equity(marks)
        run.equity = eq
        if run.reset_peak and _pos(eq):
            log.warning("킬스위치 해제로 최고자본 재설정: %.2f → %.2f", self.state.peak_equity, eq)
            self.state.peak_equity = float(eq)
        self.portfolio.update_peak(eq)
        level = risk.drawdown_level(eq, self.state.peak_equity) if _pos(eq) else "stop_all"
        run.dd_level = level
        if level == "stop_all" and _pos(eq) and not self.state.kill_switch:
            self.portfolio.set_kill("drawdown_stop_all")
            self._save()
            dd = (1.0 - eq / self.state.peak_equity) * 100.0 if self.state.peak_equity > 0 else float("nan")
            self._notify(self._event("kill_switch", run, reason=f"drawdown_stop_all: 최고자본 대비 낙폭 {dd:.2f}%",
                                     equity=eq))

    # ================================================================ 4. 청산
    def _mark_for(self, run: _Run, pos: Position) -> float | None:
        m = run.marks.get(pos.symbol)
        if _pos(m):
            return float(m)  # type: ignore[arg-type]
        return _pos(pos.last_mark)

    def _code_exits(self, run: _Run) -> None:
        for sym in sorted(self.state.positions):
            pos = self.state.positions.get(sym)
            if pos is None:
                continue
            mark = self._mark_for(run, pos)
            reason = risk.check_exits(pos, mark if mark is not None else float("nan"), int(self.state.cycle))
            if reason is None:
                continue
            log.info("코드 청산 %s: %s (mark=%s sl=%.8g tp=%.8g)", sym, reason, mark, pos.stop_loss_price,
                     pos.take_profit_price)
            self._stage(run, f"exit:{sym}", lambda s=sym, r=reason: self._sell(run, s, 1.0, r))

    def _sell(self, run: _Run, symbol: str, fraction: float, reason: ExitReason) -> ClosedTrade | None:
        """매도 → close_position → save → notify → (전량이면) 저널 사후분석."""
        pos = self.state.positions.get(symbol)
        if pos is None:
            return None
        cycle = int(self.state.cycle)
        amount = float(pos.base_amount) * max(0.0, min(1.0, float(fraction)))
        if self.settings.mode != "paper":
            try:
                amount = min(amount, float(self.broker.free_base(symbol)))
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 가용 수량 조회 실패 type=%s — 장부 수량 사용", symbol, type(exc).__name__)
        try:
            amount = float(self.broker.amount_to_precision(symbol, amount))
            risk.validate_order_request(OrderRequest(symbol=symbol, side="sell", base_amount=amount),
                                        tuple(self.settings.symbols), list(self.state.positions))
            fill = self.broker.market_sell(symbol, amount)
        except Exception as exc:  # noqa: BLE001 - 거래소/검증 오류는 거부 알림
            text = f"{reason}: {type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}"
            run.errors.append(f"sell:{symbol}: {text}")
            log.error("매도 실패 %s %s", symbol, text)
            self._notify(self._event("order_rejected", run, symbol=symbol, side="sell", base_amount=amount,
                                     reason=text))
            return None
        trade = self.portfolio.close_position(symbol, fill, reason, cycle)
        self._save()
        partial = symbol in self.state.positions
        marks = dict(run.marks)
        if not partial:
            marks.pop(symbol, None)
        try:
            eq = self._equity(marks)
        except Exception:  # noqa: BLE001
            eq = None
        run.exits.append({"symbol": symbol, "reason": trade.reason, "requested_reason": reason,
                          "pnl_quote": trade.pnl_quote, "pnl_pct": trade.pnl_pct, "partial": partial,
                          "price": trade.exit_price, "base_amount": trade.base_amount})
        run.actions[symbol] = {"action": "SELL", "executed": True, "reason": f"{reason}" + (" (부분)" if partial else "")}
        self._notify(self._event(
            "partial_exit" if partial else "exit", run, symbol=symbol, side="sell", base_amount=trade.base_amount,
            price=trade.exit_price, quote_amount=trade.quote_received, reason=trade.reason if not partial else reason,
            pnl_quote=trade.pnl_quote, pnl_pct=trade.pnl_pct, equity=eq,
            extra={"order_id": fill.order_id, "requested_reason": reason, "cycle": cycle}))
        self._post_trade(run, trade, partial)
        return trade

    def _post_trade(self, run: _Run, trade: ClosedTrade, partial: bool) -> None:
        js = self.journal_state
        review = None
        if not partial:
            try:
                J.refresh_stats(js, self.state.closed_trades, self.settings.cycle_minutes)
            except Exception as exc:  # noqa: BLE001
                log.warning("저널 통계 갱신 실패 type=%s", type(exc).__name__)
            if self._llm_available(run) and not self._deadline_passed(run, f"journal:{trade.symbol}"):
                review = J.post_trade_review(self.runner, trade, {"journal_state": js})
        rec: dict | None
        if review is not None:
            rec = _dump(review)
        elif partial:
            rec = None
        else:
            rec = {"code_summary": J.code_post_trade_summary(trade), "llm": False}
        try:
            reports.append_journal(self.journal_path, trade, rec)
        except (OSError, TypeError, ValueError) as exc:
            run.errors.append(f"journal_log:{trade.symbol}: {type(exc).__name__}")
            log.error("저널 기록 실패 %s type=%s", trade.symbol, type(exc).__name__)
        if not partial:
            J.clear_symbol(js, trade.symbol)
        self._save_journal(run)

    # ================================================================ 5. G0
    def _g0(self, run: _Run) -> G.GateDecision:
        budget_ok = True
        try:
            budget_ok = bool(self.ledger.budget_ok())
        except Exception as exc:  # noqa: BLE001
            log.warning("예산 확인 실패 type=%s — 소진으로 간주", type(exc).__name__)
            budget_ok = False
        if not budget_ok and not self.portfolio.llm_paused(run.now_ms):
            self._pause_for_budget(run, "일일/월간 LLM 예산 소진 (G0)")
        funding = bool(run.scan.funding_window) if run.scan is not None else False
        eq = run.equity if _finite(run.equity) else float("nan")
        return G.g0_preflight(self.state, eq, run.now_ms, budget_ok, funding)

    @staticmethod
    def _positions_only_block(g0: G.GateDecision | None) -> bool:
        """G0 실패 사유가 포지션 수 한도(및 펀딩 창)뿐인가(DESIGN §3: 포지션 수는 신규 진입 조건)."""
        if g0 is None or g0.passed:
            return False
        codes = set(G.reason_codes(g0))
        return "max_positions" in codes and codes <= {"max_positions", "funding_window"}

    def _pause_for_budget(self, run: _Run, reason: str) -> None:
        until = next_utc_midnight_ms(run.now)
        self.portfolio.pause_llm(until, "budget_exceeded")
        self._save()
        J.record_halt(self.journal_state, "budget_exceeded")
        extra: dict[str, Any] = {"until_ts": until, "budget_usd": float(self.settings.llm_daily_budget_usd)}
        try:
            extra["spent_usd"] = float(self.ledger.spent_today())
        except Exception:  # noqa: BLE001
            pass
        self._notify(self._event("budget_exceeded", run, reason=reason, extra=extra))

    # ================================================================ 6. 파이프라인
    def _pipeline(self, run: _Run, *, no_candidates: bool = False) -> PipelineResult:
        assert run.scan is not None
        scan = run.scan.model_copy(update={"candidates": []}) if no_candidates else run.scan
        try:
            return run_pipeline(scan, self.state, run.equity, self.runner, self.journal_state, run.now_ms,
                                news_store=self.news_store)
        except BudgetExceeded:
            # 파이프라인이 잡지 못한 경로(방어): 예산 초과로 처리
            run.budget_exceeded = True
            self._pause_for_budget(run, "LLM 예산 초과 (파이프라인)")
            raise

    def _after_pipeline(self, run: _Run) -> None:
        res = run.result
        assert res is not None
        js = self.journal_state
        cycle = int(self.state.cycle)
        if res.budget_exceeded:
            run.budget_exceeded = True
            if not self.portfolio.llm_paused(run.now_ms):
                self._pause_for_budget(run, "LLM 예산 초과 — 남은 단계 HOLD")
        auth = [a for a in res.abstained if a.endswith(":auth")]
        if auth and len(auth) >= max(1, math.ceil(max(res.llm_calls, 1) / 2)):
            until = run.now_ms + AUTH_PAUSE_MINUTES * 60_000
            self.portfolio.pause_llm(until, "auth_error")
            self._save()
            J.record_halt(js, "llm_paused_auth")
            self._notify(self._event("llm_paused", run, reason=f"인증 오류 기권 {len(auth)}건 — LLM {AUTH_PAUSE_MINUTES}분 일시정지",
                                     extra={"until_ts": until}))
        if res.news is not None:
            self._last_market_tone = float(res.news.market_tone) if _finite(res.news.market_tone) else None
        # 게이트 카운터
        for k, v in sorted((res.gate_counts or {}).items()):
            if v:
                J.record_gate(js, k, int(v))
        # 보유 종목 논지 추적(thesis_history·prev_assumption_status)
        for pc in res.positions:
            pos = self.state.positions.get(pc.symbol)
            if pos is None or (pc.risk is None and pc.technical is None):
                continue
            mark = self._mark_for(run, pos)
            J.append_thesis_history(
                js, pc.symbol, cycle=cycle, health=pc.risk.health if pc.risk is not None else None,
                assumption_status=list(pc.risk.assumption_status) if pc.risk is not None else [],
                red_line_hits=list(pc.red_line_hits), action=pc.risk.action if pc.risk is not None else None,
                exit_flag=pc.technical.exit_flag if pc.technical is not None else None,
                unrealized_pct=P.unrealized_pct(pos.entry_price, mark))
        # 보유 중 뉴스 위험
        for sym in res.news_risk_symbols:
            if sym in self.state.positions:
                J.record_news_risk(js, sym, f"cycle {cycle}: news risk_event")
        self._save_journal(run)

    # ================================================================ 7. head 결정
    def _execute_decisions(self, run: _Run) -> None:
        res = run.result
        if res is None or res.head is None:
            return
        ctxs = {c.symbol: c for c in res.candidates}
        pcs = {p.symbol: p for p in res.positions}
        # SELL 을 먼저 처리한다(실현 손익이 뒤이은 BUY 의 전역 게이트 재검사에 반영되도록)
        ordered = sorted(res.head.decisions, key=lambda x: 0 if x.action == "SELL" else 1)
        for d in ordered:
            sym = d.symbol
            if d.action == "SELL":
                if sym not in self.state.positions:
                    run.actions[sym] = {"action": "HOLD", "executed": False, "reason": "SELL 대상 미보유"}
                    continue
                pc = pcs.get(sym)
                fraction = TRIM_FRACTION if (pc is not None and pc.risk is not None and pc.risk.action == "trim") else 1.0
                log.info("총괄 SELL %s (비율 %.0f%%): %s", sym, fraction * 100, d.rationale[:120])
                self._stage(run, f"head_sell:{sym}", lambda s=sym, f=fraction: self._sell(run, s, f, "head_sell"))
            elif d.action == "BUY":
                if self._deadline_passed(run, f"buy:{sym}"):
                    run.actions[sym] = {"action": "HOLD", "executed": False, "reason": DEADLINE_REASON}
                    continue
                self._stage(run, f"buy:{sym}", lambda dd=d: self._buy(run, dd, ctxs.get(dd.symbol)))
            else:
                log.info("총괄 HOLD %s: %s", sym, d.rationale[:120])
                run.actions.setdefault(sym, {"action": "HOLD", "executed": False, "reason": d.rationale[:200]})

    def _downgrade(self, run: _Run, symbol: str, reason: str, *, quote: float | None = None) -> None:
        J.record_gate(self.journal_state, "g4_downgrade")
        run.downgrades.append({"symbol": symbol, "reason": reason})
        run.actions[symbol] = {"action": "HOLD", "executed": False, "reason": f"gate_downgrade: {reason}"}
        self._notify(self._event("order_rejected", run, symbol=symbol, side="buy", quote_amount=quote,
                                 reason=f"gate_downgrade: {reason}"[:500]))

    def _buy(self, run: _Run, d: HeadDecision, ctx: CandidateContext | None) -> None:
        res = run.result
        assert res is not None and run.scan is not None and run.g0 is not None
        sym = d.symbol
        if sym in self.state.positions:
            run.actions[sym] = {"action": "HOLD", "executed": False, "reason": "already_held"}
            return
        if ctx is None or ctx.risk is None:
            self._downgrade(run, sym, "risk_abstained: 승인 맥락 없음")
            return
        # 전역 게이트 재검사: 같은 사이클 앞선 청산 손실로 당일 −3%·연속 3패·킬스위치에 닿았으면 신규 0
        if run.exits:
            try:
                eq_now = self._equity({s: m for s, m in run.marks.items() if s in self.state.positions})
            except Exception:  # noqa: BLE001
                eq_now = float("nan")
            verdict = risk.global_gates(self.state, eq_now, run.now_ms, True)
            if not verdict.allowed:
                self._downgrade(run, sym, "global_gates: " + "; ".join(verdict.reasons))
                return
            if _finite(eq_now):
                run.equity = float(eq_now)
        mark = _pos(run.scan.marks.get(sym))
        fp = run.scan.feature_packs.get(sym)
        atr_1h = None
        if fp is not None and _pos(fp.atr_pct_1h) and mark is not None:
            atr_1h = float(fp.atr_pct_1h) / 100.0 * mark
        gate = effective_gate(ctx, res.risk_global)
        mult = float(gate["size_multiplier"]) * float(run.g0.size_multiplier)
        try:
            free_quote = float(self.broker.free_quote())
        except Exception as exc:  # noqa: BLE001
            self._downgrade(run, sym, f"free_quote 조회 실패: {type(exc).__name__}")
            return
        tech_stop = ctx.technical.stop_pct if ctx.technical is not None else None
        g4, plan = G.g4_final(d, ctx.risk, float(gate["confidence_floor"]), mult, run.equity,
                              mark if mark is not None else float("nan"), atr_1h, free_quote,
                              tech_stop_pct=tech_stop)
        ctx.gates.append(g4)
        if plan is None:
            self._downgrade(run, sym, "; ".join(g4.reasons) or "G4 HOLD")
            return
        if self._deadline_passed(run, f"order:{sym}"):
            run.actions[sym] = {"action": "HOLD", "executed": False, "reason": DEADLINE_REASON}
            return
        try:
            risk.validate_order_request(OrderRequest(symbol=sym, side="buy", quote_amount=plan.quote_amount),
                                        tuple(self.settings.symbols), list(self.state.positions))
            fill = self.broker.market_buy(sym, plan.quote_amount)
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}"
            run.errors.append(f"buy:{sym}: {text}")
            run.actions[sym] = {"action": "HOLD", "executed": False, "reason": f"order_rejected: {text}"}
            self._notify(self._event("order_rejected", run, symbol=sym, side="buy", quote_amount=plan.quote_amount,
                                     price=mark, reason=text))
            return
        # 손절/익절을 실제 체결가 기준 같은 거리로 다시 놓는다(슬리피지로 손절거리가 넓어지지 않게)
        ref = mark if mark is not None else float(fill.price)
        sl_pct = max(0.0, 1.0 - plan.stop_loss_price / ref)
        tp_pct = max(0.0, plan.take_profit_price / ref - 1.0)
        entry = float(fill.price) if _pos(fill.price) else ref
        sl, tp = risk.clamp_stops(entry, entry * (1.0 - sl_pct), entry * (1.0 + tp_pct))
        thesis_json = json.dumps(d.thesis.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        pos = self.portfolio.open_position(fill, sl, tp, thesis_json, float(d.confidence), int(self.state.cycle))
        # 저널: 구조화 논지 + 진입 판단 스냅샷
        js = self.journal_state
        try:
            ev_score = P.event_signal(P.symbol_news_events(sym, self.news_store.active_events(run.now_ms), run.now_ms),
                                      run.now_ms).get("score")
        except Exception:  # noqa: BLE001
            ev_score = None
        ret_1h = None
        if fp is not None and len(fp.last_closes_1h) >= 2 and _pos(fp.last_closes_1h[-2]):
            ret_1h = (fp.last_closes_1h[-1] / fp.last_closes_1h[-2] - 1.0) * 100.0
        verdicts = J.make_entry_verdicts(technical=ctx.technical, flow=ctx.flow, bull=ctx.bull, bear=ctx.bear,
                                         risk=ctx.risk, head=d, debias=res.head.debias if res.head else None,
                                         risk_note=res.risk_note, event_signal_score=ev_score)
        J.record_entry_verdicts(js, sym, verdicts, thesis=d.thesis, opened_ts=pos.opened_ts, entry_return_1h_pct=ret_1h)
        self._save()
        self._save_journal(run)
        bear_point = ctx.bear.strongest_point if ctx.bear is not None else None
        parts = [d.rationale]
        if d.thesis.assumptions:
            parts.append("가정: " + "; ".join(d.thesis.assumptions))
        if bear_point:
            parts.append("약세 최강 논점: " + bear_point)
        try:
            eq = self._equity(run.marks)
        except Exception:  # noqa: BLE001
            eq = None
        run.entries.append({"symbol": sym, "quote_amount": fill.quote_amount, "price": fill.price,
                            "base_amount": fill.base_amount, "stop_loss_price": sl, "take_profit_price": tp,
                            "confidence": d.confidence, "size_pct": plan.size_pct})
        run.actions[sym] = {"action": "BUY", "executed": True, "reason": d.rationale[:200]}
        self._notify(self._event(
            "entry", run, symbol=sym, side="buy", base_amount=fill.base_amount, price=fill.price,
            quote_amount=fill.quote_amount, stop_loss_price=sl, take_profit_price=tp, confidence=float(d.confidence),
            thesis=" | ".join(p for p in parts if p), equity=eq,
            extra={"bear_strongest_point": bear_point, "losing_side_strongest_point": d.losing_side_strongest_point,
                   "decisive_factor": d.decisive_factor, "size_pct": plan.size_pct, "order_id": fill.order_id,
                   "red_lines": [r.model_dump(mode="json") for r in d.thesis.red_lines]}))

    # ================================================================ 8. 로그·보고서
    def _write_decisions(self, run: _Run) -> None:
        cycle = int(self.state.cycle)
        res = run.result
        base = {"cycle": cycle, "ts": run.now_ms, "mode": self.settings.mode}
        records: list[dict] = []
        seen: set[str] = set()
        if res is not None:
            for c in res.candidates:
                d = res.decision_for(c.symbol)
                act = run.actions.get(c.symbol)
                action = act["action"] if act else (d.action if d is not None else "HOLD")
                reason = act["reason"] if act else (f"dropped_at={c.dropped_at}" if c.dropped_at else
                                                    (d.rationale if d is not None else res.head_skipped_reason))
                records.append({**base, "kind": "candidate", "symbol": c.symbol, "action": action,
                                "executed": bool(act and act.get("executed")),
                                "confidence": d.confidence if d is not None else None,
                                "reason": reason, "dropped_at": c.dropped_at, "rank": c.candidate.rank,
                                "roles": _role_summaries(c, d),
                                "gates": [g.model_dump(mode="json") for g in c.gates]})
                seen.add(c.symbol)
            for p in res.positions:
                d = res.decision_for(p.symbol)
                act = run.actions.get(p.symbol)
                roles: dict[str, Any] = {}
                if p.technical is not None:
                    roles["technical"] = {"direction": p.technical.direction, "exit_flag": p.technical.exit_flag,
                                          "reason": p.technical.exit_reason, "confidence": p.technical.confidence}
                if p.risk is not None:
                    roles["risk_manager"] = {"action": p.risk.action, "summary": f"health {p.risk.health}",
                                             "reason": p.risk.reason}
                if d is not None:
                    roles["head"] = {"action": d.action, "confidence": d.confidence, "reason": d.rationale}
                records.append({**base, "kind": "position", "symbol": p.symbol,
                                "action": act["action"] if act else (d.action if d is not None else "HOLD"),
                                "executed": bool(act and act.get("executed")),
                                "confidence": d.confidence if d is not None else None,
                                "reason": act["reason"] if act else p.flag_reason,
                                "flagged_for_head": p.flagged_for_head, "red_line_hits": list(p.red_line_hits),
                                "health_history": list(p.health_history), "roles": roles})
                seen.add(p.symbol)
        for sym, act in sorted(run.actions.items()):
            if sym not in seen:   # 코드 청산 등
                records.append({**base, "kind": "action", "symbol": sym, **act})
        g0 = run.g0
        cycle_rec = {
            **base, "kind": "cycle", "symbol": None,
            "action": "HALT" if (g0 is not None and not g0.passed) else "CYCLE",
            "reason": ("; ".join(g0.reasons) if g0 is not None and not g0.passed else
                       (res.head_skipped_reason if res is not None else None)),
            "equity": run.equity if _finite(run.equity) else None,
            "g0": g0.model_dump(mode="json") if g0 is not None else None,
            "llm_calls": res.llm_calls if res is not None else 0,
            "llm_cost_usd": res.llm_cost_usd if res is not None else 0.0,
            "abstained": list(res.abstained) if res is not None else [],
            "head_called": bool(res is not None and res.head is not None),
            "head_cycle_note": res.head.cycle_note if res is not None and res.head is not None else None,
            "gate_counts": dict(res.gate_counts) if res is not None else {},
            "candidates": [c.symbol for c in run.scan.candidates] if run.scan is not None else [],
            "regime": run.scan.regime.verdict if run.scan is not None else None,
            "exits": list(run.exits), "entries": list(run.entries), "downgrades": list(run.downgrades),
            "errors": list(run.errors), "skipped_stages": list(run.skipped), "deadline_hit": run.deadline_hit,
        }
        records.append(cycle_rec)
        for rec in records:
            reports.append_decision(self.decisions_path, rec)

    def _summary(self, run: _Run, cycle: int) -> CycleSummary:
        res = run.result
        g0 = run.g0
        decisions = [{"symbol": s, **a} for s, a in sorted(run.actions.items())]
        return CycleSummary(
            cycle=cycle, ts=run.now_ms, mode=self.settings.mode,
            equity=run.equity if _finite(run.equity) else None,
            peak_equity=float(self.state.peak_equity), drawdown_level=run.dd_level,
            g0_passed=g0.passed if g0 is not None else None, g0_reasons=list(g0.reasons) if g0 is not None else [],
            pipeline_ran=res is not None, head_called=bool(res is not None and res.head is not None),
            head_skipped_reason=res.head_skipped_reason if res is not None else None,
            llm_calls=res.llm_calls if res is not None else 0,
            llm_cost_usd=res.llm_cost_usd if res is not None else 0.0,
            abstained=list(res.abstained) if res is not None else [],
            entries=list(run.entries), exits=list(run.exits), downgrades=list(run.downgrades), decisions=decisions,
            errors=list(run.errors), skipped_stages=list(run.skipped), deadline_hit=run.deadline_hit,
            budget_exceeded=run.budget_exceeded, llm_paused=self.portfolio.llm_paused(run.now_ms),
            kill_switch=bool(self.state.kill_switch), kill_reason=self.state.kill_reason,
            daily_rollover=run.rollover, daily_review=run.review,
        )

    def _write_report(self, run: _Run, cycle: int, summary: CycleSummary) -> None:
        scan = run.scan
        payload = {
            "ts": run.now_ms, "mode": self.settings.mode, "cycle": cycle,
            "equity": summary.equity, "peak_equity": summary.peak_equity, "drawdown_level": summary.drawdown_level,
            "daily_pnl": float(self.state.daily.realized_pnl),
            "positions": {s: p.model_dump(mode="json") for s, p in sorted(self.state.positions.items())},
            "decisions": summary.decisions, "exits": summary.exits, "entries": summary.entries,
            "downgrades": summary.downgrades, "llm_calls": summary.llm_calls, "llm_cost_usd": summary.llm_cost_usd,
            "abstained": summary.abstained, "g0": run.g0.model_dump(mode="json") if run.g0 is not None else None,
            "head_skipped_reason": summary.head_skipped_reason,
            "skipped": [f"{s.get('symbol')}: {s.get('reason')}" for s in (scan.skipped if scan is not None else [])]
                       + list(run.skipped),
            "candidates": [c.model_dump(mode="json") for c in scan.candidates] if scan is not None else [],
            "regime": scan.regime.model_dump(mode="json") if scan is not None else None,
            "sentiment": scan.sentiment.model_dump(mode="json") if scan is not None else None,
            "funding_window": scan.funding_window if scan is not None else None,
            "errors": list(run.errors), "deadline_hit": run.deadline_hit,
            "kill_switch": summary.kill_switch, "kill_reason": summary.kill_reason,
            "llm_paused": summary.llm_paused,
        }
        reports.write_cycle_report(self.cycles_dir, cycle, payload)

    def __repr__(self) -> str:
        return f"Cycle(mode={self.settings.mode!r}, data_dir={str(self.data_dir)!r}, cycle={self.state.cycle})"
