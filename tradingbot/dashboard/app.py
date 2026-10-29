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
  토큰 파일은 처음부터 0600 으로 생성한다(``os.open``). 권한을 좁힐 수 없으면 경고를 남긴다.
- **Host 검증**: 루프백 이름과 ``DASHBOARD_HOST`` 외의 Host 헤더는 400 (DNS 리바인딩 방어).
- 응답 JSON은 결정적(``sort_keys=True, ensure_ascii=False``).
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tradingbot import risk
from tradingbot import fsutil
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
COST_LEDGER_PENDING_FILENAME = "cost_ledger.pending.jsonl"
BACKTEST_DIRNAME = "backtest_results"
CONTROL_DIRNAME = "control"
TOKEN_FILENAME = "dashboard_token"
POSIX_PERMISSIONS = os.name != "nt"   # Windows 는 POSIX 권한 비트가 의미 없음(ACL 사용)
STOP_FILENAME = "STOP"
CLEAR_FILENAME = "CLEAR"

_DATE_RE = re.compile(r"^\d{8}$")
_CYCLE_FILE_RE = re.compile(r"^cycle_(\d+)\.json$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f  ]")
_MAX_LIMIT = 1000

#: 파일 읽기에서 삼키는 예외. ``read_text(encoding="utf-8")``의 UnicodeDecodeError는 ValueError 하위다.
_READ_ERRORS = (OSError, ValueError)

#: risk.drawdown_level 어휘 -> 대시보드 어휘
_RISK_LEVEL_TO_DASH = {"normal": "none", "caution": "caution", "block": "block", "stop_all": "stop_all"}


def _clean_text(text: str, max_len: int) -> str:
    """사용자 입력(reason)의 개행·제어문자를 공백으로 바꾸고 길이를 자른다 (로그 주입 방지)."""
    return _CONTROL_CHARS_RE.sub(" ", text)[:max_len].strip()


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
        # 잘린 멀티바이트(크래시·디스크 풀)가 있어도 나머지 줄은 살린다.
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except _READ_ERRORS as e:
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
    """낙폭(소수)을 사다리 단계 문자열로. ``none``/``caution``/``block``/``stop_all``.

    봇의 게이트(``risk.drawdown_level``)와 같은 경계·허용오차를 쓴다: 센트 단위로 반올림된 자본에서
    낙폭이 0.0599999999999999처럼 경계 바로 아래로 떨어져도 봇과 같은 단계를 보여준다.
    """
    if not math.isfinite(drawdown_pct):
        return "stop_all"
    # peak=1, equity=1-dd 로 환산하면 risk 와 정확히 같은 비교가 된다.
    return _RISK_LEVEL_TO_DASH[risk.drawdown_level(1.0 - float(drawdown_pct), 1.0)]


def positions_value(state: BotState) -> float:
    """보유 포지션 평가액 합(마크 없으면 진입가)."""
    total = 0.0
    for pos in state.positions.values():
        mark = pos.last_mark if pos.last_mark is not None else pos.entry_price
        total += float(pos.base_amount) * float(mark)
    return total


def compute_equity(state: BotState) -> float:
    """paper 모드의 자본: 현금(paper_quote_balance) + 포지션 평가액.

    testnet/live 에서는 현금이 거래소에 있어 ``paper_quote_balance``가 0이므로 이 값은 자본이 아니다.
    그 경우 ``summarize_state(..., equity=...)``로 사이클 보고서의 자본을 넘긴다.
    """
    return float(state.paper_quote_balance) + positions_value(state)


