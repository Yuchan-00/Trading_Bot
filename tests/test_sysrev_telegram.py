"""시스템 리뷰 — 텔레그램 알림 커버리지 관점 (sysrev: telegram).

요구사항: 모든 거래 이벤트마다 텔레그램 메시지. 각 테스트는 현재 코드에서 실패해야 하는 결함 재현이다.
전부 오프라인(가짜 Transport / 가짜 urlopen). 구현자 테스트(test_cycle.py, test_main.py)의 부품을 재사용한다.
"""
from __future__ import annotations

import io
import json
import re
import threading
import urllib.error
from pathlib import Path

import pytest

from test_cycle import Rig, happy, head_with, risk_with  # noqa: F401 - 부품 재사용
from tradingbot import cycle as C
from tradingbot import journal as J
from tradingbot import main as M
from tradingbot.agents.schemas import JournalPostTrade
from tradingbot.models import TradeEvent
from tradingbot.notify import Notifier, TelegramTransport

PROJECT = Path(__file__).resolve().parent.parent
TOKEN = "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-ab"
CHAT = "-1001234567890"


# ======================================================================= 1. 전역 중단(일일 −3%·연속 3패·낙폭 차단) 알림
@pytest.mark.parametrize("setup,needle", [
    ("daily_loss", "daily_loss_limit"),
    ("consecutive", "consecutive_losses"),
    ("dd_block", "drawdown_block"),
])
def test_global_halt_is_notified(tmp_path, setup, needle):
    """DESIGN §7: '중단·해제(일일 한도·연속손실·낙폭 사다리·킬스위치·진입 일시중단)' 는 텔레그램 이벤트다.

    G0 가 당일 −3%·연속 3패·낙폭 −4.5% 차단으로 신규 진입을 막아도 cycle 은 로그·journal.record_halt 만 하고
    텔레그램은 한 통도 보내지 않는다(사용자는 봇이 왜 거래를 멈췄는지 모른다).
    """
    rig = Rig(tmp_path)
    st = rig.state
    st.daily.date = "2026-10-28"           # 시계와 같은 날(롤오버로 초기화되지 않게)
    st.daily.start_equity = 10_000.0
    if setup == "daily_loss":
        st.daily.realized_pnl = -350.0       # −3.5%
        st.daily.trades, st.daily.losses = 1, 1
    elif setup == "consecutive":
        st.daily.consecutive_losses = 3
        st.daily.realized_pnl = -10.0
        st.daily.trades, st.daily.losses = 3, 3
    else:
        st.peak_equity = 10_500.0            # 10,000 / 10,500 → 낙폭 4.76% (block, stop_all 아님)
    s = rig.cycle.run_once()
    assert s.g0_passed is False and needle in " ".join(s.g0_reasons)
    halt_msgs = [m for m in rig.transport.sent if needle in m or "중단" in m or "차단" in m]
    assert halt_msgs, f"G0 중단({needle})에 텔레그램 알림이 없음: sent={rig.transport.sent!r}"


# ======================================================================= 2. 총괄 BUY → 코드 강등(파이프라인 정리) 알림
def test_head_buy_downgraded_by_pipeline_exposure_cap_is_notified(tmp_path):
    """SPEC v2 §7: 'BUY → G4 → ... 강등은 notify(order_rejected 종류 gate_downgrade)', DESIGN §7: '하드 게이트에 의한
    강등(총괄 BUY→HOLD ...)'. pipeline._sanitize_head 가 노출 한도·남은 슬롯·미승인 때문에 총괄 BUY 를
    HOLD("코드 강등: ...")로 바꾸면 cycle 은 HOLD 로 로그만 남긴다 — 텔레그램 없음.
    """
    rig = Rig(tmp_path, candidates=("SOL/USDT",))
    rig.hold_position("ETH/USDT", base=27.0, sl=50.0, tp=200.0)   # 노출 27% → 남은 노출 3% < BUY 5%
    s = rig.cycle.run_once()
    assert "head" in rig.roles() and s.entries == []
    dec = [d for d in s.decisions if d["symbol"] == "SOL/USDT"]
    assert dec and dec[0]["action"] == "HOLD" and "코드 강등" in dec[0]["reason"], s.decisions
    msgs = [m for m in rig.transport.sent if "SOL/USDT" in m]
    assert msgs, f"총괄 BUY(SOL/USDT)가 코드에서 강등됐는데 알림이 없음: sent={rig.transport.sent!r}"


