"""Windows 운영 위험 수정 테스트 (docs/workflows/tradingbot-windows-audit.js 점검 결과).

리눅스에서는 Windows 동작을 흉내 낸다(``PermissionError(13, ..., None, 32)`` 로 공유 위반, 다른 스레드의
``signal.raise_signal`` 로 'Ctrl+C 가 대기를 깨지 못함', 가짜 kernel32). ``os.name == "nt"`` 에서만 도는 테스트는
CI 의 windows-latest 에서 실제 공유 위반·실제 API 를 확인한다.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import signal
import socket
import threading
import time
from pathlib import Path

import ccxt
import pytest

from tradingbot import config as CFG
from tradingbot import cycle as C
from tradingbot import fsutil
from tradingbot import main as M
from tradingbot.config import ConfigError, load_settings, parse_data_dir, read_env
from tradingbot.exchange import CcxtBroker, _CcxtBase, _install_step_safe_throttle
from tradingbot.llm.cost import CostLedger, pending_ledger_path
from tradingbot.models import TradeEvent
from tradingbot.notify import Notifier

from test_exchange_ccxt import FakeExchange, make_settings
from test_main import FakeCycle, FakeNotifier, Factory, call, env_for, fake_components  # noqa: F401

WINDOWS = os.name == "nt"


def win_error(winerror: int, path: object = None) -> PermissionError:
    """``winerror`` 를 실은 PermissionError. 생성자의 4번째 인자는 Windows 에서만 속성이 되므로 직접 붙인다."""
    exc = PermissionError(13, "The process cannot access the file because it is being used by another process",
                          str(path) if path is not None else None)
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


def sharing_error(path: object = None) -> PermissionError:
    """Windows 의 ERROR_SHARING_VIOLATION(32) — 다른 프로세스가 파일을 열고 있음."""
    return win_error(32, path)


# ======================================================================= fsutil
def test_transient_sharing_error_classification(monkeypatch):
    assert fsutil.is_transient_sharing_error(sharing_error())
    assert fsutil.is_transient_sharing_error(win_error(5))
    assert not fsutil.is_transient_sharing_error(win_error(2))
    assert not fsutil.is_transient_sharing_error(FileNotFoundError(2, "x"))
    assert not fsutil.is_transient_sharing_error(OSError(28, "No space left on device"))
    # winerror 없는 PermissionError: Windows 의 open() 은 errno 만 싣는다 → errno_only 판정 + Windows 에서만 공유 위반.
    # os.replace/os.unlink 는 Windows 에서 항상 winerror 를 싣는다 → 기본 판정에서는 공유 위반이 아니다(테스트 대역 등).
    plain = PermissionError(13, "denied")
    monkeypatch.setattr(fsutil.os, "name", "posix")
    assert not fsutil.is_transient_sharing_error(plain, errno_only=True)
    monkeypatch.setattr(fsutil.os, "name", "nt")
    assert fsutil.is_transient_sharing_error(plain, errno_only=True)
    assert not fsutil.is_transient_sharing_error(plain)


def test_retry_delays_respect_budget():
    ds = list(fsutil.retry_delays(3.0))
    assert ds[0] == pytest.approx(0.05) and max(ds) <= 1.0
    assert sum(ds) == pytest.approx(3.0)
    assert list(fsutil.retry_delays(0)) == []


def test_replace_retries_sharing_violation_then_succeeds(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    real = os.replace
    fails = [sharing_error(dst), sharing_error(dst)]

    def flaky(a, b):
        if fails:
            raise fails.pop(0)
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    sleeps: list[float] = []
    fsutil.replace(src, dst, sleep=sleeps.append)
    assert dst.read_text(encoding="utf-8") == "new" and not src.exists()
    assert sleeps == [pytest.approx(0.05), pytest.approx(0.1)]


def test_replace_does_not_retry_other_errors(tmp_path, monkeypatch):
    def broken(a, b):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken)
    sleeps: list[float] = []
    with pytest.raises(OSError) as ei:
        fsutil.replace(tmp_path / "a", tmp_path / "b", sleep=sleeps.append)
    assert ei.value.errno == 28 and sleeps == []


def test_replace_gives_up_after_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(sharing_error(b)))
    sleeps: list[float] = []
    with pytest.raises(PermissionError):
        fsutil.replace(tmp_path / "a", tmp_path / "b", budget_s=1.0, sleep=sleeps.append)
    assert sum(sleeps) == pytest.approx(1.0)


def test_unlink_and_append_retry(tmp_path, monkeypatch):
    p = tmp_path / "x.jsonl"
    p.write_bytes(b"a\n")
    real_unlink = Path.unlink
    state = {"n": 1}

    def flaky_unlink(self, missing_ok=False):
        if state["n"]:
            state["n"] -= 1
            raise sharing_error(self)
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    fsutil.unlink(p, sleep=lambda s: None)
    assert not p.exists()
    fsutil.unlink(p, sleep=lambda s: None)              # 없는 파일은 조용히
    monkeypatch.undo()
    fsutil.append_bytes(p, "첫 줄\n".encode("utf-8"))
    fsutil.append_bytes(p, b"second\n")
    assert p.read_bytes() == "첫 줄\nsecond\n".encode("utf-8")      # LF 그대로(OS 무관)


@pytest.mark.skipif(not WINDOWS, reason="실제 Windows 공유 위반(열린 핸들)은 Windows 에서만 재현된다")
def test_windows_real_open_handle_blocks_replace_until_closed(tmp_path):
    dst, src = tmp_path / "state.json", tmp_path / "state.json.tmp"
    dst.write_text("old", encoding="utf-8")
    src.write_text("new", encoding="utf-8")
    fh = open(dst, "rb")                              # CPython open(): FILE_SHARE_DELETE 없음
    with pytest.raises(PermissionError):
        os.replace(src, dst)                          # 결함 재현: 다른 핸들이 열려 있으면 교체 거부
    t = threading.Timer(0.3, fh.close)
    t.start()
    try:
        fsutil.replace(src, dst)                      # 수정: 핸들이 닫힐 때까지 재시도
    finally:
        t.join()
        fh.close()
    assert dst.read_text(encoding="utf-8") == "new"


# ======================================================================= 로그 펌프(콘솔 선택 모드)
class BlockingStream(io.StringIO):
    """WriteConsoleW 가 콘솔 선택 중에 막히는 것처럼 ``release`` 전까지 write 가 막힌다."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def write(self, s: str) -> int:
        self.entered.set()
        self.release.wait(10)
        return super().write(s)


def test_log_pump_keeps_trading_thread_running_while_console_is_blocked():
    stream = BlockingStream()
    console = logging.StreamHandler(stream)
    console.setFormatter(logging.Formatter(M.LOG_FORMAT))
    pump = M._LogPump([console])
    pump.start()
    lg = logging.getLogger("tradingbot.test_pump")
    lg.propagate = False
    qh = logging.handlers.QueueHandler(pump.queue)
    lg.addHandler(qh)
    lg.setLevel(logging.INFO)
    try:
        t0 = time.monotonic()
        lg.info("스캔 완료 %d", 1)
        assert stream.entered.wait(5)                 # 펌프 스레드는 콘솔 쓰기에서 막혔다
        for i in range(50):
            lg.info("코드 청산 %d", i)                  # 거래 스레드는 막히지 않는다
        assert time.monotonic() - t0 < 2.0
        stream.release.set()
        pump.stop(timeout_s=5)
        text = stream.getvalue()
        assert "스캔 완료 1" in text and "코드 청산 49" in text and " INFO tradingbot.test_pump: " in text
    finally:
        stream.release.set()
        lg.removeHandler(qh)


def test_configure_logging_routes_root_through_pump_and_restores(monkeypatch):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.raiseExceptions, M._LOG_PUMP)
    stream = io.StringIO()
    try:
        pump = M.configure_logging(stream=stream)
        assert any(isinstance(h, logging.handlers.QueueHandler) for h in root.handlers)
        logging.getLogger("tradingbot.x").warning("경고 %s", "한글🟢")
        pump.stop(timeout_s=5)
        assert "WARNING tradingbot.x: 경고 한글🟢" in stream.getvalue()
    finally:
        logging.captureWarnings(False)
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
        logging.raiseExceptions = saved[2]
        M._LOG_PUMP = saved[3]


