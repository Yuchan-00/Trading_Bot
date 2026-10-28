"""텔레그램 알림 (SPEC §11). 모든 거래 이벤트는 텔레그램으로 간다.

구성
- ``Transport``          전송 프로토콜. ``send(text)``는 실패 시 예외를 던진다.
- ``TelegramTransport``  Bot API ``sendMessage``로 전송. ``http_post``를 주입할 수 있어 오프라인 테스트가 가능하다.
                         repr/str/로그/예외 메시지 어디에도 토큰(URL 포함)이 나오지 않는다.
- ``Notifier``           ``TradeEvent``를 한국어로 포맷해 보낸다. ``async_send=True``면 큐에 넣고 즉시 반환하고
                         데몬 워커 스레드가 전송한다. 실패하면 1s/3s/9s 간격으로 재시도하고(지연은 주입 가능),
                         끝내 실패하면 ``failed_log``(jsonl)에 이벤트와 오류를 남기고 ERROR 로그를 낸다.
                         HTTP 429(flood control)면 ``retry_after``(본문 parameters.retry_after 또는 Retry-After
                         헤더)만큼 기다리고(최대 ``RATE_LIMIT_MAX_WAIT_S``), 그 재시도는 재시도 예산에서 빼지 않는다
                         (단, 레이트 리밋 추가 시도는 ``RATE_LIMIT_MAX_EXTRA_ATTEMPTS``회까지).

불변식: 알림 실패는 절대 호출자(사이클)로 전파되지 않는다. ``notify()``는 예외를 던지지 않는다.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import HardLimits, mask_secret
from .models import TradeEvent

log = logging.getLogger("tradingbot.notify")

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_TEXT = 4096
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0, 9.0)
THESIS_MAX_CHARS = 200
RATE_LIMIT_MAX_WAIT_S = 60.0          # 429 retry_after 대기 상한
RATE_LIMIT_MAX_EXTRA_ATTEMPTS = 5     # 429 로 인한 추가 시도 상한(재시도 예산과 별도)
REVIEW_TEXT_MAX_CHARS = 1000          # 일일 리뷰 본문(저널 telegram_text ≤400자 + 날짜 접두) 상한, 줄바꿈 유지

EMOJI: dict[str, str] = {
    "entry": "🟢",
    "exit_loss": "🔴",
    "exit_gain": "🔵",
    "partial_exit": "🟠",
    "order_rejected": "⚠️",
    "kill_switch": "⛔",
    "budget_exceeded": "💸",
    "daily_summary": "📊",
    "error": "❗",
    "startup": "▶️",
    "shutdown": "⏹",
    "llm_paused": "⏸",
    "post_trade_review": "📝",
    "halt": "⛔",
    "halt_lifted": "✅",
    "heartbeat": "💓",
    "alert": "🔔",
    "regime": "🌐",
    "news_risk": "📰",
    "position_health": "🩺",
}

# G0 중단 사유 코드 → 한국어 설명 (risk.global_gates / gates.g0_preflight 의 괄호 코드)
HALT_REASON_KO: dict[str, str] = {
    "daily_loss": "당일 손실 한도(−3%) 도달",
    "daily_loss_limit": "당일 실현손실 한도(−3%) 도달",
    "daily_loss_limit_equity": "당일 자본 손실 한도(−3%) 도달",
    "consecutive_losses": "연속 손실 한도(3연패) 도달",
    "drawdown_block": "낙폭 차단 단계(−4.5%) — 신규 진입 금지",
    "drawdown_stop_all": "낙폭 전면 중단 단계(−6%) — 사람이 해제해야 함",
    "kill_switch": "킬스위치 활성",
    "llm_budget": "LLM 예산 소진",
    "llm_paused": "LLM 일시정지 중",
    "max_positions": "최대 보유 포지션 수 도달",
    "funding_window": "펀딩 정산 창(±1분)",
    "invalid_equity": "자본 값 이상",
    "invalid_start_equity": "당일 시작자본 값 이상",
    "invalid_realized_pnl": "당일 실현손익 값 이상",
}
HALT_DETAIL_MAX_CHARS = 500     # cycle._notify_halts 가 사유를 500자로 자른다 — 구 error 형식과 같은 길이

# 체결 후 상태 저장 실패(extra.state_saved == False) 경고 줄 — 진입/청산 메시지 헤더 바로 아래에 붙인다
STATE_SAVE_WARNING = "⚠️ 상태 저장 실패 — 체결은 됐으나 장부 미저장, 수동 확인 필요"

EXIT_REASON_KO: dict[str, str] = {
    "stop_loss": "손절",
    "take_profit": "익절",
    "time_stop": "시간손절",
    "head_sell": "헤드 매도",
    "kill_switch": "킬스위치",
    "manual": "수동",
    "partial": "부분",
}


class TransportError(Exception):
    """전송 실패. 메시지에 비밀값이 들어 있지 않다."""


class TransportRateLimited(TransportError):
    """HTTP 429. ``retry_after``(초)는 서버가 요구한 대기 시간(알 수 없으면 None)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: object) -> float | None:
    """retry_after 값(초) 해석. 숫자·숫자 문자열만 받는다. 음수/NaN/해석 불가 → None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f or f < 0 or f == float("inf"):
        return None
    return f


def _retry_after_from_body(body: object) -> float | None:
    """Bot API 오류 본문(dict / JSON str·bytes)에서 parameters.retry_after 를 꺼낸다."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return None
    if isinstance(body, dict):
        params = body.get("parameters")
        if isinstance(params, dict):
            return _parse_retry_after(params.get("retry_after"))
    return None


# ---------------------------------------------------------------- 전송
@runtime_checkable
class Transport(Protocol):
    def send(self, text: str) -> None:  # 실패 시 예외
        ...


