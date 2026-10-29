"""파일 교체·삭제·추가 쓰기의 Windows 공유 위반 재시도.

Windows 에서는 다른 프로세스(대시보드·``status`` 명령·백신 실시간 검사·검색 색인·동기화 프로그램·
파일을 연 Excel 등)가 파일을 열고 있는 동안 ``os.replace``(MoveFileExW)·``os.unlink``(DeleteFileW)가
``PermissionError``(WinError 5 ACCESS_DENIED / 32 SHARING_VIOLATION / 33 LOCK_VIOLATION)로 거부된다
(CPython 의 ``open()`` 은 ``FILE_SHARE_DELETE`` 를 주지 않는다). 대부분 수 ms~수 초 안에 풀리는
일시적 경합이라, 여기서 짧게 기다렸다가 다시 시도한다.

POSIX 에서는 이런 일시적 거부가 없다 — ``winerror`` 가 없는 예외는 재시도하지 않고 그대로 올린다
(리눅스 동작 불변). 재시도 후에도 실패하면 원래 예외를 올린다(호출자의 기존 오류 처리 그대로).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

log = logging.getLogger("tradingbot.fsutil")

__all__ = [
    "WINDOWS_SHARING_ERRORS", "DEFAULT_BUDGET_S", "is_transient_sharing_error", "retry_delays",
    "retry", "replace", "unlink", "append_bytes",
]

#: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
WINDOWS_SHARING_ERRORS = frozenset({5, 32, 33})
#: 기본 재시도 총 대기(초). 상태 파일처럼 잃으면 안 되는 곳은 호출자가 더 길게 준다.
DEFAULT_BUDGET_S = 3.0
_FIRST_DELAY_S = 0.05
_MAX_DELAY_S = 1.0


def is_transient_sharing_error(exc: BaseException, *, errno_only: bool = False) -> bool:
    """Windows 의 일시적 공유 위반인가(다른 프로세스가 파일을 열고 있음).

    ``os.replace``/``os.unlink`` 는 Windows 에서 항상 Win32 오류 코드(``winerror``)를 싣는다 — 그 코드가 5/32/33 일
    때만 공유 위반이다. ``open()`` 은 C 런타임을 거쳐 ``errno`` 13 만 싣고 ``winerror`` 가 비므로, 추가 쓰기처럼
    ``open()`` 실패를 판정할 때는 ``errno_only=True`` 로 Windows 의 ``winerror`` 없는 ``PermissionError`` 도
    공유 위반으로 본다. 다른 OS 의 ``PermissionError`` 는 진짜 권한 문제라 False.
    """
    if not isinstance(exc, PermissionError):
        return False
    win = getattr(exc, "winerror", None)
    if win is not None:
        return win in WINDOWS_SHARING_ERRORS
    return errno_only and os.name == "nt"


def retry_delays(budget_s: float) -> Iterator[float]:
    """0.05 s 부터 두 배씩(최대 1 s) 늘린 대기 시간들. 합이 ``budget_s`` 를 넘지 않는다."""
    spent = 0.0
    delay = _FIRST_DELAY_S
    while spent + 1e-9 < budget_s:
        d = min(delay, _MAX_DELAY_S, budget_s - spent)
        yield d
        spent += d
        delay *= 2.0


def retry(op: Callable[[], None], *, what: str, path: object, budget_s: float = DEFAULT_BUDGET_S,
          sleep: Callable[[float], None] = time.sleep, errno_only: bool = False) -> None:
    """``op()`` 을 실행하고, Windows 공유 위반이면 최대 ``budget_s`` 초 동안 다시 시도한다(그 밖의 예외는 그대로).

    ``errno_only``: ``op`` 이 ``open()`` 으로 실패할 수 있으면 True(:func:`is_transient_sharing_error` 참고).
    """
    delays = retry_delays(budget_s)
    tries = 0
    while True:
        try:
            op()
            if tries:
                log.info("%s 성공 (공유 위반 재시도 %d회 후): %s", what, tries, path)
            return
        except OSError as exc:
            if not is_transient_sharing_error(exc, errno_only=errno_only):
                raise
            delay = next(delays, None)
            if delay is None:
                log.warning("%s 실패 — 다른 프로그램이 파일을 열고 있음(재시도 %d회, winerror=%s): %s",
                            what, tries, getattr(exc, "winerror", None), path)
                raise
            tries += 1
            sleep(delay)


def replace(src: str | os.PathLike, dst: str | os.PathLike, *, budget_s: float = DEFAULT_BUDGET_S,
            sleep: Callable[[float], None] = time.sleep) -> None:
    """``os.replace(src, dst)`` + Windows 공유 위반이면 최대 ``budget_s`` 초 재시도."""
    retry(lambda: os.replace(src, dst), what="파일 교체", path=dst, budget_s=budget_s, sleep=sleep)


def unlink(path: str | os.PathLike, *, missing_ok: bool = True, budget_s: float = DEFAULT_BUDGET_S,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """``Path(path).unlink(missing_ok=...)`` + Windows 공유 위반이면 최대 ``budget_s`` 초 재시도."""
    p = Path(path)
    retry(lambda: p.unlink(missing_ok=missing_ok), what="파일 삭제", path=p, budget_s=budget_s, sleep=sleep)


def append_bytes(path: str | os.PathLike, data: bytes, *, budget_s: float = DEFAULT_BUDGET_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
    """``data`` 를 파일 끝에 추가한다(없으면 만든다) + Windows 공유 위반이면 최대 ``budget_s`` 초 재시도.

    쓰기 잠금으로 파일을 연 프로그램(Excel 등)이 있으면 ``open('ab')`` 자체가 거부된다.
    """
    p = Path(path)

    def _op() -> None:
        with p.open("ab") as fh:
            fh.write(data)

    retry(_op, what="파일 추가 쓰기", path=p, budget_s=budget_s, sleep=sleep, errno_only=True)