def test_attach_log_file_writes_utf8_with_single_bom(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_LOG_PUMP", None)
    p = tmp_path / "logs" / "bot.log"
    lg = logging.getLogger("tradingbot.test_logfile")
    lg.setLevel(logging.INFO)
    for i in range(2):
        h = M.attach_log_file(p)
        assert h is not None
        lg.info("사이클 %d 완료 🟢", i)
        M.detach_log_file(h)
    data = p.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and data.count(b"\xef\xbb\xbf") == 1
    text = data.decode("utf-8-sig")
    assert "사이클 0 완료 🟢" in text and "사이클 1 완료 🟢" in text
    assert all(not isinstance(x, logging.FileHandler) or x.baseFilename != str(p) for x in logging.getLogger().handlers)


def test_attach_log_file_failure_is_only_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(M, "_LOG_PUMP", None)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tradingbot.main"):
        assert M.attach_log_file(blocker / "bot.log") is None      # 부모가 파일 → 열 수 없음
    assert "로그 파일을 열지 못함" in caplog.text


def test_run_attaches_log_file_from_settings_and_detaches(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(M, "_LOG_PUMP", None)
    caplog.set_level(logging.INFO)              # cli() 는 루트를 INFO 로 둔다
    f = Factory()
    code, _, err = call(["run", "--once"], tmp_path, factory=f, env=env_for(tmp_path, LOG_FILE="logs/bot.log"))
    assert code == M.EXIT_OK, err
    p = tmp_path / "data" / "logs" / "bot.log"
    text = p.read_text(encoding="utf-8-sig")
    assert "데이터 폴더:" in text and "잠금 해제" in text
    assert not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(p)
                   for h in logging.getLogger().handlers)


@contextlib.contextmanager
def cli_logging(stream):
    """``cli()`` 처럼 루트를 콘솔 펌프로 돌리고, 끝나면 원래 핸들러(pytest 의 것 포함)로 되돌린다."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.raiseExceptions, M._LOG_PUMP)
    pump = M.configure_logging(stream=stream)
    try:
        yield pump
    finally:
        if isinstance(stream, BlockingStream):
            stream.release.set()
        pump.stop(timeout_s=5)
        logging.captureWarnings(False)
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])
        logging.raiseExceptions = saved[2]
        M._LOG_PUMP = saved[3]


def _eventually(pred, timeout_s: float = 5.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_log_file_keeps_writing_while_console_is_blocked(tmp_path):
    """콘솔이 선택 모드로 막혀도 LOG_FILE 에는 계속 쓴다(파일은 자기 큐·스레드)."""
    stream = BlockingStream()
    p = tmp_path / "bot.log"
    with cli_logging(stream):
        h = M.attach_log_file(p)
        lg = logging.getLogger("tradingbot.cycle")
        lg.info("첫 기록")
        assert stream.entered.wait(5)                         # 콘솔 쓰기는 막혔다
        for i in range(5):
            lg.warning("손절 매도 %d", i)
        assert _eventually(lambda: "손절 매도 4" in p.read_text(encoding="utf-8-sig"))
        feed = h._tb_pump[1]
        M.detach_log_file(h)
        root_handlers = logging.getLogger().handlers
        assert feed not in root_handlers and h not in root_handlers                # 파일 큐 핸들러를 뗐다
        assert sum(isinstance(x, logging.handlers.QueueHandler) for x in root_handlers) == 1   # 콘솔 것만 남음
        assert h.stream is None                                                   # 파일을 닫았다


def test_detach_log_file_writes_queued_records_before_closing(tmp_path):
    p = tmp_path / "bot.log"
    with cli_logging(io.StringIO()):
        lg = logging.getLogger("tradingbot.main")
        for round_ in range(3):
            h = M.attach_log_file(p)
            for i in range(200):
                lg.info("기록 %d-%d", round_, i)
            lg.info("잠금 해제 pid=%d", round_)
            M.detach_log_file(h)                               # 큐에 남은 것을 다 쓴 뒤 닫는다
            assert f"잠금 해제 pid={round_}" in p.read_text(encoding="utf-8-sig")


def test_fatal_stop_reason_reaches_log_file_through_cli_logging(tmp_path):
    """cli() 와 같은 로그 배선에서 치명 중단 사유·종료 알림·잠금 해제가 LOG_FILE 에 남는다(텔레그램 없음)."""
    stream = io.StringIO()
    f = Factory(cycle=FakeCycle(raise_on={1: C.LockLostError("x")}), notifier=Notifier(None))
    with cli_logging(stream):
        code, _, err = call(["run"], tmp_path, factory=f, env=env_for(tmp_path, LOG_FILE="logs/bot.log"))
    assert code == M.EXIT_ERROR, err
    text = (tmp_path / "data" / "logs" / "bot.log").read_text(encoding="utf-8-sig")
    for needle in ("치명 오류로 중단: LockLostError", "[알림 비활성]", "잠금 해제"):
        assert needle in text, needle


def test_log_pump_stop_reports_a_stuck_console():
    stream = BlockingStream()
    console = logging.StreamHandler(stream)
    pump = M._LogPump([console])
    pump.start()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "기록", None, None)
    pump.queue.put(rec)
    assert stream.entered.wait(5)
    assert pump.stop(timeout_s=0.2) is False                  # 막힌 채 → 포기하고 알린다
    stream.release.set()
    assert pump.stop(timeout_s=5) is True


def test_cli_exits_even_if_the_console_stays_blocked(tmp_path):
    """콘솔 선택 모드가 풀리지 않은 채 끝나도 프로세스가 멈춰 있지 않는다(종료 코드 유지)."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    child = (
        "import logging, sys, threading\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from tradingbot import main as M\n"
        "class Blocked:\n"
        "    encoding = 'utf-8'\n"
        "    def write(self, s):\n"
        "        threading.Event().wait()\n"
        "    def flush(self):\n"
        "        threading.Event().wait()\n"
        "    def isatty(self):\n"
        "        return False\n"
        "def fake_main(argv=None, **kw):\n"
        "    logging.getLogger('tradingbot.test').warning('사이클 1 완료')\n"
        "    return 3\n"
        "M.main = fake_main\n"
        "sys.stderr = Blocked()\n"
        "sys.exit(M.cli())\n"
    )
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", child], cwd=tmp_path, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 3 and time.monotonic() - t0 < 30


# ======================================================================= 절전 차단
def test_keep_system_awake_requests_and_releases():
    calls: list[int] = []
    with M.keep_system_awake(setter=lambda flags: calls.append(flags) or 0x80000000) as ok:
        assert ok is True and calls == [0x80000001]
    assert calls == [0x80000001, 0x80000000]


def test_keep_system_awake_failure_does_not_stop_run(caplog):
    calls: list[int] = []
    with caplog.at_level(logging.WARNING, logger="tradingbot.main"):
        with M.keep_system_awake(setter=lambda flags: calls.append(flags) or 0) as ok:
            assert ok is False
    assert calls == [0x80000001]                                     # 실패했으면 되돌리지 않는다
    assert "자동 절전 차단 실패" in caplog.text

    def boom(flags):
        raise OSError("no kernel32")

    with M.keep_system_awake(setter=boom) as ok:
        assert ok is False


def test_keep_system_awake_noop_off_windows(monkeypatch):
    monkeypatch.setattr(M.os, "name", "posix")
    with M.keep_system_awake() as ok:
        assert ok is False


def test_cmd_run_holds_awake_request_around_run_loop(tmp_path, monkeypatch):
    order: list[str] = []

    @contextlib.contextmanager
    def fake_awake():
        order.append("awake:on")
        try:
            yield True
        finally:
            order.append("awake:off")

    class RecCycle(FakeCycle):
        def run_once(self):
            order.append("cycle")
            return super().run_once()

    monkeypatch.setattr(M, "keep_system_awake", fake_awake)
    f = Factory(cycle=RecCycle())
    code, _, _ = call(["run", "--once"], tmp_path, factory=f)
    assert code == M.EXIT_OK and order == ["awake:on", "cycle", "awake:off"]


@pytest.mark.skipif(not WINDOWS, reason="실제 SetThreadExecutionState 는 Windows 에만 있다")
def test_windows_real_execution_state_roundtrip():
    setter = M._windows_execution_state_setter()
    assert setter is not None
    with M.keep_system_awake(setter=setter) as ok:
        assert ok is True


# ======================================================================= Ctrl+C·대기
class RecEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float] = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        return super().wait(0)            # 실제로 기다리지 않는다


def test_poll_wait_slices_at_most_one_second():
    ev = RecEvent()
    t = [0.0]

    def mono():
        return t[0]

    def tick_wait(timeout=None):
        ev.timeouts.append(timeout)
        t[0] += timeout
        return False

    ev.wait = tick_wait  # type: ignore[method-assign]
    assert M._poll_wait(ev, 5.5, monotonic=mono) is False
    assert ev.timeouts and max(ev.timeouts) <= 1.0 and sum(ev.timeouts) == pytest.approx(5.5)


def test_poll_wait_returns_immediately_after_wall_clock_jump():
    """PC 절전에서 깨어나면 벽시계가 크게 앞서 있다 → 남은 대기를 버린다."""
    ev = threading.Event()
    wall = [1000.0]
    mono = [0.0]

    def wait(timeout=None):
        mono[0] += timeout
        wall[0] += 8 * 3600.0             # 첫 조각에서 8시간 절전
        return False

    ev.wait = wait  # type: ignore[method-assign]
    t0 = time.monotonic()
    assert M._poll_wait(ev, 900.0, clock=lambda: wall[0], monotonic=lambda: mono[0]) is False
    assert mono[0] <= 1.0 and time.monotonic() - t0 < 1.0


def test_poll_wait_true_when_stopped():
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    t0 = time.monotonic()
    assert M._poll_wait(ev, 30.0) is True
    assert time.monotonic() - t0 < 2.0


class FakeLock:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1


def test_ctrl_c_during_idle_wait_is_acted_on_within_a_second():
    """Windows CPython ≤3.13 처럼 신호가 대기 중인 메인 스레드를 깨우지 못해도(다른 스레드에서 raise_signal)
    1초 조각 대기 덕분에 핸들러가 곧 돌아 루프가 끝난다(예전: 잠금 갱신 조각 = 최대 300초)."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("신호 핸들러는 메인 스레드에서만 설치된다")
    settings = load_settings(env_for(Path("."), CYCLE_MINUTES="1"))
    notifier = FakeNotifier()
    comp = fake_components(settings, notifier=notifier)
    stop = threading.Event()
    prev = M.install_signal_handlers(stop)
    try:
        threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT)).start()
        t0 = time.monotonic()
        code = M.run_loop(comp, once=False, stop_event=stop, clock=lambda: 1000.0, lock=FakeLock(),
                          lock_refresh_s=6.0)
        elapsed = time.monotonic() - t0
    finally:
        M._restore_signals(prev)
    assert code == M.EXIT_OK and comp.cycle.n == 1
    assert notifier.kinds()[-1] == "shutdown"
    assert elapsed < 3.0, elapsed


def test_ctrl_c_during_an_llm_stage_is_handled_within_a_second(tmp_path):
    """분석가·변론·리스크·총괄 호출(스레드 풀) 중에도 메인 스레드가 1초마다 돌아와 Ctrl+C 처리기가 돈다
    (예전: 시간 제한 없는 fut.result() — Windows Python ≤3.13 은 총괄 호출이 끝날 때까지 '종료 신호 수신' 이 늦었다)."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("신호 핸들러는 메인 스레드에서만 설치된다")
    from tradingbot.agents import schemas
    from tradingbot.agents.roles import ROLES
    from tradingbot.agents.runner import AgentRunner
    from tradingbot.llm.backend import MockBackend
    from tradingbot.skills import SkillRegistry

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for role_id in ROLES:
        (prompts / f"{role_id}.md").write_text(f"# Role: {role_id}\n", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    stop = threading.Event()

    def slow_call(payload):
        stop.wait(6.0)                     # 처리기가 stop 을 세우면 곧 끝난다
        return schemas.BullOutput.conservative_example()

    runner = AgentRunner(load_settings(env_for(tmp_path)), MockBackend({"bull": slow_call}),
                         SkillRegistry(tmp_path / "skills"), prompts)
    prev = M.install_signal_handlers(stop)
    try:
        threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT)).start()
        t0 = time.monotonic()
        results = runner.run_many([("bull", {})])
        elapsed = time.monotonic() - t0
    finally:
        M._restore_signals(prev)
    assert stop.is_set() and results[0].ok
    assert elapsed < 3.0, elapsed


