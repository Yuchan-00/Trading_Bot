"""코드 게이트 G0~G4 (SPEC §19 + v2.1 보완) — 순수 함수.

원칙
- 게이트는 LLM 출력을 **조이기만** 한다: 탈락시키거나, 사이즈 배수를 줄이거나, 확신도 하한을 올린다.
  어떤 경로로도 하드 리밋(손절 ≤ 2%, 익절 ≤ 4%, 비중 ≤ 10%, 확신도 ≥ 0.65)을 풀지 않는다.
- 입력이 무효(NaN/inf/None/형식 오류)이면 가장 보수적인 쪽(탈락·HOLD)으로 해석하고 **예외를 내지 않는다**.
  내부 오류도 잡아서 ``passed=False`` 로 돌려준다(fail-closed).
- ``reasons`` 는 탈락/차단 사유, ``notes`` 는 통과는 했지만 남길 참고 사항이다. 각 항목은
  ``"<영문 코드>: <한국어 설명>"`` 형식이라 :func:`reason_codes` 로 코드만 뽑을 수 있다
  (G0 은 ``risk.global_gates`` 의 문장을 그대로 싣는다).
- 단위: ``stop_pct``·``size_pct``·``exposure_pct``·``exec_cost_pct`` 는 % 단위(1.5 = 1.5%).
  ``HardLimits`` 의 비율(0.02 = 2%)과 섞지 않도록 변환은 이 모듈 안에서만 한다.

순서(사이클): G0(전역) → 분석가 → G1(후보 사전게이트) → bull/bear → G2(약세 킬) → risk_manager →
G3(리스크 재강제) → head → G4(최종 주문 계획).
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tradingbot import risk
from tradingbot.agents.schemas import (
    BearOutput,
    FlowSymbolView,
    HeadDecision,
    RiskCandidateVerdict,
    TechnicalOutput,
)
from tradingbot.config import HardLimits
from tradingbot.features import FeaturePack, RegimePack, RiskPack, SentimentComposite
from tradingbot.models import BotState
from tradingbot.scanner import LiquidityGate, Thresholds

log = logging.getLogger("tradingbot.gates")

__all__ = [
    "GateDecision",
    "OrderPlan",
    "g0_preflight",
    "g1_candidate",
    "g2_bear",
    "g3_risk",
    "g4_final",
    "reason_codes",
    "combine_gates",
    "RAISED_CONFIDENCE_FLOOR",
    "G1_MIN_TECH_CONFIDENCE",
    "G1_MAX_SENTIMENT",
    "G1_MAX_CORR_TO_OPEN",
    "G3_MAX_VOLUME_SHARE",
    "G3_SPREAD_MULT",
    "G3_DEPTH_MULT",
    "G3_EXEC_COST_STOP_SHARE",
]

# ---------------------------------------------------------------- 상수(하드 리밋이 아닌 게이트 임계값)
#: 주의 상태(FUSED·쇠퇴·편향 심함·약세 기권)에서 올리는 확신도 하한.
RAISED_CONFIDENCE_FLOOR: float = 0.75
#: G1 기술분석 경로의 최소 확신도(경계 포함).
G1_MIN_TECH_CONFIDENCE: float = 0.55
#: G1 종합심리 상한(초과 시 탈락, 평균회귀 셋업은 예외). 경계 80 은 통과.
G1_MAX_SENTIMENT: float = 80.0
#: G1 보유 종목과의 |상관| 상한(초과 시 탈락). 경계 0.8 은 통과.
G1_MAX_CORR_TO_OPEN: float = 0.8
#: G3 주문액 / (24h 거래대금/96) 상한.
G3_MAX_VOLUME_SHARE: float = 0.05
#: G3 스프레드 차단 배수(× Thresholds.MAX_SPREAD_BPS).
G3_SPREAD_MULT: float = 2.0
#: G3 깊이 요구 배수(ask 1% 깊이 ≥ 3 × 주문액).
G3_DEPTH_MULT: float = 3.0
#: G3 실행비용(%) / 손절거리(%) 상한.
G3_EXEC_COST_STOP_SHARE: float = 0.25

#: 최대 손절/익절/비중을 % 단위로.
_MAX_STOP_PCT: float = HardLimits.MAX_STOP_LOSS_PCT * 100.0      # 2.0
_MAX_TP_PCT: float = HardLimits.MAX_TAKE_PROFIT_PCT * 100.0      # 4.0
_MAX_SIZE_PCT: float = HardLimits.MAX_POSITION_PCT * 100.0       # 10.0
_MAX_EXPOSURE_PCT: float = HardLimits.MAX_POSITIONS * _MAX_SIZE_PCT  # 30.0
#: 왕복 수수료(%) = 2 × FEE_PCT × 100 = 0.2.
_ROUND_TRIP_FEE_PCT: float = 2.0 * HardLimits.FEE_PCT * 100.0
#: 부동소수 잡음 허용 오차(비율 비교).
_TOL: float = 1e-9
_TEXT_MAX: int = 200
_CODE_RE = re.compile(r"[a-z][a-z0-9_]*")
_PAREN_CODE_RE = re.compile(r"\(([a-z][a-z0-9_]*)\)")


# ---------------------------------------------------------------- 모델
class GateDecision(BaseModel):
    """게이트 판정. ``passed`` False 면 해당 후보(또는 사이클의 신규 진입)는 거기서 멈춘다.

    ``size_multiplier`` 는 0~1(무효값은 0), ``confidence_floor`` 는 최소 ``HardLimits.MIN_CONFIDENCE``
    (무효값은 1.0 — 사실상 진입 불가)로 검증 단계에서 조인다.
    """

    model_config = ConfigDict(extra="forbid")

    gate: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    size_multiplier: float = 1.0
    confidence_floor: float = HardLimits.MIN_CONFIDENCE
    notes: list[str] = Field(default_factory=list)

    @field_validator("size_multiplier")
    @classmethod
    def _v_mult(cls, v: float) -> float:
        if not _finite(v):
            return 0.0
        return min(max(float(v), 0.0), 1.0)

    @field_validator("confidence_floor")
    @classmethod
    def _v_floor(cls, v: float) -> float:
        if not _finite(v):
            return 1.0
        return max(float(v), HardLimits.MIN_CONFIDENCE)


class OrderPlan(BaseModel):
    """G4 를 통과한 신규 매수 계획. 가격은 USDT, ``size_pct`` 는 자본 대비 실제 비중(%)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    quote_amount: float
    stop_loss_price: float
    take_profit_price: float
    size_pct: float
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 도우미
def _finite(x: object) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError, OverflowError):
        return False


