"""대시보드 FastAPI 앱 (SPEC §14).

설계 원칙
- **읽기 전용·파일 기반**: 봇 프로세스의 메모리에 손대지 않고 ``data_dir`` 아래 파일만 읽는다.
  (``state.json``, ``logs/decisions.jsonl``, ``logs/journal.jsonl``, ``logs/cycles/<YYYYMMDD>/cycle_<n>.json``,
  ``cost_ledger.jsonl``, ``backtest_results/*.json``)
- **없는 파일은 빈 응답**: 봇이 아직 한 번도 돌지 않았어도 어떤 엔드포인트도 500을 내지 않는다.
- **쓰기는 control/ 파일 두 개뿐**: ``POST /api/kill``이 ``control/STOP``을, ``POST /api/kill/clear``가
  ``control/CLEAR``를 만든다. 실제 kill_switch 반영은 ``cycle``이 다음 사이클에 파일을 읽어서 한다.
- **토큰**: 두 쓰기 요청은 ``X-Dashboard-Token`` 헤더가 ``control/dashboard_token`` 내용과 같아야 한다.
  ``create_app``이 파일이 없으면 ``secrets.token_urlsafe(24)``로 만든다. 토큰은 로그에 가린 형태로만 나온다.
- 응답 JSON은 결정적(``sort_keys=True, ensure_ascii=False``).
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from tradingbot.config import HardLimits, Settings, mask_secret
from tradingbot.models import BotState

log = logging.getLogger("tradingbot.dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

STATE_FILENAME = "state.json"
LOGS_DIRNAME = "logs"
DECISIONS_FILENAME = "decisions.jsonl"
JOURNAL_FILENAME = "journal.jsonl"
CYCLES_DIRNAME = "cycles"
COST_LEDGER_FILENAME = "cost_ledger.jsonl"
BACKTEST_DIRNAME = "backtest_results"
CONTROL_DIRNAME = "control"
TOKEN_FILENAME = "dashboard_token"
STOP_FILENAME = "STOP"
CLEAR_FILENAME = "CLEAR"

_DATE_RE = re.compile(r"^\d{8}$")
_CYCLE_FILE_RE = re.compile(r"^cycle_(\d+)\.json$")
_MAX_LIMIT = 1000


# ---------------------------------------------------------------- JSON 응답(결정적)
class SortedJSONResponse(JSONResponse):
    """키 정렬 + 한글 그대로. NaN/inf는 null로 바꾼다(표준 JSON 유지)."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            _sanitize(content), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")