def test_sigbreak_is_handled_when_available(monkeypatch):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("신호 핸들러는 메인 스레드에서만 설치된다")
    sig = getattr(signal, "SIGBREAK", None)
    if sig is None:
        monkeypatch.setattr(signal, "SIGBREAK", signal.SIGUSR1, raising=False)
        sig = signal.SIGUSR1
    stop = threading.Event()
    prev = M.install_signal_handlers(stop)
    try:
        assert sig in prev
        signal.raise_signal(sig)
        assert stop.is_set()
    finally:
        M._restore_signals(prev)


# ======================================================================= 절전 복귀·사이클 공백
def test_wait_cut_short_after_wall_clock_jump_then_settles():
    waits: list[float] = []
    wall = [1000.0]

    def wait_fn(s):
        waits.append(s)
        if len(waits) == 1:
            wall[0] += 8 * 3600.0          # 첫 조각 중 절전
        return False

    stopped = M._wait_with_lock_refresh(wait_fn, 870.0, FakeLock(), 300.0, clock=lambda: wall[0])
    assert stopped is False
    assert waits == [300.0, M.WAKE_SETTLE_S]       # 예전: [300, 300, 270]


def test_wait_never_longer_than_planned_after_backward_jump():
    waits: list[float] = []
    wall = [10_000.0]

    def wait_fn(s):
        waits.append(s)
        wall[0] -= 3600.0                 # 시계가 뒤로
        return False

    M._wait_with_lock_refresh(wait_fn, 870.0, FakeLock(), 300.0, clock=lambda: wall[0])
    assert sum(waits) == pytest.approx(870.0)


def test_wait_unchanged_with_constant_clock():
    waits: list[float] = []
    lock = FakeLock()
    M._wait_with_lock_refresh(lambda s: waits.append(s) or False, 805.0, lock, 300.0, clock=lambda: 1000.0)
    assert waits == [300.0, 300.0, 205.0] and lock.refreshes == 2


def test_cycle_gap_alert_after_sleep():
    settings = load_settings(env_for(Path("."), CYCLE_MINUTES="15"))
    notifier = FakeNotifier()
    comp = fake_components(settings, notifier=notifier)
    wall = [1_800_000_000.0]

    def wait(s):
        wall[0] += 3 * 3600.0             # 3시간 절전
        return False

    code = M.run_loop(comp, once=False, max_cycles=2, clock=lambda: wall[0], wait=wait)
    assert code == M.EXIT_OK
    alerts = [e for e in notifier.events if e.kind == "alert"]
    assert len(alerts) == 1 and alerts[0].extra["alert"] == "cycle_gap"
    assert alerts[0].extra["gap_s"] >= 3 * 3600 and "손절·익절" in alerts[0].reason
    text = Notifier.format(alerts[0])
    assert "사이클 공백" in text and "180분" in text


def test_no_cycle_gap_alert_on_normal_schedule():
    settings = load_settings(env_for(Path("."), CYCLE_MINUTES="15"))
    notifier = FakeNotifier()
    comp = fake_components(settings, notifier=notifier)
    wall = [1_800_000_000.0]

    def wait(s):
        wall[0] += s
        return False

    M.run_loop(comp, once=False, max_cycles=3, clock=lambda: wall[0], wait=wait)
    assert not [e for e in notifier.events if e.kind == "alert"]


# ======================================================================= 잠금
def test_lock_single_refresh_failure_after_sleep_is_not_fatal(tmp_path, monkeypatch):
    clock = [1_800_000_000_000]
    lk = M.BotLock(tmp_path / "bot.lock", pid=111, pid_alive=lambda p: True, clock_ms=lambda: clock[0],
                   hostname=lambda: "h", stale_after_s=2700)
    lk.acquire()
    clock[0] += 25 * 60 * 1000            # 25분 절전: 한도(1350초)는 넘었지만 낡은 잠금(2700초)까지는 여유가 있다
    real = os.replace
    fails = [OSError(5, "Access is denied")]

    def flaky(a, b):
        if fails:
            raise fails.pop(0)
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError) as ei:
        lk.refresh()
    assert not isinstance(ei.value, C.LockLostError) and lk.held
    lk.refresh()                          # 다음 갱신은 성공
    assert json.loads((tmp_path / "bot.lock").read_text(encoding="utf-8"))["ts"] == clock[0]


def test_lock_single_refresh_failure_after_long_sleep_is_fatal(tmp_path, monkeypatch):
    """긴 절전 뒤에는 ts 가 이미 낡았다 — 한 번의 실패라도 계속 거래하면 다른 인스턴스와 함께 거래할 수 있다."""
    real = os.replace
    deny = {"on": False}

    def replace(a, b):
        if deny["on"]:
            raise OSError(5, "Access is denied")
        return real(a, b)

    monkeypatch.setattr(os, "replace", replace)
    clock = [1_800_000_000_000]
    for i, slept_s in enumerate((8 * 3600, 2200)):        # 8시간 절전 / 낡기 직전(2700 − 300 − 300 = 2100초 이상)
        lk = M.BotLock(tmp_path / f"bot{i}.lock", pid=111, pid_alive=lambda p: True, clock_ms=lambda: clock[0],
                       hostname=lambda: "h", stale_after_s=2700)
        deny["on"] = False
        lk.acquire()
        clock[0] += slept_s * 1000
        deny["on"] = True
        with pytest.raises(C.LockLostError):
            lk.refresh()


def test_lock_two_consecutive_failures_past_limit_are_fatal(tmp_path, monkeypatch):
    clock = [1_800_000_000_000]
    lk = M.BotLock(tmp_path / "bot.lock", pid=111, pid_alive=lambda p: True, clock_ms=lambda: clock[0],
                   hostname=lambda: "h", stale_after_s=2700)
    lk.acquire()
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(28, "disk full")))
    clock[0] += 1400 * 1000               # 한도(1350초)를 넘었지만 첫 실패
    with pytest.raises(OSError) as ei:
        lk.refresh()
    assert not isinstance(ei.value, C.LockLostError)
    clock[0] += 300 * 1000
    with pytest.raises(C.LockLostError, match="갱신"):
        lk.refresh()


def test_lock_write_retries_transient_sharing_violation(tmp_path, monkeypatch):
    lk = M.BotLock(tmp_path / "bot.lock", pid=111, pid_alive=lambda p: True, hostname=lambda: "h")
    lk.acquire()
    real = os.replace
    fails = [sharing_error(), sharing_error()]

    def flaky(a, b):
        if fails:
            raise fails.pop(0)
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    lk.refresh()                          # 예외 없음
    assert not fails and not list(tmp_path.glob(".bot.lock.*.tmp"))


def test_lock_release_failure_only_warns(tmp_path, monkeypatch, caplog):
    lk = M.BotLock(tmp_path / "bot.lock", pid=111, pid_alive=lambda p: True, hostname=lambda: "h")
    lk.acquire()
    monkeypatch.setattr(fsutil, "unlink", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with caplog.at_level(logging.WARNING, logger="tradingbot.main"):
        lk.release()
    assert lk.held is False and "잠금 파일 삭제 실패" in caplog.text


def _identity(created_ms=None, image=None):
    return lambda pid: (created_ms, image)


def _write_lock(path: Path, **info) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")


@pytest.mark.parametrize("created,image,expect_takeover", [
    (1_800_000_000_000 + 120_000, "python.exe", True),    # 잠금 이후 생성 → 다른 프로세스가 PID 를 받음
    (1_800_000_000_000 - 1_000, "python.exe", False),     # 잠금을 쓴 그 프로세스
    (None, "msedge.exe", True),                           # 실행 파일이 다름
    (None, None, False),                                  # 판단 불가 → 지킨다
])
def test_lock_pid_reuse_detection(tmp_path, created, image, expect_takeover):
    now = 1_800_000_060_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=4321, host="me", ts=now - 60_000, started_ts=1_800_000_000_000, image="python.exe")
    lk = M.BotLock(p, pid=1234, pid_alive=lambda pid: True, clock_ms=lambda: now, hostname=lambda: "me",
                   process_identity=_identity(created, image))
    if expect_takeover:
        lk.acquire()
        assert lk.held and lk.took_over and lk.took_over["pid"] == 4321 and "PID 재사용" in lk.took_over["reason"]
        lk.release()
    else:
        with pytest.raises(M.LockHeldError, match="지우고 다시"):
            lk.acquire()


def test_lock_pid_reuse_check_skips_legacy_lock(tmp_path):
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=4321, host="me", ts=1_800_000_000_000)          # started_ts·image 없음
    lk = M.BotLock(p, pid=1234, pid_alive=lambda pid: True, clock_ms=lambda: 1_800_000_060_000,
                   hostname=lambda: "me", process_identity=_identity(1_900_000_000_000, "msedge.exe"))
    with pytest.raises(M.LockHeldError):
        lk.acquire()


def test_identity_check_disabled_when_pid_alive_injected(tmp_path):
    lk = M.BotLock(tmp_path / "bot.lock", pid=1, pid_alive=lambda pid: True)
    assert lk._identity is None


def test_lock_records_image_and_status_flags_reuse(tmp_path):
    lk = M.BotLock(tmp_path / "bot.lock", process_identity=_identity(None, "python.exe"))
    lk.acquire()
    info = json.loads((tmp_path / "bot.lock").read_text(encoding="utf-8"))
    assert info["image"] == "python.exe" and info["pid"] == os.getpid()
    lk.release()
    # status: 살아 있는 PID 지만 실행 파일이 다르면 '낡음: PID 재사용'
    _write_lock(tmp_path / "bot.lock", pid=os.getpid(), host=socket.gethostname(), ts=int(time.time() * 1000),
                started_ts=int(time.time() * 1000), image="definitely-not-this.exe")
    if WINDOWS:
        lock, label, running = M._describe_lock(tmp_path, 15)
        assert running is False and "PID 재사용" in label


def test_status_shows_absolute_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = call(["status"], tmp_path, env=env_for(tmp_path, DATA_DIR="."))
    assert code == M.EXIT_OK and f"데이터: {os.path.abspath('.')}" in out