# ======================================================================= 3. 체결 후 부대 작업 실패 → 진입 알림 유실
def test_entry_notified_even_if_journal_bookkeeping_fails(tmp_path, monkeypatch):
    """_buy: market_buy 체결 → open_position → (저널 스냅샷) → save → notify(entry).
    저널 스냅샷(J.record_entry_verdicts/make_entry_verdicts)이 예외를 내면 _stage 가 잡아 'error' 알림만 보내고
    실제 체결된 진입 알림(수량·가격·SL/TP)은 영영 가지 않는다. 체결 직후 저장도 건너뛴다.
    """
    rig = Rig(tmp_path)

    def boom(*a, **k):
        raise ValueError("journal snapshot broke")
    monkeypatch.setattr(J, "record_entry_verdicts", boom)
    rig.cycle.run_once()
    assert "ETH/USDT" in rig.state.positions          # 체결·포지션은 존재한다
    entry = [m for m in rig.transport.sent if m.startswith("🟢") and "ETH/USDT" in m]
    assert entry, f"체결된 진입에 진입 알림이 없음: sent={rig.transport.sent!r}"


def test_fill_reported_even_if_state_save_fails(tmp_path, monkeypatch):
    """체결 후 상태 저장이 최종 실패하면 StateSaveError 로 사이클이 멈춘다(SPEC). 그런데 거래소에서는 이미 체결됐는데
    텔레그램에는 체결 내역이 한 줄도 가지 않는다(run_loop 는 '치명 오류로 중단: StateSaveError'만 보낸다).
    live 에서는 재시작한 봇이 모르는 포지션이 거래소에 남는다 — 사람이 알아야 할 정보(심볼·수량·가격)가 사라진다.
    """
    rig = Rig(tmp_path)
    real_buy = rig.broker.market_buy
    filled = {"n": 0}

    def buy(symbol, quote_amount):
        f = real_buy(symbol, quote_amount)
        filled["n"] += 1
        return f

    def bad_save():
        if filled["n"]:
            raise OSError(28, "No space left on device")
    monkeypatch.setattr(rig.broker, "market_buy", buy)
    monkeypatch.setattr(rig.portfolio, "save", bad_save)
    with pytest.raises(C.StateSaveError):
        rig.cycle.run_once()
    assert filled["n"] == 1
    msgs = [m for m in rig.transport.sent if "ETH/USDT" in m]
    assert msgs, f"체결 후 저장 실패 — 체결 내역 알림 없음: sent={rig.transport.sent!r}"


# ======================================================================= 4. TelegramTransport: HTTP 429 retry_after
def test_telegram_429_retry_after_is_honored(monkeypatch):
    """Bot API 는 flood control 시 HTTP 429 + parameters.retry_after(초)를 준다. 그 시간 전에 다시 보내면 계속 429.
    현재: _urllib_post 가 본문을 버리고 상태 코드만 돌려주며, Notifier 는 고정 1s/3s/9s(총 13초)로 재시도한다.
    retry_after=20 이면 4번 모두 실패 → 거래 알림이 failed_log 로만 간다.
    """
    import urllib.request

    calls = {"n": 0}
    body = json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 20",
                       "parameters": {"retry_after": 20}}).encode()

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok":true,"result":{}}'

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"Retry-After": "20", "Content-Type": "application/json"}, io.BytesIO(body))
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    slept: list[float] = []
    n = Notifier(TelegramTransport(TOKEN, CHAT), async_send=False, sleep=slept.append)
    n.notify(TradeEvent(kind="entry", ts=0, mode="paper", symbol="ETH/USDT", base_amount=1.0, price=100.0))
    assert calls["n"] == 2 and n.sent_count == 1
    assert slept and max(slept) >= 20, f"retry_after=20 을 무시하고 {slept} 만큼만 기다림"


