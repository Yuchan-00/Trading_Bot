"""``control/STOP``·``control/CLEAR`` 파일 읽기와 상태 판정 — 사이클·``status``·대시보드가 같은 규칙을 쓴다.

- ``read_control_file``: ``{"reason", "ts", "mtime_ms"}`` (+ 읽지 못했으면 ``"unreadable": True``). 없으면 None.
  ``ts`` 는 JSON 의 ``ts``(유한한 수 — 숫자 문자열 포함), 없으면 파일 수정 시각(ms). ``mtime_ms`` 는 늘 수정 시각.
- 봇은 처리한 파일의 ``ts`` 와 ``mtime_ms`` 를 ``BotState.control_applied_ts`` 에 ``kind`` / ``kind_mtime`` 로 남기고,
  파일을 지우면 그 표시도 지운다. 남은 파일(지우지 못함)은 읽히면 ``ts`` 로, 읽지 못하면 수정 시각으로 알아본다 —
  읽기가 막힐 때마다 '새 요청' 으로 보여 같은 STOP 이 다시 적용되거나 옛 CLEAR 가 킬스위치를 끄지 않게.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

ControlState = Literal["pending", "handled", "unreadable"]


def _finite(x: Any) -> bool:
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def mtime_key(kind: str) -> str:
    return f"{kind}_mtime"


def read_control_file(path: Path | str) -> dict | None:
    """control 파일 내용. 없으면 None. JSON 이 아니면 본문을 사유로."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        mtime_ms: int | None = p.stat().st_mtime_ns // 1_000_000
    except OSError:
        mtime_ms = None
    unreadable = False
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None                      # 확인과 읽기 사이에 지워짐
    except OSError:
        raw, unreadable = "", True       # 다른 프로그램이 읽기까지 막고 열고 있음(Windows)
    except ValueError:
        raw = ""
    data: dict = {}
    if raw:
        try:
            obj = json.loads(raw)
            data = dict(obj) if isinstance(obj, Mapping) else {"reason": str(obj)}
        except ValueError:
            data = {"reason": raw[:500]}
    ts = data.get("ts")
    if not _finite(ts):
        ts = mtime_ms if mtime_ms is not None else 0
    reason = data.get("reason")
    out: dict = {"reason": str(reason)[:500] if reason else None, "ts": int(float(ts)), "mtime_ms": mtime_ms}
    if unreadable:
        out["unreadable"] = True
    return out


def is_handled(kind: str, info: Mapping[str, Any] | None, applied: Mapping[str, int] | None) -> bool:
    """이미 처리했지만 지우지 못하고 남은 파일인가(읽혔으면 내용 ts, 아니면 수정 시각으로 비교)."""
    if not info or not applied:
        return False
    if not info.get("unreadable") and applied.get(kind) is not None and info.get("ts") == applied.get(kind):
        return True
    m = applied.get(mtime_key(kind))
    return m is not None and info.get("mtime_ms") == m


def file_state(kind: str, info: Mapping[str, Any] | None, applied: Mapping[str, int] | None) -> ControlState | None:
    """None(파일 없음) / ``handled``(처리했지만 남음) / ``unreadable``(읽지 못한 새 요청) / ``pending``(다음 사이클에 처리)."""
    if info is None:
        return None
    if is_handled(kind, info, applied):
        return "handled"
    return "unreadable" if info.get("unreadable") else "pending"
