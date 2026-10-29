"""System review — Claude API integration (anthropic SDK 2.3.0).

Proof tests for the Claude API lens. Tests that are expected to FAIL against the current code are the
confirmed findings; the rest pin behaviour verified as correct. Everything is offline: the real SDK client is
driven through an ``httpx2.MockTransport`` (no network, fake key).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from tradingbot import cycle as C
from tradingbot.agents.roles import ROLES
from tradingbot.agents.runner import AgentRunner
from tradingbot.agents.schemas import ROLE_OUTPUT_MODELS, JournalDaily
from tradingbot.config import load_settings
from tradingbot.llm import anthropic_backend as AB
from tradingbot.llm.anthropic_backend import AnthropicBackend, _output_format
from tradingbot.llm.cost import CACHE_WRITE_MULTIPLIER, PRICES_USD_PER_MTOK, BudgetExceeded, CostLedger
from tradingbot.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "sk-ant-api03-FAKEKEYFORTESTS-0123456789abcdef"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

# Documented list prices (shared/models.md, 2026-10): (input, cache_read, output) USD / MTok
DOC_PRICES = {
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
# The most expensive per-token model a user can configure (claude-fable-5-1: $10 / $50, config accepts any claude-*)
MOST_EXPENSIVE_KNOWN = ("claude-fable-5-1", 10.0, 50.0)


def _settings(**extra: str):
    env = {"MODE": "paper", "AGENT_BACKEND": "mock", "ANTHROPIC_API_KEY": FAKE_KEY,
           "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
    env.update(extra)
    return load_settings(env)


def _ledger(tmp_path: Path, daily: float = 10.0, monthly: float = 200.0) -> CostLedger:
    return CostLedger(tmp_path / "cost_ledger.jsonl", daily, monthly, now=lambda: NOW)


def _example_json(output_model) -> str:
    ex = output_model.conservative_example()
    return ex.model_dump_json(by_alias=True)


def _message(model: str, text: str, *, stop_reason: str = "end_turn", usage: dict | None = None,
             stop_details: dict | None = None, thinking: bool = True) -> dict:
    content: list[dict] = []
    if thinking:  # adaptive thinking is on by default on the 5.5 family; display omitted -> empty text
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    content.append({"type": "text", "text": text})
    body = {
        "id": "msg_01", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": usage or {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return body


class _Transport:
    """Records request bodies; ``responder(body) -> httpx2.Response | Exception``."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.bodies: list[dict] = []
        self.attempts = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode("utf-8"))
        with self._lock:
            self.bodies.append(body)
            self.attempts += 1
        out = self.responder(body)
        if isinstance(out, BaseException):
            raise out
        return out


def _ok_responder(body: dict) -> httpx2.Response:
    # answer with the conservative example of whichever schema was requested
    title = body["output_config"]["format"]["schema"].get("title")
    model = next(m for m in ROLE_OUTPUT_MODELS.values() if m.__name__ == title)
    return httpx2.Response(200, json=_message(body["model"], _example_json(model)), headers={"request-id": "req_ok"})


def _real_backend(monkeypatch, tmp_path, responder, *, ledger: CostLedger | None = None, settings=None):
    """AnthropicBackend that builds its *own* real SDK client (defaults intact), wired to a mock transport."""
    transport = _Transport(responder)
    real_cls = anthropic.Anthropic

    def factory(**kw):
        return real_cls(**kw, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(transport)))

    monkeypatch.setattr(AB.anthropic, "Anthropic", factory)
    monkeypatch.setattr(anthropic._base_client.time, "sleep", lambda s: None)  # no real retry back-off waits
    led = ledger or _ledger(tmp_path)
    backend = AnthropicBackend(settings or _settings(), led)
    return backend, transport, led


def _runner(backend, settings=None) -> AgentRunner:
    return AgentRunner(settings or _settings(), backend, SkillRegistry(ROOT / "skills"), ROOT / "prompts")


def _payload_for(role: str, salt: str) -> dict:
    return {"role": role, "cycle": salt, "ts": salt, "symbols": [f"{salt}/USDT"]}


def _calls_for_every_role(salt: str) -> list[tuple]:
    calls: list[tuple] = []
    for role in ROLES:
        calls.append((role, _payload_for(role, salt)))
    calls.append(("journal", _payload_for("journal", salt), JournalDaily))
    return calls


# ===================================================================== (1) request kwargs vs SDK 2.3.0
def test_sdk_version_is_the_reviewed_one():
    assert anthropic.__version__ == "2.3.0"


