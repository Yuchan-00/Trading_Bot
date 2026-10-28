"""시스템 리뷰 수정 통합 — 교차 파일 요청 반영 검증(notify 사후분석·저장 실패 경고, 사이클 기한 전달, 러너 예약 추정,
킬스위치 CLI). 전부 오프라인."""
from __future__ import annotations

import io
import json

import pytest

from test_cycle import Rig, happy  # noqa: F401 - 부품 재사용
from test_pipeline import happy_responses, make_scan, run  # noqa: F401
from test_pipeline import make_runner, prompts_dir, settings, skills  # noqa: F401 - 픽스처
from tradingbot import cycle as C
from tradingbot import main as M
from tradingbot.agents.roles import get_role
from tradingbot.agents.runner import RESERVATION_SAFETY, AgentRunner
from tradingbot.agents.schemas import JournalPostTrade
from tradingbot.llm.backend import MockBackend
from tradingbot.llm.cost import CostLedger, project_call_cost
from tradingbot.models import TradeEvent
from tradingbot.notify import STATE_SAVE_WARNING, Notifier


def _exit_event(**kw) -> TradeEvent:
    base = dict(kind="exit", ts=1_760_000_000_000, mode="paper", symbol="ETH/USDT", side="sell", base_amount=5.0,
                price=95.0, pnl_quote=-25.0, pnl_pct=-0.05, reason="stop_loss")
    base.update(kw)
    return TradeEvent(**base)


# ======================================================================= notify: 사후분석 전용 메시지 (TG-7)
def test_post_trade_review_has_own_header_without_qty_line():
    text = "손절: BTC 급락으로 가정 붕괴\n교훈: stop_too_tight"
    msg = Notifier.format(_exit_event(extra={"post_trade_review": text, "follow_up": True}))
    first = msg.splitlines()[0]
    assert first.startswith("📝 사후분석 [paper] ETH/USDT"), msg
    assert "청산" not in first
    assert "5 @ 95" not in msg and "→ 손익" not in msg         # 수량·가격 줄을 반복하지 않는다
    assert "손절: BTC 급락으로 가정 붕괴" in msg and "교훈: stop_too_tight" in msg   # 줄바꿈 유지


def test_plain_exit_still_uses_exit_format():
    msg = Notifier.format(_exit_event())
    assert msg.startswith("🔴 청산 [paper] ETH/USDT 손절") and "5 @ 95.00" in msg


