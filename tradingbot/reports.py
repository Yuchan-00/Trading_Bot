"""리포트·결정 로그·거래 일지 (SPEC §12).

모두 파일 기반이며 네트워크·LLM 없이 동작한다. JSON은 항상 ``sort_keys=True, ensure_ascii=False``로
직렬화해 같은 입력이면 같은 바이트가 나온다(결정성). 비밀값은 여기에 들어오면 안 되고(호출자 책임),
이 모듈은 어떤 설정값도 읽지 않는다.

파일
- ``logs/decisions.jsonl``            사이클별 에이전트/리스크 결정 한 줄 JSON (``append_decision``)
- ``logs/cycles/<YYYYMMDD>/cycle_<n>.json`` + ``.md``   사이클 보고서 (``write_cycle_report``)
- ``logs/journal.jsonl``              청산 거래 + 사후 리뷰 (``append_journal``)
- ``daily_summary()``                 날짜 롤오버 시 텔레그램으로 보낼 ``TradeEvent(kind="daily_summary")``

``write_cycle_report``의 payload는 자유 형식 dict이지만, Markdown 요약은 다음 키를 알면 읽는다(없으면 생략):
``ts``(epoch ms), ``mode``, ``equity``, ``decisions``(list[dict: symbol, action, confidence, reason|thesis]),
``exits``(list[dict: symbol, reason, pnl_quote, pnl_pct]), ``llm_calls``, ``llm_cost_usd``
(또는 ``llm: {calls, cost_usd}``), ``errors``(list[str]), ``skipped``(list[str]).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import BotState, ClosedTrade, TradeEvent

log = logging.getLogger("tradingbot.reports")

DECISIONS_FILENAME = "decisions.jsonl"
JOURNAL_FILENAME = "journal.jsonl"
CYCLES_DIRNAME = "cycles"


# ---------------------------------------------------------------- JSON 도우미
def _json_default(obj: Any) -> Any:
    """json.dumps가 모르는 타입을 결정적으로 바꾼다."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    # 알 수 없는 객체: 클래스가 __str__을 직접 정의했으면 그 문자열을 쓰고, 아니면 repr(SDK 클라이언트 등은
    # repr에 API 키가 들어갈 수 있음 — SPEC §0)을 그대로 쓰지 않고 타입 이름만 남긴다.
    if type(obj).__str__ is not object.__str__:
        return str(obj)
    return f"<{type(obj).__name__}>"


def dumps(obj: Any, *, indent: int | None = None) -> str:
    """결정적 JSON 문자열. NaN/inf는 null(표준 JSON 유지)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=indent, default=_json_default, allow_nan=False)


def _sanitize(obj: Any) -> Any:
    """allow_nan=False가 거부하는 NaN/inf float를 미리 None으로 바꾼다 (중첩 포함)."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, Mapping):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, BaseModel):
        return _sanitize(obj.model_dump(mode="json"))
    return obj


def _append_line(path: Path, record: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = dumps(_sanitize(record))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _write_atomic(path: Path, text: str) -> None:
    """tmp 파일에 쓰고 os.replace. 부분적으로 써진 파일이 남지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - 정리 실패는 무시
                pass


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    """jsonl을 읽는다(대시보드/테스트용). 깨진 줄은 건너뛴다. ``limit``이면 마지막 n줄."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("깨진 jsonl 줄 건너뜀: %s", path)
                continue
            if isinstance(obj, dict):
                out.append(obj)
    if limit is not None and limit >= 0:
        out = out[-limit:] if limit else []
    return out


# ---------------------------------------------------------------- 시각 도우미
def _now_ms() -> int:
    return int(time.time() * 1000)


def _date_from_payload(payload: Mapping[str, Any]) -> str:
    """payload의 ts(epoch ms) 또는 date로 YYYYMMDD를 만든다. 없으면 오늘(UTC)."""
    ts = payload.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts) and ts > 0:
        seconds = ts / 1000.0 if ts > 10_000_000_000 else float(ts)  # ms/초 둘 다 허용
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y%m%d")
    d = payload.get("date")
    if isinstance(d, str):
        digits = d.replace("-", "").strip()
        if len(digits) >= 8 and digits[:8].isdigit():
            return digits[:8]
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d")


