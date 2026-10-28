"""에이전트 백엔드 계약과 오프라인 Mock 백엔드 (SPEC §8.1).

``AgentBackend.complete()``는 역할(role)·티어(tier)·시스템 프롬프트·사용자 페이로드를 받아
``output_model``(pydantic) 인스턴스를 ``AgentResult.output``에 담아 돌려준다.
실패는 예외가 아니라 ``ok=False`` + ``error`` 문자열로 보고한다(예산 초과만 예외).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger("tradingbot.llm.backend")

Tier = Literal["analysis", "research", "head"]
Effort = Literal["low", "medium", "high"]
T = TypeVar("T", bound=BaseModel)


class AgentResult(BaseModel):
    """한 번의 에이전트 호출 결과.

    ``output``은 호출자가 넘긴 ``output_model``의 인스턴스(성공 시) 또는 None.
    ``error``는 짧은 코드 문자열("refusal:<category>", "max_tokens", "parse_error",
    "auth", "rate_limit", "timeout", "server", "network", "api:<type>", "no_mock_response" 등).
    비밀값은 어떤 필드에도 들어가지 않는다.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    role: str
    ok: bool
    output: Any = None
    error: str | None = None
    model: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    request_id: str | None = None


@runtime_checkable
class AgentBackend(Protocol):
    """LLM 호출 추상화. 구현: ``AnthropicBackend``, ``MockBackend``."""

    def complete(
        self,
        *,
        role: str,
        tier: Tier,
        system: str,
        user_payload: dict,
        output_model: type[BaseModel],
        max_tokens: int,
        effort: Effort,
        timeout_s: float,
    ) -> AgentResult: ...


ResponseSpec = Callable[[dict], BaseModel] | BaseModel
DefaultFactory = Callable[[str, type[BaseModel], dict], BaseModel]


class MockBackend:
    """결정적 오프라인 백엔드.

    - ``responses[role]``: 고정 출력(BaseModel 인스턴스) 또는 ``payload -> BaseModel`` 팩토리.
      팩토리가 ``AgentResult``를 돌려주면 그대로 결과로 쓴다(실패 시뮬레이션용).
      값이 BaseModel *클래스*이면 그 클래스의 기본 예시를 만든다.
    - 역할에 설정이 없으면 ``default_factory(role, output_model, payload)``,
      그것도 없으면 ``output_model.conservative_example()``(있을 때),
      없으면 ``output_model()`` → ``output_model.model_construct()`` 순으로 시도하고
      전부 실패하면 ``ok=False, error="no_mock_response"``.
    - 모든 호출을 ``self.calls``(role, tier, payload, output_model, ...)에 기록한다.
    - 비용은 항상 0, 토큰도 0.
    """

    def __init__(
        self,
        responses: Mapping[str, ResponseSpec] | None = None,
        *,
        default_factory: DefaultFactory | None = None,
    ) -> None:
        self.responses: dict[str, ResponseSpec] = dict(responses or {})
        self.default_factory = default_factory
        self.calls: list[dict] = []

    # ------------------------------------------------------------------ 내부
    @staticmethod
    def _default_output(output_model: type[BaseModel]) -> BaseModel | None:
        example = getattr(output_model, "conservative_example", None)
        if callable(example):
            return example()
        try:
            return output_model()
        except (ValidationError, TypeError, ValueError):
            pass
        try:
            return output_model.model_construct()
        except Exception:  # noqa: BLE001 - 어떤 실패든 no_mock_response 로 보고
            return None

    def _resolve(self, role: str, output_model: type[BaseModel], payload: dict) -> BaseModel | AgentResult | None:
        spec = self.responses.get(role)
        if spec is not None:
            if isinstance(spec, BaseModel):
                return spec
            if isinstance(spec, type) and issubclass(spec, BaseModel):
                return self._default_output(spec)
            if callable(spec):
                return spec(payload)
            return spec  # type: ignore[return-value]
        if self.default_factory is not None:
            return self.default_factory(role, output_model, payload)
        return self._default_output(output_model)

    # ------------------------------------------------------------------ API
    def complete(
        self,
        *,
        role: str,
        tier: Tier,
        system: str,
        user_payload: dict,
        output_model: type[BaseModel],
        max_tokens: int,
        effort: Effort,
        timeout_s: float,
    ) -> AgentResult:
        payload = dict(user_payload)
        self.calls.append(
            {
                "role": role,
                "tier": tier,
                "payload": payload,
                "output_model": output_model.__name__,
                "system_len": len(system),
                "max_tokens": max_tokens,
                "effort": effort,
                "timeout_s": timeout_s,
            }
        )
        model_name = f"mock-{tier}"
        out = self._resolve(role, output_model, payload)
        if isinstance(out, AgentResult):
            return out
        if out is None:
            logger.warning("mock 응답 없음 role=%s output_model=%s", role, output_model.__name__)
            return AgentResult(role=role, ok=False, output=None, error="no_mock_response", model=model_name)
        logger.debug(
            "mock 응답 role=%s tier=%s payload=%s",
            role,
            tier,
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)[:200],
        )
        return AgentResult(role=role, ok=True, output=out, error=None, model=model_name)