def test_takeover_note_in_startup_event(tmp_path):
    data = tmp_path / "data"
    _write_lock(data / "bot.lock", pid=2 ** 22 + 12345, ts=1_700_000_000_000)          # 죽은 PID
    f = Factory()
    code, _, _ = call(["run", "--once"], tmp_path, factory=f)
    assert code == M.EXIT_OK
    start = [e for e in f.comp.notifier.events if e.kind == "startup"][0]
    assert "이전 실행이 정상 종료되지 않았습니다" in start.reason and str(2 ** 22 + 12345) in start.reason


def test_startup_note_for_stale_last_cycle_with_positions(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings)
    st = comp.portfolio.state
    st.last_cycle_ts = int((time.time() - 5 * 3600) * 1000)
    from tradingbot.models import Position
    st.positions["ETH/USDT"] = Position(symbol="ETH/USDT", base_amount=1.0, entry_price=100.0, quote_spent=100.0,
                                        stop_loss_price=99.0, take_profit_price=103.0, opened_cycle=1, opened_ts=1)
    note = M._startup_recovery_note(M.BotLock(tmp_path / "x.lock"), st, settings)
    assert "마지막 사이클" in note and "보유 1개" in note


def test_cmd_run_reports_lock_os_error_without_traceback(tmp_path, monkeypatch):
    def boom(self):
        raise PermissionError(13, "Access is denied", str(self.path), 5)

    monkeypatch.setattr(M.BotLock, "acquire", boom)
    code, _, err = call(["run", "--once"], tmp_path, factory=Factory())
    assert code == M.EXIT_ERROR and "잠금 파일 처리 실패" in err


# ======================================================================= stop / clear-stop
def test_stop_prints_absolute_path_and_warns_without_running_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = call(["stop", "--reason", "x"], tmp_path, env=env_for(tmp_path, DATA_DIR="botdata"))
    assert code == M.EXIT_OK
    assert str(tmp_path / "botdata" / "control" / "STOP") in out
    assert "실행 중인 봇을 찾지 못했습니다" in err and str(tmp_path / "botdata") in err


def test_stop_names_running_bot(tmp_path):
    data = tmp_path / "data"
    _write_lock(data / "bot.lock", pid=os.getpid(), host=socket.gethostname(), ts=int(time.time() * 1000))
    code, out, err = call(["clear-stop"], tmp_path)
    assert code == M.EXIT_OK and f"대상 봇: pid={os.getpid()}" in out and "찾지 못했습니다" not in err


def test_clear_written_even_if_stop_cannot_be_deleted(tmp_path, monkeypatch):
    data = tmp_path / "data"
    M.write_stop(data, "x")
    real = fsutil.unlink

    def deny_stop(path, **kw):
        if Path(path).name == C.STOP_FILENAME:
            raise sharing_error(path)
        return real(path, **kw)

    monkeypatch.setattr(fsutil, "unlink", deny_stop)
    M.write_clear(data)
    assert (data / "control" / "CLEAR").is_file() and (data / "control" / "STOP").is_file()


def test_control_write_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_write_control", lambda p, r: (_ for _ in ()).throw(sharing_error(p)))
    code, _, err = call(["stop", "--reason", "x"], tmp_path)
    assert code == M.EXIT_ERROR and "제어 파일 쓰기 실패" in err


def test_older_stop_left_behind_does_not_override_newer_clear(tmp_path):
    from test_cycle import Rig

    rig = Rig(tmp_path)
    rig.state.peak_equity = 11_000.0
    rig.portfolio.set_kill("drawdown_stop_all")
    ctl = rig.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    now = rig.clock.ms()
    (ctl / "STOP").write_text(json.dumps({"reason": "old", "ts": now - 60_000}), encoding="utf-8")
    (ctl / "CLEAR").write_text(json.dumps({"reason": "new", "ts": now - 30_000}), encoding="utf-8")
    s = rig.cycle.run_once()
    assert rig.state.kill_switch is False and s.drawdown_level == "normal"
    assert rig.state.peak_equity == pytest.approx(10_000.0, rel=1e-3)     # 낙폭 정지 해제가 망가지지 않음
    assert not rig.sent_with("⛔")                                       # 옛 STOP 으로 킬스위치 알림 없음
    assert not (ctl / "STOP").exists() and not (ctl / "CLEAR").exists()


def test_undeletable_clear_is_not_reapplied_after_restart(tmp_path, monkeypatch):
    """지우지 못한 CLEAR 가 남은 채 STOP 으로 킬스위치를 켜고 재시작해도, 옛 CLEAR 가 다시 적용돼 끄지 않는다."""
    from test_cycle import Rig

    real = fsutil.unlink

    def deny_clear(path, **kw):
        if Path(path).name == C.CLEAR_FILENAME:
            raise sharing_error(path)
        return real(path, **kw)

    monkeypatch.setattr(fsutil, "unlink", deny_clear)
    rig = Rig(tmp_path)
    rig.portfolio.set_kill("manual_stop: 처음")
    ctl = rig.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    now = rig.clock.ms()
    (ctl / "CLEAR").write_text(json.dumps({"reason": "해제", "ts": now - 60_000}), encoding="utf-8")
    rig.cycle.run_once()
    assert rig.state.kill_switch is False and (ctl / "CLEAR").is_file()          # 적용됐지만 지우지 못함
    (ctl / "STOP").write_text(json.dumps({"reason": "다시 정지", "ts": now - 30_000}), encoding="utf-8")
    rig.cycle.run_once()
    assert rig.state.kill_switch is True and rig.state.kill_reason == "manual_stop: 다시 정지"
    restarted = Rig(tmp_path)                       # 같은 데이터 폴더로 새 프로세스(재시작)
    assert restarted.state.kill_switch is True
    restarted.cycle.run_once()
    assert restarted.state.kill_switch is True and (ctl / "CLEAR").is_file()
    assert not restarted.sent_with("킬스위치 해제")

    # status·대시보드는 그 CLEAR 를 '대기'가 아니라 '이미 처리함(삭제 실패)'으로 보여 준다
    out, err = io.StringIO(), io.StringIO()
    assert M.main(["status"], env=env_for(tmp_path, DATA_DIR=str(rig.data)), out=out, err=err) == M.EXIT_OK
    assert "CLEAR 대기" not in out.getvalue() and "CLEAR: 이미 처리함" in out.getvalue()
    from fastapi.testclient import TestClient

    from tradingbot.dashboard import create_app

    health = TestClient(create_app(restarted.settings, rig.data)).get("/api/health").json()
    assert health["clear_pending"] is False and health["kill_switch"] is True


def test_control_file_reusing_an_applied_ts_is_applied_again_once_the_old_one_is_gone(tmp_path):
    """수정 시각이 유지되는 템플릿 복사·ts 를 고정한 JSON: 앞 파일이 지워진 뒤 같은 ts 의 새 STOP 도 적용된다."""
    from test_cycle import Rig

    rig = Rig(tmp_path)
    ctl = rig.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    stop = json.dumps({"reason": "주말 정지", "ts": 5})
    (ctl / "STOP").write_text(stop, encoding="utf-8")
    rig.cycle.run_once()
    assert rig.state.kill_switch is True and not (ctl / "STOP").exists()
    rig.cycle.run_once()                                   # 파일이 없어진 사이클 — '처리함' 표시도 지운다
    assert "stop" not in rig.state.control_applied_ts
    (ctl / "CLEAR").write_text(json.dumps({"reason": "재개", "ts": 6}), encoding="utf-8")
    rig.cycle.run_once()
    assert rig.state.kill_switch is False
    rig.cycle.run_once()
    (ctl / "STOP").write_text(stop, encoding="utf-8")       # 같은 템플릿을 다시
    rig.cycle.run_once()
    assert rig.state.kill_switch is True and rig.state.kill_reason == "manual_stop: 주말 정지"


def test_state_json_leaves_out_empty_control_applied_ts(tmp_path):
    """이 필드를 모르는 이전 버전(extra=forbid)도 state.json 을 읽게, 비었으면 쓰지 않는다."""
    from tradingbot import portfolio as PF

    pf = PF.Portfolio.load(load_settings(env_for(tmp_path)), tmp_path / "state.json")
    pf.save()
    assert "control_applied_ts" not in json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    pf.state.control_applied_ts["clear"] = 123
    pf.save()
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["control_applied_ts"] == {"clear": 123}
    assert PF.Portfolio.load(load_settings(env_for(tmp_path)), tmp_path / "state.json").state.control_applied_ts == {
        "clear": 123}


