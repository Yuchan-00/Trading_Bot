"""CLI 진입점 (SPEC §15 ``main.py``, §22 ``main.py run``).

명령
- ``run [--once] [--cycle-minutes N] [--offline-fixture]``  사이클 루프(``cycle_minutes`` 경계 + 5초).
  ``--once`` 는 1회 후 종료. ``--offline-fixture`` 는 ccxt 시세 대신 결정적 합성 시세
  (``tradingbot.testing.FakeMarketData``)를 쓰고 뉴스 조회를 끈다 — 키·네트워크 없이 체험(paper 전용).
  합성 상태가 실제 paper 저널을 오염시키지 않도록 데이터는 ``<DATA_DIR>/offline_fixture/`` 에 따로 쓰고,
  백엔드는 mock 으로 강제하며 텔레그램 전송은 끈다(유료 API 호출·네트워크 0).
  ``AGENT_BACKEND=mock`` 이면 ``MOCK_SCENARIO``(conservative|approve) 로 Mock 응답 시나리오를 고른다.
- ``dashboard``                         로컬 대시보드(FastAPI + uvicorn, 읽기 전용).
- ``backtest [--days 90] [--symbols ...]`` 규칙 전용 기준선 백테스트(LLM 없음).
- ``status``                            상태 요약 출력(네트워크·LLM 없음, 파일만 읽음).
- ``stop --reason ...``                 ``control/STOP`` 작성 → 다음 사이클에 킬스위치.
- ``clear-stop``                        ``control/STOP`` 삭제 + ``control/CLEAR`` 작성 → 다음 사이클에 해제.
- ``notify-test``                       텔레그램 연결 테스트 메시지 1건을 동기 전송(``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID``
                                        만 필요 — 다른 설정·API 키 없이 동작). 성공 0, 전송 실패 1(토큰은 가림), 설정 없음 2.

안전장치
- ``MODE=live`` 이면 ``run`` 시작 시 콘솔에 ``LIVE`` 를 직접 입력해야 진행한다(환경변수로 우회 불가).
  표준입력이 대화형 터미널(TTY)이 아니면(``echo LIVE | ... run`` 등 파이프/리다이렉트) 묻지 않고 거부한다.
- ``bot.lock``(PID) 으로 단일 인스턴스. 기록된 PID 가 살아 있지 않거나, 기록된 호스트가 이 호스트가 아니거나,
  ``ts`` 가 ``max(3 × cycle_minutes × 60, 1800)`` 초보다 오래됐으면 낡은 잠금으로 보고 가져온다.
  루프는 사이클마다, 그리고 사이클 사이 대기 중에도 최대 5분마다 ``ts`` 를 갱신한다.
- SIGINT/SIGTERM/SIGBREAK(Windows Ctrl+Break) → 진행 중 사이클을 마친 뒤 notify(shutdown) + ``notifier.flush()``
  + 잠금 해제. 사이클 사이 대기는 1초 조각으로 나눠 기다린다(Windows CPython ≤3.13 은 ``Event.wait`` 가
  Ctrl+C 로 깨지 않는다).
- 하트비트 notify(kind ``heartbeat``: 자본·당일 손익·보유 포지션 미실현 %·노출 %·오늘 LLM 비용) 4시간마다.
- 사이클 사이 공백이 예정 주기의 2배를 넘으면(PC 절전·콘솔 일시정지) 경보(kind ``alert``, ``cycle_gap``).
- Windows: ``run`` 동안 자동 절전(유휴 타이머)을 막는다(``SetThreadExecutionState``). 로그는 전용 스레드가
  콘솔에 쓴다(콘솔 창 선택 모드가 쓰기를 막아도 사이클은 계속). ``LOG_FILE`` 이면 UTF-8 로그 파일도 남긴다.
- ``status`` 는 ``stop``/``clear-stop`` 처럼 설정 검증이 실패해도(키 없음 등) ``DATA_DIR`` 만 풀어 파일 상태를 출력한다.

출력은 ``out``/``err`` 스트림에 쓴다(테스트 주입). 로그는 ``logging.getLogger("tradingbot.main")``.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from tradingbot import cycle as C
from tradingbot import fsutil
from tradingbot.control import file_state as control_file_state, read_control_file
from tradingbot.config import (
    ConfigError,
    Settings,
    dotenv_location,
    load_settings,
    mask_secret,
    parse_data_dir,
    read_env,
)
from tradingbot.models import BotState, TradeEvent

log = logging.getLogger("tradingbot.main")

__all__ = [
    "main", "build_parser", "BotLock", "LockHeldError", "Components", "build_components", "confirm_live",
    "run_loop", "next_run_ts", "install_signal_handlers", "status_text", "write_stop", "write_clear",
    "HEARTBEAT_S", "RUN_OFFSET_S", "LIVE_CONFIRM_WORD", "OFFLINE_FIXTURE_DIRNAME", "offline_fixture_settings",
    "LOCK_STALE_MIN_S", "LOCK_REFRESH_S", "LOCK_REFRESH_FAIL_SHARE", "lock_stale_after_s", "heartbeat_event", "FEAR_GREED_CACHE_FILENAME",
    "status_text_from_env", "notify_test", "NOTIFY_TEST_TEXT",
    "WAIT_POLL_S", "WAKE_OVERDUE_S", "WAKE_SETTLE_S", "CYCLE_GAP_FACTOR", "LOG_FORMAT", "keep_system_awake",
    "configure_logging", "attach_log_file",
]

HEARTBEAT_S = 4 * 3600.0
RUN_OFFSET_S = 5.0
LIVE_CONFIRM_WORD = "LIVE"
OFFLINE_FIXTURE_DIRNAME = "offline_fixture"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_LOCKED = 4
LOCK_STALE_MIN_S = 1800.0        # 잠금 ts 정체 판정 하한(초)
LOCK_STALE_CYCLES = 3            # ts 가 이 사이클 수보다 오래되면 낡은 잠금
LOCK_REFRESH_S = 300.0           # 사이클 사이 대기 중 잠금 ts 갱신 주기(초)
LOCK_REFRESH_FAIL_SHARE = 0.5    # ts 갱신 실패가 stale_after_s 의 이 비율 이상 이어지면 잠금 상실로 보고 중단
DEFAULT_CYCLE_MINUTES = 15
WAIT_POLL_S = 1.0                # 사이클 사이 대기 조각(초) — Windows 에서 Ctrl+C 가 이 안에 반영된다
WAKE_OVERDUE_S = 120.0           # 대기가 계획보다 이만큼 늦게 끝났으면 PC 절전 복귀로 보고 바로 사이클
WAKE_SETTLE_S = 20.0             # 절전 복귀 직후 네트워크(Wi-Fi·DHCP)가 돌아올 시간
CYCLE_GAP_FACTOR = 2.0           # 사이클 간격이 주기의 이 배수를 넘으면 cycle_gap 경보
PID_REUSE_GRACE_MS = 60_000      # 잠금의 started_ts 보다 이만큼 늦게 생성된 프로세스는 같은 PID 의 다른 프로세스
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FEAR_GREED_CACHE_FILENAME = "fear_greed.json"
NOTIFY_TEST_TEXT = "✅ 텔레그램 연결 테스트 [{mode}] — 이 메시지가 보이면 거래 알림이 정상 전송됩니다"


# ======================================================================= 잠금
class LockHeldError(RuntimeError):
    """다른 살아 있는 프로세스가 ``bot.lock`` 을 갖고 있다."""


def lock_stale_after_s(cycle_minutes: int | float | None) -> float:
    """잠금 ``ts`` 정체 판정 기준(초): ``max(3 × cycle_minutes × 60, 1800)``. 잘못된 값이면 기본 15분으로 본다."""
    try:
        cm = float(cycle_minutes) if cycle_minutes is not None and not isinstance(cycle_minutes, bool) else 0.0
    except (TypeError, ValueError):
        cm = 0.0
    if not (cm > 0) or cm == float("inf"):
        cm = float(DEFAULT_CYCLE_MINUTES)
    return max(LOCK_STALE_CYCLES * cm * 60.0, LOCK_STALE_MIN_S)


def _pid_alive(pid: int) -> bool:
    """PID 가 살아 있는 프로세스인지. 판단할 수 없으면 True(잠금을 빼앗지 않는 쪽)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 존재하지만 다른 사용자
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows: ``OpenProcess`` + ``GetExitCodeProcess`` 로 확인한다.

    Windows 에서 ``os.kill(pid, 0)`` 은 생존 확인이 아니다 — 시그널 0 은 ``CTRL_C_EVENT`` 라서
    ``GenerateConsoleCtrlEvent`` 로 그 프로세스 그룹(같은 콘솔의 봇 자신 포함)에 Ctrl+C 를 보낸다.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - ctypes 없는 특수 빌드: 살아 있다고 보고 잠금을 지킨다
        return True
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87      # 그런 PID 없음
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 87 = 그런 PID 없음. 5(ACCESS_DENIED, 다른 사용자의 프로세스)·기타 오류는 판단 불가 → 살아 있다고 본다.
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - API 호출 실패: 잠금을 지키는 쪽
        return True


_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000   # 1601-01-01 → 1970-01-01, 100ns 단위


def _windows_process_identity(pid: int) -> tuple[int | None, str | None]:
    """Windows: 살아 있는 PID 의 (생성 시각 epoch ms, 실행 파일 이름 소문자). 모르면 각각 None.

    Windows 는 끝난 프로세스의 PID 를 금방 다른 프로세스에 다시 준다. 창 닫기·작업 끝내기로 강제 종료된 봇의
    ``bot.lock`` PID 를 브라우저 등이 받으면 '살아 있음'으로 보여 재시작이 막히므로, 잠금을 쓴 프로세스인지
    생성 시각·이미지 이름으로 확인한다. 핸들을 못 열면(보호된 프로세스·다른 사용자) (None, None).
    """
    import ntpath

    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        ft_p = ctypes.POINTER(wintypes.FILETIME)
        k.GetProcessTimes.argtypes = (wintypes.HANDLE, ft_p, ft_p, ft_p, ft_p)
        k.GetProcessTimes.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        handle = k.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None, None
        try:
            created: int | None = None
            times = [wintypes.FILETIME() for _ in range(4)]
            if k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                ft = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                created = (ft - _FILETIME_EPOCH_OFFSET) // 10_000 if ft > _FILETIME_EPOCH_OFFSET else None
            image: str | None = None
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                image = ntpath.basename(buf.value).lower() or None
            return created, image
        finally:
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 판단 불가: 재사용으로 보지 않는다(잠금을 지키는 쪽)
        return None, None


