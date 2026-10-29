"""SYSTEM REVIEW — persistence, crash safety, ordering (failing proof tests).

Each test simulates a crash (a ``BaseException`` that escapes every ``except Exception`` stage guard, i.e. the
process dies at that point) or an auxiliary failure, then "restarts" by building a fresh ``Rig`` over the same
data directory (``Portfolio.load`` + ``load_journal_state`` from disk), and checks the invariant that should
survive. All offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cycle import (  # noqa: F401 - reuse the cycle rig
    Clock,
    Rig,
    _debias,
    happy,
)
from tradingbot import journal as J
from tradingbot.agents.schemas import HeadDecision, HeadOutput, RedLine, Thesis
from tradingbot.llm.cost import CostLedger
from tradingbot.models import Position


class _Crash(BaseException):
    """Process death at this exact point (not caught by stage guards)."""


SYM = "ETH/USDT"


def _head_with_red_line(conf: float = 0.8):
    """head: BUY every approved candidate with a red line ``unrealized_pct < -0.5``."""
    def _f(p: dict) -> HeadOutput:
        decisions = [HeadDecision(
            symbol=a["symbol"], action="BUY", confidence=conf, size_pct=5.0, stop_pct=1.0, tp_pct=2.25,
            thesis=Thesis(reasons=["추세 지속"], assumptions=["BTC 안정", "거래량 유지"],
                          red_lines=[RedLine(metric="unrealized_pct", op="<", value=-0.5)], horizon_bars=16),
            disagreement_class="horizon", losing_side_strongest_point="BTC 의존", decisive_factor="mtf",
            rationale="다중 시간대 일치 추세") for a in p["approved"]]
        return HeadOutput(decisions=decisions, debias=_debias(), reevaluation_triggers=[], hold_reasons=[],
                          cycle_note="테스트")
    return _f


def _position_records(rig: Rig, symbol: str) -> list[dict]:
    return [r for r in rig.decisions() if r.get("kind") == "position" and r.get("symbol") == symbol]


# ======================================================================= journal_state vs positions
def test_crash_after_entry_state_save_keeps_red_lines_active_after_restart(tmp_path, monkeypatch):
    """Crash between ``portfolio.save()`` and ``save_journal_state`` in ``_buy``.

    state.json holds the position (and ``Position.thesis`` holds the full thesis JSON incl. red lines), but
    journal_state.json never got ``thesis[symbol]``. After restart the pipeline only evaluates red lines from
    ``journal_state.thesis`` and there is no reconciliation from ``Position.thesis`` — so red lines are silently
    disabled for the life of the position (also happens whenever journal_state.json is corrupt and quarantined).
    """
    clock = Clock()
    rig1 = Rig(tmp_path, happy(head=_head_with_red_line()), clock=clock)
    real_save = J.save_journal_state

    def crashing_save(state, data_dir):
        if SYM in state.thesis:          # the save right after the entry state save
            raise _Crash("process died after state.json save, before journal_state save")
        return real_save(state, data_dir)

    monkeypatch.setattr(J, "save_journal_state", crashing_save)
    with pytest.raises(_Crash):
        rig1.cycle.run_once()
    monkeypatch.setattr(J, "save_journal_state", real_save)

    saved = json.loads((rig1.data / "state.json").read_text(encoding="utf-8"))
    assert SYM in saved["positions"]                                   # fill was persisted
    assert json.loads(saved["positions"][SYM]["thesis"])["red_lines"]  # thesis is recoverable from state

    # restart
    clock.advance(900)
    rig2 = Rig(tmp_path, happy(head=_head_with_red_line()), clock=clock, candidates=())
    pos = rig2.state.positions[SYM]
    # mark 0.8% below entry: above the 1% stop, below the -0.5% red line
    rig2.market.prices[SYM] = pos.entry_price * 0.992
    rig2.cycle.run_once()
    assert SYM in rig2.state.positions
    recs = _position_records(rig2, SYM)
    assert recs, "held position must be reviewed by the pipeline"
    assert recs[-1]["red_line_hits"], (
        "red line unrealized_pct < -0.5 is breached but not evaluated: journal_state.thesis lost on crash and "
        "never reconciled from Position.thesis")


def test_crash_after_exit_state_save_leaves_no_orphan_journal_entries(tmp_path, monkeypatch):
    """Crash after the exit's ``portfolio.save()`` but before ``_post_trade`` finished (journal cleanup + save).

    After restart, journal_state still holds ``thesis``/``entry_verdicts``/``thesis_history`` for a symbol that
    is no longer held, and nothing ever cleans them (SPEC v2.1: entries are cleared after the journal call).
    """
    clock = Clock()
    rig1 = Rig(tmp_path, clock=clock)
    rig1.cycle.run_once()
    assert SYM in rig1.state.positions
    assert SYM in J.load_journal_state(rig1.data).thesis

    pos = rig1.state.positions[SYM]
    rig1.scanner.candidates = []
    rig1.market.prices[SYM] = pos.stop_loss_price * 0.99
    clock.advance(900)

    def crash(*a, **k):
        raise _Crash("process died during post-trade journaling")

    monkeypatch.setattr(J, "post_trade_review", crash)
    monkeypatch.setattr(J, "refresh_stats", crash)
    with pytest.raises(_Crash):
        rig1.cycle.run_once()
    monkeypatch.undo()
    saved = json.loads((rig1.data / "state.json").read_text(encoding="utf-8"))
    assert saved["positions"] == {}                                  # exit persisted

    # restart and run a normal cycle with nothing to trade
    clock.advance(900)
    rig2 = Rig(tmp_path, clock=clock, candidates=())
    rig2.cycle.run_once()
    js = rig2.cycle.journal_state
    held = set(rig2.state.positions)
    orphans = {k for d in (js.thesis, js.entry_verdicts, js.thesis_history) for k in d if k not in held}
    assert not orphans, f"orphan journal entries for symbols no longer held: {sorted(orphans)}"


# ======================================================================= fill -> save -> notify
def test_entry_notify_and_save_survive_journal_bookkeeping_error(tmp_path, monkeypatch):
    """Journal bookkeeping sits between ``open_position`` and ``save()``/notify in ``_buy`` with no guard.

    If it raises, the stage guard swallows the error: the real fill is in memory only (no immediate save) and
    the ``entry`` Telegram event is never sent — violating "every trade event goes to Telegram" and
    "fill -> state save -> notify".
    """
    rig = Rig(tmp_path)

    def boom(**kw):
        raise RuntimeError("journal bug")

    monkeypatch.setattr(J, "make_entry_verdicts", boom)
    saves_with_position: list[bool] = []
    real_save = rig.portfolio.save

    def spy_save():
        saves_with_position.append(SYM in rig.portfolio.state.positions)
        return real_save()

    rig.portfolio.save = spy_save
    sent_before_end: list[int] = []
    real_write = rig.cycle._write_decisions

    def spy_write(run):
        sent_before_end.append(len(rig.sent_with("진입")))
        return real_write(run)

    rig.cycle._write_decisions = spy_write
    rig.cycle.run_once()
    assert SYM in rig.state.positions                        # the buy was filled and booked
    assert rig.sent_with("진입"), "entry fill was never notified"
    assert sent_before_end and sent_before_end[0] >= 1
    assert saves_with_position and saves_with_position[0] is True


# ======================================================================= control files / kill switch
def test_drawdown_clear_survives_crash_before_mark_update(tmp_path):
    """CLEAR of a drawdown kill: ``clear_kill`` is saved in ``_process_control`` but the peak reset is a
    run-local flag applied later in ``_update_marks``. A crash in between persists "kill cleared, old peak",
    so after restart the drawdown kill fires again and the human's CLEAR is lost.
    """
    clock = Clock()
    rig1 = Rig(tmp_path, clock=clock)
    rig1.state.peak_equity = 11_000.0                 # equity 10,000 → −9%
    rig1.portfolio.set_kill("drawdown_stop_all")
    rig1.portfolio.save()
    ctl = rig1.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "CLEAR").write_text(json.dumps({"reason": "사람 확인", "ts": clock.ms()}), encoding="utf-8")

    def die():
        raise _Crash("process died during scan")

    rig1.scanner.hook = die
    with pytest.raises(_Crash):
        rig1.cycle.run_once()
    assert not (ctl / "CLEAR").exists()               # CLEAR consumed

    clock.advance(900)
    rig2 = Rig(tmp_path, clock=clock, candidates=())
    rig2.cycle.run_once()
    assert rig2.state.kill_switch is False, (
        f"operator CLEAR lost after crash: kill re-fired ({rig2.state.kill_reason}), "
        f"peak={rig2.state.peak_equity}")


def test_stop_kill_notification_not_lost_when_control_unlink_fails(tmp_path, monkeypatch):
    """``_process_control`` does save → unlink → notify. If unlinking STOP fails once, the stage guard skips the
    notify; next cycle the kill is already set with the same reason so no event is produced — the kill_switch
    Telegram message is lost forever.
    """
    rig = Rig(tmp_path)
    ctl = rig.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    stop = ctl / "STOP"
    stop.write_text(json.dumps({"reason": "수동", "ts": rig.clock.ms()}), encoding="utf-8")
    real_unlink = Path.unlink
    fails = {"n": 0}

    def flaky_unlink(self, missing_ok=False):
        if self == stop and fails["n"] == 0:
            fails["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    rig.cycle.run_once()
    rig.clock.advance(900)
    rig.cycle.run_once()
    assert rig.state.kill_switch is True
    assert rig.sent_with("킬스위치 발동"), "kill switch engaged but never notified"


# ======================================================================= cost ledger crash safety
def test_cost_ledger_record_after_torn_tail_is_restored(tmp_path):
    """A crash mid-append leaves a torn last line without ``\\n``. The next ``record()`` appends directly onto
    it, so the new (valid) record is merged into the garbage line and dropped on restore → spend undercounted
    after the next restart (budget guard weakened).
    """
    p = tmp_path / "cost_ledger.jsonl"
    led = CostLedger(p, 10.0, 100.0)
    led.record(role="head", model="claude-opus-5-5", input_tokens=100_000, cached_tokens=0, output_tokens=10_000)
    # crash mid-write of the next line
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"cached_tokens": 0, "cost_usd": 0.5, "da')
    led2 = CostLedger(p, 10.0, 100.0)
    before = led2.spent_today()
    cost = led2.record(role="head", model="claude-opus-5-5", input_tokens=100_000, cached_tokens=0,
                       output_tokens=10_000)
    led3 = CostLedger(p, 10.0, 100.0)
    assert led3.spent_today() == pytest.approx(before + cost), "record appended after a torn line was lost"