def summarize_state(state: BotState, now_ms: int | None = None, equity: float | None = None) -> dict:
    """헤더에 쓰는 요약값. 금액은 USDT, 비율은 소수.

    - paper: 자본 = 현금 + 포지션 평가액 (``equity`` 인자는 무시).
    - testnet/live: 현금은 거래소에 있어 파일만으로는 알 수 없다. ``equity``(최신 사이클 보고서의 자본)가 주어지면
      그것을 쓰고, 없으면 자본·현금·낙폭은 ``None``, 낙폭 단계는 ``"unknown"``. 포지션 평가액을 자본으로
      내놓아 가짜 stop_all 을 표시하지 않는다.
    - 당일 손익 = 자본 - 당일 시작자본 (reports.daily_summary 와 같은 정의; 전날 미실현을 이중 계산하지 않음).
      자본을 모르거나 시작자본이 없으면 실현손익만.
    """
    now_ms = _now_ms() if now_ms is None else now_ms
    pos_value = positions_value(state)
    if state.mode == "paper":
        cash: float | None = float(state.paper_quote_balance)
        equity = cash + pos_value
    else:
        cash = None
        equity = float(equity) if equity is not None and math.isfinite(float(equity)) else None

    peak_state = float(state.peak_equity) if state.peak_equity > 0 else 0.0
    if equity is not None:
        peak: float | None = max(peak_state, equity)
        dd: float | None = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
        level = _RISK_LEVEL_TO_DASH[risk.drawdown_level(equity, peak)] if peak > 0 else "none"
    else:
        peak = peak_state if peak_state > 0 else None
        dd = None
        level = "unknown"

    start = float(state.daily.start_equity)
    realized = float(state.daily.realized_pnl)
    unrealized = sum(
        float(p.base_amount) * ((p.last_mark if p.last_mark is not None else p.entry_price) - p.entry_price)
        for p in state.positions.values()
    )
    if equity is not None and start > 0:
        daily_pnl = equity - start
    else:
        daily_pnl = realized
    llm_paused = state.llm_paused_until_ts is not None and state.llm_paused_until_ts > now_ms
    return {
        "mode": state.mode,
        "cycle": state.cycle,
        "equity": equity,
        "cash": cash,
        "positions_value": pos_value,
        "peak_equity": peak,
        "drawdown_pct": dd,
        "drawdown_level": level,
        "daily_date": state.daily.date,
        "daily_start_equity": start,
        "daily_realized_pnl": realized,
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


# ---------------------------------------------------------------- Host 허용 목록
#: 루프백 이름. ``testserver`` 는 starlette/fastapi ``TestClient`` 의 기본 Host 다(공개 DNS 로 등록될 수 없는
#: 단일 레이블 이름이라 리바인딩 공격자가 쓸 수 없다).
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "testserver")
_WILDCARD_BIND = ("0.0.0.0", "::", "[::]", "*", "")


