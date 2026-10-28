"""AnthropicBackend 테스트: 가짜 client 로 create 호출 인자·정지 사유·예외 매핑·예산·비밀 비노출.

가짜 client 는 실제 SDK 처럼 원본 Message(text 블록 + usage + stop_reason)를 돌려준다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from tradingbot.config import ConfigError, load_settings, mask_secret
from tradingbot.llm import AgentBackend, AnthropicBackend, BudgetExceeded, CostLedger
from tradingbot.llm.cost import CACHE_WRITE_MULTIPLIER, PRICES_USD_PER_MTOK

KEY = "sk-ant-api03-SUPERSECRETKEY-abcdefghijklmnopqrstuvwxyz0123456789"
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["BUY", "HOLD"]
    confidence: float
    thesis: str = ""


def make_settings(**over):
    env = {
        "MODE": "paper",
        "AGENT_BACKEND": "anthropic",
        "ANTHROPIC_API_KEY": KEY,
        "LLM_DAILY_BUDGET_USD": "10",
        "LLM_MONTHLY_BUDGET_USD": "200",
    }
    env.update(over)
    return load_settings(env)


def fake_response(
    parsed=None,
    *,
    stop_reason="end_turn",
    stop_details=None,
    input_tokens=1000,
    output_tokens=200,
    cache_read=0,
    cache_write=0,
    request_id="req_abc",
):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    if parsed is None:
        content = []
    elif isinstance(parsed, BaseModel):
        content = [SimpleNamespace(type="text", text=parsed.model_dump_json())]
    elif isinstance(parsed, str):
        content = [SimpleNamespace(type="text", text=parsed)]
    else:
        content = [SimpleNamespace(type="text", text=json.dumps(parsed, ensure_ascii=False, sort_keys=True))]
    resp = SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=usage,
        content=content,
    )
    resp._request_id = request_id
    return resp


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.responder(kwargs) if callable(self.responder) else self.responder
        if isinstance(out, BaseException):
            raise out
        return out


class FakeClient:
    def __init__(self, responder):
        self.messages = FakeMessages(responder)


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(
        tmp_path / "ledger.jsonl", 10.0, 200.0,
        now=lambda: datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
    )


def make_backend(responder, ledger: CostLedger, settings=None, **kw):
    client = FakeClient(responder)
    backend = AnthropicBackend(settings or make_settings(), ledger, client=client, **kw)
    return backend, client


PAYLOAD = {"zeta": 1, "alpha": {"y": 2, "x": [3, 1]}, "symbol": "BTC/USDT", "한글": "값"}


def run(backend, *, role="analyst", tier="analysis", system="SYSTEM PROMPT", payload=None,
        output_model=Decision, max_tokens=800, effort="medium", timeout_s=30.0):
    return backend.complete(
        role=role, tier=tier, system=system, user_payload=PAYLOAD if payload is None else payload,
        output_model=output_model, max_tokens=max_tokens, effort=effort, timeout_s=timeout_s,
    )


def status_error(cls, status: int, message="boom", body=None):
    return cls(message, response=httpx2.Response(status, request=REQ), body=body)


# ---------------------------------------------------------------- 생성
def test_satisfies_protocol(ledger):
    backend, _ = make_backend(fake_response(Decision(action="HOLD", confidence=0.1)), ledger)
    assert isinstance(backend, AgentBackend)


def test_requires_api_key_when_no_client_injected(ledger):
    settings = make_settings(AGENT_BACKEND="mock", ANTHROPIC_API_KEY="")
    with pytest.raises(ConfigError):
        AnthropicBackend(settings, ledger)


def test_default_client_is_constructed_with_key_and_retries(ledger, monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)
            self.messages = FakeMessages(lambda k: None)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    backend = AnthropicBackend(make_settings(), ledger)
    assert captured["api_key"] == KEY
    # SDK 자동 재시도는 끈다: 재시도는 백엔드가 역할 timeout 예산 안에서만 한다(CLAUDE-API-1)
    assert captured["max_retries"] == 0
    assert backend.max_retries == 2
    assert captured["timeout"] == 60
    assert KEY not in repr(backend)
    assert isinstance(backend.client, FakeAnthropic)


def test_invalid_cache_ttl(ledger):
    with pytest.raises(ValueError):
        make_backend(None, ledger, cache_ttl="2h")


def test_repr_masks_key(ledger):
    backend, _ = make_backend(None, ledger)
    assert KEY not in repr(backend)
    assert mask_secret(KEY) in repr(backend)


# ---------------------------------------------------------------- create 호출 인자
def test_create_kwargs(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger)
    r = run(backend, tier="research", max_tokens=777, effort="high", timeout_s=12.5)
    assert r.ok
    assert len(client.messages.calls) == 1
    kw = client.messages.calls[0]
    assert set(kw) == {"model", "max_tokens", "system", "messages", "output_config", "timeout"}
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["max_tokens"] == 777
    assert kw["system"] == [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert kw["messages"] == [{"role": "user", "content": json.dumps(PAYLOAD, ensure_ascii=False, sort_keys=True)}]
    assert kw["output_config"]["effort"] == "high"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert set(fmt["schema"]["properties"]) == {"action", "confidence", "thesis"}
    assert "output_format" not in kw
    assert kw["timeout"] == 12.5
    assert "thinking" not in kw
    assert "tool_choice" not in kw
    assert "tools" not in kw


def test_user_content_is_sort_keys_json_with_unicode(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger)
    run(backend, payload={"b": 1, "a": {"d": 1, "c": 2}, "k": "한글"})
    content = client.messages.calls[0]["messages"][0]["content"]
    assert content == '{"a": {"c": 2, "d": 1}, "b": 1, "k": "한글"}'
    assert "\\u" not in content


def test_user_content_identical_for_different_key_order(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger)
    run(backend, payload={"b": 1, "a": 2})
    run(backend, payload={"a": 2, "b": 1})
    c0 = client.messages.calls[0]["messages"][0]["content"]
    c1 = client.messages.calls[1]["messages"][0]["content"]
    assert c0 == c1


@pytest.mark.parametrize("tier,expected", [
    ("analysis", "claude-haiku-5-5"),
    ("research", "claude-sonnet-5-5"),
    ("head", "claude-opus-5-5"),
])
def test_model_chosen_by_tier(ledger, tier, expected):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger)
    r = run(backend, tier=tier)
    assert client.messages.calls[0]["model"] == expected
    assert r.model == expected


def test_model_override_from_settings(ledger):
    settings = make_settings(MODEL_ANALYSIS="claude-sonnet-5-5")
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger, settings)
    run(backend, tier="analysis")
    assert client.messages.calls[0]["model"] == "claude-sonnet-5-5"


def test_cache_ttl_5m(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger, cache_ttl="5m")
    run(backend)
    assert client.messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_effort_passed_in_output_config(ledger, effort):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger)
    run(backend, effort=effort)
    assert client.messages.calls[0]["output_config"]["effort"] == effort


def test_system_text_byte_identical(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.2)), ledger)
    system = "역할: 분석가\n\n규칙 1\n규칙 2  "
    run(backend, system=system)
    assert client.messages.calls[0]["system"][0]["text"] == system


# ---------------------------------------------------------------- 성공 결과
def test_success_result_fields(ledger):
    out = Decision(action="BUY", confidence=0.8, thesis="t")
    backend, _ = make_backend(
        fake_response(out, input_tokens=1000, output_tokens=200, cache_read=4000, cache_write=500, request_id="req_x"),
        ledger,
    )
    r = run(backend, role="head_trader", tier="head")
    assert r.ok is True and r.error is None
    assert r.output == out and isinstance(r.output, Decision)
    assert r.role == "head_trader"
    assert r.model == "claude-opus-5-5"
    assert r.input_tokens == 1000 and r.cached_tokens == 4000 and r.output_tokens == 200
    assert r.request_id == "req_x"
    assert r.latency_s >= 0.0
    p_in, p_cached, p_out = PRICES_USD_PER_MTOK["claude-opus-5-5"]
    expected = (1000 * p_in + 4000 * p_cached + 200 * p_out + 500 * p_in * CACHE_WRITE_MULTIPLIER["1h"]) / 1e6
    assert r.cost_usd == pytest.approx(expected)


def test_usage_recorded_in_ledger(ledger):
    out = Decision(action="HOLD", confidence=0.1)
    backend, _ = make_backend(
        fake_response(out, input_tokens=1000, output_tokens=200, cache_read=4000, cache_write=500, request_id="req_x"),
        ledger,
    )
    r = run(backend, role="analyst", tier="analysis")
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["role"] == "analyst"
    assert rec["model"] == "claude-haiku-5-5"
    assert rec["input_tokens"] == 1000
    assert rec["cached_tokens"] == 4000
    assert rec["cache_write_tokens"] == 500
    assert rec["output_tokens"] == 200
    assert rec["request_id"] == "req_x"
    p_in, p_cached, p_out = PRICES_USD_PER_MTOK["claude-haiku-5-5"]
    expected = (1000 * p_in + 4000 * p_cached + 200 * p_out + 500 * p_in * 2.0) / 1e6
    assert rec["cost_usd"] == pytest.approx(expected)
    assert r.cost_usd == pytest.approx(expected)
    assert ledger.spent_today() == pytest.approx(expected)


def test_usage_none_cache_fields_treated_as_zero(ledger):
    out = Decision(action="HOLD", confidence=0.1)
    backend, _ = make_backend(fake_response(out, cache_read=None, cache_write=None), ledger)
    r = run(backend)
    assert r.ok and r.cached_tokens == 0
    rec = json.loads(ledger.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["cached_tokens"] == 0 and rec["cache_write_tokens"] == 0


def test_missing_request_id_is_none(ledger):
    resp = fake_response(Decision(action="HOLD", confidence=0.1))
    del resp._request_id
    backend, _ = make_backend(resp, ledger)
    assert run(backend).request_id is None


def test_parsed_output_dict_is_validated_into_model(ledger):
    backend, _ = make_backend(fake_response({"action": "BUY", "confidence": 0.9}), ledger)
    r = run(backend)
    assert r.ok and isinstance(r.output, Decision) and r.output.action == "BUY"


def test_parsed_output_other_model_is_converted(ledger):
    class Other(BaseModel):
        action: str
        confidence: float

    backend, _ = make_backend(fake_response(Other(action="HOLD", confidence=0.3)), ledger)
    r = run(backend)
    assert r.ok and isinstance(r.output, Decision)


# ---------------------------------------------------------------- 정지 사유 / 파싱
def test_refusal_with_category(ledger):
    details = SimpleNamespace(type="refusal", category="cyber", explanation="x")
    backend, _ = make_backend(
        fake_response(None, stop_reason="refusal", stop_details=details, input_tokens=50, output_tokens=0), ledger
    )
    r = run(backend)
    assert r.ok is False and r.output is None
    assert r.error == "refusal:cyber"
    assert r.input_tokens == 50
    # 토큰은 소비됐으므로 원장에 기록된다
    assert len(ledger.path.read_text(encoding="utf-8").splitlines()) == 1


def test_refusal_without_details(ledger):
    backend, _ = make_backend(fake_response(None, stop_reason="refusal", stop_details=None), ledger)
    assert run(backend).error == "refusal:unknown"


def test_refusal_details_as_dict(ledger):
    backend, _ = make_backend(fake_response(None, stop_reason="refusal", stop_details={"category": "bio"}), ledger)
    assert run(backend).error == "refusal:bio"


def test_refusal_ignores_parsed_output(ledger):
    backend, _ = make_backend(
        fake_response(Decision(action="BUY", confidence=0.9), stop_reason="refusal", stop_details=None), ledger
    )
    r = run(backend)
    assert r.ok is False and r.output is None and r.error.startswith("refusal:")


def test_max_tokens(ledger):
    backend, _ = make_backend(
        fake_response(Decision(action="BUY", confidence=0.9), stop_reason="max_tokens", output_tokens=800), ledger
    )
    r = run(backend)
    assert r.ok is False and r.output is None and r.error == "max_tokens"
    assert r.output_tokens == 800
    assert r.cost_usd > 0


def test_parsed_output_none_is_parse_error(ledger):
    backend, _ = make_backend(fake_response(None, stop_reason="end_turn"), ledger)
    r = run(backend)
    assert r.ok is False and r.output is None and r.error == "parse_error"
    assert r.cost_usd > 0  # 사용량은 기록됨


def test_parsed_output_invalid_dict_is_parse_error(ledger):
    backend, _ = make_backend(fake_response({"action": "SELL", "confidence": "high"}), ledger)
    assert run(backend).error == "parse_error"


def test_content_property_raising_is_parse_error(ledger):
    class Boom:
        stop_reason = "end_turn"
        stop_details = None
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        _request_id = "r"

        @property
        def content(self):
            raise ValueError("bad json")

    backend, _ = make_backend(Boom(), ledger)
    assert run(backend).error == "parse_error"


def test_truncated_json_text_is_max_tokens_with_usage(ledger):
    """실제 SDK 가 max_tokens 로 자른 JSON 을 돌려줘도 stop_reason 이 우선하고 usage 는 기록된다."""
    backend, _ = make_backend(fake_response('{"action": "BU', stop_reason="max_tokens", output_tokens=800), ledger)
    r = run(backend)
    assert r.ok is False and r.error == "max_tokens"
    assert r.output_tokens == 800 and r.cost_usd > 0
    assert ledger.spent_today() > 0


def test_non_json_text_is_parse_error_with_usage(ledger):
    backend, _ = make_backend(fake_response("I cannot answer.", stop_reason="end_turn"), ledger)
    r = run(backend)
    assert r.ok is False and r.error == "parse_error"
    assert r.cost_usd > 0


def test_validation_error_raised_by_client_is_parse_error(ledger):
    """client 자체가 ValidationError 를 던지면(응답 객체 없음) parse_error, 기록할 usage 도 없다."""
    def responder(kw):
        try:
            Decision.model_validate({"action": "SELL"})
        except ValidationError as e:
            return e
        raise AssertionError

    backend, _ = make_backend(responder, ledger)
    r = run(backend)
    assert r.ok is False and r.error == "parse_error"
    assert r.cost_usd == 0.0


# ---------------------------------------------------------------- 예산
def test_budget_exceeded_propagates_before_api_call(tmp_path):
    ledger = CostLedger(tmp_path / "l.jsonl", 0.0001, 200.0)
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.1)), ledger)
    with pytest.raises(BudgetExceeded):
        run(backend, tier="head", max_tokens=4000)
    assert client.messages.calls == []
    assert not ledger.path.exists()


def test_budget_projection_uses_system_payload_and_max_tokens(tmp_path):
    # head(opus): 프로젝션 = (len(system)//4 + len(payload)//4)*4/1e6 + max_tokens*20/1e6
    ledger = CostLedger(tmp_path / "l.jsonl", 10.0, 200.0)
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.1)), ledger)
    # max_tokens=500_000 -> 10.0 USD 출력 예상 + 입력 > 10 -> 초과
    with pytest.raises(BudgetExceeded):
        run(backend, tier="head", max_tokens=500_000)
    assert client.messages.calls == []
    # max_tokens=400_000 -> 8.0 + 소량 입력 < 10 -> 통과
    r = run(backend, tier="head", max_tokens=400_000)
    assert r.ok
    assert len(client.messages.calls) == 1


def test_budget_exceeded_after_spend(ledger):
    backend, client = make_backend(
        fake_response(Decision(action="HOLD", confidence=0.1), input_tokens=0, output_tokens=500_000), ledger
    )
    r = run(backend, tier="head")  # 10.0 USD 소비
    assert r.ok and r.cost_usd == pytest.approx(10.0)
    with pytest.raises(BudgetExceeded):
        run(backend, tier="analysis")
    assert len(client.messages.calls) == 1


def test_budget_exceeded_logs_without_key(ledger, caplog):
    led = CostLedger(ledger.path, 0.0001, 200.0)
    backend, _ = make_backend(None, led)
    with caplog.at_level(logging.DEBUG, logger="tradingbot"):
        with pytest.raises(BudgetExceeded):
            run(backend)
    assert all(KEY not in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 예외 매핑
@pytest.mark.parametrize("exc,expected", [
    (lambda: status_error(anthropic.AuthenticationError, 401), "auth"),
    (lambda: status_error(anthropic.RateLimitError, 429), "rate_limit"),
    (lambda: anthropic.APITimeoutError(request=REQ), "timeout"),
    (lambda: status_error(anthropic.InternalServerError, 500), "server"),
    (lambda: status_error(anthropic.APIStatusError, 503), "server"),
    (lambda: status_error(anthropic.APIStatusError, 529, body={"error": {"type": "overloaded_error"}}), "server"),
    (lambda: anthropic.APIConnectionError(request=REQ), "network"),
    (lambda: anthropic.APIConnectionError(message="reset", request=REQ), "network"),
    (lambda: status_error(anthropic.BadRequestError, 400, body={"error": {"type": "invalid_request_error"}}), "api:invalid_request_error"),
    (lambda: status_error(anthropic.PermissionDeniedError, 403, body={"error": {"type": "permission_error"}}), "api:permission_error"),
    (lambda: status_error(anthropic.NotFoundError, 404), "api:NotFoundError"),
    (lambda: anthropic.APIError("generic", REQ, body=None), "api:APIError"),
    (lambda: RuntimeError("weird"), "unexpected:RuntimeError"),
])
def test_exception_mapping(ledger, exc, expected):
    backend, client = make_backend(lambda kw: exc(), ledger)
    r = run(backend)
    assert r.ok is False and r.output is None
    assert r.error == expected
    assert r.cost_usd == 0.0 and r.input_tokens == 0 and r.output_tokens == 0
    assert r.latency_s >= 0.0
    assert r.model == "claude-haiku-5-5"
    assert len(client.messages.calls) == 1
    assert not ledger.path.exists()  # 실패는 원장에 기록하지 않는다


def test_exception_does_not_kill_subsequent_calls(ledger):
    calls = {"n": 0}

    def responder(kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return status_error(anthropic.RateLimitError, 429)
        return fake_response(Decision(action="HOLD", confidence=0.1))

    backend, _ = make_backend(responder, ledger)
    assert run(backend).error == "rate_limit"
    assert run(backend).ok


def test_status_error_request_id_propagated(ledger):
    resp = httpx2.Response(500, request=REQ, headers={"request-id": "req_err_1"})
    backend, _ = make_backend(anthropic.InternalServerError("x", response=resp, body=None), ledger)
    r = run(backend)
    assert r.error == "server" and r.request_id == "req_err_1"


# ---------------------------------------------------------------- 비밀 비노출
def test_api_key_never_in_error_or_logs(ledger, caplog):
    exc = status_error(anthropic.AuthenticationError, 401, message=f"invalid x-api-key: {KEY}")
    backend, _ = make_backend(exc, ledger)
    with caplog.at_level(logging.DEBUG, logger="tradingbot"):
        r = run(backend, payload={"note": f"key is {KEY}"})
    assert r.error == "auth"
    assert KEY not in (r.error or "")
    assert KEY not in repr(r)
    assert KEY not in r.model_dump_json()
    for rec in caplog.records:
        assert KEY not in rec.getMessage()
        assert KEY not in str(rec.args)
    assert any(mask_secret(KEY) in rec.getMessage() for rec in caplog.records)


def test_api_key_in_unknown_error_type_is_masked(ledger, caplog):
    exc = status_error(anthropic.BadRequestError, 400, body={"error": {"type": f"bad-{KEY}"}})
    backend, _ = make_backend(exc, ledger)
    with caplog.at_level(logging.DEBUG, logger="tradingbot"):
        r = run(backend)
    assert r.error.startswith("api:")
    assert KEY not in r.error
    assert all(KEY not in rec.getMessage() for rec in caplog.records)


def test_api_key_in_refusal_category_is_masked(ledger):
    details = SimpleNamespace(category=f"cat-{KEY}")
    backend, _ = make_backend(fake_response(None, stop_reason="refusal", stop_details=details), ledger)
    r = run(backend)
    assert KEY not in r.error and r.error.startswith("refusal:")


def test_success_logs_have_no_key_or_full_prompt(ledger, caplog):
    out = Decision(action="HOLD", confidence=0.1, thesis="Z" * 1000)
    backend, _ = make_backend(fake_response(out), ledger)
    with caplog.at_level(logging.DEBUG, logger="tradingbot"):
        run(backend, system="S" * 5000, payload={"blob": "P" * 5000, "k": KEY})
    msgs = [rec.getMessage() for rec in caplog.records]
    assert msgs, "로그가 있어야 한다"
    for m in msgs:
        assert KEY not in m
        assert "P" * 300 not in m  # 프롬프트 본문은 앞 200자만
        assert "Z" * 300 not in m  # 응답 본문도 앞 200자만
        assert "S" * 300 not in m
    info = [rec for rec in caplog.records if rec.levelno == logging.INFO and rec.name == "tradingbot.llm.anthropic_backend"]
    assert len(info) == 1
    text = info[0].getMessage()
    for token in ("role=analyst", "model=claude-haiku-5-5", "in=1000", "out=200", "cost=", "latency=", "request_id=req_abc"):
        assert token in text


def test_ledger_cache_ttl_mismatch_warns(ledger, caplog):
    with caplog.at_level(logging.WARNING, logger="tradingbot.llm.anthropic_backend"):
        make_backend(None, ledger, cache_ttl="5m")
    assert any("cache_ttl" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------- 실제 SDK 시그니처 호환성
def test_parse_kwargs_are_accepted_by_real_sdk_signature(ledger):
    """가짜 client 에 넘긴 kwargs 가 설치된 SDK 의 messages.create 시그니처에 실제로 존재하는지."""
    import inspect

    from anthropic.resources.messages import Messages

    params = set(inspect.signature(Messages.create).parameters)
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.1)), ledger)
    run(backend)
    assert set(client.messages.calls[0]) <= params


# ---------------------------------------------------------------- 시간 예산 안의 재시도·사이클 기한 (CLAUDE-API-1)
from tradingbot.llm import anthropic_backend as _AB  # noqa: E402


def _retrying_backend(responder, ledger, monkeypatch, sleeps=None):
    backend, client = make_backend(responder, ledger)
    backend.max_retries = 2  # 직접 만든 client 와 같은 백엔드 재시도 정책
    monkeypatch.setattr(_AB.time, "sleep", lambda s: (sleeps.append(s) if sleeps is not None else None))
    return backend, client


def test_injected_client_is_not_retried_by_backend(ledger):
    backend, client = make_backend(lambda kw: status_error(anthropic.InternalServerError, 500), ledger)
    assert backend.max_retries == 0
    assert run(backend).error == "server"
    assert len(client.messages.calls) == 1


def test_fast_retryable_errors_are_retried_within_budget(ledger, monkeypatch):
    sleeps: list[float] = []
    backend, client = _retrying_backend(lambda kw: status_error(anthropic.InternalServerError, 500), ledger,
                                        monkeypatch, sleeps)
    r = run(backend, timeout_s=30.0)
    assert r.error == "server"
    assert len(client.messages.calls) == 3
    assert len(sleeps) == 2
    # 첫 시도는 역할 timeout 그대로, 재시도는 남은 시간(<= timeout)
    assert client.messages.calls[0]["timeout"] == 30.0
    assert all(0 < c["timeout"] <= 30.0 for c in client.messages.calls[1:])


def test_retry_then_success(ledger, monkeypatch):
    seq = iter([status_error(anthropic.RateLimitError, 429), fake_response(Decision(action="HOLD", confidence=0.1))])
    backend, client = _retrying_backend(lambda kw: next(seq), ledger, monkeypatch)
    r = run(backend)
    assert r.ok and len(client.messages.calls) == 2


def test_timeout_is_not_retried(ledger, monkeypatch):
    backend, client = _retrying_backend(lambda kw: anthropic.APITimeoutError(request=REQ), ledger, monkeypatch)
    assert run(backend, timeout_s=100.0).error == "timeout"
    assert len(client.messages.calls) == 1


def test_non_retryable_status_is_not_retried(ledger, monkeypatch):
    backend, client = _retrying_backend(lambda kw: status_error(anthropic.BadRequestError, 400), ledger, monkeypatch)
    assert run(backend).error.startswith("api:")
    assert len(client.messages.calls) == 1


def test_retry_skipped_when_budget_too_small(ledger, monkeypatch):
    # 남은 시간(timeout 3s)이 재시도 최소 시간보다 작으면 다시 보내지 않는다
    backend, client = _retrying_backend(lambda kw: status_error(anthropic.InternalServerError, 503), ledger,
                                        monkeypatch)
    assert run(backend, timeout_s=3.0).error == "server"
    assert len(client.messages.calls) == 1


def test_deadline_passed_skips_call(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.1)), ledger)
    backend.set_deadline(0.0)
    r = run(backend)
    assert r.ok is False and r.output is None and r.error == "deadline"
    assert client.messages.calls == []
    assert not ledger.path.exists()
    backend.set_deadline(None)
    assert run(backend).ok


def test_deadline_caps_request_timeout(ledger):
    backend, client = make_backend(fake_response(Decision(action="HOLD", confidence=0.1)), ledger)
    backend.set_deadline(20.0)
    assert run(backend, timeout_s=100.0).ok
    assert 0 < client.messages.calls[0]["timeout"] <= 20.0


def test_budget_projection_prices_system_as_cache_write(tmp_path):
    # head(opus) 1h: system 4000자 -> 1000토큰 x1.3 = 1300 x 2(캐시 쓰기) = 2600 input 토큰 상당
    led = CostLedger(tmp_path / "l.jsonl", 10.0, 200.0)
    got = _AB.project_call_cost(led, "claude-opus-5-5", 4000, 0, 0, cache_ttl="1h")
    assert got == pytest.approx(2600 * 4.0 / 1e6)
    assert got > led.estimate("claude-opus-5-5", 1000, 0, 0) * CACHE_WRITE_MULTIPLIER["1h"]
