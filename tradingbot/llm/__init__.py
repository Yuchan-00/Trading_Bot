"""LLM 백엔드 패키지 (SPEC §8).

- ``backend``: ``AgentResult``/``AgentBackend`` 프로토콜과 오프라인용 ``MockBackend``
- ``cost``: 토큰 단가표·비용 원장(``CostLedger``)·예산 초과 예외
- ``anthropic_backend``: Claude API 실제 호출 백엔드

이 패키지는 네트워크·API 키 없이 import 가능하다.
"""
from __future__ import annotations

from tradingbot.llm.anthropic_backend import AnthropicBackend
from tradingbot.llm.backend import AgentBackend, AgentResult, MockBackend
from tradingbot.llm.cost import PRICES_USD_PER_MTOK, BudgetExceeded, CostLedger

__all__ = [
    "AgentBackend",
    "AgentResult",
    "AnthropicBackend",
    "BudgetExceeded",
    "CostLedger",
    "MockBackend",
    "PRICES_USD_PER_MTOK",
]
