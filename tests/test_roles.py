"""roles.py (SPEC §20.2) 테스트."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import get_args

import pytest

from tradingbot.agents import schemas
from tradingbot.agents.roles import JOURNAL_OUTPUT_MODELS, ROLES, RoleSpec, get_role
from tradingbot.llm.backend import Tier
from tradingbot.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "technical": ("analysis", "prompts/technical.md", ("technical-basic", "ichimoku", "candlestick", "smc", "volatility", "minute-analysis"), schemas.TechnicalOutput, 3000, "low", 30, False),
    "derivatives_flow": ("analysis", "prompts/derivatives_flow.md", ("perp-funding-basis", "crypto-derivatives", "liquidation-heatmap", "investor-lenses", "market-microstructure"), schemas.FlowOutput, 5000, "low", 35, True),
    "news_sentiment": ("analysis", "prompts/news_sentiment.md", ("event-driven", "sentiment-analysis", "research-discipline"), schemas.NewsOutput, 5000, "low", 35, True),
    "bull": ("research", "prompts/bull.md", ("behavioral-finance", "research-discipline", "event-driven"), schemas.BullOutput, 5000, "medium", 60, False),
    "bear": ("research", "prompts/bear.md", ("investor-lenses", "behavioral-finance", "research-discipline", "correlation-regime", "perp-funding-basis"), schemas.BearOutput, 5000, "medium", 60, False),
    "risk_manager": ("research", "prompts/risk_manager.md", ("risk-analysis", "correlation-regime", "correlation-analysis", "asset-allocation", "market-microstructure", "execution-model", "thesis-tracker", "trade-journal", "strategy-dev-manager", "volatility", "behavioral-finance"), schemas.RiskOutput, 7000, "medium", 75, True),
    "head": ("head", "prompts/head.md", ("investor-lenses", "behavioral-finance", "thesis-tracker", "sentiment-analysis", "event-driven", "research-discipline"), schemas.HeadOutput, 9000, "high", 100, True),
    "journal": ("analysis", "prompts/journal.md", ("trade-journal", "performance-attribution", "report-generate", "strategy-dev-manager"), schemas.JournalPostTrade, 4000, "low", 60, False),
}


def test_role_ids_exact():
    assert set(ROLES) == set(EXPECTED)


@pytest.mark.parametrize("role_id", sorted(EXPECTED))
def test_role_spec_matches_spec(role_id):
    tier, prompt, skills, model, max_tokens, effort, timeout, batched = EXPECTED[role_id]
    spec = ROLES[role_id]
    assert isinstance(spec, RoleSpec)
    assert spec.role_id == role_id
    assert spec.tier == tier
    assert spec.prompt_file == prompt
    assert spec.skills == skills
    assert spec.output_model is model
    assert spec.max_tokens == max_tokens
    assert spec.effort == effort
    assert spec.timeout_s == pytest.approx(timeout)
    assert isinstance(spec.timeout_s, float)
    assert spec.batched is batched


def test_rolespec_is_frozen():
    spec = ROLES["technical"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.max_tokens = 1  # type: ignore[misc]


def test_rolespec_field_order():
    names = [f.name for f in dataclasses.fields(RoleSpec)]
    assert names == ["role_id", "tier", "prompt_file", "skills", "output_model", "max_tokens", "effort", "timeout_s", "batched"]


@pytest.mark.parametrize("role_id", sorted(EXPECTED))
def test_tiers_effort_valid(role_id):
    spec = ROLES[role_id]
    assert spec.tier in get_args(Tier)
    assert spec.effort in ("low", "medium", "high")
    assert isinstance(spec.skills, tuple)
    assert len(spec.skills) == len(set(spec.skills)), "스킬 중복 금지"
    assert spec.max_tokens > 0 and spec.timeout_s > 0


@pytest.mark.parametrize("role_id", sorted(EXPECTED))
def test_output_model_has_conservative_example(role_id):
    model = ROLES[role_id].output_model
    ex = model.conservative_example()
    assert isinstance(ex, model)


def test_journal_output_models():
    assert JOURNAL_OUTPUT_MODELS == (schemas.JournalPostTrade, schemas.JournalDaily)
    assert ROLES["journal"].output_model in JOURNAL_OUTPUT_MODELS


def test_get_role_ok_and_unknown():
    assert get_role("head") is ROLES["head"]
    with pytest.raises(KeyError, match="알 수 없는 역할"):
        get_role("nope")


@pytest.mark.parametrize("role_id", sorted(EXPECTED))
def test_prompt_file_exists_in_repo(role_id):
    path = ROOT / ROLES[role_id].prompt_file
    if not (ROOT / "prompts").is_dir():
        pytest.skip("prompts 디렉터리 없음")
    assert path.is_file(), f"{path} 없음"


@pytest.mark.parametrize("role_id", sorted(EXPECTED))
def test_repo_skills_assignment_matches_frontmatter(role_id):
    """SKILL.md frontmatter roles 와 RoleSpec.skills 배정이 일치(스킬 렌더 = 역할 배정)."""
    skills_dir = ROOT / "skills"
    if not skills_dir.is_dir():
        pytest.skip("skills 디렉터리 없음")
    reg = SkillRegistry(skills_dir)
    spec = ROLES[role_id]
    for name in spec.skills:
        assert name in reg, f"스킬 {name} 이 로드되지 않음"
    assert {s.name for s in reg.for_role(role_id)} == set(spec.skills)
