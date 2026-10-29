"""main.py (SPEC §15·§22) — CLI 인자·live 확인 입력·잠금·status·루프·신호 (전부 오프라인)."""
from __future__ import annotations

import io
import json
import os
import signal
import threading
from pathlib import Path

import pytest

from tradingbot import cycle as C
from tradingbot import main as M
from tradingbot.config import load_settings
from tradingbot.models import DailyStats, Fill, Position
from tradingbot.portfolio import Portfolio

SECRET = "LiveSecretKeyValue-ABCDEFG123456"


def env_for(tmp_path: Path, **extra) -> dict:
    env = {"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "data"), "SYMBOLS": "ETH/USDT"}
    env.update(extra)
    return env


def live_env(tmp_path: Path) -> dict:
    return env_for(tmp_path, MODE="live", CONFIRM_LIVE="YES", BINANCE_LIVE_API_KEY=SECRET,
                   BINANCE_LIVE_API_SECRET=SECRET + "x")


def call(argv, tmp_path, *, env=None, input_fn=None, factory=None, stop_event=None, pid=None):
    out, err = io.StringIO(), io.StringIO()
    code = M.main(argv, env=env if env is not None else env_for(tmp_path),
                  input_fn=input_fn or (lambda prompt: ""), out=out, err=err, components_factory=factory,
                  stop_event=stop_event, pid=pid)
    return code, out.getvalue(), err.getvalue()


# ======================================================================= 가짜 구성 요소
class FakeNotifier:
    def __init__(self) -> None:
        self.events = []
        self.flushed = 0

    def notify(self, event) -> None:
        self.events.append(event)

    def flush(self, timeout_s: float = 5.0) -> None:
        self.flushed += 1

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class FakeSummary:
    def __init__(self, n: int) -> None:
        self.cycle = n
        self.errors: list[str] = []


class FakeCycle:
    def __init__(self, *, raise_on: dict | None = None, stop_after: int | None = None,
                 stop_event: threading.Event | None = None) -> None:
        self.n = 0
        self.raise_on = raise_on or {}
        self.stop_after = stop_after
        self.stop_event = stop_event

    def run_once(self):
        self.n += 1
        exc = self.raise_on.get(self.n)
        if self.stop_after is not None and self.n >= self.stop_after and self.stop_event is not None:
            self.stop_event.set()
        if exc is not None:
            raise exc
        return FakeSummary(self.n)


class FakePortfolio:
    def __init__(self, settings) -> None:
        from tradingbot.models import BotState
        self.state = BotState(mode=settings.mode, cycle=0, positions={}, paper_quote_balance=10_000.0,
                              peak_equity=10_000.0, daily=DailyStats(date="2026-10-28", start_equity=10_000.0))

    def equity(self, marks, quote_balance=None):
        return self.state.paper_quote_balance


def fake_components(settings, cycle=None, notifier=None) -> M.Components:
    return M.Components(settings=settings, market=None, broker=None, portfolio=FakePortfolio(settings), backend=None,
                        ledger=None, notifier=notifier or FakeNotifier(), skills=None, cycle=cycle or FakeCycle())


class Factory:
    def __init__(self, **kw) -> None:
        self.kw = kw
        self.calls = 0
        self.comp: M.Components | None = None
        self.lock = None

    def __call__(self, settings, *, lock=None):
        self.calls += 1
        self.lock = lock
        self.comp = fake_components(settings, **self.kw)
        return self.comp


# ======================================================================= 인자 파싱
def test_parser_run_flags():
    p = M.build_parser()
    a = p.parse_args(["run", "--once", "--cycle-minutes", "5"])
    assert a.command == "run" and a.once is True and a.cycle_minutes == 5
    a = p.parse_args(["run"])
    assert a.once is False and a.cycle_minutes is None


def test_parser_other_commands():
    p = M.build_parser()
    a = p.parse_args(["backtest", "--days", "30", "--symbols", "BTC/USDT", "ETH/USDT"])
    assert a.command == "backtest" and a.days == 30 and a.symbols == ["BTC/USDT", "ETH/USDT"]
    assert p.parse_args(["backtest"]).days == 90
    assert p.parse_args(["stop", "--reason", "점검"]).reason == "점검"
    for cmd in ("dashboard", "status", "clear-stop"):
        assert p.parse_args([cmd]).command == cmd


def test_main_usage_errors_return_code(tmp_path):
    assert call([], tmp_path)[0] == M.EXIT_USAGE
    assert call(["stop"], tmp_path)[0] == M.EXIT_USAGE          # --reason 필수
    assert call(["bogus"], tmp_path)[0] == M.EXIT_USAGE


def test_main_config_error(tmp_path):
    code, out, err = call(["run", "--once"], tmp_path, env=env_for(tmp_path, MODE="moon"))
    assert code == M.EXIT_USAGE and "설정 오류" in err


def test_status_with_config_error_falls_back_to_files(tmp_path):
    """status 는 stop/clear-stop 처럼 설정 오류여도 DATA_DIR 의 파일만 읽어 출력한다."""
    code, out, err = call(["status"], tmp_path, env=env_for(tmp_path, MODE="moon"))
    assert code == M.EXIT_OK and "설정 경고" in err
    assert "설정 검증 실패" in out and "state.json 없음" in out


def test_cycle_minutes_override_and_validation(tmp_path):
    f = Factory()
    code, _, _ = call(["run", "--once", "--cycle-minutes", "7"], tmp_path, factory=f)
    assert code == M.EXIT_OK and f.comp.settings.cycle_minutes == 7
    code, _, err = call(["run", "--once", "--cycle-minutes", "0"], tmp_path, factory=Factory())
    assert code == M.EXIT_USAGE and "cycle-minutes" in err


def test_parse_symbols():
    assert M._parse_symbols(None, ("ETH/USDT",)) == ["ETH/USDT"]
    assert M._parse_symbols(["btc/usdt,eth/usdt", "BTC/USDT"], ()) == ["BTC/USDT", "ETH/USDT"]


# ======================================================================= live 확인
def test_live_requires_typing_live_refused(tmp_path):
    f = Factory()
    code, out, err = call(["run", "--once"], tmp_path, env=live_env(tmp_path), input_fn=lambda p: "yes", factory=f)
    assert code == M.EXIT_REFUSED
    assert f.calls == 0
    assert "LIVE" in out and "확인 실패" in err
    assert not (tmp_path / "data" / "bot.lock").exists()
    assert SECRET not in out + err


def test_live_refused_on_eof_and_lowercase(tmp_path):
    def eof(prompt):
        raise EOFError
    for fn in (eof, lambda p: "live", lambda p: " LIVE!"):
        f = Factory()
        code, _, _ = call(["run", "--once"], tmp_path, env=live_env(tmp_path), input_fn=fn, factory=f)
        assert code == M.EXIT_REFUSED and f.calls == 0


def test_live_cannot_be_bypassed_by_env(tmp_path):
    env = live_env(tmp_path)
    env.update({"LIVE_CONFIRM": "LIVE", "CONFIRM": "LIVE", "CONFIRM_LIVE_CONSOLE": "LIVE"})
    asked = []
    f = Factory()
    code, _, _ = call(["run", "--once"], tmp_path, env=env, input_fn=lambda p: asked.append(p) or "", factory=f)
    assert asked and code == M.EXIT_REFUSED and f.calls == 0


def test_live_confirmed_proceeds(tmp_path):
    f = Factory()
    code, out, _ = call(["run", "--once"], tmp_path, env=live_env(tmp_path), input_fn=lambda p: "  LIVE \n", factory=f)
    assert code == M.EXIT_OK and f.calls == 1 and f.comp.cycle.n == 1


def test_paper_does_not_ask(tmp_path):
    asked = []
    code, _, _ = call(["run", "--once"], tmp_path, input_fn=lambda p: asked.append(p) or "", factory=Factory())
    assert code == M.EXIT_OK and asked == []


def test_confirm_live_function():
    out = io.StringIO()
    assert M.confirm_live(lambda p: "LIVE", out) is True
    assert M.confirm_live(lambda p: "no", out) is False
    def intr(p):
        raise KeyboardInterrupt
    assert M.confirm_live(intr, out) is False


# ======================================================================= 잠금
def test_lock_acquire_release(tmp_path):
    lk = M.BotLock(tmp_path / "bot.lock", pid=4242, pid_alive=lambda p: True)
    lk.acquire()
    info = json.loads((tmp_path / "bot.lock").read_text(encoding="utf-8"))
    assert info["pid"] == 4242 and "ts" in info
    assert lk.holder()["pid"] == 4242
    lk.refresh()
    lk.release()
    assert not (tmp_path / "bot.lock").exists()
    lk.release()   # 두 번 호출해도 안전


def test_lock_held_by_live_pid(tmp_path):
    a = M.BotLock(tmp_path / "bot.lock", pid=1001, pid_alive=lambda p: True)
    a.acquire()
    b = M.BotLock(tmp_path / "bot.lock", pid=1002, pid_alive=lambda p: True)
    with pytest.raises(M.LockHeldError):
        b.acquire()
    assert a.holder()["pid"] == 1001


def test_lock_stale_takeover(tmp_path):
    a = M.BotLock(tmp_path / "bot.lock", pid=1001)
    a.acquire()
    b = M.BotLock(tmp_path / "bot.lock", pid=1002, pid_alive=lambda p: False)
    b.acquire()
    assert b.holder()["pid"] == 1002 and b.held


@pytest.mark.parametrize("content", ["garbage{", "", "12345"])
def test_lock_corrupt_or_plain_pid_file(tmp_path, content):
    p = tmp_path / "bot.lock"
    p.write_text(content, encoding="utf-8")
    lk = M.BotLock(p, pid=777, pid_alive=lambda pid: False)
    lk.acquire()
    assert lk.holder()["pid"] == 777


def test_lock_reentrant_same_pid(tmp_path):
    a = M.BotLock(tmp_path / "bot.lock", pid=55, pid_alive=lambda p: True)
    a.acquire()
    b = M.BotLock(tmp_path / "bot.lock", pid=55, pid_alive=lambda p: True)
    b.acquire()
    assert b.held


def test_lock_lost_detected_on_refresh(tmp_path):
    a = M.BotLock(tmp_path / "bot.lock", pid=1001)
    a.acquire()
    (tmp_path / "bot.lock").write_text(json.dumps({"pid": 2002}), encoding="utf-8")
    with pytest.raises(C.LockLostError):
        a.refresh()
    a.release()     # 남의 잠금은 지우지 않는다
    assert json.loads((tmp_path / "bot.lock").read_text(encoding="utf-8"))["pid"] == 2002


def test_lock_lost_when_file_deleted(tmp_path):
    a = M.BotLock(tmp_path / "bot.lock", pid=1001)
    a.acquire()
    (tmp_path / "bot.lock").unlink()
    with pytest.raises(C.LockLostError):
        a.refresh()


def test_pid_alive_real():
    assert M._pid_alive(os.getpid()) is True
    assert M._pid_alive(0) is False and M._pid_alive(-5) is False
    assert M._pid_alive(True) is False          # bool 은 PID 가 아니다


def test_pid_alive_windows_branch_never_sends_signals(monkeypatch):
    """Windows 에서 os.kill(pid, 0) 은 CTRL_C_EVENT(콘솔에 Ctrl+C) 이므로 절대 쓰면 안 된다."""
    seen: list[int] = []

    def boom(*a, **kw):
        raise AssertionError("Windows 분기에서 os.kill 호출됨")

    monkeypatch.setattr(M, "_pid_alive_windows", lambda pid: seen.append(pid) or True)
    monkeypatch.setattr(M.os, "kill", boom)
    monkeypatch.setattr(M.os, "name", "nt")
    try:
        assert M._pid_alive(4242) is True
    finally:
        monkeypatch.undo()
    assert seen == [4242]


def test_run_refuses_when_locked(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "bot.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    f = Factory()
    code, _, err = call(["run", "--once"], tmp_path, factory=f, pid=os.getpid() + 100000)
    assert code == M.EXIT_LOCKED and f.calls == 0 and "실행 중" in err


def test_run_takes_stale_lock_and_releases(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "bot.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345}), encoding="utf-8")   # 존재하지 않는 PID
    f = Factory()
    code, _, _ = call(["run", "--once"], tmp_path, factory=f)
    assert code == M.EXIT_OK and f.calls == 1
    assert isinstance(f.lock, M.BotLock)
    assert not (data / "bot.lock").exists()       # 종료 시 해제


# ======================================================================= run 루프
def test_run_once_notifies_startup_and_shutdown(tmp_path):
    f = Factory()
    code, _, _ = call(["run", "--once"], tmp_path, factory=f)
    n = f.comp.notifier
    assert code == M.EXIT_OK
    assert n.kinds()[0] == "startup" and n.kinds()[-1] == "shutdown"
    assert n.flushed == 1
    assert f.comp.cycle.n == 1


def test_run_loop_until_stop_event(tmp_path):
    settings = load_settings(env_for(tmp_path))
    stop = threading.Event()
    comp = fake_components(settings, cycle=FakeCycle(stop_after=3, stop_event=stop))
    t = [1000.0]
    waits = []
    code = M.run_loop(comp, once=False, stop_event=stop, clock=lambda: t[0], monotonic=lambda: t[0],
                      wait=lambda s: waits.append(s) or stop.is_set())
    assert code == M.EXIT_OK and comp.cycle.n == 3
    assert waits == [805.0, 805.0, 805.0]      # 15분 경계 + 5초까지 대기
    assert comp.notifier.kinds()[-1] == "shutdown" and "신호" in comp.notifier.events[-1].reason
    assert comp.notifier.flushed == 1


def test_run_loop_heartbeat_every_4h(tmp_path):
    settings = load_settings(env_for(tmp_path))
    stop = threading.Event()
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 3 * 3600.0
        return clock["t"]
    comp = fake_components(settings, cycle=FakeCycle())
    M.run_loop(comp, once=False, stop_event=stop, clock=lambda: 0.0, monotonic=mono, max_cycles=4,
               wait=lambda s: False)
    hb = [e for e in comp.notifier.events if e.extra.get("heartbeat")]
    assert hb and "하트비트" in hb[0].reason


def test_run_loop_fatal_error_stops(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle(raise_on={1: C.LockLostError("x")}))
    code = M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0, monotonic=lambda: 0.0)
    assert code == M.EXIT_ERROR
    assert comp.notifier.kinds() == ["error", "shutdown"] and comp.notifier.flushed == 1


def test_run_loop_state_save_error_stops(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle(raise_on={1: C.StateSaveError(28, "disk")}))
    assert M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0,
                      monotonic=lambda: 0.0) == M.EXIT_ERROR


def test_run_loop_corrupt_state_stops(tmp_path):
    from tradingbot.portfolio import CorruptStateError
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle(raise_on={1: CorruptStateError("bad")}))
    assert M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0,
                      monotonic=lambda: 0.0) == M.EXIT_ERROR


