"""텔레그램 알림 (SPEC §11). 모든 거래 이벤트는 텔레그램으로 간다.

구성
- ``Transport``          전송 프로토콜. ``send(text)``는 실패 시 예외를 던진다.
- ``TelegramTransport``  Bot API ``sendMessage``로 전송. ``http_post``를 주입할 수 있어 오프라인 테스트가 가능하다.
                         repr/str/로그/예외 메시지 어디에도 토큰(URL 포함)이 나오지 않는다.
- ``Notifier``           ``TradeEvent``를 한국어로 포맷해 보낸다. ``async_send=True``면 큐에 넣고 즉시 반환하고
                         데몬 워커 스레드가 전송한다. 실패하면 1s/3s/9s 간격으로 재시도하고(지연은 주입 가능),
                         끝내 실패하면 ``failed_log``(jsonl)에 이벤트와 오류를 남기고 ERROR 로그를 낸다.

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

from .config import mask_secret
from .models import TradeEvent

log = logging.getLogger("tradingbot.notify")

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_TEXT = 4096
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0, 9.0)
THESIS_MAX_CHARS = 200

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
}

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


# ---------------------------------------------------------------- 전송
@runtime_checkable
class Transport(Protocol):
    def send(self, text: str) -> None:  # 실패 시 예외
        ...


def _urllib_post(url: str, payload: dict, timeout_s: float) -> int:
    """기본 HTTP POST(JSON). 상태 코드를 돌려준다. HTTPError도 상태 코드로 바꾼다."""
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
        return int(e.code)


class TelegramTransport:
    """텔레그램 Bot API ``sendMessage`` 전송.

    parse_mode는 쓰지 않는다(이스케이프 문제 회피). ``disable_web_page_preview=True``.
    ``http_post(url, payload) -> status_code``를 주입하면 네트워크 없이 테스트할 수 있다.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http_post: Callable[[str, dict], int] | None = None,
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
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            raise TransportError(f"텔레그램 응답 상태를 해석할 수 없음: {self._scrub(repr(status))}") from None
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
        if self.async_send:
            self._ensure_worker()
            self._queue.put((event, text))
            return
        self._deliver(event, text)

    def flush(self, timeout_s: float = 5.0) -> None:
        """큐가 빌 때까지(최대 ``timeout_s``) 기다린다. 동기 모드면 즉시 반환."""
        if not self.async_send:
            return
        q = self._queue
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with q.all_tasks_done:
            while q.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("알림 flush 시간 초과: 미전송 %d건", q.unfinished_tasks)
                    return
                q.all_tasks_done.wait(remaining)

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
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, name="tradingbot-notify", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                event, text = item
                self._deliver(event, text)
            except Exception:  # noqa: BLE001 - 워커는 절대 죽지 않는다
                log.exception("알림 워커 내부 오류")
            finally:
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
        for i in range(attempts):
            try:
                transport.send(text)
            except Exception as e:  # noqa: BLE001
                last_error = f"{type(e).__name__}: {e}"
                if i < attempts - 1:
                    delay = self._delay_for(i)
                    log.warning(
                        "알림 전송 실패 (%d/%d, kind=%s): %s — %.1fs 후 재시도",
                        i + 1, attempts, event.kind, last_error, delay,
                    )
                    try:
                        self._sleep(delay)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            self.sent_count += 1
            log.debug("알림 전송 성공 (kind=%s, 시도 %d)", event.kind, i + 1)
            return True
        self.failed_count += 1
        log.error("알림 최종 실패 (kind=%s, 시도 %d회): %s", event.kind, attempts, last_error)
        self._record_failure(event, text, last_error, attempts)
        return False

    def _record_failure(self, event: TradeEvent, text: str, error: str, attempts: int) -> None:
        if self.failed_log is None:
            return
        record = {
            "ts": int(time.time() * 1000),
            "kind": event.kind,
            "event": event.model_dump(mode="json"),
            "text": text,
            "error": error,
            "attempts": attempts,
        }
        try:
            self.failed_log.parent.mkdir(parents=True, exist_ok=True)
            with self.failed_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("failed_log 기록 실패 (%s): %s", self.failed_log, e)

    # 포맷 -----------------------------------------------------------------------
    @staticmethod
    def format(event: TradeEvent) -> str:
        """이벤트를 한국어 텔레그램 메시지로 만든다. 결정적(같은 입력 -> 같은 출력)."""
        kind = event.kind
        fn = _FORMATTERS.get(kind, _format_generic)
        return fn(event)


# ---------------------------------------------------------------- 종류별 포맷
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
    return "\n".join(lines)


def _format_exit(e: TradeEvent, *, partial: bool = False) -> str:
    pnl = e.pnl_quote
    if partial:
        emoji, title = EMOJI["partial_exit"], "부분 청산"
    else:
        emoji = EMOJI["exit_loss"] if (pnl is not None and pnl < 0) else EMOJI["exit_gain"]
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
        lines.append(_truncate(e.reason))
    return "\n".join(lines)


def _format_error(e: TradeEvent) -> str:
    lines = [_header(EMOJI["error"], "오류", e)]
    lines.append(_truncate(e.reason, 500) or "(상세 없음)")
    stage = (e.extra or {}).get("stage")
    if stage:
        lines.append(f"단계: {stage}")
    return "\n".join(lines)


def _format_startup(e: TradeEvent) -> str:
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
}

__all__ = [
    "DEFAULT_RETRY_DELAYS",
    "EMOJI",
    "EXIT_REASON_KO",
    "Notifier",
    "TelegramTransport",
    "Transport",
    "TransportError",
]
