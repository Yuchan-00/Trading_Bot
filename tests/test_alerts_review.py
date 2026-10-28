"""적대적 리뷰 — 경보(kind="alert")·notify-test 변경의 결함 재현 테스트(전부 오프라인, 수정 없음).

각 테스트는 현재 코드에서 실패해야 한다(결함 증명). 부품은 test_alerts / test_cycle 에서 재사용한다.
"""
from __future__ import annotations

import json
import os

import pytest
from test_alerts import alerts, head_hold, make_rig, restart, seed_news, step  # noqa: F401
from test_cycle import happy, risk_with

from tradingbot import cycle as C
from tradingbot.agents.schemas import RedLine, Thesis

H = 3_600_000


# ---------------------------------------------------------------------------------------------- 뉴스 위험
def _add_event(rig, hid: str, sym_base: str, score: float, ts_ms: int, summary: str, event_type: str = "sentiment"):
    rig.cycle.news_store.add_events([{"headline_id": hid, "score": score, "event_type": event_type,
                                      "symbols": [sym_base], "summary": summary, "stale": False}],
                                     rig.clock.ms(), {hid: ts_ms})


def test_news_risk_flag_without_risky_event_realerts_on_every_new_neutral_headline(tmp_path):
    """risk_event_symbols 로만 들어온 위험(점수 ≤ −0.66 이벤트 없음)은 '가장 나쁜 이벤트'가 최신 중립 헤드라인이 되어
    같은 위험이 새 헤드라인마다 다른 키(심볼|새 id)로 다시 경보된다(그리고 무관한 중립 기사를 위험 근거로 보여준다)."""
    rig = make_rig(tmp_path)
    now = rig.clock.ms()
    rig.cycle.news_store.add_risk_symbols(["ETH/USDT"], now)          # 뉴스 분석가 risk_event_symbols
    _add_event(rig, "aaaaaaaaaaaa", "ETH", 0.0, now - H, "이더리움 컨퍼런스 일정")
    rig.cycle.run_once()
    for i, hid in enumerate(("bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd")):
        rig.clock.advance(900)
        _add_event(rig, hid, "ETH", 0.0, rig.clock.ms() - 60_000, f"이더리움 일반 기사 {i}")
        rig.cycle.run_once()
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 1, f"같은 뉴스 위험이 {len(msgs)}번 경보됨"


def test_news_risk_duplicate_alert_when_event_expires_before_risk_symbol(tmp_path):
    """이벤트 만료는 게시 시각+24h, 위험 심볼 만료는 판정 시각+24h → 그 사이 창에서 같은 위험이 'risk_event' 키로 다시 경보."""
    rig = make_rig(tmp_path)
    now = rig.clock.ms()
    rig.cycle.news_store.add_events([{"headline_id": "abc123def456", "score": -1.0, "event_type": "hack",
                                      "symbols": ["SOL"], "summary": "브리지 해킹", "stale": False}],
                                     now, {"abc123def456": now - 3 * H})     # 3시간 전 게시된 기사를 지금 채점
    rig.cycle.news_store.add_risk_symbols(["SOL/USDT"], now)
    rig.cycle.run_once()
    assert len(alerts(rig, "news_risk")) == 1
    rig.clock.advance(int(21.5 * 3600))           # 이벤트는 만료(ts+24h), 위험 심볼은 아직 유효(now+24h)
    rig.cycle.run_once()
    msgs = alerts(rig, "news_risk")
    assert len(msgs) == 1, "같은 해킹 뉴스가 24h 안에 'risk_event' 키로 다시 경보됨:\n" + "\n---\n".join(msgs)


# ---------------------------------------------------------------------------------------------- 레짐
def test_regime_flapping_alerts_every_cycle(tmp_path):
    """fused 판정은 avg_corr > 0.8 의 단순 임계값(히스테리시스 없음) → 0.8 근처에서 매 사이클 경보."""
    rig = make_rig(tmp_path)
    rig.cycle.run_once()
    for i in range(6):
        rig.scanner.verdict = "fused" if i % 2 == 0 else "normal"
        step(rig)
    msgs = alerts(rig, "regime")
    assert len(msgs) <= 2, f"90분 동안 레짐 경보 {len(msgs)}건(15분마다)"


def test_regime_btc_missing_forced_normal_claims_base_rules_while_fused_still_applies(tmp_path):
    """BTC 데이터 실패 시 scanner 는 verdict 만 normal 로 바꾸고 fused=True 는 남긴다 → G3 는 계속 ×0.5·하한 0.75 인데
    경보는 'fused → normal, 기본 규칙(사이즈 ×1)' 이라고 잘못 알린다."""
    rig = make_rig(tmp_path)
    rig.scanner.verdict = "fused"
    rig.cycle.run_once()
    base_scan = rig.scanner.scan

    def btc_missing_scan(state, now_ms, **kw):
        res = base_scan(state, now_ms, **kw)
        rg = res.regime.model_copy(update={"verdict": "normal", "fused": True, "btc_regime_4h": "unknown",
                                           "avg_pairwise_corr_1h": 0.85})
        return res.model_copy(update={"regime": rg})

    rig.scanner.scan = btc_missing_scan
    step(rig)
    msgs = alerts(rig, "regime")
    wrong = [m for m in msgs if "기본 규칙" in m]
    assert not wrong, "fused 규칙이 그대로인데 기본 규칙으로 바뀌었다고 알림:\n" + "\n".join(wrong)