# ======================================================================= 5. 일일 리뷰 텍스트 200자 절단·줄바꿈 소실
def test_daily_review_telegram_text_not_truncated_to_200():
    """journal 프롬프트: 일일 리뷰 telegram_text 는 ≤400자. cycle 은 reason=f"{날짜} 일일 리뷰: {text}" 로 보내는데
    _format_daily_summary 는 extra 에 'date' 가 없으면 reason 을 _truncate(200자, 줄바꿈 제거)한다 → 리뷰의 절반 이상이
    잘려 나간다. (cycle._maybe_daily_review 가 만드는 이벤트 모양 그대로.)
    """
    text = ("BTC 대비 +0.42%p 초과, 손익 +12.30 USDT (+0.12%)\n"
            "청산 4건 승률 50%, MDD 1.20%, 편향 없음, 쇠퇴 normal\n"
            "LLM 비용 $3.21 — 액션 아이템 0건, 변경 보류")
    text = text + "\n" + "추가 메모: " + "가" * 150        # 총 ~300자(≤400)
    assert len(text) <= J.TELEGRAM_MAX_CHARS
    ev = TradeEvent(kind="daily_summary", ts=0, mode="paper", reason=f"2026-10-27 일일 리뷰: {text}",
                    extra={"review_date": "2026-10-27", "llm_review": True})
    msg = Notifier.format(ev)
    assert "변경 보류" in msg and text.splitlines()[-1] in msg, msg


# ======================================================================= 6. 청산 사후분석 telegram_text 미전송
def test_post_trade_review_telegram_text_is_sent(tmp_path):
    """DESIGN §6: '저널 역할은 청산마다 3~5줄 사후분석 + 교훈 태그 ... logs/journal.jsonl 과 텔레그램에 보낸다'.
    JournalPostTrade.telegram_text 는 텔레그램용으로 만들어지지만 cycle._post_trade 는 journal.jsonl 에만 쓴다.
    """
    marker = "사후분석 테스트문구: 손절, 가정 BTC 안정 깨짐, 교훈 stop_too_tight"

    def journal(p):
        return JournalPostTrade(trade_id=str(p.get("trade", {}).get("trade_id", "x")), summary_bullets=["a", "b", "c"],
                                assumptions_at_exit=[], broken_assumption=None, near_miss_rules=[], lesson="l",
                                lesson_tag="stop_too_tight", telegram_text=marker)
    rig = Rig(tmp_path, happy(journal=journal), candidates=())
    rig.hold_position(sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.cycle.run_once()
    assert rig.state.closed_trades and rig.state.closed_trades[-1].reason == "stop_loss"
    assert any(c["role"] == "journal" for c in rig.backend.calls)
    lines = rig.journal_lines()
    assert lines and marker in json.dumps(lines, ensure_ascii=False)     # 저널에는 기록됨
    assert any(marker in m for m in rig.transport.sent), f"사후분석이 텔레그램에 없음: {rig.transport.sent!r}"


# ======================================================================= 7. 하트비트가 '일일 요약'으로 표시
def test_heartbeat_message_is_not_labelled_daily_summary(tmp_path):
    """main.run_loop 는 4시간 하트비트를 kind='daily_summary' 로 보낸다 → 포맷 결과 '📊 일일 요약 [paper]'.
    하루 6번 '일일 요약' 이 와서 진짜 일일 요약(UTC 롤오버)과 구분되지 않는다.
    """
    from test_main import FakeCycle, env_for, fake_components
    from tradingbot.config import load_settings

    settings = load_settings(env_for(tmp_path))
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 3 * 3600.0
        return clock["t"]
    comp = fake_components(settings, cycle=FakeCycle())
    M.run_loop(comp, once=False, stop_event=threading.Event(), clock=lambda: 0.0, monotonic=mono, max_cycles=4,
               wait=lambda s: False)
    hb = [e for e in comp.notifier.events if e.extra.get("heartbeat")]
    assert hb
    msg = Notifier.format(hb[0])
    assert "하트비트" in msg
    assert "일일 요약" not in msg, msg


# ======================================================================= 8. README 의 실패 로그 경로
def test_readme_failed_notification_log_path_matches_code(tmp_path):
    """README: '실패한 메시지는 logs/notify_failed.jsonl 에 남습니다'. 실제 main.build_components 는
    logs/failed_notifications.jsonl 을 쓴다 → 사용자가 엉뚱한 파일을 찾는다."""
    from tradingbot.config import load_settings

    env = {"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "d"), "SYMBOLS": "ETH/USDT",
           "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "ANTHROPIC_API_KEY": ""}
    comp = M.build_components(load_settings(env), offline_fixture=True)
    name = comp.notifier.failed_log.name
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"logs/([\w.-]+\.jsonl)", readme)) & {"notify_failed.jsonl", name}
    assert name in mentioned, f"README 가 실패 로그를 {mentioned} 로 안내하지만 코드는 {name}"
