"""MockBackend / AgentResult / AgentBackend 프로토콜 테스트."""
from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from tradingbot.llm import AgentBackend, AgentResult, MockBackend


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["BUY", "HOLD"]
    confidence: float
    thesis: str = ""

    @classmethod
    def conservative_example(cls) -> "Decision":
        return cls(action="HOLD", confidence=0.0, thesis="보수적 기본값")


class AllDefaults(BaseModel):
    note: str = "n/a"
    score: float = 0.0


class Strict(BaseModel):
    """conservative_example 없음 + 필수 필드 -> model_construct() 로 만들어진다."""

    model_config = ConfigDict(extra="forbid")
    required: str


class Unbuildable(BaseModel):
    required: str

    @classmethod
    def model_construct(cls, *a, **kw):  # type: ignore[override]
        raise RuntimeError("cannot construct")


def call(backend, role="analyst", payload=None, output_model=Decision, tier="analysis"):
    return backend.complete(
        role=role, tier=tier, system="SYS", user_payload=payload or {"symbol": "BTC/USDT"},
        output_model=output_model, max_tokens=512, effort="low", timeout_s=5.0,
    )


# ---------------------------------------------------------------- AgentResult
def test_agent_result_defaults():
    r = AgentResult(role="x", ok=True)
    assert r.output is None and r.error is None and r.model == ""
    assert r.input_tokens == 0 and r.cached_tokens == 0 and r.output_tokens == 0
    assert r.cost_usd == 0.0 and r.latency_s == 0.0 and r.request_id is None


def test_agent_result_forbids_extra():
    with pytest.raises(ValidationError):
        AgentResult(role="x", ok=True, bogus=1)  # type: ignore[call-arg]


def test_agent_result_holds_pydantic_output():
    out = Decision.conservative_example()
    r = AgentResult(role="x", ok=True, output=out)
    assert r.output is out


# ---------------------------------------------------------------- 프로토콜
def test_mock_backend_satisfies_protocol():
    assert isinstance(MockBackend(), AgentBackend)


# ---------------------------------------------------------------- 기본 동작
def test_default_uses_conservative_example():
    r = call(MockBackend())
    assert r.ok is True and r.error is None
    assert isinstance(r.output, Decision)
    assert r.output == Decision.conservative_example()
    assert r.cost_usd == 0.0 and r.input_tokens == 0 and r.output_tokens == 0
    assert r.model == "mock-analysis"
    assert r.role == "analyst"


def test_default_without_example_instantiates_with_defaults():
    r = call(MockBackend(), output_model=AllDefaults)
    assert r.ok and isinstance(r.output, AllDefaults) and r.output.note == "n/a"


def test_default_falls_back_to_model_construct():
    r = call(MockBackend(), output_model=Strict)
    assert r.ok and isinstance(r.output, Strict)


def test_no_mock_response_when_nothing_works():
    r = call(MockBackend(), output_model=Unbuildable)
    assert r.ok is False and r.output is None and r.error == "no_mock_response"
    assert r.model == "mock-analysis"


def test_fixed_response_instance():
    fixed = Decision(action="BUY", confidence=0.9, thesis="t")
    r = call(MockBackend({"analyst": fixed}))
    assert r.ok and r.output is fixed


def test_factory_response_receives_payload():
    seen = []

    def factory(payload: dict) -> Decision:
        seen.append(payload)
        return Decision(action="BUY", confidence=payload["conf"])

    r = call(MockBackend({"analyst": factory}), payload={"conf": 0.8})
    assert r.ok and r.output.confidence == 0.8
    assert seen == [{"conf": 0.8}]


def test_factory_can_return_agent_result_for_failure_simulation():
    fail = AgentResult(role="analyst", ok=False, error="timeout", model="mock")
    r = call(MockBackend({"analyst": lambda p: fail}))
    assert r is fail


def test_response_given_as_model_class_builds_example():
    r = call(MockBackend({"analyst": Decision}))
    assert r.ok and r.output == Decision.conservative_example()


def test_role_without_config_uses_default_factory():
    def default(role: str, output_model: type[BaseModel], payload: dict) -> BaseModel:
        assert output_model is Decision
        return Decision(action="HOLD", confidence=0.1, thesis=f"{role}:{payload['symbol']}")

    r = call(MockBackend({"other": Decision.conservative_example()}, default_factory=default))
    assert r.ok and r.output.thesis == "analyst:BTC/USDT"


def test_configured_role_beats_default_factory():
    fixed = Decision(action="BUY", confidence=0.7)
    r = call(MockBackend({"analyst": fixed}, default_factory=lambda *_: Decision.conservative_example()))
    assert r.output is fixed


def test_deterministic_across_calls():
    b = MockBackend()
    a = call(b)
    c = call(b)
    assert a.output == c.output
    assert a.model_dump() == c.model_dump()


# ---------------------------------------------------------------- 호출 기록
def test_records_calls():
    b = MockBackend()
    call(b, role="r1", payload={"a": 1}, tier="analysis")
    call(b, role="r2", payload={"b": 2}, tier="head")
    assert len(b.calls) == 2
    assert b.calls[0]["role"] == "r1" and b.calls[0]["payload"] == {"a": 1}
    assert b.calls[1]["role"] == "r2" and b.calls[1]["payload"] == {"b": 2}
    assert b.calls[1]["tier"] == "head"
    assert b.calls[0]["output_model"] == "Decision"
    assert b.calls[0]["effort"] == "low" and b.calls[0]["max_tokens"] == 512


def test_recorded_payload_is_a_copy():
    b = MockBackend()
    payload = {"a": 1}
    call(b, payload=payload)
    payload["a"] = 2
    assert b.calls[0]["payload"] == {"a": 1}


def test_calls_recorded_even_when_no_response():
    b = MockBackend()
    call(b, output_model=Unbuildable)
    assert len(b.calls) == 1


def test_tier_in_model_name():
    assert call(MockBackend(), tier="research").model == "mock-research"
    assert call(MockBackend(), tier="head").model == "mock-head"