def test_run_loop_unexpected_error_continues(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle(raise_on={1: RuntimeError("oops")}))
    code = M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0, monotonic=lambda: 0.0,
                      max_cycles=2, wait=lambda s: False)
    assert code == M.EXIT_OK and comp.cycle.n == 2
    assert "error" in comp.notifier.kinds()


def test_run_loop_wait_true_means_shutdown(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle())
    code = M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0, monotonic=lambda: 0.0,
                      wait=lambda s: True)
    assert code == M.EXIT_OK and comp.cycle.n == 1 and comp.notifier.kinds() == ["shutdown"]


def test_next_run_ts_boundaries():
    # 15분 경계 + 5초
    assert M.next_run_ts(0.0, 15) == 5.0
    assert M.next_run_ts(5.0, 15) == 905.0
    assert M.next_run_ts(904.0, 15) == 905.0
    assert M.next_run_ts(906.0, 15) == 1805.0
    assert M.next_run_ts(100.0, 1) == 125.0


def test_signal_handler_sets_stop_event():
    stop = threading.Event()
    prev = M.install_signal_handlers(stop)
    try:
        assert signal.SIGINT in prev
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert stop.is_set()
    finally:
        M._restore_signals(prev)
    assert signal.getsignal(signal.SIGINT) == prev[signal.SIGINT]