def _num(x: object) -> float | None:
    """유한한 실수면 float, 아니면 None(bool 은 숫자로 치지 않는다)."""
    return float(x) if _finite(x) else None


def _pos(x: object) -> float | None:
    v = _num(x)
    return v if v is not None and v > 0.0 else None


def _clip(text: object, n: int = _TEXT_MAX) -> str:
    s = "" if text is None else str(text)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _r(code: str, text: str) -> str:
    return f"{code}: {text}"


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """model_construct 로 만든 불완전 객체·dict 에도 안전한 속성 조회."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 - 속성 접근 자체가 깨진 객체
        return default


def _fail(gate: str, exc: BaseException) -> GateDecision:
    """내부 오류 → fail-closed. 예외 메시지는 싣지 않는다(비밀값·원문 유출 방지)."""
    log.warning("%s 내부 오류(%s) → 탈락 처리", gate, type(exc).__name__)
    return GateDecision(gate=gate, passed=False, size_multiplier=0.0,
                        reasons=[_r("internal_error", f"{gate} 계산 중 오류({type(exc).__name__}) → 보수적으로 탈락")])


def reason_codes(decision: GateDecision) -> list[str]:
    """``reasons`` 의 영문 코드 목록(``"code: 설명"`` 의 앞부분). G0 은 괄호 안 토큰을 뽑는다."""
    out: list[str] = []
    for r in decision.reasons:
        prefix = r.split(":", 1)[0].strip()
        if _CODE_RE.fullmatch(prefix):
            out.append(prefix)
            continue
        m = _PAREN_CODE_RE.search(r)
        out.append(m.group(1) if m else prefix)
    return out


def combine_gates(decisions: Iterable[GateDecision], gate: str = "combined") -> GateDecision:
    """여러 게이트를 합친 실효 판정: 전부 통과해야 통과, 하한은 최댓값, 배수는 곱, 사유·노트는 합집합(순서 유지)."""
    passed = True
    floor = HardLimits.MIN_CONFIDENCE
    mult = 1.0
    reasons: list[str] = []
    notes: list[str] = []
    seen_r: set[str] = set()
    seen_n: set[str] = set()
    for d in decisions:
        passed = passed and bool(d.passed)
        floor = max(floor, d.confidence_floor)
        mult *= d.size_multiplier
        for r in d.reasons:
            if r not in seen_r:
                seen_r.add(r)
                reasons.append(r)
        for n in d.notes:
            if n not in seen_n:
                seen_n.add(n)
                notes.append(n)
    if not passed:
        mult = 0.0
    return GateDecision(gate=gate, passed=passed, reasons=reasons, size_multiplier=mult,
                        confidence_floor=floor, notes=notes)


# ====================================================================== G0
def g0_preflight(state: BotState, equity: float, now_ms: int, budget_ok: bool, funding_window: bool) -> GateDecision:
    """사이클 사전점검: ``risk.global_gates``(킬스위치·낙폭 사다리·당일 손실·연속 손실·포지션 수·LLM 예산·
    LLM 일시정지) + 펀딩 정산 창.

    - 전역 게이트가 막으면 ``passed=False``(사이클은 LLM 0회로 끝나고 코드 청산만 한다).
    - 낙폭 주의 단계는 ``size_multiplier=0.5``.
    - 펀딩 정산 창은 **신규 주문 금지 사유만 추가**한다: ``passed`` 는 바꾸지 않고(보유 포지션 검토는 계속)
      ``reasons``·``notes`` 에 ``funding_window`` 를 남기고 ``size_multiplier=0`` 으로 만들어 G4 가 HOLD 하게 한다.
    """
    try:
        budget = budget_ok is True
        verdict = risk.global_gates(state, equity, now_ms, budget)
        reasons = list(verdict.reasons)
        notes: list[str] = []
        mult = verdict.size_multiplier if verdict.allowed else 0.0
        if verdict.allowed and mult < 1.0:
            notes.append(_r("drawdown_caution", f"낙폭 주의 단계 → 사이즈 배수 {mult:.2f}"))
        if budget_ok is not True and budget_ok is not False:
            notes.append(_r("invalid_budget_flag", f"예산 플래그가 bool 이 아님({type(budget_ok).__name__}) → 소진으로 간주"))
        if funding_window is not False:
            # None·비 bool 도 창 안으로 본다(보수적).
            reasons.append("펀딩 정산 창 ±1분 (funding_window): 신규 주문 금지(보유 검토·청산은 계속)")
            notes.append(_r("funding_window", "펀딩 정산 창 — 이번 사이클 신규 주문 없음"))
            mult = 0.0
        passed = bool(verdict.allowed)
        if not passed:
            log.info("G0 차단: %s", "; ".join(reasons))
        return GateDecision(gate="G0", passed=passed, reasons=reasons, size_multiplier=mult,
                            confidence_floor=HardLimits.MIN_CONFIDENCE, notes=notes)
    except Exception as exc:  # noqa: BLE001
        return _fail("G0", exc)


# ====================================================================== G1
def _code_noise_ok(tech: TechnicalOutput, fp: FeaturePack | None) -> tuple[bool, str]:
    """v2.1: ``fp.noise_ok_2pct is True ∧ tech.stop_pct ≥ fp.noise_floor_stop_pct`` (코드 재계산)."""
    if fp is None:
        return False, "피처팩 없음 → 노이즈 조건 검증 불가"
    ok2 = _get(fp, "noise_ok_2pct")
    floor = _num(_get(fp, "noise_floor_stop_pct"))
    stop = _pos(_get(tech, "stop_pct"))
    if ok2 is not True:
        return False, f"feature_pack.noise_ok_2pct={ok2!r} (1.5·ATR% ≥ 2% 또는 미상)"
    if stop is None:
        return False, f"technical.stop_pct={_get(tech, 'stop_pct')!r} 무효"
    if floor is None:
        return False, "feature_pack.noise_floor_stop_pct 미상"
    if stop + _TOL < floor:
        return False, f"technical.stop_pct {stop:.3g}% < 노이즈 하한 {floor:.3g}%"
    return True, f"stop_pct {stop:.3g}% ≥ 노이즈 하한 {floor:.3g}%"


def g1_candidate(
    symbol: str,
    tech: TechnicalOutput | None,
    flow: FlowSymbolView | None,
    news_risk_symbols: set[str],
    sentiment: SentimentComposite,
    position_count: int,
    exposure_pct: float,
    corr_to_open: dict[str, float],
    tech_abstained: bool,
    *,
    fp: FeaturePack | None = None,
) -> GateDecision:
    """후보 사전게이트(bull/bear 호출 전).

    통과 조건(둘 중 하나):
      A) technical.direction == long ∧ mtf_agreement ∧ confidence ≥ 0.55 ∧ 노이즈 조건
         (노이즈 = tech.noise_check ∧ fp.noise_ok_2pct is True ∧ tech.stop_pct ≥ fp.noise_floor_stop_pct)
      B) flow.washout_setup ∧ technical.direction ≠ avoid
    탈락 조건(하나라도): 기술분석 기권, flow.block_new_longs, flow.stop_inside_cascade, flow.cascade_risk == high,
      뉴스 risk_event 종목, 종합심리 > 80(평균회귀 셋업 제외), 포지션 수 ≥ 3, 노출 ≥ 30%,
      |상관| > 0.8 인 보유 종목. flow 기권(None)은 허용하되 notes 에 남긴다.
    """
    try:
        reasons: list[str] = []
        notes: list[str] = []

        # --- 기술분석 기권
        abstained = tech is None or tech_abstained is not False
        if abstained:
            reasons.append(_r("technical_abstained", "기술분석 기권 → 진입 근거 없음"))
        elif _get(tech, "symbol") != symbol:
            reasons.append(_r("symbol_mismatch", f"technical.symbol={_get(tech, 'symbol')!r} ≠ {symbol!r}"))

        # --- 파생/자금흐름
        if flow is None:
            notes.append(_r("flow_abstained", "파생/자금흐름 기권 → 플로우 차단 조건 확인 불가(허용)"))
        else:
            if _get(flow, "symbol") not in (None, symbol):
                notes.append(_r("flow_symbol_mismatch", f"flow.symbol={_get(flow, 'symbol')!r} ≠ {symbol!r} → 플로우 무시"))
                flow = None
        if flow is not None:
            if _get(flow, "block_new_longs") is not False:
                reasons.append(_r("flow_block_new_longs",
                                  f"플로우 신규 롱 차단: {_clip(_get(flow, 'block_reason') or '사유 없음')}"))
            if _get(flow, "stop_inside_cascade") is not False:
                reasons.append(_r("stop_inside_cascade", "손절이 연쇄청산 군집 안에 있음"))
            if _get(flow, "cascade_risk") == "high":
                reasons.append(_r("cascade_risk_high", "연쇄청산 위험 high(2% 이내 롱 청산 군집)"))

        # --- 뉴스 위험 이벤트
        try:
            news_set = set(news_risk_symbols or ())
        except TypeError:
            news_set = set()
            notes.append(_r("news_risk_symbols_invalid", "news_risk_symbols 형식 오류 → 비어 있다고 간주"))
        if symbol in news_set:
            reasons.append(_r("news_risk_event", "뉴스 위험 이벤트 대상 종목"))

        # --- 종합심리
        setup = _get(tech, "setup_type") if not abstained else None
        score = _num(_get(sentiment, "score_0_100"))
        if sentiment is None or score is None:
            notes.append(_r("sentiment_unknown", "종합심리 미상 → 과열 조건 판단 생략"))
        elif score > G1_MAX_SENTIMENT:
            if setup == "mean_reversion":
                notes.append(_r("sentiment_hot_mr_exempt", f"종합심리 {score:.1f} > {G1_MAX_SENTIMENT:g} 이지만 평균회귀 셋업 예외"))
            else:
                reasons.append(_r("sentiment_overheated", f"종합심리 {score:.1f} > {G1_MAX_SENTIMENT:g} (과열)"))

        # --- 포지션 수·노출
        if isinstance(position_count, bool) or not _finite(position_count):
            reasons.append(_r("invalid_position_count", f"보유 포지션 수 무효({position_count!r}) → 보수적으로 탈락"))
        elif int(position_count) >= HardLimits.MAX_POSITIONS:
            reasons.append(_r("max_positions", f"보유 포지션 {int(position_count)}개 ≥ {HardLimits.MAX_POSITIONS}"))
        exp = _num(exposure_pct)
        if exp is None:
            reasons.append(_r("invalid_exposure", f"노출 비중 무효({exposure_pct!r}) → 보수적으로 탈락"))
        elif exp >= _MAX_EXPOSURE_PCT - _TOL:
            reasons.append(_r("max_exposure", f"총 노출 {exp:.2f}% ≥ {_MAX_EXPOSURE_PCT:g}%"))

        # --- 보유 종목 상관
        if isinstance(corr_to_open, Mapping):
            for other in sorted(corr_to_open, key=str):
                c = _num(corr_to_open[other])
                if c is None:
                    # 무효 상관(NaN/inf/None) → '같은 베팅' 여부 판단 불가 → 보수적으로 탈락(fail-closed)
                    reasons.append(_r("corr_unknown", f"보유 {other} 와의 상관 미상 → 같은 베팅 여부 판단 불가로 탈락"))
                elif abs(c) > G1_MAX_CORR_TO_OPEN:
                    reasons.append(_r("corr_to_open", f"보유 {other} 와 |상관| {abs(c):.2f} > {G1_MAX_CORR_TO_OPEN:g} (같은 베팅)"))
        elif corr_to_open is not None:
            notes.append(_r("corr_to_open_invalid", "corr_to_open 형식 오류 → 판단 생략"))

        # --- 통과 조건
        if not abstained and _get(tech, "symbol") == symbol:
            direction = _get(tech, "direction")
            conf = _num(_get(tech, "confidence"))
            mtf = _get(tech, "mtf_agreement") is True
            llm_noise = _get(tech, "noise_check") is True
            code_noise, noise_text = _code_noise_ok(tech, fp)
            fp_mtf = _get(fp, "mtf_agreement") if fp is not None else None
            if fp is not None and isinstance(fp_mtf, bool) and fp_mtf != mtf:
                notes.append(_r("mtf_mismatch", f"technical.mtf_agreement={mtf} ≠ feature_pack.mtf_agreement={fp_mtf}"))

            path_a_fail: list[str] = []
            if direction != "long":
                path_a_fail.append(f"direction={direction}")
            if not mtf:
                path_a_fail.append("mtf_agreement=false")
            if conf is None or conf + _TOL < G1_MIN_TECH_CONFIDENCE:
                path_a_fail.append(f"confidence={conf if conf is not None else 'NaN'} < {G1_MIN_TECH_CONFIDENCE}")
            if not llm_noise:
                path_a_fail.append("noise_check=false")
            if not code_noise:
                path_a_fail.append(f"코드 노이즈 조건 실패({noise_text})")
            path_a = not path_a_fail

            washout = flow is not None and _get(flow, "washout_setup") is True
            path_b = washout and direction != "avoid"

            if path_a:
                notes.append(_r("setup_technical", f"기술분석 경로 통과({noise_text})"))
            elif path_b:
                notes.append(_r("setup_washout", "플로우 washout 경로 통과(기술분석 direction≠avoid)"))
                if not code_noise:
                    notes.append(_r("washout_noise_warning", f"노이즈 조건 미충족 — {noise_text}"))
            else:
                detail = ", ".join(path_a_fail)
                if washout and direction == "avoid":
                    detail += "; washout 이지만 direction=avoid"
                reasons.append(_r("no_entry_setup", f"진입 셋업 없음({detail})"))

        passed = not reasons
        return GateDecision(gate="G1", passed=passed, reasons=reasons, size_multiplier=1.0 if passed else 0.0,
                            confidence_floor=HardLimits.MIN_CONFIDENCE, notes=notes)
    except Exception as exc:  # noqa: BLE001
        return _fail("G1", exc)


# ====================================================================== G2
def g2_bear(bear: BearOutput | None) -> GateDecision:
    """약세 킬: ``verdict == decline`` → 탈락. 기권(None)은 통과시키되 notes 에 ``bear_abstained``·하한 0.75."""
    try:
        if bear is None:
            return GateDecision(gate="G2", passed=True, reasons=[], size_multiplier=1.0,
                                confidence_floor=RAISED_CONFIDENCE_FLOOR,
                                notes=["bear_abstained", _r("bear_abstained_floor",
                                                            f"약세 변론 기권 → 확신도 하한 {RAISED_CONFIDENCE_FLOOR}")])
        verdict = _get(bear, "verdict")
        strongest = _clip(_get(bear, "strongest_point") or "")
        if verdict == "decline":
            return GateDecision(gate="G2", passed=False, size_multiplier=0.0,
                                reasons=[_r("bear_decline", f"약세 변론 decline — {strongest or '사유 없음'}")])
        if verdict == "survivable_if_monitored":
            conds = [_clip(c, 120) for c in (_get(bear, "monitoring_conditions") or [])][:5]
            notes = [_r("bear_survivable_if_monitored", "감시 조건부 생존: " + ("; ".join(conds) if conds else "조건 미기재"))]
            return GateDecision(gate="G2", passed=True, notes=notes)
        if verdict == "survivable":
            return GateDecision(gate="G2", passed=True, notes=[_r("bear_survivable", "약세 변론: 생존 가능")])
        return GateDecision(gate="G2", passed=False, size_multiplier=0.0,
                            reasons=[_r("bear_invalid_verdict", f"약세 판정 형식 오류({verdict!r}) → 탈락")])
    except Exception as exc:  # noqa: BLE001
        return _fail("G2", exc)


# ====================================================================== G3
def g3_risk(
    symbol: str,
    verdict: RiskCandidateVerdict | None,
    regime: RegimePack,
    decay_state: str,
    bias: dict[str, str],
    risk_pack: RiskPack,
    liquidity: LiquidityGate,
    planned_notional: float,
    stop_pct: float,
    *,
    thresholds: Thresholds | None = None,
) -> GateDecision:
    """리스크 거부권을 코드가 다시 강제한다.

    - verdict None(기권) → 탈락(``risk_abstained``). veto → 탈락. conditional → 통과(조건은 notes).
    - regime FUSED → 사이즈 ×0.5·하한 0.75 ; macro_alarm → 탈락.
    - decay_state == decayed 또는 bias 중 severe 하나라도 → 하한 0.75.
    - 주문액 > 5% × (24h 거래대금/96) → 배수를 줄여 맞춘다(맞춘 금액 < MIN_ORDER_USDT 면 탈락).
    - 스프레드 > 2 × MAX_SPREAD_BPS 또는 ask 1% 깊이 < 3 × 주문액 → 탈락(값 미상도 탈락).
    - risk_pack/liquidity 의 symbol 이 후보와 다르면 탈락. 리스크 팩이 없거나 값이 전부 미상이면 탈락.
    - 24h 거래대금이 없으면 risk_pack 의 비율과 팩 주문액(impact·depth/100)으로 15분 거래대금을 복원(불가 시 탈락).
    - 실행비용: ``risk_pack.exec_cost_pct`` 와 ``impact_bps_est/100 + spread_bps/200 + 2·FEE_PCT·100`` 중 큰 값 > 0.25 × 손절거리 → 탈락.
      손절거리 = min(stop_pct, verdict.stop_pct, 2.0) (% 단위; 무효값은 제외). 잰 비용이 팩 값보다 크면(또는 팩 값이
      None 이면) ``risk_pack.exec_cost_pct`` 를 그 값으로 올려 둔다 — G4 최종 손절 재검사가 같은 비용을 보게(MONEY-7).
    - CVaR 사이즈 상한: 배수 ×= min(1, cvar_size_pct / 10).

    ``planned_notional`` 은 이 후보에 낼 수 있는 **최대** 주문액(예: equity × 10%)으로 넘긴다 —
    배수 조정은 그 금액을 기준으로 하므로 실제 주문액이 더 작으면 더 보수적이 된다.
    """
    try:
        th = thresholds or Thresholds()
        reasons: list[str] = []
        notes: list[str] = []
        mult = 1.0
        floor = HardLimits.MIN_CONFIDENCE

        # --- 리스크 관리자 판정
        if verdict is None:
            reasons.append(_r("risk_abstained", "리스크 관리자 기권 → 신규 진입 없음"))
        else:
            v_sym = _get(verdict, "symbol")
            v = _get(verdict, "verdict")
            if v_sym != symbol:
                reasons.append(_r("symbol_mismatch", f"risk.symbol={v_sym!r} ≠ {symbol!r}"))
            if v == "veto":
                vr = [_clip(x, 120) for x in (_get(verdict, "veto_reasons") or [])][:5]
                reasons.append(_r("risk_veto", "리스크 거부: " + ("; ".join(vr) if vr else "사유 미기재")))
            elif v == "conditional":
                conds = [_clip(x, 120) for x in (_get(verdict, "conditions") or [])][:5]
                notes.append(_r("risk_conditional", "조건부 승인: " + ("; ".join(conds) if conds else "조건 미기재")))
            elif v == "approve":
                notes.append(_r("risk_approve", "리스크 승인"))
            else:
                reasons.append(_r("risk_invalid_verdict", f"리스크 판정 형식 오류({v!r}) → 탈락"))
            if _pos(_get(verdict, "max_size_pct")) is None:
                reasons.append(_r("risk_zero_size", f"risk.max_size_pct={_get(verdict, 'max_size_pct')!r} → 진입 불가"))

        # --- 레짐
        r_verdict = _get(regime, "verdict")
        fused = _get(regime, "fused") is True or r_verdict == "fused"
        if regime is None:
            notes.append(_r("regime_unknown", "레짐 정보 없음"))
        if r_verdict == "macro_alarm":
            reasons.append(_r("macro_alarm", "BTC 충격 ∧ 상관 융합(macro_alarm) → 신규 진입 금지"))
        if fused:
            mult *= 0.5
            floor = max(floor, RAISED_CONFIDENCE_FLOOR)
            notes.append(_r("regime_fused", f"상관 융합(FUSED) → 사이즈 ×0.5, 하한 {RAISED_CONFIDENCE_FLOOR}"))
        if symbol in (_get(regime, "name_alarm_symbols") or []):
            reasons.append(_r("name_alarm", "종목 단독 급변(name_alarm) → 신규 진입 금지"))

        # --- 쇠퇴·편향
        if decay_state == "decayed":
            floor = max(floor, RAISED_CONFIDENCE_FLOOR)
            notes.append(_r("decay_decayed", f"전략 쇠퇴(decayed) → 하한 {RAISED_CONFIDENCE_FLOOR}"))
        elif decay_state == "warning":
            notes.append(_r("decay_warning", "전략 쇠퇴 경고(warning)"))
        severe = []
        if isinstance(bias, Mapping):
            severe = sorted(str(k) for k, val in bias.items() if val == "severe")
        if severe:
            floor = max(floor, RAISED_CONFIDENCE_FLOOR)
            notes.append(_r("bias_severe", f"편향 심함({', '.join(severe)}) → 하한 {RAISED_CONFIDENCE_FLOOR}"))

        # --- 리스크 팩·유동성 게이트의 종목 일치(다른 종목 팩이 섞이면 모든 검사가 엉뚱한 시장으로 돈다)
        if risk_pack is not None and _get(risk_pack, "symbol") != symbol:
            reasons.append(_r("risk_pack_symbol_mismatch",
                              f"risk_pack.symbol={_get(risk_pack, 'symbol')!r} ≠ {symbol!r}"))
        if liquidity is not None and _get(liquidity, "symbol") != symbol:
            reasons.append(_r("liquidity_symbol_mismatch",
                              f"liquidity.symbol={_get(liquidity, 'symbol')!r} ≠ {symbol!r}"))
        # 리스크 팩이 없거나 핵심 위험 값이 전부 미상이면(파이프라인의 빈 RiskPack(symbol=s)) fail-closed.
        _rp_fields = ("spread_bps", "depth_taker_usdt", "impact_bps_est", "exec_cost_pct", "cvar_size_pct",
                      "order_notional_vs_15m_volume_share")
        if risk_pack is None or all(_num(_get(risk_pack, k)) is None for k in _rp_fields):
            reasons.append(_r("risk_pack_missing", "리스크 팩 없음/전부 미상 → CVaR·실행비용 판단 불가로 차단"))

        # --- 주문액
        notional = _pos(planned_notional)
        if notional is None:
            reasons.append(_r("invalid_notional", f"계획 주문액 무효({planned_notional!r})"))

        # --- 유동성 게이트(스캐너) 재확인
        if liquidity is None:
            reasons.append(_r("liquidity_unknown", "유동성 게이트 정보 없음"))
        elif _get(liquidity, "ok") is not True:
            lr = [_clip(x, 80) for x in (_get(liquidity, "reasons") or [])][:5]
            reasons.append(_r("liquidity_gate_failed", "유동성 게이트 실패: " + (", ".join(lr) if lr else "사유 미기재")))

        # --- 스프레드
        spreads = [s for s in (_num(_get(risk_pack, "spread_bps")), _num(_get(liquidity, "spread_bps"))) if s is not None]
        max_spread = G3_SPREAD_MULT * float(th.MAX_SPREAD_BPS)
        if not spreads:
            reasons.append(_r("spread_unknown", "스프레드 미상 → 차단"))
        elif max(spreads) > max_spread:
            reasons.append(_r("wide_spread", f"스프레드 {max(spreads):.2f}bps > {max_spread:g}bps"))

        # --- 깊이
        depth = _num(_get(risk_pack, "depth_taker_usdt"))
        if depth is None:
            depth = _num(_get(liquidity, "depth_1pct_usdt"))
            if depth is not None:
                notes.append(_r("depth_fallback", "ask 깊이 미상 → 유동성 게이트의 1% 깊이 사용"))
        if depth is None:
            reasons.append(_r("depth_unknown", "호가 깊이 미상 → 차단"))
        elif notional is not None and depth < G3_DEPTH_MULT * notional:
            reasons.append(_r("thin_depth", f"ask 1% 깊이 {depth:.0f} < {G3_DEPTH_MULT:g} × 주문액 {notional:.2f}"))

        # --- 24h 거래대금 대비 주문액
        if notional is not None:
            qv = _pos(_get(liquidity, "quote_volume_24h"))
            share = _num(_get(risk_pack, "order_notional_vs_15m_volume_share"))
            allowed: float | None = None
            if qv is not None:
                allowed = G3_MAX_VOLUME_SHARE * qv / 96.0
                cur_share = notional / (qv / 96.0)
            elif share is not None and share > 0.0:
                # share 는 리스크 팩을 만들 때의 주문액(pack_notional = impact_bps·depth/100) 기준이다.
                # 15분 거래대금 = pack_notional / share 로 복원해 G3 의 주문액에 다시 적용한다.
                p_impact = _num(_get(risk_pack, "impact_bps_est"))
                p_depth = _pos(_get(risk_pack, "depth_taker_usdt"))
                pack_notional = p_impact * p_depth / 100.0 if p_impact is not None and p_depth is not None else None
                if pack_notional is not None and pack_notional > 0.0:
                    vol15 = pack_notional / share
                    allowed = G3_MAX_VOLUME_SHARE * vol15
                    cur_share = notional / vol15
                    notes.append(_r("volume_share_fallback",
                                    "24h 거래대금 미상 → risk_pack 의 주문액/15분 거래대금 비율로 15분 거래대금 복원"))
            if allowed is None:
                reasons.append(_r("volume_unknown", "24h 거래대금 미상 → 주문 규모 판단 불가"))
            elif notional > allowed * (1.0 + _TOL):
                scale = allowed / notional
                if allowed < HardLimits.MIN_ORDER_USDT:
                    reasons.append(_r("volume_too_small",
                                      f"허용 주문액 {allowed:.2f} USDT < 최소 주문 {HardLimits.MIN_ORDER_USDT:g} (주문액/15분 거래대금 {cur_share:.3f})"))
                else:
                    mult *= scale
                    notes.append(_r("volume_scaled",
                                    f"주문액/15분 거래대금 {cur_share:.3f} > {G3_MAX_VOLUME_SHARE:g} → 사이즈 ×{scale:.3f}"))

        # --- 실행비용 vs 손절거리
        stops = [_pos(stop_pct), _pos(_get(verdict, "stop_pct")) if verdict is not None else None, _MAX_STOP_PCT]
        if _pos(stop_pct) is None:
            notes.append(_r("stop_pct_invalid", f"stop_pct={stop_pct!r} 무효 → risk.stop_pct·2% 로 판단"))
        stop_eff = min(s for s in stops if s is not None)
        costs: list[float] = []
        ec = _num(_get(risk_pack, "exec_cost_pct"))
        if ec is not None:
            costs.append(ec)
        impacts = [i for i in (_num(_get(risk_pack, "impact_bps_est")),) if i is not None]
        if notional is not None and depth is not None and depth > 0:
            impacts.append(notional / depth * 100.0)
        if impacts:
            # v2.1 실행비용 = impact/100 + spread_bps/200 + 왕복 수수료 (스프레드는 둘 중 나쁜 값)
            spread_term = max(spreads) / 200.0 if spreads else 0.0
            costs.append(max(impacts) / 100.0 + spread_term + _ROUND_TRIP_FEE_PCT)
        if not costs:
            reasons.append(_r("exec_cost_unknown", "실행비용 계산 불가 → 차단"))
        else:
            cost = max(costs)
            limit = G3_EXEC_COST_STOP_SHARE * stop_eff
            if cost > limit + _TOL:
                reasons.append(_r("exec_cost_too_high",
                                  f"실행비용 {cost:.3f}% > {G3_EXEC_COST_STOP_SHARE:g} × 손절거리 {stop_eff:.3f}% (= {limit:.3f}%)"))
            else:
                notes.append(_r("exec_cost_ok", f"실행비용 {cost:.3f}% ≤ {limit:.3f}%"))
            # G4 재검사(MONEY-7)가 G3 와 같은(더 나쁜) 비용을 보도록 측정값을 리스크 팩에 남긴다(올리기만).
            _record_exec_cost(risk_pack, cost)

        # --- CVaR 사이즈 상한
        cvar_size = _num(_get(risk_pack, "cvar_size_pct"))
        if cvar_size is None:
            notes.append(_r("cvar_unknown", "CVaR 사이즈 상한 미상"))
        elif cvar_size <= 0.0:
            reasons.append(_r("cvar_zero", f"CVaR 사이즈 상한 {cvar_size:g}% → 진입 불가"))
        elif cvar_size < _MAX_SIZE_PCT:
            scale = cvar_size / _MAX_SIZE_PCT
            mult *= scale
            notes.append(_r("cvar_cap", f"CVaR 사이즈 상한 {cvar_size:.2f}% → 사이즈 ×{scale:.3f}"))

        # --- 최종 사이즈가 최소 주문 미만이면 탈락
        if not reasons and notional is not None and notional * mult < HardLimits.MIN_ORDER_USDT:
            reasons.append(_r("size_below_min_order",
                              f"조정 후 주문액 {notional * mult:.2f} USDT < 최소 주문 {HardLimits.MIN_ORDER_USDT:g}"))

        passed = not reasons
        if not passed:
            mult = 0.0
        return GateDecision(gate="G3", passed=passed, reasons=reasons, size_multiplier=mult,
                            confidence_floor=floor, notes=notes)
    except Exception as exc:  # noqa: BLE001
        return _fail("G3", exc)


def _record_exec_cost(risk_pack: Any, cost: float) -> None:
    """G3 가 잰 실효 실행비용(%)을 ``risk_pack.exec_cost_pct`` 에 기록한다 — 기존 값보다 클 때(또는 없을 때)만.

    G3 비용은 ``max(risk_pack.exec_cost_pct, impact/100 + max(팩·스캐너 스프레드)/200 + 왕복 수수료)`` 이고
    주문액/깊이 충격도 포함하므로 팩 값보다 클 수 있다. cycle 은 G4 에 ``risk_pack.exec_cost_pct`` 를 넘기므로,
    여기 남기지 않으면 G4 가 G3 보다 약한(또는 None 이라 생략되는) 비용으로 최종 손절을 재검사한다.
    기록할 수 없는 객체(불변·None)는 조용히 넘어간다 — 판정에는 영향이 없다.
    """
    c = _num(cost)
    if risk_pack is None or c is None:
        return
    try:
        prev = _num(_get(risk_pack, "exec_cost_pct"))
        if prev is not None and prev >= c:
            return
        if isinstance(risk_pack, MutableMapping):
            risk_pack["exec_cost_pct"] = c
        else:
            setattr(risk_pack, "exec_cost_pct", c)
    except Exception as exc:  # noqa: BLE001
        log.debug("G3 실행비용 기록 실패: %s", type(exc).__name__)


# ====================================================================== G4
def _hold(reasons: list[str], notes: list[str], floor: float, mult: float) -> tuple[GateDecision, None]:
    log.info("G4 HOLD: %s", "; ".join(reasons))
    return (GateDecision(gate="G4", passed=False, reasons=reasons, size_multiplier=0.0,
                         confidence_floor=floor, notes=notes), None)


def g4_final(
    decision: HeadDecision,
    verdict: RiskCandidateVerdict,
    floor: float,
    size_multiplier: float,
    equity: float,
    entry_price: float,
    atr_1h: float | None,
    free_quote: float,
    *,
    tech_stop_pct: float | None = None,
    exec_cost_pct: float | None = None,
) -> tuple[GateDecision, OrderPlan | None]:
    """총괄 BUY 결정 → 최종 주문 계획. 위반은 HOLD(계획 None)로 강등한다.

    순서(SPEC §19): 확신도 ≥ 하한 → stop_pct = min(head.stop_pct, verdict.stop_pct, 2.0[, 기술분석 손절]) →
    ``risk.clamp_stops`` → ``risk.enforce_net_rr``(손절만 조임; rr_impossible → HOLD) →
    size_pct = min(head.size_pct, verdict.max_size_pct, 10) → ``risk.size_position``(size_multiplier) 후
    size_pct 상한 적용 → MIN_ORDER_USDT 미만이면 HOLD.
    ``tech_stop_pct``(% 단위)를 주면 최종 손절은 기술분석 손절 후보보다 넓어지지 않는다(v2.1).
    ``atr_1h`` 는 손절이 1h ATR 노이즈 안으로 조여졌는지 notes 에 남기는 데만 쓴다.
    ``exec_cost_pct``(% 단위, 보통 ``RiskPack.exec_cost_pct`` — G3 가 잰 실효 비용으로 올려 둔 값)를 주면 clamp_stops·enforce_net_rr 로 **최종** 손절거리가
    정해진 뒤 실행비용 > ``G3_EXEC_COST_STOP_SHARE``(0.25) × 최종 손절거리(%) 이면 HOLD 한다(MONEY-7: G3 는 조여지기
    전 손절로 판단하므로 head·tech·net-RR 이 손절을 조이면 재검사가 필요). 유한한 수가 아니면 무시하고 notes 에 남긴다.
    """
    floor_eff = HardLimits.MIN_CONFIDENCE
    try:
        notes: list[str] = []
        f = _num(floor)
        if f is None:
            return _hold([_r("invalid_floor", f"확신도 하한 무효({floor!r}) → HOLD")], notes, 1.0, 0.0)
        floor_eff = max(HardLimits.MIN_CONFIDENCE, f)

        if decision is None:
            return _hold([_r("head_missing", "총괄 결정 없음 → HOLD")], notes, floor_eff, 0.0)
        symbol = _get(decision, "symbol")
        action = _get(decision, "action")
        if action != "BUY":
            return _hold([_r("not_buy", f"총괄 action={action!r} → 신규 진입 없음")], notes, floor_eff, 0.0)
        if not isinstance(symbol, str) or not symbol:
            return _hold([_r("invalid_symbol", f"종목 기호 무효({symbol!r})")], notes, floor_eff, 0.0)
        if verdict is None:
            return _hold([_r("risk_abstained", "리스크 판정 없음 → HOLD")], notes, floor_eff, 0.0)
        if _get(verdict, "symbol") != symbol:
            return _hold([_r("symbol_mismatch", f"risk.symbol={_get(verdict, 'symbol')!r} ≠ {symbol!r}")],
                         notes, floor_eff, 0.0)
        if _get(verdict, "verdict") not in ("approve", "conditional"):
            return _hold([_r("risk_veto", f"리스크 판정 {_get(verdict, 'verdict')!r} → HOLD")], notes, floor_eff, 0.0)

        # 1) 확신도
        conf = _num(_get(decision, "confidence"))
        if conf is None or conf + 1e-12 < floor_eff:
            return _hold([_r("below_confidence_floor", f"총괄 확신도 {conf if conf is not None else 'NaN'} < 하한 {floor_eff:.2f}")],
                         notes, floor_eff, 0.0)

        entry = _pos(entry_price)
        eq = _pos(equity)
        fq = _num(free_quote)
        if entry is None:
            return _hold([_r("invalid_entry", f"진입가 무효({entry_price!r})")], notes, floor_eff, 0.0)
        if eq is None:
            return _hold([_r("invalid_equity", f"자본 무효({equity!r})")], notes, floor_eff, 0.0)
        if fq is None or fq <= 0.0:
            return _hold([_r("no_free_quote", f"가용 잔고 없음({free_quote!r})")], notes, floor_eff, 0.0)
        mult = _num(size_multiplier)
        if mult is None or mult <= 0.0:
            return _hold([_r("zero_size_multiplier", f"사이즈 배수 {size_multiplier!r} → 신규 주문 없음")],
                         notes, floor_eff, 0.0)
        mult = min(mult, 1.0)

        # 2) 손절거리(%)
        head_stop = _pos(_get(decision, "stop_pct"))
        risk_stop = _pos(_get(verdict, "stop_pct"))
        if head_stop is None:
            return _hold([_r("invalid_stop", f"head.stop_pct={_get(decision, 'stop_pct')!r} 무효")], notes, floor_eff, 0.0)
        if risk_stop is None:
            return _hold([_r("invalid_stop", f"risk.stop_pct={_get(verdict, 'stop_pct')!r} 무효")], notes, floor_eff, 0.0)
        stops = [head_stop, risk_stop, _MAX_STOP_PCT]
        ts = _pos(tech_stop_pct)
        if ts is not None:
            stops.append(ts)
        elif tech_stop_pct is not None:
            notes.append(_r("tech_stop_invalid", f"기술분석 손절 {tech_stop_pct!r} 무효 → 무시"))
        stop_pct = min(stops)

        # 익절거리(%): head 값, 무효면 risk 값. 상한 4%.
        tp_pct = _pos(_get(decision, "tp_pct"))
        if tp_pct is None:
            tp_pct = _pos(_get(verdict, "take_profit_pct"))
            if tp_pct is None:
                return _hold([_r("invalid_tp", "익절거리 무효(head·risk 모두)")], notes, floor_eff, 0.0)
            notes.append(_r("tp_from_risk", f"head.tp_pct 무효 → risk.take_profit_pct {tp_pct:g}% 사용"))
        tp_pct = min(tp_pct, _MAX_TP_PCT)
        notes.append(_r("stop_tp", f"손절 {stop_pct:.3f}% (head {head_stop:g}, risk {risk_stop:g}"
                                   f"{f', tech {ts:g}' if ts is not None else ''}), 익절 {tp_pct:.3f}%"))

        sl_raw = entry * (1.0 - stop_pct / 100.0)
        tp_raw = entry * (1.0 + tp_pct / 100.0)
        sl, tp = risk.clamp_stops(entry, sl_raw, tp_raw)
        # clamp_stops 는 무효 입력을 기본값으로 바꿀 수 있다 → 선택한 손절보다 넓어지면 선택값으로 되돌린다.
        if (entry - sl) / entry > stop_pct / 100.0 + _TOL:
            sl = max(sl, sl_raw)

        # 3) 순손익비
        sl, tp, rr_note = risk.enforce_net_rr(entry, sl, tp)
        notes.append(_r("net_rr", rr_note))
        if "rr_impossible" in rr_note:
            return _hold([_r("rr_impossible", f"비용 반영 순손익비 {HardLimits.MIN_NET_RR} 불가 — {rr_note}")],
                         notes, floor_eff, 0.0)
        sl_dist = (entry - sl) / entry
        tp_dist = (tp - entry) / entry

        # 3b) 실행비용 vs 최종 손절거리(MONEY-7) — G3 이후 조여진 손절로 다시 본다
        if exec_cost_pct is not None:
            ec = _num(exec_cost_pct)
            final_stop_pct = sl_dist * 100.0
            if ec is None:
                notes.append(_r("exec_cost_invalid", f"실행비용 {exec_cost_pct!r} 무효 → 최종 손절 재검사 생략"))
            else:
                limit = G3_EXEC_COST_STOP_SHARE * final_stop_pct
                if ec > limit + _TOL:
                    return _hold([_r("exec_cost_too_high",
                                     f"실행비용 {ec:.3f}% > {G3_EXEC_COST_STOP_SHARE:g} × 최종 손절거리 "
                                     f"{final_stop_pct:.3f}% (= {limit:.3f}%) → HOLD")], notes, floor_eff, 0.0)
                notes.append(_r("exec_cost_final_ok", f"실행비용 {ec:.3f}% ≤ 최종 손절 기준 {limit:.3f}%"))
        atr = _pos(atr_1h)
        if atr is not None and sl_dist < atr / entry:
            notes.append(_r("stop_inside_atr", f"손절거리 {sl_dist:.3%} < 1h ATR {atr / entry:.3%} (노이즈 손절 위험)"))

        # 4) 사이즈 상한(%)
        head_size = _num(_get(decision, "size_pct"))
        risk_size = _num(_get(verdict, "max_size_pct"))
        if head_size is None or risk_size is None:
            return _hold([_r("invalid_size", f"size_pct 무효(head {head_size!r}, risk {risk_size!r})")], notes, floor_eff, 0.0)
        size_pct = min(head_size, risk_size, _MAX_SIZE_PCT)
        if size_pct <= 0.0:
            return _hold([_r("zero_size", f"사이즈 상한 {size_pct:g}% → 진입 없음")], notes, floor_eff, 0.0)

        # 5) 금액
        quote, size_notes = risk.size_position(eq, entry, sl, conf, mult, fq)
        notes.extend(size_notes)
        cap_quote = eq * size_pct / 100.0
        if quote > cap_quote:
            quote = cap_quote
            notes.append(_r("size_pct_cap", f"사이즈 상한 {size_pct:g}% → {cap_quote:.2f} USDT"))
        quote = min(quote, fq, eq * HardLimits.MAX_POSITION_PCT)
        quote = math.floor(quote * 100.0) / 100.0   # 센트 단위 내림(늘어나지 않는다)
        if not _finite(quote) or quote < HardLimits.MIN_ORDER_USDT:
            return _hold([_r("below_min_order", f"주문액 {quote:.2f} USDT < 최소 주문 {HardLimits.MIN_ORDER_USDT:g}")],
                         notes, floor_eff, 0.0)

        # 6) 최종 불변식(방어): 어긋나면 HOLD
        broken: list[str] = []
        if not (0.0 < sl_dist <= HardLimits.MAX_STOP_LOSS_PCT + _TOL) or sl_dist > stop_pct / 100.0 + _TOL:
            broken.append(f"손절거리 {sl_dist:.4%}")
        if not (0.0 < tp_dist <= HardLimits.MAX_TAKE_PROFIT_PCT + _TOL):
            broken.append(f"익절거리 {tp_dist:.4%}")
        if quote > eq * HardLimits.MAX_POSITION_PCT + 1e-6 or quote > fq + 1e-6:
            broken.append(f"주문액 {quote:.2f}")
        if broken:
            return _hold([_r("invariant_violation", "하드 리밋 불변식 위반: " + ", ".join(broken))], notes, floor_eff, 0.0)

        actual_pct = quote / eq * 100.0
        plan = OrderPlan(symbol=symbol, quote_amount=quote, stop_loss_price=sl, take_profit_price=tp,
                         size_pct=actual_pct, notes=list(notes))
        log.info("G4 통과 %s: %.2f USDT (%.2f%%) sl=%.6g tp=%.6g", symbol, quote, actual_pct, sl, tp)
        return (GateDecision(gate="G4", passed=True, reasons=[], size_multiplier=mult,
                             confidence_floor=floor_eff, notes=notes), plan)
    except Exception as exc:  # noqa: BLE001
        return _fail("G4", exc), None