def _urllib_post(url: str, payload: dict, timeout_s: float) -> int | tuple[int, float | None]:
    """기본 HTTP POST(JSON). 상태 코드를 돌려준다. HTTPError도 상태 코드로 바꾼다.

    HTTP 429면 ``(429, retry_after)`` 를 돌려준다(본문 parameters.retry_after 우선, 없으면 Retry-After 헤더).
    """
    import urllib.error
    import urllib.request

    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - https 고정
            return int(resp.status)
    except urllib.error.HTTPError as e:
        code = int(e.code)
        if code != 429:
            return code
        retry_after: float | None = None
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001 - 본문이 없거나 읽기 실패
            raw = None
        if raw:
            retry_after = _retry_after_from_body(raw)
        if retry_after is None:
            try:
                headers = e.headers if e.headers is not None else e.hdrs
                retry_after = _parse_retry_after(headers.get("Retry-After") if headers is not None else None)
            except Exception:  # noqa: BLE001
                retry_after = None
        return (code, retry_after)


class TelegramTransport:
    """텔레그램 Bot API ``sendMessage`` 전송.

    parse_mode는 쓰지 않는다(이스케이프 문제 회피). ``disable_web_page_preview=True``.
    ``http_post(url, payload) -> status_code``를 주입하면 네트워크 없이 테스트할 수 있다.
    ``http_post`` 는 ``(status_code, extra)`` 를 돌려줄 수도 있다. extra 는 retry_after(초, 숫자) 또는
    Bot API 응답 본문(dict / JSON str·bytes)이며, 429일 때 ``TransportRateLimited.retry_after`` 로 쓰인다.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http_post: Callable[[str, dict], object] | None = None,
        timeout_s: float = 10,
    ) -> None:
        if not token:
            raise ValueError("텔레그램 토큰이 비어 있음")
        if chat_id is None or str(chat_id) == "":
            raise ValueError("텔레그램 chat_id가 비어 있음")
        self._token = str(token)
        self._chat_id = str(chat_id)
        self._timeout_s = float(timeout_s)
        self._http_post = http_post
        self._url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"

    # 비밀 보호 -------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        """예외 메시지 등에서 토큰(그리고 토큰이 든 URL)을 가린다."""
        text = str(text)
        if self._url in text:
            text = text.replace(self._url, "<telegram-url>")
        if self._token and self._token in text:
            text = text.replace(self._token, mask_secret(self._token))
        return text

    def __repr__(self) -> str:
        return (
            f"TelegramTransport(token={mask_secret(self._token)}, "
            f"chat_id={mask_secret(self._chat_id)}, timeout_s={self._timeout_s})"
        )

    __str__ = __repr__

    # 전송 ----------------------------------------------------------------------
    def send(self, text: str) -> None:
        text = str(text)
        if len(text) > TELEGRAM_MAX_TEXT:
            text = text[: TELEGRAM_MAX_TEXT - 1] + "…"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            if self._http_post is not None:
                status = self._http_post(self._url, payload)
            else:
                status = _urllib_post(self._url, payload, self._timeout_s)
        except Exception as e:  # noqa: BLE001 - 원인 예외는 URL(토큰)을 품을 수 있어 체인을 끊는다
            raise TransportError(
                f"텔레그램 전송 실패: {type(e).__name__}: {self._scrub(str(e))}"
            ) from None
        extra: object = None
        if isinstance(status, (tuple, list)) and len(status) == 2:
            status, extra = status[0], status[1]
        try:
            if isinstance(status, bool):
                raise TypeError("bool status")
            status_int = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise TransportError(f"텔레그램 응답 상태를 해석할 수 없음: {self._scrub(repr(status))}") from None
        if status_int == 429:
            if isinstance(extra, (int, float, str)) and not isinstance(extra, bool) \
                    and _parse_retry_after(extra) is not None:
                retry_after = _parse_retry_after(extra)
            else:
                retry_after = _retry_after_from_body(extra)
            suffix = f" (retry_after={retry_after:g}s)" if retry_after is not None else ""
            raise TransportRateLimited(f"텔레그램 응답 상태 429{suffix}", retry_after)
        if status_int != 200:
            raise TransportError(f"텔레그램 응답 상태 {status_int}")
        log.debug("텔레그램 전송 완료 (%d자)", len(text))


# ---------------------------------------------------------------- 포맷 도우미
def _fmt_price(x: float | None) -> str:
    """가격: 천 단위 구분, 크기에 따라 소수 자릿수 조절 (83,084.2 / 150.12 / 0.5123)."""
    if x is None:
        return "-"
    ax = abs(float(x))
    if ax >= 1000:
        return f"{x:,.1f}"
    if ax >= 1:
        return f"{x:,.2f}"
    if ax >= 0.01:
        return f"{x:,.4f}"
    return f"{x:,.6f}"


def _fmt_quote(x: float | None) -> str:
    """USDT 금액(자본·주문 금액): 천 단위 구분, 소수 1자리 (9,996.2)."""
    if x is None:
        return "-"
    return f"{x:,.1f}"


def _fmt_pnl(x: float | None) -> str:
    """손익 USDT: 부호 포함, 소수 2자리 (-3.77)."""
    if x is None:
        return "-"
    return f"{x:+,.2f}"


def _fmt_pct(x: float | None) -> str:
    """비율(소수) -> 퍼센트 문자열, 부호 포함, 소수 2자리 (0.0205 -> +2.05%)."""
    if x is None:
        return "-"
    return f"{x * 100:+.2f}%"


def _fmt_amount(x: float | None) -> str:
    """수량: 천 단위 구분, 뒤따르는 0 제거 (0.00481 / 1.5 / 12)."""
    if x is None:
        return "-"
    s = f"{x:,.8f}".rstrip("0").rstrip(".")
    return s or "0"


def _truncate_multiline(s: str | None, limit: int) -> str:
    """줄바꿈을 유지하며 줄마다 공백을 정리하고 ``limit`` 자로 자른다(빈 줄 제거)."""
    lines = [" ".join(line.split()) for line in (s or "").splitlines()]
    out = "\n".join(line for line in lines if line)
    if len(out) <= limit:
        return out
    return out[: limit - 1] + "…"


def _truncate(s: str | None, limit: int = THESIS_MAX_CHARS) -> str:
    s = (s or "").strip()
    s = " ".join(s.split())  # 줄바꿈·연속 공백 정리
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return str(ts)


def _pct_from(price: float | None, other: float | None) -> float | None:
    if price is None or other is None or price == 0:
        return None
    return (other - price) / price


def _header(emoji: str, title: str, event: TradeEvent, *, with_symbol: bool = True) -> str:
    parts = [f"{emoji} {title} [{event.mode}]"]
    if with_symbol and event.symbol:
        parts.append(event.symbol)
    return " ".join(parts)


# ---------------------------------------------------------------- 알림기
class Notifier:
    """거래 이벤트를 포맷해 전송한다. 실패해도 호출자에게 예외를 던지지 않는다.

    매개변수
    - ``transport``     None이면 로그만 남긴다.
    - ``failed_log``    최종 실패 시 기록할 jsonl 경로.
    - ``retries``       재시도 횟수(첫 시도 제외). 지연은 ``retry_delays``(기본 1s/3s/9s; 부족하면 마지막 값 반복).
    - ``async_send``    True면 큐+데몬 워커 스레드, False면 동기 전송(테스트·CLI status용).
    - ``retry_delays``/``sleep``  테스트용 주입 지점.
    """

    def __init__(
        self,
        transport: Transport | None,
        *,
        failed_log: Path | None = None,
        retries: int = 3,
        async_send: bool = True,
        retry_delays: Sequence[float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.transport = transport
        self.failed_log = Path(failed_log) if failed_log is not None else None
        self.retries = max(0, int(retries))
        self.async_send = bool(async_send)
        self.retry_delays: tuple[float, ...] = (
            tuple(float(d) for d in retry_delays) if retry_delays is not None else DEFAULT_RETRY_DELAYS
        )
        self._sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._queue: queue.Queue[tuple[TradeEvent, str]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._inflight: tuple[TradeEvent, str] | None = None
        self._lock = threading.Lock()
        self.sent_count = 0
        self.failed_count = 0

    # 공개 API ------------------------------------------------------------------
    def notify(self, event: TradeEvent) -> None:
        """포맷 후 (async면) 큐에 넣고 즉시 반환. 어떤 경우에도 예외를 던지지 않는다."""
        try:
            text = self.format(event)
        except Exception:  # noqa: BLE001 - 포맷 실패도 사이클을 죽이면 안 된다
            log.exception("알림 포맷 실패 (kind=%s)", getattr(event, "kind", "?"))
            try:
                text = f"❗ 알림 포맷 실패 [{event.mode}] {event.kind}"
            except Exception:  # noqa: BLE001
                return
        if self.transport is None:
            log.info("[알림 비활성] %s", text.replace("\n", " | "))
            return
        try:
            if self.async_send and self._ensure_worker():
                self._queue.put((event, text))
                return
            # 동기 모드이거나 워커를 띄울 수 없으면(자원 고갈·인터프리터 종료 중) 동기 전송으로 강등
            self._deliver(event, text)
        except Exception:  # noqa: BLE001 - 어떤 경우에도 사이클로 전파하지 않는다
            log.exception("알림 처리 중 내부 오류 (kind=%s)", getattr(event, "kind", "?"))

    def flush(self, timeout_s: float = 5.0) -> None:
        """큐가 빌 때까지(최대 ``timeout_s``) 기다린다. 동기 모드면 즉시 반환.

        시간 초과 시 큐에 남은(그리고 전송 중인) 이벤트는 유실되지 않도록 failed_log에 기록한다
        (SPEC §11: 최종 실패 → failed_log). 프로세스 종료 시 데몬 워커가 함께 사라지기 때문이다.
        """
        if not self.async_send:
            return
        q = self._queue
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with q.all_tasks_done:
            while q.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("알림 flush 시간 초과: 미전송 %d건 — failed_log로 기록", q.unfinished_tasks)
                    break
                q.all_tasks_done.wait(remaining)
            else:
                return
        self._drain_to_failed_log()

    def _drain_to_failed_log(self) -> None:
        """큐에 남은 이벤트를 모두 꺼내 failed_log에 기록한다. 전송 중인 이벤트도 함께 기록한다."""
        drained = 0
        while True:
            try:
                event, text = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.failed_count += 1
                self._record_failure(event, text, "flush 시간 초과: 전송 전 종료", 0)
                drained += 1
            finally:
                self._queue.task_done()
        inflight = self._inflight
        if inflight is not None:
            event, text = inflight
            self._record_failure(event, text, "flush 시간 초과: 전송 중 종료", 0)
            drained += 1
        if drained:
            log.error("알림 flush 시간 초과로 %d건을 failed_log에 기록", drained)

    @property
    def pending(self) -> int:
        """아직 전송이 끝나지 않은 이벤트 수."""
        return int(self._queue.unfinished_tasks)

    def __repr__(self) -> str:
        return (
            f"Notifier(transport={self.transport!r}, async_send={self.async_send}, "
            f"retries={self.retries}, pending={self.pending})"
        )

    # 내부: 워커 ----------------------------------------------------------------
    def _ensure_worker(self) -> bool:
        """워커 스레드가 살아 있도록 한다. 스레드를 만들 수 없으면 False(호출자는 동기 전송으로 강등)."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return True
            try:
                worker = threading.Thread(target=self._run, name="tradingbot-notify", daemon=True)
                worker.start()
            except Exception as e:  # noqa: BLE001 - 자원 고갈·인터프리터 종료 중
                self._worker = None
                log.warning("알림 워커 스레드를 시작할 수 없음 (%s: %s) — 동기 전송으로 강등", type(e).__name__, e)
                return False
            self._worker = worker
            return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            self._inflight = item
            try:
                event, text = item
                self._deliver(event, text)
            except Exception:  # noqa: BLE001 - 워커는 절대 죽지 않는다
                log.exception("알림 워커 내부 오류")
            finally:
                self._inflight = None
                self._queue.task_done()

    # 내부: 전송·재시도 -----------------------------------------------------------
    def _delay_for(self, attempt_index: int) -> float:
        if not self.retry_delays:
            return 0.0
        i = min(attempt_index, len(self.retry_delays) - 1)
        return max(0.0, float(self.retry_delays[i]))

    def _deliver(self, event: TradeEvent, text: str) -> bool:
        """재시도 포함 전송. 최종 실패 시 failed_log 기록. 예외를 던지지 않는다."""
        transport = self.transport
        if transport is None:
            log.info("[알림 비활성] %s", text.replace("\n", " | "))
            return True
        attempts = self.retries + 1
        last_error = "unknown"
        i = 0                 # 재시도 예산을 쓰는 실패 횟수
        tries = 0             # 실제 시도 횟수
        rate_limited_extra = 0
        while i < attempts:
            tries += 1
            try:
                transport.send(text)
            except Exception as e:  # noqa: BLE001
                last_error = f"{type(e).__name__}: {self._scrub_error(transport, str(e))}"
                retry_after = self._retry_after_of(e)
                if retry_after is not None and rate_limited_extra < RATE_LIMIT_MAX_EXTRA_ATTEMPTS:
                    # 429: 서버가 요구한 시간만큼 기다리고, 재시도 예산은 쓰지 않는다
                    rate_limited_extra += 1
                    delay = min(max(self._delay_for(i), retry_after), RATE_LIMIT_MAX_WAIT_S)
                    log.warning(
                        "알림 전송 레이트 리밋 (kind=%s): %s — %.1fs 후 재시도 (%d/%d)",
                        event.kind, last_error, delay, rate_limited_extra, RATE_LIMIT_MAX_EXTRA_ATTEMPTS,
                    )
                    self._safe_sleep(delay)
                    continue
                if i < attempts - 1:
                    delay = self._delay_for(i)
                    if retry_after is not None:
                        delay = min(max(delay, retry_after), RATE_LIMIT_MAX_WAIT_S)
                    log.warning(
                        "알림 전송 실패 (%d/%d, kind=%s): %s — %.1fs 후 재시도",
                        i + 1, attempts, event.kind, last_error, delay,
                    )
                    self._safe_sleep(delay)
                i += 1
                continue
            self.sent_count += 1
            log.debug("알림 전송 성공 (kind=%s, 시도 %d)", event.kind, tries)
            return True
        self.failed_count += 1
        log.error("알림 최종 실패 (kind=%s, 시도 %d회): %s", event.kind, tries, last_error)
        self._record_failure(event, text, last_error, tries)
        return False

    def _safe_sleep(self, delay: float) -> None:
        try:
            self._sleep(delay)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _retry_after_of(exc: BaseException) -> float | None:
        """레이트 리밋 예외이고 retry_after(초)를 알면 그 값, 아니면 None(일반 실패로 취급: 기본 지연·예산 소모)."""
        if not isinstance(exc, TransportRateLimited):
            return None
        return _parse_retry_after(getattr(exc, "retry_after", None))

    @staticmethod
    def _scrub_error(transport: object, message: str) -> str:
        """전송기가 비밀값 가리기(_scrub)를 제공하면 적용한다. 그 외 전송기는 메시지 그대로."""
        scrub = getattr(transport, "_scrub", None)
        if callable(scrub):
            try:
                return str(scrub(message))
            except Exception:  # noqa: BLE001
                return message
        return message

    @staticmethod
    def _event_payload(event: TradeEvent) -> object:
        """failed_log용 이벤트 표현. JSON 직렬화 불가 값(extra 등)은 repr로 대체한다."""
        try:
            payload = event.model_dump(mode="json")
            json.dumps(payload, sort_keys=True, ensure_ascii=False)
            return payload
        except Exception:  # noqa: BLE001 - PydanticSerializationError 등
            pass
        try:
            raw = event.model_dump(mode="python")
            return json.loads(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=repr))
        except Exception:  # noqa: BLE001
            return repr(event)

    def _record_failure(self, event: TradeEvent, text: str, error: str, attempts: int) -> None:
        """최종 실패를 failed_log(jsonl)에 남긴다. 어떤 경우에도 예외를 던지지 않는다."""
        if self.failed_log is None:
            return
        try:
            record = {
                "ts": int(time.time() * 1000),
                "kind": str(getattr(event, "kind", "?")),
                "event": self._event_payload(event),
                "text": str(text),
                "error": str(error),
                "attempts": int(attempts),
            }
            line = json.dumps(record, sort_keys=True, ensure_ascii=False, default=repr) + "\n"
            self.failed_log.parent.mkdir(parents=True, exist_ok=True)
            with self.failed_log.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:  # noqa: BLE001 - 기록 실패가 사이클을 죽이면 안 된다
            log.error("failed_log 기록 실패 (%s): %s: %s", self.failed_log, type(e).__name__, e)

    # 포맷 -----------------------------------------------------------------------
    @staticmethod
    def format(event: TradeEvent) -> str:
        """이벤트를 한국어 텔레그램 메시지로 만든다. 결정적(같은 입력 -> 같은 출력)."""
        kind = event.kind
        fn = _FORMATTERS.get(kind, _format_generic)
        return fn(event)


