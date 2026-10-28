"""tradingbot/notify.py 테스트 (SPEC §11): 큐·재시도·실패 로그·토큰 비노출·포맷. 전부 오프라인."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import get_args

import pytest

from tradingbot.config import mask_secret
from tradingbot.models import EventKind, TradeEvent
from tradingbot.notify import (
    DEFAULT_RETRY_DELAYS,
    EMOJI,
    Notifier,
    TelegramTransport,
    Transport,
    TransportError,
)

TOKEN = "123456789:AAHfakeTOKENxyzSECRET_value_0000"
CHAT = "987654321"
TS = 1_761_600_000_000  # 2025-10-27 ~ UTC


# ---------------------------------------------------------------- 도우미
class RecordingTransport:
    """항상 성공. 보낸 텍스트를 기록."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class FailingTransport:
    """항상 예외."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self.exc = exc or RuntimeError("boom")

    def send(self, text: str) -> None:
        self.calls += 1
        raise self.exc


class FlakyTransport:
    """처음 n번 실패 후 성공."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"fail #{self.calls}")
        self.sent.append(text)


class BlockingTransport:
    """release 이벤트가 set될 때까지 send가 막힌다 (flush 타임아웃 테스트)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.started.set()
        self.release.wait(5)
        self.sent.append(text)


def ev(kind: str = "entry", **kw) -> TradeEvent:
    base = dict(kind=kind, ts=TS, mode="paper")
    base.update(kw)
    return TradeEvent(**base)


def entry_event(**kw) -> TradeEvent:
    base = dict(
        symbol="BTC/USDT", side="buy", base_amount=0.00481, price=83084.2, quote_amount=400.4,
        stop_loss_price=82362.2, take_profit_price=84790.3, confidence=0.72,
        thesis="4h 추세 상승, 15m 되돌림 후 거래량 동반 반등",
    )
    base.update(kw)
    return ev("entry", **base)


def exit_event(**kw) -> TradeEvent:
    base = dict(
        symbol="BTC/USDT", side="sell", base_amount=0.00481, price=82300.0, reason="stop_loss",
        pnl_quote=-3.77, pnl_pct=-0.0094, equity=9996.2,
    )
    base.update(kw)
    return ev("exit", **base)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ================================================================ 포맷
class TestFormat:
    @pytest.mark.parametrize("kind", get_args(EventKind))
    def test_every_kind_formats(self, kind: str):
        e = ev(
            kind, symbol="ETH/USDT", side="buy", base_amount=1.5, price=3000.0, quote_amount=4500.0,
            stop_loss_price=2950.0, take_profit_price=3100.0, confidence=0.7, thesis="t", reason="r",
            pnl_quote=1.0, pnl_pct=0.01, equity=10000.0,
            extra={"date": "2025-10-27", "trades": 2, "wins": 1, "losses": 1, "llm_cost_usd": 0.5,
                   "symbols": ["ETH/USDT"], "until_ts": TS, "stage": "x", "spent_usd": 11.0, "budget_usd": 10.0},
        )
        text = Notifier.format(e)
        assert isinstance(text, str) and text.strip()
        assert "[paper]" in text
        # 종류별 이모지
        if kind == "exit":
            assert text.startswith(EMOJI["exit_gain"])
        else:
            assert text.startswith(EMOJI[kind])
        # 포맷은 결정적
        assert Notifier.format(e) == text

    def test_entry_example_matches_spec(self):
        text = Notifier.format(entry_event())
        lines = text.split("\n")
        assert lines[0] == "🟢 진입 [paper] BTC/USDT"
        assert lines[1] == "수량 0.00481 @ 83,084.2 (400.4 USDT)"
        assert lines[2] == "손절 82,362.2 (-0.87%) / 익절 84,790.3 (+2.05%)"
        assert lines[3] == "확신도 0.72"
        assert lines[4].startswith("근거: 4h 추세 상승")

    def test_entry_thesis_truncated_to_200(self):
        long = "가" * 500
        text = Notifier.format(entry_event(thesis=long))
        thesis_line = [ln for ln in text.split("\n") if ln.startswith("근거: ")][0]
        body = thesis_line[len("근거: "):]
        assert len(body) <= 200
        assert body.endswith("…")
        assert "가" * 199 == body[:199]

    def test_entry_thesis_exactly_200_not_truncated(self):
        thesis = "나" * 200
        text = Notifier.format(entry_event(thesis=thesis))
        assert f"근거: {thesis}" in text

    def test_entry_without_optional_fields(self):
        text = Notifier.format(ev("entry", symbol="SOL/USDT", base_amount=2.0, price=150.123))
        assert text == "🟢 진입 [paper] SOL/USDT\n수량 2 @ 150.12"

    def test_exit_loss_red_and_example(self):
        text = Notifier.format(exit_event())
        assert text == (
            "🔴 청산 [paper] BTC/USDT 손절\n"
            "0.00481 @ 82,300.0 → 손익 -3.77 USDT (-0.94%)\n"
            "자본 9,996.2"
        )

    def test_exit_gain_blue(self):
        text = Notifier.format(exit_event(reason="take_profit", price=84790.3, pnl_quote=7.33, pnl_pct=0.0183, equity=10007.3))
        assert text.startswith("🔵 청산 [paper] BTC/USDT 익절")
        assert "+7.33 USDT (+1.83%)" in text
        assert "자본 10,007.3" in text

    def test_exit_zero_pnl_is_blue(self):
        text = Notifier.format(exit_event(pnl_quote=0.0, pnl_pct=0.0))
        assert text.startswith("🔵")

    def test_exit_none_pnl_is_blue(self):
        text = Notifier.format(exit_event(pnl_quote=None, pnl_pct=None))
        assert text.startswith("🔵")
        assert "손익" not in text

    @pytest.mark.parametrize(
        "reason,ko",
        [("stop_loss", "손절"), ("take_profit", "익절"), ("time_stop", "시간손절"), ("head_sell", "헤드 매도"),
         ("kill_switch", "킬스위치"), ("manual", "수동"), ("partial", "부분"), ("weird", "weird")],
    )
    def test_exit_reason_korean(self, reason, ko):
        text = Notifier.format(exit_event(reason=reason))
        assert text.split("\n")[0].endswith(ko)

    def test_partial_exit_orange(self):
        text = Notifier.format(ev("partial_exit", symbol="BTC/USDT", base_amount=0.002, price=83000.0,
                                  reason="partial", pnl_quote=-0.5, pnl_pct=-0.003))
        assert text.startswith("🟠 부분 청산 [paper] BTC/USDT 부분")
        assert "-0.50 USDT (-0.30%)" in text

    def test_order_rejected(self):
        text = Notifier.format(ev("order_rejected", symbol="XRP/USDT", side="buy", quote_amount=1234.5,
                                  reason="최소 주문 금액 미달"))
        assert text == "⚠️ 주문 거부 [paper] XRP/USDT\n매수 1,234.5 USDT\n사유: 최소 주문 금액 미달"

    def test_kill_switch(self):
        text = Notifier.format(ev("kill_switch", mode="live", reason="낙폭 6% 초과", equity=9400.0))
        assert text.startswith("⛔ 킬스위치 발동 [live]")
        assert "사유: 낙폭 6% 초과" in text
        assert "자본 9,400.0" in text

    def test_budget_exceeded(self):
        text = Notifier.format(ev("budget_exceeded", reason="일일 예산 초과", extra={"spent_usd": 10.5, "budget_usd": 10.0}))
        assert text.startswith("💸 LLM 예산 초과 [paper]")
        assert "사용 $10.50 / 예산 $10.00" in text

    def test_daily_summary(self):
        text = Notifier.format(ev(
            "daily_summary", reason="2025-10-27 일일 요약", pnl_quote=12.345, pnl_pct=0.0012, equity=10012.3,
            extra={"date": "2025-10-27", "trades": 3, "wins": 2, "losses": 1, "win_rate": 2 / 3,
                   "start_equity": 10000.0, "llm_cost_usd": 1.2345},
        ))
        lines = text.split("\n")
        assert lines[0] == "📊 일일 요약 2025-10-27 [paper]"
        assert "손익 +12.35 USDT (+0.12%)" in text
        assert "거래 3건 (승 2 / 패 1) 승률 66.67%" in text
        assert "자본 10,012.3 (시작 10,000.0)" in text
        assert "LLM 비용 $1.23" in text

    def test_error(self):
        text = Notifier.format(ev("error", symbol="BTC/USDT", reason="거래소 타임아웃", extra={"stage": "fetch"}))
        assert text == "❗ 오류 [paper] BTC/USDT\n거래소 타임아웃\n단계: fetch"

    def test_startup_shutdown(self):
        s = Notifier.format(ev("startup", equity=10000.0, extra={"symbols": ["BTC/USDT", "ETH/USDT"], "cycle_minutes": 15}))
        assert s.startswith("▶️ 봇 시작 [paper]")
        assert "종목: BTC/USDT, ETH/USDT" in s
        assert "사이클 15분" in s
        assert "UTC" in s
        d = Notifier.format(ev("shutdown", reason="SIGTERM"))
        assert d.startswith("⏹ 봇 종료 [paper]")
        assert "사유: SIGTERM" in d

    def test_llm_paused(self):
        text = Notifier.format(ev("llm_paused", reason="API 오류 연속 3회", extra={"until_ts": TS + 3_600_000}))
        assert text.startswith("⏸ LLM 일시정지 [paper]")
        assert "사유: API 오류 연속 3회" in text
        assert "해제 예정:" in text and "UTC" in text

    def test_thousands_and_percent_formatting(self):
        text = Notifier.format(entry_event(price=1234567.891, quote_amount=12345.678, stop_loss_price=1222222.2,
                                           take_profit_price=1259259.3))
        assert "@ 1,234,567.9 (12,345.7 USDT)" in text
        assert "손절 1,222,222.2 (-1.00%)" in text
        assert "익절 1,259,259.3 (+2.00%)" in text

    def test_small_price_formatting(self):
        text = Notifier.format(ev("entry", symbol="XRP/USDT", base_amount=1000, price=0.51234))
        assert "수량 1,000 @ 0.5123" in text
        text = Notifier.format(ev("entry", symbol="SHIB/USDT", base_amount=1e6, price=0.00001234))
        assert "@ 0.000012" in text

    def test_thesis_newlines_collapsed(self):
        text = Notifier.format(entry_event(thesis="첫째 줄\n둘째   줄"))
        assert "근거: 첫째 줄 둘째 줄" in text


# ================================================================ TelegramTransport
class TestTelegramTransport:
    def test_posts_json_to_sendmessage(self):
        calls: list[tuple[str, dict]] = []

        def post(url, payload):
            calls.append((url, payload))
            return 200

        t = TelegramTransport(TOKEN, CHAT, http_post=post)
        t.send("안녕 BTC/USDT")
        assert len(calls) == 1
        url, payload = calls[0]
        assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        assert payload == {"chat_id": CHAT, "text": "안녕 BTC/USDT", "disable_web_page_preview": True}
        assert "parse_mode" not in payload

    def test_non_200_raises(self):
        t = TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: 429)
        with pytest.raises(TransportError) as ei:
            t.send("x")
        assert "429" in str(ei.value)
        assert TOKEN not in str(ei.value)

    def test_post_exception_wrapped_and_scrubbed(self):
        def post(url, payload):
            raise ConnectionError(f"cannot reach {url}")

        t = TelegramTransport(TOKEN, CHAT, http_post=post)
        with pytest.raises(TransportError) as ei:
            t.send("x")
        msg = str(ei.value)
        assert TOKEN not in msg
        assert "api.telegram.org" not in msg
        assert "<telegram-url>" in msg
        assert "ConnectionError" in msg
        # 원인 체인에 URL이 남지 않도록 끊는다
        assert ei.value.__cause__ is None and ei.value.__suppress_context__

    def test_bare_token_in_exception_masked(self):
        def post(url, payload):
            raise RuntimeError(f"bad token {TOKEN}")

        t = TelegramTransport(TOKEN, CHAT, http_post=post)
        with pytest.raises(TransportError) as ei:
            t.send("x")
        msg = str(ei.value)
        assert TOKEN not in msg
        assert mask_secret(TOKEN) in msg

    def test_bad_status_type_raises(self):
        t = TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: "nope")
        with pytest.raises(TransportError):
            t.send("x")

    def test_repr_str_mask_token(self):
        t = TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: 200)
        for s in (repr(t), str(t), f"{t}"):
            assert TOKEN not in s
            assert CHAT not in s
            assert mask_secret(TOKEN) in s

    def test_empty_token_or_chat_rejected(self):
        with pytest.raises(ValueError):
            TelegramTransport("", CHAT)
        with pytest.raises(ValueError):
            TelegramTransport(TOKEN, "")

    def test_long_text_truncated_to_telegram_limit(self):
        seen = {}
        t = TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: seen.update(p) or 200)
        t.send("a" * 5000)
        assert len(seen["text"]) == 4096
        assert seen["text"].endswith("…")

    def test_url_and_token_never_logged(self, caplog):
        caplog.set_level(logging.DEBUG)

        def post(url, payload):
            raise RuntimeError(f"error at {url}")

        t = TelegramTransport(TOKEN, CHAT, http_post=post)
        with pytest.raises(TransportError):
            t.send("x")
        TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: 200).send("ok")
        n = Notifier(t, async_send=False, retry_delays=[0, 0, 0])
        n.notify(entry_event())
        logging.getLogger("tradingbot.notify").info("transport=%r", t)
        assert caplog.text  # 뭔가 기록은 됐다
        assert TOKEN not in caplog.text
        assert "api.telegram.org" not in caplog.text

    def test_default_http_post_uses_urllib(self, monkeypatch):
        import urllib.request

        captured = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            captured["ctype"] = req.get_header("Content-type")
            return FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        t = TelegramTransport(TOKEN, CHAT, timeout_s=3)
        t.send("hi")
        assert captured["url"].endswith(f"/bot{TOKEN}/sendMessage")
        assert captured["method"] == "POST"
        assert captured["body"]["text"] == "hi"
        assert captured["body"]["chat_id"] == CHAT
        assert captured["body"]["disable_web_page_preview"] is True
        assert captured["timeout"] == 3
        assert captured["ctype"].startswith("application/json")

    def test_default_http_post_http_error_status(self, monkeypatch):
        import urllib.error
        import urllib.request

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        t = TelegramTransport(TOKEN, CHAT)
        with pytest.raises(TransportError) as ei:
            t.send("hi")
        assert "403" in str(ei.value)
        assert TOKEN not in str(ei.value)

    def test_default_http_post_network_error_scrubbed(self, monkeypatch):
        import urllib.error
        import urllib.request

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError(f"unreachable {req.full_url}")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        t = TelegramTransport(TOKEN, CHAT)
        with pytest.raises(TransportError) as ei:
            t.send("hi")
        assert TOKEN not in str(ei.value)

    def test_is_transport_protocol(self):
        assert isinstance(TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: 200), Transport)
        assert isinstance(RecordingTransport(), Transport)


# ================================================================ Notifier (동기)
class TestNotifierSync:
    def test_sends_formatted_text(self):
        t = RecordingTransport()
        n = Notifier(t, async_send=False)
        n.notify(entry_event())
        assert t.sent == [Notifier.format(entry_event())]
        assert n.sent_count == 1 and n.failed_count == 0

    def test_transport_none_logs_only(self, caplog):
        caplog.set_level(logging.INFO, logger="tradingbot.notify")
        n = Notifier(None, async_send=False)
        n.notify(exit_event())
        assert "알림 비활성" in caplog.text
        assert "BTC/USDT" in caplog.text
        n2 = Notifier(None)  # async여도 큐에 넣지 않는다
        n2.notify(exit_event())
        assert n2.pending == 0
        n2.flush(0.01)

    def test_retries_then_failed_log(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG, logger="tradingbot.notify")
        failed = tmp_path / "logs" / "notify_failed.jsonl"
        t = FailingTransport(RuntimeError("telegram down"))
        n = Notifier(t, failed_log=failed, retries=3, async_send=False, retry_delays=[0, 0, 0])
        e = entry_event()
        n.notify(e)  # 예외 없음
        assert t.calls == 4  # 첫 시도 + 재시도 3회
        recs = read_jsonl(failed)
        assert len(recs) == 1
        r = recs[0]
        assert r["kind"] == "entry"
        assert r["attempts"] == 4
        assert "telegram down" in r["error"]
        assert r["text"] == Notifier.format(e)
        assert TradeEvent.model_validate(r["event"]) == e
        assert isinstance(r["ts"], int)
        # 결정적 JSON (sort_keys, ensure_ascii=False)
        line = failed.read_text(encoding="utf-8").splitlines()[0]
        assert line == json.dumps(r, sort_keys=True, ensure_ascii=False)
        assert "근거" in line  # ensure_ascii=False
        # ERROR 로그
        errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
        assert errors and "알림 최종 실패" in errors[0].getMessage()
        assert n.failed_count == 1

    def test_retry_delays_default_sequence(self):
        slept: list[float] = []
        t = FailingTransport()
        n = Notifier(t, retries=3, async_send=False, sleep=slept.append)
        assert n.retry_delays == DEFAULT_RETRY_DELAYS == (1.0, 3.0, 9.0)
        n.notify(entry_event())
        assert slept == [1.0, 3.0, 9.0]
        assert t.calls == 4

    def test_retry_delays_repeat_last_when_more_retries(self):
        slept: list[float] = []
        n = Notifier(FailingTransport(), retries=5, async_send=False, sleep=slept.append)
        n.notify(entry_event())
        assert slept == [1.0, 3.0, 9.0, 9.0, 9.0]

    def test_retries_zero_no_sleep(self):
        slept: list[float] = []
        t = FailingTransport()
        n = Notifier(t, retries=0, async_send=False, sleep=slept.append)
        n.notify(entry_event())
        assert t.calls == 1 and slept == []

    def test_flaky_transport_succeeds_after_retry(self, tmp_path):
        failed = tmp_path / "f.jsonl"
        t = FlakyTransport(fail_times=2)
        n = Notifier(t, failed_log=failed, retries=3, async_send=False, retry_delays=[0, 0, 0])
        n.notify(entry_event())
        assert t.calls == 3 and len(t.sent) == 1
        assert not failed.exists()
        assert n.sent_count == 1 and n.failed_count == 0

    def test_failed_log_dir_created(self, tmp_path):
        failed = tmp_path / "a" / "b" / "c.jsonl"
        n = Notifier(FailingTransport(), failed_log=failed, retries=0, async_send=False)
        n.notify(ev("error", reason="x"))
        assert failed.exists() and len(read_jsonl(failed)) == 1

    def test_failed_log_unwritable_does_not_raise(self, tmp_path, caplog):
        caplog.set_level(logging.ERROR, logger="tradingbot.notify")
        bad = tmp_path / "dir_as_file"
        bad.mkdir()
        n = Notifier(FailingTransport(), failed_log=bad, retries=0, async_send=False)
        n.notify(ev("error", reason="x"))  # 예외 없음
        assert "failed_log 기록 실패" in caplog.text

    def test_no_failed_log_configured(self):
        n = Notifier(FailingTransport(), retries=0, async_send=False)
        n.notify(ev("error", reason="x"))
        assert n.failed_count == 1

    def test_flush_sync_is_noop(self):
        n = Notifier(RecordingTransport(), async_send=False)
        n.flush(0.0)
        assert n.pending == 0

    def test_sleep_exception_ignored(self):
        def bad_sleep(_):
            raise RuntimeError("no sleep")

        t = FailingTransport()
        n = Notifier(t, retries=2, async_send=False, sleep=bad_sleep)
        n.notify(entry_event())
        assert t.calls == 3

    def test_repr_masks_transport(self):
        n = Notifier(TelegramTransport(TOKEN, CHAT, http_post=lambda u, p: 200), async_send=False)
        assert TOKEN not in repr(n)
        assert "Notifier(" in repr(n)


# ================================================================ Notifier (비동기)
class TestNotifierAsync:
    def test_notify_returns_immediately_with_failing_transport(self, tmp_path):
        failed = tmp_path / "notify_failed.jsonl"
        t = FailingTransport(RuntimeError("always"))
        n = Notifier(t, failed_log=failed, retries=3, async_send=True, retry_delays=[0, 0, 0])
        e = exit_event()
        t0 = time.perf_counter()
        n.notify(e)
        n.notify(entry_event())
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.2
        n.flush(timeout_s=5.0)
        assert n.pending == 0
        recs = read_jsonl(failed)
        assert [r["kind"] for r in recs] == ["exit", "entry"]
        assert recs[0]["attempts"] == 4
        assert t.calls == 8
        assert TradeEvent.model_validate(recs[0]["event"]) == e

    def test_notify_never_blocks_even_when_transport_blocks(self):
        t = BlockingTransport()
        n = Notifier(t, retries=0, async_send=True)
        t0 = time.perf_counter()
        for _ in range(50):
            n.notify(ev("error", reason="x"))
        assert time.perf_counter() - t0 < 0.2
        assert n.pending == 50
        t.release.set()
        n.flush(5.0)
        assert len(t.sent) == 50 and n.pending == 0

    def test_worker_is_daemon_thread(self):
        n = Notifier(RecordingTransport())
        n.notify(ev("startup"))
        assert n._worker is not None and n._worker.daemon
        assert n._worker.name == "tradingbot-notify"
        n.flush(2.0)

    def test_order_preserved_and_success(self):
        t = RecordingTransport()
        n = Notifier(t, async_send=True)
        events = [ev("startup"), entry_event(), exit_event(), ev("shutdown")]
        for e in events:
            n.notify(e)
        n.flush(5.0)
        assert t.sent == [Notifier.format(e) for e in events]
        assert n.sent_count == 4

    def test_worker_survives_failure_and_continues(self, tmp_path):
        failed = tmp_path / "f.jsonl"
        t = FlakyTransport(fail_times=1)  # 첫 호출만 실패 -> retries=0 이므로 첫 이벤트는 실패
        n = Notifier(t, failed_log=failed, retries=0, async_send=True)
        n.notify(ev("error", reason="first"))
        n.notify(ev("error", reason="second"))
        n.flush(5.0)
        assert len(read_jsonl(failed)) == 1
        assert len(t.sent) == 1 and "second" in t.sent[0]
        assert n._worker.is_alive()

    def test_flush_timeout_returns_and_warns(self, caplog):
        caplog.set_level(logging.WARNING, logger="tradingbot.notify")
        t = BlockingTransport()
        n = Notifier(t, retries=0, async_send=True)
        n.notify(ev("error", reason="x"))
        assert t.started.wait(2.0)
        t0 = time.perf_counter()
        n.flush(timeout_s=0.05)
        assert 0.04 <= time.perf_counter() - t0 < 0.2
        assert "flush 시간 초과" in caplog.text
        assert n.pending == 1
        t.release.set()
        n.flush(5.0)
        assert n.pending == 0 and len(t.sent) == 1

    def test_flush_empty_queue_immediate(self):
        n = Notifier(RecordingTransport())
        t0 = time.perf_counter()
        n.flush(5.0)
        assert time.perf_counter() - t0 < 0.1

    def test_notify_from_many_threads(self):
        t = RecordingTransport()
        n = Notifier(t)

        def work(i):
            for _ in range(10):
                n.notify(ev("error", reason=f"thread{i}"))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        n.flush(5.0)
        assert len(t.sent) == 50
        assert n._worker.is_alive()

    def test_token_not_in_logs_async_failures(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)

        def post(url, payload):
            raise RuntimeError(f"502 from {url}")

        tr = TelegramTransport(TOKEN, CHAT, http_post=post)
        failed = tmp_path / "f.jsonl"
        n = Notifier(tr, failed_log=failed, retries=2, retry_delays=[0, 0, 0])
        n.notify(entry_event())
        n.flush(5.0)
        assert TOKEN not in caplog.text
        assert "api.telegram.org" not in caplog.text
        content = failed.read_text(encoding="utf-8")
        assert TOKEN not in content
        assert "api.telegram.org" not in content
        assert "<telegram-url>" in content


# ================================================================ 시그니처 (SPEC §11)
def test_signatures_match_spec():
    import inspect

    p = inspect.signature(TelegramTransport.__init__).parameters
    assert list(p)[:3] == ["self", "token", "chat_id"]
    assert p["http_post"].kind is inspect.Parameter.KEYWORD_ONLY and p["http_post"].default is None
    assert p["timeout_s"].kind is inspect.Parameter.KEYWORD_ONLY and p["timeout_s"].default == 10

    q = inspect.signature(Notifier.__init__).parameters
    assert list(q)[:2] == ["self", "transport"]
    for name, default in (("failed_log", None), ("retries", 3), ("async_send", True)):
        assert q[name].kind is inspect.Parameter.KEYWORD_ONLY and q[name].default == default
    assert inspect.signature(Notifier.flush).parameters["timeout_s"].default == 5.0
    assert isinstance(inspect.getattr_static(Notifier, "format"), staticmethod)
