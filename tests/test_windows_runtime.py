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
    clock[0] += 8 * 3600 * 1000           # 8시간 절전
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