# ---------------------------------------------------------------- 종류별 포맷
def _state_save_failed(e: TradeEvent) -> bool:
    """체결 후 상태 저장이 실패했다고 표시된 이벤트인가(``extra.state_saved is False``)."""
    return (e.extra or {}).get("state_saved") is False


def _add_state_save_warning(lines: list[str], e: TradeEvent) -> None:
    """상태 저장 실패 경고 줄을 헤더 바로 아래에 넣는다(이미 본문에 같은 경고가 있으면 생략)."""
    if not _state_save_failed(e):
        return
    if any(STATE_SAVE_WARNING in line for line in lines) or STATE_SAVE_WARNING in (e.thesis or "") \
            or STATE_SAVE_WARNING in (e.reason or ""):
        return
    lines.insert(1, STATE_SAVE_WARNING)


def _format_entry(e: TradeEvent) -> str:
    lines = [_header(EMOJI["entry"], "진입", e)]
    qty_line = f"수량 {_fmt_amount(e.base_amount)} @ {_fmt_price(e.price)}"
    if e.quote_amount is not None:
        qty_line += f" ({_fmt_quote(e.quote_amount)} USDT)"
    lines.append(qty_line)
    if e.stop_loss_price is not None or e.take_profit_price is not None:
        sl = f"손절 {_fmt_price(e.stop_loss_price)}"
        sl_pct = _pct_from(e.price, e.stop_loss_price)
        if sl_pct is not None:
            sl += f" ({_fmt_pct(sl_pct)})"
        tp = f"익절 {_fmt_price(e.take_profit_price)}"
        tp_pct = _pct_from(e.price, e.take_profit_price)
        if tp_pct is not None:
            tp += f" ({_fmt_pct(tp_pct)})"
        lines.append(f"{sl} / {tp}")
    if e.confidence is not None:
        lines.append(f"확신도 {e.confidence:.2f}")
    if e.thesis:
        lines.append(f"근거: {_truncate(e.thesis)}")
    _add_state_save_warning(lines, e)
    return "\n".join(lines)


