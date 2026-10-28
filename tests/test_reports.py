"""tradingbot.reports 테스트 (SPEC §12). 전부 오프라인, tmp_path 사용."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradingbot.models import BotState, ClosedTrade, DailyStats, Position, TradeEvent
from tradingbot.reports import (
    append_decision,
    append_journal,
    daily_summary,
    dumps,
    read_jsonl,
    render_cycle_markdown,
    write_cycle_report,
)


# ---------------------------------------------------------------- 도우미
def read_lines(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def make_trade(**over) -> ClosedTrade:
    base = dict(
        symbol="BTC/USDT", base_amount=0.005, entry_price=80_000.0, exit_price=81_000.0,
        quote_spent=400.0, quote_received=405.0, fee_quote_total=0.8, pnl_quote=4.2, pnl_pct=0.0105,
        reason="take_profit", opened_cycle=10, closed_cycle=14, opened_ts=1_700_000_000_000,
        closed_ts=1_700_003_600_000, thesis="추세 추종 진입", confidence=0.72, exit_order_id="x1",
    )
    base.update(over)
    return ClosedTrade(**base)


def make_state(**over) -> BotState:
    base = dict(
        mode="paper", cycle=42, paper_quote_balance=9_500.0, peak_equity=10_200.0,
        daily=DailyStats(date="2026-10-28", start_equity=10_000.0, realized_pnl=-35.5, trades=4,
                         wins=1, losses=3, consecutive_losses=2, llm_cost_usd=0.31),
    )
    base.update(over)
    return BotState(**base)


# ---------------------------------------------------------------- dumps
class TestDumps:
    def test_sort_keys_and_unicode(self):
        assert dumps({"b": 1, "a": "한글"}) == '{"a": "한글", "b": 1}'

    def test_nested_sorted(self):
        assert dumps({"z": {"y": 1, "x": 2}, "a": [3, {"d": 1, "c": 2}]}) == '{"a": [3, {"c": 2, "d": 1}], "z": {"x": 2, "y": 1}}'

    def test_pydantic_model_and_path_and_datetime(self):
        t = make_trade()
        out = json.loads(dumps({"t": t, "p": Path("a/b"), "d": datetime(2026, 1, 2, tzinfo=timezone.utc)}))
        assert out["t"]["symbol"] == "BTC/USDT"
        assert out["p"] == "a/b"
        assert out["d"] == "2026-01-02T00:00:00+00:00"

    def test_set_sorted(self):
        assert json.loads(dumps({"s": {"b", "a"}})) == {"s": ["a", "b"]}

    def test_unknown_object_stringified(self):
        class Weird:
            def __str__(self):
                return "weird!"

        assert json.loads(dumps({"w": Weird()})) == {"w": "weird!"}


# ---------------------------------------------------------------- append_decision
class TestAppendDecision:
    def test_appends_sorted_lines(self, tmp_path: Path):
        p = tmp_path / "logs" / "decisions.jsonl"
        append_decision(p, {"ts": 1, "symbol": "BTC/USDT", "action": "HOLD", "cycle": 1})
        append_decision(p, {"ts": 2, "cycle": 2, "action": "BUY", "symbol": "ETH/USDT"})
        raw = p.read_text(encoding="utf-8").splitlines()
        assert raw[0] == '{"action": "HOLD", "cycle": 1, "symbol": "BTC/USDT", "ts": 1}'
        assert raw[1] == '{"action": "BUY", "cycle": 2, "symbol": "ETH/USDT", "ts": 2}'
        assert len(read_lines(p)) == 2

    def test_creates_parent_dirs(self, tmp_path: Path):
        p = tmp_path / "deep" / "er" / "d.jsonl"
        append_decision(p, {"ts": 1})
        assert p.exists()

    def test_fills_ts_when_missing(self, tmp_path: Path):
        p = tmp_path / "d.jsonl"
        before = datetime.now(tz=timezone.utc).timestamp() * 1000
        append_decision(p, {"cycle": 3})
        rec = read_lines(p)[0]
        assert rec["cycle"] == 3
        assert isinstance(rec["ts"], int) and rec["ts"] >= int(before) - 1

    def test_does_not_mutate_input(self, tmp_path: Path):
        rec = {"cycle": 1}
        append_decision(tmp_path / "d.jsonl", rec)
        assert rec == {"cycle": 1}

    def test_korean_not_escaped(self, tmp_path: Path):
        p = tmp_path / "d.jsonl"
        append_decision(p, {"ts": 1, "reason": "추세 전환"})
        assert "추세 전환" in p.read_text(encoding="utf-8")
        assert "\\u" not in p.read_text(encoding="utf-8")

    def test_newlines_in_strings_stay_one_line(self, tmp_path: Path):
        p = tmp_path / "d.jsonl"
        append_decision(p, {"ts": 1, "thesis": "줄1\n줄2"})
        assert len(p.read_text(encoding="utf-8").splitlines()) == 1
        assert read_lines(p)[0]["thesis"] == "줄1\n줄2"

    def test_nested_models_serialized(self, tmp_path: Path):
        p = tmp_path / "d.jsonl"
        pos = Position(symbol="BTC/USDT", base_amount=0.1, entry_price=1.0, quote_spent=0.1,
                       stop_loss_price=0.98, take_profit_price=1.04, opened_cycle=1, opened_ts=1)
        append_decision(p, {"ts": 1, "position": pos})
        assert read_lines(p)[0]["position"]["symbol"] == "BTC/USDT"

    def test_nan_becomes_null(self, tmp_path: Path):
        p = tmp_path / "d.jsonl"
        append_decision(p, {"ts": 1, "x": float("nan"), "nested": [float("inf")]})
        rec = read_lines(p)[0]
        assert rec["x"] is None and rec["nested"] == [None]

    def test_rejects_non_dict(self, tmp_path: Path):
        with pytest.raises(TypeError):
            append_decision(tmp_path / "d.jsonl", ["not", "a", "dict"])  # type: ignore[arg-type]

    def test_deterministic_bytes(self, tmp_path: Path):
        rec = {"ts": 5, "b": [1, 2], "a": {"y": 1.5, "x": "ㄱ"}}
        p1, p2 = tmp_path / "1.jsonl", tmp_path / "2.jsonl"
        append_decision(p1, rec)
        append_decision(p2, dict(reversed(list(rec.items()))))
        assert p1.read_bytes() == p2.read_bytes()

    def test_many_appends_keep_order(self, tmp_path: Path):
        p = tmp_path / "d.jsonl"
        for i in range(50):
            append_decision(p, {"ts": i})
        assert [r["ts"] for r in read_lines(p)] == list(range(50))


# ---------------------------------------------------------------- read_jsonl
class TestReadJsonl:
    def test_missing_file_empty(self, tmp_path: Path):
        assert read_jsonl(tmp_path / "none.jsonl") == []

    def test_skips_broken_and_limits(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text('{"a": 1}\nnot json\n\n[1,2]\n{"a": 2}\n{"a": 3}\n', encoding="utf-8")
        assert read_jsonl(p) == [{"a": 1}, {"a": 2}, {"a": 3}]
        assert read_jsonl(p, limit=2) == [{"a": 2}, {"a": 3}]
        assert read_jsonl(p, limit=0) == []


# ---------------------------------------------------------------- write_cycle_report
PAYLOAD = {
    "ts": 1_761_600_000_000,  # 2025-10-27T21:20:00Z
    "mode": "paper",
    "equity": 10_123.456,
    "daily_pnl": 12.3,
    "positions": {"BTC/USDT": {"base_amount": 0.01}},
    "decisions": [
        {"symbol": "BTC/USDT", "action": "BUY", "confidence": 0.78, "reason": "4h 추세 상승, 15m 눌림목", "executed": True},
        {"symbol": "ETH/USDT", "action": "HOLD", "confidence": 0.5, "thesis": "거래량 부족"},
        {"symbol": "SOL/USDT", "action": "BUY", "confidence": 0.9, "executed": False, "risk_notes": ["쿨다운", "낙폭 주의"]},
    ],
    "exits": [{"symbol": "XRP/USDT", "reason": "stop_loss", "pnl_quote": -3.77, "pnl_pct": -0.0094}],
    "llm_calls": 5,
    "llm_cost_usd": 0.0123,
    "errors": ["news 피드 실패: timeout"],
    "skipped": ["BNB/USDT: stale_15m:1800s"],
}


class TestWriteCycleReport:
    def test_paths_and_json(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "cycles", 7, PAYLOAD)
        assert out == tmp_path / "cycles" / "20251027" / "cycle_7.json"
        assert out.exists()
        md = out.with_suffix(".md")
        assert md.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["cycle"] == 7
        assert data["equity"] == pytest.approx(10_123.456)
        assert data["decisions"][0]["symbol"] == "BTC/USDT"
        # 정렬된 키
        assert list(data.keys()) == sorted(data.keys())

    def test_json_is_deterministic_and_pretty(self, tmp_path: Path):
        a = write_cycle_report(tmp_path / "a", 1, PAYLOAD).read_bytes()
        b = write_cycle_report(tmp_path / "b", 1, dict(reversed(list(PAYLOAD.items())))).read_bytes()
        assert a == b
        assert b"\n  " in a  # indent
        assert a.endswith(b"\n")
        assert "추세".encode("utf-8") in a  # ensure_ascii=False

    def test_markdown_contents_korean(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "cycles", 7, PAYLOAD)
        md = out.with_suffix(".md").read_text(encoding="utf-8")
        assert md.startswith("# 사이클 7 보고서")
        assert "2025-10-27 21:20 UTC" in md
        assert "모드: paper" in md
        assert "자본: 10,123.46 USDT" in md
        assert "당일 실현손익: 12.30 USDT" in md
        assert "보유 포지션: 1개" in md
        assert "LLM 호출: 5회 / 비용 $0.0123" in md
        assert "## 결정 (3)" in md
        assert "- BTC/USDT: **BUY** (확신도 0.78, 체결) — 4h 추세 상승, 15m 눌림목" in md
        assert "- ETH/USDT: **HOLD** (확신도 0.50) — 거래량 부족" in md
        assert "- SOL/USDT: **BUY** (확신도 0.90, 미체결, 리스크: 쿨다운; 낙폭 주의)" in md
        assert "## 청산 (1)" in md
        assert "- XRP/USDT: stop_loss → 손익 -3.77 USDT (-0.94%)" in md
        assert "## 건너뜀 (1)" in md and "BNB/USDT: stale_15m:1800s" in md
        assert "## 오류 (1)" in md and "news 피드 실패: timeout" in md

    def test_markdown_minimal_payload(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 0, {})
        md = out.with_suffix(".md").read_text(encoding="utf-8")
        assert "# 사이클 0 보고서" in md
        assert "자본: - USDT" in md
        assert "LLM 호출: 0회 / 비용 $0.0000" in md
        assert "## 결정 (0)\n- 없음" in md
        assert "## 청산 (0)\n- 없음" in md
        assert "## 오류" not in md and "## 건너뜀" not in md

    def test_date_defaults_to_today_utc(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 3, {"equity": 1.0})
        today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        assert out.parent.name == today

    def test_date_from_date_field(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 3, {"date": "2026-02-03"})
        assert out.parent.name == "20260203"

    def test_ts_in_seconds_also_accepted(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 3, {"ts": 1_761_600_000})
        assert out.parent.name == "20251027"

    def test_llm_nested_dict(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 1, {"llm": {"calls": 2, "cost_usd": 0.5}})
        assert "LLM 호출: 2회 / 비용 $0.5000" in out.with_suffix(".md").read_text(encoding="utf-8")

    def test_overwrite_same_cycle(self, tmp_path: Path):
        d = tmp_path / "c"
        write_cycle_report(d, 1, {"ts": 1_761_600_000_000, "equity": 1.0})
        out = write_cycle_report(d, 1, {"ts": 1_761_600_000_000, "equity": 2.0})
        assert json.loads(out.read_text(encoding="utf-8"))["equity"] == 2.0
        assert sorted(p.name for p in out.parent.iterdir()) == ["cycle_1.json", "cycle_1.md"]  # tmp 없음

    def test_existing_cycle_key_preserved(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 9, {"cycle": 9, "x": 1})
        assert json.loads(out.read_text(encoding="utf-8"))["cycle"] == 9

    def test_does_not_mutate_payload(self, tmp_path: Path):
        payload = {"equity": 1.0}
        write_cycle_report(tmp_path / "c", 2, payload)
        assert payload == {"equity": 1.0}

    def test_nan_in_payload_written_as_null(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 2, {"equity": float("nan"), "decisions": [{"confidence": float("inf")}]})
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["equity"] is None and data["decisions"][0]["confidence"] is None
        assert "자본: - USDT" in out.with_suffix(".md").read_text(encoding="utf-8")

    def test_pydantic_in_payload(self, tmp_path: Path):
        out = write_cycle_report(tmp_path / "c", 2, {"trade": make_trade()})
        assert json.loads(out.read_text(encoding="utf-8"))["trade"]["pnl_quote"] == pytest.approx(4.2)

    def test_rejects_bad_args(self, tmp_path: Path):
        with pytest.raises(ValueError):
            write_cycle_report(tmp_path / "c", -1, {})
        with pytest.raises(TypeError):
            write_cycle_report(tmp_path / "c", 1, "nope")  # type: ignore[arg-type]

    def test_decisions_as_dict_keyed_by_symbol(self, tmp_path: Path):
        payload = {"decisions": {"BTC/USDT": {"symbol": "BTC/USDT", "action": "HOLD"}}}
        md = render_cycle_markdown(1, payload)
        assert "## 결정 (1)" in md and "- BTC/USDT: **HOLD**" in md

    def test_long_reason_clipped(self):
        md = render_cycle_markdown(1, {"decisions": [{"symbol": "A", "action": "HOLD", "reason": "가" * 300}]})
        line = [ln for ln in md.splitlines() if ln.startswith("- A:")][0]
        assert line.endswith("…") and len(line) < 200

    def test_markdown_deterministic(self):
        assert render_cycle_markdown(3, PAYLOAD) == render_cycle_markdown(3, dict(PAYLOAD))


# ---------------------------------------------------------------- append_journal
class TestAppendJournal:
    def test_appends_trade_and_review(self, tmp_path: Path):
        p = tmp_path / "logs" / "journal.jsonl"
        t = make_trade()
        append_journal(p, t, {"lesson": "손절 너무 타이트", "grade": "B"})
        append_journal(p, make_trade(symbol="ETH/USDT", reason="stop_loss", pnl_quote=-3.0), None)
        recs = read_lines(p)
        assert len(recs) == 2
        assert recs[0]["trade"] == t.model_dump(mode="json")
        assert recs[0]["review"] == {"grade": "B", "lesson": "손절 너무 타이트"}
        assert recs[0]["ts"] == t.closed_ts
        assert recs[0]["symbol"] == "BTC/USDT" and recs[0]["reason"] == "take_profit"
        assert recs[0]["pnl_quote"] == pytest.approx(4.2)
        assert recs[1]["review"] is None and recs[1]["symbol"] == "ETH/USDT"

    def test_sorted_keys(self, tmp_path: Path):
        p = tmp_path / "j.jsonl"
        append_journal(p, make_trade(), {"z": 1, "a": 2})
        line = p.read_text(encoding="utf-8").splitlines()[0]
        obj = json.loads(line)
        assert list(obj.keys()) == sorted(obj.keys())
        assert list(obj["trade"].keys()) == sorted(obj["trade"].keys())
        assert line.index('"a": 2') < line.index('"z": 1')

    def test_roundtrip_to_closed_trade(self, tmp_path: Path):
        p = tmp_path / "j.jsonl"
        t = make_trade()
        append_journal(p, t, None)
        assert ClosedTrade(**read_lines(p)[0]["trade"]) == t

    def test_rejects_wrong_types(self, tmp_path: Path):
        with pytest.raises(TypeError):
            append_journal(tmp_path / "j.jsonl", {"symbol": "X"}, None)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            append_journal(tmp_path / "j.jsonl", make_trade(), "review")  # type: ignore[arg-type]

    def test_creates_parent_dir(self, tmp_path: Path):
        p = tmp_path / "a" / "b" / "j.jsonl"
        append_journal(p, make_trade(), None)
        assert p.exists()

    def test_review_not_mutated(self, tmp_path: Path):
        review = {"k": "v"}
        append_journal(tmp_path / "j.jsonl", make_trade(), review)
        assert review == {"k": "v"}


# ---------------------------------------------------------------- daily_summary
class TestDailySummary:
    def test_builds_event(self):
        st = make_state()
        before = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        ev = daily_summary(st, equity=9_964.5, ledger_spent=0.42)
        assert isinstance(ev, TradeEvent)
        assert ev.kind == "daily_summary"
        assert ev.mode == "paper"
        assert ev.ts >= before
        assert ev.equity == pytest.approx(9_964.5)
        assert ev.pnl_quote == pytest.approx(-35.5)
        assert ev.pnl_pct == pytest.approx(-35.5 / 10_000.0)
        assert ev.symbol is None and ev.side is None and ev.price is None
        assert "2026-10-28" in (ev.reason or "")
        x = ev.extra
        assert x["date"] == "2026-10-28"
        assert x["trades"] == 4 and x["wins"] == 1 and x["losses"] == 3
        assert x["consecutive_losses"] == 2
        assert x["cost_usd"] == pytest.approx(0.42)  # ledger 값이 state.daily 보다 우선
        assert x["start_equity"] == pytest.approx(10_000.0)
        assert x["equity_change_pct"] == pytest.approx((9_964.5 - 10_000.0) / 10_000.0)
        assert x["win_rate"] == pytest.approx(0.25)
        assert x["open_positions"] == 0
        assert x["cycle"] == 42 and x["kill_switch"] is False
        assert x["peak_equity"] == pytest.approx(10_200.0)

    def test_zero_trades(self):
        st = make_state(daily=DailyStats(date="2026-01-01", start_equity=100.0))
        ev = daily_summary(st, equity=100.0, ledger_spent=0.0)
        assert ev.pnl_quote == 0.0 and ev.pnl_pct == 0.0
        assert ev.extra["trades"] == 0 and ev.extra["win_rate"] is None

    def test_zero_start_equity_no_division_error(self):
        st = make_state(daily=DailyStats(date="2026-01-01", start_equity=0.0, realized_pnl=5.0))
        ev = daily_summary(st, equity=5.0, ledger_spent=0.0)
        assert ev.pnl_pct == 0.0 and ev.extra["equity_change_pct"] == 0.0

    def test_ledger_spent_fallback_when_nan(self):
        st = make_state()
        ev = daily_summary(st, equity=1.0, ledger_spent=float("nan"))
        assert ev.extra["cost_usd"] == pytest.approx(0.31)

    def test_open_positions_and_unrealized(self):
        pos = Position(symbol="BTC/USDT", base_amount=0.01, entry_price=80_000.0, quote_spent=800.0,
                       stop_loss_price=79_000.0, take_profit_price=82_000.0, opened_cycle=1, opened_ts=1,
                       last_mark=81_000.0)
        pos2 = Position(symbol="ETH/USDT", base_amount=1.0, entry_price=3_000.0, quote_spent=3_000.0,
                        stop_loss_price=2_950.0, take_profit_price=3_100.0, opened_cycle=1, opened_ts=1)
        st = make_state(positions={"BTC/USDT": pos, "ETH/USDT": pos2}, kill_switch=True, kill_reason="수동")
        ev = daily_summary(st, equity=10_000.0, ledger_spent=0.0)
        assert ev.extra["open_positions"] == 2
        assert ev.extra["unrealized_pnl"] == pytest.approx(10.0)  # mark 없는 포지션은 제외
        assert ev.extra["kill_switch"] is True

    def test_event_serializable_and_sorted(self):
        ev = daily_summary(make_state(), equity=1.0, ledger_spent=0.0)
        s = dumps(ev)
        obj = json.loads(s)
        assert obj["kind"] == "daily_summary"
        assert list(obj.keys()) == sorted(obj.keys())

    def test_does_not_mutate_state(self):
        st = make_state()
        snapshot = st.model_dump()
        daily_summary(st, equity=1.0, ledger_spent=1.0)
        assert st.model_dump() == snapshot

    def test_mode_passthrough(self):
        st = make_state(mode="testnet")
        assert daily_summary(st, equity=1.0, ledger_spent=0.0).mode == "testnet"

    def test_no_secrets_in_event(self):
        ev = daily_summary(make_state(), equity=1.0, ledger_spent=0.0)
        text = dumps(ev)
        for forbidden in ("api_key", "token", "secret"):
            assert forbidden not in text.lower()

    def test_pnl_values_finite(self):
        ev = daily_summary(make_state(), equity=1.0, ledger_spent=0.0)
        assert math.isfinite(ev.pnl_pct) and math.isfinite(ev.pnl_quote)