class BotLock:
    """PID 파일 잠금(``<data_dir>/bot.lock``). 내용: ``{"pid","host","ts","started_ts"}`` JSON.

    - ``acquire()``: O_EXCL 생성. 이미 있으면 낡았는지 판정한다.
      같은 호스트: 기록된 PID 가 살아 있고 ``ts`` 가 ``stale_after_s`` 안이면 ``LockHeldError``.
      PID 가 죽었거나, 파일이 깨졌거나, ``ts`` 가 ``stale_after_s`` 보다 오래됐으면 낡은 잠금으로 보고 가져온다(WARNING).
      다른 호스트(공유 DATA_DIR — NFS·한 볼륨의 두 컨테이너): PID 검사는 의미가 없으므로 ``ts`` 만 본다 —
      ``ts`` 가 ``stale_after_s`` 안이면 ``LockHeldError``, 오래됐거나 없으면 가져온다.
      host/ts 가 없는 구버전 파일은 PID 규칙만 쓴다.
    - ``refresh()``: 파일의 PID·호스트가 내가 쓴 것인지 확인하고 ``ts`` 를 갱신한다. 아니면 ``cycle.LockLostError``.
      호스트는 **획득 시점의 이름**으로 고정해 기록·비교한다(실행 중 hostname 이 바뀌어도 — macOS Bonjour 이름 변경,
      DHCP — 내 잠금을 잃었다고 오판하지 않는다). ``ts`` 쓰기가 마지막 성공 이후 ``stale_after_s × LOCK_REFRESH_FAIL_SHARE``
      이상 계속 실패하고 그 실패가 **연속 2회 이상**이면 ``LockLostError``(다른 인스턴스가 곧 낡은 잠금으로 보고
      가져갈 수 있으므로 치명 처리). PC 절전 복귀 직후 한 번의 일시 실패는 치명으로 보지 않는다 — 단, 이번 사이클이
      끝나기 전에 ``stale_after_s`` 에 닿을 만큼 오래됐으면(``stale_after_s − LOCK_REFRESH_S − CYCLE_DEADLINE_S`` 이상,
      긴 절전) 첫 실패도 ``LockLostError``. 그 전의 실패는 원래 예외를 올린다(호출자가 WARNING 후 다음에 다시 시도).
    - ``release()``: 내 잠금일 때만 삭제한다. 삭제 실패는 경고만(다음 실행이 낡은 잠금으로 가져간다).
    - Windows: 파일 교체·삭제는 공유 위반(다른 프로세스가 파일을 읽는 중)이면 잠시 재시도한다(``fsutil``).
      PID 가 살아 있어도 잠금을 쓴 프로세스가 아니면(생성 시각이 ``started_ts`` 보다 늦거나 실행 파일 이름이
      ``image`` 와 다름 — PID 재사용) 낡은 잠금으로 본다(``pid_alive`` 를 주입하면 이 검사는 쓰지 않는다).
    - ``took_over``: ``acquire()`` 가 낡은 잠금을 가져왔으면 그 정보(``pid``·``reason``·``ts``), 아니면 None.

    ``stale_after_s`` 기본값은 ``lock_stale_after_s(15)``(= 2700초). ``run`` 은 ``cycle_minutes`` 로 계산해 넘긴다.
    """

    def __init__(self, path: Path, *, pid: int | None = None, pid_alive: Callable[[int], bool] | None = None,
                 clock_ms: Callable[[], int] | None = None, stale_after_s: float | None = None,
                 hostname: Callable[[], str] | None = None,
                 process_identity: Callable[[int], tuple[int | None, str | None]] | None = None) -> None:
        self.path = Path(path)
        self.pid = int(pid if pid is not None else os.getpid())
        self._alive = pid_alive or _pid_alive
        if process_identity is None and pid_alive is None and os.name == "nt":
            process_identity = _windows_process_identity
        self._identity = process_identity
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._hostname = hostname or socket.gethostname
        self.stale_after_s = float(stale_after_s) if stale_after_s is not None \
            else lock_stale_after_s(DEFAULT_CYCLE_MINUTES)
        self.held = False
        self._started = 0
        self._host: str | None = None        # 획득 시점에 고정한 호스트 이름(기록·비교용)
        self._image: str | None = None       # 획득 시점의 내 실행 파일 이름(Windows, PID 재사용 판별용)
        self._last_ok_ms: int | None = None  # 마지막으로 ts 쓰기에 성공한 시각
        self._fails = 0                      # 연속 ts 쓰기 실패 횟수
        self.took_over: dict | None = None

    def _my_host(self) -> str:
        try:
            return str(self._hostname())
        except Exception:  # noqa: BLE001
            return ""

    def _foreign_host(self, info: dict | None) -> str | None:
        """잠금에 기록된 호스트가 내 호스트와 다르면 그 이름(같거나 알 수 없으면 None)."""
        if not info:
            return None
        host = info.get("host")
        mine = self._host or self._my_host()
        if isinstance(host, str) and host and mine and host != mine:
            return host
        return None

    def _age_s(self, info: dict) -> float | None:
        ts = info.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts == ts and ts > 0 \
                and ts != float("inf"):
            return (float(self._clock()) - float(ts)) / 1000.0
        return None

    def stale_reason(self, info: dict | None) -> str | None:
        """잠금 내용 ``info`` 가 PID 생존과 무관하게 낡았다고 볼 이유(없으면 None).

        깨진 파일 · ``ts`` 가 ``stale_after_s`` 보다 오래됨 · 다른 호스트인데 ``ts`` 가 없음. 다른 호스트라도
        ``ts`` 가 신선하면 낡지 않았다(그 호스트에서 살아 있다). 죽은 PID 판정은 호출자가 따로 한다(같은 호스트만).
        """
        if not info:
            return None
        if info.get("corrupt"):
            return "잠금 파일 손상"
        foreign = self._foreign_host(info)
        age_s = self._age_s(info)
        if age_s is not None and age_s > self.stale_after_s:
            why = f"갱신이 {age_s:.0f}초 전(기준 {self.stale_after_s:.0f}초)"
            return f"다른 호스트({foreign})의 잠금, {why}" if foreign else why
        if foreign and age_s is None:
            return f"다른 호스트({foreign})의 잠금, 갱신 시각 없음"
        return None

    def pid_reused(self, info: dict | None) -> str | None:
        """기록된 PID 가 살아 있지만 잠금을 쓴 프로세스가 아니면 그 이유(Windows 전용; 모르면 None).

        - 실행 파일 이름이 잠금의 ``image`` 와 다름(예: msedge.exe 가 PID 를 받음)
        - 프로세스 생성 시각이 잠금의 ``started_ts`` + ``PID_REUSE_GRACE_MS`` 보다 늦음(잠금을 쓴 프로세스는
          잠금보다 먼저 생성됐다 — 다른 python.exe 가 PID 를 받은 경우도 잡는다)
        판단할 수 없으면(핸들을 못 엶 등) None — 잠금을 지키는 쪽.
        """
        if self._identity is None or not info:
            return None
        pid = info.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        try:
            created, image = self._identity(pid)
        except Exception:  # noqa: BLE001
            return None
        rec_image = info.get("image")
        if isinstance(rec_image, str) and rec_image and isinstance(image, str) and image \
                and image.lower() != rec_image.lower():
            return f"PID 재사용(현재 {image}, 잠금 {rec_image})"
        started = info.get("started_ts")
        if isinstance(started, int) and not isinstance(started, bool) and started > 0 \
                and isinstance(created, int) and created > started + PID_REUSE_GRACE_MS:
            return "PID 재사용(잠금 이후 생성된 프로세스)"
        return None

    def holder(self) -> dict | None:
        """현재 잠금 내용(없으면 None, 깨졌으면 ``{"pid": None, "corrupt": True}``)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"pid": None, "corrupt": True}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            pid = data.get("pid")
            data["pid"] = int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None
            return data
        except ValueError:
            s = raw.strip()
            return {"pid": int(s)} if s.isdigit() else {"pid": None, "corrupt": True}

    def _content(self) -> str:
        host = self._host if self._host is not None else self._my_host()
        info: dict[str, Any] = {"pid": self.pid, "host": host, "ts": self._clock(), "started_ts": self._started}
        if self._image:
            info["image"] = self._image
        return json.dumps(info, sort_keys=True, ensure_ascii=False)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._started = self._clock()
        self._host = self._my_host()
        if self._identity is not None and self.pid == os.getpid():
            try:
                self._image = self._identity(self.pid)[1]
            except Exception:  # noqa: BLE001
                self._image = None
        self.took_over = None
        created = False
        for attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = self.holder()
                other = info.get("pid") if info else None
                stale = self.stale_reason(info)
                foreign = self._foreign_host(info)
                if other == self.pid and foreign is None:
                    break                       # 이미 내 잠금(재진입)
                # 다른 호스트의 PID 는 이 호스트에서 검사할 수 없다 → 신선한 ts 가 유일한 생존 신호
                alive = foreign is not None or (other is not None and self._alive(other))
                if alive and stale is None and foreign is None:
                    stale = self.pid_reused(info)
                if alive and stale is None:
                    where = f", host={foreign}" if foreign else ""
                    raise LockHeldError(
                        f"다른 인스턴스가 실행 중입니다 (pid={other}{where}, lock={self.path}) — 실행 중인 봇이 "
                        f"정말 없다면 그 파일을 지우고 다시 실행하세요")
                why = stale if stale is not None else "살아 있지 않음"
                log.warning("낡은 잠금 파일을 가져옵니다 (기록 pid=%s, %s): %s", other, why, self.path)
                self.took_over = {"pid": other, "reason": why,
                                  "ts": info.get("ts") if isinstance(info, dict) else None}
                fsutil.unlink(self.path)
                if attempt == 1:
                    raise LockHeldError(f"잠금을 얻지 못했습니다: {self.path}")
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._content())
            created = True
            break
        self.held = True
        if created:
            self._last_ok_ms = self._clock()
            self._fails = 0
        else:
            try:
                self._write()                   # 재진입: ts 만 새로 쓴다
            except OSError as exc:
                log.warning("잠금 ts 갱신 실패(%s) — 첫 사이클에서 다시 시도합니다: %s", type(exc).__name__, self.path)
        log.info("잠금 획득 pid=%d path=%s", self.pid, self.path)

    def _write(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{self.pid}.tmp")
        try:
            tmp.write_text(self._content(), encoding="utf-8")
            fsutil.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        self._last_ok_ms = self._clock()
        self._fails = 0

    def refresh(self) -> None:
        info = self.holder()
        other = info.get("pid") if info else None
        host = info.get("host") if info else None
        mine = self._host if self._host is not None else self._my_host()
        if other != self.pid or (isinstance(host, str) and host and mine and host != mine):
            self.held = False
            raise C.LockLostError(f"잠금을 잃었습니다 (파일 pid={other}, 내 pid={self.pid}): {self.path}")
        try:
            self._write()
        except Exception as exc:  # noqa: BLE001 - 디스크 가득 참·tmp 경로 쓰기 불가 등
            self._fails += 1
            now = self._clock()
            since = self._last_ok_ms if self._last_ok_ms is not None else self._started
            failing_s = (float(now) - float(since)) / 1000.0 if since else 0.0
            limit_s = self.stale_after_s * LOCK_REFRESH_FAIL_SHARE
            # 연속 2회 이상이면 치명. 한 번의 실패(PC 절전 복귀 직후의 일시 실패 — 마지막 성공이 절전 전이라
            # failing_s 가 큼)는 다음 갱신(최대 1 사이클 뒤)이 판단하되, 이번 사이클이 끝나기 전에 다른 인스턴스가
            # 낡은 잠금으로 볼 만큼 오래됐으면(긴 절전) 첫 실패도 치명 — 두 인스턴스가 함께 거래하지 않게.
            near_stale = failing_s >= self.stale_after_s - (LOCK_REFRESH_S + C.CYCLE_DEADLINE_S)
            if failing_s >= limit_s and (self._fails >= 2 or near_stale):
                raise C.LockLostError(
                    f"잠금 ts 갱신이 {failing_s:.0f}초째 실패({type(exc).__name__}) — 기준 {limit_s:.0f}초 초과, "
                    f"다른 인스턴스가 낡은 잠금으로 볼 수 있어 중단: {self.path}") from exc
            raise

    def release(self) -> None:
        if not self.held:
            return
        info = self.holder()
        if info is not None and info.get("pid") == self.pid:
            try:
                fsutil.unlink(self.path)
            except OSError as exc:
                # 지우지 못해도 다음 실행이 죽은 PID 의 낡은 잠금으로 보고 가져간다
                log.warning("잠금 파일 삭제 실패(%s): %s", type(exc).__name__, self.path)
        self.held = False
        log.info("잠금 해제 pid=%d", self.pid)

    def __enter__(self) -> "BotLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ======================================================================= 구성 요소
@dataclass
class Components:
    settings: Settings
    market: Any
    broker: Any
    portfolio: Any
    backend: Any
    ledger: Any
    notifier: Any
    skills: Any
    cycle: Any


def build_components(settings: Settings, *, lock: BotLock | None = None, offline_fixture: bool = False,
                     mock_scenario: str | None = None) -> Components:
    """``load_settings()`` 결과로 실제 객체를 만든다(네트워크 호출은 첫 사이클에서 일어난다).

    ``offline_fixture=True`` → 합성 시세(``FakeMarketData``) + 뉴스·Fear&Greed 조회 없음(paper 전용, 네트워크 0).
    그 외에는 ``FearGreedFetcher``(캐시 ``<data_dir>/cache/fear_greed.json``)를 스캐너 종합심리에 넣는다.
    ``mock_scenario`` → ``AGENT_BACKEND=mock`` 일 때 ``tradingbot.testing`` 시나리오(None 이면 conservative).
    """
    from tradingbot import news
    from tradingbot.exchange import CcxtBroker, CcxtMarketData, PaperBroker
    from tradingbot.llm.backend import MockBackend
    from tradingbot.llm.cost import CostLedger
    from tradingbot.notify import Notifier, TelegramTransport
    from tradingbot.portfolio import Portfolio
    from tradingbot.scanner import Scanner, Thresholds
    from tradingbot.skills import SkillRegistry

    if settings.mode == "live" and settings.agent_backend == "mock":
        # 창에 남은 AGENT_BACKEND=mock(set/$env:)이 .env 를 덮으면 가짜 판단으로 실제 주문이 나간다
        raise ConfigError("MODE=live 에서는 AGENT_BACKEND=mock 을 쓸 수 없습니다(가짜 판단으로 실제 주문) — "
                          "환경변수 AGENT_BACKEND 를 지우거나 anthropic 으로 두세요(setx 로 넣었다면 README §4)")
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if offline_fixture:
        if settings.mode != "paper":
            raise ConfigError("--offline-fixture 는 MODE=paper 에서만 쓸 수 있습니다")
        from tradingbot.testing import FakeMarketData

        log.warning("오프라인 픽스처: 합성 시세(FakeMarketData) 사용 — 실제 시장 데이터가 아닙니다")
        market: Any = FakeMarketData()
        broker: Any = PaperBroker(market, settings.paper_start_balance)
    elif settings.mode == "paper":
        market = CcxtMarketData(settings)
        broker: Any = PaperBroker(market, settings.paper_start_balance)
    else:
        broker = CcxtBroker(settings)
        market = broker
    portfolio = Portfolio.load(settings, data_dir / C.STATE_FILENAME)
    ledger = CostLedger(data_dir / "cost_ledger.jsonl", settings.llm_daily_budget_usd, settings.llm_monthly_budget_usd)
    if settings.agent_backend == "mock":
        from tradingbot.testing import mock_backend_from_env

        backend: Any = mock_backend_from_env({"MOCK_SCENARIO": mock_scenario or ""})
    else:
        from tradingbot.llm.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend(settings, ledger)
    transport = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        transport = TelegramTransport(settings.telegram_bot_token, settings.telegram_chat_id)
    else:
        log.warning("텔레그램 토큰/채팅 ID 없음 — 알림은 로그로만 남깁니다")
    notifier = Notifier(transport, failed_log=data_dir / "logs" / "failed_notifications.jsonl")
    skills = SkillRegistry(C.PROJECT_ROOT / "skills")
    fear_greed = None
    if not offline_fixture:
        from tradingbot.sentiment_sources import FearGreedFetcher

        fear_greed = FearGreedFetcher(cache_path=data_dir / C.CACHE_DIRNAME / FEAR_GREED_CACHE_FILENAME)
    # SCANNER_* 는 Settings 와 같은 출처(os.environ 위에 .env)에서 — 없으면 Scanner 기본(os.environ)
    scanner_env = dict(getattr(settings, "scanner_env", ()) or ())
    thresholds = Thresholds.from_env(scanner_env) if scanner_env else None
    if thresholds is not None and thresholds != Thresholds():
        log.info("스캐너 임계값(설정 반영): %s", {k: v for k, v in dataclasses.asdict(thresholds).items()
                                         if v != getattr(Thresholds(), k)})
    scanner = Scanner(settings, market, news_fetcher=None if offline_fixture else news.default_fetcher,
                      fear_greed_fetcher=fear_greed, cache_dir=data_dir / C.CACHE_DIRNAME, thresholds=thresholds)
    cycle = C.Cycle(settings, market, broker, portfolio, backend, ledger, notifier, skills, scanner=scanner, lock=lock)
    return Components(settings, market, broker, portfolio, backend, ledger, notifier, skills, cycle)


# ======================================================================= 확인·스케줄·신호
def confirm_live(input_fn: Callable[[str], str], out: TextIO) -> bool:
    """콘솔에서 정확히 ``LIVE`` 를 입력해야 True. EOF/인터럽트/다른 값은 False."""
    out.write("경고: MODE=live — 실제 자금으로 주문합니다. 계속하려면 LIVE 를 입력하세요: ")
    out.flush()
    try:
        answer = input_fn("")
    except (EOFError, KeyboardInterrupt):
        return False
    return isinstance(answer, str) and answer.strip() == LIVE_CONFIRM_WORD


def _stdin_is_tty() -> bool:
    """현재 ``sys.stdin`` 이 대화형 터미널인지(파이프·리다이렉트·닫힘 → False)."""
    try:
        stdin = sys.stdin
        return bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def offline_fixture_settings(settings: Settings) -> Settings:
    """``--offline-fixture`` 용 격리 설정: ``<data_dir>/offline_fixture`` + mock 백엔드 + 텔레그램 끔.

    합성 시세로 연 포지션·사이클 카운터·OI/펀딩 이력이 실제 paper ``state.json``/``cache`` 에 섞이지 않게 하고,
    "키·네트워크 없음" 약속대로 유료 LLM 호출과 텔레그램 전송을 막는다. 이미 격리된 설정이면 그대로 둔다.
    """
    src_dir = Path(settings.data_dir)
    data_dir = src_dir if src_dir.name == OFFLINE_FIXTURE_DIRNAME else src_dir / OFFLINE_FIXTURE_DIRNAME
    log_file = getattr(settings, "log_file", None)
    if log_file is not None and data_dir != src_dir:
        try:   # DATA_DIR 아래 로그(LOG_FILE 상대 경로)는 격리 폴더로 옮긴다 — 합성 실행 로그가 실제 로그에 섞이지 않게
            log_file = data_dir / Path(log_file).relative_to(src_dir)
        except ValueError:
            pass
    return dataclasses.replace(settings, data_dir=data_dir, agent_backend="mock",
                               telegram_bot_token=None, telegram_chat_id=None, log_file=log_file)


def next_run_ts(now_s: float, cycle_minutes: int, offset_s: float = RUN_OFFSET_S) -> float:
    """다음 실행 시각(epoch 초): ``cycle_minutes`` 경계 + ``offset_s``. 지금이 경계+offset 이전이면 이번 경계."""
    period = max(1, int(cycle_minutes)) * 60.0
    base = (now_s // period) * period
    cand = base + offset_s
    if cand <= now_s:
        cand += period
    return cand


def install_signal_handlers(stop_event: threading.Event) -> dict:
    """SIGINT/SIGTERM/SIGBREAK → ``stop_event.set()``. 이전 핸들러를 돌려준다(메인 스레드가 아니면 빈 dict).

    SIGBREAK 는 Windows 의 Ctrl+Break(기본 처리는 즉시 강제 종료 — 종료 알림·잠금 해제 없음). 다른 OS 에는 없다.
    콘솔 창 닫기·작업 끝내기·로그오프는 잡을 수 없다(즉시 종료; 다음 실행이 낡은 잠금을 가져간다).
    """
    prev: dict = {}

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        log.warning("종료 신호 수신(%s) — 진행 중 사이클을 마치고 종료합니다", signum)
        stop_event.set()

    if threading.current_thread() is not threading.main_thread():
        return prev
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            prev[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - 플랫폼 제약
            pass
    return prev


def _restore_signals(prev: Mapping) -> None:
    for sig, h in prev.items():
        try:
            signal.signal(sig, h)
        except (ValueError, OSError, TypeError):  # pragma: no cover
            pass


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_execution_state_setter() -> Callable[[int], int] | None:  # pragma: no cover - Windows 전용
    """kernel32.SetThreadExecutionState (argtypes 를 선언한 비공유 인스턴스). 못 쓰면 None."""
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k.SetThreadExecutionState
        fn.argtypes = (wintypes.DWORD,)
        fn.restype = wintypes.DWORD
        return lambda flags: int(fn(flags))
    except Exception:  # noqa: BLE001
        return None


@contextlib.contextmanager
def keep_system_awake(*, setter: Callable[[int], int] | None = None) -> Iterator[bool]:
    """``run`` 동안 Windows 자동 절전(유휴 타이머)을 막는다 — 절전 중에는 손절·익절이 집행되지 않는다.

    ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``: 화면 꺼짐은 막지 않는다. 덮개 닫기·전원 버튼·
    시작 > 절전 같은 사용자 절전과 배터리 사용 중 모던 스탠바이는 막지 못한다. 요청은 이 스레드에 묶이며
    끝나면 ``ES_CONTINUOUS`` 로 되돌린다(프로세스가 끝나도 Windows 가 풀어 준다). Windows 가 아니면(그리고
    ``setter`` 를 주지 않으면) 아무것도 하지 않는다. 실패해도 실행은 계속한다. 값은 차단 성공 여부.
    """
    if setter is None:
        if os.name != "nt":
            yield False
            return
        setter = _windows_execution_state_setter()  # pragma: no cover - Windows 전용
    ok = False
    if setter is not None:
        try:
            ok = bool(setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        except Exception:  # noqa: BLE001
            ok = False
    if ok:
        log.info("Windows 자동 절전 차단(실행 중에만, 화면 꺼짐은 허용)")
    else:
        log.warning("Windows 자동 절전 차단 실패 — 전원 설정에서 절전 모드를 '안 함'으로 두세요"
                    "(PC 가 잠든 동안 손절·익절이 집행되지 않습니다)")
    try:
        yield ok
    finally:
        if ok and setter is not None:
            with contextlib.suppress(Exception):
                setter(ES_CONTINUOUS)


def _event(settings: Settings, kind: str, **kw: Any) -> TradeEvent:
    return TradeEvent(kind=kind, ts=int(time.time() * 1000), mode=settings.mode, **kw)  # type: ignore[arg-type]


def _safe_equity(comp: Components) -> float | None:
    try:
        if comp.settings.mode == "paper":
            return float(comp.portfolio.equity({}))
        return float(comp.portfolio.equity({}, quote_balance=float(comp.broker.free_quote())))
    except Exception:  # noqa: BLE001
        return None


def _finite(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    f = float(x)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def heartbeat_event(comp: Components) -> TradeEvent:
    """4시간 하트비트 이벤트(kind ``heartbeat``). 네트워크 없이 상태 파일 값(포지션 ``last_mark``)만 쓴다.

    필드: ``equity``, ``pnl_quote``/``pnl_pct``(당일 = 자본 − 당일 시작자본; 시작자본이 없으면 실현손익),
    ``extra``: ``heartbeat``(True, 구버전 호환) · ``cycle`` · ``kill_switch`` · ``positions``
    (``[{symbol, unrealized_pct, value_quote}]``) · ``exposure_pct``(보유 평가액 / 자본) · ``llm_cost_today_usd``
    · ``realized_pnl``. 값을 못 구하면 해당 필드는 빠진다.
    """
    settings = comp.settings
    st = comp.portfolio.state
    equity = _safe_equity(comp)
    positions: list[dict] = []
    pos_value = 0.0
    for sym, p in sorted((getattr(st, "positions", None) or {}).items()):
        entry = _finite(getattr(p, "entry_price", None))
        mark = _finite(getattr(p, "last_mark", None)) or entry
        base = _finite(getattr(p, "base_amount", None))
        item: dict[str, Any] = {"symbol": sym}
        if entry and entry > 0 and mark:
            item["unrealized_pct"] = mark / entry - 1.0
        if base is not None and mark:
            item["value_quote"] = base * mark
            pos_value += base * mark
        positions.append(item)
    daily = getattr(st, "daily", None)
    start = _finite(getattr(daily, "start_equity", None))
    realized = _finite(getattr(daily, "realized_pnl", None))
    pnl_quote: float | None = None
    pnl_pct: float | None = None
    if equity is not None and start is not None and start > 0:
        pnl_quote = equity - start
        pnl_pct = pnl_quote / start
    elif realized is not None:
        pnl_quote = realized
        pnl_pct = realized / start if start is not None and start > 0 else None
    llm_cost: float | None = None
    try:
        if comp.ledger is not None:
            llm_cost = _finite(float(comp.ledger.spent_today()))
    except Exception:  # noqa: BLE001
        llm_cost = None
    if llm_cost is None:
        llm_cost = _finite(getattr(daily, "llm_cost_usd", None))
    extra: dict[str, Any] = {"heartbeat": True, "cycle": int(getattr(st, "cycle", 0) or 0),
                             "kill_switch": bool(getattr(st, "kill_switch", False)), "positions": positions}
    if equity is not None and equity > 0:
        extra["exposure_pct"] = pos_value / equity
    if llm_cost is not None:
        extra["llm_cost_today_usd"] = llm_cost
    if realized is not None:
        extra["realized_pnl"] = realized
    reason = (f"하트비트: 정상 동작 중 (cycle {extra['cycle']}, 보유 {len(positions)}개, "
              f"킬스위치 {'ON' if extra['kill_switch'] else 'OFF'})")
    return _event(settings, "heartbeat", equity=equity, pnl_quote=pnl_quote, pnl_pct=pnl_pct, reason=reason,
                  extra=extra)


def _poll_wait(stop: threading.Event, seconds: float, *, poll_s: float = WAIT_POLL_S,
               clock: Callable[[], float] | None = None,
               monotonic: Callable[[], float] = time.monotonic) -> bool:
    """``stop.wait(seconds)`` 를 ``poll_s`` 조각으로 나눠 기다린다(종료 요청이면 True).

    - Windows CPython ≤3.13 에서는 ``Event.wait(timeout)`` 이 Ctrl+C 로 깨지 않는다(gh-125541, 3.14 에서 수정).
      파이썬 SIGINT 핸들러는 대기가 끝나야 돌므로, 조각마다 돌아와야 Ctrl+C 가 ~1초 안에 반영된다.
    - 마감은 ``monotonic`` 과 벽시계(``clock``) 둘 중 먼저 오는 쪽: PC 절전에서 깨어나면(벽시계는 절전 시간을
      센다) 남은 대기를 버리고 바로 돌아온다. 벽시계가 뒤로 가도 ``monotonic`` 마감이 상한이다.
    """
    seconds = max(0.0, float(seconds))
    deadline = monotonic() + seconds
    wall_deadline = clock() + seconds if clock is not None else None
    while True:
        left = deadline - monotonic()
        if wall_deadline is not None:
            left = min(left, wall_deadline - clock())
        if left <= 0:
            return stop.is_set()
        if stop.wait(min(left, poll_s)):
            return True


def _wait_with_lock_refresh(wait_fn: Callable[[float], bool], wait_s: float, lock: Any,
                            refresh_s: float = LOCK_REFRESH_S, *, clock: Callable[[], float] | None = None,
                            settle_s: float = WAKE_SETTLE_S) -> bool:
    """``wait_s`` 를 최대 ``refresh_s`` 조각으로 나눠 기다리며 조각 사이마다 ``lock.refresh()`` (ts 갱신).

    종료 요청이면 True. 잠금이 없으면 한 번에 기다린다. ``LockLostError`` 는 전파한다(호출자가 치명 처리).
    ``clock``(벽시계)을 주면 남은 대기를 ``시작 + wait_s`` 마감으로 자른다: PC 가 절전했다 깨어나면 남은 조각을
    마저 기다리지 않는다. 마감을 ``WAKE_OVERDUE_S`` 넘게 지났으면(절전 복귀) 네트워크가 돌아올 ``settle_s`` 만
    더 기다린 뒤 돌아온다.
    """
    refresh = getattr(lock, "refresh", None) if lock is not None else None
    if not callable(refresh) or refresh_s <= 0:
        return bool(wait_fn(wait_s))
    remaining = max(0.0, float(wait_s))
    deadline = clock() + remaining if clock is not None else None
    while True:
        chunk = min(remaining, float(refresh_s))
        if wait_fn(chunk):
            return True
        remaining -= chunk
        if deadline is not None:
            late = clock() - deadline
            if late > WAKE_OVERDUE_S:
                log.warning("대기가 예정보다 %.0f초 늦게 끝남(PC 절전·일시정지 의심) — %.0f초 뒤 바로 사이클을 돌립니다",
                            late, settle_s)
                return bool(settle_s > 0 and wait_fn(settle_s))
            remaining = min(remaining, -late)
        if remaining <= 1e-9:
            return False
        try:
            refresh()
        except C.LockLostError:
            raise
        except Exception as exc:  # noqa: BLE001 - ts 갱신 실패(디스크 등)는 다음 사이클 refresh 가 다시 시도
            log.warning("대기 중 잠금 갱신 실패: %s", type(exc).__name__)


def _notify_cycle_gap(comp: Components, gap_s: float) -> None:
    """사이클 시작 간격 ``gap_s`` 가 주기의 ``CYCLE_GAP_FACTOR`` 배를 넘으면 경보(kind ``alert``, ``cycle_gap``)."""
    try:
        period_s = max(1, int(comp.settings.cycle_minutes)) * 60.0
        if not (gap_s > CYCLE_GAP_FACTOR * period_s):
            return
        n_pos = len(getattr(comp.portfolio.state, "positions", None) or {})
        reason = (f"{gap_s / 60.0:.0f}분 동안 사이클이 없었습니다(예정 {period_s / 60.0:.0f}분마다) — 그동안 손절·익절·"
                  "시간손절이 집행되지 않았습니다. PC 절전·콘솔 일시정지·과부하를 확인하세요")
        log.warning("사이클 공백 %.0f초(보유 %d개) — PC 절전·일시정지 의심", gap_s, n_pos)
        comp.notifier.notify(_event(comp.settings, "alert", reason=reason,
                                    extra={"alert": "cycle_gap", "gap_s": round(gap_s),
                                           "cycle_minutes": int(comp.settings.cycle_minutes),
                                           "positions": n_pos}))
    except Exception as exc:  # noqa: BLE001 - 경보 실패가 루프를 멈추면 안 된다
        log.warning("사이클 공백 경보 생성 실패: %s", type(exc).__name__)


def run_loop(comp: Components, *, once: bool, stop_event: threading.Event | None = None,
             clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
             heartbeat_s: float = HEARTBEAT_S, max_cycles: int | None = None,
             wait: Callable[[float], bool] | None = None, lock: Any | None = None,
             lock_refresh_s: float = LOCK_REFRESH_S) -> int:
    """사이클 루프. 치명 오류(손상 상태·저장 실패·잠금 상실·설정 오류)면 EXIT_ERROR.

    ``wait(seconds) -> bool`` 은 다음 실행까지 대기하고 종료 요청이면 True(기본: ``stop_event`` 를 1초 조각으로
    기다림 — Windows 에서도 Ctrl+C 가 바로 반영되고, PC 절전에서 깨어나면 남은 대기를 버린다).
    ``lock``(없으면 ``comp.cycle.lock``)이 있으면 대기를 ``lock_refresh_s``(기본 5분) 조각으로 나눠 그 사이마다
    ``lock.refresh()`` 로 ``ts`` 를 갱신한다(긴 대기 중에 잠금이 낡아 보이지 않게).
    사이클 시작 간격이 주기의 ``CYCLE_GAP_FACTOR`` 배를 넘으면 notify(alert ``cycle_gap``) — 그동안 손절·익절이
    집행되지 않았음을 알린다.
    ``max_cycles`` 는 시도 횟수(실패 포함) 상한(테스트용). 종료 시 항상 notify(shutdown) + ``notifier.flush()``.
    """
    stop = stop_event or threading.Event()
    wait_fn = wait or (lambda seconds: _poll_wait(stop, seconds, clock=clock))
    settings = comp.settings
    notifier = comp.notifier
    if lock is None:
        lock = getattr(comp.cycle, "lock", None)
    code = EXIT_OK
    reason = "정상 종료"
    last_hb = monotonic()
    last_start: float | None = None
    n = 0
    try:
        while not stop.is_set():
            n += 1
            started = clock()
            if last_start is not None:
                _notify_cycle_gap(comp, started - last_start)
            last_start = started
            try:
                summary = comp.cycle.run_once()
                log.info("사이클 %s 완료 (오류 %d)", summary.cycle, len(summary.errors))
            except (C.LockLostError, C.StateSaveError, ConfigError) as exc:
                code, reason = EXIT_ERROR, f"치명 오류로 중단: {type(exc).__name__}"
                log.error("%s", reason)
                notifier.notify(_event(settings, "error", reason=reason, extra={"stage": "cycle"}))
                break
            except Exception as exc:  # noqa: BLE001
                from tradingbot.portfolio import CorruptStateError

                if isinstance(exc, (CorruptStateError, OSError)):
                    code, reason = EXIT_ERROR, f"치명 오류로 중단: {type(exc).__name__}"
                    log.error("%s", reason)
                    notifier.notify(_event(settings, "error", reason=reason, extra={"stage": "cycle"}))
                    break
                # 예상 밖 예외: 기록하고 다음 사이클 시도
                log.error("사이클 예외(계속) type=%s", type(exc).__name__)
                notifier.notify(_event(settings, "error", reason=f"사이클 예외: {type(exc).__name__}",
                                       extra={"stage": "cycle"}))
            if once or (max_cycles is not None and n >= max_cycles):
                break
            if monotonic() - last_hb >= heartbeat_s:
                last_hb = monotonic()
                try:
                    notifier.notify(heartbeat_event(comp))
                except Exception as exc:  # noqa: BLE001 - 하트비트 실패가 루프를 멈추면 안 된다
                    log.warning("하트비트 생성 실패: %s", type(exc).__name__)
            wait_s = max(0.0, next_run_ts(clock(), settings.cycle_minutes) - clock())
            try:
                stopped = _wait_with_lock_refresh(wait_fn, wait_s, lock, lock_refresh_s, clock=clock)
            except C.LockLostError as exc:
                code, reason = EXIT_ERROR, f"치명 오류로 중단: {type(exc).__name__}"
                log.error("%s (대기 중 잠금 갱신)", reason)
                notifier.notify(_event(settings, "error", reason=reason, extra={"stage": "lock_refresh"}))
                break
            if stopped or stop.is_set():
                reason = "종료 신호(SIGINT/SIGTERM)"
                break
        else:
            reason = "종료 신호(SIGINT/SIGTERM)"
    except KeyboardInterrupt:
        reason = "KeyboardInterrupt"
    finally:
        try:
            notifier.notify(_event(settings, "shutdown", reason=reason, equity=_safe_equity(comp)))
        finally:
            notifier.flush()
    return code


# ======================================================================= status / stop / clear
def _read_state(data_dir: Path) -> tuple[BotState | None, str | None]:
    p = data_dir / C.STATE_FILENAME
    if not p.is_file():
        return None, "state.json 없음 (아직 실행 전)"
    try:
        return BotState.model_validate(json.loads(p.read_text(encoding="utf-8"))), None
    except (OSError, ValueError) as exc:
        return None, f"state.json 읽기/검증 실패: {type(exc).__name__}"


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def status_text(settings: Settings) -> str:
    """상태 요약(네트워크 없음). 시세는 포지션의 last_mark 를 쓴다."""
    return _status_text(Path(settings.data_dir), mode=settings.mode, symbols=", ".join(settings.symbols),
                        daily_budget=settings.llm_daily_budget_usd, monthly_budget=settings.llm_monthly_budget_usd,
                        cycle_minutes=settings.cycle_minutes)


def _env_float(src: Mapping[str, str], key: str, default: float) -> float:
    try:
        v = float((src.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default
    return v if v == v and v not in (float("inf"), float("-inf")) and v > 0 else default


def status_text_from_env(env: Mapping[str, str] | None = None, *, err: TextIO | None = None) -> str:
    """설정 검증 없이(백엔드 키가 없거나 설정이 깨져도) ``DATA_DIR`` 의 파일만 읽어 상태를 요약한다.

    ``env`` 가 None 이면 ``read_env(lenient=True)``(os.environ 위에 .env; 인코딩이 깨진 .env 도 경고 후 읽음).
    모드·종목·예산은 원문 값(없으면 기본)을 표시만 한다. DATA_DIR 값이 잘못됐으면 ``ConfigError``.
    """
    src: Mapping[str, str] = _lenient_env(err) if env is None else env
    data_dir = _control_data_dir(src)
    mode = (src.get("MODE") or "").strip().lower() or "paper"
    from tradingbot.config import DEFAULT_SYMBOLS

    symbols = ", ".join(x.strip() for x in ((src.get("SYMBOLS") or "").strip() or DEFAULT_SYMBOLS).split(",")
                        if x.strip()) or "-"
    try:
        cm: int | None = int((src.get("CYCLE_MINUTES") or "").strip() or DEFAULT_CYCLE_MINUTES)
    except ValueError:
        cm = None
    return _status_text(data_dir, mode=f"{mode} (설정 검증 실패 — 파일만 읽음)", symbols=symbols,
                        daily_budget=_env_float(src, "LLM_DAILY_BUDGET_USD", 10.0),
                        monthly_budget=_env_float(src, "LLM_MONTHLY_BUDGET_USD", 200.0), cycle_minutes=cm)


def _describe_lock(data_dir: Path, cycle_minutes: int | None) -> tuple[dict | None, str, bool]:
    """``<data_dir>/bot.lock`` 을 읽어 (내용, 상태 라벨, 실행 중인 봇이 있는가). 파일이 없으면 (None, "없음", False)."""
    lk = BotLock(Path(data_dir) / C.LOCK_FILENAME, stale_after_s=lock_stale_after_s(cycle_minutes))
    lock = lk.holder()
    if lock is None:
        return None, "없음", False
    pid = lock.get("pid")
    stale = lk.stale_reason(lock)
    foreign = lk._foreign_host(lock)
    alive = _pid_alive(pid) if isinstance(pid, int) else False
    if foreign is not None and stale is None:
        return lock, f"실행 중(다른 호스트 {foreign})", True    # 다른 호스트의 PID 는 여기서 검사할 수 없다 → ts 로 판단
    if alive and stale is None:
        reused = lk.pid_reused(lock)
        if reused is not None:
            return lock, f"낡음: {reused}", False
        return lock, "실행 중", True
    if stale is not None:
        return lock, f"낡음: {stale}", False
    return lock, "낡음: 프로세스 없음", False


def _status_text(data_dir: Path, *, mode: str, symbols: str, daily_budget: float, monthly_budget: float,
                 cycle_minutes: int | None) -> str:
    data_dir = Path(os.path.abspath(data_dir))    # resolve() 는 하지 않는다(매핑·subst 드라이브 문자 유지)
    lines = [f"모드: {mode}  데이터: {data_dir}  종목: {symbols}"]
    state, err = _read_state(data_dir)
    if state is None:
        lines.append(err or "상태 없음")
    else:
        pos_val = sum(p.base_amount * (p.last_mark or p.entry_price) for p in state.positions.values())
        lines.append(f"사이클: {state.cycle}  마지막 실행: {_fmt_ts(state.last_cycle_ts)}")
        if state.mode == "paper":
            eq = state.paper_quote_balance + pos_val
            lines.append(f"자본(추정): {eq:,.2f} USDT  현금 {state.paper_quote_balance:,.2f}  최고 {state.peak_equity:,.2f}")
        else:
            lines.append(f"보유 평가액: {pos_val:,.2f} USDT  최고자본 {state.peak_equity:,.2f} (현금은 거래소 조회 필요)")
        d = state.daily
        lines.append(f"당일({d.date}): 실현손익 {d.realized_pnl:+,.2f}  거래 {d.trades} (승 {d.wins}/패 {d.losses})  "
                     f"연속손실 {d.consecutive_losses}  LLM ${d.llm_cost_usd:.2f}")
        lines.append(f"킬스위치: {'ON — ' + (state.kill_reason or '') if state.kill_switch else 'OFF'}")
        now_ms = int(time.time() * 1000)
        if state.llm_paused_until_ts and now_ms < state.llm_paused_until_ts:
            lines.append(f"LLM 일시정지: {_fmt_ts(state.llm_paused_until_ts)} 까지 ({state.llm_pause_reason})")
        else:
            lines.append("LLM 일시정지: 없음")
        lines.append(f"포지션 {len(state.positions)}개:")
        for sym, p in sorted(state.positions.items()):
            mark = p.last_mark or p.entry_price
            pnl = (mark / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
            lines.append(f"  {sym}: {p.base_amount:.8g} @ {p.entry_price:.6g} → {mark:.6g} ({pnl:+.2f}%)  "
                         f"손절 {p.stop_loss_price:.6g} / 익절 {p.take_profit_price:.6g}  진입 사이클 {p.opened_cycle}")
        lines.append(f"청산 거래 누적: {len(state.closed_trades)}건")
    applied = dict(getattr(state, "control_applied_ts", None) or {}) if state is not None else {}
    for kind, name in (("stop", C.STOP_FILENAME), ("clear", C.CLEAR_FILENAME)):
        path = data_dir / C.CONTROL_DIRNAME / name
        info = read_control_file(path)
        cstate = control_file_state(kind, info, applied)
        if cstate == "pending":
            lines.append(f"STOP 대기: {info.get('reason') or '-'}" if kind == "stop"
                         else "CLEAR 대기: 다음 사이클에 킬스위치 해제")
        elif cstate == "unreadable":
            lines.append(f"{name}: 읽지 못함(다른 프로그램이 열고 있음) — 그 프로그램을 닫으면 다음 사이클에 처리됩니다")
        elif cstate == "handled":
            lines.append(f"{name}: 이미 처리함 — 파일을 지우지 못했습니다(다른 프로그램이 열고 있음). {path} 을 직접 지우세요")
    lock, label, _running = _describe_lock(data_dir, cycle_minutes)
    if lock is None:
        lines.append("실행 중 인스턴스: 없음")
    else:
        ts = lock.get("ts")
        ts_s = _fmt_ts(ts) if isinstance(ts, int) and not isinstance(ts, bool) else "-"
        lines.append(f"잠금: pid={lock.get('pid')} ({label}) 갱신 {ts_s}")
    ledger_p = data_dir / "cost_ledger.jsonl"
    from tradingbot.llm.cost import pending_ledger_path

    if ledger_p.is_file() or pending_ledger_path(ledger_p).is_file():
        try:
            from tradingbot.llm.cost import CostLedger

            led = CostLedger(ledger_p, daily_budget, monthly_budget)
            lines.append(f"LLM 비용: 오늘 ${led.spent_today():.4f} / ${daily_budget:.2f}, "
                         f"이달 ${led.spent_month():.4f} / ${monthly_budget:.2f}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"LLM 비용: 원장 읽기 실패 ({type(exc).__name__})")
    return "\n".join(lines) + "\n"


def _write_control(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"reason": reason, "ts": int(time.time() * 1000)}, sort_keys=True, ensure_ascii=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        fsutil.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def write_stop(data_dir: Path, reason: str) -> Path:
    """``control/STOP`` 작성(+ 대기 중 CLEAR 삭제). 다음 사이클에 킬스위치.

    CLEAR 를 지우지 못해도(다른 프로그램이 열고 있음) 실패로 보지 않는다 — 사이클은 시각 순으로 처리하므로
    방금 쓴(더 새) STOP 이 이긴다.
    """
    control = Path(data_dir) / C.CONTROL_DIRNAME
    stop = control / C.STOP_FILENAME
    _write_control(stop, reason or "cli")
    try:
        fsutil.unlink(control / C.CLEAR_FILENAME)
    except OSError as exc:
        log.warning("대기 중 CLEAR 삭제 실패(%s) — 더 새 STOP 이 우선합니다", type(exc).__name__)
    return stop


def write_clear(data_dir: Path, reason: str = "cli") -> Path:
    """``control/STOP`` 삭제 + ``control/CLEAR`` 작성. 다음 사이클에 킬스위치 해제.

    STOP 을 지우지 못해도 CLEAR 는 쓴다 — 사이클은 CLEAR 보다 오래된 STOP 을 적용하지 않고 지운다.
    """
    control = Path(data_dir) / C.CONTROL_DIRNAME
    try:
        fsutil.unlink(control / C.STOP_FILENAME)
    except OSError as exc:
        log.warning("대기 중 STOP 삭제 실패(%s) — 더 새 CLEAR 를 기록합니다", type(exc).__name__)
    clear = control / C.CLEAR_FILENAME
    _write_control(clear, reason)
    return clear


# ======================================================================= CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradingbot", description="Claude 멀티에이전트 Binance 현물 봇")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="사이클 루프 실행")
    r.add_argument("--once", action="store_true", help="한 사이클만 실행하고 종료")
    r.add_argument("--cycle-minutes", type=int, default=None, help="사이클 주기(분) 덮어쓰기")
    r.add_argument("--offline-fixture", action="store_true",
                   help="합성 시세(키·네트워크 없음, paper 전용)로 실행 — 체험/점검용(백엔드는 자동으로 mock)")
    r.add_argument("--mock-scenario", choices=("conservative", "approve"), default=None,
                   help="mock 백엔드 응답 시나리오(MOCK_SCENARIO 보다 우선). --offline-fixture 또는 AGENT_BACKEND=mock 전용")
    sub.add_parser("dashboard", help="로컬 대시보드 실행")
    b = sub.add_parser("backtest", help="규칙 전용 기준선 백테스트")
    b.add_argument("--days", type=int, default=90)
    b.add_argument("--symbols", nargs="+", default=None, help="종목(BTC/USDT ...). 쉼표 구분도 허용")
    sub.add_parser("status", help="상태 요약(네트워크 없음)")
    s = sub.add_parser("stop", help="킬스위치 요청(control/STOP)")
    s.add_argument("--reason", required=True)
    sub.add_parser("clear-stop", help="킬스위치 해제 요청(control/CLEAR)")
    sub.add_parser("notify-test", help="텔레그램 연결 테스트 메시지 1건 전송")
    return p


def _positive_int(v: int | None, name: str) -> None:
    if v is not None and v < 1:
        raise ConfigError(f"{name} 는 1 이상이어야 합니다 (현재 {v})")


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         input_fn: Callable[[str], str] | None = None, out: TextIO | None = None, err: TextIO | None = None,
         components_factory: Callable[..., Components] | None = None,
         stop_event: threading.Event | None = None, pid: int | None = None,
         http_post: Callable[[str, dict], object] | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "notify-test":
        # 텔레그램 값만 있으면 된다 — API 키·백엔드 설정이 없어도 연결을 확인할 수 있어야 한다(stop/clear-stop 처럼)
        return notify_test(env, out, err, http_post=http_post)
    offline = args.command == "run" and bool(getattr(args, "offline_fixture", False))
    try:
        settings_env: Mapping[str, str] | None = env
        if offline:
            # 오프라인 픽스처는 어차피 mock 백엔드로 돈다 — ANTHROPIC_API_KEY 없이 체험할 수 있게 검증도 mock 으로
            settings_env = {**(read_env(None) if env is None else env), "AGENT_BACKEND": "mock"}
        settings = load_settings(settings_env)
        if args.command == "run":
            _positive_int(args.cycle_minutes, "--cycle-minutes")
            if args.cycle_minutes is not None:
                settings = dataclasses.replace(settings, cycle_minutes=int(args.cycle_minutes))
            if getattr(args, "mock_scenario", None) and settings.agent_backend != "mock":
                raise ConfigError("--mock-scenario 는 --offline-fixture 또는 AGENT_BACKEND=mock 에서만 쓸 수 있습니다")
        if args.command == "backtest":
            _positive_int(args.days, "--days")
    except ConfigError as exc:
        if args.command in ("stop", "clear-stop"):
            # 제어 파일 쓰기는 DATA_DIR 만 있으면 된다 — 백엔드/키 설정이 깨져 있어도 킬스위치는 켜고 끌 수 있어야 한다
            err.write(f"설정 경고(무시하고 진행): {exc}\n")
            try:
                data_dir = _control_data_dir(env, err)
            except ConfigError as exc2:
                err.write(f"설정 오류: {exc2}\n")
                return EXIT_USAGE
            return _cmd_control(args, data_dir, out, err)
        if args.command == "status":
            # status 도 파일만 읽는다 — 키가 없거나 설정이 깨져 있어도 상태는 볼 수 있어야 한다
            err.write(f"설정 경고(무시하고 진행): {exc}\n")
            try:
                out.write(status_text_from_env(env, err=err))
            except ConfigError as exc2:
                err.write(f"설정 오류: {exc2}\n")
                return EXIT_USAGE
            return EXIT_OK
        err.write(f"설정 오류: {exc}\n")
        return EXIT_USAGE

    if args.command in ("stop", "clear-stop"):
        return _cmd_control(args, Path(settings.data_dir), out, err, cycle_minutes=settings.cycle_minutes)
    if args.command == "status":
        out.write(status_text(settings))
        return EXIT_OK
    if args.command == "dashboard":
        return _cmd_dashboard(settings, out)
    if args.command == "backtest":
        return _cmd_backtest(settings, args, out, err)
    # load_settings 와 같은 출처(os.environ 위에 .env)에서 읽는다 — .env 의 MOCK_SCENARIO 도 반영.
    # --mock-scenario 가 있으면 그것이 우선한다.
    src_env: Mapping[str, str] = read_env(None) if env is None else env
    mock_scenario = getattr(args, "mock_scenario", None) or (src_env.get("MOCK_SCENARIO") or "").strip() or None
    # input_fn 미주입(실제 CLI) → 기본 input() + live 확인은 TTY 에서만(파이프로 'LIVE' 주입 차단).
    require_tty = input_fn is None
    return _cmd_run(settings, args, input_fn=input_fn or input, out=out, err=err,
                    components_factory=components_factory, stop_event=stop_event, pid=pid,
                    mock_scenario=mock_scenario, require_tty=require_tty)


def notify_test(env: Mapping[str, str] | None, out: TextIO, err: TextIO, *,
                http_post: Callable[[str, dict], object] | None = None) -> int:
    """``notify-test``: ``TelegramTransport`` 로 테스트 메시지 1건을 동기 전송한다(재시도·큐 없음).

    설정 검증 없이 ``read_env()``(os.environ 위에 .env)에서 ``TELEGRAM_BOT_TOKEN``·``TELEGRAM_CHAT_ID``·``MODE`` 만 읽는다.
    출력·오류 어디에도 토큰을 쓰지 않는다(전송기 ``_scrub`` + 토큰 문자열 재확인). 종료 코드: 성공 0, 실패 1, 설정 없음 2.
    """
    from tradingbot.notify import TelegramTransport  # noqa: PLC0415 - 다른 명령의 import 비용을 늘리지 않는다

    try:
        src: Mapping[str, str] = _lenient_env(err) if env is None else env
    except ConfigError as exc:
        err.write(f"설정 오류: {exc}\n")
        return EXIT_USAGE
    token = (src.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (src.get("TELEGRAM_CHAT_ID") or "").strip()
    mode = (src.get("MODE") or "").strip().lower()[:16] or "paper"
    missing = [k for k, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)) if not v]
    if missing:
        err.write(f"텔레그램 설정 없음: {', '.join(missing)} 이(가) 비어 있습니다 — "
                  ".env 또는 환경변수에 설정한 뒤 다시 실행하세요\n")
        return EXIT_USAGE

    def _mask(text: str) -> str:
        return text.replace(token, mask_secret(token)) if token and token in text else text

    transport = TelegramTransport(token, chat_id, http_post=http_post)   # 둘 다 비어 있지 않음 → 예외 없음
    try:
        transport.send(NOTIFY_TEST_TEXT.format(mode=mode))
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 가린 메시지로 보고하고 1
        # TransportError 는 이미 토큰·URL 을 가린다. 다른 예외도 한 번 더 가린다.
        msg = _mask(transport._scrub(str(exc)))[:300]
        log.warning("notify-test 실패 type=%s", type(exc).__name__)
        detail = msg or type(exc).__name__
        prefix = "텔레그램 전송 실패: "
        if detail.startswith(prefix):  # TransportError 메시지가 이미 같은 접두어를 가짐
            detail = detail[len(prefix):]
        err.write(f"{prefix}{detail} (chat_id={mask_secret(chat_id)})\n")
        return EXIT_ERROR
    out.write(f"텔레그램 전송 성공 [{mode}] (chat_id={mask_secret(chat_id)}) — 채팅에서 테스트 메시지를 확인하세요\n")
    return EXIT_OK


def _control_data_dir(env: Mapping[str, str] | None, err: TextIO | None = None) -> Path:
    """설정 검증 없이 DATA_DIR 만 푼다(load_settings 와 같은 출처: os.environ 위에 .env, 기본 '.').

    ``.env`` 인코딩이 깨졌어도 관대하게 읽는다(경고는 ``err``). DATA_DIR 값 자체가 잘못됐으면 ``ConfigError``.
    """
    src: Mapping[str, str] = _lenient_env(err) if env is None else env
    return parse_data_dir((src.get("DATA_DIR") or "").strip())


def _lenient_env(err: TextIO | None) -> dict[str, str]:
    """``read_env(lenient=True)`` + 경고를 ``err`` 로(파일만 읽는 명령: status·stop·clear-stop·notify-test)."""
    warnings: list[str] = []
    src = read_env(None, lenient=True, warnings=warnings)
    for w in warnings:
        (err or sys.stderr).write(f"설정 경고: {w}\n")
    return src


def _cmd_control(args: argparse.Namespace, data_dir: Path, out: TextIO, err: TextIO | None = None, *,
                 cycle_minutes: int | None = None) -> int:
    """``stop`` / ``clear-stop``: control 파일만 쓴다.

    절대 경로를 보여 주고, 그 폴더에서 실행 중인 봇이 없으면 경고한다(봇이 다른 DATA_DIR 로 돌고 있으면
    이 요청은 적용되지 않는다). 쓰기 실패는 트레이스백 대신 안내 문구 + 종료 코드 1.
    """
    err = err or sys.stderr
    abs_dir = Path(os.path.abspath(data_dir))     # resolve() 는 하지 않는다(매핑·subst 드라이브 문자 유지)
    try:
        if args.command == "stop":
            path = write_stop(abs_dir, args.reason)
            msg = f"STOP 요청 기록: {path} — 다음 사이클에 킬스위치가 켜집니다\n"
        else:
            path = write_clear(abs_dir)
            msg = f"CLEAR 요청 기록: {path} — 다음 사이클에 킬스위치가 해제됩니다\n"
    except OSError as exc:
        err.write(f"제어 파일 쓰기 실패({type(exc).__name__}): {abs_dir / C.CONTROL_DIRNAME} — "
                  "다른 프로그램이 그 파일을 열고 있지 않은지 확인하고 잠시 뒤 다시 실행하세요\n")
        return EXIT_ERROR
    out.write(msg)
    lock, label, running = _describe_lock(abs_dir, cycle_minutes)
    if running and lock is not None:
        out.write(f"대상 봇: pid={lock.get('pid')} ({label})\n")
    else:
        err.write(f"경고: {abs_dir} 에서 실행 중인 봇을 찾지 못했습니다 — 봇이 다른 DATA_DIR(.env·환경변수)로 "
                  "실행 중이면 이 요청은 적용되지 않습니다(다음에 이 폴더로 시작하는 봇에는 적용)\n")
    return EXIT_OK


def _cmd_run(settings: Settings, args: argparse.Namespace, *, input_fn: Callable[[str], str], out: TextIO,
             err: TextIO, components_factory: Callable[..., Components] | None,
             stop_event: threading.Event | None, pid: int | None, mock_scenario: str | None = None,
             require_tty: bool = False) -> int:
    offline = bool(getattr(args, "offline_fixture", False))
    if offline and settings.mode != "paper":
        err.write("--offline-fixture 는 MODE=paper 에서만 쓸 수 있습니다\n")
        return EXIT_USAGE
    if offline:
        settings = offline_fixture_settings(settings)
        out.write(f"오프라인 픽스처: 합성 시세 · mock 백엔드 · 텔레그램 끔 — 데이터는 {settings.data_dir} "
                  "(실제 paper 상태와 분리)\n")
        log.warning("오프라인 픽스처: 격리 데이터 디렉터리 %s 사용(실제 paper 상태 미변경)", settings.data_dir)
    if settings.mode == "live":
        if require_tty and not _stdin_is_tty():
            err.write("MODE=live 는 대화형 콘솔(TTY)에서 직접 LIVE 를 입력해야 합니다 — "
                      "파이프/리다이렉트된 표준입력은 거부합니다. 실행하지 않습니다\n")
            log.warning("live 확인: 표준입력이 TTY 가 아님 — 실행 거부")
            return EXIT_REFUSED
        out.write(f"판단 백엔드: {settings.agent_backend} · 종목: {', '.join(settings.symbols)} · "
                  f"데이터: {os.path.abspath(settings.data_dir)}\n")
        if not confirm_live(input_fn, out):
            err.write("\nLIVE 확인 실패 — 실행하지 않습니다\n")
            log.warning("live 확인 입력 불일치 — 실행 거부")
            return EXIT_REFUSED
        out.write("\n")
    data_dir = Path(settings.data_dir)
    lock = BotLock(data_dir / C.LOCK_FILENAME, pid=pid, stale_after_s=lock_stale_after_s(settings.cycle_minutes))
    try:
        lock.acquire()
    except LockHeldError as exc:
        err.write(f"{exc}\n")
        return EXIT_LOCKED
    except OSError as exc:
        err.write(f"잠금 파일 처리 실패({type(exc).__name__}): {os.path.abspath(lock.path)} — 폴더 경로·권한을 "
                  "확인하고, 다른 프로그램이 그 파일을 열고 있다면 잠시 뒤 다시 실행하세요\n")
        return EXIT_ERROR
    log_handler = attach_log_file(Path(settings.log_file)) if getattr(settings, "log_file", None) else None
    stop = stop_event or threading.Event()
    prev = install_signal_handlers(stop)
    try:
        _log_locations(settings)
        factory = components_factory or build_components
        try:
            extra: dict[str, Any] = {}
            if offline:
                extra["offline_fixture"] = True
            if mock_scenario is not None:
                extra["mock_scenario"] = mock_scenario
            comp = factory(settings, lock=lock, **extra)
        except (ConfigError, OSError, ValueError) as exc:
            err.write(f"초기화 실패: {type(exc).__name__}: {exc}\n")
            return EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 - CorruptStateError 등
            err.write(f"초기화 실패: {type(exc).__name__}\n")
            return EXIT_ERROR
        log.info("시작 설정: %s", settings.redacted())
        st = comp.portfolio.state
        comp.notifier.notify(_event(settings, "startup", equity=_safe_equity(comp),
                                    reason=("1회 실행" if args.once else "루프 시작")
                                    + (" — 킬스위치 ON" if st.kill_switch else "")
                                    + _startup_recovery_note(lock, st, settings),
                                    extra={"symbols": list(settings.symbols), "cycle_minutes": settings.cycle_minutes,
                                           "backend": settings.agent_backend,
                                           **({"mock_scenario": mock_scenario}
                                              if settings.agent_backend == "mock" and mock_scenario else {}),
                                           **({"offline_fixture": True} if offline else {})}))
        with keep_system_awake():
            return run_loop(comp, once=bool(args.once), stop_event=stop, lock=lock)
    finally:
        _restore_signals(prev)
        lock.release()
        detach_log_file(log_handler)


def _log_locations(settings: Settings) -> None:
    """시작 시 실제로 쓰는 위치(절대 경로)를 남긴다 — 다른 폴더에서 실행해 .env 를 못 읽은 경우를 바로 알 수 있게."""
    env_path, env_found = dotenv_location()
    if env_found:
        log.info("설정 파일: %s", env_path)
    else:
        log.warning("작업 폴더 %s 에 .env 가 없습니다 — 환경변수와 기본값만 씁니다(봇 폴더에서 실행했는지 확인)",
                    os.getcwd())
        if Path(".env.txt").is_file():
            log.warning("'.env.txt' 가 있습니다 — 메모장이 확장자를 붙였을 수 있습니다. 이름을 '.env' 로 바꾸세요")
        elif Path(".env").is_dir():
            log.warning("'.env' 가 폴더입니다(가상환경 이름일 수 있음) — 설정 파일로 읽지 않습니다")
    log.info("데이터 폴더: %s", os.path.abspath(settings.data_dir))


def _startup_recovery_note(lock: BotLock, state: Any, settings: Settings) -> str:
    """시작 알림에 붙일 복구 안내: 낡은 잠금을 가져왔으면(이전 실행이 비정상 종료) + 마지막 사이클이 오래됐으면."""
    parts: list[str] = []
    took = getattr(lock, "took_over", None)
    if isinstance(took, dict):
        ts = took.get("ts")
        when = f", 마지막 갱신 {_fmt_ts(ts)}" if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0 else ""
        parts.append(f"이전 실행이 정상 종료되지 않았습니다(잠금 인수: pid {took.get('pid')}{when})")
    last = getattr(state, "last_cycle_ts", None)
    if isinstance(last, int) and not isinstance(last, bool) and last > 0:
        age_s = time.time() - last / 1000.0
        period_s = max(1, int(settings.cycle_minutes)) * 60.0
        n_pos = len(getattr(state, "positions", None) or {})
        if age_s > CYCLE_GAP_FACTOR * period_s and n_pos:
            parts.append(f"마지막 사이클 {age_s / 60.0:.0f}분 전 — 보유 {n_pos}개는 그동안 손절·익절이 집행되지 않았습니다")
    return "".join(f" — {p}" for p in parts)


def _cmd_dashboard(settings: Settings, out: TextIO) -> int:  # pragma: no cover - 서버 실행
    import uvicorn

    from tradingbot.dashboard.app import create_app

    out.write(f"대시보드: http://{settings.dashboard_host}:{settings.dashboard_port}\n")
    # timeout_graceful_shutdown: Windows Proactor 루프에서 리셋된 연결이 남으면 Ctrl+C 뒤 wait_closed() 가 끝나지
    # 않는다(CPython _call_connection_lost 의 shutdown 이 WinError 10054) — 5초 뒤 남은 요청을 끊고 종료한다.
    uvicorn.run(create_app(settings, Path(settings.data_dir)), host=settings.dashboard_host,
                port=settings.dashboard_port, log_level="warning", timeout_graceful_shutdown=5)
    return EXIT_OK


def _parse_symbols(raw: Sequence[str] | None, default: Sequence[str]) -> list[str]:
    if not raw:
        return list(default)
    out: list[str] = []
    for item in raw:
        for s in str(item).split(","):
            s = s.strip().upper()
            if s and s not in out:
                out.append(s)
    return out


def _cmd_backtest(settings: Settings, args: argparse.Namespace, out: TextIO, err: TextIO,
                  market: Any | None = None) -> int:
    from tradingbot import backtest

    symbols = _parse_symbols(args.symbols, settings.symbols)
    data_dir = Path(settings.data_dir)
    cfg = backtest.BacktestConfig(symbols=symbols, days=int(args.days), start_balance=settings.paper_start_balance,
                                  cache_dir=data_dir / "backtest_cache", out_dir=data_dir / "backtest_results")
    if market is None:  # pragma: no cover - 네트워크
        from tradingbot.exchange import CcxtMarketData

        market = CcxtMarketData(settings)
    try:
        result = backtest.run(cfg, market=market)
    except Exception as exc:  # noqa: BLE001
        err.write(f"백테스트 실패: {type(exc).__name__}: {exc}\n")
        return EXIT_ERROR
    verdict = getattr(result, "verdict", "?")
    out.write(f"백테스트 완료: {', '.join(symbols)} {args.days}일 — {verdict}\n")
    if getattr(result, "json_path", None):
        out.write(f"결과: {result.json_path}\n")
    reasons = getattr(result, "reasons", None)
    if reasons:
        out.write("사유: " + "; ".join(str(r) for r in reasons) + "\n")
    return EXIT_OK


def prepare_stdio(streams: Sequence[object] | None = None) -> None:
    """파일·파이프로 리다이렉트된 표준출력/표준에러는 UTF-8 로 쓴다.

    Windows 는 리다이렉트된 스트림에 로캘 인코딩(한국어 Windows 는 cp949)을 쓰는데, cp949 는 알림 로그의
    이모지(🟢 등)를 인코딩하지 못해 로그 오류가 난다. 콘솔(TTY)은 그대로 둔다(Windows 콘솔은 유니코드 API 사용).
    """
    for stream in (streams if streams is not None else (sys.stdout, sys.stderr)):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            isatty = getattr(stream, "isatty", None)
            if callable(isatty) and isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 스트림 준비 실패는 실행을 막지 않는다
            continue


class _LogPump:
    """루트 로거 → 무제한 큐 → 전용 데몬 스레드 → 핸들러(콘솔용 하나, ``LOG_FILE`` 용 하나 — 따로 돈다).

    Windows 콘솔(conhost)에서 창을 클릭·드래그해 선택 모드(QuickEdit·Ctrl+A·Ctrl+M·Pause)에 들어가면 콘솔 쓰기
    (WriteConsoleW)가 선택이 끝날 때까지 막힌다. 로그를 거래 스레드에서 직접 쓰면 손절·익절·시간손절을 포함한
    사이클 전체가 그동안 멈추므로, 거래 스레드는 큐에 넣기만 하고 막히는 것은 이 스레드뿐이게 한다.
    큐는 무제한(``SimpleQueue``): 크기 제한이 있으면 가득 찼을 때 ``handleError`` 가 호출 스레드에서 stderr 에 쓴다.
    """

    def __init__(self, handlers: Sequence[logging.Handler]) -> None:
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.handlers: list[logging.Handler] = list(handlers)
        self._thread = threading.Thread(target=self._run, name="log-pump", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while True:
            record = self.queue.get()
            if record is None:
                return
            for h in list(self.handlers):
                try:
                    if record.levelno >= h.level:
                        h.handle(record)
                except Exception:  # noqa: BLE001 - 로그 출력 실패가 펌프를 멈추면 안 된다
                    pass

    def add_handler(self, handler: logging.Handler) -> None:
        self.handlers.append(handler)

    def remove_handler(self, handler: logging.Handler) -> None:
        with contextlib.suppress(ValueError):
            self.handlers.remove(handler)

    def stop(self, timeout_s: float = 2.0) -> bool:
        """남은 기록을 내보내고 끝낸다. 출력이 막혀 있으면 ``timeout_s`` 뒤 포기하고 False(데몬 스레드는 남는다)."""
        self.queue.put(None)
        self._thread.join(timeout_s)
        return not self._thread.is_alive()


_LOG_PUMP: _LogPump | None = None


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> _LogPump:
    """콘솔 로그를 전용 스레드로 내보내도록 루트 로거를 구성한다(``cli()`` 가 부른다; 테스트는 caplog 를 쓴다)."""
    global _LOG_PUMP
    console = logging.StreamHandler(stream)        # None → sys.stderr
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    pump = _LogPump([console])
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(logging.handlers.QueueHandler(pump.queue))
    root.setLevel(level)
    logging.captureWarnings(True)        # warnings.showwarning 은 sys.stderr 에 직접 쓴다
    logging.raiseExceptions = False      # 핸들러 오류 트레이스백을 호출(거래) 스레드에서 stderr 에 쓰지 않는다
    pump.start()
    _LOG_PUMP = pump
    return pump


def attach_log_file(path: Path) -> logging.Handler | None:
    """``LOG_FILE`` → UTF-8(BOM, 메모장·PowerShell ``Get-Content`` 가 바로 읽음) 추가 쓰기 로그 파일 핸들러.

    콘솔 펌프가 있으면(``cli()``) 파일에도 자기 큐·스레드를 따로 준다 — 파일 쓰기가 거래 스레드를 막지 않고,
    콘솔이 선택 모드로 막혀도 파일에는 계속 쓰인다. 열지 못하면 경고만 하고 None.
    회전(rotate)은 하지 않는다 — Windows 는 다른 프로그램이 연 파일의 이름을 바꿀 수 없다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8-sig")
    except OSError as exc:
        log.warning("로그 파일을 열지 못함(%s): %s — 콘솔 로그만 씁니다", type(exc).__name__, path)
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    if _LOG_PUMP is not None:
        pump = _LogPump([handler])
        pump.start()
        feed = logging.handlers.QueueHandler(pump.queue)
        handler._tb_pump = (pump, feed)          # type: ignore[attr-defined]
        logging.getLogger().addHandler(feed)
    else:
        logging.getLogger().addHandler(handler)
    return handler