# ======================================================================= .env·설정
@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k in ("MODE", "AGENT_BACKEND", "MOCK_SCENARIO", "DATA_DIR", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "TELEGRAM_CHAT_ID", "LOG_FILE", "CYCLE_MINUTES", "SYMBOLS") or k.startswith("SCANNER_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_dotenv_utf8_bom_first_key(clean_env):
    (clean_env / ".env").write_bytes(b"\xef\xbb\xbfDATA_DIR=botdata\r\nAGENT_BACKEND=mock\r\n")
    env = read_env()
    assert env["DATA_DIR"] == "botdata" and env["AGENT_BACKEND"] == "mock"
    assert "\ufeffDATA_DIR" not in env


def test_dotenv_utf16_is_decoded_with_warning(clean_env):
    (clean_env / ".env").write_bytes("\ufeffAGENT_BACKEND=mock\r\nDATA_DIR=botdata\r\n".encode("utf-16-le"))
    warnings: list[str] = []
    env = read_env(lenient=True, warnings=warnings)
    assert env["DATA_DIR"] == "botdata" and any("UTF-16" in w for w in warnings)
    assert load_settings().data_dir == Path("botdata")


def test_dotenv_cp949_strict_error_names_line_lenient_reads(clean_env):
    (clean_env / ".env").write_bytes("AGENT_BACKEND=mock\r\n# 텔레그램 설정\r\nDATA_DIR=botdata\r\n".encode("cp949"))
    with pytest.raises(ConfigError, match="2번째 줄"):
        load_settings()
    warnings: list[str] = []
    env = read_env(lenient=True, warnings=warnings)
    assert env["DATA_DIR"] == "botdata" and warnings and "UTF-8" in warnings[0]


def test_dotenv_nul_bytes_from_powershell_append(clean_env):
    data = "AGENT_BACKEND=mock\r\n".encode("utf-8") + "LLM_DAILY_BUDGET_USD=1\r\n".encode("utf-16-le")
    (clean_env / ".env").write_bytes(data)
    with pytest.raises(ConfigError, match="NUL"):
        load_settings()
    warnings: list[str] = []
    env = read_env(lenient=True, warnings=warnings)
    assert env.get("LLM_DAILY_BUDGET_USD") == "1" and any("NUL" in w for w in warnings)


def test_dotenv_unclosed_quote_swallowing_next_line_is_an_error(clean_env):
    (clean_env / ".env").write_bytes(b'AGENT_BACKEND=mock\nDATA_DIR="X:\\data\\"\nTELEGRAM_BOT_TOKEN="1:a"\n')
    with pytest.raises(ConfigError, match="2번째 줄"):
        load_settings()
    warnings: list[str] = []
    read_env(lenient=True, warnings=warnings)
    assert any("해석하지 못했습니다" in w for w in warnings)


def test_dotenv_parse_error_never_shows_secret_text(clean_env):
    """':' 나 공백으로 쓴 비밀값 줄·비밀값으로 시작하는 줄: 오류에는 줄 번호만(이름은 NAME= 꼴일 때만)."""
    (clean_env / ".env").write_bytes(b"AGENT_BACKEND=mock\nTELEGRAM_BOT_TOKEN: 1234:SECRETPARTxyz\n"
                                     b"ANTHROPIC_API_KEY sk-ant-api03-SECRETBODY\nSECRETBARE123abc note\n")
    with pytest.raises(ConfigError, match="2번째 줄") as ei:
        load_settings()
    assert "SECRET" not in str(ei.value) and "1234" not in str(ei.value) and "'='" in str(ei.value)
    warnings: list[str] = []
    read_env(lenient=True, warnings=warnings)
    assert len(warnings) == 3 and not any("SECRET" in w or "1234" in w for w in warnings)
    out, err = io.StringIO(), io.StringIO()
    M.main(["notify-test"], out=out, err=err)
    assert "SECRET" not in out.getvalue() + err.getvalue()


def test_dotenv_parse_error_line_number_skips_blank_lines(clean_env):
    (clean_env / ".env").write_bytes(b'MODE=paper\n\nDATA_DIR="X:\\data\\"\nAGENT_BACKEND=mock\n')
    with pytest.raises(ConfigError, match=r"3번째 줄\(DATA_DIR="):
        load_settings()
    (clean_env / ".env").write_bytes(b'MODE=paper\r\n\r\n\r\nDATA_DIR="X:\\data\\"\r\nAGENT_BACKEND=mock\r\n')
    with pytest.raises(ConfigError, match=r"4번째 줄\(DATA_DIR="):
        load_settings()
    # 해석 실패 뒤의 CRLF 를 두 번 세지 않는다(메모장 파일, lenient 의 두 번째 이후 경고)
    (clean_env / ".env").write_bytes(b"AGENT_BACKEND=mock\r\n\r\nset DATA_DIR=d\r\n\r\nset MODE=paper\r\n")
    warnings: list[str] = []
    read_env(lenient=True, warnings=warnings)
    assert [w.split(" ", 2)[1] for w in warnings] == ["3번째", "5번째"]
    assert "(DATA_DIR)" in warnings[0] and "set" in warnings[0]


def test_dotenv_utf16_with_appended_8bit_lines(clean_env):
    """PowerShell '>' 로 만든 UTF-16 파일에 cmd·Git Bash 로 덧붙인 줄: 조용히 사라지지 않고, stop 은 계속 된다."""
    head = "\ufeffAGENT_BACKEND=mock\r\nDATA_DIR=botdata\r\n".encode("utf-16-le")
    for tail in (b"MODE=paper\n", "# 텔레그램\r\n".encode("cp949")):         # 홀수 길이 / 잘못된 서로게이트
        (clean_env / ".env").write_bytes(head + tail)
        with pytest.raises(ConfigError, match="UTF-16"):
            load_settings()
        warnings: list[str] = []
        assert read_env(lenient=True, warnings=warnings)["DATA_DIR"] == "botdata" and warnings
        out, err = io.StringIO(), io.StringIO()
        assert M.main(["stop", "--reason", "x"], out=out, err=err) == M.EXIT_OK
        assert (clean_env / "botdata" / "control" / "STOP").is_file()
        out, err = io.StringIO(), io.StringIO()
        assert M.main(["notify-test"], out=out, err=err) == M.EXIT_USAGE       # 트레이스백 없이 '설정 없음'
    # 짝수 길이로 풀리면 덧붙인 줄이 '=' 없는 한 줄(CJK 글자)이 된다 → 그 줄 번호로 알린다(조용히 기본값으로 돌지 않게)
    (clean_env / ".env").write_bytes("\ufeffAGENT_BACKEND=mock\r\n".encode("utf-16-le") + b"DATA_DIR=D:/bots\r\n")
    with pytest.raises(ConfigError, match="2번째 줄"):
        load_settings()
    warnings = []
    read_env(lenient=True, warnings=warnings)
    assert any("2번째 줄" in w and "인코딩이 섞인" in w for w in warnings)


def test_dotenv_utf16_copy_of_env_example_still_loads(clean_env):
    data = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    (clean_env / ".env").write_bytes(("\ufeff" + data.replace("\n", "\r\n")).encode("utf-16-le"))
    warnings: list[str] = []
    env = read_env(lenient=True, warnings=warnings)
    assert len(warnings) == 1 and "UTF-16" in warnings[0] and env["MODE"] == "paper"


def test_dotenv_invisible_characters_from_copy_paste(clean_env):
    """복사·붙여넣기로 섞인 보이지 않는 글자(U+200B·U+FEFF·U+3164 등): 빈 줄·주석이면 무시, 이름에 붙었으면 지우고 읽는다."""
    (clean_env / ".env").write_bytes("AGENT_BACKEND=mock\n\u200b# 텔레그램 설정\n\ufeff\n\u3164\n"
                                     "\u200bDATA_DIR=botdata\n".encode("utf-8"))
    warnings: list[str] = []
    env = read_env(lenient=False, warnings=warnings)
    assert env["DATA_DIR"] == "botdata" and "\u200bDATA_DIR" not in env
    assert len(warnings) == 1 and "보이지 않는 글자" in warnings[0] and "DATA_DIR" in warnings[0]
    assert load_settings().data_dir == Path("botdata")


def test_dotenv_utf16_with_an_8bit_tail_glued_to_the_last_line(clean_env):
    """UTF-16 의 마지막 줄에 줄바꿈이 없으면 덧붙인 8비트 줄이 그 값에 붙어 사라진다 — 그 줄 번호로 알린다."""
    head = "\ufeffAGENT_BACKEND=mock\r\nTELEGRAM_CHAT_ID=12345".encode("utf-16-le")
    (clean_env / ".env").write_bytes(head + b"DATA_DIR=D:/bots\r\n")
    with pytest.raises(ConfigError, match="2번째 줄에 다른 인코딩"):
        load_settings()
    warnings: list[str] = []
    read_env(lenient=True, warnings=warnings)
    assert any("다른 인코딩" in w for w in warnings)


def test_dotenv_quote_errors_on_export_and_lowercase_lines_get_quote_advice(clean_env):
    (clean_env / ".env").write_bytes(b'export DATA_DIR="B:\\TradingBot\\data\\"\nAGENT_BACKEND=mock\n')
    with pytest.raises(ConfigError, match=r"1번째 줄\(DATA_DIR=…\).*따옴표") as ei:
        load_settings()
    assert "set" not in str(ei.value)
    (clean_env / ".env").write_bytes(b'data_dir="B:\\data\\"\nAGENT_BACKEND=mock\n')
    with pytest.raises(ConfigError, match="1번째 줄을 해석하지 못했습니다 — 닫히지 않은 따옴표"):
        load_settings()


def test_env_override_warning_ignores_empty_placeholders(clean_env, monkeypatch, caplog):
    """.env.example 을 복사한 .env 의 빈 자리(ANTHROPIC_API_KEY= …)는 환경변수에 둔 비밀값과 겹쳐도 경고하지 않는다."""
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    (clean_env / ".env").write_text(example, encoding="utf-8")
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(k, "from-env")
    monkeypatch.setattr(CFG, "_WARNED_OVERRIDES", set())
    with caplog.at_level(logging.WARNING, logger="tradingbot.config"):
        read_env()
    assert not [r for r in caplog.records if "덮어씁니다" in r.getMessage()]
    monkeypatch.setenv("AGENT_BACKEND", "mock")          # .env 는 AGENT_BACKEND=anthropic
    with caplog.at_level(logging.WARNING, logger="tradingbot.config"):
        read_env()
    msgs = [r.getMessage() for r in caplog.records if "덮어씁니다" in r.getMessage()]
    assert len(msgs) == 1 and "AGENT_BACKEND" in msgs[0] and "ANTHROPIC_API_KEY" not in msgs[0]


def test_dotenv_directory_is_ignored_like_a_missing_file(clean_env, monkeypatch, caplog):
    """``python -m venv .env`` 로 만든 폴더는 설정 파일이 아니다(전처럼 환경변수만으로 실행)."""
    (clean_env / ".env").mkdir()
    monkeypatch.setenv("AGENT_BACKEND", "mock")
    monkeypatch.setenv("DATA_DIR", str(clean_env / "data"))
    assert load_settings().agent_backend == "mock"
    out, err = io.StringIO(), io.StringIO()
    with caplog.at_level(logging.INFO, logger="tradingbot.main"):
        code = M.main(["run", "--once"], env=None, input_fn=lambda prompt: "", out=out, err=err,
                      components_factory=Factory())
    assert code == M.EXIT_OK, err.getvalue()
    assert "에 .env 가 없습니다" in caplog.text and "폴더입니다" in caplog.text


def test_data_dir_validation():
    with pytest.raises(ConfigError, match="이스케이프"):
        parse_data_dir("X:\x08ot_data")              # "X:\bot_data" 를 큰따옴표로 쓴 결과
    with pytest.raises(ConfigError, match="%변수%"):
        parse_data_dir("%USERPROFILE%\\bot")
    with pytest.raises(ConfigError):
        parse_data_dir('B:\\TradingBot"')
    assert parse_data_dir("B:/TradingBot/data") == Path("B:/TradingBot/data")
    assert parse_data_dir("") == Path(".")


def test_double_quoted_windows_data_dir_in_dotenv_is_rejected(clean_env):
    (clean_env / ".env").write_bytes(b'AGENT_BACKEND=mock\nDATA_DIR="X:\\bot_data"\n')
    with pytest.raises(ConfigError, match="DATA_DIR"):
        load_settings()
    out, err = io.StringIO(), io.StringIO()
    assert M.main(["stop", "--reason", "x"], out=out, err=err) == M.EXIT_USAGE
    assert "DATA_DIR" in err.getvalue()


def test_broken_dotenv_still_allows_stop_and_status(clean_env):
    (clean_env / ".env").write_bytes("AGENT_BACKEND=mock\r\n# 한글 주석\r\nDATA_DIR=botdata\r\n".encode("cp949"))
    out, err = io.StringIO(), io.StringIO()
    assert M.main(["stop", "--reason", "x"], out=out, err=err) == M.EXIT_OK
    assert (clean_env / "botdata" / "control" / "STOP").is_file() and ".env" in err.getvalue()
    out, err = io.StringIO(), io.StringIO()
    assert M.main(["status"], out=out, err=err) == M.EXIT_OK
    out, err = io.StringIO(), io.StringIO()
    assert M.main(["run", "--once", "--offline-fixture"], out=out, err=err) == M.EXIT_USAGE
    assert ".env" in err.getvalue()


def test_env_override_warning_names_keys_only(clean_env, monkeypatch, caplog):
    (clean_env / ".env").write_text("AGENT_BACKEND=anthropic\nANTHROPIC_API_KEY=dotenv-secret-value\n",
                                    encoding="utf-8")
    monkeypatch.setenv("AGENT_BACKEND", "mock")
    monkeypatch.setattr(CFG, "_WARNED_OVERRIDES", set())
    with caplog.at_level(logging.WARNING, logger="tradingbot.config"):
        env = read_env()
        read_env()
    assert env["AGENT_BACKEND"] == "mock"
    msgs = [r.getMessage() for r in caplog.records if "덮어씁니다" in r.getMessage()]
    assert len(msgs) == 1 and "AGENT_BACKEND" in msgs[0] and "dotenv-secret-value" not in msgs[0]


def test_log_file_setting(tmp_path):
    s = load_settings(env_for(tmp_path, LOG_FILE="logs/bot.log"))
    assert s.log_file == Path(tmp_path / "data" / "logs" / "bot.log")
    assert load_settings(env_for(tmp_path, LOG_FILE="none")).log_file is None
    assert load_settings(env_for(tmp_path)).log_file is None
    with pytest.raises(ConfigError, match="LOG_FILE"):
        load_settings(env_for(tmp_path, LOG_FILE="C:\x08ot.log"))


def test_scanner_thresholds_from_dotenv_reach_scanner(clean_env):
    (clean_env / ".env").write_text("MODE=paper\nAGENT_BACKEND=mock\nDATA_DIR=d\nSCANNER_MAX_SPREAD_BPS=3\n"
                                    "SCANNER_MIN_QUOTE_VOLUME_24H=100000000\n", encoding="utf-8")
    comp = M.build_components(load_settings(), offline_fixture=True)
    th = comp.cycle.scanner.thresholds
    assert th.MAX_SPREAD_BPS == 3.0 and th.MIN_QUOTE_VOLUME_24H == 1e8


def test_scanner_thresholds_env_var_wins_over_dotenv(clean_env, monkeypatch):
    (clean_env / ".env").write_text("AGENT_BACKEND=mock\nDATA_DIR=d\nSCANNER_MAX_SPREAD_BPS=3\n", encoding="utf-8")
    monkeypatch.setenv("SCANNER_MAX_SPREAD_BPS", "5")
    comp = M.build_components(load_settings(), offline_fixture=True)
    assert comp.cycle.scanner.thresholds.MAX_SPREAD_BPS == 5.0


def test_startup_logs_dotenv_location(clean_env, caplog):
    f = Factory()
    with caplog.at_level(logging.INFO, logger="tradingbot.main"):
        code, _, _ = call(["run", "--once"], clean_env, factory=f)
    assert code == M.EXIT_OK
    assert "에 .env 가 없습니다" in caplog.text and "데이터 폴더: " in caplog.text
    (clean_env / ".env.txt").write_text("MODE=paper\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="tradingbot.main"):
        call(["run", "--once"], clean_env, factory=Factory())
    assert ".env.txt" in caplog.text


# ======================================================================= run 옵션·안전
class KwFactory(Factory):
    def __call__(self, settings, *, lock=None, **kw):
        self.kw_seen = kw
        self.settings = settings
        return super().__call__(settings, lock=lock)


def test_offline_fixture_needs_no_api_key(tmp_path):
    f = KwFactory()
    env = {"MODE": "paper", "AGENT_BACKEND": "anthropic", "DATA_DIR": str(tmp_path / "data")}   # 키 없음
    code, _, err = call(["run", "--once", "--offline-fixture", "--mock-scenario", "approve"], tmp_path, env=env,
                        factory=f)
    assert code == M.EXIT_OK, err
    assert f.settings.agent_backend == "mock" and f.kw_seen.get("mock_scenario") == "approve"


def test_mock_scenario_flag_requires_mock_backend(tmp_path):
    env = env_for(tmp_path, AGENT_BACKEND="anthropic", ANTHROPIC_API_KEY="k" * 30)
    code, _, err = call(["run", "--once", "--mock-scenario", "approve"], tmp_path, env=env, factory=KwFactory())
    assert code == M.EXIT_USAGE and "--mock-scenario" in err


def test_live_with_mock_backend_is_refused_by_real_components(tmp_path):
    env = env_for(tmp_path, MODE="live", CONFIRM_LIVE="YES", BINANCE_LIVE_API_KEY="k" * 20,
                  BINANCE_LIVE_API_SECRET="s" * 20)
    with pytest.raises(ConfigError, match="mock"):
        M.build_components(load_settings(env))


def test_live_prompt_shows_backend(tmp_path):
    env = env_for(tmp_path, MODE="live", CONFIRM_LIVE="YES", BINANCE_LIVE_API_KEY="k" * 20,
                  BINANCE_LIVE_API_SECRET="s" * 20)
    code, out, _ = call(["run", "--once"], tmp_path, env=env, input_fn=lambda p: "no", factory=Factory())
    assert code == M.EXIT_REFUSED and "판단 백엔드: mock" in out


def test_startup_message_flags_mock_backend():
    ev = TradeEvent(kind="startup", ts=1_800_000_000_000, mode="paper", reason="루프 시작",
                    extra={"backend": "mock", "mock_scenario": "approve", "symbols": ["BTC/USDT"]})
    text = Notifier.format(ev)
    assert "mock" in text and "approve" in text and "실제 Claude 판단이 아닙니다" in text
    ev2 = ev.model_copy(update={"extra": {"backend": "anthropic"}})
    assert "실제 Claude 판단이 아닙니다" not in Notifier.format(ev2)


# ======================================================================= 비용 원장(잠긴 파일)
def test_state_save_waits_the_sharing_budget_once_then_fails(tmp_path, monkeypatch):
    """state.json 이 다른 프로그램에 열려 있으면 SAVE_SHARING_BUDGET_S 동안만 기다리고(전체 재시도 반복 없이) 실패한다."""
    from tradingbot import portfolio as PF

    pf = PF.Portfolio.load(load_settings(env_for(tmp_path)), tmp_path / "state.json")
    sleeps: list[float] = []
    pf.sleep = sleeps.append
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(sharing_error(b)))
    with pytest.raises(PermissionError):
        pf.save()
    assert sum(sleeps) == pytest.approx(PF.SAVE_SHARING_BUDGET_S)
    assert not list(tmp_path.glob("state.json.tmp.*"))
    monkeypatch.undo()
    real = os.replace
    fails = [sharing_error(), sharing_error()]

    def flaky(a, b):
        if fails:
            raise fails.pop(0)
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    sleeps.clear()
    pf.save()
    assert (tmp_path / "state.json").is_file() and 0 < sum(sleeps) < PF.SAVE_SHARING_BUDGET_S


def test_cost_ledger_locked_file_goes_to_pending_and_is_counted(tmp_path, monkeypatch):
    path = tmp_path / "cost_ledger.jsonl"
    led = CostLedger(path, 10, 200)
    real = CostLedger._append_line

    def locked(p, line):
        if Path(p) == path:
            raise sharing_error(p)
        return real(p, line)

    monkeypatch.setattr(CostLedger, "_append_line", staticmethod(locked))
    cost = led.record(role="head", model="claude-opus-5-5", input_tokens=1000, cached_tokens=0, output_tokens=100)
    assert pending_ledger_path(path).is_file() and not path.exists()
    monkeypatch.undo()
    led.record(role="head", model="claude-opus-5-5", input_tokens=1000, cached_tokens=0, output_tokens=100)
    again = CostLedger(path, 10, 200)
    assert again.spent_today() == pytest.approx(2 * cost)


class _NtOs:
    """``tradingbot.fsutil`` 에게만 Windows 로 보이게 한다(전역 ``os.name`` 을 바꾸면 pathlib 이 깨진다)."""
    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


def test_cost_ledger_windows_open_denied_goes_to_pending(tmp_path, monkeypatch):
    """Windows 의 open() 은 공유 위반에도 errno 13 만 싣는다(winerror 없음) — 그 모양 그대로 보조 파일로 간다."""
    from tradingbot.llm import cost as COST

    monkeypatch.setattr(fsutil, "os", _NtOs())
    monkeypatch.setattr(COST, "LEDGER_SHARING_BUDGET_S", 0.05)
    path = tmp_path / "cost_ledger.jsonl"
    led = CostLedger(path, 10, 200)
    real_open = Path.open
    tries = {"n": 0}

    def open_(self, *a, **k):
        if self == path:
            tries["n"] += 1
            raise PermissionError(13, "Permission denied", str(self))          # winerror 없음
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", open_)
    led.record(role="head", model="claude-opus-5-5", input_tokens=1000, cached_tokens=0, output_tokens=100)
    assert pending_ledger_path(path).is_file() and not path.exists()
    assert tries["n"] >= 2                                     # 원장 자체도 잠깐 다시 시도한 뒤 보조 파일로


def test_append_bytes_retries_windows_open_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "os", _NtOs())
    target = tmp_path / "decisions.jsonl"
    real_open = Path.open
    denied = {"n": 2}

    def open_(self, *a, **k):
        if self == target and denied["n"]:
            denied["n"] -= 1
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", open_)
    sleeps: list[float] = []
    fsutil.append_bytes(target, b"x\n", sleep=sleeps.append)
    assert target.read_bytes() == b"x\n" and len(sleeps) == 2


