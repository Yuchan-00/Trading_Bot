"""journal.py (SPEC §21, v2.1 §20.3a.7·보완 사항) 테스트 — 전부 오프라인."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pytest

from tradingbot import journal as J
from tradingbot.agents.runner import AgentRunner
from tradingbot.agents.schemas import (
    AssumptionAtExit,
    BearOutput,
    BullOutput,
    Debias,
    FlowSymbolView,
    HeadDecision,
    JournalDaily,
    JournalPostTrade,
    RedLine,
    RiskCandidateVerdict,
    TechnicalOutput,
    Thesis,
    ThesisPoint,
)
from tradingbot.config import load_settings
from tradingbot.llm.backend import AgentResult, MockBackend
from tradingbot.llm.cost import BudgetExceeded
from tradingbot.models import ClosedTrade, Position
from tradingbot.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3_600_000
DAY = 86_400_000
T0 = 1_760_000_000_000  # 2025-10-09 근처 epoch ms


# ---------------------------------------------------------------- 헬퍼
def mk_trade(
    *,
    symbol: str = "ETH/USDT",
    pnl_pct: float = 0.01,
    entry: float = 100.0,
    exit_price: float | None = None,
    opened_ts: int = T0,
    hold_ms: int = 4 * HOUR,
    opened_cycle: int = 10,
    bars: int = 16,
    reason: str = "take_profit",
    quote: float = 1000.0,
) -> ClosedTrade:
    if exit_price is None:
        exit_price = entry * (1 + pnl_pct)
    pnl_quote = quote * pnl_pct
    return ClosedTrade(
        symbol=symbol, base_amount=quote / entry, entry_price=entry, exit_price=exit_price, quote_spent=quote,
        quote_received=quote + pnl_quote, fee_quote_total=2.0, pnl_quote=pnl_quote, pnl_pct=pnl_pct,
        reason=reason, opened_cycle=opened_cycle, closed_cycle=opened_cycle + bars, opened_ts=opened_ts,
        closed_ts=opened_ts + hold_ms, thesis="논지 문자열", confidence=0.7, exit_order_id="x1",
    )


def mk_trades(n: int, *, spacing_ms: int = DAY, **kw) -> list[ClosedTrade]:
    """일 1건씩(기본) 교대로 이익/손실, 보유시간 동일, 청산가가 진입가와 충분히 떨어진 거래."""
    out = []
    for i in range(n):
        pnl = 0.02 if i % 2 == 0 else -0.01
        out.append(mk_trade(opened_ts=T0 + i * spacing_ms, pnl_pct=pnl,
                            reason="take_profit" if pnl > 0 else "stop_loss", **kw))
    return out


def mk_position(**kw) -> Position:
    base = dict(symbol="ETH/USDT", base_amount=10.0, entry_price=100.0, quote_spent=1000.0, fee_quote=1.0,
                stop_loss_price=98.0, take_profit_price=104.0, opened_cycle=5, opened_ts=T0, thesis="x",
                confidence=0.7, entry_order_id="o1", last_mark=None)
    base.update(kw)
    return Position(**base)


def mk_thesis(*red_lines: tuple[str, str, float]) -> Thesis:
    return Thesis(
        reasons=["4h 추세 정렬"], assumptions=["펀딩이 과열되지 않는다", "BTC 가 버틴다"],
        red_lines=[RedLine(metric=m, op=op, value=v) for m, op, v in red_lines], horizon_bars=24,
    )


def post_trade_output(trade_id: str = "ETH/USDT-1-2", text: str = "요약") -> JournalPostTrade:
    return JournalPostTrade(
        trade_id=trade_id, summary_bullets=["a", "b", "c"],
        assumptions_at_exit=[AssumptionAtExit(assumption="펀딩", status="holding")],
        broken_assumption=None, near_miss_rules=[], lesson="교훈", lesson_tag="good_exit", telegram_text=text,
    )


def daily_output(text: str = "일일") -> JournalDaily:
    return JournalDaily(summary_bullets=["a", "b", "c"], what_worked=[], what_failed=[], risks=["r1", "r2", "r3"],
                        action_items=[], abstain_reason="표본 부족", do_not_change_yet=True, telegram_text=text)


class FakeRunner:
    """runner.run(role, payload, output_model=...) 만 흉내낸다."""

    def __init__(self, result=None, exc: BaseException | None = None):
        self.result = result
        self.exc = exc
        self.calls: list[tuple[str, dict, type | None]] = []

    def run(self, role_id, payload, *, output_model=None):
        self.calls.append((role_id, payload, output_model))
        if self.exc is not None:
            raise self.exc
        if callable(self.result):
            return self.result(payload, output_model)
        return self.result


def ok_result(output) -> AgentResult:
    return AgentResult(role="journal", ok=True, output=output)


@pytest.fixture
def real_runner(tmp_path):
    def _make(responses=None):
        settings = load_settings({"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "data")})
        backend = MockBackend(responses or {})
        runner = AgentRunner(settings, backend, SkillRegistry(ROOT / "skills"), ROOT / "prompts")
        return runner, backend
    return _make


# ================================================================ JournalState / 저장
class TestJournalState:
    def test_defaults(self):
        js = J.JournalState()
        assert js.closed_count == 0
        assert js.decay_state == "insufficient_sample"
        assert js.bias == {k: "insufficient_sample" for k in J.BIAS_KEYS}
        for f in ("entry_verdicts", "thesis_history", "prev_assumption_status", "news_risk_after_entry",
                  "gate_stats_today", "gate_stats", "health_history", "thesis"):
            assert getattr(js, f) == {}
        assert js.halts_today == []
        assert js.last_daily_review_date is None

    def test_v21_fields_present(self):
        fields = set(J.JournalState.model_fields)
        assert {"closed_count", "decay_state", "bias", "gate_stats", "health_history", "thesis",
                "last_daily_review_date", "entry_verdicts", "thesis_history", "prev_assumption_status",
                "news_risk_after_entry", "gate_stats_today", "halts_today"} <= fields

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            J.JournalState(unknown_field=1)

    def test_invalid_bias_level_rejected(self):
        with pytest.raises(Exception):
            J.JournalState(bias={"disposition": "huge"})

    def test_invalid_decay_rejected(self):
        with pytest.raises(Exception):
            J.JournalState(decay_state="great")

    def test_load_missing_returns_fresh(self, tmp_path):
        js = J.load_journal_state(tmp_path)
        assert js == J.JournalState()

    def test_roundtrip(self, tmp_path):
        js = J.JournalState(closed_count=3)
        J.set_thesis(js, "ETH/USDT", mk_thesis(("price", "<", 95.0)))
        J.append_thesis_history(js, "ETH/USDT", cycle=7, health=8, assumption_status=["holding", "holding"])
        J.record_gate(js, "candidates_seen", 2)
        J.record_halt(js, "daily_loss_limit")
        p = J.save_journal_state(js, tmp_path)
        assert p == tmp_path / "journal_state.json"
        back = J.load_journal_state(tmp_path)
        assert back == js
        assert isinstance(back.thesis["ETH/USDT"], Thesis)

    def test_save_is_deterministic_and_sorted(self, tmp_path):
        js = J.JournalState(gate_stats={"z": 1, "a": 2})
        J.save_journal_state(js, tmp_path)
        a = (tmp_path / "journal_state.json").read_text(encoding="utf-8")
        J.save_journal_state(js, tmp_path)
        b = (tmp_path / "journal_state.json").read_text(encoding="utf-8")
        assert a == b
        data = json.loads(a)
        assert list(data) == sorted(data)

    def test_save_korean_not_escaped(self, tmp_path):
        js = J.JournalState(halts_today=["일일 손실 한도"])
        J.save_journal_state(js, tmp_path)
        assert "일일 손실 한도" in (tmp_path / "journal_state.json").read_text(encoding="utf-8")

    def test_save_atomic_no_tmp_left(self, tmp_path):
        J.save_journal_state(J.JournalState(), tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["journal_state.json"]

    def test_save_creates_dir(self, tmp_path):
        d = tmp_path / "nested" / "dir"
        J.save_journal_state(J.JournalState(), d)
        assert (d / "journal_state.json").exists()

    def test_save_failure_propagates_and_keeps_old(self, tmp_path, monkeypatch):
        J.save_journal_state(J.JournalState(closed_count=1), tmp_path)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(J.os, "replace", boom)
        with pytest.raises(OSError):
            J.save_journal_state(J.JournalState(closed_count=2), tmp_path)
        monkeypatch.undo()
        assert J.load_journal_state(tmp_path).closed_count == 1
        assert not any(".tmp." in p.name for p in tmp_path.iterdir())

    def test_corrupt_json_quarantined(self, tmp_path, caplog):
        (tmp_path / "journal_state.json").write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="tradingbot.journal"):
            js = J.load_journal_state(tmp_path)
        assert js == J.JournalState()
        names = [p.name for p in tmp_path.iterdir()]
        assert "journal_state.json" not in names
        assert any(n.startswith("journal_state.json.corrupt-") for n in names)
        assert "손상" in caplog.text

    def test_schema_invalid_quarantined(self, tmp_path):
        (tmp_path / "journal_state.json").write_text(json.dumps({"closed_count": "many", "bogus": 1}), encoding="utf-8")
        js = J.load_journal_state(tmp_path)
        assert js.closed_count == 0

    def test_classmethod_helpers(self, tmp_path):
        js = J.JournalState(closed_count=5)
        js.save(tmp_path)
        assert J.JournalState.load(tmp_path).closed_count == 5

    def test_append_journal_log(self, tmp_path):
        p = J.append_journal_log(tmp_path, {"b": 1, "a": "한글", "x": float("nan")})
        p2 = J.append_journal_log(tmp_path, {"c": 2})
        assert p == p2 == tmp_path / "logs" / "journal.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0] == '{"a": "한글", "b": 1, "x": null}'


# ================================================================ 상태 기록 헬퍼
class TestRecordHelpers:
    def test_append_thesis_history_normalizes(self):
        js = J.JournalState()
        item = J.append_thesis_history(js, "ETH/USDT", {"cycle": 3, "health": 7, "bogus": 1})
        assert set(item) == set(J.THESIS_HISTORY_KEYS)
        assert item["assumption_status"] == [] and item["red_line_hits"] == []
        assert item["action"] is None and item["exit_flag"] is None and item["unrealized_pct"] is None
        assert js.thesis_history["ETH/USDT"] == [item]

    def test_append_thesis_history_kwargs_override_dict(self):
        js = J.JournalState()
        item = J.append_thesis_history(js, "ETH/USDT", {"health": 3}, health=9, action="hold", exit_flag=False,
                                       unrealized_pct=1.25, red_line_hits=["x"])
        assert item["health"] == 9 and item["action"] == "hold" and item["exit_flag"] is False
        assert item["unrealized_pct"] == 1.25 and item["red_line_hits"] == ["x"]

    def test_health_history_derived(self):
        js = J.JournalState()
        for h in (9, 7, None, 4):
            J.append_thesis_history(js, "ETH/USDT", cycle=1, health=h)
        assert js.health_history["ETH/USDT"] == [9, 7, 4]

    def test_prev_assumption_status_tracks_last_nonempty(self):
        js = J.JournalState()
        J.append_thesis_history(js, "ETH/USDT", health=8, assumption_status=["holding", "weakening"])
        J.append_thesis_history(js, "ETH/USDT", health=8, assumption_status=[])
        assert js.prev_assumption_status["ETH/USDT"] == ["holding", "weakening"]
        J.append_thesis_history(js, "ETH/USDT", health=3, assumption_status=["broken", "weakening"])
        assert js.prev_assumption_status["ETH/USDT"] == ["broken", "weakening"]

    def test_history_cap(self):
        js = J.JournalState()
        for i in range(J.HISTORY_CAP + 10):
            J.append_thesis_history(js, "A/USDT", cycle=i, health=5)
        assert len(js.thesis_history["A/USDT"]) == J.HISTORY_CAP
        assert js.thesis_history["A/USDT"][0]["cycle"] == 10

    def test_nan_values_become_none(self):
        js = J.JournalState()
        item = J.append_thesis_history(js, "A/USDT", cycle=float("nan"), health=float("inf"),
                                       unrealized_pct=float("nan"))
        assert item["cycle"] is None and item["health"] is None and item["unrealized_pct"] is None

    def test_record_gate_today_and_cumulative(self):
        js = J.JournalState()
        J.record_gate(js, "candidates_seen")
        J.record_gate(js, "candidates_seen", 2)
        J.record_gate(js, "g1_dropped")
        assert js.gate_stats_today == {"candidates_seen": 3, "g1_dropped": 1}
        assert js.gate_stats == {"candidates_seen": 3, "g1_dropped": 1}
        J.start_new_day(js, "2026-10-28")
        assert js.gate_stats_today == {} and js.gate_stats["candidates_seen"] == 3
        assert js.last_daily_review_date == "2026-10-28"

    def test_record_gate_unknown_key_warns(self, caplog):
        js = J.JournalState()
        with caplog.at_level(logging.WARNING, logger="tradingbot.journal"):
            J.record_gate(js, "weird")
        assert js.gate_stats_today["weird"] == 1
        assert "weird" in caplog.text

    def test_record_halt_dedup_and_reset(self):
        js = J.JournalState()
        J.record_halt(js, "연속 손실")
        J.record_halt(js, "연속 손실")
        J.record_halt(js, "")
        assert js.halts_today == ["연속 손실"]
        J.start_new_day(js)
        assert js.halts_today == [] and js.last_daily_review_date is None

    def test_record_news_risk_dedup(self):
        js = J.JournalState()
        J.record_news_risk(js, "ETH/USDT", "hack:abc")
        J.record_news_risk(js, "ETH/USDT", "hack:abc")
        J.record_news_risk(js, "ETH/USDT", "reg:def")
        assert js.news_risk_after_entry["ETH/USDT"] == ["hack:abc", "reg:def"]

    def test_record_entry_verdicts_resets_symbol_tracking(self):
        js = J.JournalState()
        J.append_thesis_history(js, "ETH/USDT", health=3, assumption_status=["broken"])
        J.record_news_risk(js, "ETH/USDT", "old")
        th = mk_thesis(("price", "<", 95.0))
        J.record_entry_verdicts(js, "ETH/USDT", {"technical": {"setup_type": "breakout", "confidence": 0.7}},
                                thesis=th, opened_ts=T0, entry_return_1h_pct=3.5)
        assert js.thesis_history["ETH/USDT"] == []
        assert js.health_history["ETH/USDT"] == []
        assert js.news_risk_after_entry["ETH/USDT"] == []
        assert "ETH/USDT" not in js.prev_assumption_status
        assert js.thesis["ETH/USDT"] == th
        v = js.entry_verdicts["ETH/USDT"]
        assert set(v) == set(J.ENTRY_VERDICT_KEYS)
        assert v["technical"] == {"setup_type": "breakout", "confidence": 0.7}
        assert v["bull"] is None and v["bear"] is None and v["risk"] is None and v["head"] is None
        # SPEC v2.1: 모르는 값은 null(False 로 채우면 불일치를 날조한다)
        assert v["channel_agreement"] == {"technical": None, "flow": None, "news": None}
        assert js.entry_context[J.entry_key("ETH/USDT", T0)] == {"setup_type": "breakout", "return_1h_pct": 3.5}

    def test_record_entry_verdicts_thesis_from_dict(self):
        js = J.JournalState()
        J.record_entry_verdicts(js, "A/USDT", None, thesis=mk_thesis().model_dump())
        assert isinstance(js.thesis["A/USDT"], Thesis)
        assert js.entry_verdicts["A/USDT"]["technical"] is None

    def test_entry_context_cap_drops_oldest_by_ts(self):
        js = J.JournalState()
        n = J.ENTRY_CONTEXT_CAP + 3
        # 알파벳 순서와 시간 순서가 다르도록 심볼을 역순으로
        for i in range(n):
            J.record_entry_verdicts(js, f"S{n - i:04d}/USDT", None, opened_ts=T0 + i)
        assert len(js.entry_context) == J.ENTRY_CONTEXT_CAP
        remaining_ts = sorted(int(k.rsplit("-", 1)[1]) for k in js.entry_context)
        assert remaining_ts[0] == T0 + 3

    def test_clear_symbol(self):
        js = J.JournalState()
        J.record_entry_verdicts(js, "ETH/USDT", {}, thesis=mk_thesis(), opened_ts=T0)
        J.append_thesis_history(js, "ETH/USDT", health=5, assumption_status=["holding"])
        J.record_news_risk(js, "ETH/USDT", "n")
        J.record_entry_verdicts(js, "BTC/USDT", {}, thesis=mk_thesis())
        J.clear_symbol(js, "ETH/USDT")
        for d in (js.entry_verdicts, js.thesis_history, js.prev_assumption_status, js.news_risk_after_entry,
                  js.health_history, js.thesis):
            assert "ETH/USDT" not in d
        assert "BTC/USDT" in js.entry_verdicts and "BTC/USDT" in js.thesis
        assert J.entry_key("ETH/USDT", T0) in js.entry_context  # 지표 집계용으로 남김

    def test_clear_symbol_missing_is_noop(self):
        js = J.JournalState()
        J.clear_symbol(js, "NOPE/USDT")
        assert js == J.JournalState()


# ================================================================ make_entry_verdicts
def _tech(direction="long", setup="trend_continuation", conf=0.72):
    return TechnicalOutput.conservative_example("ETH/USDT").model_copy(
        update={"direction": direction, "setup_type": setup, "confidence": conf})


def _flow(bias="long"):
    return FlowSymbolView(symbol="ETH/USDT", posture="neutral", crowding_risk="low", squeeze_direction="none",
                          cascade_risk="low", nearest_long_liq_cluster_pct_below=None, stop_inside_cascade=False,
                          washout_setup=False, block_new_longs=False, block_reason=None, flow_bias=bias,
                          confidence=0.6, evidence=["funding_regime"])


def _bull():
    return BullOutput.conservative_example("ETH/USDT").model_copy(update={
        "case_strength": 4, "confidence": 0.7,
        "thesis_points": [ThesisPoint(claim="추세 지속", evidence_field="technical.setup_type")]})


def _bear():
    return BearOutput.conservative_example("ETH/USDT").model_copy(update={"verdict": "survivable_if_monitored"})


def _risk():
    return RiskCandidateVerdict.conservative_example("ETH/USDT").model_copy(update={
        "verdict": "conditional", "conditions": ["BTC 유지"], "required_monitoring": ["funding"],
        "rr_net_estimate": 1.8, "max_size_pct": 5.0})


def _head():
    return HeadDecision(symbol="ETH/USDT", action="BUY", confidence=0.7, size_pct=5.0, stop_pct=1.5, tp_pct=3.0,
                        thesis=mk_thesis(), disagreement_class="horizon", losing_side_strongest_point="과열",
                        decisive_factor="4h 추세", rationale="근거")


def _debias(chasing=True):
    return Debias(anchored_to_entry=False, ignored_bearish_news=False, overtrading=False, chasing_spike=chasing,
                  acting_on_sentiment_alone=False)


class TestMakeEntryVerdicts:
    def test_full_shape(self):
        v = J.make_entry_verdicts(technical=_tech(), flow=_flow(), bull=_bull(), bear=_bear(), risk=_risk(),
                                  head=_head(), debias=_debias(True), risk_note="노트", event_signal_score=0.4)
        assert set(v) == set(J.ENTRY_VERDICT_KEYS)
        for ch, keys in J.ENTRY_VERDICT_KEYS.items():
            assert set(v[ch]) == set(keys), ch
        assert v["technical"] == {"setup_type": "trend_continuation", "confidence": 0.72}
        assert v["bull"]["thesis_points"] == [{"claim": "추세 지속", "evidence_field": "technical.setup_type"}]
        assert v["bear"]["verdict"] == "survivable_if_monitored"
        assert v["bear"]["death_paths"][0]["family"] == "data_incident"
        assert v["risk"]["risk_note"] == "노트" and v["risk"]["rr_net_estimate"] == 1.8
        assert v["head"]["debias_chasing_spike"] is True
        assert v["channel_agreement"] == {"technical": True, "flow": True, "news": True}
        json.dumps(v)  # JSON 가능

    def test_abstained_channels_none(self):
        v = J.make_entry_verdicts(technical=None, flow=None, bull=None, bear=None, risk=None, head=None)
        assert all(v[ch] is None for ch in ("technical", "bull", "bear", "risk", "head"))
        assert v["channel_agreement"] == {"technical": False, "flow": False, "news": False}

    @pytest.mark.parametrize("score,expected", [(None, False), (0.0, False), (-0.3, False), (0.01, True),
                                                (float("nan"), False)])
    def test_news_agreement(self, score, expected):
        v = J.make_entry_verdicts(technical=None, flow=None, bull=None, bear=None, risk=None, head=None,
                                  event_signal_score=score)
        assert v["channel_agreement"]["news"] is expected

    def test_non_long_disagreement(self):
        v = J.make_entry_verdicts(technical=_tech(direction="flat"), flow=_flow("neutral"), bull=None, bear=None,
                                  risk=None, head=_head(), debias=None)
        assert v["channel_agreement"]["technical"] is False and v["channel_agreement"]["flow"] is False
        assert v["head"]["debias_chasing_spike"] is None

    def test_normalize_drops_extra_keys_and_fills(self):
        v = J.normalize_entry_verdicts({"technical": {"setup_type": "breakout", "extra": 1}, "junk": {},
                                        "channel_agreement": {"technical": 1, "news": None}})
        assert v["technical"] == {"setup_type": "breakout", "confidence": None}
        assert "junk" not in v
        assert v["channel_agreement"] == {"technical": True, "flow": None, "news": None}


# ================================================================ 레드라인
class TestRedLines:
    def _eval(self, thesis, *, mark=100.0, pos=None, funding=None, btc=None, sent=None, now=T0 + 5 * HOUR):
        return J.evaluate_red_lines(thesis, mark, pos or mk_position(), funding, btc, sent, now)

    def test_no_red_lines(self):
        assert self._eval(mk_thesis()) == []

    def test_price_below_hit(self):
        hits = self._eval(mk_thesis(("price", "<", 97.0)), mark=96.5)
        assert len(hits) == 1
        assert "price" in hits[0] and "<" in hits[0] and "97" in hits[0] and "96.5" in hits[0]
        assert "레드라인" in hits[0]

    def test_price_strict_boundary_not_hit(self):
        assert self._eval(mk_thesis(("price", "<", 97.0)), mark=97.0) == []
        assert self._eval(mk_thesis(("price", ">", 97.0)), mark=97.0) == []

    def test_price_above_hit(self):
        assert len(self._eval(mk_thesis(("price", ">", 103.0)), mark=103.5)) == 1

    def test_unrealized_pct_from_entry(self):
        # entry 100, mark 98.4 → −1.6%
        th = mk_thesis(("unrealized_pct", "<", -1.5))
        assert len(self._eval(th, mark=98.4)) == 1
        assert self._eval(th, mark=98.6) == []

    def test_unrealized_pct_uses_position_entry(self):
        pos = mk_position(entry_price=200.0)
        th = mk_thesis(("unrealized_pct", ">", 4.0))
        assert len(self._eval(th, mark=208.2, pos=pos)) == 1
        assert self._eval(th, mark=207.9, pos=pos) == []

    def test_funding(self):
        th = mk_thesis(("funding_rate", ">", 0.0005))
        assert len(self._eval(th, funding=0.0006)) == 1
        assert self._eval(th, funding=0.0004) == []

    def test_btc_return(self):
        th = mk_thesis(("btc_return_1h_pct", "<", -2.0))
        assert len(self._eval(th, btc=-2.5)) == 1
        assert self._eval(th, btc=-1.0) == []

    def test_sentiment(self):
        th = mk_thesis(("composite_sentiment", ">", 80.0))
        assert len(self._eval(th, sent=85.0)) == 1
        assert self._eval(th, sent=79.0) == []

    def test_hours_held_from_opened_ts(self):
        th = mk_thesis(("hours_held", ">", 12.0))
        assert self._eval(th, now=T0 + 12 * HOUR) == []
        assert len(self._eval(th, now=T0 + 13 * HOUR)) == 1

    def test_missing_metrics_not_triggered_but_logged(self, caplog):
        th = mk_thesis(("funding_rate", ">", 0.0001), ("btc_return_1h_pct", "<", 0.0),
                       ("composite_sentiment", "<", 100.0))
        with caplog.at_level(logging.INFO, logger="tradingbot.journal"):
            hits = self._eval(th, funding=None, btc=None, sent=float("nan"))
        assert hits == []
        assert "funding_rate" in caplog.text and "btc_return_1h_pct" in caplog.text
        assert "composite_sentiment" in caplog.text

    def test_bad_mark_means_missing_price_metrics(self):
        th = mk_thesis(("price", "<", 1e9), ("unrealized_pct", "<", 1e9))
        assert self._eval(th, mark=float("nan")) == []
        assert self._eval(th, mark=0.0) == []
        assert self._eval(th, mark=-5.0) == []

    def test_hours_held_before_open_missing(self):
        th = mk_thesis(("hours_held", "<", 1e9))
        assert self._eval(th, now=T0 - HOUR) == []

    def test_multiple_hits_preserve_order(self):
        th = mk_thesis(("btc_return_1h_pct", "<", -1.0), ("price", "<", 99.0), ("funding_rate", ">", 1.0))
        hits = self._eval(th, mark=98.0, btc=-3.0, funding=0.0001)
        assert len(hits) == 2
        assert "btc_return_1h_pct" in hits[0] and "price" in hits[1]

    def test_korean_label_included(self):
        hits = self._eval(mk_thesis(("hours_held", ">", 1.0)))
        assert "보유시간" in hits[0]

    def test_red_line_metrics_values(self):
        vals = J.red_line_metrics(110.0, mk_position(), 0.0001, -0.5, 55.0, T0 + 2 * HOUR)
        assert vals["price"] == 110.0
        assert vals["unrealized_pct"] == pytest.approx(10.0)
        assert vals["hours_held"] == pytest.approx(2.0)
        assert vals["funding_rate"] == 0.0001 and vals["btc_return_1h_pct"] == -0.5
        assert vals["composite_sentiment"] == 55.0


# ================================================================ 편향 심각도
class TestBiasSeverities:
    def test_min_sample(self):
        assert J.bias_severities(mk_trades(19), 15) == {k: "insufficient_sample" for k in J.BIAS_KEYS}
        assert J.bias_severities([], 15) == {k: "insufficient_sample" for k in J.BIAS_KEYS}

    def test_exactly_20_evaluates(self):
        out = J.bias_severities(mk_trades(20), 15)
        assert set(out) == set(J.BIAS_KEYS)
        assert out["disposition"] == "none"
        assert out["overtrading"] == "none"
        assert out["chasing"] == "insufficient_sample"   # 진입 직전 수익률 정보 없음
        assert out["anchoring"] == "none"

    # --- disposition
    def _disp_trades(self, loss_hold_h: float, win_hold_h: float = 4.0) -> list[ClosedTrade]:
        out = []
        for i in range(20):
            win = i % 2 == 0
            out.append(mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.02 if win else -0.01,
                                hold_ms=int((win_hold_h if win else loss_hold_h) * HOUR)))
        return out

    @pytest.mark.parametrize("loss_h,expected", [(4.0, "none"), (4.7, "none"), (4.8, "mild"), (5.9, "mild"),
                                                 (6.0, "severe"), (12.0, "severe"), (1.0, "none")])
    def test_disposition_thresholds(self, loss_h, expected):
        assert J.bias_severities(self._disp_trades(loss_h), 15)["disposition"] == expected

    def test_disposition_ratio_values(self):
        assert J.disposition_ratio(self._disp_trades(6.0), 15) == pytest.approx(1.5)

    def test_disposition_no_losses_none(self):
        trades = [mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.01) for i in range(20)]
        assert J.bias_severities(trades, 15)["disposition"] == "none"

    def test_disposition_no_wins_insufficient(self):
        trades = [mk_trade(opened_ts=T0 + i * DAY, pnl_pct=-0.01, reason="stop_loss") for i in range(20)]
        assert J.bias_severities(trades, 15)["disposition"] == "insufficient_sample"

    def test_disposition_falls_back_to_cycles(self):
        # 시각 차이가 0 이면 사이클 × cycle_minutes 로 보유시간 계산
        out = []
        for i in range(20):
            win = i % 2 == 0
            out.append(mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.02 if win else -0.01, hold_ms=0,
                                bars=10 if win else 16))
        assert J.disposition_ratio(out, 15) == pytest.approx(1.6)
        assert J.bias_severities(out, 15)["disposition"] == "severe"

    # --- overtrading
    def _per_day(self, per_day: int, days: int = 3) -> list[ClosedTrade]:
        out = []
        for d in range(days):
            for k in range(per_day):
                i = len(out)
                out.append(mk_trade(opened_ts=T0 + d * DAY + k * 60_000, pnl_pct=0.02 if i % 2 == 0 else -0.01))
        return out

    @pytest.mark.parametrize("per_day,expected", [(7, "none"), (8, "none"), (9, "mild"), (12, "mild"),
                                                  (13, "severe")])
    def test_overtrading_thresholds(self, per_day, expected):
        trades = self._per_day(per_day, days=max(3, math.ceil(20 / per_day)))
        assert len(trades) >= 20
        assert J.bias_severities(trades, 15)["overtrading"] == expected

    def test_overtrading_window_last_7_days(self):
        # 오래전 하루 30건 폭주 + 최근 7일은 하루 2건 → 최근 창만 본다
        old = [mk_trade(opened_ts=T0 + k * 60_000) for k in range(30)]
        recent = [mk_trade(opened_ts=T0 + 30 * DAY + d * DAY + k * 60_000) for d in range(7) for k in range(2)]
        assert J.trades_per_day(old + recent) == pytest.approx(2.0)
        assert J.bias_severities(old + recent, 15)["overtrading"] == "none"

    def test_trades_per_day_empty(self):
        assert J.trades_per_day([]) is None

    # --- chasing
    def _chasing(self, n_chase: int, n_total: int = 20, ret: float = 3.0):
        trades = mk_trades(n_total)
        rets = {}
        for i, t in enumerate(trades):
            rets[J.entry_key(t.symbol, t.opened_ts)] = ret if i < n_chase else 0.5
        return trades, rets

    @pytest.mark.parametrize("n_chase,expected", [(0, "none"), (6, "none"), (7, "mild"), (10, "mild"),
                                                  (11, "severe"), (20, "severe")])
    def test_chasing_thresholds(self, n_chase, expected):
        trades, rets = self._chasing(n_chase)
        assert J.bias_severities(trades, 15, entry_returns_1h_pct=rets)["chasing"] == expected

    def test_chasing_return_boundary_inclusive(self):
        trades, rets = self._chasing(20, ret=3.0)
        assert J.bias_severities(trades, 15, entry_returns_1h_pct=rets)["chasing"] == "severe"
        trades, rets = self._chasing(20, ret=2.99)
        assert J.bias_severities(trades, 15, entry_returns_1h_pct=rets)["chasing"] == "none"

    def test_chasing_accepts_dict_values_and_trade_id_keys(self):
        trades = mk_trades(20)
        rets = {J.trade_id(t): {"return_1h_pct": 5.0} for t in trades}
        assert J.bias_severities(trades, 15, entry_returns_1h_pct=rets)["chasing"] == "severe"

    def test_chasing_needs_20_with_data(self):
        trades, rets = self._chasing(19, n_total=25)
        partial = dict(list(rets.items())[:19])
        assert J.bias_severities(trades, 15, entry_returns_1h_pct=partial)["chasing"] == "insufficient_sample"

    def test_chasing_none_values_excluded(self):
        trades, rets = self._chasing(20)
        rets[next(iter(rets))] = None
        assert J.bias_severities(trades, 15, entry_returns_1h_pct=rets)["chasing"] == "insufficient_sample"

    # --- anchoring
    def _anchor(self, n_near: int, n_total: int = 20):
        out = []
        for i in range(n_total):
            if i < n_near:
                out.append(mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.001, exit_price=100.25))
            else:
                out.append(mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.02, exit_price=102.0))
        return out

    @pytest.mark.parametrize("n_near,expected", [(0, "none"), (6, "none"), (7, "mild"), (10, "mild"),
                                                 (11, "severe")])
    def test_anchoring_thresholds(self, n_near, expected):
        assert J.bias_severities(self._anchor(n_near), 15)["anchoring"] == expected

    def test_anchoring_band_inclusive(self):
        trades = [mk_trade(opened_ts=T0 + i * DAY, exit_price=100.3) for i in range(20)]
        assert J.bias_severities(trades, 15)["anchoring"] == "severe"
        trades = [mk_trade(opened_ts=T0 + i * DAY, exit_price=99.7) for i in range(20)]
        assert J.bias_severities(trades, 15)["anchoring"] == "severe"
        trades = [mk_trade(opened_ts=T0 + i * DAY, exit_price=100.31) for i in range(20)]
        assert J.bias_severities(trades, 15)["anchoring"] == "none"

    def test_values_are_valid_levels(self):
        out = J.bias_severities(self._anchor(11), 15)
        assert all(v in J.BIAS_LEVELS for v in out.values())


# ================================================================ 쇠퇴 상태
def _pf_trades(n: int, gross_win: float, gross_loss: float, *, start: int = 0) -> list[ClosedTrade]:
    """n 건(짝수): 절반 이익, 절반 손실로 총이익/총손실을 맞춘다(quote 1000 기준)."""
    half = n // 2
    out = []
    for i in range(n):
        win = i < half
        pnl_q = (gross_win / half) if win else -(gross_loss / (n - half))
        out.append(mk_trade(opened_ts=T0 + (start + i) * DAY, pnl_pct=pnl_q / 1000.0,
                            reason="take_profit" if win else "stop_loss"))
    return out


class TestDecayState:
    def test_insufficient(self):
        assert J.decay_state([]) == "insufficient_sample"
        assert J.decay_state(_pf_trades(18, 300, 100)) == "insufficient_sample"

    @pytest.mark.parametrize("gw,gl,expected", [(110, 100, "healthy"), (300, 100, "healthy"),
                                                (109, 100, "warning"), (90, 100, "warning"),
                                                (89, 100, "decayed"), (0, 100, "decayed")])
    def test_thresholds(self, gw, gl, expected):
        assert J.decay_state(_pf_trades(20, gw, gl)) == expected

    def test_no_losses_healthy(self):
        assert J.decay_state([mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.01) for i in range(20)]) == "healthy"

    def test_all_flat_warning(self):
        assert J.decay_state([mk_trade(opened_ts=T0 + i * DAY, pnl_pct=0.0) for i in range(20)]) == "warning"

    def test_uses_last_20_by_close_time(self):
        old_bad = _pf_trades(20, 10, 100, start=0)
        new_good = _pf_trades(20, 300, 100, start=100)
        assert J.decay_state(old_bad + new_good) == "healthy"
        # 입력 순서가 섞여 있어도 closed_ts 기준
        assert J.decay_state(new_good + old_bad) == "healthy"
        assert J.decay_state(new_good + _pf_trades(20, 10, 100, start=200)) == "decayed"

    def test_profit_factor_helper(self):
        assert J.profit_factor(_pf_trades(4, 200, 100)) == pytest.approx(2.0)
        assert J.profit_factor([]) is None
        assert math.isinf(J.profit_factor([mk_trade(pnl_pct=0.01)]))

    def test_refresh_stats(self):
        js = J.JournalState()
        trades = mk_trades(20)
        for t in trades:
            js.entry_context[J.entry_key(t.symbol, t.opened_ts)] = {"setup_type": "breakout", "return_1h_pct": 4.0}
        J.refresh_stats(js, trades, 15)
        assert js.closed_count == 20
        assert js.decay_state == "healthy"   # PF = 0.2*10/0.1*10 = 2
        assert js.bias["chasing"] == "severe"
        assert js.bias["disposition"] == "none"

    def test_refresh_stats_small_sample(self):
        js = J.JournalState(decay_state="healthy")
        J.refresh_stats(js, mk_trades(5), 15)
        assert js.closed_count == 5 and js.decay_state == "insufficient_sample"
        assert set(js.bias.values()) == {"insufficient_sample"}


# ================================================================ noise_trade_share
class TestNoiseShare:
    def test_below_min(self):
        assert J.noise_trade_share(mk_trades(4)) is None

    def test_share(self):
        trades = [mk_trade(opened_ts=T0 + i, pnl_pct=0.002) for i in range(3)] + \
                 [mk_trade(opened_ts=T0 + 10 + i, pnl_pct=0.01) for i in range(7)]
        assert J.noise_trade_share(trades) == pytest.approx(0.3)

    def test_last_20_only(self):
        noisy_old = [mk_trade(opened_ts=T0 + i, pnl_pct=0.0) for i in range(30)]
        clean_new = [mk_trade(opened_ts=T0 + 100 + i, pnl_pct=0.01) for i in range(20)]
        assert J.noise_trade_share(noisy_old + clean_new) == 0.0

    def test_boundary(self):
        # |pnl_pct| = 0.3% 는 노이즈가 아니다(< 0.3)
        trades = [mk_trade(opened_ts=T0 + i, pnl_pct=-0.003) for i in range(5)]
        assert J.noise_trade_share(trades) == 0.0


# ================================================================ code_metrics
class TestCodeMetrics:
    def test_trade_stats_known(self):
        trades = [
            mk_trade(opened_ts=T0, pnl_pct=0.02, reason="take_profit", bars=10, symbol="ETH/USDT"),
            mk_trade(opened_ts=T0 + 1, pnl_pct=0.04, reason="take_profit", bars=20, symbol="SOL/USDT"),
            mk_trade(opened_ts=T0 + 2, pnl_pct=-0.01, reason="stop_loss", bars=5, symbol="ETH/USDT"),
            mk_trade(opened_ts=T0 + 3, pnl_pct=-0.02, reason="time_stop", bars=96, symbol="ETH/USDT"),
            mk_trade(opened_ts=T0 + 4, pnl_pct=0.001, reason="head_sell", bars=1, symbol="SOL/USDT"),
        ]
        m = J.code_metrics(trades, [], None)
        assert m["n_trades"] == 5 and m["wins"] == 3 and m["losses"] == 2
        assert m["win_rate"] == pytest.approx(0.6)
        # 총이익 20+40+1 = 61, 총손실 10+20 = 30
        assert m["profit_factor"] == pytest.approx(61 / 30)
        assert m["profit_factor_infinite"] is False
        assert m["avg_win_pct"] == pytest.approx((2 + 4 + 0.1) / 3)
        assert m["avg_loss_pct"] == pytest.approx(-1.5)
        assert m["expectancy_pct"] == pytest.approx((2 + 4 - 1 - 2 + 0.1) / 5)
        assert m["expectancy_quote"] == pytest.approx(31 / 5)
        assert m["total_pnl_quote"] == pytest.approx(31.0)
        assert m["avg_bars_held"] == pytest.approx((10 + 20 + 5 + 96 + 1) / 5)
        assert m["by_exit_reason"]["take_profit"] == {"count": 2, "pnl_pct_sum": pytest.approx(6.0)}
        assert m["by_exit_reason"]["time_stop"]["count"] == 1
        assert m["by_symbol"]["ETH/USDT"]["count"] == 3
        assert m["by_symbol"]["SOL/USDT"]["pnl_pct_sum"] == pytest.approx(4.1)
        assert m["attribution_buckets"] == {"noise_trades": 1, "stop_losses": 1, "take_profits": 2,
                                            "time_stops": 1, "head_sells": 1}
        assert m["by_setup"] == {"unknown": {"count": 5, "pnl_pct_sum": pytest.approx(3.1)}}
        assert m["noise_trade_share"] == pytest.approx(0.2)

    def test_expectancy_formula_matches_win_loss(self):
        trades = [mk_trade(opened_ts=T0 + i, pnl_pct=p) for i, p in enumerate([0.03, -0.01, -0.01, 0.02])]
        m = J.code_metrics(trades, [], None)
        wr = m["win_rate"]
        assert m["expectancy_pct"] == pytest.approx(wr * m["avg_win_pct"] + (1 - wr) * m["avg_loss_pct"])

    def test_empty(self):
        m = J.code_metrics([], [], None)
        assert m["n_trades"] == 0 and m["win_rate"] is None and m["profit_factor"] is None
        assert m["avg_win_pct"] is None and m["avg_loss_pct"] is None and m["expectancy_pct"] is None
        assert m["sharpe"] is None and m["sortino"] is None and m["calmar"] is None
        assert m["max_drawdown_pct"] is None and m["total_return_pct"] is None
        assert m["btc_buy_hold_pct"] is None and m["excess_vs_btc_pct"] is None
        assert m["by_exit_reason"] == {} and m["attribution_buckets"]["noise_trades"] == 0

    def test_pf_infinite_flag(self):
        m = J.code_metrics([mk_trade(pnl_pct=0.01)], [], None)
        assert m["profit_factor"] is None and m["profit_factor_infinite"] is True

    def test_by_setup_from_entry_context(self):
        t1 = mk_trade(opened_ts=T0, pnl_pct=0.02)
        t2 = mk_trade(opened_ts=T0 + 1, pnl_pct=-0.01)
        t3 = mk_trade(opened_ts=T0 + 2, pnl_pct=0.01)
        setups = {J.entry_key(t1.symbol, t1.opened_ts): {"setup_type": "breakout"},
                  J.entry_key(t2.symbol, t2.opened_ts): "mean_reversion"}
        m = J.code_metrics([t1, t2, t3], [], None, setups=setups)
        assert m["by_setup"]["breakout"] == {"count": 1, "pnl_pct_sum": pytest.approx(2.0)}
        assert m["by_setup"]["mean_reversion"]["pnl_pct_sum"] == pytest.approx(-1.0)
        assert m["by_setup"]["unknown"]["count"] == 1

    def test_equity_curve_known_values(self):
        curve = [100.0, 110.0, 99.0, 108.9]
        m = J.code_metrics([], curve, None)
        rets = [0.10, -0.10, 0.10]
        mean = sum(rets) / 3
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / 2)
        assert m["n_days"] == 3
        assert m["sharpe"] == pytest.approx(mean / sd * math.sqrt(365))
        dd = math.sqrt((0.1 ** 2) / 3)
        assert m["sortino"] == pytest.approx(mean / dd * math.sqrt(365))
        assert m["max_drawdown_pct"] == pytest.approx(10.0)
        assert m["total_return_pct"] == pytest.approx(8.9)
        assert m["calmar"] is None          # 일수익률 3개 < MIN_CALMAR_DAYS → 연율화하지 않는다
        assert m["equity_start"] == 100.0 and m["equity_end"] == 108.9

    def test_calmar_known_value_with_enough_days(self):
        n = J.MIN_CALMAR_DAYS
        curve = [100.0, 90.0] + [90.0 * (1.005 ** i) for i in range(1, n)]   # 일수익률 n 개
        m = J.code_metrics([], curve, None)
        assert m["n_days"] == n
        growth = curve[-1] / curve[0]
        ann = growth ** (365 / n) - 1
        assert m["max_drawdown_pct"] == pytest.approx(10.0)
        assert m["calmar"] == pytest.approx(ann / 0.10)

    def test_calmar_none_below_min_days(self):
        n = J.MIN_CALMAR_DAYS - 1
        curve = [100.0, 90.0] + [90.0 * (1.005 ** i) for i in range(1, n)]
        m = J.code_metrics([], curve, None)
        assert m["n_days"] == n and m["max_drawdown_pct"] > 0
        assert m["calmar"] is None

    def test_calmar_single_day_is_none(self):
        m = J.code_metrics([], [100.0, 99.957], None)        # 하루치 −0.043% → ^365 연율화하면 ≈ −338
        assert m["n_days"] == 1 and m["calmar"] is None

    def test_monotonic_curve(self):
        m = J.code_metrics([], [100.0, 101.0, 102.0, 103.0], None)
        assert m["max_drawdown_pct"] == 0.0
        assert m["calmar"] is None          # MDD 0 → 정의 안 됨
        assert m["sortino"] is None         # 하방 편차 0
        assert m["sharpe"] is not None and m["sharpe"] > 0

    def test_constant_curve(self):
        m = J.code_metrics([], [100.0] * 5, None)
        assert m["sharpe"] is None and m["sortino"] is None
        assert m["total_return_pct"] == 0.0

    def test_short_curve(self):
        m = J.code_metrics([], [100.0], None)
        assert m["sharpe"] is None and m["max_drawdown_pct"] is None and m["total_return_pct"] is None
        m2 = J.code_metrics([], [100.0, 105.0], None)
        assert m2["sharpe"] is None   # 수익률 1개
        assert m2["total_return_pct"] == pytest.approx(5.0)

    def test_nan_in_curve_skipped(self):
        m = J.code_metrics([], [100.0, float("nan"), 110.0], None)
        assert m["total_return_pct"] == pytest.approx(10.0)

    def test_btc_comparison(self):
        m = J.code_metrics([], [100.0, 102.0, 104.0], [50000.0, 49000.0, 51000.0])
        assert m["btc_buy_hold_pct"] == pytest.approx(2.0)
        assert m["excess_vs_btc_pct"] == pytest.approx(2.0)
        assert m["btc_max_drawdown_pct"] == pytest.approx(2.0)

    def test_btc_underperformance(self):
        m = J.code_metrics([], [100.0, 101.0], [100.0, 110.0])
        assert m["excess_vs_btc_pct"] == pytest.approx(-9.0)

    def test_btc_length_mismatch(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tradingbot.journal"):
            m = J.code_metrics([], [100.0, 101.0, 102.0], [100.0, 110.0])
        assert m["btc_buy_hold_pct"] is None and m["excess_vs_btc_pct"] is None
        assert "btc_curve" in caplog.text

    def test_btc_none(self):
        m = J.code_metrics([], [100.0, 101.0], None)
        assert m["btc_buy_hold_pct"] is None

    def test_json_serializable(self):
        m = J.code_metrics(mk_trades(6), [100.0, 99.0, 101.0], [1.0, 1.1, 1.2])
        json.dumps(m, allow_nan=False)

    def test_max_drawdown_helper(self):
        assert J.max_drawdown_pct([100, 120, 90, 130, 117]) == pytest.approx(25.0)
        assert J.max_drawdown_pct([]) is None


# ================================================================ payload: post_trade
POST_TRADE_TRADE_KEYS = set(ClosedTrade.model_fields) | {"trade_id", "bars_held"}


class TestPostTradePayload:
    def _payload(self, **kw):
        trade = kw.pop("trade", mk_trade(pnl_pct=-0.0123456, reason="stop_loss", entry=43123.456, bars=11))
        thesis = kw.pop("thesis", mk_thesis(("funding_rate", ">", 0.00051234)))
        history = kw.pop("history", [{"cycle": 11, "health": 9, "assumption_status": ["holding", "holding"],
                                      "red_line_hits": [], "action": "hold", "exit_flag": False,
                                      "unrealized_pct": 0.123456}])
        verdicts = kw.pop("verdicts", J.make_entry_verdicts(technical=_tech(), flow=_flow(), bull=_bull(),
                                                             bear=_bear(), risk=_risk(), head=_head(),
                                                             debias=_debias(False), risk_note="n",
                                                             event_signal_score=None))
        return trade, J.build_journal_post_trade(trade, thesis, history, verdicts, **kw)

    def test_top_level_keys(self):
        _, p = self._payload()
        assert set(p) == {"role", "mode", "trade", "thesis_history", "verdicts", "hard_limits"}
        assert p["role"] == "journal" and p["mode"] == "post_trade"
        assert p["hard_limits"] == {"max_position_pct": 10, "max_positions": 3, "max_stop_pct": 2,
                                    "max_tp_pct": 4, "min_confidence": 0.65}

    def test_trade_keys_and_values(self):
        trade, p = self._payload()
        t = p["trade"]
        assert set(t) == POST_TRADE_TRADE_KEYS
        assert t["trade_id"] == f"{trade.symbol}-{trade.opened_ts}-{trade.closed_ts}"
        assert t["bars_held"] == 11
        assert t["pnl_pct"] == pytest.approx(-1.235)          # % 단위 + 4유효자리
        assert t["entry_price"] == 43120.0                     # 4유효자리
        assert t["opened_ts"] == trade.opened_ts               # int 는 반올림하지 않는다
        assert t["thesis"]["assumptions"] == ["펀딩이 과열되지 않는다", "BTC 가 버틴다"]
        assert t["thesis"]["red_lines"][0] == {"metric": "funding_rate", "op": ">", "value": 0.0005123}

    def test_thesis_none(self):
        _, p = self._payload(thesis=None)
        assert p["trade"]["thesis"] is None

    def test_thesis_dict_and_invalid(self):
        _, p = self._payload(thesis=mk_thesis().model_dump())
        assert p["trade"]["thesis"]["horizon_bars"] == 24
        _, p = self._payload(thesis={"garbage": True})
        assert p["trade"]["thesis"] is None

    def test_thesis_history_exact_keys(self):
        _, p = self._payload(history=[{"cycle": 1, "health": 5, "extra": "x"}])
        assert len(p["thesis_history"]) == 1
        assert set(p["thesis_history"][0]) == set(J.THESIS_HISTORY_KEYS)
        _, p2 = self._payload()
        assert p2["thesis_history"][0]["unrealized_pct"] == 0.1235

    def test_empty_history(self):
        _, p = self._payload(history=[])
        assert p["thesis_history"] == []
        _, p = self._payload(history=None)
        assert p["thesis_history"] == []

    def test_verdicts_shape(self):
        _, p = self._payload(news_risk_after_entry=["hack:123"])
        v = p["verdicts"]
        assert set(v) == set(J.ENTRY_VERDICT_KEYS) | {"news_risk_after_entry"}
        assert v["news_risk_after_entry"] == ["hack:123"]
        assert v["head"]["debias_chasing_spike"] is False

    def test_verdicts_none(self):
        _, p = self._payload(verdicts=None)
        v = p["verdicts"]
        assert all(v[k] is None for k in ("technical", "bull", "bear", "risk", "head"))
        assert v["news_risk_after_entry"] == []

    def test_news_from_verdicts_dict(self):
        verdicts = {"news_risk_after_entry": ["x"]}
        _, p = self._payload(verdicts=verdicts)
        assert p["verdicts"]["news_risk_after_entry"] == ["x"]

    def test_deterministic_json(self):
        _, a = self._payload()
        _, b = self._payload()
        sa = json.dumps(a, sort_keys=True, ensure_ascii=False, allow_nan=False)
        assert sa == json.dumps(b, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def test_nan_becomes_null(self):
        trade = mk_trade()
        history = [{"cycle": 1, "health": 5, "unrealized_pct": float("nan")}]
        p = J.build_journal_post_trade(trade, None, history, None)
        assert p["thesis_history"][0]["unrealized_pct"] is None
        json.dumps(p, allow_nan=False)

    def test_no_raw_thesis_string(self):
        _, p = self._payload(thesis=None)
        assert "논지 문자열" not in json.dumps(p, ensure_ascii=False)


# ================================================================ payload: daily
class TestDailyPayload:
    def _table(self):
        js = J.JournalState(decay_state="warning", bias={"disposition": "mild", "overtrading": "none",
                                                          "chasing": "severe", "anchoring": "none"})
        for k, n in {"candidates_seen": 10, "g1_dropped": 4, "g2_bear_decline": 3, "g3_risk_veto": 1,
                     "head_calls": 2}.items():
            J.record_gate(js, k, n)
        J.record_halt(js, "연속 손실")
        trades = mk_trades(6)
        m = J.code_metrics(trades, [10000.0, 9900.0, 10100.0], [60000.0, 61000.0, 59000.0])
        m["day_pnl_pct"] = 0.4567891
        m["llm"] = {"calls": 12, "cost_usd": 1.234567, "budget_usd": 10, "abstain_count": 1, "extra": 5}
        return js, trades, m

    def test_exact_keys(self):
        js, _, m = self._table()
        p = J.build_journal_daily(J.build_metrics_table(m, js), 42)
        assert set(p) == {"role", "mode", "closed_count", "hard_limits", "metrics_table"}
        assert p["role"] == "journal" and p["mode"] == "daily" and p["closed_count"] == 42
        mt = p["metrics_table"]
        assert set(mt) == set(J.METRICS_TABLE_KEYS)
        assert set(mt["returns"]) == {"day_pnl_pct", "cum_pnl_pct", "equity", "max_drawdown_pct",
                                      "btc_buy_hold_pct", "excess_vs_btc_pct"}
        assert set(mt["trade_stats"]) == {"closed_today", "win_rate", "avg_win_pct", "avg_loss_pct",
                                          "profit_factor", "avg_bars_held", "sharpe", "sortino", "calmar"}
        assert set(mt["attribution_buckets"]) == {"noise_trades", "stop_losses", "take_profits", "time_stops",
                                                  "head_sells"}
        assert set(mt["gate_stats"]) == {"candidates_seen", "g1_dropped", "g2_bear_decline", "g3_risk_veto",
                                         "g3_other", "g4_downgrade", "head_calls", "bear_decline_rate",
                                         "risk_veto_rate"}
        assert set(mt["llm"]) == {"calls", "cost_usd", "budget_usd", "abstain_count"}
        assert set(mt["bias_severities"]) == {"disposition", "overtrading", "chasing", "anchoring"}
        for grp in ("by_exit_reason", "by_symbol", "by_setup"):
            for v in mt[grp].values():
                assert set(v) == {"count", "pnl_pct_sum"}

    def test_values(self):
        js, trades, m = self._table()
        mt = J.build_journal_daily(J.build_metrics_table(m, js), 6)["metrics_table"]
        assert mt["returns"]["day_pnl_pct"] == 0.4568
        assert mt["returns"]["cum_pnl_pct"] == pytest.approx(1.0)
        assert mt["returns"]["equity"] == 10100.0
        assert mt["returns"]["max_drawdown_pct"] == 1.0
        assert mt["returns"]["btc_buy_hold_pct"] == pytest.approx(-1.667)
        assert mt["returns"]["excess_vs_btc_pct"] == pytest.approx(2.667)
        assert mt["trade_stats"]["closed_today"] == 6
        assert mt["trade_stats"]["win_rate"] == 0.5
        assert mt["decay_state"] == "warning"
        assert mt["bias_severities"]["chasing"] == "severe"
        assert mt["gate_stats"]["candidates_seen"] == 10 and mt["gate_stats"]["g3_other"] == 0
        assert mt["gate_stats"]["bear_decline_rate"] == 0.5         # 3 / (10-4)
        assert mt["gate_stats"]["risk_veto_rate"] == pytest.approx(0.3333)   # 1 / 3
        assert mt["llm"] == {"calls": 12, "cost_usd": 1.235, "budget_usd": 10.0, "abstain_count": 1}
        assert mt["halts"] == ["연속 손실"]

    def test_kwargs_override(self):
        js, _, m = self._table()
        t = J.build_metrics_table(m, js, closed_today=2, day_pnl_pct=-1.0, cum_pnl_pct=3.0, equity=5.0,
                                  llm={"calls": 1})
        assert t["trade_stats"]["closed_today"] == 2
        assert t["returns"]["day_pnl_pct"] == -1.0 and t["returns"]["cum_pnl_pct"] == 3.0
        assert t["returns"]["equity"] == 5.0
        assert t["llm"] == {"calls": 1, "cost_usd": None, "budget_usd": None, "abstain_count": None}

    def test_normalize_empty_table(self):
        mt = J.build_journal_daily({}, 0)["metrics_table"]
        assert set(mt) == set(J.METRICS_TABLE_KEYS)
        assert all(v is None for v in mt["returns"].values())
        assert mt["decay_state"] == "insufficient_sample"
        assert mt["bias_severities"] == {k: "insufficient_sample" for k in J.BIAS_KEYS}
        assert mt["gate_stats"]["bear_decline_rate"] is None
        assert mt["halts"] == [] and mt["by_symbol"] == {}

    def test_normalize_drops_extra_and_bad_values(self):
        mt = J.normalize_metrics_table({"returns": {"day_pnl_pct": float("inf"), "zzz": 1}, "extra_top": 1,
                                        "decay_state": "awesome", "bias_severities": {"chasing": "bad"},
                                        "by_symbol": {"A/USDT": {"count": 2, "pnl_pct_sum": 1.5, "x": 9}}})
        assert "extra_top" not in mt and "zzz" not in mt["returns"]
        assert mt["returns"]["day_pnl_pct"] is None
        assert mt["decay_state"] == "insufficient_sample"
        assert mt["bias_severities"]["chasing"] == "insufficient_sample"
        assert mt["by_symbol"] == {"A/USDT": {"count": 2, "pnl_pct_sum": 1.5}}

    def test_gate_rates_zero_denominator(self):
        g = J.gate_stats_table({"candidates_seen": 3, "g1_dropped": 3})
        assert g["bear_decline_rate"] is None and g["risk_veto_rate"] is None
        g = J.gate_stats_table(None)
        assert g["candidates_seen"] == 0

    def test_deterministic(self):
        js, _, m = self._table()
        a = json.dumps(J.build_journal_daily(J.build_metrics_table(m, js), 6), sort_keys=True, ensure_ascii=False,
                       allow_nan=False)
        b = json.dumps(J.build_journal_daily(J.build_metrics_table(m, js), 6), sort_keys=True, ensure_ascii=False,
                       allow_nan=False)
        assert a == b

    def test_compact_rounding(self):
        assert J.compact_payload({"a": 1.23456789, "b": [0.000123456], "c": True, "d": 7, "e": None,
                                  "f": float("nan"), "g": -0.0}) == \
            {"a": 1.235, "b": [0.0001235], "c": True, "d": 7, "e": None, "f": None, "g": 0.0}


# ================================================================ 리뷰 호출
class TestPostTradeReview:
    def test_success_with_fake_runner(self):
        trade = mk_trade()
        tid = J.trade_id(trade)
        runner = FakeRunner(ok_result(post_trade_output(tid)))
        out = J.post_trade_review(runner, trade, {"thesis": mk_thesis(), "thesis_history": [], "verdicts": None})
        assert isinstance(out, JournalPostTrade) and out.trade_id == tid
        role, payload, model = runner.calls[0]
        assert role == "journal" and model is JournalPostTrade
        assert payload["mode"] == "post_trade" and payload["trade"]["trade_id"] == tid

    def test_uses_journal_state(self):
        trade = mk_trade(symbol="SOL/USDT")
        js = J.JournalState()
        J.record_entry_verdicts(js, "SOL/USDT", J.make_entry_verdicts(
            technical=_tech(), flow=None, bull=None, bear=None, risk=None, head=None), thesis=mk_thesis())
        J.append_thesis_history(js, "SOL/USDT", cycle=12, health=6, assumption_status=["holding", "weakening"])
        J.record_news_risk(js, "SOL/USDT", "규제 뉴스")
        runner = FakeRunner(ok_result(post_trade_output(J.trade_id(trade))))
        J.post_trade_review(runner, trade, {"journal_state": js})
        p = runner.calls[0][1]
        assert p["trade"]["thesis"]["horizon_bars"] == 24
        assert p["thesis_history"][0]["health"] == 6
        assert p["verdicts"]["technical"]["setup_type"] == "trend_continuation"
        assert p["verdicts"]["news_risk_after_entry"] == ["규제 뉴스"]

    def test_explicit_ctx_overrides_state(self):
        trade = mk_trade()
        js = J.JournalState()
        J.append_thesis_history(js, trade.symbol, health=6)
        runner = FakeRunner(ok_result(post_trade_output(J.trade_id(trade))))
        J.post_trade_review(runner, trade, {"journal_state": js, "thesis_history": []})
        assert runner.calls[0][1]["thesis_history"] == []

    def test_failure_returns_none(self):
        runner = FakeRunner(AgentResult(role="journal", ok=False, error="timeout"))
        assert J.post_trade_review(runner, mk_trade(), {}) is None

    def test_exception_returns_none(self):
        assert J.post_trade_review(FakeRunner(exc=RuntimeError("boom")), mk_trade(), {}) is None

    def test_budget_exceeded_returns_none(self):
        assert J.post_trade_review(FakeRunner(exc=BudgetExceeded("over")), mk_trade(), None) is None

    def test_wrong_output_type_returns_none(self):
        assert J.post_trade_review(FakeRunner(ok_result(daily_output())), mk_trade(), {}) is None
        assert J.post_trade_review(FakeRunner(ok_result("text")), mk_trade(), {}) is None
        assert J.post_trade_review(FakeRunner(None), mk_trade(), {}) is None

    def test_dict_output_validated(self):
        trade = mk_trade()
        out = post_trade_output(J.trade_id(trade)).model_dump()
        res = J.post_trade_review(FakeRunner(ok_result(out)), trade, {})
        assert isinstance(res, JournalPostTrade)

    def test_trade_id_corrected_and_telegram_clipped(self):
        trade = mk_trade()
        runner = FakeRunner(ok_result(post_trade_output("wrong-id", text="가" * 500)))
        out = J.post_trade_review(runner, trade, {})
        assert out.trade_id == J.trade_id(trade)
        assert len(out.telegram_text) <= 400

    def test_with_real_runner_mock_backend(self, real_runner):
        trade = mk_trade(reason="stop_loss", pnl_pct=-0.015)
        expected = post_trade_output(J.trade_id(trade))
        runner, backend = real_runner({"journal": lambda payload: expected})
        out = J.post_trade_review(runner, trade, {"thesis": mk_thesis(("price", "<", 95.0))})
        assert out == expected
        call = backend.calls[0]
        assert call["role"] == "journal" and call["output_model"] == "JournalPostTrade"
        assert call["payload"]["mode"] == "post_trade"
        assert call["payload"]["trade"]["pnl_pct"] == -1.5

    def test_real_runner_default_conservative(self, real_runner):
        runner, _ = real_runner()
        out = J.post_trade_review(runner, mk_trade(), {})
        # MockBackend 기본값 = conservative_example → trade_id 를 코드가 교정
        assert out.lesson_tag == "other" and out.trade_id == J.trade_id(mk_trade())

    def test_real_runner_failure(self, real_runner):
        runner, _ = real_runner({"journal": lambda p: AgentResult(role="journal", ok=False, error="refusal:x")})
        assert J.post_trade_review(runner, mk_trade(), {}) is None


class TestDailyReview:
    def test_success_builds_table_from_code_metrics(self):
        js = J.JournalState(closed_count=25, decay_state="healthy")
        trades = mk_trades(4)
        m = J.code_metrics(trades, [100.0, 101.0], None)
        runner = FakeRunner(ok_result(daily_output()))
        out = J.daily_review(runner, js, trades, m)
        assert isinstance(out, JournalDaily)
        role, payload, model = runner.calls[0]
        assert role == "journal" and model is JournalDaily
        assert payload["mode"] == "daily" and payload["closed_count"] == 25
        assert payload["metrics_table"]["decay_state"] == "healthy"
        assert payload["metrics_table"]["trade_stats"]["closed_today"] == 4

    def test_accepts_prebuilt_table(self):
        js = J.JournalState()
        table = {"returns": {"day_pnl_pct": 1.0}, "trade_stats": {"closed_today": 3}, "decay_state": "decayed"}
        runner = FakeRunner(ok_result(daily_output()))
        J.daily_review(runner, js, mk_trades(3), table)
        mt = runner.calls[0][1]["metrics_table"]
        assert mt["returns"]["day_pnl_pct"] == 1.0 and mt["decay_state"] == "decayed"
        assert runner.calls[0][1]["closed_count"] == 3   # state.closed_count 0 → len(trades)

    def test_failure_none(self):
        runner = FakeRunner(AgentResult(role="journal", ok=False, error="parse_error"))
        assert J.daily_review(runner, J.JournalState(), [], {}) is None
        assert J.daily_review(FakeRunner(exc=BudgetExceeded("x")), J.JournalState(), [], {}) is None
        assert J.daily_review(FakeRunner(ok_result(post_trade_output())), J.JournalState(), [], {}) is None

    def test_telegram_clipped(self):
        out = J.daily_review(FakeRunner(ok_result(daily_output("나" * 450))), J.JournalState(), [], {})
        assert len(out.telegram_text) == 400

    def test_with_real_runner_mock_backend(self, real_runner):
        runner, backend = real_runner()
        js = J.JournalState(closed_count=9)
        out = J.daily_review(runner, js, mk_trades(9), J.code_metrics(mk_trades(9), [1.0, 1.01, 1.02], None))
        assert out == JournalDaily.conservative_example()
        call = backend.calls[0]
        assert call["output_model"] == "JournalDaily" and call["payload"]["mode"] == "daily"
        assert call["payload"]["closed_count"] == 9
        json.dumps(call["payload"], allow_nan=False)


# ================================================================ 코드 요약
class TestCodeSummaries:
    def test_post_trade_summary(self):
        s = J.code_post_trade_summary(mk_trade(pnl_pct=-0.0123, reason="stop_loss", bars=7))
        assert "stop_loss" in s and "-1.23%" in s and "7사이클" in s
        assert len(s) <= 400

    def test_daily_summary(self):
        js = J.JournalState(bias={"disposition": "mild", "overtrading": "none", "chasing": "none",
                                  "anchoring": "none"})
        t = J.build_metrics_table(J.code_metrics(mk_trades(4), [100.0, 101.0], [1.0, 1.0]), js,
                                  day_pnl_pct=1.0)
        s = J.code_daily_summary(t, 4)
        assert "+1.00%" in s and "disposition" in s and "누적 청산 4건" in s
        assert len(s) <= 400

    def test_daily_summary_empty(self):
        s = J.code_daily_summary({}, 0)
        assert "n/a" in s and "편향 없음" in s


# ================================================================ 기타
def test_trade_id_format():
    t = mk_trade(symbol="BTC/USDT", opened_ts=111, hold_ms=222)
    assert J.trade_id(t) == "BTC/USDT-111-333"
    assert J.entry_key("BTC/USDT", 111) == "BTC/USDT-111"


def test_no_print_and_logger_name():
    src = (ROOT / "tradingbot" / "journal.py").read_text(encoding="utf-8")
    assert "print(" not in src
    assert J.log.name == "tradingbot.journal"


def test_hard_limits_payload_matches_hardlimits():
    from tradingbot.config import HardLimits
    hl = J.HARD_LIMITS_PAYLOAD
    assert hl["max_position_pct"] == HardLimits.MAX_POSITION_PCT * 100
    assert hl["max_positions"] == HardLimits.MAX_POSITIONS
    assert hl["max_stop_pct"] == HardLimits.MAX_STOP_LOSS_PCT * 100
    assert hl["max_tp_pct"] == HardLimits.MAX_TAKE_PROFIT_PCT * 100
    assert hl["min_confidence"] == HardLimits.MIN_CONFIDENCE