def test_request_body_is_valid_for_messages_create_for_every_role(monkeypatch, tmp_path):
    backend, transport, _ = _real_backend(monkeypatch, tmp_path, _ok_responder)
    runner = _runner(backend)
    results = [runner.run(*c) if len(c) == 2 else runner.run(c[0], c[1], output_model=c[2])
               for c in _calls_for_every_role("A")]
    assert all(r.ok for r in results), [(r.role, r.error) for r in results]
    expected_model = {"analysis": "claude-haiku-5-5", "research": "claude-sonnet-5-5", "head": "claude-opus-5-5"}
    allowed_keys = {"model", "max_tokens", "system", "messages", "output_config"}
    for (call, body) in zip(_calls_for_every_role("A"), transport.bodies):
        spec = ROLES[call[0]]
        assert set(body) == allowed_keys, sorted(body)  # no thinking / tool_choice / temperature / top_p / stream
        assert body["model"] == expected_model[spec.tier]
        assert body["max_tokens"] == spec.max_tokens
        oc = body["output_config"]
        assert set(oc) == {"effort", "format"}
        assert oc["effort"] == spec.effort and oc["effort"] in ("low", "medium", "high")
        assert oc["format"]["type"] == "json_schema" and isinstance(oc["format"]["schema"], dict)
        assert body["messages"] == [{"role": "user", "content": json.dumps(call[1], ensure_ascii=False, sort_keys=True, default=str)}]
        assert len(body["system"]) == 1 and body["system"][0]["type"] == "text"


def test_per_request_timeout_is_forwarded(monkeypatch, tmp_path):
    seen: list[Any] = []
    backend, transport, _ = _real_backend(monkeypatch, tmp_path, _ok_responder)
    orig = backend.client.messages.create

    def spy(**kw):
        seen.append(kw.get("timeout"))
        return orig(**kw)

    monkeypatch.setattr(backend.client.messages, "create", spy)
    _runner(backend).run("head", {"x": 1})
    assert seen == [ROLES["head"].timeout_s]


# ===================================================================== (2) structured-output schema limits
_FORBIDDEN = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
              "minLength", "maxLength", "pattern", "minProperties", "maxProperties", "uniqueItems",
              "oneOf", "not", "if", "then", "else", "default"}


def _walk(node: Any, path: str, root: dict, errors: list[str], seen_refs: tuple[str, ...] = ()) -> None:
    if isinstance(node, list):
        for i, n in enumerate(node):
            _walk(n, f"{path}[{i}]", root, errors, seen_refs)
        return
    if not isinstance(node, dict):
        return
    for k in node:
        if k in _FORBIDDEN:
            errors.append(f"{path}: unsupported keyword {k}")
    if "minItems" in node and node["minItems"] not in (0, 1):
        errors.append(f"{path}: minItems {node['minItems']} unsupported")
    if "maxItems" in node:
        errors.append(f"{path}: maxItems unsupported")
    if "format" in node and node["format"] not in {"date-time", "time", "date", "duration", "email", "hostname",
                                                   "uri", "ipv4", "ipv6", "uuid"}:
        errors.append(f"{path}: string format {node['format']!r} unsupported")
    t = node.get("type")
    if t is not None and t not in ("object", "array", "string", "integer", "number", "boolean", "null"):
        errors.append(f"{path}: type {t!r}")
    if t == "object" or "properties" in node:
        if node.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        props = node.get("properties", {})
        if sorted(node.get("required", [])) != sorted(props):
            errors.append(f"{path}: required {node.get('required')} != properties {sorted(props)}")
    if "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/$defs/"):
            errors.append(f"{path}: non-local ref {ref}")
        else:
            name = ref.split("/")[-1]
            if name not in root.get("$defs", {}):
                errors.append(f"{path}: unresolvable ref {ref}")
            elif name in seen_refs:
                errors.append(f"{path}: recursive ref {ref}")
            else:
                _walk(root["$defs"][name], f"$defs/{name}", root, errors, seen_refs + (name,))
    for k, v in node.items():
        if k in ("$defs",):
            continue
        if isinstance(v, (dict, list)):
            _walk(v, f"{path}/{k}", root, errors, seen_refs)


@pytest.mark.parametrize("role_key", sorted(ROLE_OUTPUT_MODELS))
def test_output_format_schema_obeys_structured_output_limits(role_key):
    model = ROLE_OUTPUT_MODELS[role_key]
    fmt = _output_format(model)
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    errors: list[str] = []
    _walk(schema, "#", schema, errors)
    for name, d in schema.get("$defs", {}).items():
        _walk(d, f"$defs/{name}", schema, errors, (name,))
    assert schema.get("type") == "object"
    assert not errors, "\n".join(errors)
    json.dumps(schema)  # serialisable