def test_signal_handlers_noop_outside_main_thread():
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("prev", M.install_signal_handlers(threading.Event())))
    th.start()
    th.join()
    assert out["prev"] == {}


# ======================================================================= status / stop / clear-stop
def _write_state(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path))
    pf = Portfolio(settings, tmp_path / "data" / "state.json")
    fill = Fill(order_id="p-1", symbol="ETH/USDT", side="buy", base_amount=2.0, price=100.0, quote_amount=200.0,
                fee_quote=0.2, fee_base=0.0, ts=1_793_000_000_000)
    pf.open_position(fill, 98.5, 103.0, "{}", 0.8, 3)
    pf.state.cycle = 7
    pf.state.positions["ETH/USDT"].last_mark = 101.0
    pf.save()


def test_status_prints_summary_without_network(tmp_path, monkeypatch):
    _write_state(tmp_path)

    def no_net(*a, **kw):
        raise AssertionError("status 는 네트워크를 쓰면 안 된다")
    monkeypatch.setattr("tradingbot.exchange.CcxtMarketData.__init__", no_net)
    monkeypatch.setattr("socket.create_connection", no_net)
    code, out, _ = call(["status"], tmp_path)
    assert code == M.EXIT_OK
    assert "모드: paper" in out and "사이클: 7" in out
    assert "ETH/USDT" in out and "98.5" in out and "103" in out
    assert "킬스위치: OFF" in out and "포지션 1개" in out
    assert "자본(추정)" in out and "실행 중 인스턴스: 없음" in out