def test_cost_ledger_other_errors_still_raise(tmp_path, monkeypatch):
    led = CostLedger(tmp_path / "cost_ledger.jsonl", 10, 200)
    monkeypatch.setattr(CostLedger, "_append_line", staticmethod(lambda p, line: (_ for _ in ()).throw(
        OSError(28, "No space left on device"))))
    with pytest.raises(OSError):
        led.record(role="head", model="claude-opus-5-5", input_tokens=1, cached_tokens=0, output_tokens=1)


def test_dashboard_costs_include_pending_ledger(tmp_path):
    from fastapi.testclient import TestClient

    from tradingbot.dashboard import create_app

    data = tmp_path / "data"
    led = CostLedger(pending_ledger_path(data / "cost_ledger.jsonl"), 10, 200)
    led.record(role="head", model="claude-opus-5-5", input_tokens=1000, cached_tokens=0, output_tokens=100)
    settings = load_settings({"AGENT_BACKEND": "mock", "DATA_DIR": str(data)})
    body = TestClient(create_app(settings, data)).get("/api/costs").json()
    assert body["today_usd"] > 0


# ======================================================================= 대시보드 STOP/해제
def _dash(tmp_path):
    from fastapi.testclient import TestClient

    from tradingbot.dashboard import create_app

    data = tmp_path / "data"
    settings = load_settings({"AGENT_BACKEND": "mock", "DATA_DIR": str(data)})
    client = TestClient(create_app(settings, data))
    token = (data / "control" / "dashboard_token").read_text(encoding="utf-8").strip()
    return client, data, {"X-Dashboard-Token": token}