def _format_post_trade_review(e: TradeEvent, review: str) -> str:
    """전량 청산 뒤 사후분석 후속 알림(cycle ``extra.post_trade_review`` + ``extra.follow_up``).

    청산 알림이 이미 수량·가격·손익을 보냈으므로 그 줄은 반복하지 않는다. 사후분석 문구는 줄바꿈을 유지한다.
    """
    head = _header(EMOJI["post_trade_review"], "사후분석", e)
    reason = (e.reason or "").strip()
    if reason and "\n" not in reason and len(reason) <= 40:
        head += f" {EXIT_REASON_KO.get(reason, reason)}"
    lines = [head]
    if e.pnl_pct is not None:
        lines.append(f"손익 {_fmt_pct(e.pnl_pct)}")
    lines.append(_truncate_multiline(review, REVIEW_TEXT_MAX_CHARS) or "-")
    return "\n".join(lines)


def _format_exit(e: TradeEvent, *, partial: bool = False) -> str:
    ex = e.extra or {}
    review = ex.get("post_trade_review")
    if not partial and ex.get("follow_up") and isinstance(review, str) and review.strip():
        return _format_post_trade_review(e, review)
    pnl = e.pnl_quote
    if partial:
        emoji, title = EMOJI["partial_exit"], "부분 청산"
    else:
        # 손익 부호: pnl_quote 우선, 없으면 pnl_pct로 판단 (SPEC §11: 🔴 손실 / 🔵 이익)
        sign_src = pnl if pnl is not None else e.pnl_pct
        emoji = EMOJI["exit_loss"] if (sign_src is not None and sign_src < 0) else EMOJI["exit_gain"]
        title = "청산"
    head = _header(emoji, title, e)
    if e.reason:
        head += f" {EXIT_REASON_KO.get(e.reason, e.reason)}"
    lines = [head]
    body = f"{_fmt_amount(e.base_amount)} @ {_fmt_price(e.price)}"
    if pnl is not None or e.pnl_pct is not None:
        body += f" → 손익 {_fmt_pnl(pnl)} USDT"
        if e.pnl_pct is not None:
            body += f" ({_fmt_pct(e.pnl_pct)})"
    lines.append(body)
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    _add_state_save_warning(lines, e)
    return "\n".join(lines)