def test_status_without_state(tmp_path):
    code, out, _ = call(["status"], tmp_path)
    assert code == M.EXIT_OK and "state.json 없음" in out


def test_status_corrupt_state(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text("{broken", encoding="utf-8")
    code, out, _ = call(["status"], tmp_path)
    assert code == M.EXIT_OK and "검증 실패" in out
    assert (tmp_path / "data" / "state.json").exists()     # 읽기 전용: 격리하지 않는다


def test_status_shows_lock_and_costs(tmp_path):
    _write_state(tmp_path)
    data = tmp_path / "data"
    import socket
    import time as _time
    (data / "bot.lock").write_text(json.dumps({"pid": os.getpid(), "ts": int(_time.time() * 1000),
                                               "host": socket.gethostname()}), encoding="utf-8")
    from tradingbot.llm.cost import CostLedger
    CostLedger(data / "cost_ledger.jsonl", 10, 200).record(role="head", model="claude-opus-5-5", input_tokens=1000,
                                                           cached_tokens=0, output_tokens=100)
    code, out, _ = call(["status"], tmp_path)
    assert f"pid={os.getpid()}" in out and "실행 중" in out
    assert "LLM 비용: 오늘 $" in out


def test_stop_and_clear_stop_files(tmp_path):
    code, out, _ = call(["stop", "--reason", "점검 중"], tmp_path)
    stop = tmp_path / "data" / "control" / "STOP"
    assert code == M.EXIT_OK and stop.exists()
    info = json.loads(stop.read_text(encoding="utf-8"))
    assert info["reason"] == "점검 중" and isinstance(info["ts"], int)
    _, out, _ = call(["status"], tmp_path)
    assert "STOP 대기: 점검 중" in out
    code, _, _ = call(["clear-stop"], tmp_path)
    assert code == M.EXIT_OK and not stop.exists()
    assert (tmp_path / "data" / "control" / "CLEAR").exists()
    _, out, _ = call(["status"], tmp_path)
    assert "CLEAR 대기" in out
    # 다시 stop 하면 CLEAR 는 지워진다
    call(["stop", "--reason", "again"], tmp_path)
    assert not (tmp_path / "data" / "control" / "CLEAR").exists()


def test_stop_file_read_by_cycle_format(tmp_path):
    """CLI 가 쓴 STOP 파일을 cycle 의 read_control_file 이 읽는다."""
    M.write_stop(tmp_path, "cli 테스트")
    info = C.read_control_file(tmp_path / "control" / "STOP")
    assert info["reason"] == "cli 테스트" and info["ts"] > 0


# ======================================================================= build_components / backtest
def test_build_components_paper_mock_offline(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = M.build_components(settings)
    from tradingbot.exchange import CcxtMarketData, PaperBroker
    from tradingbot.llm.backend import MockBackend
    assert isinstance(comp.market, CcxtMarketData) and isinstance(comp.broker, PaperBroker)
    assert isinstance(comp.backend, MockBackend) and comp.notifier.transport is None
    assert isinstance(comp.cycle, C.Cycle) and comp.cycle.scanner.news_fetcher is not None
    assert comp.portfolio.path == tmp_path / "data" / "state.json"


def test_build_components_with_telegram(tmp_path):
    settings = load_settings(env_for(tmp_path, TELEGRAM_BOT_TOKEN="123:ABCSECRETTOKEN", TELEGRAM_CHAT_ID="42"))
    comp = M.build_components(settings)
    from tradingbot.notify import TelegramTransport
    assert isinstance(comp.notifier.transport, TelegramTransport)
    assert "ABCSECRETTOKEN" not in repr(comp.notifier.transport)


def test_backtest_command_with_injected_market(tmp_path):
    import argparse

    class R:
        verdict = "NOT VALIDATED"
        reasons = ["거래 수 부족"]
        json_path = tmp_path / "r.json"
    seen = {}

    def fake_run(cfg, market=None, candles=None):
        seen["cfg"] = cfg
        return R()
    import tradingbot.backtest as bt
    orig = bt.run
    bt.run = fake_run
    try:
        settings = load_settings(env_for(tmp_path))
        out, err = io.StringIO(), io.StringIO()
        code = M._cmd_backtest(settings, argparse.Namespace(days=30, symbols=["btc/usdt"]), out, err, market=object())
    finally:
        bt.run = orig
    assert code == M.EXIT_OK and "NOT VALIDATED" in out.getvalue() and "거래 수 부족" in out.getvalue()
    assert seen["cfg"].symbols == ["BTC/USDT"] and seen["cfg"].days == 30
    assert seen["cfg"].out_dir == tmp_path / "data" / "backtest_results"


def test_run_init_failure_releases_lock(tmp_path):
    def bad_factory(settings, *, lock=None):
        raise ValueError("bad init")
    code, _, err = call(["run", "--once"], tmp_path, factory=bad_factory)
    assert code == M.EXIT_ERROR and "초기화 실패" in err
    assert not (tmp_path / "data" / "bot.lock").exists()


def test_run_once_end_to_end_with_real_cycle(tmp_path):
    """실제 Cycle(가짜 시장·mock 백엔드)로 run --once — 상태 파일·잠금 해제·종료 알림."""
    from tradingbot.exchange import PaperBroker
    from tradingbot.llm.backend import MockBackend
    from tradingbot.llm.cost import CostLedger
    from tradingbot.models import Ticker
    from tradingbot.skills import SkillRegistry

    class Mkt:
        def fetch_ticker(self, symbol):
            return Ticker(symbol=symbol, last=100.0, bid=99.99, ask=100.01, quote_volume_24h=1e9,
                          change_24h_pct=0.0, ts=0)

    class Scn:
        def scan(self, state, now_ms, **kw):
            raise RuntimeError("no data")

    notifier = FakeNotifier()

    def factory(settings, *, lock=None):
        data = Path(settings.data_dir)
        pf = Portfolio.load(settings, data / "state.json")
        mkt = Mkt()
        (tmp_path / "sk").mkdir(exist_ok=True)
        cyc = C.Cycle(settings, mkt, PaperBroker(mkt, 10_000), pf, MockBackend(),
                      CostLedger(data / "cost_ledger.jsonl", 10, 200), notifier, SkillRegistry(tmp_path / "sk"),
                      scanner=Scn(), lock=lock)
        return M.Components(settings, mkt, cyc.broker, pf, cyc.backend, cyc.ledger, notifier, cyc.skills, cyc)

    code, _, _ = call(["run", "--once"], tmp_path, factory=factory)
    assert code == M.EXIT_OK
    data = tmp_path / "data"
    assert json.loads((data / "state.json").read_text(encoding="utf-8"))["cycle"] == 1
    assert not (data / "bot.lock").exists()
    assert notifier.kinds()[0] == "startup" and "error" in notifier.kinds() and notifier.kinds()[-1] == "shutdown"


# ======================================================================= PERS-8: 잠금 정체 판정(호스트·ts)
def _write_lock(path: Path, **info) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")


def test_lock_stale_after_s_formula():
    assert M.lock_stale_after_s(15) == 2700.0
    assert M.lock_stale_after_s(5) == 1800.0          # 하한 30분
    assert M.lock_stale_after_s(60) == 3 * 3600.0
    assert M.lock_stale_after_s(None) == 2700.0 and M.lock_stale_after_s(0) == 2700.0


def test_lock_live_pid_but_stale_ts_is_taken_over(tmp_path):
    now = 10_000_000_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001, host="me", ts=now - 2701 * 1000)
    lk = M.BotLock(p, pid=1002, pid_alive=lambda pid: True, clock_ms=lambda: now, hostname=lambda: "me",
                   stale_after_s=M.lock_stale_after_s(15))
    lk.acquire()
    assert lk.held and lk.holder()["pid"] == 1002 and lk.holder()["host"] == "me"


def test_lock_live_pid_fresh_ts_same_host_is_held(tmp_path):
    now = 10_000_000_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001, host="me", ts=now - 2699 * 1000)
    lk = M.BotLock(p, pid=1002, pid_alive=lambda pid: True, clock_ms=lambda: now, hostname=lambda: "me",
                   stale_after_s=2700)
    with pytest.raises(M.LockHeldError):
        lk.acquire()
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == 1001