def test_dashboard_kill_ok_even_if_pending_clear_is_locked(tmp_path, monkeypatch):
    client, data, hdr = _dash(tmp_path)
    (data / "control" / "CLEAR").write_text("{}", encoding="utf-8")
    real = fsutil.unlink
    monkeypatch.setattr(fsutil, "unlink", lambda p, **k: (_ for _ in ()).throw(sharing_error(p))
                        if Path(p).name == "CLEAR" else real(p, **k))
    r = client.post("/api/kill", json={"reason": "x"}, headers=hdr)
    assert r.status_code == 200 and (data / "control" / "STOP").is_file()


def test_dashboard_clear_written_even_if_stop_is_locked(tmp_path, monkeypatch):
    client, data, hdr = _dash(tmp_path)
    (data / "control" / "STOP").write_text("{}", encoding="utf-8")
    real = fsutil.unlink
    monkeypatch.setattr(fsutil, "unlink", lambda p, **k: (_ for _ in ()).throw(sharing_error(p))
                        if Path(p).name == "STOP" else real(p, **k))
    r = client.post("/api/kill/clear", json={"reason": "x"}, headers=hdr)
    assert r.status_code == 200 and r.json()["stop_removed"] is False and (data / "control" / "CLEAR").is_file()


# ======================================================================= 거래소 시계
class NonceExchange(FakeExchange):
    """FakeExchange + load_time_difference(재보정 횟수 기록)."""

    def __init__(self) -> None:
        super().__init__()
        self.resyncs = 0

    def load_time_difference(self, params=None):
        self.resyncs += 1
        self.options["timeDifference"] = 1500
        return 1500


def _broker(ex, sleeps):
    return CcxtBroker(make_settings("testnet"), exchange=ex, sleep=sleeps.append)


def test_configure_exchange_disables_presync_signed_calls():
    ex = FakeExchange()
    _CcxtBase._configure_exchange(ex, sandbox=False)
    assert ex.options["fetchCurrencies"] is False and ex.options["fetchMargins"] is False
    assert ex.options["adjustForTimeDifference"] is True and ex.options["defaultType"] == "spot"


def test_invalid_nonce_resyncs_clock_then_retries_without_sleep():
    ex = NonceExchange()
    sleeps: list[float] = []
    b = _broker(ex, sleeps)
    ex.fail_queue["fetch_balance"] = [ccxt.InvalidNonce("binance -1021 Timestamp for this request is outside of "
                                                        "the recvWindow")]
    assert b.free_quote() == 5_000.0
    assert ex.resyncs == 1 and sleeps == []


def test_invalid_nonce_on_order_resyncs_and_orders_once():
    ex = NonceExchange()
    b = _broker(ex, [])
    ex.fail_queue["create_order"] = [ccxt.InvalidNonce("binance -1021")]
    fill = b.market_buy("BTC/USDT", 100.0)
    creates = [c for c in ex.calls if c[0] == "create_order"]
    assert fill.base_amount > 0 and len(creates) == 2 and len(ex.orders) == 1 and ex.resyncs == 1


def test_invalid_nonce_without_resync_support_falls_back_to_backoff():
    ex = FakeExchange()
    sleeps: list[float] = []
    b = _broker(ex, sleeps)
    ex.fail_queue["fetch_balance"] = [ccxt.InvalidNonce("binance -1021")]
    assert b.free_quote() == 5_000.0 and sleeps                       # 예전처럼 기다렸다 재시도


class ThrottleEx:
    def __init__(self) -> None:
        self.now = 10_000_000
        self.lastRestRequestTimestamp = 0
        self.rateLimit = 50
        self.slept: list[float] = []

    def milliseconds(self):
        return self.now

    def throttle(self, cost=None):
        elapsed = self.now - self.lastRestRequestTimestamp
        sleep_ms = self.rateLimit * (1 if cost is None else cost)
        if elapsed < sleep_ms:
            self.slept.append((sleep_ms - elapsed) / 1000.0)


def test_step_safe_throttle_caps_wait_after_backward_clock_step():
    ex = ThrottleEx()
    _install_step_safe_throttle(ex)
    _install_step_safe_throttle(ex)              # 두 번 설치해도 한 번만 감싼다
    ex.lastRestRequestTimestamp = ex.now
    ex.now -= 3_600_000                          # 시계가 1시간 뒤로
    ex.throttle()
    assert ex.slept and max(ex.slept) <= 0.05 + 1e-9          # 예전: 3600초 잠듦
    ex.slept.clear()
    ex.lastRestRequestTimestamp = ex.now - 10    # 정상: 10ms 전 요청 → 40ms 대기(그대로)
    ex.throttle()
    assert ex.slept == [pytest.approx(0.04)]


def test_step_safe_throttle_ignores_test_doubles():
    class Bare:
        pass

    b = Bare()
    _install_step_safe_throttle(b)               # 속성 없음 → 아무것도 안 함
    assert not hasattr(b, "throttle")


def test_futures_client_gets_step_safe_throttle():
    from test_exchange_ccxt import FakeExchange as _FE

    broker = CcxtBroker(make_settings("testnet"), exchange=_FE(), sleep=lambda s: None, futures_factory=ThrottleEx)
    assert getattr(broker._futures_client().throttle, "_tb_step_safe", False)


def test_real_ccxt_binance_gets_step_safe_throttle():
    ex = ccxt.binance({"enableRateLimit": True})
    _CcxtBase._configure_exchange(ex, sandbox=False)
    assert getattr(ex.throttle, "_tb_step_safe", False)
    ex.lastRestRequestTimestamp = ex.milliseconds() + 3_600_000       # 마지막 요청이 '미래'(시계 역행)
    t0 = time.monotonic()
    ex.throttle(1)
    assert time.monotonic() - t0 < 1.0


# ======================================================================= 사이클 순서(손절 먼저)
def test_post_trade_reviews_run_after_all_code_exits(tmp_path):
    from test_cycle import Rig

    rig = Rig(tmp_path)
    order: list[str] = []
    real_sell = rig.broker.market_sell

    def sell(symbol, base_amount):
        order.append(f"sell:{symbol}")
        return real_sell(symbol, base_amount)

    rig.broker.market_sell = sell
    real_complete = rig.backend.complete

    def complete(**k):
        order.append(f"llm:{k.get('role')}")
        return real_complete(**k)

    rig.backend.complete = complete
    rig.hold_position("ETH/USDT", sl=99.0, tp=110.0)
    rig.hold_position("SOL/USDT", sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.market.prices["SOL/USDT"] = 95.0
    rig.scanner.candidates = []
    s = rig.cycle.run_once()
    assert len(s.exits) == 2
    sells = [i for i, x in enumerate(order) if x.startswith("sell:")]
    llms = [i for i, x in enumerate(order) if x.startswith("llm:")]
    assert len(sells) == 2 and llms and max(sells) < min(llms), order       # 두 번째 손절이 사후분석을 기다리지 않음
    assert len(rig.sent_with("사후분석")) == 2 and len(rig.journal_lines()) == 2


@pytest.mark.parametrize("partial", [False, True])
def test_earlier_exit_is_journaled_when_a_later_exit_save_fails(tmp_path, monkeypatch, partial):
    """사후분석을 미뤄도, 같은 사이클의 뒤 매도가 상태 저장 실패(치명)로 끝나면 앞 거래의 저널 기록은 남는다."""
    from test_cycle import Rig

    rig = Rig(tmp_path)
    rig.hold_position("ETH/USDT", sl=99.0, tp=110.0)
    rig.hold_position("SOL/USDT", sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.market.prices["SOL/USDT"] = 95.0
    rig.scanner.candidates = []
    real_sell = rig.broker.market_sell
    sold: list[str] = []

    def sell(symbol, base_amount):
        sold.append(symbol)
        if partial and symbol == "ETH/USDT":
            base_amount = base_amount / 2                   # 절반만 체결(남은 절반은 보유 중 → 재시작해도 고아 아님)
        return real_sell(symbol, base_amount)

    real_save = rig.portfolio.save

    def save(*a, **k):
        if len(sold) >= 2:
            raise OSError(28, "No space left on device")
        return real_save(*a, **k)

    monkeypatch.setattr(rig.broker, "market_sell", sell)
    monkeypatch.setattr(rig.portfolio, "save", save)
    with pytest.raises(C.StateSaveError):
        rig.cycle.run_once()
    lines = rig.journal_lines()
    assert len(sold) == 2 and [ln.get("symbol") for ln in lines] == [sold[0]], lines


def test_restart_does_not_duplicate_a_trade_journaled_before_the_journal_state_save_failed(tmp_path, monkeypatch):
    """중단 처리가 저널 줄은 썼지만 저널 상태 저장은 실패(디스크 가득)한 채 멈춰도, 재시작 대사가 같은 거래를 또 쓰지 않는다."""
    from test_cycle import Rig

    from tradingbot import journal as J

    rig = Rig(tmp_path)
    for sym in ("ETH/USDT", "SOL/USDT"):
        rig.hold_position(sym, sl=99.0, tp=110.0)
        rig.cycle.journal_state.entry_verdicts[sym] = J.normalize_entry_verdicts(None)   # 재시작 대사 대상
        rig.market.prices[sym] = 95.0
    J.save_journal_state(rig.cycle.journal_state, rig.data)         # 진입 사이클이 저장해 둔 저널 상태
    rig.scanner.candidates = []
    real_sell, real_save = rig.broker.market_sell, rig.portfolio.save
    sold: list[str] = []

    def sell(symbol, base_amount):
        sold.append(symbol)
        return real_sell(symbol, base_amount)

    def save(*a, **k):
        if len(sold) >= 2:
            raise OSError(28, "No space left on device")
        return real_save(*a, **k)

    monkeypatch.setattr(rig.broker, "market_sell", sell)
    monkeypatch.setattr(rig.portfolio, "save", save)
    monkeypatch.setattr(J, "save_journal_state", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "disk full")))
    with pytest.raises(C.StateSaveError):
        rig.cycle.run_once()
    monkeypatch.undo()
    assert [ln["symbol"] for ln in rig.journal_lines()] == [sold[0]]
    restarted = Rig(tmp_path)
    restarted.scanner.candidates = []
    restarted.cycle.run_once()
    assert [ln["symbol"] for ln in restarted.journal_lines()].count(sold[0]) == 1


