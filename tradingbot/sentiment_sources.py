"""시장 심리 외부 소스 (Fear & Greed Index).

- ``FearGreedFetcher()()`` 는 오늘의 Crypto Fear & Greed 값(0..100 정수)을 돌려준다.
  응답 JSON: ``{"data": [{"value": "34", ...}]}``.
- 메모리와 ``cache_path`` (JSON ``{"value", "fetched_ts"}``)에 캐시하고 ``ttl_s`` 안에서는
  다시 받지 않는다.
- 네트워크/파싱 오류·범위 밖 값이면 마지막 캐시 값을 돌려준다(오래됐어도 24시간까지).
  쓸 캐시도 없으면 ``None``. **절대 예외를 올리지 않는다.**
- ``http_get(url, timeout_s) -> str`` 을 주입할 수 있다(테스트). 기본은 urllib(User-Agent)이고 **전체** 소요 시간을
  ``timeout_s`` 로 자른다(데몬 스레드; 소켓 연산별 타임아웃만으로는 바이트를 흘리는 서버·DNS 정체를 못 막는다).
- 받기(또는 파싱)에 실패하면 ``retry_after_failure_s``(기본 1800s) 동안은 다시 받지 않고 캐시/None 을 돌려준다
  (실패도 캐시 — 멈춘 엔드포인트가 매 사이클 스캔 시간을 잡아먹지 않게).
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradingbot import fsutil

log = logging.getLogger("tradingbot.sentiment_sources")

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
USER_AGENT = "tradingbot/0.1 (+fng; python-urllib)"
DEFAULT_TIMEOUT_S = 10.0
MAX_STALE_S = 24 * 3600
MAX_BODY_BYTES = 64 * 1024            # 응답 본문 상한(정상 응답은 수백 바이트)
DEFAULT_RETRY_AFTER_FAILURE_S = 1800.0  # 받기 실패 뒤 이 시간 동안은 다시 받지 않는다(매 사이클 지연 반복 방지)


def _urllib_get(url: str, timeout_s: float, deadline: float) -> str:  # pragma: no cover - 네트워크
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    chunks: list[bytes] = []
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - https 고정
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("fear&greed 응답이 전체 제한 시간을 넘김")
            chunk = resp.read1(MAX_BODY_BYTES) if hasattr(resp, "read1") else resp.read(MAX_BODY_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > MAX_BODY_BYTES:
                raise ValueError("fear&greed 응답이 너무 큼")
    return b"".join(chunks).decode("utf-8", errors="replace")


def default_http_get(url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """urllib으로 URL 본문을 받는다(User-Agent). **전체** 소요 시간을 ``timeout_s`` 로 제한한다.

    urllib 의 ``timeout`` 은 소켓 연산(connect/recv) 하나하나에 걸리고 DNS 조회에는 걸리지 않으므로, 바이트를
    조금씩 흘리는 서버나 멈춘 DNS 가 동기 스캔을 무한정 붙잡을 수 있다. 그래서 받기는 데몬 스레드에서 하고
    호출자는 최대 ``timeout_s`` 만 기다린다(넘으면 ``TimeoutError``; 남은 스레드는 소켓 타임아웃으로 스스로 끝난다).
    """
    limit = float(timeout_s)
    if not (limit > 0) or not math.isfinite(limit):
        limit = DEFAULT_TIMEOUT_S
    deadline = time.monotonic() + limit
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["text"] = _urllib_get(url, limit, deadline)
        except BaseException as e:  # noqa: BLE001 - 호출자에게 넘긴다
            box["error"] = e

    t = threading.Thread(target=work, name="fear-greed-get", daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        raise TimeoutError(f"fear&greed 받기가 {limit:g}s 안에 끝나지 않음")
    if "error" in box:
        raise box["error"]
    return box.get("text", "")


def _coerce_value(raw: Any) -> int | None:
    """0..100 정수로 바꾼다. 아니면 None."""
    if isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, str):
            raw = raw.strip()
            num = float(raw)
        elif isinstance(raw, (int, float)):
            num = float(raw)
        else:
            return None
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num != int(num):
        return None
    val = int(num)
    if not 0 <= val <= 100:
        return None
    return val


def parse_fear_greed(text: str) -> int | None:
    """API 응답 본문에서 값을 꺼낸다. 형식이 틀리거나 범위 밖이면 None."""
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    data = doc.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    return _coerce_value(data[0].get("value"))


class FearGreedFetcher:
    """Fear & Greed 값을 캐시와 함께 가져오는 호출 가능 객체. 예외를 올리지 않는다."""

    def __init__(
        self,
        cache_path: Path | None = None,
        ttl_s: int = 3600,
        *,
        http_get: Callable[[str, float], str] | None = None,
        url: str = FEAR_GREED_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
        retry_after_failure_s: float = DEFAULT_RETRY_AFTER_FAILURE_S,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.ttl_s = ttl_s
        self.http_get = http_get or default_http_get
        self.url = url
        self.timeout_s = timeout_s
        self.clock = clock
        self.retry_after_failure_s = retry_after_failure_s
        self._mem: tuple[int, float] | None = None  # (value, fetched_ts)
        self._failed_at: float | None = None        # 마지막 받기 실패 시각(clock 기준)

    # ------------------------------------------------------------ 캐시
    def _load_file(self) -> tuple[int, float] | None:
        if self.cache_path is None:
            return None
        try:
            if not self.cache_path.exists():
                return None
            doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                return None
            val = _coerce_value(doc.get("value"))
            ts = doc.get("fetched_ts")
            if val is None or isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
                return None
            return val, float(ts)
        except Exception as e:
            log.debug("fear&greed 캐시 읽기 실패: %s", type(e).__name__)
            return None

    def _save_file(self, value: int, ts: float) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            tmp.write_text(json.dumps({"value": value, "fetched_ts": ts}), encoding="utf-8")
            fsutil.replace(tmp, self.cache_path)
        except Exception as e:
            log.warning("fear&greed 캐시 저장 실패: %s", type(e).__name__)

    def _cached(self) -> tuple[int, float] | None:
        if self._mem is None:
            self._mem = self._load_file()
        return self._mem

    # ------------------------------------------------------------ 호출
    def __call__(self) -> int | None:
        try:
            return self._fetch()
        except Exception as e:  # 최후의 방어선: 절대 예외를 올리지 않는다
            log.warning("fear&greed 조회 실패: %s", type(e).__name__)
            return None

    def _fetch(self) -> int | None:
        now = float(self.clock())
        cached = self._cached()
        if cached is not None:
            age = now - cached[1]
            if 0 <= age < self.ttl_s:
                return cached[0]

        value: int | None = None
        backoff = self._failed_at is not None and 0 <= now - self._failed_at < float(self.retry_after_failure_s)
        if backoff:
            log.debug("fear&greed 최근 실패 → %.0fs 동안 다시 받지 않음", float(self.retry_after_failure_s))
        else:
            try:
                text = self.http_get(self.url, self.timeout_s)
                value = parse_fear_greed(text)
                if value is None:
                    log.warning("fear&greed 응답 파싱 실패 또는 범위 밖 값")
            except Exception as e:
                log.warning("fear&greed 받기 실패: %s", type(e).__name__)

        if value is not None:
            self._failed_at = None
            self._mem = (value, now)
            self._save_file(value, now)
            return value
        if not backoff:
            self._failed_at = now

        if cached is not None and abs(now - cached[1]) <= MAX_STALE_S:
            log.info("fear&greed 오래된 캐시 사용 (age=%.0fs)", now - cached[1])
            return cached[0]
        return None
