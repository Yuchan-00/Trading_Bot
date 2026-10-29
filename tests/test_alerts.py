"""DESIGN §7 경보 — 레짐 전환·뉴스 위험·포지션 점검(kind="alert") 판정·중복 방지·포맷 (전부 오프라인).

기록용 가짜 전송기 + ``Notifier(async_send=False)`` 로 사이클이 보낸 메시지를 그대로 확인한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cycle import Clock, FakeScanner, FakeTransport, Rig, happy, risk_with  # noqa: F401 - 부품 재사용

from tradingbot import cycle as C
from tradingbot import gates as G
from tradingbot.agents.pipeline import NewsStore
from tradingbot.agents.schemas import Debias, HeadDecision, HeadOutput, RedLine, Thesis
from tradingbot.config import HardLimits
from tradingbot.models import TradeEvent
from tradingbot.notify import EMOJI, FUSED_CONFIDENCE_FLOOR, Notifier, regime_rule_text

H = 3_600_000


# ======================================================================= 부품
class RegimeScanner(FakeScanner):
    """FakeScanner + 바꿀 수 있는 레짐 판정."""

    def __init__(self, market, candidates=(), **kw) -> None:
        super().__init__(market, candidates, **kw)
        self.verdict = "normal"
        self.btc_shock = False
        self.name_alarms: list[str] = []

    def scan(self, state, now_ms, **kw):
        res = super().scan(state, now_ms, **kw)
        fused = self.verdict in ("fused", "macro_alarm")
        rg = res.regime.model_copy(update={"verdict": self.verdict, "fused": fused, "btc_shock": self.btc_shock,
                                           "avg_pairwise_corr_1h": 0.85 if fused else 0.4,
                                           "name_alarm_symbols": list(self.name_alarms)})
        return res.model_copy(update={"regime": rg})


class RecordingTransport(FakeTransport):
    """보낸 메시지 + 경보 전송 시점의 state.json 사이클 번호를 기록한다."""

    def __init__(self, data_dir: Path | None = None, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.data_dir = data_dir
        self.saved_cycle_at_alert: list[int | None] = []

    def send(self, text: str) -> None:
        if self.data_dir is not None and text.startswith((EMOJI["regime"], EMOJI["news_risk"],
                                                          EMOJI["position_health"])):
            p = self.data_dir / "state.json"
            self.saved_cycle_at_alert.append(json.loads(p.read_text(encoding="utf-8"))["cycle"] if p.exists() else None)
        super().send(text)


def make_rig(tmp_path: Path, responses=None, *, candidates=(), transport=None) -> Rig:
    rig = Rig(tmp_path, responses, candidates=candidates, transport=transport)
    rig.scanner = RegimeScanner(rig.market, candidates)
    rig.cycle.scanner = rig.scanner
    return rig


def restart(rig: Rig) -> None:
    """같은 DATA_DIR 로 Cycle 을 새로 만든다(프로세스 재시작 흉내 — 메모리 상태 없음)."""
    rig.cycle = C.Cycle(rig.settings, rig.market, rig.broker, rig.portfolio, rig.backend, rig.ledger, rig.notifier,
                        rig.skills, now=rig.clock, scanner=rig.scanner, prompts_dir=rig.tmp / "prompts",
                        monotonic=rig.clock.monotonic)


def step(rig: Rig, seconds: float = 900):
    rig.clock.advance(seconds)
    return rig.cycle.run_once()


def alerts(rig: Rig, emoji_key: str) -> list[str]:
    return [m for m in rig.transport.sent if m.startswith(EMOJI[emoji_key])]


def head_hold(p: dict) -> HeadOutput:
    """flagged 포지션을 HOLD 로 두는 총괄(경보 조건이 여러 사이클 이어지게)."""
    decisions = [HeadDecision(symbol=f["symbol"], action="HOLD", confidence=0.6, size_pct=0.0, stop_pct=0.0,
                              tp_pct=0.0, thesis=Thesis(reasons=["관망"], assumptions=[], red_lines=[], horizon_bars=0),
                              disagreement_class="none", losing_side_strongest_point="-", decisive_factor="wait",
                              rationale="레드라인 적중이지만 손절 위라 관망")
                 for f in p["flagged_positions"]]
    return HeadOutput(decisions=decisions, debias=Debias(anchored_to_entry=False, ignored_bearish_news=False,
                                                         overtrading=False, chasing_spike=False,
                                                         acting_on_sentiment_alone=False),
                      reevaluation_triggers=[], hold_reasons=[], cycle_note="테스트")


def ev(extra: dict, **kw) -> TradeEvent:
    return TradeEvent(kind="alert", ts=1_761_645_605_000, mode="paper", extra=extra, **kw)


# ======================================================================= 레짐 전환
def test_regime_transition_once_no_spam_across_cycles_and_restart(tmp_path):
    rig = make_rig(tmp_path)
    rig.cycle.run_once()                       # 첫 실행 normal → 경보 없음
    assert alerts(rig, "regime") == []
    rig.scanner.verdict = "fused"
    step(rig)
    msgs = alerts(rig, "regime")
    assert len(msgs) == 1
    m = msgs[0]
    assert m.startswith("🌐 레짐 전환 [paper]")
    assert "normal(정상) → fused(상관 융합(FUSED))" in m
    assert "절반" in m and "0.75" in m
    step(rig)
    step(rig)
    assert len(alerts(rig, "regime")) == 1     # 같은 판정이면 다시 보내지 않는다
    restart(rig)
    step(rig)
    assert len(alerts(rig, "regime")) == 1     # 재시작해도(영속 기록) 다시 보내지 않는다
    rig.scanner.verdict = "macro_alarm"
    rig.scanner.btc_shock = True
    step(rig)
    msgs = alerts(rig, "regime")
    assert len(msgs) == 2 and "fused(상관 융합(FUSED)) → macro_alarm" in msgs[1]
    assert "신규 진입 금지" in msgs[1] and "BTC 충격: 예" in msgs[1]
    rig.scanner.verdict = "normal"
    rig.scanner.btc_shock = False
    step(rig)
    msgs = alerts(rig, "regime")
    assert len(msgs) == 3 and "→ normal(정상)" in msgs[2] and "기본 규칙" in msgs[2]
    saved = json.loads((rig.data / C.ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    assert saved["regime"] == "normal"


def test_regime_first_cycle_non_normal_alerts_from_start(tmp_path):
    rig = make_rig(tmp_path)
    rig.scanner.verdict = "fused"
    rig.scanner.name_alarms = ["SOL/USDT"]
    rig.cycle.run_once()
    msgs = alerts(rig, "regime")
    assert len(msgs) == 1
    assert "시작(이전 기록 없음) → fused" in msgs[0]
    assert "NAME 알람: SOL/USDT" in msgs[0]
    restart(rig)
    step(rig)
    assert len(alerts(rig, "regime")) == 1


def test_regime_restart_with_changed_verdict_alerts_once(tmp_path):
    rig = make_rig(tmp_path)
    rig.cycle.run_once()
    restart(rig)
    rig.scanner.verdict = "fused"
    step(rig)
    msgs = alerts(rig, "regime")
    assert len(msgs) == 1 and "normal(정상) → fused" in msgs[0]


def test_regime_no_scan_no_alert_and_state_kept(tmp_path):
    rig = make_rig(tmp_path)
    rig.scanner.verdict = "fused"
    rig.cycle.run_once()
    rig.scanner.fail = RuntimeError("scan down")
    step(rig)
    rig.scanner.fail = None
    step(rig)
    assert len(alerts(rig, "regime")) == 1


# ======================================================================= 뉴스 위험
def seed_news(rig: Rig, sym: str = "SOL/USDT", hid: str = "abc123def456", score: float = -1.0,
              event_type: str = "hack", summary: str = "솔라나 브리지 해킹 보도") -> None:
    now = rig.clock.ms()
    store: NewsStore = rig.cycle.news_store
    store.add_events([{"headline_id": hid, "score": score, "event_type": event_type, "symbols": [sym.split("/")[0]],
                       "summary": summary, "stale": False}], now, {hid: now - H})
    store.add_risk_symbols([sym], now)


def test_news_risk_alert_once_dedupe_across_cycles_and_restart(tmp_path):
    rig = make_rig(tmp_path)
    seed_news(rig)
    rig.cycle.run_once()
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 1
    m = msgs[0]
    assert m.startswith("📰 뉴스 위험 [paper] SOL/USDT")
    assert "유형: hack(해킹)" in m and "점수: -1.00 (매우 강한 악재" in m
    assert "요약: 솔라나 브리지 해킹 보도" in m
    assert "보유: 아니오" in m and "신규 진입 차단" in m and "보유 중" not in m
    step(rig)
    step(rig)
    assert len(alerts(rig, "news_risk")) == 1
    restart(rig)
    step(rig)
    assert len(alerts(rig, "news_risk")) == 1
    saved = json.loads((rig.data / C.ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    assert "SOL/USDT|abc123def456" in saved["news"]


def test_news_risk_new_headline_same_symbol_alerts_again(tmp_path):
    rig = make_rig(tmp_path)
    seed_news(rig, score=-0.66, hid="aaaaaaaaaaaa", event_type="regulatory", summary="규제 조사")
    rig.cycle.run_once()
    rig.clock.advance(900)
    seed_news(rig, score=-1.0, hid="bbbbbbbbbbbb", summary="거래소 상장폐지 예고", event_type="delisting")
    rig.cycle.run_once()
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 2
    assert "regulatory" in msgs[0] and "-0.66 (강한 악재" in msgs[0]
    assert "delisting(상장폐지)" in msgs[1] and "거래소 상장폐지 예고" in msgs[1]


def test_news_risk_dedupe_expires_after_24h(tmp_path):
    rig = make_rig(tmp_path)
    seed_news(rig)
    rig.cycle.run_once()
    rig.clock.advance(24 * 3600 + 60)
    seed_news(rig)                             # 같은 헤드라인 id 가 24h 뒤 다시 위험으로 들어옴
    rig.cycle.run_once()
    assert len(alerts(rig, "news_risk")) == 2


def test_news_risk_ignores_symbol_outside_watchlist(tmp_path):
    rig = make_rig(tmp_path)
    seed_news(rig, sym="DOGE/USDT", hid="dddddddddddd")
    rig.cycle.run_once()
    assert alerts(rig, "news_risk") == []


def test_news_risk_held_symbol_mentions_head_review(tmp_path):
    rig = make_rig(tmp_path)
    rig.hold_position("SOL/USDT", sl=90.0)
    seed_news(rig)
    rig.cycle.run_once()
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 1
    assert "보유: 예" in msgs[0] and "보유 중 →" in msgs[0]


def test_news_risk_without_event_uses_risk_event_key(tmp_path):
    rig = make_rig(tmp_path)
    rig.cycle.news_store.add_risk_symbols(["ETH/USDT"], rig.clock.ms())
    rig.cycle.run_once()
    step(rig)
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 1 and "ETH/USDT" in msgs[0] and "점수: -" in msgs[0]
    saved = json.loads((rig.data / C.ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    assert f"ETH/USDT|{C.NEWS_ALERT_NO_HEADLINE}" in saved["news"]


# ======================================================================= 포지션 점검
@pytest.mark.parametrize("health,expected", [(4, 1), (5, 0)])
def test_health_threshold_edge(tmp_path, health, expected):
    rig = make_rig(tmp_path, happy(risk_manager=risk_with(pos_health=health)))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.run_once()
    msgs = alerts(rig, "position_health")
    assert len(msgs) == expected
    if expected:
        m = msgs[0]
        assert m.startswith("🩺 포지션 점검 [paper] ETH/USDT")
        assert "건강도 4/10 (≤4 경고)" in m
        assert "가정: holding 1 · weakening 1 · impaired 0 · broken 0" in m
        assert "리스크 조치: hold(유지)" in m
        assert "레드라인: 적중 없음" in m
        assert "총괄 결정: 이번 사이클 없음" in m      # 건강도 ≤4 1회로는 총괄에게 올리지 않는다


def test_health_low_alerts_once_per_condition_change(tmp_path):
    rig = make_rig(tmp_path, happy(risk_manager=risk_with(pos_health=4), head=head_hold))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.run_once()
    step(rig)                                   # 2회 연속 ≤4 → 총괄 HOLD, 조건 같음 → 경보 없음
    step(rig)
    assert len(alerts(rig, "position_health")) == 1
    restart(rig)
    step(rig)
    assert len(alerts(rig, "position_health")) == 1
    rig.backend.responses["risk_manager"] = risk_with(pos_health=8)
    step(rig)                                   # 회복 → 조건 기록 해제
    assert len(alerts(rig, "position_health")) == 1
    saved = json.loads((rig.data / C.ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    assert "ETH/USDT" not in saved["health"]
    rig.backend.responses["risk_manager"] = risk_with(pos_health=3)
    step(rig)
    msgs = alerts(rig, "position_health")
    assert len(msgs) == 2 and "건강도 3/10" in msgs[1]


def test_red_line_alert_with_head_decision(tmp_path):
    rig = make_rig(tmp_path, happy(head=head_hold))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.journal_state.thesis["ETH/USDT"] = Thesis(
        reasons=["추세"], assumptions=["BTC 안정"], red_lines=[RedLine(metric="unrealized_pct", op="<", value=-1.0)],
        horizon_bars=16)
    rig.market.prices["ETH/USDT"] = 98.0
    rig.cycle.run_once()
    msgs = alerts(rig, "position_health")
    assert len(msgs) == 1
    m = msgs[0]
    assert "건강도 7/10" in m and "≤4" not in m
    assert "레드라인: 레드라인 적중: unrealized_pct < -1" in m and "(적중)" in m
    assert "미실현 -2.00%" in m
    assert "총괄 결정: HOLD(보유) 확신도 0.60" in m and "관망" in m
    rig.market.prices["ETH/USDT"] = 97.5         # 현재값만 바뀜 → 같은 조건
    step(rig)
    assert len(alerts(rig, "position_health")) == 1
    rig.market.prices["ETH/USDT"] = 100.0        # 레드라인 해소
    step(rig)
    rig.market.prices["ETH/USDT"] = 98.0         # 다시 적중 → 새 경보
    step(rig)
    assert len(alerts(rig, "position_health")) == 2


def test_red_line_head_sell_executed_reported(tmp_path):
    rig = make_rig(tmp_path)                    # head_with: flagged → SELL
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.journal_state.thesis["ETH/USDT"] = Thesis(
        reasons=["추세"], assumptions=[], red_lines=[RedLine(metric="unrealized_pct", op="<", value=-1.0)],
        horizon_bars=16)
    rig.market.prices["ETH/USDT"] = 98.0
    rig.cycle.run_once()
    assert "ETH/USDT" not in rig.state.positions
    msgs = alerts(rig, "position_health")
    assert len(msgs) == 1 and "총괄 결정: SELL(매도)" in msgs[0] and "실행됨" in msgs[0]
    saved = json.loads((rig.data / C.ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    assert saved["health"] == {}                # 청산된 심볼의 조건 기록은 지운다


# ======================================================================= 실패·순서
def test_alerts_sent_after_state_saved(tmp_path):
    rig = make_rig(tmp_path)
    rig.transport = RecordingTransport(rig.data)
    rig.notifier.transport = rig.transport
    rig.scanner.verdict = "fused"
    seed_news(rig)
    rig.cycle.run_once()
    assert rig.transport.saved_cycle_at_alert == [1, 1]


def test_transport_failure_never_breaks_cycle(tmp_path):
    rig = make_rig(tmp_path, transport=FakeTransport(fail=True))
    rig.scanner.verdict = "fused"
    seed_news(rig)
    summary = rig.cycle.run_once()
    assert summary.errors == []
    failed = (rig.data / "logs" / "failed.jsonl").read_text(encoding="utf-8")
    assert '"kind": "alert"' in failed
    # 전송 실패여도 중복 방지 기록은 남는다(다음 사이클 재전송 폭주 방지)
    saved = json.loads((rig.data / C.ALERT_STATE_FILENAME).read_text(encoding="utf-8"))
    assert saved["regime"] == "fused"


def test_alert_judgement_failure_isolated(tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    rig.scanner.verdict = "fused"
    seed_news(rig)

    def boom(run):
        raise RuntimeError("boom")

    monkeypatch.setattr(rig.cycle, "_regime_alerts", boom)
    summary = rig.cycle.run_once()
    assert any(e.startswith("alert:regime") for e in summary.errors)
    assert len(alerts(rig, "news_risk")) == 1    # 다른 경보는 그대로


def test_alert_state_save_failure_does_not_break(tmp_path):
    rig = make_rig(tmp_path)
    (rig.data / C.ALERT_STATE_FILENAME).mkdir(parents=True)      # 파일 자리에 디렉터리 → 저장 실패
    rig.scanner.verdict = "fused"
    summary = rig.cycle.run_once()
    assert any(e.startswith("alert_state_save") for e in summary.errors)
    assert len(alerts(rig, "regime")) == 1


def test_corrupt_alert_state_starts_fresh(tmp_path):
    rig = make_rig(tmp_path)
    rig.data.mkdir(parents=True, exist_ok=True)
    (rig.data / C.ALERT_STATE_FILENAME).write_text("{not json", encoding="utf-8")
    restart(rig)
    rig.scanner.verdict = "fused"
    summary = rig.cycle.run_once()
    assert summary.errors == []
    assert len(alerts(rig, "regime")) == 1


# ======================================================================= 포맷
def test_fused_floor_matches_gates():
    assert FUSED_CONFIDENCE_FLOOR == G.RAISED_CONFIDENCE_FLOOR
    assert f"{HardLimits.MIN_CONFIDENCE:.2f}" in regime_rule_text("normal")
    assert "신규 진입 금지" in regime_rule_text("macro_alarm")


def test_format_regime():
    text = Notifier.format(ev({"alert": "regime", "old_verdict": "normal", "new_verdict": "fused", "btc_shock": False,
                               "name_alarm_symbols": [], "btc_regime_4h": "bull", "avg_pairwise_corr_1h": 0.83}))
    assert text.splitlines()[0] == "🌐 레짐 전환 [paper]"
    assert "normal(정상) → fused(상관 융합(FUSED))" in text
    assert "규칙: 신규 진입 사이즈 절반(×0.5), 확신도 하한 0.75" in text
    assert "BTC 4h bull · 평균 상관 0.83" in text and "BTC 충격: 아니오" in text and "NAME 알람: 없음" in text
    assert Notifier.format(ev({"alert": "regime", "old_verdict": "normal", "new_verdict": "fused"})) == \
        Notifier.format(ev({"alert": "regime", "old_verdict": "normal", "new_verdict": "fused"}))


def test_format_news_risk():
    text = Notifier.format(ev({"alert": "news_risk", "headline_id": "x", "event_type": "hack", "score": -0.7,
                               "summary": "브리지 해킹\n자금 유출", "held": True, "flagged_for_head": True,
                               "flag_reason": "red_line: x"}, symbol="SOL/USDT"))
    lines = text.splitlines()
    assert lines[0] == "📰 뉴스 위험 [paper] SOL/USDT"
    assert "유형: hack(해킹)" in text
    assert "점수: -0.66 (강한 악재, 7단계 척도" in text      # 척도 값으로 맞춰 표시
    assert "요약: 브리지 해킹 자금 유출" in text
    assert "보유: 예" in text and "총괄 검토 대상으로 표시됨 (red_line: x)" in text
    assert "신규 진입 차단(G1 뉴스 위험, 24시간)" in text


def test_format_position_health():
    text = Notifier.format(ev({"alert": "position_health", "health": 3, "red_lines": ["레드라인 적중: price < 95"],
                               "risk_action": "exit", "risk_reason": "가정 붕괴",
                               "assumption_status": ["broken", "impaired", "holding"], "unrealized_pct": -1.234,
                               "head_decision": {"action": "SELL", "confidence": 0.8, "rationale": "논지 훼손",
                                                 "executed": True}}, symbol="ETH/USDT"))
    assert text.splitlines()[0] == "🩺 포지션 점검 [paper] ETH/USDT"
    assert "건강도 3/10 (≤4 경고)" in text
    assert "레드라인: 레드라인 적중: price < 95 (적중)" in text
    assert "리스크 조치: exit(청산) — 가정 붕괴" in text
    assert "가정: holding 1 · weakening 0 · impaired 1 · broken 1" in text
    assert "미실현 -1.23%" in text
    assert "총괄 결정: SELL(매도) 확신도 0.80 — 실행됨" in text and "근거: 논지 훼손" in text


def test_format_alert_unknown_subtype_and_bad_values_do_not_raise():
    text = Notifier.format(ev({"alert": "mystery"}, reason="r"))
    assert text.startswith(EMOJI["alert"]) and "[paper]" in text
    text = Notifier.format(ev({"alert": "position_health", "health": "x", "red_lines": "a",
                               "assumption_status": None, "head_decision": "SELL"}, symbol="ETH/USDT"))
    assert "건강도 -" in text and "총괄 결정: 이번 사이클 없음" in text
    text = Notifier.format(ev({"alert": "news_risk", "score": float("nan")}, symbol="ETH/USDT"))
    assert "점수: -" in text and "보유: 아니오" in text


# ======================================================================= 리뷰 후속(히스테리시스·NAME 알람·악화·실효 레짐)
def test_format_regime_name_alarm_only_and_raw_verdict_note():
    text = Notifier.format(ev({"alert": "regime", "verdict_changed": False, "old_verdict": "normal",
                               "new_verdict": "normal", "raw_verdict": "normal", "name_alarm_symbols": ["SOL/USDT"],
                               "name_alarm_held_new": ["SOL/USDT"]}))
    assert text.splitlines()[0] == "🌐 NAME 알람(보유 종목) [paper]"
    assert "레짐 판정 유지: normal(정상)" in text and "보유 종목 새 NAME 알람: SOL/USDT" in text
    text = Notifier.format(ev({"alert": "regime", "old_verdict": None, "new_verdict": "fused",
                               "raw_verdict": "normal", "fused": True}))
    assert "규칙: 신규 진입 사이즈 절반" in text and "스캐너 판정: normal(정상)" in text


def test_format_position_health_worsened_line():
    text = Notifier.format(ev({"alert": "position_health", "health": 1, "risk_action": "exit",
                               "worsened": {"health": [4, 1], "risk_action": ["hold", "exit"]}}, symbol="ETH/USDT"))
    assert "악화: 건강도 4 → 1 · 리스크 조치 hold → exit" in text


def test_regime_flap_deferred_alert_after_cooldown(tmp_path):
    rig = make_rig(tmp_path)
    rig.cycle.run_once()
    rig.scanner.verdict = "fused"
    step(rig)                                   # 경보 1
    rig.scanner.verdict = "normal"
    step(rig)                                   # 되돌림 — 보류
    assert len(alerts(rig, "regime")) == 1
    step(rig, C.REGIME_FLAP_COOLDOWN_MS / 1000)  # 쿨다운 뒤에도 normal → 그때 알림
    msgs = alerts(rig, "regime")
    assert len(msgs) == 2 and "fused(상관 융합(FUSED)) → normal(정상)" in msgs[1]


def test_name_alarm_held_once_until_cleared_and_cooldown(tmp_path):
    rig = make_rig(tmp_path, happy(head=head_hold))
    rig.hold_position("SOL/USDT", sl=90.0)
    rig.cycle.run_once()
    rig.scanner.name_alarms = ["SOL/USDT"]
    step(rig)
    step(rig)
    assert len(alerts(rig, "regime")) == 1
    rig.scanner.name_alarms = []
    step(rig)
    rig.scanner.name_alarms = ["SOL/USDT"]
    step(rig)                                   # 해제 직후 재발 — 쿨다운 안
    assert len(alerts(rig, "regime")) == 1
    rig.scanner.name_alarms = []
    step(rig, C.REGIME_FLAP_COOLDOWN_MS / 1000 + 60)
    rig.scanner.name_alarms = ["SOL/USDT"]
    step(rig)
    assert len(alerts(rig, "regime")) == 2


def test_news_risk_flag_only_does_not_show_neutral_article(tmp_path):
    rig = make_rig(tmp_path)
    now = rig.clock.ms()
    rig.cycle.news_store.add_risk_symbols(["ETH/USDT"], now)
    rig.cycle.news_store.add_events([{"headline_id": "aaaaaaaaaaaa", "score": 0.0, "event_type": "sentiment",
                                      "symbols": ["ETH"], "summary": "이더리움 컨퍼런스", "stale": False}],
                                     now, {"aaaaaaaaaaaa": now - H})
    rig.cycle.run_once()
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 1 and "컨퍼런스" not in msgs[0] and "점수: -" in msgs[0]


def test_old_health_record_format_still_loads(tmp_path):
    rig = make_rig(tmp_path, happy(risk_manager=risk_with(pos_health=4), head=head_hold))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.data.mkdir(parents=True, exist_ok=True)
    (rig.data / C.ALERT_STATE_FILENAME).write_text(json.dumps(
        {"regime": "normal", "news": {}, "health": {"ETH/USDT": json.dumps({"low": True, "red": []})}}),
        encoding="utf-8")
    restart(rig)
    rig.cycle.run_once()
    assert alerts(rig, "position_health") == []      # 같은 조건(구버전 기록) → 다시 알리지 않음
