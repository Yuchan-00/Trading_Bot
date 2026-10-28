"""CLI 진입점 (SPEC §15 ``main.py``, §22 ``main.py run``).

명령
- ``run [--once] [--cycle-minutes N] [--offline-fixture]``  사이클 루프(``cycle_minutes`` 경계 + 5초).
  ``--once`` 는 1회 후 종료. ``--offline-fixture`` 는 ccxt 시세 대신 결정적 합성 시세
  (``tradingbot.testing.FakeMarketData``)를 쓰고 뉴스 조회를 끈다 — 키·네트워크 없이 체험(paper 전용).
  합성 상태가 실제 paper 저널을 오염시키지 않도록 데이터는 ``<DATA_DIR>/offline_fixture/`` 에 따로 쓰고,
  백엔드는 mock 으로 강제하며 텔레그램 전송은 끈다(유료 API 호출·네트워크 0).
  ``AGENT_BACKEND=mock`` 이면 ``MOCK_SCENARIO``(conservative|approve) 로 Mock 응답 시나리오를 고른다.
- ``dashboard``                         로컬 대시보드(FastAPI + uvicorn, 읽기 전용).
- ``backtest [--days 90] [--symbols ...]`` 규칙 전용 기준선 백테스트(LLM 없음).
- ``status``                            상태 요약 출력(네트워크·LLM 없음, 파일만 읽음).
- ``stop --reason ...``                 ``control/STOP`` 작성 → 다음 사이클에 킬스위치.
- ``clear-stop``                        ``control/STOP`` 삭제 + ``control/CLEAR`` 작성 → 다음 사이클에 해제.

안전장치
- ``MODE=live`` 이면 ``run`` 시작 시 콘솔에 ``LIVE`` 를 직접 입력해야 진행한다(환경변수로 우회 불가).
  표준입력이 대화형 터미널(TTY)이 아니면(``echo LIVE | ... run`` 등 파이프/리다이렉트) 묻지 않고 거부한다.
- ``bot.lock``(PID) 으로 단일 인스턴스. 기록된 PID 가 살아 있지 않으면 낡은 잠금으로 보고 가져온다.
- SIGINT/SIGTERM → 진행 중 사이클을 마친 뒤 notify(shutdown) + ``notifier.flush()`` + 잠금 해제.
- 하트비트 notify 4시간마다.

출력은 ``out``/``err`` 스트림에 쓴다(테스트 주입). 로그는 ``logging.getLogger("tradingbot.main")``.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from tradingbot import cycle as C
from tradingbot.config import ConfigError, Settings, _read_env, load_settings
from tradingbot.models import BotState, TradeEvent

log = logging.getLogger("tradingbot.main")

__all__ = [
    "main", "build_parser", "BotLock", "LockHeldError", "Components", "build_components", "confirm_live",
    "run_loop", "next_run_ts", "install_signal_handlers", "status_text", "write_stop", "write_clear",
    "HEARTBEAT_S", "RUN_OFFSET_S", "LIVE_CONFIRM_WORD", "OFFLINE_FIXTURE_DIRNAME", "offline_fixture_settings",
]

HEARTBEAT_S = 4 * 3600.0
RUN_OFFSET_S = 5.0
LIVE_CONFIRM_WORD = "LIVE"
OFFLINE_FIXTURE_DIRNAME = "offline_fixture"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_LOCKED = 4


# ======================================================================= 잠금
class LockHeldError(RuntimeError):
    """다른 살아 있는 프로세스가 ``bot.lock`` 을 갖고 있다."""


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 존재하지만 다른 사용자
    except OSError:
        return False
    return True


class BotLock:
    """PID 파일 잠금(``<data_dir>/bot.lock``). 내용: ``{"pid","host","ts","started_ts"}`` JSON.

    - ``acquire()``: O_EXCL 생성. 이미 있으면 기록된 PID 가 살아 있는지 확인 — 살아 있으면 ``LockHeldError``,
      죽었거나 파일이 깨졌으면 낡은 잠금으로 보고 가져온다(WARNING).
    - ``refresh()``: 파일의 PID 가 내 것인지 확인하고 ``ts`` 를 갱신한다. 아니면 ``cycle.LockLostError``.
    - ``release()``: 내 잠금일 때만 삭제한다.
    """

    def __init__(self, path: Path, *, pid: int | None = None, pid_alive: Callable[[int], bool] | None = None,
                 clock_ms: Callable[[], int] | None = None) -> None:
        self.path = Path(path)
        self.pid = int(pid if pid is not None else os.getpid())
        self._alive = pid_alive or _pid_alive
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self.held = False
        self._started = 0

    def holder(self) -> dict | None:
        """현재 잠금 내용(없으면 None, 깨졌으면 ``{"pid": None, "corrupt": True}``)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"pid": None, "corrupt": True}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            pid = data.get("pid")
            data["pid"] = int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None
            return data
        except ValueError:
            s = raw.strip()
            return {"pid": int(s)} if s.isdigit() else {"pid": None, "corrupt": True}

    def _content(self) -> str:
        return json.dumps({"pid": self.pid, "host": socket.gethostname(), "ts": self._clock(),
                           "started_ts": self._started}, sort_keys=True, ensure_ascii=False)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._started = self._clock()
        for attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = self.holder()
                other = info.get("pid") if info else None
                if other == self.pid:
                    break                       # 이미 내 잠금(재진입)
                if other is not None and self._alive(other):
                    raise LockHeldError(f"다른 인스턴스가 실행 중입니다 (pid={other}, lock={self.path})")
                log.warning("낡은 잠금 파일을 가져옵니다 (기록 pid=%s, 살아 있지 않음): %s", other, self.path)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                if attempt == 1:
                    raise LockHeldError(f"잠금을 얻지 못했습니다: {self.path}")
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._content())
            break
        self.held = True
        self._write()
        log.info("잠금 획득 pid=%d path=%s", self.pid, self.path)

    def _write(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{self.pid}.tmp")
        tmp.write_text(self._content(), encoding="utf-8")
        os.replace(tmp, self.path)

    def refresh(self) -> None:
        info = self.holder()
        other = info.get("pid") if info else None
        if other != self.pid:
            self.held = False
            raise C.LockLostError(f"잠금을 잃었습니다 (파일 pid={other}, 내 pid={self.pid}): {self.path}")
        self._write()

    def release(self) -> None:
        if not self.held:
            return
        info = self.holder()
        if info is not None and info.get("pid") == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.held = False
        log.info("잠금 해제 pid=%d", self.pid)

    def __enter__(self) -> "BotLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ======================================================================= 구성 요소
@dataclass
class Components:
    settings: Settings
    market: Any
    broker: Any
    portfolio: Any
    backend: Any
    ledger: Any
    notifier: Any
    skills: Any
    cycle: Any


def build_components(settings: Settings, *, lock: BotLock | None = None, offline_fixture: bool = False,
                     mock_scenario: str | None = None) -> Components:
    """``load_settings()`` 결과로 실제 객체를 만든다(네트워크 호출은 첫 사이클에서 일어난다).

    ``offline_fixture=True`` → 합성 시세(``FakeMarketData``) + 뉴스 조회 없음(paper 전용, 네트워크 0).
    ``mock_scenario`` → ``AGENT_BACKEND=mock`` 일 때 ``tradingbot.testing`` 시나리오(None 이면 conservative).
    """
    from tradingbot import news
    from tradingbot.exchange import CcxtBroker, CcxtMarketData, PaperBroker
    from tradingbot.llm.backend import MockBackend
    from tradingbot.llm.cost import CostLedger
    from tradingbot.notify import Notifier, TelegramTransport
    from tradingbot.portfolio import Portfolio
    from tradingbot.scanner import Scanner
    from tradingbot.skills import SkillRegistry

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if offline_fixture:
        if settings.mode != "paper":
            raise ConfigError("--offline-fixture 는 MODE=paper 에서만 쓸 수 있습니다")
        from tradingbot.testing import FakeMarketData

        log.warning("오프라인 픽스처: 합성 시세(FakeMarketData) 사용 — 실제 시장 데이터가 아닙니다")
        market: Any = FakeMarketData()
        broker: Any = PaperBroker(market, settings.paper_start_balance)
    elif settings.mode == "paper":
        market = CcxtMarketData(settings)
        broker: Any = PaperBroker(market, settings.paper_start_balance)
    else:
        broker = CcxtBroker(settings)
        market = broker
    portfolio = Portfolio.load(settings, data_dir / C.STATE_FILENAME)
    ledger = CostLedger(data_dir / "cost_ledger.jsonl", settings.llm_daily_budget_usd, settings.llm_monthly_budget_usd)
    if settings.agent_backend == "mock":
        from tradingbot.testing import mock_backend_from_env

        backend: Any = mock_backend_from_env({"MOCK_SCENARIO": mock_scenario or ""})
    else:
        from tradingbot.llm.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend(settings, ledger)
    transport = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        transport = TelegramTransport(settings.telegram_bot_token, settings.telegram_chat_id)
    else:
        log.warning("텔레그램 토큰/채팅 ID 없음 — 알림은 로그로만 남깁니다")
    notifier = Notifier(transport, failed_log=data_dir / "logs" / "failed_notifications.jsonl")
    skills = SkillRegistry(C.PROJECT_ROOT / "skills")
    scanner = Scanner(settings, market, news_fetcher=None if offline_fixture else news.default_fetcher,
                      cache_dir=data_dir / C.CACHE_DIRNAME)
    cycle = C.Cycle(settings, market, broker, portfolio, backend, ledger, notifier, skills, scanner=scanner, lock=lock)
    return Components(settings, market, broker, portfolio, backend, ledger, notifier, skills, cycle)


# ======================================================================= 확인·스케줄·신호
def confirm_live(input_fn: Callable[[str], str], out: TextIO) -> bool:
    """콘솔에서 정확히 ``LIVE`` 를 입력해야 True. EOF/인터럽트/다른 값은 False."""
    out.write("경고: MODE=live — 실제 자금으로 주문합니다. 계속하려면 LIVE 를 입력하세요: ")
    out.flush()
    try:
        answer = input_fn("")
    except (EOFError, KeyboardInterrupt):
        return False
    return isinstance(answer, str) and answer.strip() == LIVE_CONFIRM_WORD


def _stdin_is_tty() -> bool:
    """현재 ``sys.stdin`` 이 대화형 터미널인지(파이프·리다이렉트·닫힘 → False)."""
    try:
        stdin = sys.stdin
        return bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def offline_fixture_settings(settings: Settings) -> Settings:
    """``--offline-fixture`` 용 격리 설정: ``<data_dir>/offline_fixture`` + mock 백엔드 + 텔레그램 끔.

    합성 시세로 연 포지션·사이클 카운터·OI/펀딩 이력이 실제 paper ``state.json``/``cache`` 에 섞이지 않게 하고,
    "키·네트워크 없음" 약속대로 유료 LLM 호출과 텔레그램 전송을 막는다. 이미 격리된 설정이면 그대로 둔다.
    """
    data_dir = Path(settings.data_dir)
    if data_dir.name != OFFLINE_FIXTURE_DIRNAME:
        data_dir = data_dir / OFFLINE_FIXTURE_DIRNAME
    return dataclasses.replace(settings, data_dir=data_dir, agent_backend="mock",
                               telegram_bot_token=None, telegram_chat_id=None)


def next_run_ts(now_s: float, cycle_minutes: int, offset_s: float = RUN_OFFSET_S) -> float:
    """다음 실행 시각(epoch 초): ``cycle_minutes`` 경계 + ``offset_s``. 지금이 경계+offset 이전이면 이번 경계."""
    period = max(1, int(cycle_minutes)) * 60.0
    base = (now_s // period) * period
    cand = base + offset_s
    if cand <= now_s:
        cand += period
    return cand


def install_signal_handlers(stop_event: threading.Event) -> dict:
    """SIGINT/SIGTERM → ``stop_event.set()``. 이전 핸들러를 돌려준다(메인 스레드가 아니면 빈 dict)."""
    prev: dict = {}

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        log.warning("종료 신호 수신(%s) — 진행 중 사이클을 마치고 종료합니다", signum)
        stop_event.set()

    if threading.current_thread() is not threading.main_thread():
        return prev
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            prev[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - 플랫폼 제약
            pass
    return prev


def _restore_signals(prev: Mapping) -> None:
    for sig, h in prev.items():
        try:
            signal.signal(sig, h)
        except (ValueError, OSError, TypeError):  # pragma: no cover
            pass


def _event(settings: Settings, kind: str, **kw: Any) -> TradeEvent:
    return TradeEvent(kind=kind, ts=int(time.time() * 1000), mode=settings.mode, **kw)  # type: ignore[arg-type]


def _safe_equity(comp: Components) -> float | None:
    try:
        if comp.settings.mode == "paper":
            return float(comp.portfolio.equity({}))
        return float(comp.portfolio.equity({}, quote_balance=float(comp.broker.free_quote())))
    except Exception:  # noqa: BLE001
        return None


def run_loop(comp: Components, *, once: bool, stop_event: threading.Event | None = None,
             clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
             heartbeat_s: float = HEARTBEAT_S, max_cycles: int | None = None,
             wait: Callable[[float], bool] | None = None) -> int:
    """사이클 루프. 치명 오류(손상 상태·저장 실패·잠금 상실·설정 오류)면 EXIT_ERROR.

    ``wait(seconds) -> bool`` 은 다음 실행까지 대기하고 종료 요청이면 True(기본 ``stop_event.wait``).
    ``max_cycles`` 는 시도 횟수(실패 포함) 상한(테스트용). 종료 시 항상 notify(shutdown) + ``notifier.flush()``.
    """
    stop = stop_event or threading.Event()
    wait_fn = wait or stop.wait
    settings = comp.settings
    notifier = comp.notifier
    code = EXIT_OK
    reason = "정상 종료"
    last_hb = monotonic()
    n = 0
    try:
        while not stop.is_set():
            n += 1
            try:
                summary = comp.cycle.run_once()
                log.info("사이클 %s 완료 (오류 %d)", summary.cycle, len(summary.errors))
            except (C.LockLostError, C.StateSaveError, ConfigError) as exc:
                code, reason = EXIT_ERROR, f"치명 오류로 중단: {type(exc).__name__}"
                log.error("%s", reason)
                notifier.notify(_event(settings, "error", reason=reason, extra={"stage": "cycle"}))
                break
            except Exception as exc:  # noqa: BLE001
                from tradingbot.portfolio import CorruptStateError

                if isinstance(exc, (CorruptStateError, OSError)):
                    code, reason = EXIT_ERROR, f"치명 오류로 중단: {type(exc).__name__}"
                    log.error("%s", reason)
                    notifier.notify(_event(settings, "error", reason=reason, extra={"stage": "cycle"}))
                    break
                # 예상 밖 예외: 기록하고 다음 사이클 시도
                log.error("사이클 예외(계속) type=%s", type(exc).__name__)
                notifier.notify(_event(settings, "error", reason=f"사이클 예외: {type(exc).__name__}",
                                       extra={"stage": "cycle"}))
            if once or (max_cycles is not None and n >= max_cycles):
                break
            if monotonic() - last_hb >= heartbeat_s:
                last_hb = monotonic()
                st = comp.portfolio.state
                notifier.notify(_event(settings, "daily_summary", equity=_safe_equity(comp),
                                       reason=f"하트비트: 정상 동작 중 (cycle {st.cycle}, 보유 {len(st.positions)}개, "
                                              f"킬스위치 {'ON' if st.kill_switch else 'OFF'})",
                                       extra={"heartbeat": True}))
            wait_s = max(0.0, next_run_ts(clock(), settings.cycle_minutes) - clock())
            if wait_fn(wait_s) or stop.is_set():
                reason = "종료 신호(SIGINT/SIGTERM)"
                break
        else:
            reason = "종료 신호(SIGINT/SIGTERM)"
    except KeyboardInterrupt:
        reason = "KeyboardInterrupt"
    finally:
        try:
            notifier.notify(_event(settings, "shutdown", reason=reason, equity=_safe_equity(comp)))
        finally:
            notifier.flush()
    return code


# ======================================================================= status / stop / clear
def _read_state(data_dir: Path) -> tuple[BotState | None, str | None]:
    p = data_dir / C.STATE_FILENAME
    if not p.is_file():
        return None, "state.json 없음 (아직 실행 전)"
    try:
        return BotState.model_validate(json.loads(p.read_text(encoding="utf-8"))), None
    except (OSError, ValueError) as exc:
        return None, f"state.json 읽기/검증 실패: {type(exc).__name__}"


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def status_text(settings: Settings) -> str:
    """상태 요약(네트워크 없음). 시세는 포지션의 last_mark 를 쓴다."""
    data_dir = Path(settings.data_dir)
    lines = [f"모드: {settings.mode}  데이터: {data_dir}  종목: {', '.join(settings.symbols)}"]
    state, err = _read_state(data_dir)
    if state is None:
        lines.append(err or "상태 없음")
    else:
        pos_val = sum(p.base_amount * (p.last_mark or p.entry_price) for p in state.positions.values())
        lines.append(f"사이클: {state.cycle}  마지막 실행: {_fmt_ts(state.last_cycle_ts)}")
        if state.mode == "paper":
            eq = state.paper_quote_balance + pos_val
            lines.append(f"자본(추정): {eq:,.2f} USDT  현금 {state.paper_quote_balance:,.2f}  최고 {state.peak_equity:,.2f}")
        else:
            lines.append(f"보유 평가액: {pos_val:,.2f} USDT  최고자본 {state.peak_equity:,.2f} (현금은 거래소 조회 필요)")
        d = state.daily
        lines.append(f"당일({d.date}): 실현손익 {d.realized_pnl:+,.2f}  거래 {d.trades} (승 {d.wins}/패 {d.losses})  "
                     f"연속손실 {d.consecutive_losses}  LLM ${d.llm_cost_usd:.2f}")
        lines.append(f"킬스위치: {'ON — ' + (state.kill_reason or '') if state.kill_switch else 'OFF'}")
        now_ms = int(time.time() * 1000)
        if state.llm_paused_until_ts and now_ms < state.llm_paused_until_ts:
            lines.append(f"LLM 일시정지: {_fmt_ts(state.llm_paused_until_ts)} 까지 ({state.llm_pause_reason})")
        else:
            lines.append("LLM 일시정지: 없음")
        lines.append(f"포지션 {len(state.positions)}개:")
        for sym, p in sorted(state.positions.items()):
            mark = p.last_mark or p.entry_price
            pnl = (mark / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
            lines.append(f"  {sym}: {p.base_amount:.8g} @ {p.entry_price:.6g} → {mark:.6g} ({pnl:+.2f}%)  "
                         f"손절 {p.stop_loss_price:.6g} / 익절 {p.take_profit_price:.6g}  진입 사이클 {p.opened_cycle}")
        lines.append(f"청산 거래 누적: {len(state.closed_trades)}건")
    stop = C.read_control_file(data_dir / C.CONTROL_DIRNAME / C.STOP_FILENAME)
    clear = C.read_control_file(data_dir / C.CONTROL_DIRNAME / C.CLEAR_FILENAME)
    if stop is not None:
        lines.append(f"STOP 대기: {stop.get('reason') or '-'}")
    if clear is not None:
        lines.append("CLEAR 대기: 다음 사이클에 킬스위치 해제")
    lock = BotLock(data_dir / C.LOCK_FILENAME).holder()
    if lock is None:
        lines.append("실행 중 인스턴스: 없음")
    else:
        pid = lock.get("pid")
        alive = _pid_alive(pid) if isinstance(pid, int) else False
        lines.append(f"잠금: pid={pid} ({'실행 중' if alive else '낡음'}) 갱신 {_fmt_ts(lock.get('ts'))}")
    ledger_p = data_dir / "cost_ledger.jsonl"
    if ledger_p.is_file():
        try:
            from tradingbot.llm.cost import CostLedger

            led = CostLedger(ledger_p, settings.llm_daily_budget_usd, settings.llm_monthly_budget_usd)
            lines.append(f"LLM 비용: 오늘 ${led.spent_today():.4f} / ${settings.llm_daily_budget_usd:.2f}, "
                         f"이달 ${led.spent_month():.4f} / ${settings.llm_monthly_budget_usd:.2f}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"LLM 비용: 원장 읽기 실패 ({type(exc).__name__})")
    return "\n".join(lines) + "\n"


def _write_control(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"reason": reason, "ts": int(time.time() * 1000)}, sort_keys=True, ensure_ascii=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_stop(data_dir: Path, reason: str) -> Path:
    """``control/STOP`` 작성(+ 대기 중 CLEAR 삭제). 다음 사이클에 킬스위치."""
    control = Path(data_dir) / C.CONTROL_DIRNAME
    stop = control / C.STOP_FILENAME
    _write_control(stop, reason or "cli")
    (control / C.CLEAR_FILENAME).unlink(missing_ok=True)
    return stop


def write_clear(data_dir: Path, reason: str = "cli") -> Path:
    """``control/STOP`` 삭제 + ``control/CLEAR`` 작성. 다음 사이클에 킬스위치 해제."""
    control = Path(data_dir) / C.CONTROL_DIRNAME
    (control / C.STOP_FILENAME).unlink(missing_ok=True)
    clear = control / C.CLEAR_FILENAME
    _write_control(clear, reason)
    return clear


# ======================================================================= CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradingbot", description="Claude 멀티에이전트 Binance 현물 봇")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="사이클 루프 실행")
    r.add_argument("--once", action="store_true", help="한 사이클만 실행하고 종료")
    r.add_argument("--cycle-minutes", type=int, default=None, help="사이클 주기(분) 덮어쓰기")
    r.add_argument("--offline-fixture", action="store_true",
                   help="합성 시세(키·네트워크 없음, paper 전용)로 실행 — 체험/점검용")
    sub.add_parser("dashboard", help="로컬 대시보드 실행")
    b = sub.add_parser("backtest", help="규칙 전용 기준선 백테스트")
    b.add_argument("--days", type=int, default=90)
    b.add_argument("--symbols", nargs="+", default=None, help="종목(BTC/USDT ...). 쉼표 구분도 허용")
    sub.add_parser("status", help="상태 요약(네트워크 없음)")
    s = sub.add_parser("stop", help="킬스위치 요청(control/STOP)")
    s.add_argument("--reason", required=True)
    sub.add_parser("clear-stop", help="킬스위치 해제 요청(control/CLEAR)")
    return p


def _positive_int(v: int | None, name: str) -> None:
    if v is not None and v < 1:
        raise ConfigError(f"{name} 는 1 이상이어야 합니다 (현재 {v})")


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         input_fn: Callable[[str], str] | None = None, out: TextIO | None = None, err: TextIO | None = None,
         components_factory: Callable[..., Components] | None = None,
         stop_event: threading.Event | None = None, pid: int | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        settings = load_settings(env)
        if args.command == "run":
            _positive_int(args.cycle_minutes, "--cycle-minutes")
            if args.cycle_minutes is not None:
                settings = dataclasses.replace(settings, cycle_minutes=int(args.cycle_minutes))
        if args.command == "backtest":
            _positive_int(args.days, "--days")
    except ConfigError as exc:
        if args.command in ("stop", "clear-stop"):
            # 제어 파일 쓰기는 DATA_DIR 만 있으면 된다 — 백엔드/키 설정이 깨져 있어도 킬스위치는 켜고 끌 수 있어야 한다
            data_dir = _control_data_dir(env)
            err.write(f"설정 경고(무시하고 진행): {exc}\n")
            return _cmd_control(args, data_dir, out)
        err.write(f"설정 오류: {exc}\n")
        return EXIT_USAGE

    if args.command in ("stop", "clear-stop"):
        return _cmd_control(args, Path(settings.data_dir), out)
    if args.command == "status":
        out.write(status_text(settings))
        return EXIT_OK
    if args.command == "dashboard":
        return _cmd_dashboard(settings, out)
    if args.command == "backtest":
        return _cmd_backtest(settings, args, out, err)
    # load_settings 와 같은 출처(os.environ 위에 .env)에서 읽는다 — .env 의 MOCK_SCENARIO 도 반영.
    src_env: Mapping[str, str] = _read_env(None) if env is None else env
    mock_scenario = (src_env.get("MOCK_SCENARIO") or "").strip() or None
    # input_fn 미주입(실제 CLI) → 기본 input() + live 확인은 TTY 에서만(파이프로 'LIVE' 주입 차단).
    require_tty = input_fn is None
    return _cmd_run(settings, args, input_fn=input_fn or input, out=out, err=err,
                    components_factory=components_factory, stop_event=stop_event, pid=pid,
                    mock_scenario=mock_scenario, require_tty=require_tty)


def _control_data_dir(env: Mapping[str, str] | None) -> Path:
    """설정 검증 없이 DATA_DIR 만 푼다(load_settings 와 같은 출처: os.environ 위에 .env, 기본 '.')."""
    src: Mapping[str, str] = _read_env(None) if env is None else env
    return Path((src.get("DATA_DIR") or "").strip() or ".").expanduser()


def _cmd_control(args: argparse.Namespace, data_dir: Path, out: TextIO) -> int:
    """``stop`` / ``clear-stop``: control 파일만 쓴다."""
    if args.command == "stop":
        path = write_stop(data_dir, args.reason)
        out.write(f"STOP 요청 기록: {path} — 다음 사이클에 킬스위치가 켜집니다\n")
        return EXIT_OK
    path = write_clear(data_dir)
    out.write(f"CLEAR 요청 기록: {path} — 다음 사이클에 킬스위치가 해제됩니다\n")
    return EXIT_OK


def _cmd_run(settings: Settings, args: argparse.Namespace, *, input_fn: Callable[[str], str], out: TextIO,
             err: TextIO, components_factory: Callable[..., Components] | None,
             stop_event: threading.Event | None, pid: int | None, mock_scenario: str | None = None,
             require_tty: bool = False) -> int:
    offline = bool(getattr(args, "offline_fixture", False))
    if offline and settings.mode != "paper":
        err.write("--offline-fixture 는 MODE=paper 에서만 쓸 수 있습니다\n")
        return EXIT_USAGE
    if offline:
        settings = offline_fixture_settings(settings)
        out.write(f"오프라인 픽스처: 합성 시세 · mock 백엔드 · 텔레그램 끔 — 데이터는 {settings.data_dir} "
                  "(실제 paper 상태와 분리)\n")
        log.warning("오프라인 픽스처: 격리 데이터 디렉터리 %s 사용(실제 paper 상태 미변경)", settings.data_dir)
    if settings.mode == "live":
        if require_tty and not _stdin_is_tty():
            err.write("MODE=live 는 대화형 콘솔(TTY)에서 직접 LIVE 를 입력해야 합니다 — "
                      "파이프/리다이렉트된 표준입력은 거부합니다. 실행하지 않습니다\n")
            log.warning("live 확인: 표준입력이 TTY 가 아님 — 실행 거부")
            return EXIT_REFUSED
        if not confirm_live(input_fn, out):
            err.write("\nLIVE 확인 실패 — 실행하지 않습니다\n")
            log.warning("live 확인 입력 불일치 — 실행 거부")
            return EXIT_REFUSED
        out.write("\n")
    data_dir = Path(settings.data_dir)
    lock = BotLock(data_dir / C.LOCK_FILENAME, pid=pid)
    try:
        lock.acquire()
    except LockHeldError as exc:
        err.write(f"{exc}\n")
        return EXIT_LOCKED
    stop = stop_event or threading.Event()
    prev = install_signal_handlers(stop)
    try:
        factory = components_factory or build_components
        try:
            extra: dict[str, Any] = {}
            if offline:
                extra["offline_fixture"] = True
            if mock_scenario is not None:
                extra["mock_scenario"] = mock_scenario
            comp = factory(settings, lock=lock, **extra)
        except (ConfigError, OSError, ValueError) as exc:
            err.write(f"초기화 실패: {type(exc).__name__}: {exc}\n")
            return EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 - CorruptStateError 등
            err.write(f"초기화 실패: {type(exc).__name__}\n")
            return EXIT_ERROR
        log.info("시작 설정: %s", settings.redacted())
        st = comp.portfolio.state
        comp.notifier.notify(_event(settings, "startup", equity=_safe_equity(comp),
                                    reason=("1회 실행" if args.once else "루프 시작")
                                    + (" — 킬스위치 ON" if st.kill_switch else ""),
                                    extra={"symbols": list(settings.symbols), "cycle_minutes": settings.cycle_minutes,
                                           "backend": settings.agent_backend,
                                           **({"offline_fixture": True} if offline else {})}))
        return run_loop(comp, once=bool(args.once), stop_event=stop)
    finally:
        _restore_signals(prev)
        lock.release()


def _cmd_dashboard(settings: Settings, out: TextIO) -> int:  # pragma: no cover - 서버 실행
    import uvicorn

    from tradingbot.dashboard.app import create_app

    out.write(f"대시보드: http://{settings.dashboard_host}:{settings.dashboard_port}\n")
    uvicorn.run(create_app(settings, Path(settings.data_dir)), host=settings.dashboard_host,
                port=settings.dashboard_port, log_level="warning")
    return EXIT_OK


def _parse_symbols(raw: Sequence[str] | None, default: Sequence[str]) -> list[str]:
    if not raw:
        return list(default)
    out: list[str] = []
    for item in raw:
        for s in str(item).split(","):
            s = s.strip().upper()
            if s and s not in out:
                out.append(s)
    return out


def _cmd_backtest(settings: Settings, args: argparse.Namespace, out: TextIO, err: TextIO,
                  market: Any | None = None) -> int:
    from tradingbot import backtest

    symbols = _parse_symbols(args.symbols, settings.symbols)
    data_dir = Path(settings.data_dir)
    cfg = backtest.BacktestConfig(symbols=symbols, days=int(args.days), start_balance=settings.paper_start_balance,
                                  cache_dir=data_dir / "backtest_cache", out_dir=data_dir / "backtest_results")
    if market is None:  # pragma: no cover - 네트워크
        from tradingbot.exchange import CcxtMarketData

        market = CcxtMarketData(settings)
    try:
        result = backtest.run(cfg, market=market)
    except Exception as exc:  # noqa: BLE001
        err.write(f"백테스트 실패: {type(exc).__name__}: {exc}\n")
        return EXIT_ERROR
    verdict = getattr(result, "verdict", "?")
    out.write(f"백테스트 완료: {', '.join(symbols)} {args.days}일 — {verdict}\n")
    if getattr(result, "json_path", None):
        out.write(f"결과: {result.json_path}\n")
    reasons = getattr(result, "reasons", None)
    if reasons:
        out.write("사유: " + "; ".join(str(r) for r in reasons) + "\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
