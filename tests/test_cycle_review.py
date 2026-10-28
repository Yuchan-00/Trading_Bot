"""적대적 리뷰: cycle.py / main.py (SPEC §15·§22, DESIGN §3·§6). 각 테스트는 발견한 결함을 재현한다.

구현자 테스트(test_cycle.py)의 Rig·가짜 부품을 재사용한다. 전부 오프라인.
"""
from __future__ import annotations

import json
import logging

import pytest

from test_cycle import (  # noqa: F401 - 픽스처 재사용
    SECRET,
    START,
    FakeTransport,
    Rig,
    _debias,
    happy,
    risk_with,
)
from tradingbot import cycle as C
from tradingbot.agents.schemas import HeadDecision, HeadOutput, RedLine, Thesis


# ---------------------------------------------------------------- 도우미
def _thesis(**kw) -> Thesis:
    d = dict(reasons=["추세 지속"], assumptions=["BTC 안정", "거래량 유지"],
             red_lines=[RedLine(metric="btc_return_1h_pct", op="<", value=-2.0)], horizon_bars=16)
    d.update(kw)
    return Thesis(**d)


def head_sell_first(p: dict) -> HeadOutput:
    """플래그된 포지션 SELL 을 먼저, 승인 후보 BUY 를 나중에 내는 총괄."""
    decisions = []
    for f in p["flagged_positions"]:
        decisions.append(HeadDecision(
            symbol=f["symbol"], action="SELL", confidence=0.8, size_pct=0.0, stop_pct=1.0, tp_pct=2.0,
            thesis=Thesis(reasons=["청산"], assumptions=[], red_lines=[], horizon_bars=0),
            disagreement_class="none", losing_side_strongest_point="-", decisive_factor="risk_exit",
            rationale="논지 훼손"))
    for a in p["approved"]:
        decisions.append(HeadDecision(
            symbol=a["symbol"], action="BUY", confidence=0.8, size_pct=5.0, stop_pct=1.0, tp_pct=2.25,
            thesis=_thesis(), disagreement_class="horizon", losing_side_strongest_point="BTC 의존",
            decisive_factor="mtf", rationale="다중 시간대 일치 추세"))
    return HeadOutput(decisions=decisions, debias=_debias(), reevaluation_triggers=[], hold_reasons=[],
                      cycle_note="테스트")


def _set_today(rig: Rig, **daily) -> None:
    d = rig.state.daily
    d.date = START.strftime("%Y-%m-%d")
    d.start_equity = 10_000.0
    for k, v in daily.items():
        setattr(d, k, v)


# ======================================================================= R1: 같은 사이클 SELL 손실 후 BUY
def test_head_sell_loss_reaching_consecutive_loss_limit_blocks_later_buy_same_cycle(tmp_path):
    """연속 손실 2회 상태에서 총괄 SELL 이 3번째 손실을 만들면, 같은 사이클의 뒤이은 BUY 는 막혀야 한다.

    HardLimits.MAX_CONSECUTIVE_LOSSES=3 → '연속 3패 이후 신규 진입 중단'. G0 는 사이클 앞에서 한 번만
    평가되므로 head 결정 처리 중 생긴 손실이 신규 진입을 막지 못한다(한도 초과 진입).
    """
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="exit"), head=head_sell_first),
              candidates=("SOL/USDT",))
    rig.hold_position("ETH/USDT", entry=100.0, base=5.0, sl=98.5, tp=103.0)
    rig.market.prices["ETH/USDT"] = 99.6           # 손실 중이지만 손절 위
    _set_today(rig, trades=2, losses=2, consecutive_losses=2, realized_pnl=-10.0)

    summary = rig.cycle.run_once()

    assert summary.g0_passed is True
    assert "ETH/USDT" not in rig.state.positions, "전제: 총괄 SELL 체결"
    assert rig.state.daily.consecutive_losses >= 3, "전제: SELL 손실로 연속 3패 도달"
    # 결함: 연속 3패 이후에도 같은 사이클에서 SOL 신규 진입이 실행된다
    assert "SOL/USDT" not in rig.state.positions, "연속 손실 한도 도달 후 신규 진입이 실행됨"


def test_head_sell_loss_reaching_daily_loss_limit_blocks_later_buy_same_cycle(tmp_path):
    """당일 실현손실 −2.9% 에서 총괄 SELL 손실로 −3% 를 넘으면 같은 사이클 BUY 는 막혀야 한다(SPEC §3 불변식)."""
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="exit"), head=head_sell_first),
              candidates=("SOL/USDT",))
    rig.hold_position("ETH/USDT", entry=100.0, base=5.0, sl=98.5, tp=103.0)
    rig.market.prices["ETH/USDT"] = 98.8           # 약 −6 USDT 손실(손절 위)
    _set_today(rig, trades=1, losses=1, consecutive_losses=1, realized_pnl=-296.0)
    rig.state.peak_equity = 10_000.0

    summary = rig.cycle.run_once()

    assert summary.g0_passed is True, summary.g0_reasons
    assert "ETH/USDT" not in rig.state.positions
    assert rig.state.daily.realized_pnl <= -300.0, "전제: SELL 로 당일 −3% 도달"
    assert "SOL/USDT" not in rig.state.positions, "당일 −3% 이후 신규 진입이 실행됨"


