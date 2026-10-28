"""역할별 user payload 빌더 (SPEC §20.3, v2.1 §20.3a 필드 계약).

원칙
- 모든 빌더는 **순수 함수**다: 입력을 바꾸지 않고, 같은 입력이면 ``json.dumps(sort_keys=True)`` 결과가
  바이트 단위로 같다. 반환값은 JSON 으로 직렬화 가능한 평범한 dict/list/str/int/float/bool/None 뿐이다.
- 키 집합은 SPEC v2.1 §20.3a 를 **정확히** 따른다(추가 키 금지, 없는 값은 ``null``). 프롬프트
  (``prompts/*.md``)가 이 경로만 인용하므로 키 상수(``*_KEYS``)를 바꾸면 프롬프트도 바꿔야 한다.
- 숫자는 마지막에 ``features.compact`` 로 4유효자리 반올림한다(NaN/inf → null, 키 정렬).
- 모든 payload 에 ``"role"`` 과 ``"hard_limits"`` (= :data:`HARD_LIMITS_PAYLOAD`, 호출자가 바꿀 수 없음)가 있다.
- 캔들 원본은 넣지 않는다. 가격 시계열은 ``feature_pack.last_closes_1h``(최근 20개)뿐이다.
- 증거 채널 분리: technical payload 에는 논지(thesis)·확신도가 없고, bull payload 에는 bear 전용 키가,
  bear payload 에는 상대 변론 출력이 없다(같은 동결 증거를 각자 받는다).

입력 관용성
- 상위 맥락(``candidates_ctx`` 등)의 항목은 Mapping 이든 속성 객체(예: pipeline 의 ``CandidateContext``)든
  받는다(:func:`_get`). pydantic 모델은 ``model_dump(by_alias=True)`` 로 펼친다.
- 뉴스 이벤트 레코드는 ``NewsEvent`` 또는 Mapping 이며 ``ts``·``now_ms`` 가 있으면 나이를 다시 계산하고, 없을 때만 저장된 ``age_h`` 를 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from tradingbot.features import compact

log = logging.getLogger("tradingbot.agents.payloads")

__all__ = [
    # 상수
    "HARD_LIMITS_PAYLOAD", "FLOW_THRESHOLDS", "MAX_HEADLINES", "EVENT_WINDOW_H", "EVENT_HALF_LIFE_H",
    "MAX_SYMBOL_EVENTS", "MAX_REASON_CHARS",
    "TECHNICAL_KEYS", "TECHNICAL_POSITION_KEYS", "FLOW_KEYS", "NEWS_KEYS", "HEADLINE_KEYS",
    "FEED_HEALTH_KEYS", "PREV_EVENT_KEYS", "BULL_KEYS", "BEAR_KEYS", "BEAR_EXTRA_KEYS",
    "FEATURE_EXTRACT_KEYS", "NEWS_EVENT_KEYS", "NEWS_CONTEXT_KEYS", "EVENT_SIGNAL_KEYS",
    "REGIME_EXTRACT_KEYS", "DEBATE_POSITION_KEYS", "RISK_KEYS", "RISK_CANDIDATE_KEYS",
    "RISK_POSITION_KEYS", "EXIT_BLOCK_KEYS", "FLOW_BLOCK_KEYS", "ACCOUNT_PACK_KEYS", "BIAS_KEYS",
    "HEAD_KEYS", "HEAD_APPROVED_KEYS", "HEAD_TECH_LABEL_KEYS", "HEAD_NEWS_KEYS",
    "HEAD_RISK_EXTRA_KEYS", "GATE_KEYS", "FLAGGED_KEYS", "HEAD_SENTIMENT_KEYS",
    "HEAD_EVENT_SIGNAL_KEYS", "VETOED_KEYS", "VETO_STAGES",
    # 도우미
    "hard_limits", "to_json", "headline_id", "build_aliases", "feed_health_from",
    "normalize_news_event", "symbol_news_events", "event_signal", "combined_score",
    "channel_agreement", "unrealized_pct", "bars_held", "feature_extract", "exit_block",
    "flow_blocks_from", "normalize_account_pack", "make_account_pack",
    # 빌더
    "build_technical", "build_flow", "build_news", "build_bull", "build_bear", "build_risk",
    "build_head", "build_journal_post_trade", "build_journal_daily",
]

# ======================================================================= 상수 (SPEC v2.1 §20.3a)
HARD_LIMITS_PAYLOAD: dict[str, float] = {
    "max_position_pct": 10, "max_positions": 3, "max_stop_pct": 2, "max_tp_pct": 4, "min_confidence": 0.65,
}
FLOW_THRESHOLDS: dict[str, float] = {
    "funding_extreme_long": 0.0005, "funding_elevated_long": 0.0002, "funding_negative": -0.0001,
    "funding_squeeze": -0.0002,
    "oi_buildup_pct": 5.0, "oi_buildup_funding": 0.0003, "oi_deleveraging_pct": -5.0,
    "ls_crowded_long": 2.0, "ls_crowded_short": 0.8,
    "cascade_high_distance_pct": 2.0, "cascade_moderate_distance_pct": 3.0,
}

MAX_HEADLINES = 40              # news payload 헤드라인 최대 수
EVENT_WINDOW_H = 24.0           # 심볼 이벤트·prev_events 유효 창(시간)
EVENT_HALF_LIFE_H = 12.0        # event_signal 감쇠 반감기(시간)
UNKNOWN_AGE_H = 24.0            # 나이를 모르는 이벤트의 event_signal 가중치 계산용 나이(창 끝 = 가중치 0.25)
MAX_SYMBOL_EVENTS = 12          # 심볼별 이벤트 목록 상한(|score| 큰 것 우선 보존; n_events 는 전체 수)
MAX_REASON_CHARS = 240          # vetoed.reason / flag_reason 자르기
LAST_CLOSES_N = 20              # technical payload 의 1h 종가 개수 상한
ASSUMPTION_STATES = ("holding", "weakening", "impaired", "broken")

# --- 키 계약 ---------------------------------------------------------------
TECHNICAL_KEYS = ("role", "symbol", "feature_pack", "position", "hard_limits")
TECHNICAL_POSITION_KEYS = ("entry_price", "stop_loss_price", "take_profit_price", "last_mark",
                           "unrealized_pct", "bars_held", "opened_ts")
FLOW_KEYS = ("role", "symbols", "flow_packs", "thresholds", "hard_limits")
NEWS_KEYS = ("role", "headlines", "aliases", "now_ms", "feed_health", "prev_events", "hard_limits")
HEADLINE_KEYS = ("id", "ts", "age_h", "source", "source_grade", "title")
FEED_HEALTH_KEYS = ("items_fetched", "oldest_ts", "newest_ts", "errors")
PREV_EVENT_KEYS = ("headline_id", "score", "event_type", "symbols", "ts")
BULL_KEYS = ("role", "symbol", "technical", "feature_extract", "flow", "news_events", "news_context",
             "event_signal", "sentiment", "scanner_row", "regime", "position", "hard_limits")
BEAR_EXTRA_KEYS = ("corr_to_open", "hv_pct")
BEAR_KEYS = BULL_KEYS + BEAR_EXTRA_KEYS
FEATURE_EXTRACT_KEYS = ("cgo_60", "rsi_5", "rsi_14", "bb_position", "obv_trend",
                        "abnormal_volume_multiple", "volume_vs_hour_avg", "change_24h_pct")
NEWS_EVENT_KEYS = ("headline_id", "score", "event_type", "symbols", "summary", "stale", "age_h")
NEWS_CONTEXT_KEYS = ("market_tone", "risk_event_for_symbol", "feed_health_note")
EVENT_SIGNAL_KEYS = ("score", "n_events")
REGIME_EXTRACT_KEYS = ("btc_regime_4h", "fused", "density", "verdict", "btc_shock", "name_alarm_symbols")
DEBATE_POSITION_KEYS = ("entry_price", "unrealized_pct", "bars_held", "thesis")
RISK_KEYS = ("role", "candidates", "positions", "risk_packs", "regime", "account_pack",
             "news_risk_symbols", "flow_blocks", "hard_limits")
RISK_CANDIDATE_KEYS = ("symbol", "candidate", "technical", "flow", "bull", "bear", "planned_notional_usdt")
RISK_POSITION_KEYS = ("symbol", "entry_price", "mark", "unrealized_pct", "bars_held", "thesis",
                      "prev_assumption_status", "red_line_hits", "health_history", "technical")
EXIT_BLOCK_KEYS = ("exit_flag", "exit_reason")
FLOW_BLOCK_KEYS = ("block_new_longs", "stop_inside_cascade", "cascade_risk")
BIAS_KEYS = ("disposition", "overtrading", "chasing", "anchoring")
ACCOUNT_PACK_KEYS = ("equity", "daily_pnl_pct", "drawdown_pct", "drawdown_ladder_level", "exposure_pct",
                     "position_count", "consecutive_losses", "cooldowns", "trades_today", "decay_state",
                     "bias_severities", "noise_trade_share", "llm_budget_remaining_usd")
HEAD_KEYS = ("role", "approved", "flagged_positions", "regime", "account_pack", "sentiment", "vetoed",
             "hard_limits", "abstained_channels")
HEAD_APPROVED_KEYS = ("symbol", "candidate", "technical", "flow", "news", "bull", "bear", "risk", "gate")
HEAD_TECH_LABEL_KEYS = ("ichimoku_4h_price_vs_cloud", "cgo_60", "abnormal_volume_multiple", "change_24h_pct")
HEAD_NEWS_KEYS = ("events", "market_tone", "risk_event_for_symbol")
HEAD_RISK_EXTRA_KEYS = ("confidence_floor", "entries_paused", "exposure_cap_pct", "risk_confidence")
GATE_KEYS = ("confidence_floor", "size_multiplier", "notes")
FLAGGED_KEYS = ("symbol", "entry_price", "mark", "unrealized_pct", "bars_held", "thesis", "red_line_hits",
                "health_history", "risk", "technical", "flag_reason")
HEAD_SENTIMENT_KEYS = ("composite", "event_signal")
HEAD_EVENT_SIGNAL_KEYS = ("score", "n_events", "combined_score")
VETOED_KEYS = ("symbol", "stage", "reason")
VETO_STAGES = ("g1", "bear_decline", "risk_veto", "g3")

_DRAWDOWN_LEVELS = ("normal", "caution", "block", "stop_all")
_DECAY_STATES = ("insufficient_sample", "healthy", "warning", "decayed")
_BIAS_LEVELS = ("none", "mild", "severe", "insufficient_sample")
_STAGE_ALIASES = {
    "g1": "g1", "g1_candidate": "g1",
    "g2": "bear_decline", "g2_bear": "bear_decline", "bear": "bear_decline", "bear_decline": "bear_decline",
    "risk": "risk_veto", "veto": "risk_veto", "risk_veto": "risk_veto", "g3_risk_veto": "risk_veto",
    "g3": "g3", "g3_risk": "g3", "g3_other": "g3",
}


# ======================================================================= 기초 도우미
def hard_limits() -> dict:
    """payload 용 하드 리밋 사본(호출자가 바꿔도 상수에 영향 없음)."""
    return dict(HARD_LIMITS_PAYLOAD)


def to_json(payload: Any) -> str:
    """결정적 직렬화(키 정렬·한글 그대로·공백 없음·NaN 금지)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Mapping 이면 ``obj.get(key)``, 아니면 ``getattr``. None 객체는 default."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _has(obj: Any, key: str) -> bool:
    if obj is None:
        return False
    if isinstance(obj, Mapping):
        return key in obj
    return hasattr(obj, key)