def detach_log_file(handler: logging.Handler | None, *, timeout_s: float = 5.0) -> None:
    """``attach_log_file`` 을 되돌린다. 큐에 남은 기록(마지막 '치명 오류로 중단'·'잠금 해제' 등)을 다 쓴 뒤 닫는다."""
    if handler is None:
        return
    root = logging.getLogger()
    pumped = getattr(handler, "_tb_pump", None)
    if pumped is not None:
        pump, feed = pumped
        root.removeHandler(feed)             # 새 기록은 더 받지 않고
        if not pump.stop(timeout_s):         # 받은 것은 다 쓴다(파일이 막혀 못 끝내면 닫지 않고 둔다)
            return
    root.removeHandler(handler)
    with contextlib.suppress(Exception):
        handler.close()


def _exit_past_blocked_console(code: int) -> None:
    """콘솔 쓰기가 막힌 채(conhost 선택 모드) 끝낼 때: 인터프리터 종료(``logging.shutdown``·stderr 비우기)는 막힌
    펌프 스레드가 쥔 콘솔 핸들러·stderr 잠금을 기다리며 멈춘다 — 상태 저장·잠금 해제·알림은 이미 끝났으므로
    리디렉션된 stdout 만 비우고 바로 끝낸다."""
    with contextlib.suppress(Exception):
        if not sys.stdout.isatty():
            sys.stdout.flush()
    os._exit(code)


def cli() -> int:  # pragma: no cover - 콘솔 진입점(`tradingbot` 명령, `python -m tradingbot.main`)
    prepare_stdio()
    pump = configure_logging()
    code = EXIT_ERROR
    try:
        code = main()
        return code
    finally:
        if not pump.stop():
            _exit_past_blocked_console(code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