def test_lock_other_host_fresh_ts_is_held(tmp_path):
    """공유 DATA_DIR: 다른 호스트의 PID 는 검사할 수 없으므로 신선한 ts 가 생존 신호 — 가져오지 않는다."""
    now = 10_000_000_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001, host="old-box", ts=now - 2699 * 1000)
    lk = M.BotLock(p, pid=1002, pid_alive=lambda pid: False, clock_ms=lambda: now, hostname=lambda: "new-box",
                   stale_after_s=2700)
    with pytest.raises(M.LockHeldError, match="old-box"):
        lk.acquire()
    assert json.loads(p.read_text(encoding="utf-8"))["host"] == "old-box"


def test_lock_other_host_stale_ts_is_taken_over(tmp_path, caplog):
    now = 10_000_000_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001, host="old-box", ts=now - 2701 * 1000)
    lk = M.BotLock(p, pid=1002, pid_alive=lambda pid: True, clock_ms=lambda: now, hostname=lambda: "new-box",
                   stale_after_s=2700)
    with caplog.at_level("WARNING", logger="tradingbot.main"):
        lk.acquire()
    assert lk.holder()["pid"] == 1002 and lk.holder()["host"] == "new-box"
    assert any("다른 호스트" in r.getMessage() for r in caplog.records)