def _format_partial_exit(e: TradeEvent) -> str:
    return _format_exit(e, partial=True)


def _format_order_rejected(e: TradeEvent) -> str:
    lines = [_header(EMOJI["order_rejected"], "주문 거부", e)]
    detail: list[str] = []
    if e.side:
        detail.append("매수" if e.side == "buy" else ("매도" if e.side == "sell" else e.side))
    if e.quote_amount is not None:
        detail.append(f"{_fmt_quote(e.quote_amount)} USDT")
    elif e.base_amount is not None:
        detail.append(_fmt_amount(e.base_amount))
    if e.price is not None:
        detail.append(f"@ {_fmt_price(e.price)}")
    if detail:
        lines.append(" ".join(detail))
    lines.append(f"사유: {_truncate(e.reason) or '-'}")
    return "\n".join(lines)


def _format_kill_switch(e: TradeEvent) -> str:
    lines = [_header(EMOJI["kill_switch"], "킬스위치 발동", e)]
    lines.append(f"사유: {_truncate(e.reason) or '-'}")
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    lines.append("신규 진입 중단 — 사람이 해제해야 합니다")
    return "\n".join(lines)


def _format_budget_exceeded(e: TradeEvent) -> str:
    lines = [_header(EMOJI["budget_exceeded"], "LLM 예산 초과", e)]
    lines.append(f"사유: {_truncate(e.reason) or '-'}")
    ex = e.extra or {}
    spent, budget = ex.get("spent_usd"), ex.get("budget_usd")
    if isinstance(spent, (int, float)) and isinstance(budget, (int, float)):
        lines.append(f"사용 ${spent:,.2f} / 예산 ${budget:,.2f}")
    return "\n".join(lines)


def _format_daily_summary(e: TradeEvent) -> str:
    ex = e.extra or {}
    if ex.get("heartbeat"):
        # 구버전 하트비트(kind=daily_summary + extra.heartbeat) — 진짜 일일 요약과 구분되게 하트비트로 그린다
        return _format_heartbeat(e)
    title = "일일 요약"
    date = ex.get("date")
    if date:
        title += f" {date}"
    lines = [_header(EMOJI["daily_summary"], title, e, with_symbol=False)]
    if e.pnl_quote is not None or e.pnl_pct is not None:
        line = f"손익 {_fmt_pnl(e.pnl_quote)} USDT"
        if e.pnl_pct is not None:
            line += f" ({_fmt_pct(e.pnl_pct)})"
        lines.append(line)
    trades = ex.get("trades")
    if isinstance(trades, int):
        wins, losses = ex.get("wins", 0), ex.get("losses", 0)
        line = f"거래 {trades}건 (승 {wins} / 패 {losses})"
        wr = ex.get("win_rate")
        if isinstance(wr, (int, float)):
            line += f" 승률 {wr * 100:.2f}%"
        lines.append(line)
    if e.equity is not None:
        line = f"자본 {_fmt_quote(e.equity)}"
        start = ex.get("start_equity")
        if isinstance(start, (int, float)):
            line += f" (시작 {_fmt_quote(float(start))})"
        lines.append(line)
    cost = ex.get("llm_cost_usd")
    if isinstance(cost, (int, float)):
        lines.append(f"LLM 비용 ${cost:,.2f}")
    if e.reason and not date:
        if ex.get("review_date"):
            # 일일 리뷰(cycle._maybe_daily_review): 저널 telegram_text(≤400자, 줄 단위)를 줄바꿈째 보존
            lines.append(_truncate_multiline(e.reason, REVIEW_TEXT_MAX_CHARS))
        else:
            lines.append(_truncate(e.reason))
    return "\n".join(lines)


