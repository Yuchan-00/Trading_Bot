"""runner.py (SPEC §20.4, v2.1) 테스트 — MockBackend, 임시 prompts/skills 디렉터리."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import pytest

from tradingbot.agents import schemas
from tradingbot.agents.roles import ROLES
from tradingbot.agents.runner import SKILLS_HEADER, SKILLS_MAX_TOKENS, AgentRunner
from tradingbot.config import load_settings
from tradingbot.llm.backend import AgentBackend, AgentResult, MockBackend
from tradingbot.llm.cost import BudgetExceeded
from tradingbot.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def settings(tmp_path):
    return load_settings({"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "data")})


def _skill(root: Path, name: str, roles: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test {name}\nroles: [{roles}]\nversion: 1\nlicense: MIT\n---\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def skills_dir(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "technical-basic", "technical", "추세는 EMA20/50 정렬로 본다.")
    _skill(root, "ichimoku", "technical, head", "구름 위에서만 롱을 고려한다.")
    _skill(root, "risk-analysis", "risk_manager", "손절 2% 상한을 지킨다.")
    return root


@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    for role_id in ROLES:
        (d / f"{role_id}.md").write_text(f"# Role: {role_id}\nYou are the {role_id} agent.\n", encoding="utf-8")
    return d


@pytest.fixture
def registry(skills_dir):
    return SkillRegistry(skills_dir)


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def runner(settings, backend, registry, prompts_dir):
    return AgentRunner(settings, backend, registry, prompts_dir)


# ---------------------------------------------------------------- system_prompt
def test_backend_protocol(backend):
    assert isinstance(backend, AgentBackend)


def test_system_prompt_structure(runner, registry):
    sp = runner.system_prompt("technical")
    assert sp.startswith("# Role: technical\nYou are the technical agent.\n")
    assert SKILLS_HEADER in sp
    prompt, skills_part = sp.split(SKILLS_HEADER, 1)
    assert prompt == "# Role: technical\nYou are the technical agent.\n"
    assert skills_part == registry.render("technical", max_tokens=SKILLS_MAX_TOKENS)
    assert "## Skill: technical-basic" in skills_part
    assert "## Skill: ichimoku" in skills_part
    assert "risk-analysis" not in skills_part


def test_system_prompt_byte_identical_and_cached(runner, prompts_dir):
    a = runner.system_prompt("technical")
    b = runner.system_prompt("technical")
    assert a.encode("utf-8") == b.encode("utf-8")
    assert a is b
    # 디스크 파일이 바뀌어도 캐시된 값이 유지된다(사이클 중 바이트 고정)
    (prompts_dir / "technical.md").write_text("CHANGED", encoding="utf-8")
    assert runner.system_prompt("technical") is a
    runner.clear_cache()
    assert runner.system_prompt("technical").startswith("CHANGED")


def test_system_prompt_identical_across_runner_instances(settings, backend, skills_dir, prompts_dir):
    r1 = AgentRunner(settings, backend, SkillRegistry(skills_dir), prompts_dir)
    r2 = AgentRunner(settings, MockBackend(), SkillRegistry(skills_dir), prompts_dir)
    for role_id in ROLES:
        assert r1.system_prompt(role_id).encode() == r2.system_prompt(role_id).encode()


def test_system_prompt_same_after_runs(runner, backend):
    before = runner.system_prompt("head")
    for i in range(3):
        runner.run("head", {"role": "head", "cycle": i, "now": f"2026-10-2{i}"})
    after = runner.system_prompt("head")
    assert before.encode() == after.encode()
    # MockBackend 가 받은 system 길이가 모든 호출에서 같다
    assert len({c["system_len"] for c in backend.calls}) == 1


def test_system_prompt_has_no_cycle_or_date(runner):
    for role_id in ROLES:
        sp = runner.system_prompt(role_id)
        assert not re.search(r"\d{4}-\d{2}-\d{2}", sp)
        assert not re.search(r"\d{1,2}:\d{2}(:\d{2})?\s*(UTC)?", sp)
        assert "cycle_id" not in sp.lower()
        assert not re.search(r"cycle\s*#?\s*\d+", sp, re.IGNORECASE)


def test_system_prompt_no_skills_for_role(runner):
    sp = runner.system_prompt("bull")  # 임시 스킬 중 bull 배정 없음
    assert sp.endswith(SKILLS_HEADER)


def test_system_prompt_preserves_crlf(settings, backend, registry, tmp_path):
    d = tmp_path / "p2"
    d.mkdir()
    (d / "technical.md").write_bytes(b"line1\r\nline2\r\n")
    r = AgentRunner(settings, backend, registry, d)
    assert r.system_prompt("technical").startswith("line1\r\nline2\r\n")


def test_missing_prompt_file_raises_korean(settings, backend, registry, tmp_path):
    empty = tmp_path / "empty_prompts"
    empty.mkdir()
    r = AgentRunner(settings, backend, registry, empty)
    with pytest.raises(FileNotFoundError, match="프롬프트 파일이 없습니다") as ei:
        r.system_prompt("technical")
    assert "technical.md" in str(ei.value)
    with pytest.raises(FileNotFoundError):
        r.run("technical", {})
    assert backend.calls == []


def test_missing_prompts_dir_raises(settings, backend, registry, tmp_path):
    r = AgentRunner(settings, backend, registry, tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        r.system_prompt("head")


def test_unknown_role(runner):
    with pytest.raises(KeyError):
        runner.system_prompt("ghost")
    with pytest.raises(KeyError):
        runner.run("ghost", {})
    with pytest.raises(KeyError):
        runner.run_many([("technical", {}), ("ghost", {})])


def test_prompt_path_uses_filename(runner, prompts_dir):
    assert runner.prompt_path("risk_manager") == prompts_dir / "risk_manager.md"


def test_skills_render_uses_24000_budget(settings, backend, prompts_dir, tmp_path):
    """v2.1: 6000 토큰 기본 예산이면 잘릴 스킬도 24000 예산에서는 전부 포함."""
    root = tmp_path / "bigskills"
    big = "가" * 16000  # ≈ 4000 토큰(len//4)
    for name in ("technical-basic", "ichimoku", "candlestick"):
        _skill(root, name, "technical", big)
    reg = SkillRegistry(root)
    assert "candlestick" in reg.render("technical")  # 이름순: candlestick 먼저
    assert "technical-basic" not in reg.render("technical")  # 기본 6000 이면 잘림
    r = AgentRunner(settings, backend, reg, prompts_dir)
    sp = r.system_prompt("technical")
    for name in ("technical-basic", "ichimoku", "candlestick"):
        assert f"## Skill: {name}" in sp


def test_skill_assignment_mismatch_warns(runner, caplog):
    caplog.set_level(logging.WARNING, logger="tradingbot.agents.runner")
    runner.system_prompt("head")  # head 에 ichimoku 가 렌더되지만 RoleSpec 에 없음, 배정 스킬은 누락
    text = caplog.text
    assert "배정 스킬 누락" in text
    assert "ichimoku" in text


# ---------------------------------------------------------------- run
def test_run_passes_spec_to_backend(runner, backend):
    payload = {"role": "technical", "symbol": "BTC/USDT"}
    res = runner.run("technical", payload)
    assert res.ok is True
    assert isinstance(res.output, schemas.TechnicalOutput)
    call = backend.calls[-1]
    spec = ROLES["technical"]
    assert call["role"] == "technical"
    assert call["tier"] == spec.tier
    assert call["payload"] == payload
    assert call["output_model"] == "TechnicalOutput"
    assert call["max_tokens"] == spec.max_tokens
    assert call["effort"] == spec.effort
    assert call["timeout_s"] == spec.timeout_s
    assert call["system_len"] == len(runner.system_prompt("technical"))


class RecordingBackend:
    def __init__(self, result_fn=None):
        self.kwargs: list[dict] = []
        self.result_fn = result_fn

    def complete(self, **kw):
        self.kwargs.append(kw)
        if self.result_fn:
            return self.result_fn(kw)
        return AgentResult(role=kw["role"], ok=True, output=kw["output_model"].conservative_example(), model="rec")


@pytest.mark.parametrize("role_id", sorted(ROLES))
def test_run_every_role_exact_kwargs(settings, registry, prompts_dir, role_id):
    be = RecordingBackend()
    r = AgentRunner(settings, be, registry, prompts_dir)
    payload = {"role": role_id, "hard_limits": {"max_stop_pct": 2}}
    res = r.run(role_id, payload)
    spec = ROLES[role_id]
    assert res.ok and isinstance(res.output, spec.output_model)
    kw = be.kwargs[0]
    assert set(kw) == {"role", "tier", "system", "user_payload", "output_model", "max_tokens", "effort", "timeout_s"}
    assert kw["role"] == role_id
    assert kw["tier"] == spec.tier
    assert kw["system"] is r.system_prompt(role_id)
    assert kw["user_payload"] is payload
    assert kw["output_model"] is spec.output_model
    assert kw["max_tokens"] == spec.max_tokens
    assert kw["effort"] == spec.effort
    assert kw["timeout_s"] == spec.timeout_s


def test_run_journal_default_and_override(runner, backend):
    r1 = runner.run("journal", {"role": "journal"})
    assert isinstance(r1.output, schemas.JournalPostTrade)
    r2 = runner.run("journal", {"role": "journal"}, output_model=schemas.JournalDaily)
    assert isinstance(r2.output, schemas.JournalDaily)
    assert backend.calls[-1]["output_model"] == "JournalDaily"
    # 같은 journal 시스템 프롬프트 사용
    assert backend.calls[-1]["system_len"] == backend.calls[-2]["system_len"]


def test_run_budget_exceeded_propagates(settings, registry, prompts_dir):
    def boom(kw):
        raise BudgetExceeded("일 예산 초과")

    r = AgentRunner(settings, RecordingBackend(boom), registry, prompts_dir)
    with pytest.raises(BudgetExceeded):
        r.run("head", {})


def test_run_budget_exceeded_via_mock_factory(settings, registry, prompts_dir):
    def factory(payload):
        raise BudgetExceeded("월 예산 초과")

    r = AgentRunner(settings, MockBackend({"bull": factory}), registry, prompts_dir)
    with pytest.raises(BudgetExceeded):
        r.run("bull", {})


@pytest.mark.parametrize("exc", [RuntimeError("x"), ValueError("y"), TimeoutError(), ConnectionError("net"), KeyError("k")])
def test_run_other_exceptions_become_abstain(settings, registry, prompts_dir, exc):
    def boom(kw):
        raise exc

    r = AgentRunner(settings, RecordingBackend(boom), registry, prompts_dir)
    res = r.run("technical", {})
    assert res.ok is False
    assert res.output is None
    assert res.role == "technical"
    assert res.error == f"exception:{type(exc).__name__}"


def test_exception_message_secret_not_leaked(settings, registry, prompts_dir, caplog):
    secret = "sk-ant-SUPERSECRET123"

    def boom(kw):
        raise RuntimeError(f"auth failed key={secret}")

    caplog.set_level(logging.DEBUG)
    r = AgentRunner(settings, RecordingBackend(boom), registry, prompts_dir)
    res = r.run("technical", {})
    assert secret not in (res.error or "")
    assert secret not in res.model_dump_json()
    assert secret not in caplog.text


def test_run_backend_failure_result_passthrough(settings, registry, prompts_dir):
    fail = AgentResult(role="bear", ok=False, error="rate_limit", model="m", latency_s=1.5)
    r = AgentRunner(settings, MockBackend({"bear": lambda p: fail}), registry, prompts_dir)
    res = r.run("bear", {})
    assert res.ok is False and res.error == "rate_limit" and res.latency_s == 1.5


def test_run_dict_output_is_validated(settings, registry, prompts_dir):
    good = schemas.TechnicalOutput.conservative_example().model_dump()

    def fn(kw):
        return AgentResult(role=kw["role"], ok=True, output=dict(good))

    r = AgentRunner(settings, RecordingBackend(fn), registry, prompts_dir)
    res = r.run("technical", {})
    assert res.ok and isinstance(res.output, schemas.TechnicalOutput)


def test_run_wrong_output_type_is_parse_error(settings, registry, prompts_dir):
    def fn(kw):
        return AgentResult(role=kw["role"], ok=True, output=schemas.BearOutput.conservative_example())

    r = AgentRunner(settings, RecordingBackend(fn), registry, prompts_dir)
    res = r.run("technical", {})
    assert res.ok is False and res.error == "parse_error" and res.output is None


def test_run_invalid_dict_is_parse_error(settings, registry, prompts_dir):
    def fn(kw):
        return AgentResult(role=kw["role"], ok=True, output={"symbol": "BTC/USDT"})

    r = AgentRunner(settings, RecordingBackend(fn), registry, prompts_dir)
    res = r.run("technical", {})
    assert res.ok is False and res.error == "parse_error"


def test_run_non_agentresult_from_backend(settings, registry, prompts_dir):
    r = AgentRunner(settings, RecordingBackend(lambda kw: {"ok": True}), registry, prompts_dir)
    res = r.run("technical", {})
    assert res.ok is False and res.error == "bad_backend_result"


def test_run_sets_latency_when_backend_reports_zero(runner):
    res = runner.run("technical", {})
    assert res.latency_s >= 0.0


def test_mock_no_response_is_abstain(settings, registry, prompts_dir):
    r = AgentRunner(settings, MockBackend({"technical": lambda p: None}), registry, prompts_dir)
    res = r.run("technical", {})
    assert res.ok is False and res.error == "no_mock_response"


# ---------------------------------------------------------------- run_many
def test_run_many_empty(runner):
    assert runner.run_many([]) == []


def test_run_many_preserves_order_with_varying_latency(settings, registry, prompts_dir):
    delays = {"a": 0.08, "b": 0.0, "c": 0.04, "d": 0.01, "e": 0.06, "f": 0.0, "g": 0.02}

    def fn(payload):
        time.sleep(delays[payload["tag"]])
        return schemas.TechnicalOutput.conservative_example(symbol=payload["tag"])

    r = AgentRunner(settings, MockBackend({"technical": fn}), registry, prompts_dir, max_workers=4)
    tags = list(delays)
    results = r.run_many([("technical", {"tag": t}) for t in tags])
    assert [res.output.symbol for res in results] == tags


def test_run_many_mixed_roles_order(runner):
    calls = [("technical", {}), ("derivatives_flow", {}), ("news_sentiment", {}), ("bull", {}), ("bear", {})]
    results = runner.run_many(calls)
    assert [r.role for r in results] == [c[0] for c in calls]
    assert isinstance(results[1].output, schemas.FlowOutput)
    assert isinstance(results[4].output, schemas.BearOutput)


def test_run_many_actually_parallel(settings, registry, prompts_dir):
    lock = threading.Lock()
    state = {"cur": 0, "max": 0}
    barrier = threading.Barrier(3, timeout=5)

    def fn(payload):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        barrier.wait()
        with lock:
            state["cur"] -= 1
        return schemas.BullOutput.conservative_example()

    r = AgentRunner(settings, MockBackend({"bull": fn}), registry, prompts_dir, max_workers=6)
    results = r.run_many([("bull", {"i": i}) for i in range(3)])
    assert all(x.ok for x in results)
    assert state["max"] == 3


def test_run_many_respects_max_workers(settings, registry, prompts_dir):
    lock = threading.Lock()
    state = {"cur": 0, "max": 0}

    def fn(payload):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.02)
        with lock:
            state["cur"] -= 1
        return schemas.BullOutput.conservative_example()

    r = AgentRunner(settings, MockBackend({"bull": fn}), registry, prompts_dir, max_workers=2)
    r.run_many([("bull", {}) for _ in range(6)])
    assert state["max"] <= 2


def test_run_many_failure_isolated(settings, registry, prompts_dir):
    def fn(payload):
        if payload["i"] == 1:
            raise RuntimeError("boom")
        return schemas.TechnicalOutput.conservative_example(symbol=str(payload["i"]))

    r = AgentRunner(settings, MockBackend({"technical": fn}), registry, prompts_dir)
    results = r.run_many([("technical", {"i": i}) for i in range(3)])
    assert [x.ok for x in results] == [True, False, True]
    assert results[1].error == "exception:RuntimeError"
    assert results[2].output.symbol == "2"


def test_run_many_budget_exceeded_propagates(settings, registry, prompts_dir):
    def fn(payload):
        if payload["i"] == 2:
            raise BudgetExceeded("일 예산 초과")
        return schemas.TechnicalOutput.conservative_example()

    be = MockBackend({"technical": fn})
    r = AgentRunner(settings, be, registry, prompts_dir)
    with pytest.raises(BudgetExceeded):
        r.run_many([("technical", {"i": i}) for i in range(4)])
    assert len(be.calls) == 4  # 다른 작업은 끝까지 실행됨


def test_run_many_with_output_model_override(runner):
    results = runner.run_many([("journal", {}), ("journal", {}, schemas.JournalDaily)])
    assert isinstance(results[0].output, schemas.JournalPostTrade)
    assert isinstance(results[1].output, schemas.JournalDaily)


def test_run_many_bad_item_shape(runner):
    with pytest.raises(ValueError):
        runner.run_many([("technical",)])  # type: ignore[list-item]


def test_run_many_missing_prompt_raises_before_calls(settings, backend, registry, tmp_path):
    d = tmp_path / "partial"
    d.mkdir()
    (d / "technical.md").write_text("tech", encoding="utf-8")
    r = AgentRunner(settings, backend, registry, d)
    with pytest.raises(FileNotFoundError):
        r.run_many([("technical", {}), ("bull", {})])
    assert backend.calls == []


def test_run_many_system_prompt_identical_in_threads(settings, registry, prompts_dir):
    be = RecordingBackend()
    r = AgentRunner(settings, be, registry, prompts_dir)
    r.run_many([("technical", {"i": i}) for i in range(8)])
    systems = {id(kw["system"]) for kw in be.kwargs}
    assert len(systems) == 1


def test_concurrent_first_access_single_build(settings, registry, prompts_dir, monkeypatch):
    r = AgentRunner(settings, MockBackend(), registry, prompts_dir)
    counter = {"n": 0}
    orig = r._build_system_prompt

    def slow_build(role_id):
        counter["n"] += 1
        time.sleep(0.02)
        return orig(role_id)

    monkeypatch.setattr(r, "_build_system_prompt", slow_build)
    out: list[str] = []
    ts = [threading.Thread(target=lambda: out.append(r.system_prompt("head"))) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert counter["n"] == 1
    assert len({id(x) for x in out}) == 1


def test_invalid_max_workers(settings, backend, registry, prompts_dir):
    with pytest.raises(ValueError):
        AgentRunner(settings, backend, registry, prompts_dir, max_workers=0)


def test_repr(runner):
    assert "AgentRunner" in repr(runner)


# ---------------------------------------------------------------- 실제 저장소 프롬프트·스킬 (v2.1)
@pytest.mark.parametrize("role_id", sorted(ROLES))
def test_repo_all_assigned_skills_rendered(settings, backend, role_id):
    """SPEC v2.1: 모든 역할의 배정 스킬 전체가 시스템 프롬프트 렌더에 포함."""
    if not (ROOT / "prompts").is_dir() or not (ROOT / "skills").is_dir():
        pytest.skip("저장소 prompts/skills 없음")
    r = AgentRunner(settings, backend, SkillRegistry(ROOT / "skills"), ROOT / "prompts")
    sp = r.system_prompt(role_id)
    for name in ROLES[role_id].skills:
        assert f"## Skill: {name}\n" in sp, f"{role_id}: 스킬 {name} 누락"
    assert sp == r.system_prompt(role_id)
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", sp), "시스템 프롬프트에 날짜 금지(캐시)"