def test_news_is_fetched_after_code_exits(tmp_path):
    from test_cycle import FakeMarket, FakeScanner, Rig

    order: list[str] = []

    class DeferScanner(FakeScanner):
        def scan(self, state, now_ms, *, headline_polarity=None, planned_notional=None, include_news=True):
            order.append(f"scan:news={include_news}")
            return super().scan(state, now_ms, headline_polarity=headline_polarity,
                                planned_notional=planned_notional)

        def fetch_news(self, symbols):
            order.append("news")
            return []

    rig = Rig(tmp_path, scanner=None)
    rig.scanner = DeferScanner(rig.market, [])
    rig.cycle.scanner = rig.scanner
    real_sell = rig.broker.market_sell
    rig.broker.market_sell = lambda sym, amt: order.append("sell") or real_sell(sym, amt)
    rig.hold_position("ETH/USDT", sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.cycle.run_once()
    assert order[0] == "scan:news=False" and order.index("sell") < order.index("news")


def test_daily_review_runs_after_code_exits(tmp_path):
    from datetime import datetime, timezone

    from test_cycle import Clock, Rig

    rig = Rig(tmp_path, clock=Clock(datetime(2026, 10, 29, 0, 30, tzinfo=timezone.utc)))
    rig.cycle.journal_state.last_daily_review_date = "2026-10-27"          # 어제(28일) 리뷰가 남음
    order: list[str] = []
    real_sell = rig.broker.market_sell
    rig.broker.market_sell = lambda sym, amt: order.append("sell") or real_sell(sym, amt)
    real_complete = rig.backend.complete

    def complete(**k):
        if (k.get("user_payload") or {}).get("mode") == "daily":
            order.append("daily")
        return real_complete(**k)

    rig.backend.complete = complete
    rig.hold_position("ETH/USDT", sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.scanner.candidates = []
    rig.cycle.run_once()
    assert "sell" in order and ("daily" not in order or order.index("sell") < order.index("daily")), order


def test_offline_fixture_log_file_goes_to_isolated_dir(tmp_path):
    s = load_settings(env_for(tmp_path, LOG_FILE="logs/bot.log"))
    iso = M.offline_fixture_settings(s)
    assert iso.data_dir == tmp_path / "data" / M.OFFLINE_FIXTURE_DIRNAME
    assert iso.log_file == iso.data_dir / "logs" / "bot.log"
    absolute = load_settings(env_for(tmp_path, LOG_FILE=str(tmp_path / "elsewhere.log")))
    assert M.offline_fixture_settings(absolute).log_file == tmp_path / "elsewhere.log"



# ======================================================================= 대시보드: 큰 로그 끝부분만 읽기
def test_dashboard_tail_read_matches_full_read(tmp_path):
    from tradingbot.dashboard import app as A

    p = tmp_path / "decisions.jsonl"
    rows = [json.dumps({"i": i, "note": "결정 " * (i % 7)}, ensure_ascii=False) for i in range(5000)]
    rows.insert(4990, "{not json")                     # 끝부분의 깨진 줄
    rows.insert(4995, json.dumps([1, 2]))              # dict 가 아닌 줄
    p.write_bytes(("\n".join(rows) + "\n").encode("utf-8"))
    full = A._parse_jsonl_lines(p.read_text(encoding="utf-8").splitlines())
    for limit in (1, 10, 100, 4999, 5000, 6000):
        assert A._read_jsonl(p, limit) == full[-limit:]
    assert A._read_jsonl(p, 0) == [] and A._read_jsonl(p) == full


def test_dashboard_tail_read_bounded_and_handles_partial_last_line(tmp_path):
    from tradingbot.dashboard import app as A

    p = tmp_path / "decisions.jsonl"
    line = json.dumps({"x": "y" * 200}) + "\n"
    with p.open("wb") as fh:
        for _ in range(60_000):                         # 약 12 MB
            fh.write(line.encode("utf-8"))
        fh.write(b'{"ts": 2, "reason": "\xed\x95')     # 크래시로 잘린 마지막 줄(멀티바이트 중간)
    reads: list[int] = []
    real_open = Path.open

    def counting_open(self, mode="r", *a, **k):
        fh = real_open(self, mode, *a, **k)
        if self == p and "b" in mode:
            orig = fh.read
            fh.read = lambda n=-1: (reads.append(n), orig(n))[1]     # type: ignore[method-assign]
        return fh

    import unittest.mock as um
    with um.patch.object(Path, "open", counting_open):
        out = A._read_jsonl(p, 100)
    assert len(out) == 100 and all(r == {"x": "y" * 200} for r in out)
    assert reads and sum(reads) <= 256 * 1024          # 12 MB 전체가 아니라 끝부분만


def test_dashboard_polls_sequentially_and_bounds_shutdown():
    html = (Path(__file__).resolve().parents[1] / "tradingbot" / "dashboard" / "static" / "index.html").read_text(
        encoding="utf-8")
    assert "setInterval(refresh" not in html and "setTimeout(loop, POLL_MS)" in html
    src = (Path(__file__).resolve().parents[1] / "tradingbot" / "main.py").read_text(encoding="utf-8")
    assert "timeout_graceful_shutdown=5" in src


def test_requirements_txt_is_ascii():
    """pip < 25(Python 3.11 전부·3.12.0~3.12.9·3.13.0~3.13.2 에 딸린 pip)는 requirements 파일을 로캘 인코딩으로 읽는다 —
    한국어 Windows(cp949)에서 UTF-8 한글 주석이 있으면 ``pip install -r requirements.txt`` 가 UnicodeDecodeError 로 멈춘다."""
    root = Path(__file__).resolve().parents[1]
    assert (root / "requirements.txt").read_bytes().isascii()


def test_ledger_follower_reads_only_appended_lines(tmp_path, monkeypatch):
    from tradingbot.dashboard import app as A

    p = tmp_path / "cost_ledger.jsonl"
    p.write_text("".join(json.dumps({"date": "2026-10-29", "cost_usd": 0.1, "i": i}) + "\n" for i in range(3)),
                 encoding="utf-8")
    f = A._JsonlFollower(p)
    assert [r["i"] for r in f.read()] == [0, 1, 2]
    parsed: list[int] = []
    real = A._parse_jsonl_lines
    monkeypatch.setattr(A, "_parse_jsonl_lines", lambda lines: (parsed.append(len(lines)), real(lines))[1])
    with p.open("ab") as fh:
        fh.write((json.dumps({"date": "2026-10-29", "cost_usd": 0.1, "i": 3}) + "\n").encode())
        fh.write(json.dumps({"date": "2026-10-29", "cost_usd": 0.1, "i": 4}).encode())      # 아직 줄바꿈 없음
    assert [r["i"] for r in f.read()] == [0, 1, 2, 3, 4]
    assert parsed == [1, 1]                                   # 새 줄 1개 + 끝 조각 1개만 파싱
    with p.open("ab") as fh:
        fh.write(b"\n")
    assert [r["i"] for r in f.read()] == [0, 1, 2, 3, 4]
    # 다시 쓰인 파일(크기는 더 커도 내용이 다름) → 처음부터
    p.write_text("".join(json.dumps({"date": "2026-10-29", "cost_usd": 1.0, "j": j}) + "\n" for j in range(9)),
                 encoding="utf-8")
    assert [r["j"] for r in f.read()] == list(range(9))
    # 지난달 레코드는 keep 으로 버린다
    with p.open("ab") as fh:
        fh.write((json.dumps({"date": "2026-09-30", "cost_usd": 5.0}) + "\n").encode())
    assert all(r["date"].startswith("2026-10") for r in f.read(lambda r: r["date"].startswith("2026-10")))
    p.unlink()
    assert f.read() == []


def test_dashboard_costs_follow_appends(tmp_path):
    from fastapi.testclient import TestClient

    from tradingbot.dashboard import create_app

    data = tmp_path / "data"
    settings = load_settings({"AGENT_BACKEND": "mock", "DATA_DIR": str(data)})
    client = TestClient(create_app(settings, data))
    assert client.get("/api/costs").json()["today_usd"] == 0
    led = CostLedger(data / "cost_ledger.jsonl", 10, 200)
    c1 = led.record(role="head", model="claude-opus-5-5", input_tokens=1000, cached_tokens=0, output_tokens=100)
    assert client.get("/api/costs").json()["today_usd"] == pytest.approx(c1)
    c2 = led.record(role="head", model="claude-opus-5-5", input_tokens=500, cached_tokens=0, output_tokens=50)
    assert client.get("/api/costs").json()["today_usd"] == pytest.approx(c1 + c2)

