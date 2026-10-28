"""Claude API 백엔드 (SPEC §8.3).

``client.messages.parse()``로 구조화 출력(``output_format=<pydantic model>``)을 받는다.

- 호출 전 ``ledger.check(예상 비용)`` → ``BudgetExceeded``는 그대로 전파(호출자가 LLM 일시정지).
- system 블록은 ``cache_control={"type":"ephemeral","ttl":cache_ttl}``로 캐시한다. 역할별 system은
  바이트 단위로 고정해야 하며(날짜·사이클·심볼 금지) 변동 데이터는 전부 ``user_payload``로 보낸다.
- ``thinking``·``tool_choice``는 보내지 않는다(5.5 계열은 기본 adaptive).
- 모든 예외/거부는 ``ok=False`` + 짧은 ``error`` 코드로 보고하고, 어떤 로그·에러 문자열에도
  API 키가 들어가지 않는다(``config.mask_secret``).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

from tradingbot.config import ConfigError, Settings, mask_secret
from tradingbot.llm.backend import AgentResult, Effort, Tier
from tradingbot.llm.cost import BudgetExceeded, CostLedger

logger = logging.getLogger("tradingbot.llm.anthropic_backend")

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RETRIES = 2
_LOG_SNIPPET = 200


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class AnthropicBackend:
    """``AgentBackend`` 구현. 테스트는 ``client``에 가짜 객체(``.messages.parse``)를 주입한다."""

    def __init__(
        self,
        settings: Settings,
        ledger: CostLedger,
        *,
        client: anthropic.Anthropic | None = None,
        cache_ttl: Literal["5m", "1h"] = "1h",
    ) -> None:
        if cache_ttl not in ("5m", "1h"):
            raise ValueError(f"cache_ttl은 5m/1h 중 하나: {cache_ttl!r}")
        self.settings = settings
        self.ledger = ledger
        self.cache_ttl: Literal["5m", "1h"] = cache_ttl
        self._secret = settings.anthropic_api_key or ""
        if client is None:
            if not settings.anthropic_api_key:
                raise ConfigError("AnthropicBackend 에는 ANTHROPIC_API_KEY 가 필요합니다")
            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                max_retries=DEFAULT_MAX_RETRIES,
                timeout=DEFAULT_TIMEOUT_S,
            )
        self.client = client
        ledger_ttl = getattr(ledger, "cache_ttl", None)
        if ledger_ttl is not None and ledger_ttl != cache_ttl:
            logger.warning("ledger.cache_ttl=%s 와 backend cache_ttl=%s 가 다름: 캐시 쓰기 비용이 어긋날 수 있음", ledger_ttl, cache_ttl)

    # ------------------------------------------------------------------ 보조
    def _scrub(self, text: Any) -> str:
        """문자열에서 API 키를 가린다. 모든 로그/에러 문자열은 이 함수를 거친다."""
        s = str(text)
        if self._secret and self._secret in s:
            s = s.replace(self._secret, mask_secret(self._secret))
        return s

    def _map_exception(self, e: BaseException) -> str:
        """가장 구체적인 것부터: auth > rate_limit > timeout > server > network > api:<type>."""
        if isinstance(e, anthropic.AuthenticationError):
            return "auth"
        if isinstance(e, anthropic.RateLimitError):
            return "rate_limit"
        if isinstance(e, anthropic.APITimeoutError):
            return "timeout"
        if isinstance(e, anthropic.APIStatusError):
            status = getattr(e, "status_code", 0) or 0
            if status >= 500:
                return "server"
            etype = getattr(e, "type", None) or type(e).__name__
            return f"api:{self._scrub(etype)}"
        if isinstance(e, anthropic.APIConnectionError):
            return "network"
        if isinstance(e, anthropic.APIError):
            etype = getattr(e, "type", None) or type(e).__name__
            return f"api:{self._scrub(etype)}"
        if isinstance(e, ValidationError):
            return "parse_error"
        return f"unexpected:{type(e).__name__}"

    def _fail(self, role: str, model: str, error: str, latency: float, **usage: Any) -> AgentResult:
        return AgentResult(role=role, ok=False, output=None, error=error, model=model, latency_s=latency, **usage)

    @staticmethod
    def _coerce_output(parsed: Any, output_model: type[BaseModel]) -> BaseModel | None:
        if parsed is None:
            return None
        if isinstance(parsed, output_model):
            return parsed
        try:
            if isinstance(parsed, BaseModel):
                return output_model.model_validate(parsed.model_dump())
            if isinstance(parsed, (dict, list)):
                return output_model.model_validate(parsed)
            if isinstance(parsed, (str, bytes)):
                return output_model.model_validate_json(parsed)
        except (ValidationError, ValueError, TypeError):
            return None
        return None

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
        model = self.settings.model_for(tier)
        user_text = _dumps(user_payload)

        # 1) 예산 검사 — 초과면 API 호출 없이 BudgetExceeded 전파
        projected = self.ledger.estimate(model, len(system) // 4 + len(user_text) // 4, 0, max_tokens)
        try:
            self.ledger.check(projected)
        except BudgetExceeded:
            logger.warning("LLM 예산 초과로 호출 생략 role=%s model=%s projected=%.4f", role, model, projected)
            raise

        logger.debug(
            "요청 role=%s tier=%s model=%s effort=%s max_tokens=%d payload=%s",
            role, tier, model, effort, max_tokens, self._scrub(user_text[:_LOG_SNIPPET]),
        )

        # 2) 호출
        started = time.monotonic()
        try:
            resp = self.client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral", "ttl": self.cache_ttl},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
                output_format=output_model,
                output_config={"effort": effort},
                timeout=timeout_s,
            )
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 - 모든 LLM 오류는 사이클을 죽이지 않는다
            latency = time.monotonic() - started
            error = self._map_exception(e)
            request_id = getattr(e, "request_id", None)
            logger.warning(
                "LLM 호출 실패 role=%s model=%s error=%s latency=%.2fs request_id=%s detail=%s",
                role, model, error, latency, request_id,
                self._scrub(getattr(e, "message", None) or str(e))[:_LOG_SNIPPET],
            )
            if error.startswith("unexpected:"):
                logger.debug("예상 못한 예외 유형 %s", type(e).__name__)
            return self._fail(role, model, error, latency, request_id=request_id if isinstance(request_id, str) else None)
        latency = time.monotonic() - started

        # 3) 사용량 기록 (거부/잘림이어도 토큰은 소비됐다)
        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        request_id = getattr(resp, "_request_id", None)
        if not isinstance(request_id, str):
            request_id = None
        cost = self.ledger.record(
            role=role,
            model=model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            request_id=request_id,
        )
        usage_kw = dict(
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            request_id=request_id,
        )
        logger.info(
            "LLM 응답 role=%s model=%s in=%d cached=%d cache_write=%d out=%d cost=%.5f latency=%.2fs request_id=%s stop=%s",
            role, model, input_tokens, cached_tokens, cache_write_tokens, output_tokens, cost, latency, request_id,
            getattr(resp, "stop_reason", None),
        )

        # 4) 정지 사유
        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None)
            if category is None and isinstance(details, dict):
                category = details.get("category")
            category = self._scrub(category or "unknown")
            logger.warning("LLM 거부 role=%s model=%s category=%s", role, model, category)
            return self._fail(role, model, f"refusal:{category}", latency, **usage_kw)
        if stop_reason == "max_tokens":
            logger.warning("LLM 출력 잘림(max_tokens=%d) role=%s model=%s", max_tokens, role, model)
            return self._fail(role, model, "max_tokens", latency, **usage_kw)

        # 5) 구조화 출력 검증
        try:
            parsed = getattr(resp, "parsed_output", None)
        except Exception as e:  # noqa: BLE001 - SDK 내부 검증 실패도 parse_error
            logger.warning("LLM 출력 파싱 실패 role=%s model=%s: %s", role, model, self._scrub(e)[:_LOG_SNIPPET])
            parsed = None
        output = self._coerce_output(parsed, output_model)
        if output is None:
            logger.warning("LLM 출력 검증 실패 role=%s model=%s output_model=%s", role, model, output_model.__name__)
            return self._fail(role, model, "parse_error", latency, **usage_kw)

        logger.debug("응답 본문 role=%s: %s", role, self._scrub(output.model_dump_json())[:_LOG_SNIPPET])
        return AgentResult(role=role, ok=True, output=output, error=None, model=model, latency_s=latency, **usage_kw)

    def __repr__(self) -> str:
        return f"AnthropicBackend(models={self.settings.model_analysis},{self.settings.model_research},{self.settings.model_head}, cache_ttl={self.cache_ttl}, key={mask_secret(self._secret)})"
