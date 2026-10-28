"""notify.py 적대적 리뷰 테스트 (SPEC §11).

각 테스트는 현재 구현의 결함 하나를 재현한다. 결함이 고쳐지면 통과해야 한다.
오프라인·빠름(총 sleep < 0.2s). 구현 파일은 수정하지 않는다.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tradingbot.models import TradeEvent
from tradingbot.notify import EMOJI, Notifier


class _Opaque:
    """JSON으로 직렬화할 수 없는 임의 객체 (예: 사이클이 extra에 넣은 예외/설정 객체)."""


class _AlwaysFail:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, text: str) -> None:
        self.calls += 1
        raise RuntimeError("telegram down")


class _Blocking:
    """flush 시간 초과를 흉내 내기 위해 release될 때까지 막는 전송기."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def send(self, text: str) -> None:
        self.started.set()
        self.release.wait(2.0)


def _event(**kw) -> TradeEvent:
    base = dict(kind="error", ts=1_700_000_000_000, mode="paper", reason="boom")
    base.update(kw)
    return TradeEvent(**base)


# ---------------------------------------------------------------- 결함 1: 직렬화 불가 extra
def test_sync_notify_never_raises_when_extra_not_json_serializable(tmp_path: Path):
    """notify()는 어떤 경우에도 예외를 던지지 않아야 한다(SPEC §0 에러 정책, §11 불변식).

    extra에 JSON 직렬화 불가 객체가 있고 전송이 최종 실패하면 _record_failure의
    model_dump(mode="json")가 PydanticSerializationError를 던지고, OSError만 잡기 때문에
    동기 모드에서는 notify()가 예외를 호출자(사이클)로 전파한다.
    """
    failed = tmp_path / "failed.jsonl"
    n = Notifier(_AlwaysFail(), failed_log=failed, retries=1, async_send=False,
                 retry_delays=[0.0], sleep=lambda s: None)
    ev = _event(extra={"exc": _Opaque()})
    n.notify(ev)  # 예외가 나오면 테스트 실패
    assert n.failed_count == 1


def test_async_unserializable_extra_still_recorded_in_failed_log(tmp_path: Path):
    """비동기 모드: 최종 실패한 이벤트는 extra 내용과 무관하게 failed_log에 한 줄 남아야 한다.

    현재는 워커가 PydanticSerializationError를 '워커 내부 오류'로 삼키고 기록을 건너뛴다.
    """
    failed = tmp_path / "failed.jsonl"
    n = Notifier(_AlwaysFail(), failed_log=failed, retries=1, async_send=True,
                 retry_delays=[0.0], sleep=lambda s: None)
    n.notify(_event(extra={"exc": _Opaque()}))
    n.flush(timeout_s=2.0)
    assert n.pending == 0
    assert failed.exists(), "최종 실패 이벤트가 failed_log에 기록되지 않음"
    lines = [ln for ln in failed.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "error"
    assert "error" in rec and "text" in rec


# ---------------------------------------------------------------- 결함 2: 워커 스레드 시작 실패
def test_notify_does_not_raise_when_worker_thread_cannot_start(monkeypatch, tmp_path: Path):
    """스레드를 만들 수 없는 상황(자원 고갈, 인터프리터 종료 중 atexit 알림)에서도
    notify()는 예외를 던지지 않아야 한다. 현재 _ensure_worker의 Thread.start() 예외가
    그대로 호출자에게 전파된다.
    """
    def _boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _boom)
    sent: list[str] = []

    class _Rec:
        def send(self, text: str) -> None:
            sent.append(text)

    n = Notifier(_Rec(), failed_log=tmp_path / "f.jsonl", retries=0, async_send=True)
    n.notify(_event())  # 예외가 나오면 테스트 실패


# ---------------------------------------------------------------- 결함 3: 청산 이모지가 pnl_pct를 무시
def test_exit_loss_expressed_only_by_pnl_pct_is_red():
    """SPEC §11: exit 🔴(손실)/🔵(이익). pnl_quote가 없고 pnl_pct만 음수인 손실 청산이
    현재는 🔵(이익)으로 표시된다.
    """
    ev = TradeEvent(kind="exit", ts=1, mode="paper", symbol="BTC/USDT", reason="stop_loss",
                    base_amount=0.00481, price=82_300.0, pnl_quote=None, pnl_pct=-0.0094)
    text = Notifier.format(ev)
    assert text.startswith(EMOJI["exit_loss"]), text


# ---------------------------------------------------------------- 결함 4: flush 시간 초과 시 미전송 이벤트 유실
def test_flush_timeout_records_undelivered_events_in_failed_log(tmp_path: Path):
    """SPEC §11: 최종 실패한 알림은 failed_log(jsonl)에 기록된다. flush(timeout)이 시간 초과로
    돌아오면 프로세스는 종료되고 데몬 워커는 큐에 남은 이벤트와 함께 사라진다 — 그 이벤트들은
    어디에도 기록되지 않는다. 시간 초과 시 남은 큐를 failed_log로 흘려보내야 한다.
    """
    failed = tmp_path / "failed.jsonl"
    t = _Blocking()
    n = Notifier(t, failed_log=failed, retries=0, async_send=True)
    n.notify(_event(reason="first"))   # 워커가 여기서 막힌다
    n.notify(_event(reason="second"))  # 큐에 남는다
    assert t.started.wait(1.0)
    try:
        n.flush(timeout_s=0.05)
        assert failed.exists(), "flush 시간 초과 후 미전송 이벤트가 failed_log에 없음"
        recs = [json.loads(ln) for ln in failed.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert any(r["event"]["reason"] == "second" for r in recs)
    finally:
        t.release.set()