def test_lock_other_host_without_ts_is_taken_over(tmp_path):
    now = 10_000_000_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001, host="old-box")
    lk = M.BotLock(p, pid=1002, pid_alive=lambda pid: True, clock_ms=lambda: now, hostname=lambda: "new-box")
    lk.acquire()
    assert lk.holder()["pid"] == 1002


def test_lock_same_pid_other_host_is_not_reentrant(tmp_path):
    """같은 PID 숫자라도 다른 호스트의 잠금이면 내 것이 아니다 — ts 가 신선하면 막고, 낡았으면 가져온다.
    refresh 는 파일의 호스트가 내가 쓴 호스트인지도 본다."""
    now = 10_000_000_000
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001, host="other", ts=now)
    lk = M.BotLock(p, pid=1001, pid_alive=lambda pid: True, clock_ms=lambda: now, hostname=lambda: "me")
    with pytest.raises(M.LockHeldError):
        lk.acquire()
    _write_lock(p, pid=1001, host="other", ts=now - 2701 * 1000)
    lk.acquire()
    assert lk.holder()["host"] == "me"
    _write_lock(p, pid=1001, host="other", ts=now)
    with pytest.raises(C.LockLostError):
        lk.refresh()


def test_lock_host_is_pinned_at_acquire(tmp_path):
    """실행 중 hostname 이 바뀌어도 파일에는 획득 시점 이름을 계속 쓰고 refresh 는 잠금을 잃지 않는다."""
    p = tmp_path / "bot.lock"
    names = ["mac.local"]
    lk = M.BotLock(p, pid=111, pid_alive=lambda _: True, clock_ms=lambda: 10_000_000_000, hostname=lambda: names[0])
    lk.acquire()
    names[0] = "mac-2.local"
    lk.refresh()
    lk.refresh()
    assert lk.holder()["host"] == "mac.local" and lk.held


def test_lock_refresh_write_failure_becomes_fatal_before_lock_goes_stale(tmp_path):
    """ts 쓰기가 계속 실패하면 stale_after_s × LOCK_REFRESH_FAIL_SHARE 전까지는 원래 예외(호출자 WARNING),
    그 뒤에는 LockLostError(다른 인스턴스가 낡은 잠금으로 가져가기 전에 스스로 멈춘다)."""
    import errno

    p = tmp_path / "bot.lock"
    clock = [10_000_000_000]
    lk = M.BotLock(p, pid=111, pid_alive=lambda _: True, clock_ms=lambda: clock[0], hostname=lambda: "h",
                   stale_after_s=2700)
    lk.acquire()

    def broken():
        raise OSError(errno.ENOSPC, "No space left on device")

    lk._write = broken  # type: ignore[method-assign]
    clock[0] += 1000 * int(2700 * M.LOCK_REFRESH_FAIL_SHARE) - 1000
    with pytest.raises(OSError):
        lk.refresh()
    clock[0] += 2000
    with pytest.raises(C.LockLostError, match="갱신"):
        lk.refresh()


def test_lock_legacy_without_host_or_ts_uses_pid_rule(tmp_path):
    p = tmp_path / "bot.lock"
    _write_lock(p, pid=1001)
    with pytest.raises(M.LockHeldError):
        M.BotLock(p, pid=1002, pid_alive=lambda pid: True).acquire()


