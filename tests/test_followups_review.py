"""Adversarial review of the follow-up diff (on top of 62c0b7f).

Each test pins a defect found in the follow-up changes. They are expected to FAIL until fixed.
No external network: the Fear & Greed test talks only to a loopback server it starts itself.
"""
from __future__ import annotations

import errno
import socket
import threading
import time
from pathlib import Path

import pytest

from tradingbot import gates
from tradingbot import main as M
from tradingbot.agents.roles import ROLES
from tradingbot.agents.schemas import HeadDecision, RiskCandidateVerdict, Thesis
from tradingbot.features import RegimePack, RiskPack
from tradingbot.models import TradeEvent
from tradingbot.notify import Notifier
from tradingbot.scanner import LiquidityGate

SYM = "ETH/USDT"
NOW_MS = 1_793_000_000_000


# ----------------------------------------------------------------------------- helpers
def _verdict(**kw) -> RiskCandidateVerdict:
    base = dict(symbol=SYM, verdict="approve", veto_reasons=[], conditions=[], max_size_pct=10.0, stop_pct=1.5,
                take_profit_pct=3.2, rr_net_estimate=1.6, bull_point_reliability=[], required_monitoring=[],
                correlation_note="정상")
    base.update(kw)
    return RiskCandidateVerdict(**base)


def _head(**kw) -> HeadDecision:
    base = dict(symbol=SYM, action="BUY", confidence=0.9, size_pct=10.0, stop_pct=1.5, tp_pct=3.2,
                thesis=Thesis(reasons=["추세 지속"], assumptions=["BTC 상승 레짐"], red_lines=[], horizon_bars=16),
                disagreement_class="none", losing_side_strongest_point="펀딩", decisive_factor="mtf",
                rationale="근거")
    base.update(kw)
    return HeadDecision(**base)


def _g3(rp: RiskPack, liq: LiquidityGate, notional: float, stop_pct: float) -> gates.GateDecision:
    return gates.g3_risk(SYM, _verdict(), RegimePack(), "healthy",
                         {"disposition": "none", "overtrading": "none", "chasing": "none", "anchoring": "none"},
                         rp, liq, notional, stop_pct)


def _g4_like_cycle(rp: RiskPack, head_stop: float):
    """cycle._buy 와 똑같이 g4_final 에 ``exec_cost_pct=risk_pack.exec_cost_pct`` 만 넘긴다."""
    return gates.g4_final(_head(stop_pct=head_stop), _verdict(), 0.65, 1.0, 10_000.0, 100.0, 0.5, 10_000.0,
                          tech_stop_pct=None, exec_cost_pct=getattr(rp, "exec_cost_pct", None))


# ============================================================ 1. G4 exec-cost recheck is weaker than G3
def test_g4_recheck_uses_weaker_cost_than_g3_scanner_spread():
    """G3 cost = max(rp.exec_cost_pct, impact/100 + max(rp.spread, liquidity.spread)/200 + 0.2).
    G4 re-check only sees rp.exec_cost_pct, so a worse scanner spread that G3 counted is forgotten after the
    stop is tightened -> a trade whose (G3-measured) cost exceeds 25% of the FINAL stop still executes."""
    rp = RiskPack(symbol=SYM, spread_bps=3.0, depth_taker_usdt=40_000.0, impact_bps_est=2.5, exec_cost_pct=0.24,
                  cvar_size_pct=10.0, order_notional_vs_15m_volume_share=0.002)
    liq = LiquidityGate(symbol=SYM, ok=True, reasons=[], spread_bps=18.0, quote_volume_24h=500_000_000.0,
                        depth_1pct_usdt=40_000.0)
    g3 = _g3(rp, liq, 1_000.0, 1.5)
    assert g3.passed, g3.reasons
    g3_cost = 2.5 / 100 + 18.0 / 200 + 0.20            # 0.315% — the cost G3 itself measured
    head_stop = 1.2                                     # head tightens the stop after G3
    assert g3_cost > gates.G3_EXEC_COST_STOP_SHARE * head_stop   # 0.315 > 0.30 -> must HOLD
    d, plan = _g4_like_cycle(rp, head_stop)
    assert plan is None, f"G4 passed with cost {g3_cost:.3f}% > 25% of final stop {head_stop}% (notes={d.notes})"