# ---------------------------------------------------------------- 포맷 도우미
def _fmt_money(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(f):
        return "-"
    return f"{f:,.2f}"


def _fmt_pct(v: Any, *, ratio: bool = True) -> str:
    """ratio=True면 0.0123 -> +1.23%."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(f):
        return "-"
    if ratio:
        f *= 100.0
    return f"{f:+.2f}%"


def _fmt_conf(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{f:.2f}" if math.isfinite(f) else "-"


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, Mapping):
        return list(v.values())
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(d, BaseModel):
        d = d.model_dump(mode="json")
    if not isinstance(d, Mapping):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _clip(text: Any, n: int = 120) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 공개 API
def append_decision(path: Path, record: dict) -> None:
    """``logs/decisions.jsonl``에 결정 한 건을 한 줄 JSON으로 추가한다(sort_keys).

    ``ts``가 없으면 현재 epoch ms를 채운다. 디렉터리는 만들어 준다.
    """
    if not isinstance(record, Mapping):
        raise TypeError("record는 dict여야 함")
    rec = dict(record)
    rec.setdefault("ts", _now_ms())
    _append_line(Path(path), rec)
    log.debug("결정 기록: %s cycle=%s symbol=%s", path, rec.get("cycle"), rec.get("symbol"))


def render_cycle_markdown(cycle: int, payload: Mapping[str, Any]) -> str:
    """사이클 보고서의 짧은 한국어 Markdown 요약(사람이 읽는 용도)."""
    ts = payload.get("ts")
    when = "-"
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts) and ts > 0:
        seconds = ts / 1000.0 if ts > 10_000_000_000 else float(ts)
        when = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = payload.get("mode") or "-"
    equity = payload.get("equity")
    decisions = _as_list(payload.get("decisions"))
    exits = _as_list(payload.get("exits"))
    llm = payload.get("llm") if isinstance(payload.get("llm"), Mapping) else {}
    llm_calls = _get(payload, "llm_calls", default=_get(llm, "calls", default=0))
    llm_cost = _get(payload, "llm_cost_usd", default=_get(llm, "cost_usd", default=0.0))
    errors = _as_list(payload.get("errors"))
    skipped = _as_list(payload.get("skipped"))

    lines: list[str] = [f"# 사이클 {cycle} 보고서", ""]
    lines.append(f"- 시각: {when}")
    lines.append(f"- 모드: {mode}")
    lines.append(f"- 자본: {_fmt_money(equity)} USDT")
    daily_pnl = _get(payload, "daily_pnl", "realized_pnl_today")
    if daily_pnl is not None:
        lines.append(f"- 당일 실현손익: {_fmt_money(daily_pnl)} USDT")
    positions = payload.get("positions")
    if isinstance(positions, (Mapping, list, tuple)):
        lines.append(f"- 보유 포지션: {len(positions)}개")
    try:
        calls_i = int(llm_calls or 0)  # int(inf) -> OverflowError, int(nan) -> ValueError
    except (TypeError, ValueError, OverflowError):
        calls_i = 0
    try:
        cost_f = float(llm_cost or 0.0)
        if not math.isfinite(cost_f):
            cost_f = 0.0
    except (TypeError, ValueError, OverflowError):
        cost_f = 0.0
    lines.append(f"- LLM 호출: {calls_i}회 / 비용 ${cost_f:.4f}")
    lines.append("")

    lines.append(f"## 결정 ({len(decisions)})")
    if not decisions:
        lines.append("- 없음")
    for d in decisions:
        sym = _get(d, "symbol", default="?")
        action = _get(d, "action", "decision", default="HOLD")
        conf = _get(d, "confidence")
        reason = _get(d, "reason", "thesis", "rationale", default="")
        extra = []
        if conf is not None:
            extra.append(f"확신도 {_fmt_conf(conf)}")
        executed = _get(d, "executed")
        if executed is not None:
            extra.append("체결" if executed else "미체결")
        risk_notes = _as_list(_get(d, "risk_notes", "risk_reasons"))
        if risk_notes:
            extra.append("리스크: " + "; ".join(_clip(r, 60) for r in risk_notes))
        suffix = f" ({', '.join(extra)})" if extra else ""
        line = f"- {sym}: **{action}**{suffix}"
        if reason:
            line += f" — {_clip(reason)}"
        lines.append(line)
    lines.append("")

    lines.append(f"## 청산 ({len(exits)})")
    if not exits:
        lines.append("- 없음")
    for e in exits:
        sym = _get(e, "symbol", default="?")
        reason = _get(e, "reason", default="-")
        pnl_q = _get(e, "pnl_quote")
        pnl_p = _get(e, "pnl_pct")
        line = f"- {sym}: {reason}"
        if pnl_q is not None or pnl_p is not None:
            line += f" → 손익 {_fmt_money(pnl_q)} USDT ({_fmt_pct(pnl_p)})"
        lines.append(line)

    if skipped:
        lines.append("")
        lines.append(f"## 건너뜀 ({len(skipped)})")
        lines.extend(f"- {_clip(s)}" for s in skipped)
    if errors:
        lines.append("")
        lines.append(f"## 오류 ({len(errors)})")
        lines.extend(f"- {_clip(e, 200)}" for e in errors)
    lines.append("")
    return "\n".join(lines)


def write_cycle_report(dir: Path, cycle: int, payload: dict) -> Path:  # noqa: A002 - SPEC 시그니처
    """``<dir>/<YYYYMMDD>/cycle_<n>.json``(결정적 JSON)과 같은 이름의 ``.md`` 요약을 쓴다.

    날짜는 ``payload["ts"]``(epoch ms) 기준 UTC, 없으면 오늘. 반환값은 JSON 경로.
    ``payload["cycle"]``이 없으면 ``cycle``을 채워 넣는다. 원자적으로 쓴다(tmp → replace).
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload는 dict여야 함")
    if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle < 0:
        raise ValueError("cycle은 0 이상의 정수여야 함")
    data = dict(payload)
    data.setdefault("cycle", cycle)
    day = _date_from_payload(data)
    out_dir = Path(dir) / day
    json_path = out_dir / f"cycle_{cycle}.json"
    md_path = out_dir / f"cycle_{cycle}.md"
    _write_atomic(json_path, dumps(_sanitize(data), indent=2) + "\n")
    _write_atomic(md_path, render_cycle_markdown(cycle, data))
    log.info("사이클 보고서 저장: %s", json_path)
    return json_path


def append_journal(path: Path, trade: ClosedTrade, review: dict | None) -> None:
    """``logs/journal.jsonl``에 청산 거래와 (있으면) 사후 리뷰를 한 줄로 추가한다."""
    if not isinstance(trade, ClosedTrade):
        raise TypeError("trade는 ClosedTrade여야 함")
    if review is not None and not isinstance(review, Mapping):
        raise TypeError("review는 dict 또는 None이어야 함")
    rec = {
        "ts": trade.closed_ts,
        "symbol": trade.symbol,
        "reason": trade.reason,
        "pnl_quote": trade.pnl_quote,
        "pnl_pct": trade.pnl_pct,
        "trade": trade.model_dump(mode="json"),
        "review": dict(review) if review is not None else None,
    }
    _append_line(Path(path), rec)
    log.debug("거래 일지 기록: %s %s %s", path, trade.symbol, trade.reason)


def daily_summary(state: BotState, equity: float, ledger_spent: float) -> TradeEvent:
    """하루 통계를 ``TradeEvent(kind="daily_summary")``로 만든다 (notify/reports 공용).

    ``pnl_quote``=당일 실현손익, ``pnl_pct``=시작자본 대비, ``extra``에 거래 수·승/패·LLM 비용 등을 담는다.
    ``ledger_spent``는 CostLedger가 집계한 당일 LLM 비용(USD)이며 state.daily.llm_cost_usd 보다 우선한다.
    """
    d = state.daily
    start = float(d.start_equity)
    realized = float(d.realized_pnl)
    pnl_pct = realized / start if start > 0 and math.isfinite(start) else 0.0
    eq = float(equity)
    equity_change_pct = (eq - start) / start if start > 0 and math.isfinite(start) and math.isfinite(eq) else 0.0
    try:
        cost = float(ledger_spent)
        if not math.isfinite(cost):
            cost = float(d.llm_cost_usd)
    except (TypeError, ValueError):
        cost = float(d.llm_cost_usd)
    win_rate = (d.wins / d.trades) if d.trades > 0 else None
    unrealized = sum(
        (p.last_mark - p.entry_price) * p.base_amount
        for p in state.positions.values()
        if p.last_mark is not None
    )
    return TradeEvent(
        kind="daily_summary",
        ts=_now_ms(),
        mode=state.mode,
        reason=f"{d.date} 일일 요약",
        pnl_quote=realized,
        pnl_pct=pnl_pct,
        equity=eq,
        extra={
            "date": d.date,
            "start_equity": start,
            "equity_change_pct": equity_change_pct,
            "trades": int(d.trades),
            "wins": int(d.wins),
            "losses": int(d.losses),
            "win_rate": win_rate,
            "consecutive_losses": int(d.consecutive_losses),
            "llm_cost_usd": cost,  # notify._format_daily_summary 가 읽는 키 (DailyStats 필드명과 동일)
            "cost_usd": cost,  # 하위 호환 별칭
            "open_positions": len(state.positions),
            "unrealized_pnl": unrealized,
            "peak_equity": float(state.peak_equity),
            "kill_switch": bool(state.kill_switch),
            "cycle": int(state.cycle),
        },
    )