def test_run_takes_over_live_pid_lock_with_stale_ts(tmp_path):
    """살아 있는 PID(이 테스트 프로세스)라도 ts 가 정체 기준보다 오래되면 run 은 잠금을 가져온다."""
    import socket
    import time as _time
    data = tmp_path / "data"
    _write_lock(data / "bot.lock", pid=os.getpid(), host=socket.gethostname(),
                ts=int(_time.time() * 1000) - 2701 * 1000)
    f = Factory()
    code, _, err = call(["run", "--once"], tmp_path, factory=f, pid=os.getpid() + 100000)
    assert code == M.EXIT_OK and f.calls == 1, err
    assert f.lock.stale_after_s == 2700.0


def test_run_refuses_fresh_lock_from_other_host(tmp_path):
    """공유 DATA_DIR 에서 다른 호스트가 방금 갱신한 잠금은 가져오지 않는다(두 인스턴스 동시 거래 방지)."""
    import time as _time
    data = tmp_path / "data"
    _write_lock(data / "bot.lock", pid=os.getpid(), host="definitely-not-this-host.invalid",
                ts=int(_time.time() * 1000))
    f = Factory()
    code, _, err = call(["run", "--once"], tmp_path, factory=f, pid=os.getpid() + 100000)
    assert code == M.EXIT_LOCKED and f.calls == 0, err


def test_run_takes_over_stale_lock_from_other_host(tmp_path):
    import time as _time
    data = tmp_path / "data"
    _write_lock(data / "bot.lock", pid=os.getpid(), host="definitely-not-this-host.invalid",
                ts=int(_time.time() * 1000) - 2701 * 1000)
    f = Factory()
    code, _, err = call(["run", "--once"], tmp_path, factory=f, pid=os.getpid() + 100000)
    assert code == M.EXIT_OK and f.calls == 1, err


def test_status_marks_stale_lock(tmp_path):
    import socket
    data = tmp_path / "data"
    _write_lock(data / "bot.lock", pid=os.getpid(), host=socket.gethostname(), ts=1_000_000_000_000)
    _, out, _ = call(["status"], tmp_path)
    assert f"pid={os.getpid()} (낡음: 갱신이" in out
    _write_lock(data / "bot.lock", pid=os.getpid(), host="elsewhere.invalid", ts=1_000_000_000_000)
    _, out, _ = call(["status"], tmp_path)
    assert "낡음: 다른 호스트(elsewhere.invalid)의 잠금, 갱신이" in out
    import time as _time
    _write_lock(data / "bot.lock", pid=999_999_999, host="elsewhere.invalid", ts=int(_time.time() * 1000))
    _, out, _ = call(["status"], tmp_path)
    assert "실행 중(다른 호스트 elsewhere.invalid)" in out


class CountingLock:
    def __init__(self, fail_at: int | None = None, exc: BaseException | None = None) -> None:
        self.n = 0
        self.fail_at = fail_at
        self.exc = exc

    def refresh(self) -> None:
        self.n += 1
        if self.fail_at is not None and self.n >= self.fail_at:
            raise self.exc


def test_run_loop_refreshes_lock_while_sleeping(tmp_path):
    settings = load_settings(env_for(tmp_path))
    stop = threading.Event()
    comp = fake_components(settings, cycle=FakeCycle(stop_after=2, stop_event=stop))
    lock = CountingLock()
    waits = []
    code = M.run_loop(comp, once=False, stop_event=stop, clock=lambda: 1000.0, monotonic=lambda: 0.0, lock=lock,
                      wait=lambda s: waits.append(s) or False, max_cycles=2)
    assert code == M.EXIT_OK
    assert waits == [300.0, 300.0, 205.0]          # 805초 대기를 5분 조각으로
    assert lock.n == 2                              # 조각 사이마다 ts 갱신
    assert max(waits) <= M.LOCK_REFRESH_S


def test_run_loop_uses_cycle_lock_by_default(tmp_path):
    settings = load_settings(env_for(tmp_path))
    cyc = FakeCycle()
    cyc.lock = CountingLock()
    comp = fake_components(settings, cycle=cyc)
    waits = []
    M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 1000.0, monotonic=lambda: 0.0,
               wait=lambda s: waits.append(s) or False, max_cycles=2)
    assert waits == [300.0, 300.0, 205.0] and cyc.lock.n == 2


def test_run_loop_lock_lost_while_sleeping_is_fatal(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle())
    lock = CountingLock(fail_at=1, exc=C.LockLostError("taken"))
    code = M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 1000.0, monotonic=lambda: 0.0,
                      lock=lock, wait=lambda s: False, max_cycles=5)
    assert code == M.EXIT_ERROR and comp.cycle.n == 1
    assert comp.notifier.kinds() == ["error", "shutdown"]
    assert comp.notifier.events[0].extra["stage"] == "lock_refresh"


def test_run_loop_lock_refresh_oserror_while_sleeping_continues(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle())
    lock = CountingLock(fail_at=1, exc=OSError("disk"))
    code = M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 1000.0, monotonic=lambda: 0.0,
                      lock=lock, wait=lambda s: False, max_cycles=2)
    assert code == M.EXIT_OK and comp.cycle.n == 2