def test_g4_recheck_skipped_when_risk_pack_exec_cost_missing_but_g3_computed_cost():
    """RiskPack.exec_cost_pct is None (risk-pack spread missing) but G3 computed a cost from impact +
    liquidity spread and passed. Cycle passes None -> G4 silently skips the re-check entirely (no note)."""
    rp = RiskPack(symbol=SYM, spread_bps=None, depth_taker_usdt=40_000.0, impact_bps_est=12.5, exec_cost_pct=None,
                  cvar_size_pct=10.0, order_notional_vs_15m_volume_share=0.002)
    liq = LiquidityGate(symbol=SYM, ok=True, reasons=[], spread_bps=3.0, quote_volume_24h=500_000_000.0,
                        depth_1pct_usdt=40_000.0)
    g3 = _g3(rp, liq, 1_000.0, 2.0)
    assert g3.passed, g3.reasons
    g3_cost = 12.5 / 100 + 3.0 / 200 + 0.20            # 0.34%
    head_stop = 1.2
    assert g3_cost > gates.G3_EXEC_COST_STOP_SHARE * head_stop
    d, plan = _g4_like_cycle(rp, head_stop)
    assert plan is None, f"G4 skipped exec-cost recheck (cost {g3_cost:.3f}% vs final stop {head_stop}%)"


# ============================================================ 2. role timeouts vs actual LLM budget
def test_sequential_role_timeouts_fit_llm_deadline_budget():
    """The LLM budget handed to the backend is CYCLE_DEADLINE_S - LLM_DEADLINE_RESERVE_S (minus scan time),
    not CYCLE_DEADLINE_S. Old timeouts summed to 270 s (<= 275); head 100->120 makes it 290 s > 275 s, so
    whenever the earlier stages run long the head (the only stage that can BUY) is cut short / skipped."""
    from tradingbot import cycle as C

    stages = [("technical", "derivatives_flow", "news_sentiment"), ("bull", "bear"), ("risk_manager",), ("head",)]
    worst = sum(max(ROLES[r].timeout_s for r in st) for st in stages)
    assert worst <= C.CYCLE_DEADLINE_S - C.LLM_DEADLINE_RESERVE_S, worst


# ============================================================ 3. FearGreed blocks the scan past its timeout
def test_fear_greed_default_http_get_total_time_bounded_by_timeout(monkeypatch):
    """urllib's timeout is per socket operation; a server that drips bytes keeps the (synchronous) scan blocked
    far longer than ``timeout_s``. The fetcher promises a bounded, non-blocking call inside the cycle."""
    from tradingbot import sentiment_sources as S

    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 40\r\n\r\n")
                for ch in b'{"data": [{"value": "34"}]}            ':
                    if stop.is_set():
                        return
                    conn.sendall(bytes([ch]))
                    time.sleep(0.12)
            except OSError:
                return

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    timeout_s = 0.5
    f = S.FearGreedFetcher(cache_path=None, url=f"http://127.0.0.1:{port}/fng", timeout_s=timeout_s,
                           http_get=S.default_http_get)
    started = time.monotonic()
    try:
        f()
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        srv.close()
    assert elapsed < timeout_s * 3, f"fear&greed call blocked the scan {elapsed:.1f}s (timeout_s={timeout_s})"