def test_cycle_sends_post_trade_review_as_dedicated_message(tmp_path):
    marker = "사후분석 통합문구: 가정 BTC 안정 깨짐"

    def journal(p):
        return JournalPostTrade(trade_id="x", summary_bullets=["a", "b", "c"], assumptions_at_exit=[],
                                broken_assumption=None, near_miss_rules=[], lesson="l", lesson_tag="stop_too_tight",
                                telegram_text=marker)
    rig = Rig(tmp_path, happy(journal=journal), candidates=())
    rig.hold_position(sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.cycle.run_once()
    reviews = [m for m in rig.transport.sent if marker in m]
    assert len(reviews) == 1 and reviews[0].startswith("📝 사후분석 [paper] ETH/USDT"), rig.transport.sent
    exits = [m for m in rig.transport.sent if m.startswith("🔴 청산")]
    assert len(exits) == 1 and marker not in exits[0]


# ======================================================================= notify: 저장 실패 경고 (TG-4)
@pytest.mark.parametrize("kind", ["entry", "exit", "partial_exit"])
def test_state_saved_false_renders_warning_once(kind):
    kw = {"kind": kind, "extra": {"state_saved": False}}
    if kind == "entry":
        kw.update(reason=None, pnl_quote=None, pnl_pct=None, side="buy", thesis="근거")
    msg = Notifier.format(_exit_event(**kw))
    assert msg.count(STATE_SAVE_WARNING) == 1, msg
    assert msg.splitlines()[1] == STATE_SAVE_WARNING
    # 이미 본문에 경고가 있으면(구 형식) 중복하지 않는다
    kw2 = dict(kw, reason=f"stop_loss — {STATE_SAVE_WARNING}") if kind != "entry" else \
        dict(kw, thesis=f"{STATE_SAVE_WARNING} | 근거")
    assert Notifier.format(_exit_event(**kw2)).count(STATE_SAVE_WARNING) == 1
    assert STATE_SAVE_WARNING not in Notifier.format(_exit_event(kind=kind))


def test_cycle_entry_state_save_failure_warns_once(tmp_path, monkeypatch):
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
    entry = [m for m in rig.transport.sent if m.startswith("🟢") and "ETH/USDT" in m]
    assert len(entry) == 1 and entry[0].count(STATE_SAVE_WARNING) == 1, rig.transport.sent


# ======================================================================= 사이클 기한 → 백엔드·파이프라인
class DeadlineMock(MockBackend):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.deadlines: list[float | None] = []

    def set_deadline(self, remaining_s):
        self.deadlines.append(remaining_s)


def test_cycle_passes_deadline_to_backend_and_clears_it(tmp_path):
    rig = Rig(tmp_path)
    backend = DeadlineMock(happy())
    rig.backend = backend
    rig.cycle.backend = backend
    rig.cycle.runner = AgentRunner(rig.settings, backend, rig.skills, tmp_path / "prompts")
    rig.cycle.run_once()
    assert backend.calls, "파이프라인 LLM 호출이 있어야 함"
    assert len(backend.deadlines) == 2 and backend.deadlines[-1] is None
    first = backend.deadlines[0]
    assert first is not None and 0 < first <= C.CYCLE_DEADLINE_S - C.LLM_DEADLINE_RESERVE_S


def test_pipeline_should_stop_before_debate_holds_without_budget_pause(make_runner):  # noqa: F811
    runner, backend = make_runner(happy_responses())
    stages: list[str] = []

    def stop(stage):
        stages.append(stage)
        return stage == "debate"
    from tradingbot.agents.pipeline import NewsStore, run_pipeline
    from test_pipeline import EQUITY, NOW, make_state
    from tradingbot.journal import JournalState
    res = run_pipeline(make_scan(), make_state(), EQUITY, runner, JournalState(), NOW, news_store=NewsStore(),
                       should_stop=stop)
    roles = [c["role"] for c in backend.calls]
    assert "bull" not in roles and "bear" not in roles and "risk_manager" not in roles and "head" not in roles
    assert stages == ["debate"]
    assert res.head is None and res.head_skipped_reason == "cycle_deadline"
    assert res.budget_exceeded is False
    assert all(c.dropped_at in ("cycle_deadline", "g1") for c in res.candidates)
    assert "cycle_deadline" in res.abstained


def test_pipeline_should_stop_before_head(make_runner):  # noqa: F811
    runner, backend = make_runner(happy_responses())
    from tradingbot.agents.pipeline import NewsStore, run_pipeline
    from test_pipeline import EQUITY, NOW, make_state
    from tradingbot.journal import JournalState
    res = run_pipeline(make_scan(), make_state(), EQUITY, runner, JournalState(), NOW, news_store=NewsStore(),
                       should_stop=lambda st: st == "head")
    roles = [c["role"] for c in backend.calls]
    assert "risk_manager" in roles and "head" not in roles
    assert res.head is None and res.head_skipped_reason == "cycle_deadline" and not res.budget_exceeded
    assert [c.dropped_at for c in res.candidates] == ["cycle_deadline"]


def test_cycle_deadline_during_analysts_skips_rest(tmp_path):
    from test_cycle import Clock, tech_long
    clock = Clock()

    def slow_tech(p):
        clock.advance(400)
        return tech_long(p)
    rig = Rig(tmp_path, happy(technical=slow_tech), clock=clock)
    s = rig.cycle.run_once()
    roles = rig.roles()
    assert "technical" in roles and "bull" not in roles and "head" not in roles
    assert s.deadline_hit and rig.state.positions == {}
    assert rig.sent_with("cycle_deadline")
    assert not rig.portfolio.llm_paused(rig.clock.ms())     # 기한은 예산 일시정지가 아니다


# ======================================================================= 러너 예약 = 백엔드 사전 검사 추정
class _Ledger:
    cache_ttl = "1h"

    def __init__(self, inner: CostLedger):
        self.inner = inner
        self.checked: list[float] = []

    def estimate(self, *a):
        return self.inner.estimate(*a)

    def check(self, projected=0.0):
        self.checked.append(float(projected))


def test_runner_reservation_uses_project_call_cost(tmp_path, settings, prompts_dir, skills):  # noqa: F811
    ledger = _Ledger(CostLedger(tmp_path / "l.jsonl", 10.0, 100.0))
    backend = MockBackend({})
    backend.ledger = ledger  # type: ignore[attr-defined]
    backend.cache_ttl = "1h"  # type: ignore[attr-defined]
    runner = AgentRunner(settings, backend, skills, prompts_dir)
    spec = get_role("head")
    system = "S" * 4000
    payload = {"k": "v" * 1000}
    amount = runner._reserve_budget(spec, system, payload)
    user_text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    want = project_call_cost(ledger, settings.model_for(spec.tier), len(system), len(user_text), spec.max_tokens,
                             cache_ttl="1h")
    assert ledger.checked == [pytest.approx(want)]
    assert amount == pytest.approx(want * RESERVATION_SAFETY)
    runner._release_budget(amount)


# ======================================================================= 킬스위치 CLI 는 백엔드 설정과 무관
@pytest.mark.parametrize("argv,fname", [(["stop", "--reason", "점검"], "STOP"), (["clear-stop"], "CLEAR")])
def test_control_commands_work_without_api_key(tmp_path, argv, fname):
    env = {"AGENT_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "", "MODE": "paper", "DATA_DIR": str(tmp_path / "d")}
    out, err = io.StringIO(), io.StringIO()
    code = M.main(argv, env=env, out=out, err=err, input_fn=lambda p: "")
    assert code == M.EXIT_OK, err.getvalue()
    assert (tmp_path / "d" / "control" / fname).exists()
    assert "설정 경고" in err.getvalue()


def test_status_still_requires_valid_config(tmp_path):
    env = {"AGENT_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "", "MODE": "paper", "DATA_DIR": str(tmp_path / "d")}
    code = M.main(["status"], env=env, out=io.StringIO(), err=io.StringIO(), input_fn=lambda p: "")
    assert code == M.EXIT_USAGE