def _sanitize(obj: Any) -> Any:
    """float NaN/inf → None, Path → str, 그 외 JSON 가능한 형태로."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ---------------------------------------------------------------- 파일 읽기 도우미 (예외를 삼킨다)
def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_json(path: Path) -> Any | None:
    """JSON 파일을 읽는다. 없거나 깨졌으면 None."""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("JSON 읽기 실패 %s: %s", path, e)
        return None


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    """jsonl의 마지막 ``limit``줄(파일 순서 유지). 깨진 줄·dict가 아닌 줄은 건너뛴다."""
    try:
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning("jsonl 읽기 실패 %s: %s", path, e)
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    if limit is not None and limit >= 0:
        out = out[-limit:] if limit > 0 else []
    return out


def _clamp_limit(limit: int) -> int:
    return max(0, min(int(limit), _MAX_LIMIT))


# ---------------------------------------------------------------- 상태 파생값
def drawdown_level(drawdown_pct: float) -> str:
    """낙폭(소수)을 사다리 단계 문자열로. ``none``/``caution``/``block``/``stop_all``."""
    if drawdown_pct >= HardLimits.DRAWDOWN_STOP_ALL:
        return "stop_all"
    if drawdown_pct >= HardLimits.DRAWDOWN_BLOCK:
        return "block"
    if drawdown_pct >= HardLimits.DRAWDOWN_CAUTION:
        return "caution"
    return "none"


def compute_equity(state: BotState) -> float:
    """현금 + 포지션 평가액(마크 없으면 진입가)."""
    total = float(state.paper_quote_balance)
    for pos in state.positions.values():
        mark = pos.last_mark if pos.last_mark is not None else pos.entry_price
        total += float(pos.base_amount) * float(mark)
    return total


def summarize_state(state: BotState, now_ms: int | None = None) -> dict:
    """헤더에 쓰는 요약값. 금액은 USDT, 비율은 소수."""
    now_ms = _now_ms() if now_ms is None else now_ms
    equity = compute_equity(state)
    peak = max(float(state.peak_equity), equity) if state.peak_equity > 0 else equity
    dd = (peak - equity) / peak if peak > 0 else 0.0
    start = float(state.daily.start_equity)
    unrealized = sum(
        float(p.base_amount) * ((p.last_mark if p.last_mark is not None else p.entry_price) - p.entry_price)
        for p in state.positions.values()
    )
    daily_pnl = float(state.daily.realized_pnl) + unrealized
    llm_paused = state.llm_paused_until_ts is not None and state.llm_paused_until_ts > now_ms
    return {
        "mode": state.mode,
        "cycle": state.cycle,
        "equity": equity,
        "cash": float(state.paper_quote_balance),
        "peak_equity": peak,
        "drawdown_pct": max(0.0, dd),
        "drawdown_level": drawdown_level(dd),
        "daily_date": state.daily.date,
        "daily_start_equity": start,
        "daily_realized_pnl": float(state.daily.realized_pnl),
        "daily_unrealized_pnl": unrealized,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": (daily_pnl / start) if start > 0 else 0.0,
        "daily_trades": state.daily.trades,
        "daily_wins": state.daily.wins,
        "daily_losses": state.daily.losses,
        "consecutive_losses": state.daily.consecutive_losses,
        "daily_llm_cost_usd": float(state.daily.llm_cost_usd),
        "positions_count": len(state.positions),
        "max_positions": HardLimits.MAX_POSITIONS,
        "kill_switch": bool(state.kill_switch),
        "kill_reason": state.kill_reason,
        "llm_paused": llm_paused,
        "llm_paused_until_ts": state.llm_paused_until_ts,
        "llm_pause_reason": state.llm_pause_reason,
        "last_cycle_ts": state.last_cycle_ts,
        "last_cycle_age_s": (
            max(0, (now_ms - state.last_cycle_ts) // 1000) if state.last_cycle_ts is not None else None
        ),
    }


def _position_row(pos, now_ms: int) -> dict:
    mark = pos.last_mark if pos.last_mark is not None else pos.entry_price
    value = float(pos.base_amount) * float(mark)
    unrealized = value - float(pos.quote_spent)
    row = pos.model_dump()
    row.update({
        "mark": mark,
        "value_quote": value,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_pct": (unrealized / pos.quote_spent) if pos.quote_spent > 0 else 0.0,
        "held_seconds": max(0, (now_ms - pos.opened_ts) // 1000),
    })
    return row


# ---------------------------------------------------------------- 비용 집계
def summarize_costs(records: list[dict], today: str, settings: Settings) -> dict:
    """cost_ledger.jsonl 레코드(``date`` YYYY-MM-DD, ``model``, ``cost_usd`` ...)를 오늘/이달/모델별로 집계."""
    month_prefix = today[:7]
    today_total = 0.0
    month_total = 0.0
    by_model: dict[str, dict] = {}
    by_model_today: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    calls_today = 0
    calls_month = 0

    def _bump(bucket: dict[str, dict], key: str, rec: dict, cost: float) -> None:
        b = bucket.setdefault(key, {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0})
        b["cost_usd"] += cost
        b["calls"] += 1
        for f in ("input_tokens", "cached_tokens", "output_tokens"):
            try:
                b[f] += int(rec.get(f) or 0)
            except (TypeError, ValueError):
                pass

    for rec in records:
        date = str(rec.get("date") or "")
        try:
            cost = float(rec.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(cost):
            continue
        model = str(rec.get("model") or "?")
        role = str(rec.get("role") or "?")
        if date.startswith(month_prefix):
            month_total += cost
            calls_month += 1
            _bump(by_model, model, rec, cost)
            _bump(by_role, role, rec, cost)
        if date == today:
            today_total += cost
            calls_today += 1
            _bump(by_model_today, model, rec, cost)

    daily_budget = float(settings.llm_daily_budget_usd)
    monthly_budget = float(settings.llm_monthly_budget_usd)
    return {
        "date": today,
        "month": month_prefix,
        "today_usd": today_total,
        "month_usd": month_total,
        "daily_budget_usd": daily_budget,
        "monthly_budget_usd": monthly_budget,
        "today_budget_pct": (today_total / daily_budget) if daily_budget > 0 else 0.0,
        "month_budget_pct": (month_total / monthly_budget) if monthly_budget > 0 else 0.0,
        "remaining_today_usd": max(0.0, daily_budget - today_total),
        "remaining_month_usd": max(0.0, monthly_budget - month_total),
        "calls_today": calls_today,
        "calls_month": calls_month,
        "by_model": by_model,
        "by_model_today": by_model_today,
        "by_role": by_role,
    }


# ---------------------------------------------------------------- 백테스트 요약
def _backtest_summary(path: Path) -> dict | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    summary: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            summary[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v) and len(v) <= 50:
            summary[k] = v
        elif isinstance(v, dict) and k in ("summary", "metrics", "stress", "monte_carlo", "walk_forward", "buy_and_hold"):
            summary[k] = _sanitize(v)
    if "trades" in data and isinstance(data["trades"], list):
        summary.setdefault("trades_count", len(data["trades"]))
    try:
        mtime = int(path.stat().st_mtime * 1000)
    except OSError:
        mtime = None
    return {"name": path.name, "mtime_ms": mtime, "summary": summary}


# ---------------------------------------------------------------- 앱
def create_app(settings: Settings, data_dir: Path) -> FastAPI:
    """읽기 전용 대시보드 앱을 만든다. ``control/dashboard_token``이 없으면 생성한다."""
    data_dir = Path(data_dir)
    control_dir = data_dir / CONTROL_DIRNAME
    token_path = control_dir / TOKEN_FILENAME
    stop_path = control_dir / STOP_FILENAME
    clear_path = control_dir / CLEAR_FILENAME
    logs_dir = data_dir / LOGS_DIRNAME
    cycles_dir = logs_dir / CYCLES_DIRNAME

    # 토큰 준비 (파일 권한은 소유자만 읽기)
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        if not token_path.is_file() or not token_path.read_text(encoding="utf-8").strip():
            token = secrets.token_urlsafe(24)
            token_path.write_text(token + "\n", encoding="utf-8")
            try:
                token_path.chmod(0o600)
            except OSError:
                pass
            log.info("대시보드 토큰 생성: %s (%s)", token_path, mask_secret(token))
    except OSError as e:
        log.error("control 디렉터리/토큰 준비 실패 %s: %s", control_dir, e)

    app = FastAPI(
        title="Trading Bot Dashboard",
        version="1.0",
        default_response_class=SortedJSONResponse,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.data_dir = data_dir

    # ------------------------------------------------------------ 내부 도우미
    def _load_state() -> BotState | None:
        raw = _read_json(data_dir / STATE_FILENAME)
        if not isinstance(raw, dict):
            return None
        try:
            return BotState.model_validate(raw)
        except Exception as e:  # pydantic ValidationError 등
            log.warning("state.json 검증 실패: %s", type(e).__name__)
            return None

    def _read_token() -> str | None:
        try:
            if token_path.is_file():
                return token_path.read_text(encoding="utf-8").strip() or None
        except OSError as e:
            log.warning("토큰 파일 읽기 실패: %s", e)
        return None

    def _require_token(header_value: str | None) -> None:
        expected = _read_token()
        if expected is None:
            raise HTTPException(status_code=503, detail="대시보드 토큰 파일이 없습니다")
        if not header_value or not hmac.compare_digest(header_value.strip().encode(), expected.encode()):
            log.warning("잘못된 대시보드 토큰 (받은 값 %s)", mask_secret(header_value))
            raise HTTPException(status_code=401, detail="X-Dashboard-Token 불일치")

    def _stop_info() -> dict | None:
        if not stop_path.is_file():
            return None
        data = _read_json(stop_path)
        return data if isinstance(data, dict) else {"reason": None, "ts": None}

    def _list_cycle_files() -> list[tuple[str, int, Path]]:
        """(날짜, n, 경로) 최신순."""
        out: list[tuple[str, int, Path]] = []
        try:
            if not cycles_dir.is_dir():
                return []
            for day in cycles_dir.iterdir():
                if not day.is_dir() or not _DATE_RE.match(day.name):
                    continue
                for f in day.iterdir():
                    m = _CYCLE_FILE_RE.match(f.name)
                    if m and f.is_file():
                        out.append((day.name, int(m.group(1)), f))
        except OSError as e:
            log.warning("cycles 디렉터리 읽기 실패: %s", e)
            return []
        out.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return out

    # ------------------------------------------------------------ 라우트
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        try:
            html = INDEX_HTML.read_text(encoding="utf-8")
        except OSError:
            html = "<!doctype html><title>dashboard</title><p>index.html 없음</p>"
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health() -> dict:
        now = _now_ms()
        state = _load_state()
        stop = _stop_info()
        last = state.last_cycle_ts if state is not None else None
        return {
            "ok": True,
            "ts": now,
            "mode": settings.mode,
            "state_exists": (data_dir / STATE_FILENAME).is_file(),
            "state_valid": state is not None,
            "cycle": state.cycle if state is not None else None,
            "last_cycle_ts": last,
            "last_cycle_age_s": (max(0, (now - last) // 1000) if last is not None else None),
            "cycle_minutes": settings.cycle_minutes,
            "stop_pending": stop is not None,
            "clear_pending": clear_path.is_file(),
            "kill_switch": state.kill_switch if state is not None else None,
            "token_configured": _read_token() is not None,
        }

    @app.get("/api/state")
    def get_state() -> dict:
        state = _load_state()
        if state is None:
            return {"exists": False, "state": None, "summary": None, "stop_pending": _stop_info() is not None}
        dumped = state.model_dump()
        return {
            "exists": True,
            "state": dumped,
            "summary": summarize_state(state),
            "stop_pending": _stop_info() is not None,
        }

    @app.get("/api/positions")
    def get_positions() -> dict:
        state = _load_state()
        if state is None:
            return {"positions": [], "count": 0}
        now = _now_ms()
        rows = [_position_row(p, now) for _, p in sorted(state.positions.items())]
        return {"positions": rows, "count": len(rows), "max_positions": HardLimits.MAX_POSITIONS}

    @app.get("/api/trades")
    def get_trades(limit: int = Query(100, ge=0, le=_MAX_LIMIT)) -> dict:
        state = _load_state()
        if state is None:
            return {"trades": [], "count": 0, "total": 0}
        trades = [t.model_dump() for t in state.closed_trades]
        total = len(trades)
        trades = trades[-limit:] if limit > 0 else []
        trades.reverse()  # 최신 먼저
        return {"trades": trades, "count": len(trades), "total": total}

    @app.get("/api/decisions")
    def get_decisions(limit: int = Query(100, ge=0, le=_MAX_LIMIT)) -> dict:
        rows = _read_jsonl(logs_dir / DECISIONS_FILENAME, _clamp_limit(limit))
        rows.reverse()
        return {"decisions": rows, "count": len(rows)}

    @app.get("/api/journal")
    def get_journal(limit: int = Query(50, ge=0, le=_MAX_LIMIT)) -> dict:
        rows = _read_jsonl(logs_dir / JOURNAL_FILENAME, _clamp_limit(limit))
        rows.reverse()
        return {"journal": rows, "count": len(rows)}

    @app.get("/api/cycles")
    def get_cycles(limit: int = Query(20, ge=0, le=_MAX_LIMIT)) -> dict:
        files = _list_cycle_files()
        total = len(files)
        files = files[:limit] if limit > 0 else []
        items: list[dict] = []
        for date, n, path in files:
            payload = _read_json(path)
            item: dict[str, Any] = {"date": date, "cycle": n, "ts": None, "equity": None, "mode": None,
                                    "decisions": None, "positions": None, "errors": None}
            if isinstance(payload, dict):
                item["ts"] = payload.get("ts")
                item["equity"] = payload.get("equity")
                item["mode"] = payload.get("mode")
                for key in ("decisions", "positions", "errors"):
                    v = payload.get(key)
                    if isinstance(v, (list, dict)):
                        item[key] = len(v)
                    elif isinstance(v, (int, float)):
                        item[key] = v
            items.append(item)
        return {"cycles": items, "count": len(items), "total": total}

    @app.get("/api/cycles/{date}/{n}")
    def get_cycle(date: str, n: int) -> dict:
        if not _DATE_RE.match(date) or n < 0:
            raise HTTPException(status_code=404, detail="사이클 보고서 없음")
        path = cycles_dir / date / f"cycle_{n}.json"
        payload = _read_json(path)
        if payload is None:
            raise HTTPException(status_code=404, detail="사이클 보고서 없음")
        return {"date": date, "cycle": n, "payload": payload}

    @app.get("/api/costs")
    def get_costs() -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        records = _read_jsonl(data_dir / COST_LEDGER_FILENAME)
        return summarize_costs(records, today, settings)

    @app.get("/api/backtests")
    def get_backtests() -> dict:
        bt_dir = data_dir / BACKTEST_DIRNAME
        items: list[dict] = []
        try:
            if bt_dir.is_dir():
                files = sorted((p for p in bt_dir.iterdir() if p.is_file() and p.suffix == ".json"),
                               key=lambda p: p.name, reverse=True)
                for p in files[:50]:
                    s = _backtest_summary(p)
                    if s is not None:
                        items.append(s)
        except OSError as e:
            log.warning("backtest_results 읽기 실패: %s", e)
        return {"backtests": items, "count": len(items)}

    @app.post("/api/kill")
    def post_kill(
        body: dict | None = Body(default=None),
        x_dashboard_token: str | None = Header(default=None, alias="X-Dashboard-Token"),
    ) -> dict:
        _require_token(x_dashboard_token)
        reason = ""
        if isinstance(body, dict):
            reason = str(body.get("reason") or "").strip()
        reason = reason[:500] or "dashboard"
        ts = _now_ms()
        payload = json.dumps({"reason": reason, "ts": ts}, sort_keys=True, ensure_ascii=False)
        try:
            control_dir.mkdir(parents=True, exist_ok=True)
            tmp = stop_path.with_name(stop_path.name + ".tmp")
            tmp.write_text(payload + "\n", encoding="utf-8")
            tmp.replace(stop_path)
            if clear_path.exists():
                clear_path.unlink()
        except OSError as e:
            log.error("STOP 파일 쓰기 실패: %s", e)
            raise HTTPException(status_code=500, detail="STOP 파일을 쓸 수 없습니다") from e
        log.warning("대시보드에서 STOP 요청: %s", reason[:120])
        return {"ok": True, "reason": reason, "ts": ts, "path": str(stop_path)}

    @app.post("/api/kill/clear")
    def post_kill_clear(
        body: dict | None = Body(default=None),
        x_dashboard_token: str | None = Header(default=None, alias="X-Dashboard-Token"),
    ) -> dict:
        _require_token(x_dashboard_token)
        reason = ""
        if isinstance(body, dict):
            reason = str(body.get("reason") or "").strip()
        reason = reason[:500] or "dashboard"
        ts = _now_ms()
        removed = False
        try:
            control_dir.mkdir(parents=True, exist_ok=True)
            if stop_path.exists():
                stop_path.unlink()
                removed = True
            payload = json.dumps({"reason": reason, "ts": ts}, sort_keys=True, ensure_ascii=False)
            tmp = clear_path.with_name(clear_path.name + ".tmp")
            tmp.write_text(payload + "\n", encoding="utf-8")
            tmp.replace(clear_path)
        except OSError as e:
            log.error("CLEAR 파일 쓰기 실패: %s", e)
            raise HTTPException(status_code=500, detail="CLEAR 파일을 쓸 수 없습니다") from e
        log.warning("대시보드에서 킬스위치 해제 요청: %s (STOP 제거=%s)", reason[:120], removed)
        return {"ok": True, "reason": reason, "ts": ts, "stop_removed": removed, "path": str(clear_path)}

    return app
