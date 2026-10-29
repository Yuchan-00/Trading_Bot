"""포트폴리오 상태 저장소 (SPEC §10).

역할
- ``BotState``를 메모리에 들고 ``state.json``에 **원자적으로** 저장한다
  (tmp 파일 → ``os.replace``, 실패 시 재시도, 최종 실패는 ``OSError`` 전파 → 사이클 중단).
  Windows 에서 다른 프로세스가 state.json 을 열고 있어 교체가 거부되면(공유 위반) 최대
  ``SAVE_SHARING_BUDGET_S`` 초까지 기다리며 다시 시도한다(``fsutil``).
- 손상 정책: 파일을 읽을 수 없으면 paper는 ``state.json.corrupt.<ts>``로 격리하고 새로 시작,
  testnet/live는 ``CorruptStateError``로 멈춘다. 파일의 mode가 설정과 다를 때도 같은 원칙.
- 체결(``Fill``)을 포지션/거래 장부에 반영한다. 돈 계산은 이 모듈이 유일한 진실이다:
  paper 잔고 차감·입금, 실현 손익, 당일 통계, 연속 손실, 쿨다운(재진입 금지 사이클).
- 리스크 판단은 하지 않는다(``risk.py``). peak_equity 갱신은 호출자가 ``update_peak``로 한다.

비밀값은 이 모듈이 다루지 않지만, ``Settings`` 전체를 로그/repr에 넣지 않는다.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from tradingbot import fsutil
from tradingbot.config import HardLimits, Settings
from tradingbot.models import BotState, ClosedTrade, DailyStats, ExitReason, Fill, Position

log = logging.getLogger("tradingbot.portfolio")

#: 상태 파일 포맷 버전. 다른 값이면 손상으로 취급한다.
STATE_VERSION: int = 1

#: ``save()``가 실패했을 때 다시 시도하는 횟수(첫 시도 제외). SPEC: 5회, 0.1s 간격.
SAVE_RETRIES: int = 5
SAVE_RETRY_DELAY_S: float = 0.1
#: Windows 공유 위반(대시보드·백신·동기화 프로그램이 state.json 을 잠깐 열고 있음)이면 교체를 이만큼(초) 더 기다린다.
#: 상태 저장 실패는 봇을 멈추므로(그동안 손절·익절 미집행) 다른 파일보다 길게 준다.
SAVE_SHARING_BUDGET_S: float = 10.0

#: 부분 체결 판정 허용 오차. 체결 수량이 보유 수량의 (1 - 0.5%) 미만이면 부분 청산.
PARTIAL_FILL_TOLERANCE: float = 0.005


class CorruptStateError(RuntimeError):
    """상태 파일을 신뢰할 수 없다(파싱 실패·mode 불일치). testnet/live는 여기서 멈춘다."""


# ---------------------------------------------------------------- 도우미
def _utcnow() -> datetime:
    """테스트에서 바꿔 끼울 수 있는 현재 시각(UTC aware)."""
    return datetime.now(timezone.utc)


def _now_ms() -> int:
    return int(_utcnow().timestamp() * 1000)


def _date_str(now: datetime) -> str:
    """aware/naive datetime -> UTC 날짜 문자열(YYYY-MM-DD). naive는 UTC로 간주."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()


def _finite_positive(x: object) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return math.isfinite(x) and x > 0


def _finite_nonneg(x: object) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return math.isfinite(x) and x >= 0