@pytest.mark.parametrize("role_key", sorted(ROLE_OUTPUT_MODELS))
def test_schema_property_names_match_what_the_model_validates(role_key):
    """The JSON the API returns (keys = schema property names) must validate through model_validate_json."""
    model = ROLE_OUTPUT_MODELS[role_key]
    schema = _output_format(model)["schema"]
    ex = json.loads(_example_json(model))
    assert sorted(ex) == sorted(schema["properties"])
    assert model.model_validate_json(json.dumps(ex)) is not None


def test_transform_schema_is_the_sdk_one():
    from anthropic.lib._parse._transform import transform_schema
    assert AB._transform_schema is transform_schema


# ===================================================================== (3) prompt caching
def test_system_block_byte_identical_across_cycles_and_restarts(monkeypatch, tmp_path):
    backend, transport, _ = _real_backend(monkeypatch, tmp_path, _ok_responder)
    r1 = _runner(backend)
    for c in _calls_for_every_role("cycle1-2026-10-28T12:00"):
        r1.run(c[0], c[1], output_model=c[2] if len(c) == 3 else None)
    first = list(transport.bodies)
    transport.bodies.clear()
    r2 = _runner(backend)  # a fresh runner == process restart
    for c in _calls_for_every_role("cycle2-2026-10-28T12:15"):
        r2.run(c[0], c[1], output_model=c[2] if len(c) == 3 else None)
    second = list(transport.bodies)
    assert len(first) == len(second) == len(ROLES) + 1
    for a, b in zip(first, second):
        assert json.dumps(a["system"], ensure_ascii=False).encode() == json.dumps(b["system"], ensure_ascii=False).encode()
        assert a["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert a["messages"] != b["messages"]           # variable data only in the user turn
        assert a["output_config"] == b["output_config"]  # schema + effort pinned per role
        assert "cycle1" not in a["system"][0]["text"] and "2026-10-28" not in a["system"][0]["text"]
        # well above the 512-token cacheable minimum of the 5.5 family
        assert len(a["system"][0]["text"]) // 4 > 512


def test_backend_and_ledger_ttl_agree_in_main_wiring():
    led = CostLedger(Path("/nonexistent/x.jsonl"), 1, 1)
    be = AnthropicBackend(_settings(), led, client=object())  # type: ignore[arg-type]
    assert be.cache_ttl == led.cache_ttl == "1h"


# ===================================================================== (4) cost math
@pytest.mark.parametrize("model", sorted(DOC_PRICES))
def test_cost_per_model_matches_documented_prices(tmp_path, model):
    led = _ledger(tmp_path)
    p_in, p_read, p_out = DOC_PRICES[model]
    assert PRICES_USD_PER_MTOK[model] == (p_in, p_read, p_out)
    cost = led.record(role="r", model=model, input_tokens=1_000_000, cached_tokens=1_000_000,
                      output_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost == pytest.approx(p_in + p_read + p_out + 2.0 * p_in)
    assert CACHE_WRITE_MULTIPLIER["1h"] == 2.0 and CACHE_WRITE_MULTIPLIER["5m"] == 1.25


def test_backend_records_usage_fields_into_ledger(monkeypatch, tmp_path):
    usage = {"input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 30000,
             "cache_creation_input_tokens": 0}

    def responder(body):
        return httpx2.Response(200, json=_message(body["model"], _example_json(ROLE_OUTPUT_MODELS["head"]), usage=usage),
                               headers={"request-id": "req_1"})

    backend, _, led = _real_backend(monkeypatch, tmp_path, responder)
    res = _runner(backend).run("head", {"x": 1})
    assert res.ok and res.request_id == "req_1"
    exp = (1000 * 4.0 + 30000 * 0.20 + 2000 * 20.0) / 1e6
    assert res.cost_usd == pytest.approx(exp)
    assert led.spent_today() == pytest.approx(exp)


def test_unknown_model_fallback_is_actually_conservative(tmp_path):
    """SPEC §8.2: unknown models are estimated *conservatively*. config accepts any ``claude-*`` id
    (e.g. MODEL_HEAD=claude-fable-5-1 at $10/$50, or claude-opus-5 at $5/$25), but the fallback is Opus 5.5 at
    $4/$20, so the ledger under-records spend by up to 2.5x and the daily hard budget is overrun."""
    led = _ledger(tmp_path)
    name, p_in, p_out = MOST_EXPENSIVE_KNOWN
    settings = _settings(MODEL_HEAD=name)
    assert settings.model_head == name  # config accepts it
    cost = led.estimate(name, 1_000_000, 0, 1_000_000)
    assert cost >= p_in + p_out, f"unknown model {name} priced at {cost} USD/MTok-pair < list {p_in + p_out}"


def test_unknown_opus5_does_not_prefix_match_opus55_cheaper(tmp_path):
    led = _ledger(tmp_path)
    # claude-opus-5 ($5/$25) must not be priced below its list price
    assert led.estimate("claude-opus-5", 1_000_000, 0, 1_000_000) >= 5.0 + 25.0


# ===================================================================== (5) budget under parallel run_many
def test_parallel_run_many_never_overspends_daily_budget(monkeypatch, tmp_path):
    in_flight = {"n": 0, "max": 0}
    lock = threading.Lock()

    def responder(body):
        sys_tok = len(body["system"][0]["text"]) // 4
        user_tok = len(body["messages"][0]["content"]) // 4
        with lock:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
        time.sleep(0.05)
        with lock:
            in_flight["n"] -= 1
        usage = {"input_tokens": sys_tok + user_tok, "output_tokens": body["max_tokens"],
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        return httpx2.Response(200, json=_message(body["model"], _example_json(ROLE_OUTPUT_MODELS["technical"]),
                                                  usage=usage))

    led = _ledger(tmp_path, daily=1.0)
    backend, transport, led = _real_backend(monkeypatch, tmp_path, responder, ledger=led)
    runner = _runner(backend)
    one = led.estimate("claude-haiku-5-5", len(runner.system_prompt("technical")) // 4 + 20, 0, ROLES["technical"].max_tokens)
    # leave room for ~2.5 calls
    led.record(role="seed", model="claude-haiku-5-5", input_tokens=0, cached_tokens=0,
               output_tokens=int((1.0 - 2.5 * one) / 0.50 * 1e6))
    calls = [("technical", {"symbol": f"S{i}/USDT"}) for i in range(6)]
    with pytest.raises(BudgetExceeded):
        runner.run_many(calls)
    assert led.spent_today() <= 1.0 + 1e-9
    assert transport.attempts <= 3


def test_sequential_precheck_accounts_for_cache_write_premium(monkeypatch, tmp_path):
    """The pre-call projection prices the system prompt at 1x input, but a cold 1h-cache write costs 2x.
    Near the cap a call passes ``check`` and then pushes spend past the hard daily budget."""
    def responder(body):
        sys_tok = len(body["system"][0]["text"]) // 4
        user_tok = len(body["messages"][0]["content"]) // 4
        usage = {"input_tokens": user_tok, "output_tokens": body["max_tokens"],
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": sys_tok}
        return httpx2.Response(200, json=_message(body["model"], _example_json(ROLE_OUTPUT_MODELS["head"]), usage=usage))

    led = _ledger(tmp_path, daily=1.0)
    backend, transport, led = _real_backend(monkeypatch, tmp_path, responder, ledger=led)
    runner = _runner(backend)
    payload = {"x": 1}
    sys_text = runner.system_prompt("head")
    projected = led.estimate("claude-opus-5-5", len(sys_text) // 4 + len(json.dumps(payload)) // 4, 0,
                             ROLES["head"].max_tokens)
    # spent so that exactly the projection still fits
    seed_out = int((1.0 - projected - 1e-6) / 20.0 * 1e6)
    led.record(role="seed", model="claude-opus-5-5", input_tokens=0, cached_tokens=0, output_tokens=seed_out)
    # Fixed behaviour: the worst-case projection (cold 1h cache write) no longer fits, so the call is refused
    # before it reaches the API (BudgetExceeded -> LLM pause). If it is made, it must stay within the cap.
    try:
        res = runner.run("head", payload)
    except BudgetExceeded:
        assert transport.attempts == 0
    else:
        assert res.ok
    assert led.spent_today() <= 1.0, f"hard daily budget overrun: {led.spent_today():.4f} > 1.0"


# ===================================================================== (6) error mapping / abstain semantics
def _status(code: int, etype: str, headers: dict | None = None):
    def responder(body):
        return httpx2.Response(code, json={"type": "error", "error": {"type": etype, "message": f"boom {FAKE_KEY}"}},
                               headers={"request-id": "req_err", **(headers or {})})
    return responder


def _raise(exc_factory):
    def responder(body):
        return exc_factory()
    return responder


@pytest.mark.parametrize("responder,expected,attempts", [
    (_status(401, "authentication_error"), "auth", 1),
    (_status(429, "rate_limit_error", {"retry-after-ms": "1"}), "rate_limit", 3),
    (_status(500, "api_error"), "server", 3),
    (_status(529, "overloaded_error"), "server", 3),
    (_status(400, "invalid_request_error"), "api:invalid_request_error", 1),
    (_status(403, "permission_error"), "api:permission_error", 1),
    # a timed-out attempt used the role's whole time window: not retried (CLAUDE-API-1)
    (_raise(lambda: httpx2.ReadTimeout("slow")), "timeout", 1),
    (_raise(lambda: httpx2.ConnectError("down")), "network", 3),
])
def test_sdk_errors_map_to_abstain_codes(monkeypatch, tmp_path, caplog, responder, expected, attempts):
    backend, transport, led = _real_backend(monkeypatch, tmp_path, responder)
    caplog.set_level("DEBUG")
    res = _runner(backend).run("technical", {"x": 1})
    assert res.ok is False and res.output is None
    assert res.error == expected
    assert transport.attempts == attempts
    assert FAKE_KEY not in caplog.text and FAKE_KEY not in res.model_dump_json()
    assert led.spent_today() == 0.0


@pytest.mark.parametrize("stop_reason,stop_details,text,expected", [
    ("refusal", {"type": "refusal", "category": "cyber", "explanation": None}, "I can't", "refusal:cyber"),
    ("refusal", {"type": "refusal", "category": None, "explanation": None}, "", "refusal:unknown"),
    ("max_tokens", None, '{"symbol": "BTC/US', "max_tokens"),
    ("end_turn", None, "not json", "parse_error"),
    ("end_turn", None, '{"symbol": "BTC/USDT"}', "parse_error"),
    ("model_context_window_exceeded", None, '{"sym', "parse_error"),
])
def test_stop_reasons_abstain_but_usage_is_recorded(monkeypatch, tmp_path, stop_reason, stop_details, text, expected):
    usage = {"input_tokens": 1000, "output_tokens": 3000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def responder(body):
        return httpx2.Response(200, json=_message(body["model"], text, stop_reason=stop_reason, usage=usage,
                                                  stop_details=stop_details))

    backend, _, led = _real_backend(monkeypatch, tmp_path, responder)
    res = _runner(backend).run("technical", {"x": 1})
    assert res.ok is False and res.output is None and res.error == expected
    assert led.spent_today() == pytest.approx((1000 * 0.10 + 3000 * 0.50) / 1e6)


def test_auth_error_from_real_backend_pauses_llm_60_minutes(monkeypatch, tmp_path):
    import test_cycle as TC  # noqa: PLC0415 - reuse the offline cycle rig (not edited)

    rig = TC.Rig(tmp_path)
    backend, transport, _ = _real_backend(monkeypatch, tmp_path, _status(401, "authentication_error"),
                                          ledger=rig.ledger, settings=rig.settings)
    rig.cycle.runner.backend = backend
    rig.cycle.backend = backend
    rig.cycle.run_once()
    assert transport.attempts >= 1
    assert rig.state.llm_pause_reason == "auth_error"
    assert rig.state.llm_paused_until_ts == rig.clock.ms() + C.AUTH_PAUSE_MINUTES * 60_000
    assert C.AUTH_PAUSE_MINUTES == 60


# ===================================================================== (7) max_tokens / timeouts vs 5-minute deadline
def test_worst_case_llm_wall_time_fits_cycle_deadline(monkeypatch, tmp_path):
    """The pipeline runs four sequential LLM stages (analysts -> bull/bear -> risk -> head) and the cycle only
    checks its 300 s deadline at stage boundaries. The backend's client keeps the SDK's 2 automatic retries and
    each retry gets the full per-role ``timeout_s`` again, so one slow stage alone can take 3x its timeout."""
    backend, transport, _ = _real_backend(monkeypatch, tmp_path, _raise(lambda: httpx2.ReadTimeout("slow")))
    res = _runner(backend).run("head", {"x": 1})
    assert res.error == "timeout"
    attempts_per_call = transport.attempts  # what one hung call costs in timeouts
    stages = [("technical", "derivatives_flow", "news_sentiment"), ("bull", "bear"), ("risk_manager",), ("head",)]
    worst = sum(max(ROLES[r].timeout_s for r in stage) for stage in stages) * attempts_per_call
    assert worst <= C.CYCLE_DEADLINE_S, (
        f"worst-case LLM wall time {worst:.0f}s ({attempts_per_call} attempts/call) > cycle deadline {C.CYCLE_DEADLINE_S:.0f}s")