def _plain(obj: Any) -> Any:
    """BaseModel/Mapping/시퀀스/집합 → 평범한 JSON 타입(반올림 전). 집합은 정렬된 리스트."""
    if isinstance(obj, BaseModel):
        return _plain(obj.model_dump(mode="python", by_alias=True))
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_plain(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    return str(obj)


def _finalize(payload: Mapping[str, Any]) -> dict:
    """평범한 타입으로 바꾸고 4유효자리 반올림(키 정렬)."""
    return compact(_plain(payload))


def _dump(obj: Any) -> Any:
    """모델/Mapping → dict (없으면 None)."""
    if obj is None:
        return None
    return _plain(obj)


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _int(x: Any) -> int | None:
    v = _num(x)
    return int(v) if v is not None else None


def _pick(src: Any, keys: Sequence[str]) -> dict:
    """``src`` 에서 정확히 ``keys`` 만 뽑는다(없으면 None)."""
    d = _plain(src) if src is not None else {}
    if not isinstance(d, Mapping):
        d = {}
    return {k: d.get(k) for k in keys}


def _str_list(xs: Any) -> list[str]:
    if xs is None:
        return []
    if isinstance(xs, str):
        return [xs]
    if isinstance(xs, (set, frozenset)):
        return sorted(str(x) for x in xs)
    try:
        return [str(x) for x in xs]
    except TypeError:
        return []


def _uniq(xs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _clip_text(s: Any, n: int = MAX_REASON_CHARS) -> str | None:
    if s is None:
        return None
    t = str(s)
    return t if len(t) <= n else t[: n - 1] + "…"


def _base(symbol: str) -> str:
    return str(symbol).split("/", 1)[0].strip().upper()


def _symbol_in(symbol: str, symbols: Iterable[Any]) -> bool:
    """정확히 같거나 base 티커가 같으면 매칭('BTC' ↔ 'BTC/USDT')."""
    b = _base(symbol)
    for s in symbols or []:
        s = str(s)
        if s == symbol or _base(s) == b:
            return True
    return False


# ======================================================================= 포지션 수치
def unrealized_pct(entry_price: Any, mark: Any) -> float | None:
    """(mark/entry − 1)×100. 어느 하나라도 없거나 entry ≤ 0 이면 None."""
    e, m = _num(entry_price), _num(mark)
    if e is None or m is None or e <= 0:
        return None
    return (m / e - 1.0) * 100.0


def bars_held(position: Any, cycle: Any) -> int | None:
    """``cycle − position.opened_cycle`` (음수는 0). cycle 이 없으면 None."""
    c, o = _int(cycle), _int(_get(position, "opened_cycle"))
    if c is None or o is None:
        return None
    return max(0, c - o)


def _thesis_dump(thesis: Any) -> dict | None:
    """구조화 Thesis(모델/Mapping)만 싣는다. 문자열(Position.thesis)이나 형식 오류는 None."""
    if thesis is None or isinstance(thesis, str):
        return None
    from tradingbot.agents.schemas import Thesis

    if isinstance(thesis, Thesis):
        return _plain(thesis)
    if isinstance(thesis, Mapping):
        try:
            return _plain(Thesis.model_validate(dict(thesis)))
        except Exception:  # noqa: BLE001 - 형식 오류 논지는 null 로(날조 금지)
            log.warning("thesis 형식 오류 — null 로 전달")
            return None
    return None


def _position_numbers(item: Any) -> dict:
    """item(Mapping/객체) 또는 그 안의 ``position`` 에서 entry/mark/unrealized/bars 를 만든다."""
    pos = _get(item, "position")
    entry = _num(_get(item, "entry_price"))
    if entry is None:
        entry = _num(_get(pos, "entry_price"))
    mark = _num(_get(item, "mark"))
    if mark is None:
        mark = _num(_get(item, "last_mark"))
    if mark is None:
        mark = _num(_get(pos, "last_mark"))
    unreal = _num(_get(item, "unrealized_pct"))
    if unreal is None:
        unreal = unrealized_pct(entry, mark)
    bars = _int(_get(item, "bars_held"))
    if bars is None and pos is not None:
        bars = bars_held(pos, _get(item, "cycle"))
    return {"entry_price": entry, "mark": mark, "unrealized_pct": unreal, "bars_held": bars}


def exit_block(technical: Any) -> dict | None:
    """technical 출력에서 ``{exit_flag, exit_reason}`` 만(기권이면 None)."""
    if technical is None:
        return None
    flag = _get(technical, "exit_flag")
    reason = _get(technical, "exit_reason")
    return {"exit_flag": bool(flag) if flag is not None else False,
            "exit_reason": None if reason is None else str(reason)}


# ======================================================================= 뉴스 도우미
def headline_id(item: Any) -> str:
    """SPEC: ``sha1(url or title)[:12]``."""
    url = _get(item, "url") or ""
    key = str(url) if url else str(_get(item, "title") or "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _age_h(ts: Any, now_ms: Any) -> float | None:
    t, n = _num(ts), _num(now_ms)
    if t is None or n is None:
        return None
    return max(0.0, (n - t) / 3.6e6)


def build_aliases(symbols: Iterable[str]) -> dict[str, str]:
    """``{alias_lower: symbol}`` — base 티커 소문자 + ``news.SYMBOL_ALIASES``. 충돌 시 먼저 온 심볼이 이긴다."""
    from tradingbot.news import SYMBOL_ALIASES

    out: dict[str, str] = {}
    for sym in symbols or []:
        sym = str(sym)
        base = _base(sym)
        if not base:
            continue
        for alias in (base.lower(), *SYMBOL_ALIASES.get(base, ())):
            out.setdefault(str(alias).lower(), sym)
    return dict(sorted(out.items()))


def feed_health_from(items: Sequence[Any] | None, errors: Iterable[Any] | None = None) -> dict:
    """수집 결과로 ``feed_health`` 를 만든다(``items_fetched``·``oldest_ts``·``newest_ts``·``errors``)."""
    items = list(items or [])
    ts = [t for t in (_int(_get(it, "published_ts")) for it in items) if t is not None]
    return {"items_fetched": len(items), "oldest_ts": min(ts) if ts else None,
            "newest_ts": max(ts) if ts else None, "errors": _str_list(errors)}


def _norm_feed_health(fh: Any) -> dict:
    if fh is None:
        return {"items_fetched": None, "oldest_ts": None, "newest_ts": None, "errors": []}
    return {"items_fetched": _int(_get(fh, "items_fetched")), "oldest_ts": _int(_get(fh, "oldest_ts")),
            "newest_ts": _int(_get(fh, "newest_ts")), "errors": _str_list(_get(fh, "errors"))}


def normalize_news_event(ev: Any, now_ms: int | None = None) -> dict:
    """이벤트 레코드 → ``NEWS_EVENT_KEYS`` dict. ``age_h`` 는 ``ts``·``now_ms`` 로 다시 계산 → (불가하면) 레코드 값 순.

    저장된 ``age_h`` 는 이전 사이클 값일 수 있으므로 ``ts``·``now_ms`` 가 있으면 항상 지금 나이를 쓴다.

    ``stale`` 은 나이를 알면 코드가 정한다(age_h > 24), 모르면 레코드 값(없으면 False).
    """
    age = _age_h(_get(ev, "ts"), now_ms)
    if age is None:
        age = _num(_get(ev, "age_h"))
        if age is not None and age < 0:
            age = 0.0
    score = _num(_get(ev, "score"))
    if score is not None:
        score = max(-1.0, min(1.0, score))
    stale_raw = _get(ev, "stale")
    stale = (age > EVENT_WINDOW_H) if age is not None else bool(stale_raw)
    et = _get(ev, "event_type")
    summary = _get(ev, "summary")
    return {"headline_id": str(_get(ev, "headline_id") or ""), "score": score,
            "event_type": None if et is None else str(et), "symbols": _str_list(_get(ev, "symbols")),
            "summary": None if summary is None else str(summary), "stale": stale, "age_h": age}


def _event_sort_key(e: Mapping[str, Any]) -> tuple:
    age = e.get("age_h")
    return (age is None, age if age is not None else 0.0, e.get("headline_id") or "")


def symbol_news_events(symbol: str, events: Iterable[Any] | None, now_ms: int | None = None,
                       *, limit: int | None = MAX_SYMBOL_EVENTS) -> list[dict]:
    """이 심볼의 24h 내 이벤트(정규화, 최신순, 중복 headline_id 제거).

    나이를 모르는 이벤트는 버리지 않는다(위험 헤드라인을 놓치지 않게). ``limit`` 초과 시 |score| 큰 것을 남긴다.
    """
    out: dict[str, dict] = {}
    for ev in events or []:
        e = normalize_news_event(ev, now_ms)
        if not _symbol_in(symbol, e["symbols"]):
            continue
        if e["age_h"] is not None and e["age_h"] > EVENT_WINDOW_H:
            continue
        key = e["headline_id"] or json.dumps(e, sort_keys=True, ensure_ascii=False)
        prev = out.get(key)
        if prev is None or _event_sort_key(e) < _event_sort_key(prev):
            out[key] = e
    evs = sorted(out.values(), key=_event_sort_key)
    if limit is not None and len(evs) > limit:
        keep = sorted(evs, key=lambda e: (-(abs(e["score"]) if e["score"] is not None else 0.0),
                                          _event_sort_key(e)))[:limit]
        ids = {id(e) for e in keep}
        evs = [e for e in evs if id(e) in ids]
    return evs


def event_signal(events: Iterable[Any] | None, now_ms: int | None = None) -> dict:
    """SPEC: ``clip(Σ score·0.5^(age_h/12), −1, 1)``. 이벤트(점수 있는)가 없으면 score None.

    나이를 모르는 이벤트는 창 끝(24h, 가중치 0.25)으로 본다.
    """
    total = 0.0
    n = 0
    for ev in events or []:
        as_is = (isinstance(ev, Mapping) and "score" in ev and ev.get("age_h") is not None
                 and (now_ms is None or ev.get("ts") is None))
        e = ev if as_is else normalize_news_event(ev, now_ms)
        s = _num(e.get("score"))
        if s is None:
            continue
        s = max(-1.0, min(1.0, s))
        age = _num(e.get("age_h"))
        age = UNKNOWN_AGE_H if age is None else max(0.0, age)
        total += s * 0.5 ** (age / EVENT_HALF_LIFE_H)
        n += 1
    return {"score": max(-1.0, min(1.0, total)) if n else None, "n_events": n}


def combined_score(technical_score: Any, event_score: Any) -> float | None:
    """SPEC: ``0.6·technical.score/5 + 0.4·event_score``(이벤트 없으면 0). technical 기권이면 None(참고용)."""
    t = _num(technical_score)
    if t is None:
        return None
    e = _num(event_score)
    return 0.6 * t / 5.0 + 0.4 * (e if e is not None else 0.0)


def channel_agreement(technical: Any, flow: Any, event_score: Any) -> dict:
    """SPEC §20.3a.7: technical.direction=="long", flow.flow_bias=="long", event_signal.score>0 (기권 = False)."""
    e = _num(event_score)
    return {"technical": bool(technical is not None and _get(technical, "direction") == "long"),
            "flow": bool(flow is not None and _get(flow, "flow_bias") == "long"),
            "news": bool(e is not None and e > 0)}


def _news_risk_for(symbol: str, news: Any) -> bool:
    if news is None:
        return False
    return _symbol_in(symbol, _str_list(_get(news, "risk_event_symbols")))


# ======================================================================= feature / regime 추출
def feature_extract(fp: Any) -> dict | None:
    """bull/bear 용 행동재무 추출(rsi_5·rsi_14·bb_position·obv_trend 는 1h 값). fp 없으면 None."""
    if fp is None:
        return None
    per_tf = _get(fp, "per_tf") or {}
    h1 = per_tf.get("1h") if isinstance(per_tf, Mapping) else None
    return {"cgo_60": _get(fp, "cgo_60"), "rsi_5": _get(h1, "rsi_5"), "rsi_14": _get(h1, "rsi_14"),
            "bb_position": _get(h1, "bb_position"), "obv_trend": _get(h1, "obv_trend"),
            "abnormal_volume_multiple": _get(fp, "abnormal_volume_multiple"),
            "volume_vs_hour_avg": _get(fp, "volume_vs_hour_avg"), "change_24h_pct": _get(fp, "change_24h_pct")}


def _head_labels(fp: Any) -> dict:
    ich = _get(fp, "ichimoku_4h")
    return {"ichimoku_4h_price_vs_cloud": _get(ich, "price_vs_cloud"), "cgo_60": _get(fp, "cgo_60"),
            "abnormal_volume_multiple": _get(fp, "abnormal_volume_multiple"),
            "change_24h_pct": _get(fp, "change_24h_pct")}


def _regime_extract(regime: Any) -> dict | None:
    return None if regime is None else _pick(regime, REGIME_EXTRACT_KEYS)


def _scanner_row(scanner_row: Any, liquidity: Any) -> dict | None:
    """Candidate + ``{"liquidity": LiquidityGate}``. 후보 행이 없으면(보유만) None."""
    if scanner_row is None:
        return None
    row = _plain(scanner_row)
    if not isinstance(row, dict):
        return None
    if liquidity is not None:
        row["liquidity"] = _plain(liquidity)
    else:
        row.setdefault("liquidity", None)
    return row


# ======================================================================= account / flow blocks
def normalize_account_pack(pack: Any) -> dict:
    """ACCOUNT_PACK 을 정확한 키 집합으로(없는 값 null, 알 수 없는 enum 은 보수적 값)."""
    src = _plain(pack) if pack is not None else {}
    if not isinstance(src, Mapping):
        src = {}
    lvl = src.get("drawdown_ladder_level")
    if lvl is None and "drawdown_level" in src:      # 구 이름 관용
        lvl = src.get("drawdown_level")
    ds = src.get("decay_state")
    bias_src = src.get("bias_severities") if isinstance(src.get("bias_severities"), Mapping) else {}
    cd_src = src.get("cooldowns") if isinstance(src.get("cooldowns"), Mapping) else {}
    cooldowns = {str(k): int(v) for k, v in sorted(cd_src.items(), key=lambda kv: str(kv[0]))
                 if _int(v) is not None and int(v) > 0}
    return {
        "equity": _num(src.get("equity")),
        "daily_pnl_pct": _num(src.get("daily_pnl_pct")),
        "drawdown_pct": _num(src.get("drawdown_pct")),
        "drawdown_ladder_level": lvl if lvl in _DRAWDOWN_LEVELS else None,
        "exposure_pct": _num(src.get("exposure_pct")),
        "position_count": _int(src.get("position_count")),
        "consecutive_losses": _int(src.get("consecutive_losses")),
        "cooldowns": cooldowns,
        "trades_today": _int(src.get("trades_today")),
        "decay_state": ds if ds in _DECAY_STATES else "insufficient_sample",
        "bias_severities": {k: (bias_src.get(k) if bias_src.get(k) in _BIAS_LEVELS else "insufficient_sample")
                            for k in BIAS_KEYS},
        "noise_trade_share": _num(src.get("noise_trade_share")),
        "llm_budget_remaining_usd": _num(src.get("llm_budget_remaining_usd")),
    }


def make_account_pack(state: Any, equity: float, *, marks: Mapping[str, float] | None = None,
                      journal_state: Any = None, llm_budget_remaining_usd: float | None = None) -> dict:
    """BotState(+JournalState)로 ACCOUNT_PACK 을 만든다(순수 함수).

    - ``daily_pnl_pct`` = (equity / daily.start_equity − 1)×100, ``drawdown_pct`` = (1 − equity/peak)×100 (양수 = 낙폭).
    - ``drawdown_ladder_level`` = ``risk.drawdown_level``. ``exposure_pct`` = Σ 보유 평가액 / equity ×100
      (마크: ``marks`` → ``last_mark`` → 진입가).
    - ``cooldowns`` = 쿨다운·복수매매 창 중 남은 사이클의 최댓값(> 0 만).
    - ``noise_trade_share`` = ``journal.noise_trade_share(state.closed_trades)``.
    """
    from tradingbot import risk
    from tradingbot.journal import noise_trade_share

    eq = _num(equity)
    daily = _get(state, "daily")
    start = _num(_get(daily, "start_equity"))
    peak = _num(_get(state, "peak_equity"))
    positions = _get(state, "positions") or {}
    marks = marks or {}
    exposure = 0.0
    for sym, pos in positions.items():
        px = _num(marks.get(sym)) or _num(_get(pos, "last_mark")) or _num(_get(pos, "entry_price")) or 0.0
        exposure += (_num(_get(pos, "base_amount")) or 0.0) * px
    cycle = _int(_get(state, "cycle")) or 0
    cooldowns: dict[str, int] = {}
    for table in (_get(state, "cooldown_until_cycle") or {}, _get(state, "loss_cooldown_until_cycle") or {}):
        for sym, until in table.items():
            left = (_int(until) or 0) - cycle
            if left > 0:
                cooldowns[sym] = max(cooldowns.get(sym, 0), left)
    try:
        noise = noise_trade_share(list(_get(state, "closed_trades") or []))
    except Exception:  # noqa: BLE001
        noise = None
    pack = {
        "equity": eq,
        "daily_pnl_pct": (eq / start - 1.0) * 100.0 if eq is not None and start and start > 0 else None,
        "drawdown_pct": max(0.0, (1.0 - eq / peak) * 100.0) if eq is not None and peak and peak > 0 else None,
        "drawdown_ladder_level": risk.drawdown_level(eq if eq is not None else float("nan"), peak or 0.0),
        "exposure_pct": exposure / eq * 100.0 if eq and eq > 0 else None,
        "position_count": len(positions),
        "consecutive_losses": _int(_get(daily, "consecutive_losses")),
        "cooldowns": cooldowns,
        "trades_today": _int(_get(daily, "trades")),
        "decay_state": _get(journal_state, "decay_state", "insufficient_sample"),
        "bias_severities": dict(_get(journal_state, "bias") or {}),
        "noise_trade_share": noise,
        "llm_budget_remaining_usd": llm_budget_remaining_usd,
    }
    return normalize_account_pack(pack)


def flow_blocks_from(flow: Any) -> dict:
    """FlowOutput / FlowSymbolView 목록 / {sym: view} → ``{sym: {block_new_longs, stop_inside_cascade, cascade_risk}}``."""
    if flow is None:
        return {}
    if isinstance(flow, Mapping) and "per_symbol" not in flow:
        views = list(flow.values())
    else:
        views = _get(flow, "per_symbol", flow)
    out: dict[str, dict] = {}
    for v in views or []:
        sym = _get(v, "symbol")
        if sym is None:
            continue
        out[str(sym)] = _flow_block(v)
    return dict(sorted(out.items()))


def _flow_block(v: Any) -> dict:
    return {"block_new_longs": bool(_get(v, "block_new_longs")),
            "stop_inside_cascade": bool(_get(v, "stop_inside_cascade")),
            "cascade_risk": _get(v, "cascade_risk")}


# ======================================================================= technical
def build_technical(symbol: str, fp: Any, position: Any = None, mark: Any = None, cycle: Any = None) -> dict:
    """§20.3a.1. ``feature_pack`` 전체(1h 종가는 최근 20개만) + 보유 시 포지션 수치.

    포지션 블록에는 thesis·confidence 를 **넣지 않는다**(총괄 출력이 분석가 입력이 되지 않게).
    구 시그니처(``hard_limits`` 를 4번째 인자로)도 받아들인다: Mapping 이면 무시하고 하드 리밋 상수를 쓴다.
    """
    if isinstance(mark, Mapping):
        log.debug("build_technical: hard_limits 인자는 무시(상수 사용)")
        mark = None
    fp_d = _dump(fp)
    if isinstance(fp_d, dict):
        closes = fp_d.get("last_closes_1h") or []
        fp_d["last_closes_1h"] = list(closes)[-LAST_CLOSES_N:]
    pos_d = None
    if position is not None:
        m = _num(mark)
        if m is None:
            m = _num(_get(position, "last_mark"))
        entry = _num(_get(position, "entry_price"))
        pos_d = {"entry_price": entry, "stop_loss_price": _num(_get(position, "stop_loss_price")),
                 "take_profit_price": _num(_get(position, "take_profit_price")), "last_mark": m,
                 "unrealized_pct": unrealized_pct(entry, m), "bars_held": bars_held(position, cycle),
                 "opened_ts": _int(_get(position, "opened_ts"))}
    return _finalize({"role": "technical", "symbol": str(symbol), "feature_pack": fp_d, "position": pos_d,
                      "hard_limits": hard_limits()})


# ======================================================================= derivatives_flow
def build_flow(flow_packs: Mapping[str, Any] | None, symbols: Iterable[str], thresholds: Any = None) -> dict:
    """§20.3a.2. ``symbols``(후보 ∪ 보유, 입력 순서·중복 제거) == ``flow_packs`` 키. 없는 팩은 null.

    ``thresholds`` 는 구 시그니처 호환용이며 무시된다(항상 :data:`FLOW_THRESHOLDS`).
    """
    if thresholds is not None:
        try:
            differs = dict(thresholds) != FLOW_THRESHOLDS
        except (TypeError, ValueError):
            differs = True
        if differs:
            log.warning("build_flow: 전달된 thresholds 무시(상수 FLOW_THRESHOLDS 사용)")
    raw = symbols or []
    if isinstance(raw, (set, frozenset)):          # set 은 해시 순서 → 결정성을 위해 정렬
        raw = sorted(str(s) for s in raw)
    syms = _uniq(str(s) for s in raw)
    packs = flow_packs or {}
    out = {s: _dump(packs.get(s)) for s in syms}
    missing = [s for s in syms if out[s] is None]
    if missing:
        log.info("flow pack 없음: %s", ",".join(missing))
    return _finalize({"role": "derivatives_flow", "symbols": syms, "flow_packs": out,
                      "thresholds": dict(FLOW_THRESHOLDS), "hard_limits": hard_limits()})


# ======================================================================= news_sentiment
def _headline(item: Any, now_ms: Any) -> dict:
    hid = _get(item, "id")
    if not hid:
        hid = headline_id(item)
    ts = _get(item, "ts") if _has(item, "ts") and not _has(item, "published_ts") else _get(item, "published_ts")
    ts = _int(ts)
    grade = _get(item, "source_grade")
    if grade not in ("high", "low"):
        grade = "high" if _int(_get(item, "tier")) == 1 else "low"
    return {"id": str(hid), "ts": ts, "age_h": _age_h(ts, now_ms), "source": str(_get(item, "source") or ""),
            "source_grade": grade, "title": str(_get(item, "title") or "")}


def _aliases_from_mapping(aliases: Mapping[Any, Any]) -> dict[str, str]:
    """``{alias: symbol}`` 은 그대로(소문자 키). 값이 별칭 묶음(``news.SYMBOL_ALIASES`` 형태: 심볼/base →
    별칭 튜플)이면 ``{alias_lower: 키}`` 로 뒤집고 base 소문자도 넣는다. 값은 항상 심볼 문자열이다."""
    out: dict[str, str] = {}
    for k, v in aliases.items():
        if v is None:
            continue
        if isinstance(v, str):
            out.setdefault(str(k).lower(), v)
            continue
        sym = str(k)
        base = _base(sym)
        names = [base.lower()] if base else []
        names += _str_list(v)
        for a in names:
            out.setdefault(str(a).lower(), sym)
    return out


def build_news(headlines_new: Iterable[Any] | None, aliases: Any, now_ms: int, feed_health: Any,
               prev_events: Iterable[Any] | None) -> dict:
    """§20.3a.3. 헤드라인: 최신순(ts 없으면 뒤)·id 중복 제거·최대 40. prev_events: 24h 내(ts 없으면 제외), 최신순.

    ``aliases`` 는 ``{alias: symbol}`` 또는 심볼 목록(→ :func:`build_aliases`).
    """
    now = _int(now_ms)
    heads: dict[str, dict] = {}
    for it in headlines_new or []:
        h = _headline(it, now)
        if h["id"] not in heads:
            heads[h["id"]] = h
    ordered = sorted(heads.values(), key=lambda h: (h["ts"] is None, -(h["ts"] or 0), h["id"]))[:MAX_HEADLINES]

    if isinstance(aliases, Mapping):
        al = _aliases_from_mapping(aliases)
    else:
        al = build_aliases(aliases or [])

    prev: dict[str, dict] = {}
    for ev in prev_events or []:
        ts = _int(_get(ev, "ts"))
        if ts is None or now is None or (now - ts) / 3.6e6 > EVENT_WINDOW_H:
            continue
        score = _num(_get(ev, "score"))
        e = {"headline_id": str(_get(ev, "headline_id") or ""),
             "score": None if score is None else max(-1.0, min(1.0, score)),
             "event_type": _get(ev, "event_type"), "symbols": _str_list(_get(ev, "symbols")), "ts": ts}
        key = e["headline_id"]
        if key not in prev or ts > prev[key]["ts"]:
            prev[key] = e
    prev_list = sorted(prev.values(), key=lambda e: (-e["ts"], e["headline_id"]))

    return _finalize({"role": "news_sentiment", "headlines": ordered, "aliases": dict(sorted(al.items())),
                      "now_ms": now, "feed_health": _norm_feed_health(feed_health), "prev_events": prev_list,
                      "hard_limits": hard_limits()})


# ======================================================================= bull / bear (동결 증거)
def _debate_common(role: str, symbol: str, tech: Any, flow_view: Any, news_events: Any, sentiment: Any,
                   scanner_row: Any, regime: Any, position: Any, *, feature_pack: Any, news: Any,
                   liquidity: Any, mark: Any, cycle: Any, thesis: Any, now_ms: Any) -> dict:
    sym = str(symbol)
    all_events = symbol_news_events(sym, news_events, now_ms, limit=None)
    evs = symbol_news_events(sym, all_events, now_ms)
    news_ctx = None
    if news is not None:
        news_ctx = {"market_tone": _num(_get(news, "market_tone")),
                    "risk_event_for_symbol": _news_risk_for(sym, news),
                    "feed_health_note": _get(news, "feed_health_note")}
    pos_d = None
    if position is not None:
        nums = _position_numbers({"position": position, "mark": mark, "cycle": cycle})
        pos_d = {"entry_price": nums["entry_price"], "unrealized_pct": nums["unrealized_pct"],
                 "bars_held": nums["bars_held"], "thesis": _thesis_dump(thesis)}
    return {
        "role": role, "symbol": sym, "technical": _dump(tech), "feature_extract": feature_extract(feature_pack),
        "flow": _dump(flow_view), "news_events": evs, "news_context": news_ctx,
        "event_signal": event_signal(all_events), "sentiment": _dump(sentiment),
        "scanner_row": _scanner_row(scanner_row, liquidity), "regime": _regime_extract(regime),
        "position": pos_d, "hard_limits": hard_limits(),
    }


def build_bull(symbol: str, tech: Any, flow_view: Any, news_events: Any, sentiment: Any, scanner_row: Any,
               regime: Any, position: Any = None, *, feature_pack: Any = None, news: Any = None,
               liquidity: Any = None, mark: Any = None, cycle: Any = None, thesis: Any = None,
               now_ms: int | None = None) -> dict:
    """§20.3a.4 bull. bear 전용 키(corr_to_open·hv_pct)와 bear 출력은 없다.

    - ``news_events``: 이벤트 레코드(``NewsEvent``/Mapping; ``age_h`` 또는 ``ts``) — 이 심볼·24h 로 걸러진다.
    - ``news``: 이번 사이클 ``NewsOutput``(기권이면 None → ``news_context`` null).
    - ``thesis``: ``JournalState.thesis[symbol]``(구조화). ``Position.thesis``(문자열)는 쓰지 않는다.
    """
    return _finalize(_debate_common("bull", symbol, tech, flow_view, news_events, sentiment, scanner_row,
                                    regime, position, feature_pack=feature_pack, news=news, liquidity=liquidity,
                                    mark=mark, cycle=cycle, thesis=thesis, now_ms=now_ms))


def build_bear(symbol: str, tech: Any, flow_view: Any, news_events: Any, sentiment: Any, scanner_row: Any,
               regime: Any, position: Any = None, corr_to_open: Mapping[str, Any] | None = None,
               hv_pct: Any = None, *, feature_pack: Any = None, news: Any = None, liquidity: Any = None,
               mark: Any = None, cycle: Any = None, thesis: Any = None, now_ms: int | None = None) -> dict:
    """§20.3a.4 bear = bull 과 같은 동결 증거 + ``corr_to_open`` + ``hv_pct``(없으면 ``feature_pack.hv_pct_rank``)."""
    d = _debate_common("bear", symbol, tech, flow_view, news_events, sentiment, scanner_row, regime, position,
                       feature_pack=feature_pack, news=news, liquidity=liquidity, mark=mark, cycle=cycle,
                       thesis=thesis, now_ms=now_ms)
    corr = {str(k): _num(v) for k, v in (corr_to_open or {}).items()}
    d["corr_to_open"] = dict(sorted(corr.items()))
    hv = _num(hv_pct)
    if hv is None and feature_pack is not None:
        hv = _num(_get(feature_pack, "hv_pct_rank"))
    d["hv_pct"] = hv
    return _finalize(d)


# ======================================================================= risk_manager
def _risk_candidate(item: Any) -> dict:
    return {"symbol": str(_get(item, "symbol") or _get(_get(item, "candidate"), "symbol") or ""),
            "candidate": _dump(_get(item, "candidate")), "technical": _dump(_get(item, "technical")),
            "flow": _dump(_get(item, "flow")), "bull": _dump(_get(item, "bull")), "bear": _dump(_get(item, "bear")),
            "planned_notional_usdt": _num(_get(item, "planned_notional_usdt"))}


def _assumption_list(x: Any) -> list[str] | None:
    if x is None:
        return None
    lst = _str_list(x)
    return lst or None


def _position_common(item: Any) -> dict:
    nums = _position_numbers(item)
    sym = _get(item, "symbol") or _get(_get(item, "position"), "symbol") or ""
    tech = _get(item, "technical")
    # 이미 {exit_flag, exit_reason} 형태이거나 TechnicalOutput — 둘 다 exit 블록으로 줄인다
    return {"symbol": str(sym), **nums, "thesis": _thesis_dump(_get(item, "thesis")),
            "red_line_hits": _str_list(_get(item, "red_line_hits")),
            "health_history": [int(h) for h in (_get(item, "health_history") or []) if _int(h) is not None],
            "technical": exit_block(tech)}


def _risk_position(item: Any) -> dict:
    d = _position_common(item)
    d["prev_assumption_status"] = _assumption_list(_get(item, "prev_assumption_status"))
    return {k: d[k] for k in RISK_POSITION_KEYS}


def build_risk(candidates_ctx: Iterable[Any] | None, positions_ctx: Iterable[Any] | None,
               risk_packs: Mapping[str, Any] | None, regime: Any, account_pack: Any,
               news_risk_symbols: Iterable[str] | None, flow_blocks: Any) -> dict:
    """§20.3a.5.

    - ``candidates_ctx`` 항목: ``symbol, candidate, technical, flow, bull, bear, planned_notional_usdt``.
    - ``positions_ctx`` 항목: ``symbol`` + (``position``(Position)·``mark``·``cycle`` 또는 ``entry_price``·``mark``·
      ``unrealized_pct``·``bars_held``) + ``thesis``(구조화)·``prev_assumption_status``·``red_line_hits``·
      ``health_history``·``technical``(TechnicalOutput 또는 exit 블록).
    - ``risk_packs`` 는 후보 ∪ 보유 심볼로 줄인다(둘 다 비어 있으면 전달된 전부).
    - ``flow_blocks``: ``{sym: {...}}`` 또는 FlowOutput(→ :func:`flow_blocks_from`).
    """
    cands = [_risk_candidate(c) for c in (candidates_ctx or [])]
    poss = [_risk_position(p) for p in (positions_ctx or [])]
    wanted = {c["symbol"] for c in cands} | {p["symbol"] for p in poss}
    packs = {str(k): _dump(v) for k, v in (risk_packs or {}).items() if not wanted or str(k) in wanted}
    if isinstance(flow_blocks, Mapping) and "per_symbol" not in flow_blocks:
        fb = {str(k): _flow_block(v) for k, v in flow_blocks.items()}
    else:
        fb = flow_blocks_from(flow_blocks)
    return _finalize({
        "role": "risk_manager", "candidates": cands, "positions": poss, "risk_packs": dict(sorted(packs.items())),
        "regime": _dump(regime), "account_pack": normalize_account_pack(account_pack),
        "news_risk_symbols": sorted(set(_str_list(news_risk_symbols))), "flow_blocks": dict(sorted(fb.items())),
        "hard_limits": hard_limits(),
    })


# ======================================================================= head
def _floor(f: float | None) -> float:
    """실효 하한은 하드 최소 신뢰도(G4) 아래로 내려가지 않는다."""
    hard = float(HARD_LIMITS_PAYLOAD["min_confidence"])
    return hard if f is None else max(hard, f)


def _effective_gate(item: Any) -> dict | None:
    """``gate``(단일) 또는 ``gates``(목록: 하한 최댓값·배수 곱·노트 합집합)."""
    g = _get(item, "gate")
    if g is not None:
        return {"confidence_floor": _floor(_num(_get(g, "confidence_floor"))),
                "size_multiplier": _num(_get(g, "size_multiplier")), "notes": _str_list(_get(g, "notes"))}
    gates = list(_get(item, "gates") or [])
    if not gates:
        return None
    floors = [f for f in (_num(_get(x, "confidence_floor")) for x in gates) if f is not None]
    mult = 1.0
    for x in gates:
        m = _num(_get(x, "size_multiplier"))
        if m is not None:
            mult *= m
    notes = _uniq(n for x in gates for n in _str_list(_get(x, "notes")))
    return {"confidence_floor": _floor(max(floors) if floors else None), "size_multiplier": mult, "notes": notes}


def _is_news_block(given: Any) -> bool:
    """이미 만들어진 {events, market_tone, risk_event_for_symbol} 블록인가(덤프된 NewsOutput 은 아님)."""
    return (isinstance(given, Mapping) and "events" in given
            and "risk_event_symbols" not in given and "risk_event" not in given)


def _head_news(symbol: str, item: Any, now_ms: Any) -> dict | None:
    given = _get(item, "news")
    if _is_news_block(given):      # 이미 블록 형태
        return {"events": symbol_news_events(symbol, given.get("events"), now_ms),
                "market_tone": _num(given.get("market_tone")),
                "risk_event_for_symbol": bool(given.get("risk_event_for_symbol"))}
    news_out = _get(item, "news_output", given)
    if news_out is None:
        return None
    return {"events": symbol_news_events(symbol, _get(item, "news_events"), now_ms),
            "market_tone": _num(_get(news_out, "market_tone")),
            "risk_event_for_symbol": _news_risk_for(symbol, news_out)}


def _item_events(symbol: str, item: Any, now_ms: Any) -> list[dict]:
    given = _get(item, "news")
    if _is_news_block(given):
        return symbol_news_events(symbol, given.get("events"), now_ms, limit=None)
    return symbol_news_events(symbol, _get(item, "news_events"), now_ms, limit=None)


def _head_approved(item: Any, now_ms: Any) -> dict:
    cand = _get(item, "candidate")
    sym = str(_get(item, "symbol") or _get(cand, "symbol") or "")
    tech = _get(item, "technical")
    tech_d = None
    if tech is not None:
        tech_d = _dump(tech)
        labels = _get(item, "labels")
        if labels is None:
            labels = _head_labels(_get(item, "feature_pack"))
        tech_d["labels"] = _pick(labels, HEAD_TECH_LABEL_KEYS)
    risk = _get(item, "risk")
    risk_d = None
    if risk is not None:
        risk_d = _dump(risk)
        glob = _get(item, "risk_global")
        risk_d.update({"confidence_floor": _num(_get(glob, "confidence_floor")),
                       "entries_paused": None if glob is None else bool(_get(glob, "entries_paused")),
                       "exposure_cap_pct": _num(_get(glob, "exposure_cap_pct")),
                       "risk_confidence": _num(_get(item, "risk_confidence"))})
    return {"symbol": sym, "candidate": _dump(cand), "technical": tech_d, "flow": _dump(_get(item, "flow")),
            "news": _head_news(sym, item, now_ms), "bull": _dump(_get(item, "bull")),
            "bear": _dump(_get(item, "bear")), "risk": risk_d, "gate": _effective_gate(item)}


def _head_flagged(item: Any) -> dict:
    d = _position_common(item)
    d["risk"] = _dump(_get(item, "risk"))
    d["flag_reason"] = _clip_text(_get(item, "flag_reason"))
    return {k: d[k] for k in FLAGGED_KEYS}


def _veto_stage(stage_s: str | None) -> str | None:
    """원시 stage → ``VETO_STAGES`` 열거값. 알 수 없는 값은 접두/키워드로 분류하고 끝내 모르면 'g3'."""
    if stage_s is None:
        return None
    low = stage_s.strip().lower()
    if low in _STAGE_ALIASES:
        return _STAGE_ALIASES[low]
    if "bear" in low or low.startswith("g2"):
        out = "bear_decline"
    elif "veto" in low or low.startswith("risk"):
        out = "risk_veto"
    elif low.startswith("g1"):
        out = "g1"
    else:
        out = "g3"
    log.debug("vetoed stage %r → %s", stage_s, out)
    return out


def _vetoed(item: Any) -> dict:
    sym = str(_get(item, "symbol") or "")
    stage_raw = _get(item, "stage")
    if stage_raw is None:
        stage_raw = _get(item, "dropped_at")
    stage_s = None if stage_raw is None else str(stage_raw)
    stage = _veto_stage(stage_s)
    reason = _get(item, "reason")
    if reason is None:
        reasons = _str_list(_get(item, "reasons"))
        if not reasons:
            for g in reversed(list(_get(item, "gates") or [])):
                if _get(g, "passed") is False:
                    reasons = _str_list(_get(g, "reasons"))
                    break
        reason = "; ".join(reasons) if reasons else None
    return {"symbol": sym, "stage": stage, "reason": _clip_text(reason)}


def build_head(approved_ctx: Iterable[Any] | None, flagged_positions_ctx: Iterable[Any] | None, regime: Any,
               account_pack: Any, sentiment: Any, vetoed_ctx: Iterable[Any] | None,
               hard_limits: Any = None, abstained_channels: Iterable[str] | None = None, *,
               now_ms: int | None = None) -> dict:
    """§20.3a.6.

    - ``approved_ctx`` 항목: ``symbol, candidate, technical, feature_pack``(또는 ``labels``)``, flow, bull, bear,
      risk``(RiskCandidateVerdict)``, risk_global``(RiskGlobal)``, risk_confidence``(RiskOutput.confidence)``,
      gate``(또는 ``gates``)`` + 뉴스(``news_output``(NewsOutput|None) + ``news_events``, 또는 ``news`` 블록).
    - ``flagged_positions_ctx`` 항목: risk 포지션과 같은 입력 + ``risk``(RiskPositionView)·``flag_reason``.
    - ``vetoed_ctx`` 항목: ``symbol`` + ``stage``(또는 ``dropped_at``) + ``reason``/``reasons``/``gates``.
    - ``sentiment``: SentimentComposite(또는 ``{"composite": ...}``). ``event_signal`` 은 코드가 계산한다.
    - ``hard_limits`` 인자는 SPEC §20.3 시그니처 호환용이며 무시된다(상수 사용). 목록을 넣으면
      ``abstained_channels`` 로 본다(7-인자 호출 호환).
    """
    hl_arg = hard_limits
    if abstained_channels is None and isinstance(hl_arg, (list, tuple, set, frozenset)):
        abstained_channels, hl_arg = hl_arg, None
    if isinstance(hl_arg, Mapping) and dict(hl_arg) != HARD_LIMITS_PAYLOAD:
        log.warning("build_head: 전달된 hard_limits 무시(상수 사용)")
    approved_items = list(approved_ctx or [])
    flagged_items = list(flagged_positions_ctx or [])
    approved = [_head_approved(it, now_ms) for it in approved_items]
    flagged = [_head_flagged(it) for it in flagged_items]

    signals: dict[str, dict] = {}
    for it, row in [*zip(approved_items, approved), *zip(flagged_items, flagged)]:
        sym = row["symbol"]
        if not sym or sym in signals:
            continue
        es = event_signal(_item_events(sym, it, now_ms))
        tech = _get(it, "technical")
        signals[sym] = {"score": es["score"], "n_events": es["n_events"],
                        "combined_score": combined_score(_get(tech, "score"), es["score"])}

    comp = sentiment
    if isinstance(sentiment, Mapping) and "composite" in sentiment:
        comp = sentiment.get("composite")
    return _finalize({
        "role": "head", "approved": approved, "flagged_positions": flagged, "regime": _dump(regime),
        "account_pack": normalize_account_pack(account_pack),
        "sentiment": {"composite": _dump(comp), "event_signal": dict(sorted(signals.items()))},
        "vetoed": [_vetoed(v) for v in (vetoed_ctx or [])], "hard_limits": dict(HARD_LIMITS_PAYLOAD),
        "abstained_channels": sorted(set(_str_list(abstained_channels))),
    })


# ======================================================================= journal (journal.py 위임)
def build_journal_post_trade(trade: Any, thesis: Any, thesis_history: Any, verdicts: Any,
                             news_risk_after_entry: Sequence[str] | None = None) -> dict:
    """§20.3a.7 post_trade — ``journal.build_journal_post_trade`` 에 위임(키 계약은 journal.py 가 소유)."""
    from tradingbot import journal

    return _finalize(journal.build_journal_post_trade(trade, thesis, thesis_history, verdicts,
                                                      news_risk_after_entry))


def build_journal_daily(metrics_table: Any, closed_count: int) -> dict:
    """§20.3a.7 daily — ``journal.build_journal_daily`` 에 위임."""
    from tradingbot import journal

    return _finalize(journal.build_journal_daily(metrics_table, closed_count))
