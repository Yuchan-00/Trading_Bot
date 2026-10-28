"""역할 정의 (SPEC §20.2).

각 역할(role)의 티어·프롬프트 파일·배정 스킬·출력 모델·토큰/effort/타임아웃·배치 여부를
한 곳에 고정한다. ``journal`` 은 기본 출력 모델이 ``JournalPostTrade`` 이며, 일일 리뷰는
``AgentRunner.run(..., output_model=JournalDaily)`` 로 호출 시 덮어쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    FlowOutput,
    HeadOutput,
    JournalDaily,
    JournalPostTrade,
    NewsOutput,
    RiskOutput,
    TechnicalOutput,
)
from tradingbot.llm.backend import Tier

__all__ = ["RoleSpec", "ROLES", "JOURNAL_OUTPUT_MODELS", "get_role"]


@dataclass(frozen=True)
class RoleSpec:
    """한 역할의 불변 설정."""

    role_id: str
    tier: Tier
    prompt_file: str
    skills: tuple[str, ...]
    output_model: type[BaseModel]
    max_tokens: int
    effort: Literal["low", "medium", "high"]
    timeout_s: float
    batched: bool


ROLES: dict[str, RoleSpec] = {
    "technical": RoleSpec(
        role_id="technical",
        tier="analysis",
        prompt_file="prompts/technical.md",
        skills=("technical-basic", "ichimoku", "candlestick", "smc", "volatility", "minute-analysis"),
        output_model=TechnicalOutput,
        max_tokens=3000,
        effort="low",
        timeout_s=30.0,
        batched=False,
    ),
    "derivatives_flow": RoleSpec(
        role_id="derivatives_flow",
        tier="analysis",
        prompt_file="prompts/derivatives_flow.md",
        skills=("perp-funding-basis", "crypto-derivatives", "liquidation-heatmap", "investor-lenses", "market-microstructure"),
        output_model=FlowOutput,
        max_tokens=5000,
        effort="low",
        timeout_s=35.0,
        batched=True,
    ),
    "news_sentiment": RoleSpec(
        role_id="news_sentiment",
        tier="analysis",
        prompt_file="prompts/news_sentiment.md",
        skills=("event-driven", "sentiment-analysis", "research-discipline"),
        output_model=NewsOutput,
        max_tokens=5000,
        effort="low",
        timeout_s=35.0,
        batched=True,
    ),
    "bull": RoleSpec(
        role_id="bull",
        tier="research",
        prompt_file="prompts/bull.md",
        skills=("behavioral-finance", "research-discipline", "event-driven"),
        output_model=BullOutput,
        max_tokens=5000,
        effort="medium",
        timeout_s=60.0,
        batched=False,
    ),
    "bear": RoleSpec(
        role_id="bear",
        tier="research",
        prompt_file="prompts/bear.md",
        skills=("investor-lenses", "behavioral-finance", "research-discipline", "correlation-regime", "perp-funding-basis"),
        output_model=BearOutput,
        max_tokens=5000,
        effort="medium",
        timeout_s=60.0,
        batched=False,
    ),
    "risk_manager": RoleSpec(
        role_id="risk_manager",
        tier="research",
        prompt_file="prompts/risk_manager.md",
        skills=(
            "risk-analysis",
            "correlation-regime",
            "correlation-analysis",
            "asset-allocation",
            "market-microstructure",
            "execution-model",
            "thesis-tracker",
            "trade-journal",
            "strategy-dev-manager",
            "volatility",
            "behavioral-finance",
        ),
        output_model=RiskOutput,
        max_tokens=7000,
        effort="medium",
        timeout_s=75.0,
        batched=True,
    ),
    "head": RoleSpec(
        role_id="head",
        tier="head",
        prompt_file="prompts/head.md",
        skills=("investor-lenses", "behavioral-finance", "thesis-tracker", "sentiment-analysis", "event-driven", "research-discipline"),
        output_model=HeadOutput,
        max_tokens=9000,
        effort="high",
        timeout_s=100.0,
        batched=True,
    ),
    "journal": RoleSpec(
        role_id="journal",
        tier="analysis",
        prompt_file="prompts/journal.md",
        skills=("trade-journal", "performance-attribution", "report-generate", "strategy-dev-manager"),
        output_model=JournalPostTrade,  # 일일 리뷰는 호출 시 JournalDaily 로 지정
        max_tokens=4000,
        effort="low",
        timeout_s=60.0,
        batched=False,
    ),
}

# journal 역할이 허용하는 출력 모델(사후분석 / 일일 리뷰)
JOURNAL_OUTPUT_MODELS: tuple[type[BaseModel], ...] = (JournalPostTrade, JournalDaily)


def get_role(role_id: str) -> RoleSpec:
    """역할 조회. 없으면 명확한 메시지의 KeyError."""
    try:
        return ROLES[role_id]
    except KeyError:
        raise KeyError(f"알 수 없는 역할: {role_id!r} (가능: {', '.join(sorted(ROLES))})") from None
