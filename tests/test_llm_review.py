"""적대적 리뷰 테스트 — tradingbot/llm (SPEC §8).

각 테스트는 현재 구현에서 실패해야 하고, 결함이 고쳐지면 통과해야 한다.
구현 파일은 수정하지 않는다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pytest
from anthropic.lib._parse._response import parse_response
from anthropic.types import Message, RefusalStopDetails, TextBlock, Usage
from pydantic import BaseModel, ConfigDict

from tradingbot.config import load_settings
from tradingbot.llm import AnthropicBackend, BudgetExceeded, CostLedger

KEY = "sk-ant-api03-REVIEWSECRETKEY-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["BUY", "HOLD"]
    confidence: float


def make_settings() -> object:
    return load_settings(
        {
            "MODE": "paper",
            "AGENT_BACKEND": "anthropic",
            "ANTHROPIC_API_KEY": KEY,
            "LLM_DAILY_BUDGET_USD": "10",
            "LLM_MONTHLY_BUDGET_USD": "200",
        }
    )


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "ledger.jsonl", 10.0, 200.0, now=lambda: NOW)


# ---------------------------------------------------------------- SDK 와 같은 동작을 하는 가짜 client
def sdk_message(text: str, stop_reason: str, *, stop_details=None, output_tokens: int = 800) -> Message:
    return Message(
        id="msg_review",
        type="message",
        role="assistant",
        model="claude-haiku-5-5",
        content=[TextBlock(type="text", text=text)],
        stop_reason=stop_reason,  # type: ignore[arg-type]
        stop_details=stop_details,
        stop_sequence=None,
        usage=Usage(
            input_tokens=1000,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=500,
        ),
    )


class SdkLikeMessages:
    """실제 anthropic SDK 의 ``messages.parse`` 와 같은 의미론:

    HTTP 응답은 성공(토큰 소비)했지만 본문이 output_format 으로 검증되지 않으면
    ``parse()`` 가 ``pydantic.ValidationError`` 를 던진다(anthropic 2.3.0 ``parse_response``를 그대로 사용).
    ``create()`` 는 같은 원본 Message 를 돌려준다(수동 파싱 경로용).
    """

    def __init__(self, message: Message):
        self.message = message
        self.calls: list[tuple[str, dict]] = []

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self.message

    def parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        return parse_response(output_format=kwargs.get("output_format"), response=self.message)


class SdkLikeClient:
    def __init__(self, message: Message):
        self.messages = SdkLikeMessages(message)


def run(backend: AnthropicBackend, payload: dict | None = None, *, max_tokens: int = 800):
    return backend.complete(
        role="analyst",
        tier="analysis",
        system="SYSTEM PROMPT",
        user_payload={"symbol": "BTC/USDT"} if payload is None else payload,
        output_model=Decision,
        max_tokens=max_tokens,
        effort="low",
        timeout_s=30.0,
    )


# ---------------------------------------------------------------- 1. max_tokens 잘림: 토큰은 소비됐는데 원장에 기록되지 않는다
def test_truncated_json_from_sdk_still_records_usage_in_ledger(ledger: CostLedger):
    """SPEC §8.3 (5): usage 는 항상 ledger.record 로 기록돼야 한다.

    실제 SDK 는 max_tokens 로 잘린 JSON 을 ``parse()`` 안에서 ValidationError 로 올린다.
    현재 구현은 이를 ``except Exception`` 으로 삼키고 usage 기록 단계에 도달하지 못해
    가장 비싼 실패(출력 max_tokens 전부 소비)가 예산에서 빠진다 → 예산 한도를 넘길 수 있다.
    """
    msg = sdk_message('{"action": "BU', "max_tokens", output_tokens=800)
    backend = AnthropicBackend(make_settings(), ledger, client=SdkLikeClient(msg))

    result = run(backend)

    assert result.ok is False
    assert ledger.spent_today() > 0.0, "잘린 응답의 토큰이 원장에 기록되지 않았다"
    assert result.cost_usd > 0.0
    assert result.output_tokens == 800


def test_truncated_json_from_sdk_maps_to_max_tokens_not_parse_error(ledger: CostLedger):
    """SPEC §8.3 (3): stop_reason == "max_tokens" → error="max_tokens".

    실제 SDK 경로에서는 stop_reason 을 보기 전에 ValidationError 가 나므로 현재 구현은 "parse_error" 를 돌려준다.
    호출자는 max_tokens 를 보고 토큰 한도를 조정해야 하므로 코드가 달라지면 안 된다.
    """
    msg = sdk_message('{"action": "BU', "max_tokens")
    backend = AnthropicBackend(make_settings(), ledger, client=SdkLikeClient(msg))

    result = run(backend)

    assert result.ok is False
    assert result.error == "max_tokens"


def test_refusal_text_from_sdk_maps_to_refusal_category(ledger: CostLedger):
    """SPEC §8.3 (3): stop_reason == "refusal" → error="refusal:<category>".

    거부 응답의 텍스트는 JSON 이 아니므로 실제 SDK 의 ``parse()`` 는 ValidationError 를 던진다.
    현재 구현은 이를 "parse_error" 로 보고하고 usage 도 기록하지 않는다.
    """
    msg = sdk_message(
        "I can't help with that request.",
        "refusal",
        stop_details=RefusalStopDetails(type="refusal", category="general_harms"),
        output_tokens=12,
    )
    backend = AnthropicBackend(make_settings(), ledger, client=SdkLikeClient(msg))

    result = run(backend)

    assert result.ok is False
    assert result.error == "refusal:general_harms"
    assert ledger.spent_today() > 0.0


# ---------------------------------------------------------------- 2. 요청 로그: 자르고 나서 가리기 → 키 일부 노출
def test_request_debug_log_does_not_leak_partial_api_key(ledger: CostLedger, caplog):
    """SPEC §0: 비밀값은 mask_secret 없이는 어떤 로그에도 들어가면 안 된다.

    요청 DEBUG 로그는 payload 를 200자로 자른 *뒤* 키를 가린다. 키가 절단 경계에 걸치면
    키의 앞부분(여기서는 30자 이상)이 그대로 로그에 남는다. 가리기는 자르기 전에 해야 한다.
    """
    msg = sdk_message('{"action":"HOLD","confidence":0.1}', "end_turn")
    backend = AnthropicBackend(make_settings(), ledger, client=SdkLikeClient(msg))
    # json.dumps(sort_keys) → '{"a": "xxx…x", "k": "<KEY>"}' ; 키가 200자 경계에 걸치도록 배치
    payload = {"a": "x" * 150, "k": KEY}
    snippet = json.dumps(payload, ensure_ascii=False, sort_keys=True)[:200]
    assert KEY[:20] in snippet and KEY not in snippet  # 테스트 전제: 키가 경계에 걸친다

    with caplog.at_level(logging.DEBUG, logger="tradingbot.llm.anthropic_backend"):
        run(backend, payload)

    assert KEY[:20] not in caplog.text, "API 키 앞부분이 요청 로그에 노출됐다"


# ---------------------------------------------------------------- 3. 원장 쓰기 실패를 삼킨다 → 재시작 후 예산 상태 소실
def test_ledger_write_failure_must_not_be_silently_swallowed(tmp_path: Path):
    """SPEC §0 에러 정책: 상태 저장 실패는 멈춘다.

    비용 원장은 일/월 예산 상태의 유일한 영속 저장소다(시작 시 파일을 읽어 복원).
    append 실패를 ERROR 로그만 남기고 넘기면 재시작 후 당일/당월 합계가 0 으로 복원돼
    월 예산을 조용히 넘길 수 있다. 쓰기 실패는 예외로 올라와야 한다.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    ledger = CostLedger(blocker / "ledger.jsonl", 10.0, 200.0, now=lambda: NOW)

    with pytest.raises(OSError):
        ledger.record(role="r", model="claude-haiku-5-5", input_tokens=1000, cached_tokens=0, output_tokens=100)