# ============================================================ 4. lock staleness: dangerous direction
def test_fresh_lock_from_other_host_is_not_stolen(tmp_path):
    """Shared DATA_DIR (NFS / two containers on one volume): host A's lock was refreshed 1 s ago. The new rule
    treats ANY foreign host as stale regardless of a fresh ``ts`` -> host B takes the lock and both trade.
    For a foreign host the PID check is meaningless; the fresh ts is the only liveness signal and must win."""
    p = tmp_path / "bot.lock"
    a = M.BotLock(p, pid=111, pid_alive=lambda _: True, clock_ms=lambda: NOW_MS, hostname=lambda: "host-a")
    a.acquire()
    b = M.BotLock(p, pid=222, pid_alive=lambda _: False, clock_ms=lambda: NOW_MS + 1_000,
                  hostname=lambda: "host-b")
    with pytest.raises(M.LockHeldError):
        b.acquire()


def test_failed_lock_refresh_is_swallowed_so_live_bot_lock_goes_stale(tmp_path):
    """A live bot whose ts refresh keeps failing (OSError: disk full / tmp path unwritable) only logs a warning
    (both in the wait loop and in the cycle's 'lock' stage) and keeps trading. Before the follow-up this was
    harmless; with ts-staleness a second instance steals the lock after 45 min while the first is alive."""
    p = tmp_path / "bot.lock"
    clock = [NOW_MS]
    stale_s = M.lock_stale_after_s(15)
    a = M.BotLock(p, pid=111, pid_alive=lambda _: True, clock_ms=lambda: clock[0], hostname=lambda: "h",
                  stale_after_s=stale_s)
    a.acquire()

    def broken_write():
        raise OSError(errno.ENOSPC, "No space left on device")

    a._write = broken_write  # type: ignore[method-assign]

    def wait_fn(s: float) -> bool:
        clock[0] += int(s * 1000)
        return False

    raised = False
    try:
        M._wait_with_lock_refresh(wait_fn, stale_s + 600, a, M.LOCK_REFRESH_S)
    except Exception:  # noqa: BLE001 - stopping the live bot would be an acceptable fix
        raised = True
    if raised:
        return
    b = M.BotLock(p, pid=222, pid_alive=lambda _: True, clock_ms=lambda: clock[0], hostname=lambda: "h",
                  stale_after_s=stale_s)
    with pytest.raises(M.LockHeldError):
        b.acquire()      # live bot 111 never stopped, yet its lock is taken -> two instances


def test_hostname_change_does_not_kill_live_bot(tmp_path):
    """Same machine, hostname changes at runtime (macOS Bonjour rename 'mac.local' -> 'mac-2.local', DHCP).
    refresh() now compares the file's host with the *current* hostname and raises LockLostError -> the running
    bot exits fatally (positions stop being managed) although nobody else took the lock."""
    p = tmp_path / "bot.lock"
    names = ["mac.local"]
    a = M.BotLock(p, pid=111, pid_alive=lambda _: True, clock_ms=lambda: NOW_MS, hostname=lambda: names[0])
    a.acquire()
    names[0] = "mac-2.local"
    a.refresh()          # file pid is still ours; must not raise


# ============================================================ 5. Telegram info lost vs before
def test_halt_message_keeps_detail_previous_error_format_kept():
    """Before: G0 halt went out as kind=error, body = reason truncated at 500 chars (cycle caps at 500).
    Now _format_halt truncates the detail at 300 -> the tail of multi-code halt reasons is dropped."""
    reason = "신규 진입 중단 (G0): " + "당일 실현손익 −3.10% ≤ 한도 " * 14 + "TAIL_CONSECUTIVE_LOSSES_DETAIL"
    assert len(reason) <= 500
    e = TradeEvent(kind="halt", ts=NOW_MS, mode="paper", reason=reason, equity=9_700.0,
                   extra={"stage": "g0", "code": "halt", "halt_codes": ["daily_loss_limit", "consecutive_losses"]})
    text = Notifier.format(e)
    assert "TAIL_CONSECUTIVE_LOSSES_DETAIL" in text, text