def _finite_num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    f = float(x)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _halt_codes(e: TradeEvent) -> list[str]:
    raw = (e.extra or {}).get("halt_codes")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(c) for c in dict.fromkeys(raw) if str(c).strip()]


def _halt_label(code: str) -> str:
    ko = HALT_REASON_KO.get(code)
    return f"{ko} ({code})" if ko else code


def _format_halt(e: TradeEvent) -> str:
    """G0 신규 진입 중단(cycle._notify_halts). 구버전(kind=error + extra.code='halt')도 같은 모양."""
    lines = [_header(EMOJI["halt"], "신규 진입 중단", e, with_symbol=False)]
    codes = _halt_codes(e)
    if codes:
        lines.append("사유:")
        lines.extend(f"- {_halt_label(c)}" for c in codes)
    detail = (e.reason or "").strip()
    for prefix in ("신규 진입 중단 (G0):", "신규 진입 중단(G0):"):
        if detail.startswith(prefix):
            detail = detail[len(prefix):].strip()
    if detail:
        lines.append(f"상세: {_truncate(detail, HALT_DETAIL_MAX_CHARS)}")
    elif not codes:
        lines.append("사유: -")
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    lines.append("보유 포지션의 코드 청산 검사는 계속합니다")
    return "\n".join(lines)


def _format_halt_lifted(e: TradeEvent) -> str:
    """G0 중단 해제(cycle._notify_halts). 구버전(kind=startup + extra.event='halt_lifted')도 같은 모양."""
    lines = [_header(EMOJI["halt_lifted"], "신규 진입 재개", e, with_symbol=False)]
    codes = _halt_codes(e)
    if codes:
        lines.append("해제된 중단:")
        lines.extend(f"- {_halt_label(c)}" for c in codes)
    elif e.reason:
        lines.append(_truncate(e.reason))
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    return "\n".join(lines)


def _format_heartbeat(e: TradeEvent) -> str:
    """main.run_loop 4시간 하트비트: 자본·당일 손익·보유 포지션(미실현 %)·노출 %·오늘 LLM 비용."""
    ex = e.extra or {}
    lines = [_header(EMOJI["heartbeat"], "하트비트", e, with_symbol=False)]
    status: list[str] = []
    cycle = ex.get("cycle")
    if isinstance(cycle, int) and not isinstance(cycle, bool):
        status.append(f"사이클 {cycle}")
    if isinstance(ex.get("kill_switch"), bool):
        status.append(f"킬스위치 {'ON' if ex['kill_switch'] else 'OFF'}")
    if status:
        lines.append("정상 동작 중 — " + ", ".join(status))
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    if e.pnl_quote is not None or e.pnl_pct is not None:
        line = f"당일 손익 {_fmt_pnl(e.pnl_quote)} USDT"
        if e.pnl_pct is not None:
            line += f" ({_fmt_pct(e.pnl_pct)})"
        lines.append(line)
    positions = ex.get("positions")
    if isinstance(positions, (list, tuple)):
        lines.append(f"보유 {len(positions)}개" + (":" if positions else ""))
        for p in positions:
            if not isinstance(p, dict):
                continue
            sym = str(p.get("symbol") or "?")
            upct = _finite_num(p.get("unrealized_pct"))
            val = _finite_num(p.get("value_quote"))
            line = f"- {sym} 미실현 {_fmt_pct(upct)}"
            if val is not None:
                line += f" ({_fmt_quote(val)} USDT)"
            lines.append(line)
    exposure = _finite_num(ex.get("exposure_pct"))
    if exposure is not None:
        lines.append(f"노출 {exposure * 100:.2f}%")
    cost = _finite_num(ex.get("llm_cost_today_usd"))
    if cost is not None:
        lines.append(f"오늘 LLM 비용 ${cost:,.2f}")
    if e.reason and not isinstance(positions, (list, tuple)):
        # 구버전 하트비트: 상태 요약이 reason 문자열에만 있다
        lines.append(_truncate(e.reason))
    return "\n".join(lines)


def _format_error(e: TradeEvent) -> str:
    if (e.extra or {}).get("code") == "halt":
        return _format_halt(e)      # 구버전 G0 중단 알림(kind=error)
    lines = [_header(EMOJI["error"], "오류", e)]
    lines.append(_truncate(e.reason, 500) or "(상세 없음)")
    stage = (e.extra or {}).get("stage")
    if stage:
        lines.append(f"단계: {stage}")
    return "\n".join(lines)


def _format_startup(e: TradeEvent) -> str:
    if (e.extra or {}).get("event") == "halt_lifted":
        return _format_halt_lifted(e)   # 구버전 G0 중단 해제 알림(kind=startup)
    lines = [_header(EMOJI["startup"], "봇 시작", e, with_symbol=False)]
    ex = e.extra or {}
    syms = ex.get("symbols")
    if isinstance(syms, (list, tuple)) and syms:
        lines.append("종목: " + ", ".join(str(s) for s in syms))
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    cm = ex.get("cycle_minutes")
    if isinstance(cm, (int, float)):
        lines.append(f"사이클 {int(cm)}분")
    if e.reason:
        lines.append(_truncate(e.reason))
    lines.append(_fmt_ts(e.ts))
    return "\n".join(lines)


def _format_shutdown(e: TradeEvent) -> str:
    lines = [_header(EMOJI["shutdown"], "봇 종료", e, with_symbol=False)]
    if e.reason:
        lines.append(f"사유: {_truncate(e.reason)}")
    if e.equity is not None:
        lines.append(f"자본 {_fmt_quote(e.equity)}")
    lines.append(_fmt_ts(e.ts))
    return "\n".join(lines)