# ======================================================================= R2: STOP 파일을 저장 전에 지움
def test_stop_file_not_deleted_before_kill_is_persisted(tmp_path):
    """control/STOP 처리: set_kill → STOP 삭제 → save 순서라, save 가 실패하면 STOP 은 사라지고 kill 은 디스크에 없다.

    재시작하면 kill_switch=False 로 거래 재개(사람이 누른 STOP 이 유실). 파일은 저장 성공 후에 지워야 한다.
    """
    rig = Rig(tmp_path)
    rig.scanner.candidates = []
    rig.cycle.run_once()                                   # state.json 생성
    stop = rig.data / "control" / "STOP"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(json.dumps({"reason": "사람 정지", "ts": 1}), encoding="utf-8")

    def boom() -> None:
        raise OSError(28, "No space left on device")

    rig.portfolio.save = boom
    with pytest.raises(OSError):
        rig.cycle.run_once()
    on_disk = json.loads((rig.data / "state.json").read_text(encoding="utf-8"))
    assert on_disk["kill_switch"] is False                 # 전제: kill 은 저장되지 않았다
    assert stop.exists(), "kill 이 저장되지 않았는데 STOP 파일이 삭제됨 → 재시작 시 STOP 유실"


# ======================================================================= R3: 상태 저장 → 알림 순서 위반
class SnoopTransport(FakeTransport):
    """kill_switch 알림 전송 시점의 state.json kill_switch 값을 기록."""

    def __init__(self, state_path) -> None:
        super().__init__()
        self.state_path = state_path
        self.kill_on_disk_at_send: list[bool] = []

    def send(self, text: str) -> None:
        if "킬스위치 발동" in text:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.kill_on_disk_at_send.append(bool(data["kill_switch"]))
        super().send(text)


def test_manual_stop_kill_notify_after_state_saved(tmp_path):
    """SPEC §11/DESIGN §1-6: '상태 저장 → 알림' 순서. STOP 처리에서 kill_switch notify 가 save 보다 먼저 나간다."""
    rig = Rig(tmp_path)
    snoop = SnoopTransport(rig.data / "state.json")
    rig.notifier.transport = snoop
    rig.scanner.candidates = []
    rig.cycle.run_once()
    stop = rig.data / "control" / "STOP"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(json.dumps({"reason": "사람 정지", "ts": 1}), encoding="utf-8")
    rig.cycle.run_once()
    assert snoop.kill_on_disk_at_send, "전제: kill_switch 알림 전송"
    assert snoop.kill_on_disk_at_send == [True], "kill_switch 알림이 상태 저장 전에 전송됨"


# ======================================================================= R4: 포지션 3개면 보유 검토가 멈춤
def test_full_book_still_reviews_held_positions(tmp_path):
    """보유 3개(MAX_POSITIONS)면 G0 가 실패해 파이프라인(technical/risk_manager/head)이 0회 → 레드라인·논지 평가와
    총괄 SELL 이 전혀 없다. DESIGN §3: 포지션 수는 G1(후보) 조건, G0 '계정 단위 중단'이 아니다. DESIGN §6:
    레드라인은 코드가 매 사이클 평가. 포지션 수 한도는 신규 진입만 막아야 한다.
    """
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="exit"), head=head_sell_first),
              candidates=())
    rig.market.prices.update({"ETH/USDT": 100.0, "SOL/USDT": 100.0, "BTC/USDT": 100.0})
    for s in ("ETH/USDT", "SOL/USDT", "BTC/USDT"):
        rig.hold_position(s, entry=100.0, base=1.0, sl=98.5, tp=103.0)
    _set_today(rig)
    summary = rig.cycle.run_once()
    assert summary.g0_passed is False and any("max_positions" in r for r in summary.g0_reasons)
    roles = rig.roles()
    assert "risk_manager" in roles, "포지션 3개 보유 시 보유 포지션 검토(risk_manager)가 호출되지 않음"


# ======================================================================= R5: 파이프라인 OSError 가 봇을 죽임
def test_missing_prompt_file_does_not_kill_cycle(tmp_path):
    """프롬프트 파일 누락(FileNotFoundError, OSError) 은 상태 저장 실패가 아닌데, pipeline 단계가 fatal_oserror=True 라
    run_once 밖으로 전파 → main.run_loop 가 OSError 를 치명으로 보고 봇 종료(보유 포지션 손절 감시 중단).
    SPEC §15: 상태 저장 실패·CorruptStateError·ConfigError 외에는 단계별로 잡고 다음 단계로 진행.
    """
    rig = Rig(tmp_path)
    (tmp_path / "prompts" / "head.md").unlink()
    summary = rig.cycle.run_once()          # 결함: FileNotFoundError 전파
    assert summary.errors, "오류는 기록되고 사이클은 끝까지 진행돼야 한다"
    assert (rig.data / "logs" / "decisions.jsonl").exists()


# ======================================================================= R6: DEBUG 로그로 비밀값 유출
def test_stage_failure_debug_traceback_does_not_leak_secret(tmp_path, caplog):
    """``_record_error`` 는 메시지를 스크럽해 ERROR 로 남기지만, 바로 뒤 ``log.debug(..., exc_info=True)`` 가
    원 예외(스크럽 전 메시지)를 트레이스백째 로그에 쓴다. SPEC 공통 규칙: 비밀값은 어떤 로그에도 들어가면 안 된다.
    """
    rig = Rig(tmp_path)
    rig.scanner.fail = RuntimeError(f"upstream rejected credentials api_key={SECRET}")
    with caplog.at_level(logging.DEBUG, logger="tradingbot"):
        summary = rig.cycle.run_once()
    assert any(e.startswith("scan:") for e in summary.errors), "전제: scan 단계 실패가 기록됨"
    assert SECRET not in "\n".join(e for e in summary.errors)          # 요약은 스크럽됨
    assert SECRET not in caplog.text, "DEBUG 트레이스백에 원 비밀값이 그대로 기록됨"
