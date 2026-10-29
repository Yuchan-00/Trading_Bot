"""runner.py 적대적 리뷰 테스트 (SPEC §0 에러 정책, §20.4, DESIGN §2 예산 중단).

각 테스트는 현재 구현의 결함을 드러내며, 수정 전에는 실패해야 한다.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingbot.agents.roles import ROLES
from tradingbot.agents.runner import AgentRunner
from tradingbot.agents.schemas import TechnicalOutput
from tradingbot.config import load_settings
from tradingbot.llm import AnthropicBackend, CostLedger
from tradingbot.llm.backend import AgentResult, MockBackend
from tradingbot.llm.cost import BudgetExceeded
from tradingbot.skills import SkillRegistry

KEY = "sk-ant-api03-REVIEWSECRET-abcdefghijklmnopqrstuvwxyz0123456789"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- helpers
@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    for role_id in ROLES:
        (d / f"{role_id}.md").write_text(f"# Role: {role_id}\n", encoding="utf-8")
    return d


@pytest.fixture
def registry(tmp_path):
    return SkillRegistry(tmp_path / "no_skills")  # 스킬 없음(빈 렌더)


def anth_settings(tmp_path):
    return load_settings(
        {
            "MODE": "paper",
            "AGENT_BACKEND": "anthropic",
            "ANTHROPIC_API_KEY": KEY,
            "LLM_DAILY_BUDGET_USD": "10",
            "LLM_MONTHLY_BUDGET_USD": "200",
            "DATA_DIR": str(tmp_path / "data"),
        }
    )


def mock_settings(tmp_path):
    return load_settings({"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "data")})


def tech_json() -> str:
    return TechnicalOutput.conservative_example("BTC/USDT").model_dump_json()


def fake_resp(text: str, *, input_tokens=1000, output_tokens=3000):
    resp = SimpleNamespace(
        stop_reason="end_turn",
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        content=[SimpleNamespace(type="text", text=text)],
    )
    resp._request_id = "req_x"
    return resp


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = 0
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            self.calls += 1
        return self.responder(kwargs)


class FakeClient:
    def __init__(self, responder):
        self.messages = FakeMessages(responder)


class RecordingBackend:
    """역할별 동작을 지정할 수 있는 기록용 백엔드."""

    def __init__(self, raise_for: set[str]):
        self.raise_for = raise_for
        self.called: list[str] = []
        self._lock = threading.Lock()

    def complete(self, *, role, tier, system, user_payload, output_model, max_tokens, effort, timeout_s):
        with self._lock:
            self.called.append(role)
        if role in self.raise_for:
            raise BudgetExceeded("일일 LLM 예산 초과")
        return AgentResult(role=role, ok=True, output=output_model.conservative_example(), model="m")


# ---------------------------------------------------------------- F1
def test_ledger_write_failure_is_not_swallowed_as_abstain(tmp_path, prompts_dir, registry):
    """SPEC §0: 상태 저장 실패는 멈춘다. CostLedger.record 의 OSError 가 runner 에서 기권으로 삼켜지면
    비용 원장에 기록되지 않은 채 봇이 계속 LLM 을 호출하고, 재시작 후 합계가 복원되지 않아 월 예산을 조용히 넘긴다."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    ledger = CostLedger(blocker / "cost_ledger.jsonl", 10.0, 200.0, now=lambda: NOW)  # 쓰기 불가 경로
    client = FakeClient(lambda kw: fake_resp(tech_json(), output_tokens=100))
    backend = AnthropicBackend(anth_settings(tmp_path), ledger, client=client)
    runner = AgentRunner(anth_settings(tmp_path), backend, registry, prompts_dir)

    with pytest.raises(OSError):
        runner.run("technical", {"symbol": "BTC/USDT"})


# ---------------------------------------------------------------- F2
def test_run_many_stops_queued_calls_after_budget_exceeded(tmp_path, prompts_dir, registry):
    """BudgetExceeded(= LLM 중단 신호) 이후에도 대기 중이던 호출이 그대로 백엔드(유료)로 나가고,
    그 결과는 버려진다. 예산 초과가 알려진 뒤에는 새 호출을 시작하지 않아야 한다."""
    backend = RecordingBackend(raise_for={"bull"})
    runner = AgentRunner(mock_settings(tmp_path), backend, registry, prompts_dir, max_workers=1)

    with pytest.raises(BudgetExceeded):
        runner.run_many([("bull", {"symbol": "A"}), ("bear", {"symbol": "A"}), ("bear", {"symbol": "B"})])

    assert backend.called == ["bull"], f"예산 초과 이후 시작된 호출: {backend.called[1:]}"


# ---------------------------------------------------------------- F3
def test_run_many_parallel_calls_cannot_overshoot_daily_budget(tmp_path, prompts_dir, registry):
    """run_many 의 병렬 호출은 모두 같은 spent 값으로 ledger.check 를 통과한 뒤 기록된다(check→record 사이 예약 없음).
    순차라면 3번째 호출이 BudgetExceeded 로 막히지만, 병렬이면 일일 하드 예산을 넘긴다."""
    settings = anth_settings(tmp_path)
    model = settings.model_for("analysis")
    # 출력 1M 토큰당 $100 → 예상(max_tokens 3000) ≈ $0.30, 실제(3000+1000 토큰) ≈ $0.40
    ledger = CostLedger(
        tmp_path / "ledger.jsonl", 1.0, 200.0, now=lambda: NOW, prices={model: (100.0, 0.0, 100.0)}
    )
    barrier = threading.Barrier(3, timeout=5)

    def responder(kw):
        barrier.wait()  # 세 호출이 동시에 진행 중(= 모두 예산 검사를 통과)
        return fake_resp(tech_json(), input_tokens=1000, output_tokens=3000)

    backend = AnthropicBackend(settings, ledger, client=FakeClient(responder))
    runner = AgentRunner(settings, backend, registry, prompts_dir, max_workers=3)

    try:
        runner.run_many([("technical", {"symbol": s}) for s in ("A", "B", "C")])
    except BudgetExceeded:
        pass
    assert ledger.spent_today() <= ledger.daily_budget_usd, (
        f"일일 예산 {ledger.daily_budget_usd} 초과: 사용 {ledger.spent_today():.4f}"
    )


# ---------------------------------------------------------------- F4
def test_failed_result_never_carries_output(tmp_path, prompts_dir, registry):
    """DESIGN §1-5: 실패는 기권. ok=False 결과에 강세 출력이 남아 있으면(output 비정리) 하위 단계가
    `result.output is not None` 으로 판단할 때 실패를 신호로 쓰게 된다. runner 는 ok=True 만 정리하고
    ok=False 는 그대로 통과시킨다."""
    bullish = TechnicalOutput.conservative_example("BTC/USDT").model_copy(
        update={"direction": "long", "confidence": 0.95, "mtf_agreement": True}
    )
    backend = MockBackend(
        {"technical": lambda p: AgentResult(role="technical", ok=False, error="max_tokens", output=bullish)}
    )
    runner = AgentRunner(mock_settings(tmp_path), backend, registry, prompts_dir)

    res = runner.run("technical", {"symbol": "BTC/USDT"})
    assert res.ok is False
    assert res.output is None