def test_run_loop_stop_during_chunked_sleep(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings, cycle=FakeCycle())
    lock = CountingLock()
    calls = []
    code = M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 1000.0, monotonic=lambda: 0.0,
                      lock=lock, wait=lambda s: calls.append(s) or len(calls) == 2)
    assert code == M.EXIT_OK and calls == [300.0, 300.0] and lock.n == 1
    assert "신호" in comp.notifier.events[-1].reason


# ======================================================================= 하트비트 kind 'heartbeat'
def test_heartbeat_event_fields(tmp_path):
    settings = load_settings(env_for(tmp_path))
    comp = fake_components(settings)
    st = comp.portfolio.state
    st.cycle = 12
    st.paper_quote_balance = 9_000.0
    st.positions["ETH/USDT"] = Position(symbol="ETH/USDT", base_amount=5.0, entry_price=200.0, quote_spent=1000.0, opened_ts=1,
                                        opened_cycle=3, stop_loss_price=196.0, take_profit_price=208.0,
                                        last_mark=210.0)
    st.daily.realized_pnl = -5.0
    st.daily.llm_cost_usd = 0.75

    class Port(type(comp.portfolio)):
        def equity(self, marks, quote_balance=None):
            return self.state.paper_quote_balance + 5.0 * 210.0
    comp.portfolio.__class__ = Port

    class Ledger:
        def spent_today(self):
            return 1.25
    comp.ledger = Ledger()
    e = M.heartbeat_event(comp)
    assert e.kind == "heartbeat" and e.equity == pytest.approx(10_050.0)
    assert e.pnl_quote == pytest.approx(50.0) and e.pnl_pct == pytest.approx(0.005)
    x = e.extra
    assert x["heartbeat"] is True and x["cycle"] == 12 and x["kill_switch"] is False
    assert x["positions"] == [{"symbol": "ETH/USDT", "unrealized_pct": pytest.approx(0.05),
                               "value_quote": pytest.approx(1050.0)}]
    assert x["exposure_pct"] == pytest.approx(1050.0 / 10_050.0)
    assert x["llm_cost_today_usd"] == 1.25 and x["realized_pnl"] == -5.0
    from tradingbot.notify import Notifier
    text = Notifier.format(e)
    assert text.startswith("💓 하트비트 [paper]") and "ETH/USDT 미실현 +5.00%" in text and "$1.25" in text
    comp.ledger = None
    assert M.heartbeat_event(comp).extra["llm_cost_today_usd"] == 0.75    # 원장 없으면 상태 값


def test_run_loop_heartbeat_kind(tmp_path):
    settings = load_settings(env_for(tmp_path))
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 3 * 3600.0
        return clock["t"]
    comp = fake_components(settings, cycle=FakeCycle())
    M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0, monotonic=mono, max_cycles=4,
               wait=lambda s: False)
    hb = [e for e in comp.notifier.events if e.kind == "heartbeat"]
    assert hb and not [e for e in comp.notifier.events if e.kind == "daily_summary"]
    assert hb[0].extra["positions"] == [] and hb[0].equity == 10_000.0


# ======================================================================= read_env 공개 / Fear&Greed 연결
def test_read_env_public_and_alias(tmp_path, monkeypatch):
    from tradingbot import config
    assert config._read_env is config.read_env
    dotenv = tmp_path / ".env"
    dotenv.write_text("TB_TEST_ONLY_X=from_dotenv\nTB_TEST_ONLY_Y=dotenv_y\n", encoding="utf-8")
    monkeypatch.setenv("TB_TEST_ONLY_Y", "env_wins")
    got = config.read_env(dotenv)
    assert got["TB_TEST_ONLY_X"] == "from_dotenv" and got["TB_TEST_ONLY_Y"] == "env_wins"


def test_build_components_wires_fear_greed_fetcher(tmp_path):
    from tradingbot.sentiment_sources import FearGreedFetcher
    settings = load_settings(env_for(tmp_path))
    comp = M.build_components(settings)
    fg = comp.cycle.scanner.fear_greed_fetcher
    assert isinstance(fg, FearGreedFetcher)
    assert fg.cache_path == tmp_path / "data" / "cache" / "fear_greed.json"


def test_build_components_offline_fixture_has_no_fear_greed(tmp_path):
    settings = M.offline_fixture_settings(load_settings(env_for(tmp_path)))
    comp = M.build_components(settings, offline_fixture=True)
    assert comp.cycle.scanner.fear_greed_fetcher is None


# ======================================================================= 표준출력 인코딩(Windows 리다이렉트)
class _FakeStream:
    def __init__(self, tty: bool):
        self.tty = tty
        self.calls: list[dict] = []

    def isatty(self) -> bool:
        return self.tty

    def reconfigure(self, **kw) -> None:
        self.calls.append(kw)


def test_prepare_stdio_reconfigures_only_redirected_streams():
    piped, console = _FakeStream(tty=False), _FakeStream(tty=True)
    M.prepare_stdio([piped, console, None, object()])
    assert piped.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert console.calls == []


def test_prepare_stdio_never_raises():
    class Broken(_FakeStream):
        def reconfigure(self, **kw):
            raise ValueError("closed")

    M.prepare_stdio([Broken(tty=False)])