# ---------------------------------------------------------------- 4. 손상/조작된 원장의 음수 비용이 예산을 무력화한다
def test_restore_rejects_negative_cost_lines(tmp_path: Path):
    """원장 한 줄의 cost_usd 가 음수면(손상·조작) 당일 합계가 음수가 되어 check() 가 영원히 통과한다.

    NaN 은 걸러내면서 음수는 걸러내지 않는다. 고친 뒤에는 (a) 줄을 무시하거나 (b) 손상 상태로 보고
    시작을 거부해야 한다 — 둘 다 허용.
    """
    path = tmp_path / "ledger.jsonl"
    bad = {
        "ts": NOW.isoformat(), "date": "2026-10-28", "role": "r", "model": "claude-opus-5-5",
        "input_tokens": 1, "cached_tokens": 0, "cache_write_tokens": 0, "output_tokens": 1,
        "cost_usd": -1_000_000.0, "request_id": None,
    }
    path.write_text(json.dumps(bad, sort_keys=True) + "\n", encoding="utf-8")

    try:
        ledger = CostLedger(path, 10.0, 200.0, now=lambda: NOW)
    except Exception:
        return  # 손상 상태로 보고 시작 거부: 허용되는 수정
    assert ledger.spent_today() >= 0.0
    with pytest.raises(BudgetExceeded):
        ledger.check(10.0 + 1.0)