def _dump_state(state: BotState) -> str:
    """결정적 JSON(sort_keys, ensure_ascii=False). 같은 상태면 같은 바이트.

    ``allow_nan=False``: NaN/Infinity 토큰은 RFC 8259 JSON이 아니므로 절대 쓰지 않는다(ValueError).
    """
    return (
        json.dumps(state.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    )


def _cash_fee(fill: Fill, price: float) -> float:
    """체결의 **현금(quote) 수수료**만 돌려준다.

    SPEC §4 Fill 규약: 수수료가 base로 나가면 ``base_amount``는 이미 순수량(filled - fee_base)이고
    ``fee_quote``는 그 base 수수료를 체결가로 환산한 값을 포함한다. 순수량이 줄어든 것이 곧 수수료이므로
    손익/잔고 계산에서는 환산분을 빼고 실제로 quote에서 나간 몫만 비용으로 잡는다(이중 차감 방지).
    """
    fee = float(fill.fee_quote)
    fee_base = fill.fee_base if _finite_nonneg(fill.fee_base) else 0.0
    if fee_base > 0 and _finite_positive(price):
        fee -= fee_base * price
    return max(fee, 0.0)


def _fsync_dir(path: Path) -> None:
    """``os.replace`` 뒤 디렉터리 엔트리를 내구성 있게 만든다(best effort; 미지원 FS는 무시)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------- Portfolio
class Portfolio:
    """``BotState``의 소유자. 저장은 호출자가 체결 직후 ``save()``로 명시한다."""

    state: BotState

    def __init__(self, settings: Settings, path: Path) -> None:
        self._mode: str = settings.mode
        self._start_balance: float = float(settings.paper_start_balance)
        self.path: Path = Path(path)
        #: 재시도 대기 함수. 테스트가 ``pf.sleep = lambda s: None`` 처럼 바꿔 끼운다.
        self.sleep: Callable[[float], None] = time.sleep
        self.state = self._fresh_state()
        #: 새 상태의 DailyStats 날짜는 벽시계로 임시 기록된다. 첫 ``ensure_day``가 호출자 시계의 날짜를
        #: 채택할 때까지 False (SPEC §15: 시계는 주입 가능하므로 벽시계와 다를 수 있다).
        self._day_adopted: bool = False

    # ------------------------------------------------------------ 생성/로드
    def _fresh_state(self) -> BotState:
        """새 상태. paper는 가상 잔고로 시작, testnet/live는 잔고를 거래소가 가진다(0)."""
        is_paper = self._mode == "paper"
        balance = self._start_balance if is_paper else 0.0
        return BotState(
            version=STATE_VERSION,
            mode=self._mode,
            cycle=0,
            paper_quote_balance=balance,
            peak_equity=balance,
            daily=DailyStats(date=_date_str(_utcnow()), start_equity=balance),
        )

    @classmethod
    def load(cls, settings: Settings, path: Path) -> "Portfolio":
        """파일에서 상태를 읽는다.

        - 파일 없음 -> 새 상태.
        - 파싱/검증 실패 -> paper: 격리(``<path>.corrupt.<ts>``) 후 새 상태(ERROR 로그);
          testnet/live: ``CorruptStateError``.
        - 파일의 mode != settings.mode -> paper: 백업(``<path>.mismatch.<ts>``) 후 새 상태;
          testnet/live: ``CorruptStateError``.
        - 읽기 자체의 OSError(권한 등)는 그대로 전파한다(격리도 못 하므로 멈추는 게 맞다).
        """
        pf = cls(settings, path)
        p = pf.path
        if not p.exists():
            log.info("상태 파일 없음 -> 새 상태로 시작 (mode=%s, path=%s)", pf._mode, p)
            return pf

        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"상태 파일 최상위가 객체가 아님: {type(data).__name__}")
            state = BotState.model_validate(data)
            if state.version != STATE_VERSION:
                raise ValueError(f"지원하지 않는 상태 버전: {state.version} (기대 {STATE_VERSION})")
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError, UnicodeDecodeError) as e:
            summary = f"{type(e).__name__}: {str(e)[:200]}"
            if pf._mode != "paper":
                log.error("상태 파일 손상 (mode=%s, path=%s): %s", pf._mode, p, summary)
                raise CorruptStateError(f"상태 파일 손상: {p} ({summary})") from e
            quarantined = pf._quarantine(p, "corrupt")
            log.error("상태 파일 손상 -> %s 로 격리하고 새로 시작 (paper): %s", quarantined, summary)
            return pf

        if state.mode != pf._mode:
            msg = f"상태 파일 mode={state.mode!r} != 설정 mode={pf._mode!r}"
            if pf._mode != "paper":
                log.error("%s (path=%s)", msg, p)
                raise CorruptStateError(f"{msg}: {p}")
            backup = pf._quarantine(p, "mismatch")
            log.warning("%s -> %s 로 백업하고 새로 시작 (paper)", msg, backup)
            return pf

        pf.state = state
        pf._day_adopted = True  # 파일의 날짜는 실제로 운영된 날이다
        log.info(
            "상태 로드 (mode=%s, cycle=%d, positions=%d, closed=%d)",
            state.mode, state.cycle, len(state.positions), len(state.closed_trades),
        )
        return pf

    @staticmethod
    def _quarantine(p: Path, tag: str) -> Path:
        """``p``를 ``p.<tag>.<ts>``로 옮긴다. 같은 ms에 두 번 부르면 접미사를 늘린다."""
        base = p.with_name(f"{p.name}.{tag}.{_now_ms()}")
        target = base
        n = 1
        while target.exists():
            target = base.with_name(f"{base.name}.{n}")
            n += 1
        fsutil.replace(p, target)
        return target

    # ------------------------------------------------------------ 저장
    def save(self) -> None:
        """tmp 파일에 쓰고 ``os.replace``로 바꿔치기. 실패 시 ``SAVE_RETRIES``회 재시도.

        최종 실패는 마지막 ``OSError``를 그대로 전파한다(사이클은 여기서 멈춰야 한다).
        """
        payload = _dump_state(self.state)
        p = self.path
        tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
        last_exc: OSError | None = None
        attempts = 1 + SAVE_RETRIES
        for attempt in range(1, attempts + 1):
            replacing = False
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:  # 일부 파일시스템은 fsync 미지원
                        pass
                replacing = True
                fsutil.replace(tmp, p, budget_s=SAVE_SHARING_BUDGET_S, sleep=self.sleep)
                _fsync_dir(p.parent)
                if attempt > 1:
                    log.warning("상태 저장 성공 (재시도 %d회 후): %s", attempt - 1, p)
                return
            except OSError as e:
                last_exc = e
                log.warning("상태 저장 실패 (%d/%d): %s: %s", attempt, attempts, type(e).__name__, e)
                if replacing and fsutil.is_transient_sharing_error(e):
                    break           # 공유 위반은 fsutil.replace 가 이미 SAVE_SHARING_BUDGET_S 동안 다시 시도했다
                if attempt < attempts:
                    self.sleep(SAVE_RETRY_DELAY_S)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        log.error("상태 저장 최종 실패: %s", p)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------ 자본
    def equity(self, marks: Mapping[str, float], quote_balance: float | None = None) -> float:
        """총자본(USDT).

        paper: 가상 잔고 + Σ base*mark. testnet/live: ``quote_balance``(거래소 free quote)가 필수.
        mark가 없는 종목은 ``position.last_mark``, 그것도 없으면 진입가로 평가한다.
        """
        if self._mode == "paper":
            cash = self.state.paper_quote_balance
        else:
            if quote_balance is None:
                raise ValueError(f"{self._mode} 모드의 equity()는 quote_balance가 필요합니다")
            if not _finite_nonneg(quote_balance):
                raise ValueError(f"quote_balance가 유효하지 않음: {quote_balance!r}")
            cash = float(quote_balance)
        total = cash
        for symbol, pos in self.state.positions.items():
            mark = marks.get(symbol)
            if mark is None or not _finite_positive(mark):
                mark = pos.last_mark
            if mark is None or not _finite_positive(mark):
                log.warning("%s 시세 없음 -> 진입가로 평가", symbol)
                mark = pos.entry_price
            total += pos.base_amount * mark
        return total

    def update_marks(self, marks: Mapping[str, float]) -> None:
        """보유 포지션의 ``last_mark``를 갱신한다(유효한 값만)."""
        for symbol, pos in self.state.positions.items():
            mark = marks.get(symbol)
            if mark is not None and _finite_positive(mark):
                pos.last_mark = float(mark)

    def update_peak(self, equity: float) -> None:
        """최고 자본 갱신(단조 증가). NaN/inf/음수는 무시."""
        if not _finite_nonneg(equity):
            log.warning("update_peak: 유효하지 않은 equity 무시: %r", equity)
            return
        if equity > self.state.peak_equity:
            self.state.peak_equity = float(equity)

    def ensure_day(self, now: datetime, equity: float) -> DailyStats | None:
        """UTC 날짜가 바뀌면 끝난 날의 통계를 반환하고 새 ``DailyStats``를 시작한다.

        연속 손실 카운트는 날이 바뀌면 0으로 돌아간다(당일 안에서만 누적).
        같은 날이면 None. 단, start_equity가 아직 0(testnet/live 첫 사이클)이면 지금 자본으로 채운다.

        - 새 상태(파일 없이 시작)의 첫 호출은 롤오버가 아니다: 호출자 시계의 날짜를 채택하고 None.
        - 날짜가 **거꾸로** 가면(시계 보정 등) 롤오버하지 않는다: 경고만 남기고 당일 손실 한도·연속 손실
          같은 안전장치 카운터를 그대로 유지한다(None).
        """
        today = _date_str(now)
        daily = self.state.daily
        if not self._day_adopted:
            self._day_adopted = True
            pristine = daily.trades == 0 and daily.realized_pnl == 0.0 and daily.llm_cost_usd == 0.0
            if today != daily.date and pristine:
                log.info("새 상태의 일일 통계 날짜를 호출자 시계로 채택: %s -> %s", daily.date, today)
                daily.date = today
        if today == daily.date:
            if daily.start_equity <= 0 and _finite_positive(equity):
                daily.start_equity = float(equity)
                log.info("당일 시작 자본 설정: %.2f", equity)
            return None
        if today < daily.date:
            log.warning(
                "날짜가 거꾸로 감(%s -> %s): 시계 확인 필요. 일일 통계를 유지하고 롤오버하지 않음",
                daily.date, today,
            )
            return None
        finished = daily.model_copy(deep=True)
        start = float(equity) if _finite_nonneg(equity) else 0.0
        self.state.daily = DailyStats(date=today, start_equity=start)
        log.info(
            "일일 롤오버 %s -> %s (전일 손익 %.2f, 거래 %d, 시작자본 %.2f)",
            finished.date, today, finished.realized_pnl, finished.trades, start,
        )
        return finished

    # ------------------------------------------------------------ 포지션
    def open_position(
        self,
        fill: Fill,
        stop_loss_price: float,
        take_profit_price: float,
        thesis: str,
        confidence: float,
        cycle: int,
    ) -> Position:
        """매수 체결을 포지션으로 기록한다. paper면 잔고에서 (quote_amount + fee_quote)를 뺀다. 저장은 호출자."""
        if fill.side != "buy":
            raise ValueError(f"open_position은 buy 체결만 받습니다: {fill.side!r}")
        if fill.symbol in self.state.positions:
            raise ValueError(f"이미 보유 중인 종목: {fill.symbol} (already_held)")
        if not _finite_positive(fill.base_amount):
            raise ValueError(f"체결 수량이 유효하지 않음: {fill.base_amount!r}")
        if not _finite_nonneg(fill.quote_amount) or not _finite_nonneg(fill.fee_quote):
            raise ValueError("체결 금액/수수료가 유효하지 않음")
        if not _finite_positive(stop_loss_price) or not _finite_positive(take_profit_price):
            raise ValueError("손절/익절 가격이 유효하지 않음")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            raise ValueError(f"confidence가 유효하지 않음: {confidence!r}")

        entry_price = fill.price if _finite_positive(fill.price) else fill.quote_amount / fill.base_amount
        if not _finite_positive(entry_price):
            raise ValueError(
                f"매수 체결의 진입가가 양수가 아님: price={fill.price!r} quote_amount={fill.quote_amount!r}"
            )
        if not (stop_loss_price < entry_price < take_profit_price):
            log.warning(
                "%s 손절/익절이 진입가와 어긋남: sl=%s entry=%s tp=%s",
                fill.symbol, stop_loss_price, entry_price, take_profit_price,
            )

        # base로 나간 수수료는 순수량에 이미 반영됐다. 현금 수수료만 비용으로 기록한다.
        fee_cash = _cash_fee(fill, entry_price)
        cost = fill.quote_amount + fee_cash
        if self._mode == "paper" and cost > self.state.paper_quote_balance + 1e-9:
            raise ValueError(
                f"paper 잔고 부족: 필요 {cost:.2f} > 보유 {self.state.paper_quote_balance:.2f}"
            )

        pos = Position(
            symbol=fill.symbol,
            base_amount=fill.base_amount,
            entry_price=entry_price,
            quote_spent=fill.quote_amount,
            fee_quote=fee_cash,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            opened_cycle=cycle,
            opened_ts=fill.ts,
            thesis=thesis,
            confidence=confidence,
            entry_order_id=fill.order_id,
            last_mark=entry_price,
        )
        if self._mode == "paper":
            self.state.paper_quote_balance -= cost
        self.state.positions[fill.symbol] = pos
        log.info(
            "진입 기록 [%s] %s %.8g @ %.8g (%.2f USDT, 수수료 %.4f) sl=%.8g tp=%.8g cycle=%d",
            self._mode, fill.symbol, pos.base_amount, entry_price, fill.quote_amount, fee_cash,
            stop_loss_price, take_profit_price, cycle,
        )
        return pos

    def close_position(self, symbol: str, fill: Fill, reason: ExitReason, cycle: int) -> ClosedTrade:
        """매도 체결을 장부에 반영한다.

        - 체결 수량 < 보유 수량*(1-0.5%) -> 부분 청산: 체결분만 ``ClosedTrade(reason="partial")``
          (원가·수수료 비례 배분), 잔여 ``Position`` 유지.
        - 그 외 -> 전량 청산(초과 체결은 보유 수량으로 잘라내고 WARNING).
        - 당일 통계(실현 손익·거래·승/패·연속 손실), 쿨다운(``COOLDOWN_CYCLES``),
          손실이면 복수매매 창(``REVENGE_WINDOW_CYCLES``)을 갱신한다. peak는 호출자가 ``update_peak``.
        """
        pos = self.state.positions.get(symbol)
        if pos is None:
            raise KeyError(f"보유하지 않은 종목: {symbol}")
        if fill.side != "sell":
            raise ValueError(f"close_position은 sell 체결만 받습니다: {fill.side!r}")
        if fill.symbol != symbol:
            raise ValueError(f"체결 종목 불일치: {fill.symbol!r} != {symbol!r}")
        if not _finite_positive(fill.base_amount):
            raise ValueError(f"체결 수량이 유효하지 않음: {fill.base_amount!r}")
        if not _finite_nonneg(fill.quote_amount) or not _finite_nonneg(fill.fee_quote):
            raise ValueError("체결 금액/수수료가 유효하지 않음")

        held = pos.base_amount
        sold = fill.base_amount
        quote_received = fill.quote_amount
        # 매도 수수료도 현금 몫만(base로 나간 몫은 순수량/금액에 이미 반영).
        fill_price = fill.price if _finite_positive(fill.price) else (quote_received / sold if sold > 0 else 0.0)
        fee_exit = _cash_fee(fill, fill_price)
        if sold > held:
            scale = held / sold
            log.warning(
                "%s 초과 체결: %.8g > 보유 %.8g -> 보유 수량으로 잘라냄(금액 비례 %.4f)",
                symbol, sold, held, scale,
            )
            quote_received *= scale
            fee_exit *= scale
            sold = held

        partial = sold < held * (1.0 - PARTIAL_FILL_TOLERANCE)
        if partial:
            fraction = sold / held
            quote_spent = pos.quote_spent * fraction
            fee_entry = pos.fee_quote * fraction
            trade_reason: ExitReason = "partial"
        else:
            sold = held
            quote_spent = pos.quote_spent
            fee_entry = pos.fee_quote
            trade_reason = reason

        exit_price = fill.price if _finite_positive(fill.price) else (quote_received / sold if sold > 0 else 0.0)
        fee_total = fee_entry + fee_exit
        pnl = quote_received - quote_spent - fee_total
        pnl_pct = pnl / quote_spent if quote_spent > 0 else 0.0

        trade = ClosedTrade(
            symbol=symbol,
            base_amount=sold,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quote_spent=quote_spent,
            quote_received=quote_received,
            fee_quote_total=fee_total,
            pnl_quote=pnl,
            pnl_pct=pnl_pct,
            reason=trade_reason,
            opened_cycle=pos.opened_cycle,
            closed_cycle=cycle,
            opened_ts=pos.opened_ts,
            closed_ts=fill.ts,
            thesis=pos.thesis,
            confidence=pos.confidence,
            exit_order_id=fill.order_id,
        )

        # 장부 반영
        if partial:
            pos.base_amount = held - sold
            pos.quote_spent = pos.quote_spent - quote_spent
            pos.fee_quote = pos.fee_quote - fee_entry
            if _finite_positive(exit_price):
                pos.last_mark = exit_price
        else:
            del self.state.positions[symbol]
        if self._mode == "paper":
            self.state.paper_quote_balance += quote_received - fee_exit
        self.state.closed_trades.append(trade)

        # 당일 통계
        daily = self.state.daily
        daily.realized_pnl += pnl
        daily.trades += 1
        if pnl > 0:
            daily.wins += 1
            daily.consecutive_losses = 0
        elif pnl < 0:
            daily.losses += 1
            daily.consecutive_losses += 1

        # 쿨다운 / 복수매매 차단
        self.state.cooldown_until_cycle[symbol] = cycle + HardLimits.COOLDOWN_CYCLES
        if pnl < 0:
            self.state.loss_cooldown_until_cycle[symbol] = cycle + HardLimits.REVENGE_WINDOW_CYCLES

        log.info(
            "%s 기록 [%s] %s %s %.8g @ %.8g -> 손익 %.4f USDT (%.2f%%) cycle=%d%s",
            "부분 청산" if partial else "청산", self._mode, symbol, trade_reason, sold, exit_price,
            pnl, pnl_pct * 100.0, cycle,
            f" (잔여 {pos.base_amount:.8g})" if partial else "",
        )
        return trade

    # ------------------------------------------------------------ 킬스위치 / LLM 일시정지
    def set_kill(self, reason: str) -> None:
        self.state.kill_switch = True
        self.state.kill_reason = reason
        log.error("킬스위치 ON: %s", reason)

    def clear_kill(self) -> None:
        if self.state.kill_switch:
            log.warning("킬스위치 해제 (이전 사유: %s)", self.state.kill_reason)
        self.state.kill_switch = False
        self.state.kill_reason = None

    def pause_llm(self, until_ts: int, reason: str) -> None:
        self.state.llm_paused_until_ts = int(until_ts)
        self.state.llm_pause_reason = reason
        log.warning("LLM 일시정지 until=%d: %s", int(until_ts), reason)

    def llm_paused(self, now_ts: int) -> bool:
        until = self.state.llm_paused_until_ts
        return until is not None and now_ts < until

    # ------------------------------------------------------------ 기타
    @property
    def mode(self) -> str:
        return self._mode

    def __repr__(self) -> str:  # 비밀값 없음: settings는 보관하지 않는다
        s = self.state
        return (
            f"Portfolio(mode={self._mode!r}, path={str(self.path)!r}, cycle={s.cycle}, "
            f"positions={len(s.positions)}, closed={len(s.closed_trades)}, kill={s.kill_switch})"
        )
