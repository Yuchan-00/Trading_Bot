"""역할별 구조화 출력 모델 (SPEC §20.1).

규칙
- 모든 필드는 필수(기본값 없음): Claude 구조화 출력(`messages.parse(output_format=...)`)이 모든 키를 채운다.
- 범위 제약은 JSON 스키마가 아니라 ``field_validator`` 로 건다(API 스키마를 단순하게 유지). 위반 = 파싱 실패 = 해당 역할 기권.
- ``conservative_example()`` 은 "아무 것도 하지 않는" 안전값이다. MockBackend 의 기본 응답이며, 테스트의 기준점이다.
- 자유 텍스트 필드는 한국어로 쓰도록 프롬프트가 지시한다(스키마는 언어를 강제하지 않는다).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Direction = Literal["long", "flat", "avoid"]
Posture = Literal["capitulation", "fear", "neutral", "greed", "euphoria"]
Level3 = Literal["low", "moderate", "high"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _unit(name: str):
    """0..1 범위 검증자 팩토리."""

    def _check(cls, v: float) -> float:  # noqa: ANN001
        if v != v or not (0.0 <= v <= 1.0):
            raise ValueError(f"{name} 은 0..1 이어야 함: {v}")
        return float(v)

    return _check


def _range(name: str, lo: float, hi: float, *, lo_open: bool = False):
    def _check(cls, v: float) -> float:  # noqa: ANN001
        if v != v or v < lo or v > hi or (lo_open and v == lo):
            raise ValueError(f"{name} 범위 위반({lo}{'<' if lo_open else '<='}x<={hi}): {v}")
        return v

    return _check


# ---------------------------------------------------------------- technical
class TechnicalOutput(_Out):
    symbol: str
    direction: Direction
    setup_type: Literal["trend_continuation", "mean_reversion", "breakout", "none"]
    timing: Literal["now", "wait_pullback", "wait_confirmation"]
    mtf_agreement: bool
    score: int
    confidence: float
    stop_candidate_price: float | None
    stop_pct: float | None
    sl_reference: Literal["zone", "kijun", "atr", "none"]
    target_candidate_price: float | None
    tp_pct: float | None
    invalidation: str
    noise_check: bool
    exit_flag: bool
    exit_reason: str | None
    evidence: list[str]

    _v_conf = field_validator("confidence")(_unit("confidence"))

    @field_validator("score")
    @classmethod
    def _v_score(cls, v: int) -> int:
        if not -5 <= v <= 5:
            raise ValueError(f"score 는 -5..5: {v}")
        return v

    @field_validator("stop_pct")
    @classmethod
    def _v_stop(cls, v: float | None) -> float | None:
        if v is not None and (v != v or not 0.0 < v <= 2.0):
            raise ValueError(f"stop_pct 는 (0,2]: {v}")
        return v

    @field_validator("tp_pct")
    @classmethod
    def _v_tp(cls, v: float | None) -> float | None:
        if v is not None and (v != v or not 0.0 < v <= 4.0):
            raise ValueError(f"tp_pct 는 (0,4]: {v}")
        return v

    @classmethod
    def conservative_example(cls, symbol: str = "BTC/USDT") -> "TechnicalOutput":
        return cls(
            symbol=symbol, direction="flat", setup_type="none", timing="wait_confirmation",
            mtf_agreement=False, score=0, confidence=0.0, stop_candidate_price=None, stop_pct=None,
            sl_reference="none", target_candidate_price=None, tp_pct=None,
            invalidation="해당 없음(셋업 없음)", noise_check=False, exit_flag=False, exit_reason=None,
            evidence=["per_tf.1h.vote: none"],
        )


# ---------------------------------------------------------------- derivatives_flow
class FlowSymbolView(_Out):
    symbol: str
    posture: Posture
    crowding_risk: Level3
    squeeze_direction: Literal["up", "down", "none"]
    cascade_risk: Level3
    nearest_long_liq_cluster_pct_below: float | None
    stop_inside_cascade: bool
    washout_setup: bool
    block_new_longs: bool
    block_reason: str | None
    flow_bias: Literal["long", "neutral", "avoid"]
    confidence: float
    evidence: list[str]

    _v_conf = field_validator("confidence")(_unit("confidence"))

    @classmethod
    def conservative_example(cls, symbol: str = "BTC/USDT") -> "FlowSymbolView":
        return cls(
            symbol=symbol, posture="neutral", crowding_risk="moderate", squeeze_direction="none",
            cascade_risk="moderate", nearest_long_liq_cluster_pct_below=None, stop_inside_cascade=False,
            washout_setup=False, block_new_longs=False, block_reason=None, flow_bias="neutral",
            confidence=0.0, evidence=["data_confidence: none"],
        )


class FlowOutput(_Out):
    per_symbol: list[FlowSymbolView]
    market_wide_note: str
    data_confidence: Literal["high", "low"]

    @classmethod
    def conservative_example(cls, symbols: tuple[str, ...] = ("BTC/USDT",)) -> "FlowOutput":
        return cls(per_symbol=[FlowSymbolView.conservative_example(s) for s in symbols],
                   market_wide_note="자료 없음", data_confidence="low")


# ---------------------------------------------------------------- news_sentiment
class NewsEvent(_Out):
    headline_id: str
    score: float
    event_type: Literal["hack", "regulatory", "listing", "delisting", "macro", "etf", "exchange", "protocol", "sentiment", "technical_break", "other"]
    symbols: list[str]
    summary: str
    stale: bool

    _v_score = field_validator("score")(_range("score", -1.0, 1.0))


class NewsOutput(_Out):
    events: list[NewsEvent]
    risk_event: bool
    risk_event_symbols: list[str]
    market_tone: float
    market_note: str
    feed_health_note: str

    _v_tone = field_validator("market_tone")(_range("market_tone", -1.0, 1.0))

    @classmethod
    def conservative_example(cls) -> "NewsOutput":
        return cls(events=[], risk_event=False, risk_event_symbols=[], market_tone=0.0,
                   market_note="새 헤드라인 없음(정보 없음 ≠ 조용한 시장)", feed_health_note="피드 미조회")


# ---------------------------------------------------------------- bull / bear
class ThesisPoint(_Out):
    claim: str
    evidence_field: str


class BullOutput(_Out):
    symbol: str
    case_strength: int
    thesis_points: list[ThesisPoint]
    catalysts: list[str]
    underreaction_evidence: str
    entry_trigger: str
    expected_horizon_bars: int
    invalidation_level: float | None
    disconfirming_evidence: list[str]
    main_risk_to_bull_case: str
    what_would_disprove: str
    confidence: float

    _v_conf = field_validator("confidence")(_unit("confidence"))

    @field_validator("case_strength")
    @classmethod
    def _v_cs(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError(f"case_strength 는 1..5: {v}")
        return v

    @classmethod
    def conservative_example(cls, symbol: str = "BTC/USDT") -> "BullOutput":
        return cls(
            symbol=symbol, case_strength=1, thesis_points=[], catalysts=[], underreaction_evidence="없음",
            entry_trigger="없음", expected_horizon_bars=0, invalidation_level=None,
            disconfirming_evidence=["technical.direction: flat"], main_risk_to_bull_case="셋업 없음",
            what_would_disprove="해당 없음", confidence=0.0,
        )


class DeathPath(_Out):
    family: Literal["liquidation_cascade", "news_shock", "regime_flip", "crowded_unwind", "data_incident", "other"]
    description: str
    observable_trigger: str
    probability_band: Literal["low", "mid", "high", "unknown"]
    time_to_impact_bars: int | None


class BearOutput(_Out):
    symbol: str
    death_paths: list[DeathPath]
    greed_flags: list[str]
    disconfirming_evidence: list[str]
    strongest_point: str
    what_would_disprove_bear: str
    verdict: Literal["survivable", "survivable_if_monitored", "decline"]
    monitoring_conditions: list[str]
    confidence: float

    _v_conf = field_validator("confidence")(_unit("confidence"))

    @field_validator("death_paths")
    @classmethod
    def _v_paths(cls, v: list[DeathPath]) -> list[DeathPath]:
        if len(v) < 1:
            raise ValueError("death_paths 는 최소 1개")
        return v

    @classmethod
    def conservative_example(cls, symbol: str = "BTC/USDT") -> "BearOutput":
        return cls(
            symbol=symbol,
            death_paths=[DeathPath(family="data_incident", description="증거 부족", observable_trigger="해당 없음",
                                   probability_band="unknown", time_to_impact_bars=None)],
            greed_flags=[], disconfirming_evidence=["증거 부족"], strongest_point="증거가 부족하면 진입하지 않는다",
            what_would_disprove_bear="해당 없음", verdict="decline", monitoring_conditions=[], confidence=0.0,
        )


# ---------------------------------------------------------------- risk_manager
class BullPointReliability(_Out):
    point: str
    score_1_5: int
    pushback: str

    @field_validator("score_1_5")
    @classmethod
    def _v(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError(f"score_1_5 는 1..5: {v}")
        return v


class RiskCandidateVerdict(_Out):
    symbol: str
    verdict: Literal["approve", "conditional", "veto"]
    veto_reasons: list[str]
    conditions: list[str]
    max_size_pct: float
    stop_pct: float
    take_profit_pct: float
    rr_net_estimate: float | None
    bull_point_reliability: list[BullPointReliability]
    required_monitoring: list[str]
    correlation_note: str

    _v_size = field_validator("max_size_pct")(_range("max_size_pct", 0.0, 10.0))
    _v_stop = field_validator("stop_pct")(_range("stop_pct", 0.0, 2.0, lo_open=True))
    _v_tp = field_validator("take_profit_pct")(_range("take_profit_pct", 0.0, 4.0, lo_open=True))

    @classmethod
    def conservative_example(cls, symbol: str = "BTC/USDT") -> "RiskCandidateVerdict":
        return cls(
            symbol=symbol, verdict="veto", veto_reasons=["증거 부족"], conditions=[], max_size_pct=0.0,
            stop_pct=1.0, take_profit_pct=2.0, rr_net_estimate=None, bull_point_reliability=[],
            required_monitoring=[], correlation_note="해당 없음",
        )


class RiskPositionView(_Out):
    symbol: str
    assumption_status: list[Literal["holding", "weakening", "impaired", "broken"]]
    red_lines_triggered: list[str]
    health: int
    action: Literal["hold", "trim", "exit"]
    reason: str

    @field_validator("health")
    @classmethod
    def _v_health(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError(f"health 는 1..10: {v}")
        return v


class RiskGlobal(_Out):
    entries_paused: bool
    pause_reason: str | None
    confidence_floor: float
    exposure_cap_pct: float
    portfolio_note: str

    _v_floor = field_validator("confidence_floor")(_range("confidence_floor", 0.65, 1.0))
    _v_cap = field_validator("exposure_cap_pct")(_range("exposure_cap_pct", 0.0, 30.0))


class RiskOutput(_Out):
    candidates: list[RiskCandidateVerdict]
    positions: list[RiskPositionView]
    global_: RiskGlobal = Field(alias="global")
    risk_note: str
    confidence: float

    _v_conf = field_validator("confidence")(_unit("confidence"))

    @classmethod
    def conservative_example(cls, symbols: tuple[str, ...] = ()) -> "RiskOutput":
        return cls(
            candidates=[RiskCandidateVerdict.conservative_example(s) for s in symbols], positions=[],
            **{"global": RiskGlobal(entries_paused=False, pause_reason=None, confidence_floor=0.75,
                                     exposure_cap_pct=30.0, portfolio_note="보수적 기본값")},
            risk_note="리스크 평가 자료 부족", confidence=0.0,
        )


# ---------------------------------------------------------------- head
class RedLine(_Out):
    metric: Literal["price", "unrealized_pct", "funding_rate", "btc_return_1h_pct", "composite_sentiment", "hours_held"]
    op: Literal["<", ">"]
    value: float


class Thesis(_Out):
    reasons: list[str]
    assumptions: list[str]
    red_lines: list[RedLine]
    horizon_bars: int


class HeadDecision(_Out):
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    size_pct: float
    stop_pct: float
    tp_pct: float
    thesis: Thesis
    disagreement_class: Literal["horizon", "caliber", "risk_preference", "information", "none"]
    losing_side_strongest_point: str
    decisive_factor: str
    rationale: str

    _v_conf = field_validator("confidence")(_unit("confidence"))
    _v_size = field_validator("size_pct")(_range("size_pct", 0.0, 10.0))
    _v_stop = field_validator("stop_pct")(_range("stop_pct", 0.0, 2.0))
    _v_tp = field_validator("tp_pct")(_range("tp_pct", 0.0, 4.0))

    @classmethod
    def hold(cls, symbol: str, rationale: str = "결정 근거 부족") -> "HeadDecision":
        return cls(
            symbol=symbol, action="HOLD", confidence=0.0, size_pct=0.0, stop_pct=0.0, tp_pct=0.0,
            thesis=Thesis(reasons=[rationale], assumptions=[], red_lines=[], horizon_bars=0),
            disagreement_class="none", losing_side_strongest_point="", decisive_factor=rationale, rationale=rationale,
        )


class Debias(_Out):
    anchored_to_entry: bool
    ignored_bearish_news: bool
    overtrading: bool
    chasing_spike: bool
    acting_on_sentiment_alone: bool


class HeadOutput(_Out):
    decisions: list[HeadDecision]
    debias: Debias
    reevaluation_triggers: list[str]
    hold_reasons: list[str]
    cycle_note: str

    @classmethod
    def conservative_example(cls, symbols: tuple[str, ...] = ()) -> "HeadOutput":
        return cls(
            decisions=[HeadDecision.hold(s) for s in symbols],
            debias=Debias(anchored_to_entry=False, ignored_bearish_news=False, overtrading=False,
                          chasing_spike=False, acting_on_sentiment_alone=False),
            reevaluation_triggers=[], hold_reasons=["보수적 기본값: 결정 자료 부족"], cycle_note="HOLD",
        )


# ---------------------------------------------------------------- journal
class AssumptionAtExit(_Out):
    assumption: str
    status: Literal["holding", "weakening", "impaired", "broken", "unknown"]


class JournalPostTrade(_Out):
    trade_id: str
    summary_bullets: list[str]
    assumptions_at_exit: list[AssumptionAtExit]
    broken_assumption: str | None
    near_miss_rules: list[str]
    lesson: str
    lesson_tag: Literal["thesis_broken", "stop_too_tight", "chased_spike", "news_ignored", "good_exit", "time_stop", "regime_exit", "risk_exit", "other"]
    telegram_text: str

    @classmethod
    def conservative_example(cls, trade_id: str = "unknown") -> "JournalPostTrade":
        return cls(trade_id=trade_id, summary_bullets=["사후분석 자료 부족"], assumptions_at_exit=[],
                   broken_assumption=None, near_miss_rules=[], lesson="자료 부족으로 교훈 보류", lesson_tag="other",
                   telegram_text="사후분석: 자료 부족")


class ActionItem(_Out):
    param: str
    from_value: str
    to_value: str
    reason: str


class JournalDaily(_Out):
    summary_bullets: list[str]
    what_worked: list[str]
    what_failed: list[str]
    risks: list[str]
    action_items: list[ActionItem]
    abstain_reason: str | None
    do_not_change_yet: bool
    telegram_text: str

    @classmethod
    def conservative_example(cls) -> "JournalDaily":
        return cls(summary_bullets=["일일 리뷰 자료 부족"], what_worked=[], what_failed=[], risks=["표본 부족"],
                   action_items=[], abstain_reason="표본 부족", do_not_change_yet=True, telegram_text="일일 리뷰: 표본 부족")


ROLE_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "technical": TechnicalOutput,
    "derivatives_flow": FlowOutput,
    "news_sentiment": NewsOutput,
    "bull": BullOutput,
    "bear": BearOutput,
    "risk_manager": RiskOutput,
    "head": HeadOutput,
    "journal_post_trade": JournalPostTrade,
    "journal_daily": JournalDaily,
}