def _allowed_hosts(settings: Settings) -> list[str]:
    """대시보드가 받아들일 Host 헤더 목록 (포트는 TrustedHostMiddleware 가 떼고 비교한다).

    ``DASHBOARD_HOST`` 가 와일드카드(0.0.0.0/::)면 사용자가 일부러 네트워크에 노출한 것이고 실제 접속
    주소를 알 수 없으므로 Host 검증을 끈다(경고 로그). 그 외에는 루프백 + 설정된 호스트만 허용한다.
    """
    host = (getattr(settings, "dashboard_host", "") or "").strip()
    if host in _WILDCARD_BIND:
        log.warning("DASHBOARD_HOST=%r (와일드카드 바인딩) — Host 헤더 검증을 하지 않습니다", host)
        return ["*"]
    allowed = list(_LOOPBACK_HOSTS)
    bare = host.strip("[]")
    for h in (bare, f"[{bare}]" if ":" in bare else ""):
        if h and h not in allowed:
            allowed.append(h)
    return allowed


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

    #: STOP/CLEAR 쓰기 직렬화 (두 엔드포인트는 스레드풀에서 동시에 실행될 수 있다)
    control_lock = threading.Lock()

    def _read_token_file() -> str | None:
        """토큰 파일 내용(strip). 없거나·비었거나·읽기/디코드 실패면 None."""
        try:
            if token_path.is_file():
                return token_path.read_text(encoding="utf-8").strip() or None
        except _READ_ERRORS as e:
            log.warning("토큰 파일 읽기 실패 %s: %s", token_path, type(e).__name__)
        return None

    def _ensure_token_private() -> None:
        """기존 토큰 파일이 group/other 접근 가능하면 0600 으로 좁힌다. 실패하면 경고를 남긴다.

        POSIX 권한 비트가 없는 Windows 에서는 아무것도 하지 않는다(``chmod`` 는 읽기 전용 비트만 바꾸고
        ``st_mode`` 는 항상 0o666 이라 매번 헛된 경고만 남는다). Windows 는 사용자 프로필 ACL 로 보호된다.
        """
        if not POSIX_PERMISSIONS:
            return
        try:
            mode = stat.S_IMODE(token_path.stat().st_mode)
        except OSError:
            return
        if mode & 0o077 == 0:
            return
        try:
            token_path.chmod(0o600)
            log.warning("대시보드 토큰 파일 권한 %s -> 0600 으로 좁힘: %s", oct(mode), token_path)
        except OSError as e:
            log.warning("대시보드 토큰 파일 권한(%s)을 좁히지 못함 %s: %s — 다른 사용자가 읽을 수 있음",
                        oct(mode), token_path, type(e).__name__)

    # 토큰 준비 (파일 권한은 소유자만 읽기). 비었거나 깨진(UTF-8 아님) 파일은 새로 만든다.
    # 파일은 처음부터 0600 으로 *생성*한다(os.open). umask 권한으로 만든 뒤 chmod 하면 그 사이에
    # 토큰이 group/other 에게 읽힐 수 있다(sysrev SEC-1/SEC-2).
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        if _read_token_file() is None:
            token = secrets.token_urlsafe(24)
            try:
                token_path.unlink()  # 비었거나 깨진 기존 파일: 옛 권한을 물려받지 않도록 지운다
            except FileNotFoundError:
                pass
            created = False
            try:
                fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
                created = True
            except FileExistsError:
                pass  # 다른 프로세스가 방금 만들었다 → 그 토큰을 쓴다
            if created:
                # 내용은 0600 으로 이미 존재하는 빈 파일에 쓴다(O_TRUNC 는 권한을 바꾸지 않는다).
                token_path.write_text(token + "\n", encoding="utf-8")
                log.info("대시보드 토큰 생성: %s (%s)", token_path, mask_secret(token))
        _ensure_token_private()
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

    # Host 헤더 검증 (DNS 리바인딩 방어, sysrev SEC-3). 루프백 바인딩만으로는 악성 페이지가
    # attacker.example → 127.0.0.1 로 리바인딩해 같은 출처가 되는 것을 막지 못한다.
    allowed_hosts = _allowed_hosts(settings)
    app.state.allowed_hosts = allowed_hosts
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

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
        return _read_token_file()

    def _latest_cycle_equity() -> float | None:
        """최신 사이클 보고서의 ``equity`` (testnet/live 자본 출처). 없거나 숫자가 아니면 None."""
        files = _list_cycle_files()
        if not files:
            return None
        payload = _read_json(files[0][2])
        if not isinstance(payload, dict):
            return None
        eq = payload.get("equity")
        if isinstance(eq, bool) or not isinstance(eq, (int, float)):
            return None
        return float(eq) if math.isfinite(float(eq)) else None

    def _write_control_file(target: Path, payload: str) -> None:
        """tmp 쓰기 -> os.replace. tmp 이름은 pid+난수로 요청마다 다르다."""
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(payload + "\n", encoding="utf-8")
            fsutil.replace(tmp, target)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

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
        equity = _latest_cycle_equity() if state.mode != "paper" else None
        return {
            "exists": True,
            "state": dumped,
            "summary": summarize_state(state, equity=equity),
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
        # 원장이 다른 프로그램에 잠겼을 때 봇이 쓰는 보조 파일(llm/cost.py)도 더한다
        records += _read_jsonl(data_dir / COST_LEDGER_PENDING_FILENAME)
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
            reason = _clean_text(str(body.get("reason") or ""), 500)
        reason = reason or "dashboard"
        ts = _now_ms()
        payload = json.dumps({"reason": reason, "ts": ts}, sort_keys=True, ensure_ascii=False)
        try:
            with control_lock:
                control_dir.mkdir(parents=True, exist_ok=True)
                _write_control_file(stop_path, payload)
        except OSError as e:
            log.error("STOP 파일 쓰기 실패: %s", e)
            raise HTTPException(status_code=500, detail="STOP 파일을 쓸 수 없습니다") from e
        try:
            with control_lock:
                fsutil.unlink(clear_path)      # 못 지워도 괜찮다 — 방금 쓴(더 새) STOP 이 이긴다
        except OSError as e:
            log.warning("대기 중 CLEAR 삭제 실패(%s) — 더 새 STOP 이 우선합니다", type(e).__name__)
        log.warning("대시보드에서 STOP 요청: %r", reason[:120])
        return {"ok": True, "reason": reason, "ts": ts, "path": str(stop_path)}

    @app.post("/api/kill/clear")
    def post_kill_clear(
        body: dict | None = Body(default=None),
        x_dashboard_token: str | None = Header(default=None, alias="X-Dashboard-Token"),
    ) -> dict:
        _require_token(x_dashboard_token)
        reason = ""
        if isinstance(body, dict):
            reason = _clean_text(str(body.get("reason") or ""), 500)
        reason = reason or "dashboard"
        ts = _now_ms()
        removed = False
        try:
            with control_lock:
                control_dir.mkdir(parents=True, exist_ok=True)
                if stop_path.exists():
                    try:
                        fsutil.unlink(stop_path)
                        removed = True
                    except OSError as e:
                        # 못 지워도 CLEAR 는 쓴다 — 사이클은 CLEAR 보다 오래된 STOP 을 적용하지 않고 지운다
                        log.warning("대기 중 STOP 삭제 실패(%s) — 더 새 CLEAR 를 기록합니다", type(e).__name__)
                payload = json.dumps({"reason": reason, "ts": ts}, sort_keys=True, ensure_ascii=False)
                _write_control_file(clear_path, payload)
        except OSError as e:
            log.error("CLEAR 파일 쓰기 실패: %s", e)
            raise HTTPException(status_code=500, detail="CLEAR 파일을 쓸 수 없습니다") from e
        log.warning("대시보드에서 킬스위치 해제 요청: %r (STOP 제거=%s)", reason[:120], removed)
        return {"ok": True, "reason": reason, "ts": ts, "stop_removed": removed, "path": str(clear_path)}

    return app
