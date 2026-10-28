"""CostLedger 테스트: 단가·기록·복원·예산 검사·UTC 날짜."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingbot.llm.cost import (
    CACHE_WRITE_MULTIPLIER,
    PRICES_USD_PER_MTOK,
    BudgetExceeded,
    CostLedger,
)


class Clock:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kw) -> None:
        self.dt = self.dt + timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def ledger(tmp_path: Path, clock: Clock) -> CostLedger:
    return CostLedger(tmp_path / "cost_ledger.jsonl", 10.0, 200.0, now=clock)


# ---------------------------------------------------------------- 단가/추정
def test_prices_table_has_three_tiers():
    assert set(PRICES_USD_PER_MTOK) == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
    for v in PRICES_USD_PER_MTOK.values():
        assert len(v) == 3 and all(x > 0 for x in v)
        assert v[1] < v[0] < v[2]  # cached < input < output


@pytest.mark.parametrize("model", list(PRICES_USD_PER_MTOK))
def test_estimate_known_models(ledger: CostLedger, model: str):
    p_in, p_cached, p_out = PRICES_USD_PER_MTOK[model]
    got = ledger.estimate(model, 1_000_000, 1_000_000, 1_000_000)
    assert got == pytest.approx(p_in + p_cached + p_out)


def test_estimate_scales_per_million(ledger: CostLedger):
    assert ledger.estimate("claude-haiku-5-5", 1000, 0, 0) == pytest.approx(0.10 / 1000)
    assert ledger.estimate("claude-sonnet-5-5", 0, 2000, 0) == pytest.approx(0.20 * 2000 / 1e6)
    assert ledger.estimate("claude-opus-5-5", 0, 0, 500) == pytest.approx(20.0 * 500 / 1e6)


def test_estimate_zero(ledger: CostLedger):
    assert ledger.estimate("claude-opus-5-5", 0, 0, 0) == 0.0


def test_estimate_unknown_model_uses_opus(ledger: CostLedger, caplog):
    with caplog.at_level(logging.WARNING, logger="tradingbot.llm.cost"):
        got = ledger.estimate("claude-mystery-9", 1000, 1000, 1000)
    assert got == pytest.approx(ledger.estimate("claude-opus-5-5", 1000, 1000, 1000))
    assert any("미지 모델" in r.getMessage() for r in caplog.records)


def test_estimate_prefix_match(ledger: CostLedger):
    # 접두사 일치는 그 모델 단가를 쓴다 (날짜 접미사가 붙어 들어와도)
    assert ledger.estimate("claude-haiku-5-5-xyz", 1000, 0, 0) == pytest.approx(0.10 / 1000)


def test_estimate_negative_tokens_clamped(ledger: CostLedger):
    assert ledger.estimate("claude-opus-5-5", -100, -5, -1) == 0.0


def test_price_override(tmp_path: Path, clock: Clock):
    led = CostLedger(tmp_path / "l.jsonl", 1, 1, now=clock, prices={"claude-haiku-5-5": (1.0, 1.0, 1.0)})
    assert led.estimate("claude-haiku-5-5", 1_000_000, 0, 0) == pytest.approx(1.0)
    # 기본 테이블은 변하지 않는다
    assert PRICES_USD_PER_MTOK["claude-haiku-5-5"] == (0.10, 0.01, 0.50)


def test_invalid_budget_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        CostLedger(tmp_path / "l.jsonl", 0, 1)
    with pytest.raises(ValueError):
        CostLedger(tmp_path / "l.jsonl", 1, -1)


def test_invalid_cache_ttl_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        CostLedger(tmp_path / "l.jsonl", 1, 1, cache_ttl="2h")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 기록
def test_record_returns_cost_and_appends_jsonl(ledger: CostLedger, clock: Clock):
    cost = ledger.record(
        role="analyst", model="claude-haiku-5-5", input_tokens=1000, cached_tokens=500,
        output_tokens=200, cache_write_tokens=300, request_id="req_1",
    )
    expected = (1000 * 0.10 + 500 * 0.01 + 200 * 0.50 + 300 * 0.10 * CACHE_WRITE_MULTIPLIER["1h"]) / 1e6
    assert cost == pytest.approx(expected)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {
        "ts", "date", "role", "model", "input_tokens", "cached_tokens", "cache_write_tokens",
        "output_tokens", "cost_usd", "request_id",
    }
    assert rec["date"] == "2026-10-28"
    assert rec["ts"].startswith("2026-10-28T12:00:00")
    assert rec["role"] == "analyst"
    assert rec["model"] == "claude-haiku-5-5"
    assert rec["input_tokens"] == 1000 and rec["cached_tokens"] == 500
    assert rec["cache_write_tokens"] == 300 and rec["output_tokens"] == 200
    assert rec["request_id"] == "req_1"
    assert rec["cost_usd"] == pytest.approx(expected)
    # sort_keys 직렬화
    assert lines[0] == json.dumps(rec, sort_keys=True, ensure_ascii=False)


def test_cache_write_multiplier_1h_is_2x_and_5m_is_1_25x(tmp_path: Path, clock: Clock):
    l1h = CostLedger(tmp_path / "a.jsonl", 10, 100, now=clock, cache_ttl="1h")
    l5m = CostLedger(tmp_path / "b.jsonl", 10, 100, now=clock, cache_ttl="5m")
    c1 = l1h.record(role="r", model="claude-opus-5-5", input_tokens=0, cached_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)
    c5 = l5m.record(role="r", model="claude-opus-5-5", input_tokens=0, cached_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)
    assert c1 == pytest.approx(4.0 * 2.0)
    assert c5 == pytest.approx(4.0 * 1.25)


def test_record_cached_tokens_at_cached_price(ledger: CostLedger):
    cost = ledger.record(role="r", model="claude-opus-5-5", input_tokens=0, cached_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(0.20)


def test_record_default_request_id_null(ledger: CostLedger):
    ledger.record(role="r", model="claude-opus-5-5", input_tokens=1, cached_tokens=0, output_tokens=1)
    rec = json.loads(ledger.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["request_id"] is None
    assert rec["cache_write_tokens"] == 0


def test_record_accumulates_totals(ledger: CostLedger):
    assert ledger.spent_today() == 0.0
    a = ledger.record(role="a", model="claude-opus-5-5", input_tokens=100_000, cached_tokens=0, output_tokens=10_000)
    b = ledger.record(role="b", model="claude-sonnet-5-5", input_tokens=100_000, cached_tokens=0, output_tokens=10_000)
    assert ledger.spent_today() == pytest.approx(a + b)
    assert ledger.spent_month() == pytest.approx(a + b)
    assert ledger.remaining_today() == pytest.approx(10.0 - a - b)
    assert ledger.remaining_month() == pytest.approx(200.0 - a - b)


def test_record_creates_parent_dir(tmp_path: Path, clock: Clock):
    path = tmp_path / "nested" / "dir" / "ledger.jsonl"
    led = CostLedger(path, 10, 100, now=clock)
    led.record(role="r", model="claude-opus-5-5", input_tokens=1, cached_tokens=0, output_tokens=1)
    assert path.exists()


def test_record_write_failure_logs_and_keeps_memory_totals(tmp_path: Path, clock: Clock, caplog):
    # path 가 디렉터리이면 append 가 실패한다 -> 로그 + 메모리 합계 유지
    path = tmp_path / "asdir"
    path.mkdir()
    led = CostLedger(path, 10, 100, now=clock)
    with caplog.at_level(logging.ERROR, logger="tradingbot.llm.cost"):
        cost = led.record(role="r", model="claude-opus-5-5", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    assert cost == pytest.approx(4.0)
    assert led.spent_today() == pytest.approx(4.0)
    assert any("쓰기 실패" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 복원
def test_restore_totals_on_init(tmp_path: Path, clock: Clock):
    path = tmp_path / "ledger.jsonl"
    first = CostLedger(path, 10, 200, now=clock)
    c1 = first.record(role="a", model="claude-opus-5-5", input_tokens=100_000, cached_tokens=0, output_tokens=0)
    c2 = first.record(role="b", model="claude-haiku-5-5", input_tokens=100_000, cached_tokens=0, output_tokens=0)
    second = CostLedger(path, 10, 200, now=clock)
    assert second.spent_today() == pytest.approx(c1 + c2)
    assert second.spent_month() == pytest.approx(c1 + c2)
    # 복원 후 추가 기록도 누적
    c3 = second.record(role="c", model="claude-opus-5-5", input_tokens=100_000, cached_tokens=0, output_tokens=0)
    assert second.spent_today() == pytest.approx(c1 + c2 + c3)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_restore_separates_today_and_month(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    recs = [
        {"ts": "2026-10-01T00:00:00+00:00", "date": "2026-10-01", "role": "r", "model": "m", "input_tokens": 0,
         "cached_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 1.5, "request_id": None},
        {"ts": "2026-10-28T00:00:00+00:00", "date": "2026-10-28", "role": "r", "model": "m", "input_tokens": 0,
         "cached_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 2.0, "request_id": None},
        {"ts": "2026-09-30T00:00:00+00:00", "date": "2026-09-30", "role": "r", "model": "m", "input_tokens": 0,
         "cached_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 100.0, "request_id": None},
    ]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs), encoding="utf-8")
    led = CostLedger(path, 10, 200, now=Clock(datetime(2026, 10, 28, 23, 59, tzinfo=timezone.utc)))
    assert led.spent_today() == pytest.approx(2.0)
    assert led.spent_month() == pytest.approx(3.5)


def test_restore_ignores_corrupt_lines(tmp_path: Path, clock: Clock, caplog):
    path = tmp_path / "ledger.jsonl"
    good = {"date": "2026-10-28", "cost_usd": 0.5}
    path.write_text(
        "not json\n" + json.dumps(good) + "\n{\"date\": \"2026-10-28\"}\n\n{\"date\": \"2026-10-28\", \"cost_usd\": \"NaN\"}\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="tradingbot.llm.cost"):
        led = CostLedger(path, 10, 200, now=clock)
    assert led.spent_today() == pytest.approx(0.5)
    assert any("손상된 줄" in r.getMessage() for r in caplog.records)


def test_missing_file_starts_empty(tmp_path: Path, clock: Clock):
    led = CostLedger(tmp_path / "nope.jsonl", 10, 200, now=clock)
    assert led.spent_today() == 0.0
    assert led.spent_month() == 0.0
    assert led.remaining_today() == 10.0


# ---------------------------------------------------------------- UTC 날짜
def test_dates_are_utc_even_with_offset_clock(tmp_path: Path):
    # KST 2026-10-29 03:00 == UTC 2026-10-28 18:00
    kst = timezone(timedelta(hours=9))
    led = CostLedger(tmp_path / "l.jsonl", 10, 200, now=Clock(datetime(2026, 10, 29, 3, 0, tzinfo=kst)))
    led.record(role="r", model="claude-opus-5-5", input_tokens=1, cached_tokens=0, output_tokens=0)
    rec = json.loads(led.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["date"] == "2026-10-28"
    assert rec["ts"].startswith("2026-10-28T18:00:00+00:00")


def test_naive_clock_treated_as_utc(tmp_path: Path):
    led = CostLedger(tmp_path / "l.jsonl", 10, 200, now=Clock(datetime(2026, 10, 28, 1, 0)))
    led.record(role="r", model="claude-opus-5-5", input_tokens=1, cached_tokens=0, output_tokens=0)
    rec = json.loads(led.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["date"] == "2026-10-28"
    assert rec["ts"].endswith("+00:00")


def test_default_clock_is_utc_now(tmp_path: Path):
    led = CostLedger(tmp_path / "l.jsonl", 10, 200)
    before = datetime.now(timezone.utc)
    led.record(role="r", model="claude-opus-5-5", input_tokens=1, cached_tokens=0, output_tokens=0)
    rec = json.loads(led.path.read_text(encoding="utf-8").splitlines()[0])
    ts = datetime.fromisoformat(rec["ts"])
    assert ts.tzinfo is not None
    assert abs((ts - before).total_seconds()) < 5


def test_day_rollover_resets_today_but_not_month(ledger: CostLedger, clock: Clock):
    c = ledger.record(role="r", model="claude-opus-5-5", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    assert ledger.spent_today() == pytest.approx(c)
    clock.advance(days=1)
    assert ledger.spent_today() == 0.0
    assert ledger.spent_month() == pytest.approx(c)
    assert ledger.remaining_today() == pytest.approx(10.0)


def test_month_rollover_resets_month(ledger: CostLedger, clock: Clock):
    c = ledger.record(role="r", model="claude-opus-5-5", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    clock.advance(days=5)  # 2026-11-02
    assert ledger.spent_month() == 0.0
    assert ledger.spent_today() == 0.0
    assert c > 0


# ---------------------------------------------------------------- 예산 검사
def test_check_ok_when_under_budget(ledger: CostLedger):
    ledger.check()
    ledger.check(9.99)
    assert ledger.budget_ok(9.99)


def test_check_raises_when_projected_exceeds_daily(ledger: CostLedger):
    with pytest.raises(BudgetExceeded) as ei:
        ledger.check(10.01)
    assert "일일" in str(ei.value)
    assert not ledger.budget_ok(10.01)


def test_check_exact_budget_is_allowed(ledger: CostLedger):
    ledger.check(10.0)  # spent + projected == daily 는 초과가 아니다


def test_check_spent_today_plus_projected(ledger: CostLedger):
    ledger.record(role="r", model="claude-opus-5-5", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)  # 4.0
    ledger.check(6.0)
    with pytest.raises(BudgetExceeded):
        ledger.check(6.01)


def test_check_raises_when_spent_today_already_over(ledger: CostLedger):
    ledger.record(role="r", model="claude-opus-5-5", input_tokens=0, cached_tokens=0, output_tokens=1_000_000)  # 20.0
    with pytest.raises(BudgetExceeded):
        ledger.check()
    with pytest.raises(BudgetExceeded):
        ledger.check(0.0)
    assert ledger.remaining_today() == 0.0


def test_check_monthly_budget(tmp_path: Path, clock: Clock):
    led = CostLedger(tmp_path / "l.jsonl", 100.0, 10.0, now=clock)  # 월 한도가 일 한도보다 작은 경우
    led.record(role="r", model="claude-opus-5-5", input_tokens=2_000_000, cached_tokens=0, output_tokens=0)  # 8.0
    led.check(2.0)
    with pytest.raises(BudgetExceeded) as ei:
        led.check(2.01)
    assert "월간" in str(ei.value)


def test_check_monthly_includes_previous_days(tmp_path: Path, clock: Clock):
    led = CostLedger(tmp_path / "l.jsonl", 100.0, 10.0, now=clock)
    led.record(role="r", model="claude-opus-5-5", input_tokens=2_000_000, cached_tokens=0, output_tokens=0)  # 8.0 on 10-28
    clock.advance(days=1)  # 10-29
    assert led.spent_today() == 0.0
    with pytest.raises(BudgetExceeded):
        led.check(2.5)


def test_check_negative_projection_treated_as_zero(ledger: CostLedger):
    ledger.check(-5.0)


def test_restored_ledger_enforces_budget(tmp_path: Path, clock: Clock):
    path = tmp_path / "l.jsonl"
    first = CostLedger(path, 10.0, 200.0, now=clock)
    first.record(role="r", model="claude-opus-5-5", input_tokens=0, cached_tokens=0, output_tokens=500_000)  # 10.0
    second = CostLedger(path, 10.0, 200.0, now=clock)
    with pytest.raises(BudgetExceeded):
        second.check(0.001)
    second.check(0.0)


def test_repr_has_no_secret_and_shows_totals(ledger: CostLedger):
    r = repr(ledger)
    assert "CostLedger" in r and "today=" in r and "month=" in r