def _format_llm_paused(e: TradeEvent) -> str:
    lines = [_header(EMOJI["llm_paused"], "LLM 일시정지", e, with_symbol=False)]
    lines.append(f"사유: {_truncate(e.reason) or '-'}")
    until = (e.extra or {}).get("until_ts")
    if isinstance(until, (int, float)):
        lines.append(f"해제 예정: {_fmt_ts(int(until))}")
    lines.append("에이전트 호출 없이 코드 청산 검사만 계속합니다")
    return "\n".join(lines)


# ---------------------------------------------------------------- 경보(kind="alert", extra.alert 로 분기)
FUSED_CONFIDENCE_FLOOR = 0.75        # gates.RAISED_CONFIDENCE_FLOOR 와 같은 값(테스트로 고정)
FUSED_SIZE_MULTIPLIER = 0.5          # gates.g3: FUSED → 사이즈 ×0.5
ALERT_SUMMARY_MAX_CHARS = 300
ALERT_RATIONALE_MAX_CHARS = 200

REGIME_VERDICT_KO: dict[str, str] = {
    "normal": "정상",
    "fused": "상관 융합(FUSED)",
    "macro_alarm": "매크로 경보(BTC 충격 ∧ 상관 융합)",
}

NEWS_EVENT_TYPE_KO: dict[str, str] = {
    "hack": "해킹",
    "regulatory": "규제",
    "listing": "상장",
    "delisting": "상장폐지",
    "macro": "거시",
    "etf": "ETF",
    "exchange": "거래소",
    "protocol": "프로토콜",
    "sentiment": "심리",
    "technical_break": "기술적 이탈",
    "other": "기타",
}

# 뉴스 점수 고정 7단계 척도(SPEC §20.3a.3) → 한국어 라벨
NEWS_SCORE_SCALE: tuple[tuple[float, str], ...] = (
    (-1.0, "매우 강한 악재"), (-0.66, "강한 악재"), (-0.33, "약한 악재"), (0.0, "중립"),
    (0.33, "약한 호재"), (0.66, "강한 호재"), (1.0, "매우 강한 호재"),
)

RISK_ACTION_KO: dict[str, str] = {"hold": "유지", "trim": "축소", "exit": "청산"}
ASSUMPTION_STATUSES: tuple[str, ...] = ("holding", "weakening", "impaired", "broken")
HEAD_ACTION_KO: dict[str, str] = {"BUY": "매수", "SELL": "매도", "HOLD": "보유"}


def _regime_label(verdict: object) -> str:
    if verdict is None or str(verdict) == "":
        return "시작(이전 기록 없음)"
    v = str(verdict)
    ko = REGIME_VERDICT_KO.get(v)
    return f"{v}({ko})" if ko else v


def regime_rule_text(verdict: object) -> str:
    """레짐 판정이 만드는 규칙 변화(gates.g3_portfolio 와 같은 규칙)."""
    v = str(verdict) if verdict is not None else ""
    if v == "macro_alarm":
        return "신규 진입 금지(G3 탈락) — 보유 포지션 코드 청산 검사는 계속"
    if v == "fused":
        return (f"신규 진입 사이즈 절반(×{FUSED_SIZE_MULTIPLIER:g}), "
                f"확신도 하한 {FUSED_CONFIDENCE_FLOOR:.2f}")
    if v == "normal":
        return f"기본 규칙(사이즈 ×1, 확신도 하한 {HardLimits.MIN_CONFIDENCE:.2f})"
    return "-"


def _news_scale(score: float | None) -> tuple[float, str] | None:
    """점수를 7단계 척도의 가장 가까운 값과 라벨로(같은 거리면 낮은 쪽)."""
    if score is None:
        return None
    best = min(NEWS_SCORE_SCALE, key=lambda kv: (abs(kv[0] - score), kv[0]))
    return best


def _str_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x) for x in raw if str(x).strip()]


def _format_regime_alert(e: TradeEvent) -> str:
    ex = e.extra or {}
    old, new = ex.get("old_verdict"), ex.get("new_verdict")
    held_new = _str_list(ex.get("name_alarm_held_new"))
    if ex.get("verdict_changed") is False:
        # 판정은 그대로, 보유 종목에 새 NAME 알람
        lines = [_header(EMOJI["regime"], "NAME 알람(보유 종목)", e, with_symbol=False)]
        lines.append(f"레짐 판정 유지: {_regime_label(new)}")
    else:
        lines = [_header(EMOJI["regime"], "레짐 전환", e, with_symbol=False)]
        lines.append(f"{_regime_label(old)} → {_regime_label(new)}")
    lines.append(f"규칙: {regime_rule_text(new)}")
    raw = ex.get("raw_verdict")
    if isinstance(raw, str) and raw and new is not None and raw != str(new):
        lines.append(f"스캐너 판정: {_regime_label(raw)} — 상관 융합 플래그(fused)가 남아 위 규칙이 적용됨"
                     " (BTC 데이터 없음 등)")
    detail: list[str] = []
    btc4h = ex.get("btc_regime_4h")
    if isinstance(btc4h, str) and btc4h:
        detail.append(f"BTC 4h {btc4h}")
    corr = _finite_num(ex.get("avg_pairwise_corr_1h"))
    if corr is not None:
        detail.append(f"평균 상관 {corr:.2f}")
    if detail:
        lines.append(" · ".join(detail))
    shock = ex.get("btc_shock")
    if isinstance(shock, bool):
        lines.append(f"BTC 충격: {'예' if shock else '아니오'}")
    alarms = _str_list(ex.get("name_alarm_symbols"))
    if alarms:
        lines.append("NAME 알람: " + ", ".join(alarms) + " — 해당 종목 신규 진입 금지, 보유 시 총괄 SELL 은 전량")
    else:
        lines.append("NAME 알람: 없음")
    if held_new:
        lines.append("보유 종목 새 NAME 알람: " + ", ".join(held_new))
    return "\n".join(lines)


