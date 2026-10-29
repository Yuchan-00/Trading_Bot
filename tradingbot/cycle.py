"""한 사이클 실행기 (SPEC §15 골격 + §22 확정 순서, v2.1).

``Cycle.run_once()`` 순서(고정)
1. 잠금(``bot.lock``) 확인·갱신, ``control/STOP`` → kill, ``control/CLEAR`` → kill 해제(낙폭 정지 해제면 최고자본
   재설정까지 같은 저장에) → 저장 → notify → 파일 삭제(삭제 실패는 알림을 막지 않는다).
2. 상태(메모리의 Portfolio) 사이클 증가, 날짜 롤오버(전일 ``daily_summary`` notify), 저널 상태 ↔ 장부 대사
   (``Position.thesis`` 로 논지 복구, 미보유 심볼 항목 정리).
3. ``Scanner.scan``(testnet/live 는 거래소 자본 기준 ``planned_notional``; 뉴스 조회는 4 뒤로 미룬다) → 마크 갱신,
   equity·peak 갱신, 낙폭 사다리(−6% 이면 ``set_kill("drawdown_stop_all")`` + notify) → 일별 자본 곡선 기록 →
   testnet/live 거래소 잔고 대사(유령 포지션 정리).
4. **코드 청산**: 보유마다 ``risk.check_exits`` → market_sell → ``close_position`` → ``save`` → notify(exit).
   모든 매도가 끝난 뒤 저널 사후분석(LLM, 실패 무시) → notify(사후분석 문구) → 뉴스 조회(뉴스 피드가 없으면 묵은
   headline_polarity 제거) → 저널 일일 리뷰(00:05 UTC 이후 첫 사이클). LLM 호출·느린 피드가 손절 매도를 늦추지 않게
   이 순서를 지킨다.
5. G0(새 중단·해제는 notify). 실패면 결정 로그·보고서 쓰고 종료(LLM 0회).
6. ``run_pipeline``. ``BudgetExceeded``(→ ``PipelineResult.budget_exceeded``) → ``pause_llm(다음 UTC 자정)`` +
   notify(budget_exceeded); 인증 오류 기권 다수 → ``pause_llm(60분)`` + notify(llm_paused).
   risk_manager 가 돈 뒤 보유 종목의 ``thesis_history``·``prev_assumption_status`` 를 저널 상태에 기록한다.
7. head 결정 처리: SELL(보유) → 매도(risk action ``trim`` 이고 코드 청산 사유(레드라인·이름 경보·기술 exit·risk exit)가
   없을 때만 50%, 그 외 전량; 50% 몫이나 잔량이 최소 주문 금액 미만이면 전량)·저장·notify(exit, head_sell);
   BUY → G4 → OrderPlan → market_buy → ``open_position``(논지 JSON) → ``save`` → notify(entry) → 저널에 구조화
   논지·진입 판단 스냅샷(실패해도 체결·저장·알림에 영향 없음). G4 강등은 notify(order_rejected, "gate_downgrade: ...").
   HOLD 는 로그만.
8. 결정 로그(``logs/decisions.jsonl``), 사이클 보고서(``logs/cycles/``), 상태 저장, ``CycleSummary`` 반환.
9. 경보(DESIGN §7, ``kind="alert"``): 상태 저장 뒤 레짐 전환·뉴스 위험·포지션 점검을 판정하고
   ``alert_state.json`` 에 기록한 다음 notify 한다(재시작해도 같은 경보를 다시 보내지 않는다; 실패는 사이클을 멈추지 않는다).

체결 안전장치(testnet/live)
- 주문 뒤 브로커가 예외를 내도 주문 전후 거래소 잔고 변화로 체결을 확인하면 그 체결로 기록한다(손절 없는 고아 보유·
  유령 포지션 방지). 장부 밖 보유(팔 수 있는 크기)가 있는 종목은 사지 않는다.
- 매도 뒤 팔 수 없는 잔량(정밀도·최소 금액 미만)은 전량 청산으로 기록한다(잔량 상각, 슬롯 점유 방지).
- 체결 뒤 상태 저장이 실패해도 체결 알림(경고 포함)을 보내고 ``StateSaveError`` 를 전파한다.

에러 정책
- 모든 단계는 ``try/except`` 로 감싸 로그 + ``error`` 이벤트 notify 후 다음 단계로 간다.
- 예외: ``CorruptStateError``·``ConfigError``·``LockLostError``·상태 저장 실패(``StateSaveError``, ``OSError``)
  는 전파한다(사이클을 멈춘다). 파이프라인 안의 비용 원장 기록 실패(``OSError``)도 상태 저장 실패로 보고 전파한다.
- 사이클 기한(기본 300초): 단계가 기한을 넘겨 시작되면 남은 LLM 단계(파이프라인·저널)와 신규 진입은
  HOLD 로 건너뛰고(사유 ``cycle_deadline``) notify(error) 한다. 청산(코드 청산·총괄 SELL)은 위험을 줄이므로
  기한 뒤에도 수행한다.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import os
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradingbot import features as F
from tradingbot import gates as G
from tradingbot import journal as J
from tradingbot import reports
from tradingbot import risk
from tradingbot import fsutil
from tradingbot.agents import payloads as P
from tradingbot.agents.pipeline import (
    HEALTH_FLAG_MAX,
    NEWS_RISK_SCORE,
    CandidateContext,
    NewsStore,
    PipelineResult,
    effective_gate,
    run_pipeline,
    snap_news_score,
)
from tradingbot.agents.runner import AgentRunner, _raised_from_cost_ledger
from tradingbot.agents.schemas import HeadDecision, Thesis
from tradingbot.config import ConfigError, HardLimits, Settings, mask_secret
from tradingbot.exchange import Broker, MarketData, OrderValidationError
from tradingbot.journal import JournalState
from tradingbot.llm.backend import AgentBackend
from tradingbot.llm.cost import BudgetExceeded, CostLedger
from tradingbot.models import ClosedTrade, ExitReason, Fill, OrderRequest, Position, TradeEvent
from tradingbot.notify import STATE_SAVE_WARNING, Notifier  # noqa: F401 - STATE_SAVE_WARNING 재노출(호환)
from tradingbot.portfolio import PARTIAL_FILL_TOLERANCE, CorruptStateError, Portfolio, _fsync_dir
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
LLM_DEADLINE_RESERVE_S = 25.0            # 백엔드 LLM 기한 = 사이클 기한 - 경과 - 이 몫(매수·주문·저장용)
AUTH_PAUSE_MINUTES = 60                  # 인증 오류 → LLM 60분 일시정지
TRIM_FRACTION = 0.5                      # risk action "trim" + head SELL → 50% 매도
DAILY_REVIEW_AFTER = dtime(0, 5)         # 일일 리뷰는 00:05 UTC 이후 첫 사이클
DEADLINE_REASON = "cycle_deadline"
MAX_ERROR_TEXT = 200
BTC_SYMBOL = "BTC/USDT"

NEWS_TONE_TTL_MS = 24 * 3_600_000        # market_tone 유효기간(뉴스 이벤트와 같은 24h) — 지나면 headline_polarity=None
EQUITY_SERIES_FILENAME = "equity_daily.json"   # 일별 시작 자본·BTC 종가·LLM 호출 카운터(일일 리뷰 지표용)
EQUITY_SERIES_CAP = 800                  # 보관할 일별 점 개수 상한
LLM_COUNTER_DAYS = 60                    # 일별 LLM 카운터 보관 일수
MIN_DAYS_FOR_ANNUALISED = J.MIN_CALMAR_DAYS  # calmar(연율화)는 일수익률 표본이 이만큼 쌓이기 전에는 null(journal 과 같은 값)
RECONCILE_TOLERANCE = 0.01               # testnet/live: 거래소 base 가 장부보다 1% 넘게 적으면 장부를 맞춘다
RECONCILE_STRIKES = 2                    # 같은 부족이 연속 2사이클 관찰되면 대사
RECOVERY_MIN_SHARE = 0.01                # 주문 예외 뒤 잔고 변화가 요청 규모의 1% 이상이면 체결로 본다
# G0 중단 알림에서 뺄 코드: 포지션 수·펀딩 창은 일상적, 킬스위치·예산·LLM 일시정지는 각자 알림이 따로 있다
HALT_SILENT_CODES = frozenset({"max_positions", "funding_window", "kill_switch", "drawdown_stop_all",
                               "llm_budget", "llm_paused"})
ALERT_STATE_FILENAME = "alert_state.json"   # 경보 중복 방지 상태(마지막 레짐·뉴스 위험 키·포지션 점검 조건)
NEWS_ALERT_DEDUPE_MS = 24 * 3_600_000    # 같은 심볼+헤드라인 뉴스 위험 경보는 24h 에 한 번
NEWS_ALERT_NO_HEADLINE = "risk_event"    # 이벤트 없이 risk_event_symbols 로만 들어온 뉴스 위험의 헤드라인 키
HEALTH_ALERT_MAX = HEALTH_FLAG_MAX       # 건강도 ≤4 → 포지션 점검 경보
HEALTH_ALERT_RECOVER = HEALTH_ALERT_MAX + 2   # 경보 중이면 건강도 ≥6 이어야 해소(4/5 진동에 반복 경보하지 않게)
HEALTH_ALERT_DECLINE_STEP = 2            # 경보 중 마지막으로 알린 건강도보다 2 이상 더 떨어지면 다시 경보
HEALTH_ACTION_NAMES: tuple[str, ...] = ("hold", "trim", "exit")
HEALTH_ACTION_RANK: dict[str, int] = {a: i for i, a in enumerate(HEALTH_ACTION_NAMES)}  # 조치가 강해지면 다시 경보
REGIME_FLAP_COOLDOWN_MS = 3_600_000      # 직전 판정으로 되돌아가는 레짐 전환·해제된 NAME 알람 재경보는 1h 뒤


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


def _journal_has_trade(path: Path, trade: ClosedTrade, *, tail_bytes: int = 262_144) -> bool:
    """``journal.jsonl`` 끝부분에 이 거래(심볼·진입·청산 시각)의 기록이 이미 있는가. 읽지 못하면 False."""
    try:
        with open(path, "rb") as fh:
            end = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, end - tail_bytes))
            data = fh.read()
    except OSError:
        return False
    for raw in data.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        t = rec.get("trade") if isinstance(rec, dict) else None
        if (isinstance(t, dict) and t.get("symbol") == trade.symbol and t.get("opened_ts") == trade.opened_ts
                and t.get("closed_ts") == trade.closed_ts):
            return True
    return False


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
        # 매도 체결 뒤 미룬 사후분석(LLM)·저널 기록: (거래, 부분 청산 여부, 주문 ID). 모든 매도가 끝난 뒤 처리한다.
        self.pending_reviews: list[tuple[ClosedTrade, bool, str | None]] = []


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
        self._last_market_tone_ts: int | None = None
        self._finished_day: Any = None            # 마지막 롤오버로 끝난 DailyStats(일일 리뷰용)
        self._active_halts: set[str] = set()      # 지금 걸려 있는(알림 보낸) G0 중단 코드
        self._lifted_halts: set[str] = set()      # 이 프로세스에서 해제 알림을 보낸 코드(다시 걸리면 다시 알림)
        self._consumed_control: dict[str, int] = {}   # 처리했지만 삭제 실패한 control 파일 kind → ts
        self._reconcile_strikes: dict[str, int] = {}  # 거래소 잔고 < 장부 가 연속 관찰된 횟수(일시 오류 방지)
        self.series_path = self.data_dir / EQUITY_SERIES_FILENAME
        self._series: dict[str, Any] = self._load_series()
        self.alert_state_path = self.data_dir / ALERT_STATE_FILENAME
        self._alert_state: dict[str, Any] = self._load_alert_state()
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

    # ------------------------------------------------------------ 거래소 잔고 도우미(testnet/live)
    @property
    def _exchange_mode(self) -> bool:
        return self.settings.mode != "paper"

    def _safe_free_base(self, symbol: str) -> float | None:
        try:
            v = float(self.broker.free_base(symbol))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 가용 base 조회 실패 type=%s", symbol, type(exc).__name__)
            return None
        return v if math.isfinite(v) and v >= 0 else None

    def _safe_free_quote(self) -> float | None:
        try:
            v = float(self.broker.free_quote())
        except Exception as exc:  # noqa: BLE001
            log.warning("가용 quote 조회 실패 type=%s", type(exc).__name__)
            return None
        return v if math.isfinite(v) and v >= 0 else None

    def _min_order(self, symbol: str) -> float:
        try:
            v = float(self.broker.min_order_usdt(symbol))
            if math.isfinite(v) and v > 0:
                return v
        except Exception:  # noqa: BLE001
            pass
        return float(HardLimits.MIN_ORDER_USDT)

    def _rounded(self, symbol: str, amount: float) -> float:
        """거래소 정밀도로 내린 수량(양수가 아니거나 변환 불가면 0)."""
        if not _pos(amount):
            return 0.0
        try:
            v = float(self.broker.amount_to_precision(symbol, float(amount)))
        except Exception:  # noqa: BLE001
            return 0.0
        return v if math.isfinite(v) and v > 0 else 0.0

    def _sellable(self, symbol: str, amount: float, price: float | None) -> bool:
        """그 수량을 따로 팔 수 있는가(정밀도 내림 후 0 이 아니고, 가격을 알면 최소 주문 금액 이상)."""
        r = self._rounded(symbol, amount)
        if r <= 0:
            return False
        if _pos(price) and r * float(price) < self._min_order(symbol):  # type: ignore[arg-type]
            return False
        return True

    def _planned_notional(self) -> float | None:
        """testnet/live: 리스크 팩 충격 추정용 계획 주문액 = (거래소 free quote + 보유 평가액) × MAX_POSITION_PCT.

        paper 는 None(스캐너가 상태의 가상 잔고로 추정한다).
        """
        if not self._exchange_mode:
            return None
        q = self._safe_free_quote()
        if q is None:
            return None
        eq = q
        for pos in self.state.positions.values():
            px = _pos(pos.last_mark) or _pos(pos.entry_price) or 0.0
            eq += max(0.0, float(pos.base_amount)) * px
        return eq * HardLimits.MAX_POSITION_PCT if eq > 0 else None

    # ------------------------------------------------------------ 뉴스 톤(headline_polarity)
    def _clear_tone(self) -> None:
        self._last_market_tone = None
        self._last_market_tone_ts = None

    def _current_tone(self, run: _Run) -> float | None:
        """마지막 market_tone(24h 이내만). 지나면 지운다(SPEC §18: 없으면 None)."""
        ts = self._last_market_tone_ts
        if self._last_market_tone is not None and (ts is None or run.now_ms - ts > NEWS_TONE_TTL_MS):
            log.info("market_tone 만료(%s) — headline_polarity 없음", ts)
            self._clear_tone()
        return self._last_market_tone

    def _can_defer_news(self) -> bool:
        """스캐너가 뉴스 조회를 코드 청산 뒤로 미룰 수 있나(``scan(include_news=...)`` + ``fetch_news``).

        테스트 대역 스캐너는 대개 둘 다 없다 → 예전처럼 스캔 안에서 뉴스까지 받는다.
        """
        scan = getattr(self.scanner, "scan", None)
        if not callable(scan) or not callable(getattr(self.scanner, "fetch_news", None)):
            return False
        try:
            return "include_news" in inspect.signature(scan).parameters
        except (TypeError, ValueError):
            return False

    def _fetch_news_after_exits(self, run: _Run) -> None:
        """코드 청산 뒤에 뉴스를 받아 ``run.scan`` 에 채운다(파이프라인·경보는 그 뒤에 쓴다)."""
        scan = run.scan
        if scan is None:
            return
        items = list(self.scanner.fetch_news(list(scan.universe)))
        run.scan = scan.model_copy(update={"news": items, "news_available": bool(items)})

    def _apply_news_availability(self, run: _Run) -> None:
        """뉴스 피드가 없으면(news_available=False) 묵은 톤을 지우고 종합심리에서 headline_polarity 를 뺀다."""
        scan = run.scan
        if scan is None or scan.news_available:
            return
        self._clear_tone()
        sent = scan.sentiment
        comps = dict(sent.components or {})
        if comps.get("headline_polarity") is None:
            return
        comps["headline_polarity"] = None
        norm = {k: float(v) for k, v in comps.items() if k in F._SENTIMENT_WEIGHTS and _finite(v)}
        if norm:
            wsum = sum(F._SENTIMENT_WEIGHTS[k] for k in norm)
            score: float | None = sum(F._SENTIMENT_WEIGHTS[k] * v for k, v in norm.items()) / wsum
        else:
            score = None
        new = sent.model_copy(update={"components": comps, "score_0_100": score,
                                      "label": F.sentiment_label(score)})
        run.scan = scan.model_copy(update={"sentiment": new})
        log.info("뉴스 피드 없음 — 종합심리에서 묵은 headline_polarity 제거")

    # ------------------------------------------------------------ 일별 자본 곡선(일일 리뷰 지표)
    def _load_series(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"points": [], "llm": {}}
        p = self.series_path
        if not p.is_file():
            return empty
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("일별 자본 곡선 파일을 읽지 못함(%s) — 새로 시작", type(exc).__name__)
            return empty
        if not isinstance(obj, Mapping):
            return empty
        pts = [dict(x) for x in obj.get("points") or [] if isinstance(x, Mapping)
               and isinstance(x.get("date"), str) and _pos(x.get("equity"))]
        llm = {str(k): dict(v) for k, v in (obj.get("llm") or {}).items() if isinstance(v, Mapping)}
        return {"points": sorted(pts, key=lambda x: x["date"]), "llm": llm}

    def _save_series(self, run: _Run) -> None:
        p = self.series_path
        tmp = p.with_name(p.name + ".tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._series, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            fsutil.replace(tmp, p)
        except OSError as exc:
            # 보조 상태 — 사이클을 멈추지 않는다
            run.errors.append(f"equity_series_save: {type(exc).__name__}")
            log.error("일별 자본 곡선 저장 실패 type=%s", type(exc).__name__)

    def _record_day_point(self, run: _Run) -> None:
        """그날 첫 사이클의 자본(=전날 마감 자본)과 BTC 시세를 일별 곡선에 남긴다."""
        if not _pos(run.equity):
            return
        today = run.now.strftime("%Y-%m-%d")
        pts: list[dict] = self._series["points"]
        if pts and pts[-1]["date"] >= today:
            return
        btc = _pos(run.scan.marks.get(BTC_SYMBOL)) if run.scan is not None else None
        pts.append({"date": today, "ts": run.now_ms, "equity": float(run.equity), "btc": btc})
        if len(pts) > EQUITY_SERIES_CAP:
            del pts[: len(pts) - EQUITY_SERIES_CAP]
        self._save_series(run)

    def _count_llm(self, run: _Run) -> None:
        """당일 LLM 호출·기권 수 누적(일일 리뷰 ``metrics_table.llm``)."""
        today = run.now.strftime("%Y-%m-%d")
        llm: dict[str, dict] = self._series["llm"]
        c = llm.setdefault(today, {"calls": 0, "abstain": 0})
        res = run.result
        if res is not None:
            c["calls"] = int(c.get("calls", 0)) + int(res.llm_calls)
            c["abstain"] = int(c.get("abstain", 0)) + len(res.abstained)
        for k in sorted(llm)[:-LLM_COUNTER_DAYS]:
            llm.pop(k, None)
        self._save_series(run)

    # ------------------------------------------------------------ 대사(reconciliation)
    def _reconcile_journal(self, run: _Run) -> None:
        """저널 상태를 장부(state.positions)에 맞춘다(재시작·충돌·저널 파일 손상 뒤에도 레드라인이 살아 있게).

        - 보유 중인데 저널 논지가 없으면 ``Position.thesis`` JSON 에서 복구한다(빈 thesis_history·entry_verdicts).
        - 보유하지 않는 심볼의 저널 항목(청산 후 정리 전에 죽은 경우)은 코드 사후분석 기록을 남기고 지운다.
        """
        js = self.journal_state
        held = self.state.positions
        changed = False
        for sym, pos in sorted(held.items()):
            if sym in js.thesis:
                continue
            try:
                th = Thesis.model_validate(json.loads(pos.thesis))
            except Exception:  # noqa: BLE001 - 논지 JSON 이 없거나 형식이 다르면 복구할 것이 없다
                continue
            js.thesis[sym] = th
            if sym not in js.entry_verdicts:
                js.entry_verdicts[sym] = J.normalize_entry_verdicts(None)
            js.thesis_history.setdefault(sym, [])
            js.health_history.setdefault(sym, [])
            js.news_risk_after_entry.setdefault(sym, [])
            changed = True
            log.warning("%s 저널 논지 없음 — 포지션 논지에서 복구(레드라인 %d개)", sym, len(th.red_lines))
        orphans: set[str] = set()
        for d in (js.thesis, js.entry_verdicts, js.thesis_history, js.prev_assumption_status,
                  js.news_risk_after_entry, js.health_history):
            orphans |= {k for k in d if k not in held}
        for sym in sorted(orphans):
            trade = next((t for t in reversed(self.state.closed_trades) if t.symbol == sym), None)
            if trade is not None and _journal_has_trade(self.journal_path, trade):
                # 저널 기록은 남았지만 저널 상태 저장 전에 멈춘 경우(중단 처리·디스크 가득) — 같은 거래를 두 번 쓰지 않는다
                log.info("%s 청산 기록은 이미 저널에 있습니다 — 저널 상태만 정리", sym)
            elif trade is not None:
                try:
                    reports.append_journal(self.journal_path, trade,
                                           {"code_summary": J.code_post_trade_summary(trade), "llm": False,
                                            "reconciled": True})
                except (OSError, TypeError, ValueError) as exc:
                    run.errors.append(f"journal_log:{sym}: {type(exc).__name__}")
            J.clear_symbol(js, sym)
            changed = True
            log.warning("%s 미보유 심볼의 저널 항목 정리(청산 후 정리 누락)", sym)
        if changed:
            self._save_journal(run)

    def _reconcile_exchange(self, run: _Run) -> None:
        """testnet/live: 보유 종목의 거래소 base 잔고가 장부보다 적으면 장부를 거래소에 맞춘다.

        (매도 체결 뒤 응답 오류·수동 매도 등으로 생긴 유령 포지션이 자본에 이중 계상되고 슬롯을 점유하지 않게.)
        팔 수 있는 잔량이 남으면 부족분만, 아니면 전량을 시세 기준으로 ``manual`` 청산 기록하고 알린다.
        """
        if not self._exchange_mode:
            return
        for sym in sorted(self.state.positions):
            pos = self.state.positions.get(sym)
            if pos is None:
                continue
            free = self._safe_free_base(sym)
            if free is None:
                continue
            book = float(pos.base_amount)
            if free >= book * (1.0 - RECONCILE_TOLERANCE):
                self._reconcile_strikes.pop(sym, None)
                continue
            strikes = self._reconcile_strikes.get(sym, 0) + 1
            self._reconcile_strikes[sym] = strikes
            if strikes < RECONCILE_STRIKES:
                # 잔고 조회의 일시 오류로 실제 보유를 지우지 않게 연속 관찰된 뒤에만 장부를 맞춘다
                log.warning("%s 거래소 잔고 %.8g < 장부 %.8g — 다음 사이클에 다시 확인", sym, free, book)
                continue
            self._reconcile_strikes.pop(sym, None)
            mark = self._mark_for(run, pos) or float(pos.entry_price)
            gone = book if not self._sellable(sym, free, mark) else book - free
            text = (f"reconcile: 거래소 {sym} 잔고 {free:.8g} < 장부 {book:.8g} — 장부에서 {gone:.8g} 를 "
                    f"시세 {mark:.6g} 기준으로 정리(수동 확인 필요)")
            log.error(text)
            run.errors.append(f"reconcile:{sym}: {text}")
            self._notify(self._event("error", run, symbol=sym, reason=text, extra={"stage": "reconcile"}))
            fill = Fill(order_id=f"reconcile-{run.now_ms}", symbol=sym, side="sell", base_amount=gone, price=mark,
                        quote_amount=gone * mark, ts=run.now_ms, raw={"reconcile": True})
            self._stage(run, f"reconcile:{sym}", lambda s=sym, f=fill: self._book_sell(
                run, s, f, "manual", requested_reason="manual", note="거래소 잔고 대사"))

    # ------------------------------------------------------------ G0 중단 알림
    def _notify_halts(self, run: _Run, g0: G.GateDecision | None) -> None:
        """G0 중단(당일 −3%·연속 손실·낙폭 차단 등)이 새로 걸리면 한 번, 풀리면 한 번 알린다(DESIGN §7)."""
        if g0 is None:
            return
        js = self.journal_state
        codes = [] if g0.passed else [c for c in G.reason_codes(g0) if c not in HALT_SILENT_CODES]
        current = set(codes)
        new = [c for c in dict.fromkeys(codes) if c not in self._active_halts
               and (c not in js.halts_today or c in self._lifted_halts)]
        lifted = sorted(self._active_halts - current)
        self._active_halts = current
        if new:
            reasons = [r for r in g0.reasons if any(f"({c})" in r or r.startswith(f"{c}:") for c in new)]
            text = "신규 진입 중단 (G0): " + "; ".join(reasons or new)
            eq = run.equity if _finite(run.equity) else None
            self._notify(self._event("halt", run, reason=text[:500], equity=eq,
                                     extra={"stage": "g0", "code": "halt", "halt_codes": new}))
            self._lifted_halts -= set(new)
        if lifted:
            self._lifted_halts |= set(lifted)
            eq = run.equity if _finite(run.equity) else None
            self._notify(self._event("halt_lifted", run, reason=f"신규 진입 중단 해제: {', '.join(lifted)}",
                                     equity=eq, extra={"stage": "g0", "event": "halt_lifted", "halt_codes": lifted}))

    # ------------------------------------------------------------ 경보(DESIGN §7: 레짐·뉴스 위험·포지션 점검)
    def _load_alert_state(self) -> dict[str, Any]:
        """``alert_state.json``: {"regime": 마지막으로 알린 판정(키가 없으면 첫 실행), "regime_prev": 그 직전 판정,
        "regime_ts": 마지막 레짐 경보 시각, "name_alarm": {심볼: 시각}, "news": {"심볼|헤드라인": ts},
        "health": {심볼: 조건 기록}}. 손상·없음·형식 오류 → 빈 상태(첫 실행처럼 동작; 예외를 밖으로 내지 않는다)."""
        p = self.alert_state_path
        try:
            if not p.is_file():
                return self._empty_alert_state()
            obj = json.loads(p.read_text(encoding="utf-8"))
            return self._parse_alert_state(obj)
        except Exception as exc:  # noqa: BLE001 - OSError·ValueError·RecursionError·형식 오류 모두 새로 시작
            log.warning("경보 상태 파일을 읽지 못함(%s) — 새로 시작", type(exc).__name__)
            return self._empty_alert_state()

    @staticmethod
    def _empty_alert_state() -> dict[str, Any]:
        return {"news": {}, "health": {}, "name_alarm": {}}

    @staticmethod
    def _parse_alert_state(obj: Any) -> dict[str, Any]:
        out = Cycle._empty_alert_state()
        if not isinstance(obj, Mapping):
            return out

        def _ts_map(raw: Any) -> dict[str, int]:
            res: dict[str, int] = {}
            if isinstance(raw, Mapping):
                for k, v in raw.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and _finite(v):
                        res[str(k)] = int(float(v))
            return res

        out["news"] = _ts_map(obj.get("news"))
        out["name_alarm"] = _ts_map(obj.get("name_alarm"))
        health = obj.get("health")
        if isinstance(health, Mapping):
            out["health"] = {str(k): v for k, v in health.items() if isinstance(v, str)}
        for key in ("regime", "regime_prev"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                out[key] = v
        ts = obj.get("regime_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and _finite(ts):
            out["regime_ts"] = int(float(ts))
        return out

    def _save_alert_state(self, run: _Run) -> None:
        """tmp → fsync → replace → 디렉터리 fsync(state.json·저널·뉴스 저장소와 같은 내구성)."""
        p = self.alert_state_path
        tmp = p.with_name(p.name + ".tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._alert_state, ensure_ascii=False, sort_keys=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:  # 일부 파일시스템은 fsync 미지원
                    pass
            fsutil.replace(tmp, p)
            _fsync_dir(p.parent)
        except OSError as exc:
            # 보조 상태 — 사이클을 멈추지 않는다(다음 사이클에 같은 경보가 다시 갈 수는 있다)
            run.errors.append(f"alert_state_save: {type(exc).__name__}")
            log.error("경보 상태 저장 실패 type=%s", type(exc).__name__)
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass

    def _emit_alerts(self, run: _Run) -> None:
        """경보를 판정하고, 중복 방지 상태를 먼저 저장한 뒤 notify 한다(각 판정 실패는 서로 막지 않는다)."""
        events: list[TradeEvent] = []
        for name, fn in (("regime", self._regime_alerts), ("news_risk", self._news_risk_alerts),
                         ("position_health", self._health_alerts)):
            try:
                events.extend(fn(run))
            except Exception as exc:  # noqa: BLE001 - 경보 판정 실패는 사이클을 죽이지 않는다
                run.errors.append(f"alert:{name}: {type(exc).__name__}")
                log.error("경보 판정 실패 alert=%s type=%s", name, type(exc).__name__)
        self._save_alert_state(run)
        for ev in events:
            self._notify(ev)

    @staticmethod
    def _effective_regime(rg: Any) -> str:
        """G3 가 실제로 적용하는 레짐 규칙(gates.g3_portfolio 와 같은 판정): macro_alarm → 신규 진입 금지,
        ``verdict == "fused"`` 또는 ``fused is True`` → FUSED(×0.5·하한 0.75), 그 밖에는 normal.
        BTC 데이터 실패로 scanner 가 verdict 만 normal 로 바꾸고 fused=True 를 남기면 FUSED 규칙이 그대로다."""
        v = str(getattr(rg, "verdict", "") or "")
        if v == "macro_alarm":
            return "macro_alarm"
        if v == "fused" or getattr(rg, "fused", None) is True:
            return "fused"
        return "normal"

    def _held_symbols(self, run: _Run) -> set[str]:
        res = run.result
        held = set(self.state.positions)
        if res is not None:
            held |= {pc.symbol for pc in res.positions}
        return held

    def _regime_alerts(self, run: _Run) -> list[TradeEvent]:
        """레짐(G3 가 적용하는 규칙 기준) 이 마지막으로 알린 판정과 다르면 한 번. 기록이 없는 첫 실행은 normal 이
        아닐 때만. 직전 판정으로 되돌아가는 전환은 마지막 경보 뒤 ``REGIME_FLAP_COOLDOWN_MS`` 동안 미룬다(임계값
        근처 진동 방지 — 그 뒤에도 다르면 그때 알린다). 보유 종목에 새 NAME 알람이 생기면 판정이 같아도 알린다."""
        scan = run.scan
        if scan is None:
            return []
        rg = scan.regime
        now = int(run.now_ms)
        new = self._effective_regime(rg)
        raw = str(rg.verdict)
        st = self._alert_state
        first = "regime" not in st
        old = st.get("regime")
        changed = False
        if first:
            st["regime"] = new
            changed = new != "normal"
        elif old != new:
            last_ts = st.get("regime_ts")
            flap = (new == st.get("regime_prev") and isinstance(last_ts, int)
                    and now - last_ts < REGIME_FLAP_COOLDOWN_MS)
            if flap:
                log.info("레짐 %s → %s 되돌림 — 경보 보류(마지막 경보 뒤 %ds)", old, new, (now - last_ts) // 1000)
            else:
                changed = True
        if changed:
            st["regime_prev"] = None if first else old
            if st["regime_prev"] is None:
                st.pop("regime_prev", None)
            st["regime"] = new
            st["regime_ts"] = now

        # 보유 종목 NAME 알람(신규 진입 금지·총괄 SELL 전량) — 심볼당 한 번, 해제 뒤 쿨다운이 지나야 다시
        alarms = [str(s) for s in (rg.name_alarm_symbols or [])]
        st_na: dict[str, int] = st.setdefault("name_alarm", {})
        for k in [k for k, ts in st_na.items() if k not in alarms and now - int(ts) >= REGIME_FLAP_COOLDOWN_MS]:
            st_na.pop(k, None)
        held = self._held_symbols(run)
        held_alarms = sorted(s for s in set(alarms) if s in held)
        new_alarms = [s for s in held_alarms if s not in st_na]
        for s in held_alarms:
            if changed or s in new_alarms:
                st_na[s] = now
        if not changed and not new_alarms:
            return []
        extra = {"alert": "regime", "verdict_changed": changed,
                 "old_verdict": (None if first else old) if changed else new, "new_verdict": new,
                 "raw_verdict": raw, "fused": bool(rg.fused),
                 "btc_shock": bool(rg.btc_shock), "name_alarm_symbols": alarms,
                 "name_alarm_held_new": new_alarms,
                 "btc_regime_4h": rg.btc_regime_4h,
                 "avg_pairwise_corr_1h": rg.avg_pairwise_corr_1h if _finite(rg.avg_pairwise_corr_1h) else None}
        if changed:
            log.info("레짐 전환 %s → %s", extra["old_verdict"], new)
            reason = f"regime {extra['old_verdict']} -> {new}"
        else:
            log.info("보유 종목 NAME 알람 %s", ",".join(new_alarms))
            reason = f"name_alarm {','.join(new_alarms)}"
        return [self._event("alert", run, reason=reason, extra=extra)]

    def _news_risk_alerts(self, run: _Run) -> list[TradeEvent]:
        """관심 종목(설정 SYMBOLS·스캔 유니버스)이나 보유 종목이 뉴스 위험 집합에 들어오면 심볼+헤드라인당 24h 에 한 번.
        헤드라인은 그 심볼의 뉴스 위험 이벤트(점수 ≤ −0.66)만 쓴다 — 없으면(risk_event_symbols 로만 들어옴) 고정 키
        ``risk_event`` 이고, 같은 심볼을 24h 안에 이미 알렸으면(이벤트가 위험 심볼보다 먼저 만료된 경우 등) 다시 알리지 않는다."""
        res = run.result
        st_news: dict[str, int] = self._alert_state.setdefault("news", {})
        now = int(run.now_ms)
        for k in [k for k, ts in st_news.items() if now - int(ts) >= NEWS_ALERT_DEDUPE_MS]:
            st_news.pop(k, None)
        if res is None or not res.news_risk_symbols:
            return []
        held = self._held_symbols(run)
        watch = set(self.settings.symbols) | held
        if run.scan is not None:
            watch |= set(run.scan.universe)
        pcs = {pc.symbol: pc for pc in res.positions}
        try:
            active = self.news_store.active_events(now)
        except Exception:  # noqa: BLE001 - 저장소 조회 실패면 헤드라인 없이 알린다
            active = []
        out: list[TradeEvent] = []
        for sym in sorted(set(res.news_risk_symbols)):
            if sym not in watch:
                continue
            ev = self._worst_news_event(sym, active)
            hid = str(ev.get("headline_id")) if ev is not None and ev.get("headline_id") else NEWS_ALERT_NO_HEADLINE
            key = f"{sym}|{hid}"
            if key in st_news:
                continue
            if hid == NEWS_ALERT_NO_HEADLINE and any(k.split("|", 1)[0] == sym for k in st_news):
                continue                     # 같은 심볼 위험을 24h 안에 이미 알림(헤드라인만 먼저 만료)
            st_news[key] = now
            pc = pcs.get(sym)
            is_held = sym in held
            extra: dict[str, Any] = {
                "alert": "news_risk", "headline_id": None if ev is None else hid,
                "event_type": ev.get("event_type") if ev is not None else None,
                "score": snap_news_score(ev.get("score")) if ev is not None else None,
                "summary": self._scrub(ev.get("summary"))[:500] if ev is not None and ev.get("summary") else None,
                "held": is_held,
                "flagged_for_head": bool(pc.flagged_for_head) if pc is not None else None,
                "flag_reason": pc.flag_reason if pc is not None else None,
            }
            log.info("뉴스 위험 경보 %s headline=%s", sym, hid)
            out.append(self._event("alert", run, symbol=sym, reason=f"news_risk {sym}", extra=extra))
        return out

    @staticmethod
    def _worst_news_event(symbol: str, events: list[dict]) -> dict | None:
        """심볼을 언급한 만료 전 뉴스 위험 이벤트(점수 ≤ −0.66, 묵지 않음) 중 가장 낮은 점수·최신. 없으면 None
        (중립 기사를 위험 근거로 보여주거나 새 기사마다 키가 바뀌지 않게)."""
        def _score(e: Mapping) -> float:
            v = snap_news_score(e.get("score"))
            return float(v) if v is not None else 0.0

        risky = [e for e in events if isinstance(e, Mapping) and P._symbol_in(symbol, e.get("symbols") or [])
                 and _score(e) <= NEWS_RISK_SCORE and not e.get("stale")]
        if not risky:
            return None
        return dict(min(risky, key=lambda e: (_score(e), -int(e.get("ts") or 0), str(e.get("headline_id") or ""))))

    @staticmethod
    def _red_line_key(hit: str) -> str:
        """레드라인 적중 문구에서 현재값을 뺀 규칙 부분(조건 서명용 — 현재값이 바뀌어도 같은 조건)."""
        return str(hit).split(" — 현재", 1)[0].strip()

    @staticmethod
    def _health_record(raw: Any) -> dict[str, Any] | None:
        """저장된 조건 기록(JSON 문자열) → {low, red, health, act}. 구버전({low, red})·손상은 기본값으로."""
        if not isinstance(raw, str) or not raw:
            return None
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            obj = None
        if not isinstance(obj, Mapping):
            return {"low": False, "red": [], "health": None, "act": -1}
        h = obj.get("health")
        a = obj.get("act")
        red = obj.get("red")
        return {"low": obj.get("low") is True,
                "red": sorted({str(x) for x in red}) if isinstance(red, list) else [],
                "health": int(h) if isinstance(h, int) and not isinstance(h, bool) else None,
                "act": int(a) if isinstance(a, int) and not isinstance(a, bool) else -1}

    def _health_alerts(self, run: _Run) -> list[TradeEvent]:
        """보유 포지션의 건강도 ≤4 이거나 코드 레드라인이 적중하면 조건이 나빠질 때마다 한 번(매 사이클 아님).

        - 건강도: ≤4 로 내려오면 경보, ``HEALTH_ALERT_RECOVER`` 이상이어야 해소(히스테리시스 — 4/5 진동 방지).
          경보 중 마지막으로 알린 건강도보다 ``HEALTH_ALERT_DECLINE_STEP`` 이상 더 떨어지거나 리스크 조치가
          hold → trim → exit 로 강해지면 다시 알린다. 리스크 관리자 기권이면 건강도 조건은 지난 판정 유지.
        - 레드라인: 새 규칙이 적중하면 경보. 이번 사이클에 레드라인을 평가하지 못했으면(파이프라인 조기 종료·평가
          실패) 지난 기록을 유지한다(빈 적중을 '해소'로 읽지 않는다).
        - 모두 해소되면 기록 삭제(다시 나빠지면 새로 알림). 개선만 된 변화는 기록만 고친다."""
        st_h: dict[str, str] = self._alert_state.setdefault("health", {})
        res = run.result
        out: list[TradeEvent] = []
        for pc in (res.positions if res is not None else []):
            sym = pc.symbol
            prev = self._health_record(st_h.get(sym))
            prev_low = bool(prev and prev["low"])
            prev_red: list[str] = list(prev["red"]) if prev else []
            prev_health = prev["health"] if prev else None
            prev_act = int(prev["act"]) if prev else -1
            risk_view = pc.risk
            health = int(risk_view.health) if risk_view is not None else None
            if health is None:
                low = prev_low
            elif prev_low:
                low = health < HEALTH_ALERT_RECOVER
            else:
                low = health <= HEALTH_ALERT_MAX
            checked = bool(getattr(pc, "red_lines_checked", True))
            red_keys = sorted({self._red_line_key(h) for h in pc.red_line_hits}) if checked else prev_red
            if not low and not red_keys:
                st_h.pop(sym, None)          # 조건 해소 → 다시 나빠지면 새로 알린다
                continue
            act = HEALTH_ACTION_RANK.get(str(risk_view.action), -1) if risk_view is not None else -1
            became_low = low and not prev_low
            new_red = sorted(set(red_keys) - set(prev_red))
            declined = (low and prev_low and health is not None and prev_health is not None
                        and health <= prev_health - HEALTH_ALERT_DECLINE_STEP)
            escalated = prev is not None and act >= 1 and act > prev_act
            alert = prev is None or became_low or bool(new_red) or declined or escalated
            if alert:
                rec_health = health if low and health is not None else (prev_health if low else None)
                rec_act = max(prev_act, act)
            else:
                rec_health = prev_health if low else None
                rec_act = prev_act
            st_h[sym] = json.dumps({"low": low, "red": red_keys, "health": rec_health, "act": rec_act},
                                   ensure_ascii=False, sort_keys=True)
            if not alert:
                continue
            pos = self.state.positions.get(sym)
            upct = None
            if pos is not None:
                upct = P.unrealized_pct(pos.entry_price, self._mark_for(run, pos))
            else:
                done = next((x for x in reversed(run.exits) if x.get("symbol") == sym), None)
                if done is not None and _finite(done.get("pnl_pct")):
                    upct = float(done["pnl_pct"]) * 100.0
            hits = list(pc.red_line_hits) if checked else list(prev_red)
            reds = list(dict.fromkeys([*hits, *(risk_view.red_lines_triggered if risk_view else [])]))
            head: dict[str, Any] | None = None
            d = res.decision_for(sym) if res is not None else None
            if d is not None:
                act_rec = run.actions.get(sym) or {}
                head = {"action": d.action, "confidence": d.confidence, "rationale": self._scrub(d.rationale)[:300]}
                if d.action in ("SELL", "BUY"):
                    head["executed"] = bool(act_rec.get("executed")) and act_rec.get("action") == d.action
            worse: dict[str, Any] = {}
            if declined:
                worse["health"] = [prev_health, health]
            if escalated and prev_act >= 0:
                worse["risk_action"] = [HEALTH_ACTION_NAMES[prev_act], HEALTH_ACTION_NAMES[act]]
            extra: dict[str, Any] = {
                "alert": "position_health", "health": health, "health_low": low,
                "red_lines": [self._scrub(r)[:200] for r in reds],
                "risk_action": risk_view.action if risk_view is not None else None,
                "risk_reason": self._scrub(risk_view.reason)[:200] if risk_view is not None else None,
                "assumption_status": list(risk_view.assumption_status) if risk_view is not None else [],
                "unrealized_pct": upct if _finite(upct) else None,
                "flag_reason": pc.flag_reason, "head_decision": head,
                "worsened": worse or None,
            }
            log.info("포지션 점검 경보 %s health=%s red=%d", sym, health, len(red_keys))
            out.append(self._event("alert", run, symbol=sym, reason=f"position_health {sym}", extra=extra))
        # 보유하지 않는 심볼의 조건 기록은 지운다(다시 사면 새로 판정)
        for sym in [k for k in st_h if k not in self.state.positions]:
            st_h.pop(sym, None)
        return out

    # ================================================================ 본체
    def run_once(self) -> CycleSummary:
        run = _Run(self._now(), float(self._mono()))
        try:
            return self._run_once(run)
        except BaseException:
            # 치명 오류(뒤 매도의 상태 저장 실패 등)로 끝나도 이미 판 거래의 저널 기록은 남긴다 — 사후분석을 미룬 탓에
            # 사라지지 않게(부분 청산 기록은 재시작 뒤 복구되지 않는다)
            self._journal_pending_on_abort(run)
            raise

    def _run_once(self, run: _Run) -> CycleSummary:
        js = self.journal_state

        # 1. 잠금·control 파일
        self._stage(run, "lock", self._check_lock)
        self._stage(run, "control", lambda: self._process_control(run))

        # 2. 사이클 증가·롤오버·일일 리뷰
        self.state.cycle = int(self.state.cycle) + 1
        cycle = self.state.cycle
        self._stage(run, "sync_broker", self._sync_paper_broker)
        self._stage(run, "rollover", lambda: self._rollover(run))
        # 저널 상태 ↔ 장부 대사(재시작·충돌 뒤 레드라인 복구, 청산 후 남은 항목 정리)
        self._stage(run, "journal_reconcile", lambda: self._reconcile_journal(run))

        # 3. 스캔·마크·equity·peak·낙폭 사다리
        scan_kw: dict[str, Any] = {"headline_polarity": self._current_tone(run)}
        planned = self._planned_notional()
        if planned is not None:
            scan_kw["planned_notional"] = planned
        defer_news = self._can_defer_news()
        if defer_news:
            scan_kw["include_news"] = False
        run.scan = self._stage(run, "scan", lambda: self.scanner.scan(self.state, run.now_ms, **scan_kw))
        if not defer_news:
            self._stage(run, "news_tone", lambda: self._apply_news_availability(run))
        self._stage(run, "marks", lambda: self._update_marks(run))
        self._stage(run, "equity_series", lambda: self._record_day_point(run))
        self._stage(run, "exchange_reconcile", lambda: self._reconcile_exchange(run))

        # 4. 코드 청산 — 손절·익절·시간손절을 LLM 호출(사후분석·일일 리뷰)·뉴스 조회보다 먼저 집행한다
        self._stage(run, "code_exits", lambda: self._code_exits(run))
        self._stage(run, "post_trade", lambda: self._drain_reviews(run))
        if defer_news:
            self._stage(run, "news", lambda: self._fetch_news_after_exits(run))
            self._stage(run, "news_tone", lambda: self._apply_news_availability(run))
        self._stage(run, "daily_review", lambda: self._maybe_daily_review(run))

        # 5. G0
        g0 = self._stage(run, "g0", lambda: self._g0(run))
        run.g0 = g0
        self._stage(run, "halt_notify", lambda: self._notify_halts(run, g0))
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
        # 총괄 SELL 의 사후분석도 모든 매도가 끝난 뒤
        self._stage(run, "post_trade", lambda: self._drain_reviews(run))

        # 8. 결정 로그·보고서·저장
        self.state.last_cycle_ts = run.now_ms
        try:
            self.state.daily.llm_cost_usd = float(self.ledger.spent_today())
        except Exception:  # noqa: BLE001
            pass
        self._stage(run, "llm_counters", lambda: self._count_llm(run))
        self._stage(run, "decision_log", lambda: self._write_decisions(run))
        summary = self._summary(run, cycle)
        self._stage(run, "cycle_report", lambda: self._write_report(run, cycle, summary))
        self._save()
        self._save_journal(run)
        # 9. 경보(레짐 전환·뉴스 위험·포지션 점검) — 상태 저장 뒤, 실패해도 사이클은 계속
        self._stage(run, "alerts", lambda: self._emit_alerts(run))
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
        paths = {"stop": stop_p, "clear": clear_p}
        for order, kind in ((0, "stop"), (1, "clear")):
            info = read_control_file(paths[kind])
            if info is None:
                # 파일이 없어졌으면 '처리함' 표시도 지운다 — 같은 ts 를 가진 새 파일(수정 시각이 유지되는 템플릿 복사,
                # ts 를 고정한 JSON)은 다시 적용된다(다음 저장에 함께 남는다)
                self._consumed_control.pop(kind, None)
                self.state.control_applied_ts.pop(kind, None)
                continue
            if info["ts"] in (self._consumed_control.get(kind), self.state.control_applied_ts.get(kind)):
                # 이미 처리했지만 삭제에 실패한 파일(재시작 전에 처리한 것 포함) — 다시 적용하지 않고 삭제만 재시도한다
                log.warning("이미 처리한 %s 파일이 남아 있습니다(삭제 실패) — 다시 적용하지 않고 삭제를 다시 시도합니다",
                            paths[kind].name)
                self._unlink_control(paths[kind], kind, info["ts"])
                continue
            events.append((info["ts"], order, kind, info))
        events.sort()
        # CLEAR 보다 오래된 STOP 은 적용하지 않는다(지우기만): clear-stop 이 STOP 을 지우지 못한 채 남았거나
        # stop → clear-stop 이 한 사이클 안에 왔을 때, 옛 STOP 이 낙폭 정지 사유를 덮어써 해제를 망치지 않게.
        latest_clear = max((ts for ts, _, kind, _ in events if kind == "clear"), default=None)
        pending: list[TradeEvent] = []
        for ts, _, kind, info in events:
            if kind == "stop" and latest_clear is not None and ts <= latest_clear:
                log.info("CLEAR 보다 오래된 STOP 은 적용하지 않고 지웁니다")
                continue
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
                    # 사람이 낙폭 정지를 해제 → 최고자본을 현재 자본으로 재설정(아니면 다음 사이클에 다시 발동).
                    # 해제와 같은 저장에 담는다(마크 갱신 전에 죽어도 CLEAR 가 유지되게); 마크 갱신에서 한 번 더 맞춘다.
                    run.reset_peak = True
                    self._reset_peak_from_last_marks()
                if was:
                    pending.append(self._event(
                        "startup", run, reason=f"킬스위치 해제 — 신규 진입 재개 (이전 사유: {prev or '-'}; "
                                               f"요청: {info.get('reason') or '-'})",
                        extra={"event": "kill_cleared"}))
        if not events:
            return
        for ts, _, kind, _ in events:
            self.state.control_applied_ts[kind] = int(ts)     # 적용·무시한 파일 모두 — 같은 저장에 담는다
        # 상태 저장 → 알림 → control 파일 삭제 (저장 실패 시 파일은 남아 재시작 후 다시 처리된다;
        # 삭제 실패가 알림을 막지 않게 알림을 먼저 보낸다)
        self._save()
        for ev in pending:
            self._notify(ev)
        for _, _, kind, info in events:
            self._unlink_control(paths[kind], kind, info["ts"])

    def _unlink_control(self, path: Path, kind: str, ts: int) -> None:
        try:
            fsutil.unlink(path)
            self._consumed_control.pop(kind, None)
        except OSError as exc:
            self._consumed_control[kind] = int(ts)
            log.error("control 파일 삭제 실패 %s type=%s — 다음 사이클에 다시 적용하지 않음", path.name,
                      type(exc).__name__)

    def _reset_peak_from_last_marks(self) -> None:
        marks = {s: float(p.last_mark) for s, p in self.state.positions.items() if _pos(p.last_mark)}
        try:
            eq = self._equity(marks)
        except Exception as exc:  # noqa: BLE001 - live free_quote 실패 등: 마크 갱신 단계에서 다시 시도
            log.warning("CLEAR 최고자본 재설정용 자본 계산 실패 type=%s", type(exc).__name__)
            return
        if _pos(eq):
            log.warning("킬스위치 해제로 최고자본 재설정: %.2f → %.2f", self.state.peak_equity, eq)
            self.state.peak_equity = float(eq)

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
        # 일일 리뷰는 코드 청산 뒤에 돈다 — 이번 사이클(오늘) 청산은 '첫 가동' 판정에 넣지 않는다
        if last is None and not any(_utc_date(t.closed_ts) <= yesterday for t in trades):
            J.start_new_day(js, yesterday)        # 첫 가동: 리뷰할 것이 없다
            self._save_journal(run)
            return
        J.refresh_stats(js, trades, self.settings.cycle_minutes)
        day_trades = [t for t in trades if _utc_date(t.closed_ts) == yesterday]
        fin = self._finished_day if (self._finished_day is not None and self._finished_day.date == yesterday) else None
        start_eq = float(fin.start_equity) if fin is not None and _pos(fin.start_equity) else None
        eq_now = run.equity if _finite(run.equity) else None
        curve, btc_curve = self._review_curves(run, start_eq, eq_now)
        metrics = J.code_metrics(trades, curve, btc_curve, setups=js.entry_context)
        day_pnl = sum(t.pnl_quote for t in day_trades)
        counts = self._series["llm"].get(yesterday) or {}
        calls = counts.get("calls")
        abstain = counts.get("abstain")
        table = J.build_metrics_table(
            metrics, js, closed_today=len(day_trades),
            day_pnl_pct=(day_pnl / start_eq * 100.0) if start_eq else None,
            cum_pnl_pct=self._cum_pnl_pct(curve, eq_now),
            equity=eq_now,
            llm={"calls": int(calls) if _finite(calls) else None,
                 "cost_usd": float(fin.llm_cost_usd) if fin is not None else None,
                 "budget_usd": float(self.settings.llm_daily_budget_usd),
                 "abstain_count": int(abstain) if _finite(abstain) else None})
        self._fix_review_table(table, metrics, eq_now)
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

    def _review_curves(self, run: _Run, start_eq: float | None,
                       eq_now: float | None) -> tuple[list[float], list[float | None] | None]:
        """일일 리뷰용 일별 자본 곡선(+같은 길이 BTC 곡선). 기록이 2점 미만이면 [전일 시작, 지금] 으로 대신한다."""
        today = run.now.strftime("%Y-%m-%d")
        pts = [p for p in self._series["points"] if p["date"] <= today]
        curve = [float(p["equity"]) for p in pts]
        btc: list[float | None] = [_pos(p.get("btc")) for p in pts]
        if eq_now is not None and (not pts or pts[-1]["date"] != today):
            curve.append(float(eq_now))
            btc.append(_pos(run.scan.marks.get(BTC_SYMBOL)) if run.scan is not None else None)
        if len(curve) < 2:
            return [x for x in (start_eq, eq_now) if x is not None], None
        return curve, (btc if sum(1 for b in btc if b is not None) >= 2 else None)

    def _cum_pnl_pct(self, curve: list[float], eq_now: float | None) -> float | None:
        """누적 손익률: paper 는 시작 잔고, testnet/live 는 처음 기록한 자본 대비."""
        if eq_now is None:
            return None
        base = float(self.settings.paper_start_balance) if self.settings.mode == "paper" else (
            curve[0] if curve else None)
        return (float(eq_now) / base - 1.0) * 100.0 if base is not None and base > 0 else None

    def _fix_review_table(self, table: dict, metrics: Mapping[str, Any], eq_now: float | None) -> None:
        """최대 낙폭은 최고자본 기준과 일별 곡선 중 큰 값, calmar 는 표본이 적으면 null."""
        ret = table.get("returns")
        if isinstance(ret, dict) and eq_now is not None and _pos(self.state.peak_equity):
            dd_now = max(0.0, (1.0 - float(eq_now) / float(self.state.peak_equity)) * 100.0)
            cur = ret.get("max_drawdown_pct")
            ret["max_drawdown_pct"] = max(float(cur), dd_now) if _finite(cur) else dd_now
        stats = table.get("trade_stats")
        n_days = metrics.get("n_days")
        if isinstance(stats, dict) and "calmar" in stats and not (_finite(n_days) and n_days >= MIN_DAYS_FOR_ANNUALISED):
            stats["calmar"] = None      # 며칠치 수익률을 연율화하면 의미 없는 값(예: −338)이 나온다

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
        """매도 → close_position → save → notify → (전량이면) 저널 사후분석(+사후분석 알림).

        - 부분 매도(trim) 몫이나 그 잔량이 최소 주문 금액 미만이면 전량 매도로 바꾼다.
        - 전량 청산인데 정밀도 내림 후 수량이 0(팔 수 없는 잔량)이면 시세로 상각해 장부에서 지운다.
        - testnet/live: 주문 뒤 브로커가 예외를 내도 거래소 잔고 변화로 체결을 확인하면 그 체결로 기록한다.
        """
        pos = self.state.positions.get(symbol)
        if pos is None:
            return None
        fraction = max(0.0, min(1.0, float(fraction)))
        book = float(pos.base_amount)
        mark = self._mark_for(run, pos)
        note: str | None = None
        if fraction < 1.0 and mark is not None:
            min_o = self._min_order(symbol)
            if book * fraction * mark < min_o or book * (1.0 - fraction) * mark < min_o:
                note = f"부분 매도 {fraction:.0%} 또는 잔량이 최소 주문 {min_o:.2f} USDT 미만 → 전량 매도"
                log.info("%s %s", symbol, note)
                fraction = 1.0
        amount = book * fraction
        pre_base: float | None = None
        pre_quote: float | None = None
        if self._exchange_mode:
            pre_base = self._safe_free_base(symbol)
            if pre_base is not None:
                amount = min(amount, pre_base)
            else:
                log.warning("%s 가용 수량 조회 실패 — 장부 수량 사용", symbol)
            pre_quote = self._safe_free_quote()
        try:
            rounded = float(self.broker.amount_to_precision(symbol, amount)) if amount > 0 else 0.0
        except OrderValidationError:
            rounded = 0.0
        except Exception as exc:  # noqa: BLE001 - 거래소 오류는 거부 알림
            self._reject_sell(run, symbol, amount, reason, exc)
            return None
        if not (math.isfinite(rounded) and rounded > 0):
            if fraction >= 1.0 and self._rounded(symbol, book) <= 0:
                # 팔 수 없는 잔량(정밀도 미만) — 매 사이클 거부를 반복하며 슬롯을 점유하지 않게 장부에서 상각
                px = mark if mark is not None else float(pos.entry_price)
                fill = Fill(order_id=f"dust-writeoff-{run.now_ms}", symbol=symbol, side="sell", base_amount=book,
                            price=px, quote_amount=book * px, ts=run.now_ms, raw={"dust_writeoff": True})
                text = f"{symbol} 잔량 {book:.8g} 은 거래소 정밀도 미만이라 매도 불가 — 시세 {px:.6g} 로 상각"
                log.warning(text)
                return self._book_sell(run, symbol, fill, reason, requested_reason=reason,
                                       note=text, extra={"dust_written_off": book})
            self._reject_sell(run, symbol, amount, reason, OrderValidationError(f"{symbol} sell: 정밀도 적용 후 수량이 0"))
            return None
        amount = rounded
        try:
            risk.validate_order_request(OrderRequest(symbol=symbol, side="sell", base_amount=amount),
                                        tuple(self.settings.symbols), list(self.state.positions))
            fill = self.broker.market_sell(symbol, amount)
        except Exception as exc:  # noqa: BLE001 - 거래소/검증 오류는 거부 알림
            recovered = self._recover_sell(run, symbol, pre_base, pre_quote, amount, mark, exc)
            if recovered is None:
                self._reject_sell(run, symbol, amount, reason, exc)
                return None
            fill = recovered
        return self._book_sell(run, symbol, fill, reason, requested_reason=reason, note=note)

    def _reject_sell(self, run: _Run, symbol: str, amount: float, reason: str, exc: BaseException) -> None:
        text = f"{reason}: {type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}"
        run.errors.append(f"sell:{symbol}: {text}")
        log.error("매도 실패 %s %s", symbol, text)
        self._notify(self._event("order_rejected", run, symbol=symbol, side="sell", base_amount=amount, reason=text))

    def _recover_sell(self, run: _Run, symbol: str, pre_base: float | None, pre_quote: float | None,
                      amount: float, mark: float | None, exc: BaseException) -> Fill | None:
        """매도 주문 뒤 예외: 거래소 base 가 줄었으면 체결된 것으로 보고 잔고 변화로 체결을 복원한다."""
        if not self._exchange_mode or pre_base is None:
            return None
        post = self._safe_free_base(symbol)
        if post is None:
            return None
        sold = pre_base - post
        if not (sold > 0 and sold >= max(amount, 0.0) * RECOVERY_MIN_SHARE):
            return None
        price: float | None = None
        post_q = self._safe_free_quote()
        if pre_quote is not None and post_q is not None and post_q > pre_quote:
            p = (post_q - pre_quote) / sold
            if mark is None or 0.5 * mark <= p <= 2.0 * mark:
                price = p
        if price is None:
            pos = self.state.positions.get(symbol)
            price = mark if mark is not None else (float(pos.entry_price) if pos is not None else 0.0)
        if not _pos(price):
            return None
        text = (f"sell:{symbol}: 주문 응답 오류({type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}) — "
                f"거래소 잔고 변화로 체결 복원 {sold:.8g} @ {price:.6g} (수동 확인 권장)")
        run.errors.append(text)
        log.error(text)
        self._notify(self._event("error", run, symbol=symbol, reason=text, extra={"stage": f"sell:{symbol}"}))
        return Fill(order_id=f"recovered-sell-{run.now_ms}", symbol=symbol, side="sell", base_amount=sold,
                    price=float(price), quote_amount=sold * float(price), ts=run.now_ms,
                    raw={"recovered": True, "error": type(exc).__name__})

    def _book_sell(self, run: _Run, symbol: str, fill: Fill, reason: ExitReason, *, requested_reason: str,
                   note: str | None = None, extra: dict | None = None) -> ClosedTrade:
        """매도 체결을 장부에 반영 → 저장 → 알림 → 저널. 저장이 실패해도 체결 알림은 보내고 전파한다."""
        pos = self.state.positions[symbol]
        cycle = int(self.state.cycle)
        held = float(pos.base_amount)
        extra = dict(extra or {})
        sold = float(fill.base_amount)
        if sold < held * (1.0 - PARTIAL_FILL_TOLERANCE):
            residual = held - sold
            px = fill.price if _pos(fill.price) else self._mark_for(run, pos)
            if not self._sellable(symbol, residual, px):
                # 남는 잔량을 따로 팔 수 없다(정밀도·최소 금액 미만) → 전량 청산으로 기록(잔량 상각)
                log.warning("%s 매도 후 잔량 %.8g 은 팔 수 없는 크기 — 전량 청산으로 기록", symbol, residual)
                fill = fill.model_copy(update={"base_amount": held})
                extra["dust_written_off"] = residual
        trade = self.portfolio.close_position(symbol, fill, reason, cycle)
        partial = symbol in self.state.positions
        marks = dict(run.marks)
        if not partial:
            marks.pop(symbol, None)
        try:
            eq = self._equity(marks)
        except Exception:  # noqa: BLE001
            eq = None
        ev = self._event(
            "partial_exit" if partial else "exit", run, symbol=symbol, side="sell", base_amount=trade.base_amount,
            price=trade.exit_price, quote_amount=trade.quote_received,
            reason=trade.reason if not partial else requested_reason,
            pnl_quote=trade.pnl_quote, pnl_pct=trade.pnl_pct, equity=eq,
            extra={"order_id": fill.order_id, "requested_reason": requested_reason, "cycle": cycle,
                   **({"note": note} if note else {}), **extra})
        run.exits.append({"symbol": symbol, "reason": trade.reason, "requested_reason": requested_reason,
                          "pnl_quote": trade.pnl_quote, "pnl_pct": trade.pnl_pct, "partial": partial,
                          "price": trade.exit_price, "base_amount": trade.base_amount})
        run.actions[symbol] = {"action": "SELL", "executed": True,
                               "reason": f"{requested_reason}" + (" (부분)" if partial else "")}
        try:
            self._save()
        except StateSaveError:
            # notify 가 extra.state_saved == False 를 경고 줄로 렌더한다(SPEC §11)
            self._notify(ev.model_copy(update={"extra": {**ev.extra, "state_saved": False}}))
            raise
        self._notify(ev)
        # 사후분석(LLM 호출 — 응답이 늦으면 수십 초)은 이 사이클의 모든 매도가 끝난 뒤 _drain_reviews 가 한다:
        # 손절이 여러 개 걸린 사이클에서 앞 거래의 사후분석이 다음 손절 매도를 늦추지 않게.
        run.pending_reviews.append((trade, partial, fill.order_id))
        return trade

    def _drain_reviews(self, run: _Run) -> None:
        """미룬 사후분석·저널 기록을 순서대로 처리하고 '📝 사후분석' 후속 알림을 보낸다(거래별 실패는 서로 막지 않음)."""
        while run.pending_reviews:
            trade, partial, order_id = run.pending_reviews.pop(0)
            try:
                review_text = self._post_trade(run, trade, partial)
            except _FATAL:
                run.pending_reviews.insert(0, (trade, partial, order_id))   # 저널 기록 전(LLM 단계) — 중단 처리가 남긴다
                raise
            except Exception as exc:  # noqa: BLE001 - 한 거래의 사후분석 실패가 다른 거래 기록을 막지 않게
                run.errors.append(f"post_trade:{trade.symbol}: {type(exc).__name__}")
                log.error("사후분석 실패 %s type=%s", trade.symbol, type(exc).__name__)
                continue
            if review_text and not partial:
                # notify 가 extra.post_trade_review + follow_up 을 '📝 사후분석' 전용 메시지로 렌더한다
                self._notify(self._event(
                    "exit", run, symbol=trade.symbol, side="sell", base_amount=trade.base_amount,
                    price=trade.exit_price, pnl_quote=trade.pnl_quote, pnl_pct=trade.pnl_pct, reason=trade.reason,
                    extra={"post_trade_review": review_text, "follow_up": True, "order_id": order_id}))

    def _journal_pending_on_abort(self, run: _Run) -> None:
        """사이클이 예외로 끝날 때 남은 사후분석 대신 코드 요약으로 저널만 기록한다(LLM·알림 없음, 실패는 삼킨다)."""
        if not run.pending_reviews:
            return
        js = self.journal_state
        while run.pending_reviews:
            trade, partial, _ = run.pending_reviews.pop(0)
            try:
                rec = None if partial else {"code_summary": J.code_post_trade_summary(trade), "llm": False}
                reports.append_journal(self.journal_path, trade, rec)
                if not partial:
                    J.clear_symbol(js, trade.symbol)
            except Exception as exc:  # noqa: BLE001 - 원래 오류를 가리지 않는다
                log.error("중단 중 저널 기록 실패 %s type=%s", trade.symbol, type(exc).__name__)
        try:
            J.save_journal_state(js, self.data_dir)
        except Exception as exc:  # noqa: BLE001
            log.error("중단 중 저널 상태 저장 실패 type=%s", type(exc).__name__)

    def _post_trade(self, run: _Run, trade: ClosedTrade, partial: bool) -> str | None:
        """저널 사후분석·기록·심볼 정리. 전량 청산이면 텔레그램용 사후분석 문구(LLM 또는 코드 요약)를 돌려준다."""
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
        if partial:
            return None
        if review is not None and getattr(review, "telegram_text", None):
            return str(review.telegram_text)
        return (rec or {}).get("code_summary") if isinstance(rec, dict) else None

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
        # 백엔드(AnthropicBackend)에 사이클 기한을 알려 호출·재시도가 기한을 넘지 않게 한다. 매수·주문 몫은 남겨 둔다.
        set_deadline = getattr(self.backend, "set_deadline", None)
        if not callable(set_deadline):
            set_deadline = None
        if set_deadline is not None:
            try:
                set_deadline(max(0.0, self.deadline_s - self._elapsed(run) - LLM_DEADLINE_RESERVE_S))
            except Exception as exc:  # noqa: BLE001 - 기한 전달 실패는 단계 경계 검사로 충분
                log.warning("백엔드 기한 설정 실패 type=%s", type(exc).__name__)
                set_deadline = None
        try:
            return run_pipeline(scan, self.state, run.equity, self.runner, self.journal_state, run.now_ms,
                                news_store=self.news_store,
                                should_stop=lambda stage: self._deadline_passed(run, f"pipeline:{stage}"))
        except BudgetExceeded:
            # 파이프라인이 잡지 못한 경로(방어): 예산 초과로 처리
            run.budget_exceeded = True
            self._pause_for_budget(run, "LLM 예산 초과 (파이프라인)")
            raise
        finally:
            if set_deadline is not None:
                try:
                    set_deadline(None)
                except Exception:  # noqa: BLE001
                    pass

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
            if _finite(res.news.market_tone):
                self._last_market_tone = float(res.news.market_tone)
                self._last_market_tone_ts = run.now_ms
            else:
                self._clear_tone()
        elif any(a.startswith("news_sentiment:") for a in res.abstained):
            # 뉴스 채널 기권(피드 없음 등) → 묵은 톤을 다음 사이클 종합심리에 넣지 않는다
            self._clear_tone()
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
        downgraded = {str(x.get("symbol")): str(x.get("reason") or "코드 강등")
                      for x in (getattr(res, "head_downgrades", None) or [])}
        # SELL 을 먼저 처리한다(실현 손익이 뒤이은 BUY 의 전역 게이트 재검사에 반영되도록)
        ordered = sorted(res.head.decisions, key=lambda x: 0 if x.action == "SELL" else 1)
        for d in ordered:
            sym = d.symbol
            if d.action == "SELL":
                if sym not in self.state.positions:
                    run.actions[sym] = {"action": "HOLD", "executed": False, "reason": "SELL 대상 미보유"}
                    continue
                pc = pcs.get(sym)
                fraction = TRIM_FRACTION if self._trim_only(run, sym, pc) else 1.0
                log.info("총괄 SELL %s (비율 %.0f%%): %s", sym, fraction * 100, d.rationale[:120])
                self._stage(run, f"head_sell:{sym}", lambda s=sym, f=fraction: self._sell(run, s, f, "head_sell"))
            elif d.action == "BUY":
                if self._deadline_passed(run, f"buy:{sym}"):
                    run.actions[sym] = {"action": "HOLD", "executed": False, "reason": DEADLINE_REASON}
                    continue
                self._stage(run, f"buy:{sym}", lambda dd=d: self._buy(run, dd, ctxs.get(dd.symbol)))
            elif sym in downgraded and sym not in run.actions:
                # pipeline._sanitize_head 가 총괄 BUY 를 HOLD 로 강등(노출·슬롯·미승인) → G4 강등과 같은 알림(SPEC v2 §7)
                self._stage(run, f"downgrade:{sym}", lambda s=sym: self._downgrade(run, s, downgraded[s]))
            else:
                log.info("총괄 HOLD %s: %s", sym, d.rationale[:120])
                run.actions.setdefault(sym, {"action": "HOLD", "executed": False, "reason": d.rationale[:200]})

    @staticmethod
    def _exit_triggers(run: _Run, symbol: str, pc: Any) -> list[str]:
        """코드가 찾은(또는 청산을 요구하는) 청산 사유 — 하나라도 있으면 trim 이 아니라 전량 매도."""
        out: list[str] = []
        if pc is not None:
            if pc.red_line_hits:
                out.append("red_line")
            if pc.technical is not None and pc.technical.exit_flag:
                out.append("technical_exit_flag")
            if pc.risk is not None and pc.risk.action == "exit":
                out.append("risk_exit")
        if run.scan is not None and symbol in (run.scan.regime.name_alarm_symbols or []):
            out.append("name_alarm")
        return out

    def _trim_only(self, run: _Run, symbol: str, pc: Any) -> bool:
        """risk_manager 가 trim 이고 코드 쪽 청산 사유가 없을 때만 50% 매도(LLM 라벨은 청산을 풀지 못한다)."""
        if pc is None or pc.risk is None or pc.risk.action != "trim":
            return False
        triggers = self._exit_triggers(run, symbol, pc)
        if triggers:
            log.info("%s risk trim 이지만 청산 사유(%s) → 전량 매도", symbol, ", ".join(triggers))
            return False
        return True

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
                              tech_stop_pct=tech_stop,
                              # G3 가 잰 실효 비용(스캐너 스프레드·주문액/깊이 충격 포함)이 이 팩에 기록돼 있다
                              exec_cost_pct=getattr(run.scan.risk_packs.get(sym), "exec_cost_pct", None))
        ctx.gates.append(g4)
        if plan is None:
            self._downgrade(run, sym, "; ".join(g4.reasons) or "G4 HOLD")
            return
        if self._deadline_passed(run, f"order:{sym}"):
            run.actions[sym] = {"action": "HOLD", "executed": False, "reason": DEADLINE_REASON}
            return
        pre_base: float | None = None
        if self._exchange_mode:
            pre_base = self._safe_free_base(sym)
            if pre_base is not None and self._sellable(sym, pre_base, mark):
                # 장부 밖 보유(이전 체결 기록 실패·수동 매수 등)가 있으면 노출 한도를 넘지 않게 사지 않는다
                self._downgrade(run, sym, f"untracked_exchange_balance: 거래소에 장부 밖 {sym} {pre_base:.8g} 보유 — "
                                          "수동 확인 필요", quote=plan.quote_amount)
                return
        try:
            risk.validate_order_request(OrderRequest(symbol=sym, side="buy", quote_amount=plan.quote_amount),
                                        tuple(self.settings.symbols), list(self.state.positions))
            fill = self.broker.market_buy(sym, plan.quote_amount)
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}"
            recovered = self._recover_buy(run, sym, pre_base, free_quote, plan.quote_amount, mark, exc)
            if recovered is None:
                run.errors.append(f"buy:{sym}: {text}")
                run.actions[sym] = {"action": "HOLD", "executed": False, "reason": f"order_rejected: {text}"}
                self._notify(self._event("order_rejected", run, symbol=sym, side="buy",
                                         quote_amount=plan.quote_amount, price=mark, reason=text))
                return
            fill = recovered
        # 손절/익절을 실제 체결가 기준 같은 거리로 다시 놓는다(슬리피지로 손절거리가 넓어지지 않게)
        ref = mark if mark is not None else float(fill.price)
        sl_pct = max(0.0, 1.0 - plan.stop_loss_price / ref)
        tp_pct = max(0.0, plan.take_profit_price / ref - 1.0)
        entry = float(fill.price) if _pos(fill.price) else ref
        sl, tp = risk.clamp_stops(entry, entry * (1.0 - sl_pct), entry * (1.0 + tp_pct))
        thesis_json = json.dumps(d.thesis.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        pos = self.portfolio.open_position(fill, sl, tp, thesis_json, float(d.confidence), int(self.state.cycle))
        # 체결 → 상태 저장 → 알림. 저널 스냅샷(보조 상태)은 그 뒤에 따로 감싼다.
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
        ev = self._event(
            "entry", run, symbol=sym, side="buy", base_amount=fill.base_amount, price=fill.price,
            quote_amount=fill.quote_amount, stop_loss_price=sl, take_profit_price=tp, confidence=float(d.confidence),
            thesis=" | ".join(p for p in parts if p), equity=eq,
            extra={"bear_strongest_point": bear_point, "losing_side_strongest_point": d.losing_side_strongest_point,
                   "decisive_factor": d.decisive_factor, "size_pct": plan.size_pct, "order_id": fill.order_id,
                   "red_lines": [r.model_dump(mode="json") for r in d.thesis.red_lines],
                   **({"recovered": True} if fill.raw.get("recovered") else {})})
        try:
            self._save()
        except StateSaveError:
            self._notify(ev.model_copy(update={"extra": {**ev.extra, "state_saved": False}}))
            raise
        self._notify(ev)
        try:
            self._journal_entry(run, d, ctx, pos, fp)
        except Exception as exc:  # noqa: BLE001 - 저널은 보조 상태: 체결·저장·알림을 막지 않는다
            run.errors.append(f"journal_entry:{sym}: {type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}")
            log.error("진입 저널 스냅샷 실패 %s type=%s", sym, type(exc).__name__)
        self._save_journal(run)

    def _journal_entry(self, run: _Run, d: HeadDecision, ctx: CandidateContext, pos: Position, fp: Any) -> None:
        """저널: 구조화 논지 + 진입 판단 스냅샷."""
        res = run.result
        assert res is not None
        sym = d.symbol
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

    def _recover_buy(self, run: _Run, symbol: str, pre_base: float | None, pre_quote: float | None,
                     quote_amount: float, mark: float | None, exc: BaseException) -> Fill | None:
        """매수 주문 뒤 예외: 거래소 base 가 늘었으면 체결된 것으로 보고 잔고 변화로 체결을 복원한다.

        (복원하지 않으면 손절·익절·시간손절 없는 고아 보유가 생기고 같은 종목을 다시 살 수 있다.)
        """
        if not self._exchange_mode or pre_base is None:
            return None
        post = self._safe_free_base(symbol)
        if post is None:
            return None
        got = post - pre_base
        ref = mark
        if not (got > 0 and ref is not None and got * ref >= quote_amount * RECOVERY_MIN_SHARE):
            return None
        price: float = float(ref)
        post_q = self._safe_free_quote()
        if pre_quote is not None and post_q is not None and pre_quote > post_q:
            p = (pre_quote - post_q) / got
            if 0.5 * ref <= p <= 2.0 * ref:
                price = p
        text = (f"buy:{symbol}: 주문 응답 오류({type(exc).__name__}: {self._scrub(exc)[:MAX_ERROR_TEXT]}) — "
                f"거래소 잔고 변화로 체결 복원 {got:.8g} @ {price:.6g}, 손절·익절을 걸고 추적 (수동 확인 권장)")
        run.errors.append(text)
        log.error(text)
        self._notify(self._event("error", run, symbol=symbol, reason=text, extra={"stage": f"buy:{symbol}"}))
        return Fill(order_id=f"recovered-buy-{run.now_ms}", symbol=symbol, side="buy", base_amount=got, price=price,
                    quote_amount=got * price, ts=run.now_ms, raw={"recovered": True, "error": type(exc).__name__})

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