def test_name_alarm_on_held_symbol_without_verdict_change_not_alerted(tmp_path):
    """보유 종목에 NAME 알람(신규 진입 금지, 총괄 SELL 전량)이 생겨도 verdict 가 그대로면 아무 경보도 없다."""
    rig = make_rig(tmp_path, happy(head=head_hold))
    rig.hold_position("SOL/USDT", sl=90.0)
    rig.cycle.run_once()
    rig.scanner.name_alarms = ["SOL/USDT"]
    step(rig)
    assert any("SOL/USDT" in m for m in alerts(rig, "regime")), "보유 종목 NAME 알람 경보 없음"


# ---------------------------------------------------------------------------------------------- 포지션 점검
def test_health_red_line_record_wiped_when_pipeline_stops_early_then_realerts(tmp_path):
    """파이프라인이 레드라인 평가 전에 끝나면(사이클 기한·예산) positions 의 red_line_hits=[]·risk=None 이 '조건 해소'로
    읽혀 기록이 지워지고, 다음 사이클 같은 레드라인 적중이 새 경보로 다시 나간다."""
    rig = make_rig(tmp_path, happy(head=head_hold), candidates=("SOL/USDT",))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.journal_state.thesis["ETH/USDT"] = Thesis(
        reasons=["추세"], assumptions=["BTC 안정"], red_lines=[RedLine(metric="unrealized_pct", op="<", value=-1.0)],
        horizon_bars=16)
    rig.market.prices["ETH/USDT"] = 98.0
    rig.cycle.run_once()
    assert len(alerts(rig, "position_health")) == 1

    orig_tech = rig.backend.responses["technical"]
    slow = {"on": True}

    def tech_slow(p):
        if slow["on"]:
            rig.clock.mono += C.CYCLE_DEADLINE_S + 10      # 분석가 단계에서 사이클 기한 초과 → debate 앞에서 중단
        return orig_tech(p)

    rig.backend.responses["technical"] = tech_slow
    step(rig)
    slow["on"] = False
    step(rig)
    msgs = alerts(rig, "position_health")
    assert len(msgs) == 1, f"같은 레드라인 조건이 {len(msgs)}번 경보됨"


def test_health_flapping_around_threshold_alerts_every_other_cycle(tmp_path):
    """건강도 4/5 를 오가면(히스테리시스·쿨다운 없음) 4 로 내려올 때마다 경보."""
    rig = make_rig(tmp_path, happy(risk_manager=risk_with(pos_health=4), head=head_hold))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.run_once()
    for i in range(6):
        rig.backend.responses["risk_manager"] = risk_with(pos_health=5 if i % 2 == 0 else 4)
        step(rig)
    msgs = alerts(rig, "position_health")
    assert len(msgs) <= 2, f"105분 동안 같은 포지션 점검 경보 {len(msgs)}건"


def test_health_further_decline_not_alerted(tmp_path):
    """건강도 4 → 1 로 더 나빠지거나 리스크 조치가 hold → exit 으로 바뀌어도 서명(low, red)이 같아 경보 없음."""
    rig = make_rig(tmp_path, happy(risk_manager=risk_with(pos_health=4), head=head_hold))
    rig.hold_position("ETH/USDT", sl=90.0)
    rig.cycle.run_once()
    rig.backend.responses["risk_manager"] = risk_with(pos_health=1, pos_action="exit")
    step(rig)
    msgs = alerts(rig, "position_health")
    assert len(msgs) == 2, "건강도 4→1·리스크 exit 악화가 알려지지 않음"


# ---------------------------------------------------------------------------------------------- 영속·예외
@pytest.mark.parametrize("content", [
    json.dumps({"news": {"ETH/USDT|abc": "1.5"}}),           # 손으로 고친/다른 버전 파일: 문자열 숫자
    "[" * 200_000 + "]" * 200_000,                          # 손상: 깊은 중첩 → RecursionError
])
def test_bad_alert_state_does_not_crash_cycle_construction(tmp_path, content):
    """docstring: '손상·없음 → 빈 상태'. 그러나 ValueError(int('1.5'))·RecursionError 가 Cycle.__init__ 밖으로 샌다."""
    rig = make_rig(tmp_path)
    rig.data.mkdir(parents=True, exist_ok=True)
    (rig.data / C.ALERT_STATE_FILENAME).write_text(content, encoding="utf-8")
    restart(rig)                                             # 시작 시 예외면 봇이 뜨지 않는다


def test_alert_state_save_is_fsynced(tmp_path, monkeypatch):
    """다른 영속 파일(state.json·journal·news_store)은 tmp → fsync → replace. alert_state.json 은 fsync 없이 replace
    → 전원 손실 시 빈/잘린 파일 → '첫 실행' 으로 돌아가 뉴스·포지션 경보가 다시 나간다."""
    rig = make_rig(tmp_path)
    rig.cycle.run_once()
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    run = C._Run(rig.clock(), float(rig.clock.monotonic()))
    rig.cycle._save_alert_state(run)
    assert synced, "alert_state.json 저장에 fsync 없음"