def _format_news_risk_alert(e: TradeEvent) -> str:
    ex = e.extra or {}
    lines = [_header(EMOJI["news_risk"], "뉴스 위험", e)]
    et = ex.get("event_type")
    if isinstance(et, str) and et:
        ko = NEWS_EVENT_TYPE_KO.get(et)
        lines.append(f"유형: {et}" + (f"({ko})" if ko else ""))
    else:
        lines.append("유형: - (뉴스 분석가 risk_event 표시)")
    scale = _news_scale(_finite_num(ex.get("score")))
    if scale is not None:
        lines.append(f"점수: {scale[0]:+.2f} ({scale[1]}, 7단계 척도 −1.0~+1.0)")
    else:
        lines.append("점수: -")
    summary = ex.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"요약: {_truncate(summary, ALERT_SUMMARY_MAX_CHARS)}")
    held = ex.get("held") is True
    lines.append(f"보유: {'예' if held else '아니오'}")
    lines.append("조치: 신규 진입 차단(G1 뉴스 위험, 24시간)")
    if held:
        flagged = ex.get("flagged_for_head")
        reason = ex.get("flag_reason")
        if flagged is True:
            line = "보유 중 → 총괄 검토 대상으로 표시됨"
            if isinstance(reason, str) and reason.strip():
                line += f" ({_truncate(reason, 120)})"
        else:
            line = "보유 중 → 리스크 관리자 점검에 뉴스 위험 전달(위험 판정 시 총괄 검토)"
        lines.append(line)
    return "\n".join(lines)


def _format_position_health_alert(e: TradeEvent) -> str:
    ex = e.extra or {}
    lines = [_header(EMOJI["position_health"], "포지션 점검", e)]
    health = ex.get("health")
    if isinstance(health, int) and not isinstance(health, bool):
        line = f"건강도 {health}/10"
        if health <= 4:
            line += " (≤4 경고)"
        lines.append(line)
    else:
        lines.append("건강도 -")
    reds = _str_list(ex.get("red_lines"))
    lines.append("레드라인: " + ("; ".join(_truncate(r, 120) for r in reds) + " (적중)" if reds else "적중 없음"))
    action = ex.get("risk_action")
    if isinstance(action, str) and action:
        line = f"리스크 조치: {action}({RISK_ACTION_KO.get(action, action)})"
        reason = ex.get("risk_reason")
        if isinstance(reason, str) and reason.strip():
            line += f" — {_truncate(reason, 120)}"
        lines.append(line)
    statuses = _str_list(ex.get("assumption_status"))
    if statuses:
        counts = {k: statuses.count(k) for k in ASSUMPTION_STATUSES}
        lines.append("가정: " + " · ".join(f"{k} {counts[k]}" for k in ASSUMPTION_STATUSES))
    upct = _finite_num(ex.get("unrealized_pct"))
    if upct is not None:
        lines.append(f"미실현 {upct:+.2f}%")
    worse = ex.get("worsened")
    if isinstance(worse, dict):
        parts: list[str] = []
        wh = worse.get("health")
        if isinstance(wh, (list, tuple)) and len(wh) == 2:
            parts.append(f"건강도 {wh[0]} → {wh[1]}")
        wa = worse.get("risk_action")
        if isinstance(wa, (list, tuple)) and len(wa) == 2:
            parts.append(f"리스크 조치 {wa[0]} → {wa[1]}")
        if parts:
            lines.append("악화: " + " · ".join(parts))
    head = ex.get("head_decision")
    if isinstance(head, dict) and head.get("action"):
        act = str(head.get("action"))
        line = f"총괄 결정: {act}({HEAD_ACTION_KO.get(act, act)})"
        conf = _finite_num(head.get("confidence"))
        if conf is not None:
            line += f" 확신도 {conf:.2f}"
        executed = head.get("executed")
        if isinstance(executed, bool):
            line += " — 실행됨" if executed else " — 미실행"
        rationale = head.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            line += f"\n근거: {_truncate(rationale, ALERT_RATIONALE_MAX_CHARS)}"
        lines.append(line)
    else:
        lines.append("총괄 결정: 이번 사이클 없음")
    return "\n".join(lines)


_ALERT_FORMATTERS: dict[str, Callable[[TradeEvent], str]] = {
    "regime": _format_regime_alert,
    "news_risk": _format_news_risk_alert,
    "position_health": _format_position_health_alert,
}


def _format_alert(e: TradeEvent) -> str:
    """kind="alert": ``extra.alert`` 로 레짐 전환·뉴스 위험·포지션 점검을 그린다(DESIGN §7)."""
    sub = str((e.extra or {}).get("alert") or "")
    fn = _ALERT_FORMATTERS.get(sub)
    if fn is None:
        lines = [_header(EMOJI["alert"], f"경보 {sub or '-'}", e)]
        if e.reason:
            lines.append(_truncate(e.reason))
        return "\n".join(lines)
    return fn(e)


def _format_generic(e: TradeEvent) -> str:  # 알 수 없는 kind 방어
    lines = [_header("ℹ️", str(e.kind), e)]
    if e.reason:
        lines.append(_truncate(e.reason))
    return "\n".join(lines)


_FORMATTERS: dict[str, Callable[[TradeEvent], str]] = {
    "entry": _format_entry,
    "exit": _format_exit,
    "partial_exit": _format_partial_exit,
    "order_rejected": _format_order_rejected,
    "kill_switch": _format_kill_switch,
    "budget_exceeded": _format_budget_exceeded,
    "daily_summary": _format_daily_summary,
    "error": _format_error,
    "startup": _format_startup,
    "shutdown": _format_shutdown,
    "llm_paused": _format_llm_paused,
    "halt": _format_halt,
    "halt_lifted": _format_halt_lifted,
    "heartbeat": _format_heartbeat,
    "alert": _format_alert,
}

__all__ = [
    "ALERT_SUMMARY_MAX_CHARS",
    "DEFAULT_RETRY_DELAYS",
    "FUSED_CONFIDENCE_FLOOR",
    "NEWS_EVENT_TYPE_KO",
    "REGIME_VERDICT_KO",
    "regime_rule_text",
    "EMOJI",
    "EXIT_REASON_KO",
    "HALT_REASON_KO",
    "Notifier",
    "STATE_SAVE_WARNING",
    "TelegramTransport",
    "Transport",
    "TransportError",
    "TransportRateLimited",
]
