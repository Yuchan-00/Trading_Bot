"""System review — payload / prompt / schema contract consistency (SPEC v2.1 §20.3a).

Realistic payloads are produced by driving the real ``Cycle`` (``main.build_components`` with the offline
``FakeMarketData`` fixture and the ``approve`` MockBackend scenario) on an injected clock and recording every
``MockBackend.complete`` payload. Tests marked "PROOF" fail against the current code and demonstrate a defect.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tradingbot import main as M
from tradingbot.agents import payloads as P
from tradingbot.agents.schemas import (
    HeadDecision,
    NewsEvent,
    NewsOutput,
    RiskPositionView,
    Thesis,
)
from tradingbot.config import load_settings
from tradingbot.models import NewsItem
from tradingbot.testing import FakeMarketData

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"
UTC = timezone.utc


# ======================================================================= harness
class Bot:
    """Real Cycle on an injected clock; records payloads per role."""

    def __init__(self, tmp_path: Path, start: datetime, *, news: str = "fresh") -> None:
        data = tmp_path / "data"
        env = {"MODE": "paper", "AGENT_BACKEND": "mock", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
               "ANTHROPIC_API_KEY": "", "DATA_DIR": str(data)}
        self.settings = load_settings(env)
        self.comp = M.build_components(self.settings, offline_fixture=True, mock_scenario="approve")
        self.now = start
        clock = lambda: int(self.now.timestamp() * 1000)  # noqa: E731
        self.comp.market._clock = clock
        self.comp.broker._clock_ms = clock
        self.cycle = self.comp.cycle
        self.cycle._now = lambda: self.now
        self.be = self.comp.backend
        self.news_mode = news              # "fresh" | "constant" | "outage_after_first"
        self.n_cycles = 0
        self.exit_phase = False
        self.payloads: dict[str, list[dict]] = {}
        self.by_cycle: list[dict[str, list[dict]]] = []
        orig_scan = self.cycle.scanner.scan

        def scan(state, now_ms, **kw):  # noqa: ANN001
            r = orig_scan(state, now_ms, **kw)
            stamp = 0 if self.news_mode == "constant" else now_ms
            if self.news_mode == "outage_after_first" and self.n_cycles > 1:
                return r.model_copy(update={"news": [], "news_available": False})
            base_ts = now_ms if self.news_mode != "constant" else int(start.timestamp() * 1000)
            items = [NewsItem(title=f"Ethereum upgrade ships {stamp}", source="coindesk.com",
                              url=f"https://coindesk.com/a{stamp}", published_ts=base_ts - 3_600_000,
                              symbols=["ETH"], tier=1),
                     NewsItem(title=f"Solana validator note {stamp}", source="x.com",
                              url=f"https://x.com/b{stamp}", published_ts=base_ts - 7_200_000,
                              symbols=["SOL"], tier=3)]
            return r.model_copy(update={"news": items, "news_available": True})

        self.cycle.scanner.scan = scan

        def news_resp(p: dict) -> NewsOutput:
            evs = [NewsEvent(headline_id=h["id"], score=0.33, event_type="protocol", symbols=["ETH/USDT"],
                             summary="업그레이드", stale=False) for h in p["headlines"][:1]]
            return NewsOutput(events=evs, risk_event=False, risk_event_symbols=[], market_tone=0.1,
                              market_note="중립", feed_health_note="정상")

        approve_risk = self.be.responses["risk_manager"]
        approve_head = self.be.responses["head"]

        def risk_resp(p: dict):  # noqa: ANN202
            out = approve_risk(p)
            if self.exit_phase:
                out = out.model_copy(update={"positions": [
                    RiskPositionView(symbol=x["symbol"], assumption_status=["broken", "holding"],
                                     red_lines_triggered=[], health=3, action="exit", reason="가정 붕괴")
                    for x in p["positions"]]})
            return out

        def head_resp(p: dict):  # noqa: ANN202
            out = approve_head(p)
            sells = [HeadDecision(symbol=f["symbol"], action="SELL", confidence=0.8, size_pct=0, stop_pct=0, tp_pct=0,
                                  thesis=Thesis(reasons=["risk.action: exit"], assumptions=[], red_lines=[],
                                                horizon_bars=0),
                                  disagreement_class="none", losing_side_strongest_point="",
                                  decisive_factor="risk.action", rationale="청산")
                     for f in p["flagged_positions"]]
            return out.model_copy(update={"decisions": [*out.decisions, *sells]})

        self.be.responses.update({"news_sentiment": news_resp, "risk_manager": risk_resp, "head": head_resp})

    def run(self, at: datetime | None = None, *, exit_phase: bool = False) -> dict[str, list[dict]]:
        if at is not None:
            self.now = at
        self.exit_phase = exit_phase
        self.n_cycles += 1
        n0 = len(self.be.calls)
        self.cycle.run_once()
        got: dict[str, list[dict]] = {}
        for c in self.be.calls[n0:]:
            got.setdefault(c["role"], []).append(c["payload"])
            self.payloads.setdefault(c["role"], []).append(c["payload"])
        self.by_cycle.append(got)
        return got


T0 = datetime(2026, 10, 27, 23, 7, 30, tzinfo=UTC)      # outside the funding settlement window


@pytest.fixture(scope="module")
def full_run(tmp_path_factory) -> Bot:
    """buy → hold review → flagged exit (journal post_trade) → daily review (journal daily)."""
    bot = Bot(tmp_path_factory.mktemp("full"), T0)
    bot.run()
    bot.run(T0 + timedelta(minutes=15))
    bot.run(T0 + timedelta(minutes=30), exit_phase=True)
    bot.run(datetime(2026, 10, 28, 0, 7, 30, tzinfo=UTC))
    return bot


def _daily(bot: Bot) -> list[dict]:
    return [p for p in bot.payloads.get("journal", []) if p.get("mode") == "daily"]


# ======================================================================= path machinery
_TOK = re.compile(r"`([^`\n]+)`")
_PATH = re.compile(r"^\.?[a-z_][a-z0-9_]*(\[[a-z_0-9]*\])?((\.[a-z0-9_*]+)(\[[a-z_0-9]*\])?)+$"
                   r"|^[a-z_][a-z0-9_]*\[[a-z_0-9]*\](\.[a-z0-9_]+)*$")


def _paths(x: Any, pre: tuple = ()) -> set[tuple]:
    out = {pre}
    if isinstance(x, dict):
        for k, v in x.items():
            for key in {k, "*" if "/" in k else k}:
                out |= _paths(v, pre + (key,))
    elif isinstance(x, list):
        for v in x:
            out |= _paths(v, pre)
    return out


def _norm(tok: str) -> tuple:
    t = re.sub(r"\[(symbol|sym)\]", ".*", tok.lstrip("."))
    t = re.sub(r"\[\d*\]", "", t)
    parts = [p for p in t.split(".") if p]
    while parts and parts[-1] == "*":
        parts.pop()
    return tuple(parts)


def _resolves(tok: str, paths: set[tuple]) -> bool:
    seq = _norm(tok)
    n = len(seq)
    return any(p[i:i + n] == seq for p in paths for i in range(len(p) - n + 1))


# Prompt tokens that legitimately are not input paths of that role's payload.
_NOT_INPUT = {
    "news_sentiment": {"events[].symbols"},                                    # output field
    "bull": {"position.thesis.assumptions"},                                   # held-position branch (never debated)
    "bear": {"position.thesis.assumptions", "position.unrealized_pct"},        # held-position branch
    "risk_manager": {"event_signal.*", "feature_extract.*", "news_context.*", "news_events[]",
                     "scanner_row.factor_table.*", "scanner_row.factor_table.momentum_4h"},   # bull-payload refs
    "head": {"debias.ignored_bearish_news"},                                   # output field
    "journal": {"metrics_table.by_exit_reason.take_profit.pnl_pct_sum"},       # example key (no TP in fixture)
}


def test_every_prompt_cited_path_resolves_in_realistic_payload(full_run):
    """Coverage guard: every dotted/indexed backticked path in prompts/<role>.md exists in a recorded payload."""
    missing = {}
    for role in ("technical", "derivatives_flow", "news_sentiment", "bull", "bear", "risk_manager", "head", "journal"):
        assert full_run.payloads.get(role), f"harness produced no {role} payload"
        paths: set[tuple] = set()
        for p in full_run.payloads[role]:
            paths |= _paths(p)
        toks = {m for m in _TOK.findall((PROMPTS / f"{role}.md").read_text(encoding="utf-8")) if _PATH.match(m)}
        bad = sorted(t for t in toks - _NOT_INPUT.get(role, set()) if not _resolves(t, paths))
        if bad:
            missing[role] = bad
    assert not missing, missing


def test_channel_separation_in_recorded_payloads(full_run):
    """bull never sees bear keys/outputs, bear never sees bull output, technical never sees thesis/confidence."""
    for p in full_run.payloads["bull"]:
        assert set(p) == set(P.BULL_KEYS)
    for p in full_run.payloads["bear"]:
        assert set(p) == set(P.BEAR_KEYS)
        # no BullOutput field at any depth of the bear payload
        assert not any(k in ("case_strength", "thesis_points", "underreaction_evidence")
                       for path in _paths(p) for k in path)
    for p in full_run.payloads["bull"]:
        assert not any(k in ("death_paths", "greed_flags", "what_would_disprove_bear")
                       for path in _paths(p) for k in path)
    for p in full_run.payloads["technical"]:
        assert set(p) == set(P.TECHNICAL_KEYS)
        if p["position"] is not None:
            assert set(p["position"]) == set(P.TECHNICAL_POSITION_KEYS)
        assert not any(k in ("thesis", "confidence") for path in _paths(p["position"] or {}) for k in path)


# ======================================================================= PROOF tests
def test_scanner_never_supplies_perp_mark_so_perp_premium_is_always_null(monkeypatch, tmp_path):
    """PROOF: FlowPack.perp_premium_pct (SPEC §17: (perp_mark − spot)/spot·100) is cited by the derivatives_flow
    prompt (posture families, crowding, squeeze, confidence) but scanner.py hard-codes ``perp_mark=None``."""
    from tradingbot import scanner as S

    seen: list[Any] = []
    real = S.build_flow_pack

    def spy(symbol, futures, hist, ob, ticker, perp_mark, c1h, prev_oi):  # noqa: ANN001
        seen.append(perp_mark)
        return real(symbol, futures, hist, ob, ticker, perp_mark, c1h, prev_oi)

    monkeypatch.setattr(S, "build_flow_pack", spy)
    bot = Bot(tmp_path, T0)
    got = bot.run()
    packs = got["derivatives_flow"][0]["flow_packs"]
    assert seen and any(p["funding_now"] is not None for p in packs.values())   # futures data is present
    assert any(m is not None for m in seen), "scanner never passes a perp mark price to build_flow_pack"
    assert any(p["perp_premium_pct"] is not None for p in packs.values())


def test_bull_bear_news_context_null_on_cycles_without_new_headlines(tmp_path):
    """PROOF: §20.3a.4 news_context is null only when the news channel abstained. On a normal cycle where the feed
    is up but carries no NEW headline (news_sentiment not called, nothing abstained), bull/bear still get null, so
    their prompts read a phantom abstention (disconfirming item / unknown news_shock / data_incident)."""
    bot = Bot(tmp_path, T0, news="constant")
    first = bot.run()
    assert "news_sentiment" in first
    second = bot.run(T0 + timedelta(minutes=15))
    assert "news_sentiment" not in second                 # nothing new to score — not an abstention
    assert second.get("bull"), "harness: expected a debate on cycle 2"
    head_news = [a["news"] for h in second.get("head", []) for a in h["approved"]]
    for p in second["bull"] + second["bear"]:
        assert p["news_context"] is not None, (
            f"{p['role']} {p['symbol']}: news_context null without abstention (head news block: {head_news})")


def test_headline_polarity_persists_through_news_feed_outage(tmp_path):
    """PROOF: SPEC §18 headline_polarity = news_sentiment result, None when absent. Cycle keeps the last
    market_tone forever (``_last_market_tone`` never cleared), so after the feed goes down (news_sentiment
    abstains with feed_unavailable) the composite sentiment still contains the old headline polarity."""
    bot = Bot(tmp_path, T0, news="outage_after_first")
    bot.run()
    got = bot.run(T0 + timedelta(minutes=15))
    assert "news_sentiment" not in got
    for p in got.get("bull", []) + got.get("bear", []):
        assert p["news_context"] is None                  # channel correctly treated as abstained ...
        comp = p["sentiment"]["components"]
        assert comp["headline_polarity"] is None, f"stale headline_polarity {comp['headline_polarity']} during outage"
    assert got.get("bull"), "harness: expected a debate on cycle 2"


def test_daily_metrics_calmar_annualises_a_single_day(full_run):
    """PROOF: the daily review builds metrics from a 2-point equity curve [yesterday's start, now]; calmar then
    annualises ONE daily return (growth^365) and reports absurd values (e.g. −338)."""
    daily = _daily(full_run)
    assert daily, "harness: expected a daily journal payload"
    ts = daily[-1]["metrics_table"]["trade_stats"]
    assert ts["calmar"] is None or abs(ts["calmar"]) < 20, f"calmar from one day of data: {ts['calmar']}"


def test_daily_cum_pnl_and_max_drawdown_are_one_day_values(tmp_path):
    """PROOF: metrics_table.returns.cum_pnl_pct / max_drawdown_pct come from the same 2-point curve, so on day 2
    they describe only the previous day — not the cumulative return / drawdown from peak."""
    bot = Bot(tmp_path, T0)
    bot.run()
    bot.run(T0 + timedelta(minutes=15))
    bot.run(T0 + timedelta(minutes=30), exit_phase=True)          # three losing round trips on day 1
    bot.run(datetime(2026, 10, 28, 0, 7, 30, tzinfo=UTC))          # review of 10-27
    bot.run(datetime(2026, 10, 29, 0, 7, 30, tzinfo=UTC))          # review of 10-28 (flat day)
    daily = _daily(bot)
    assert len(daily) >= 2, "harness: expected two daily reviews"
    ret = daily[-1]["metrics_table"]["returns"]
    start = float(bot.settings.paper_start_balance)
    expected_cum = (ret["equity"] / start - 1.0) * 100.0
    assert expected_cum < -0.01, "harness: day-1 losses expected"
    assert ret["cum_pnl_pct"] == pytest.approx(expected_cum, abs=0.005), (
        f"cum_pnl_pct {ret['cum_pnl_pct']} vs cumulative {expected_cum:.4f}")
    peak = float(bot.cycle.state.peak_equity)
    dd_from_peak = max(0.0, (1.0 - ret["equity"] / peak) * 100.0)
    assert ret["max_drawdown_pct"] >= dd_from_peak - 0.005, (
        f"max_drawdown_pct {ret['max_drawdown_pct']} < drawdown from peak {dd_from_peak:.4f}")


def test_daily_metrics_btc_and_llm_fields_always_null(full_run):
    """PROOF: prompts/journal.md + performance-attribution cite returns.btc_buy_hold_pct / excess_vs_btc_pct
    (first telegram line: 'P&L against BTC') and llm.calls / llm.abstain_count, but cycle._maybe_daily_review
    hard-codes ``btc_curve=None`` and ``{"calls": None, "abstain_count": None}`` — always null in production."""
    mt = _daily(full_run)[-1]["metrics_table"]
    assert mt["returns"]["btc_buy_hold_pct"] is not None
    assert mt["returns"]["excess_vs_btc_pct"] is not None
    assert mt["llm"]["calls"] is not None
    assert mt["llm"]["abstain_count"] is not None


def test_fake_market_ticker_change_24h_is_decimal_like_ccxt_adapter():
    """PROOF: Ticker.change_24h_pct is a decimal (exchange.py converts ccxt % → decimal; features multiplies by 100).
    FakeMarketData returns percent, so the offline fixture feeds change_24h_pct values like −357% into every
    feature_extract / head label / scanner spike check."""
    now = int(T0.timestamp() * 1000)
    m = FakeMarketData(clock_ms=lambda: now)
    t = m.fetch_ticker("ETH/USDT")
    expected_decimal = m.price_at("ETH/USDT", now) / m.price_at("ETH/USDT", now - 86_400_000) - 1.0
    assert t.change_24h_pct == pytest.approx(expected_decimal, rel=1e-9)


def test_offline_fixture_change_24h_pct_is_plausible(full_run):
    """PROOF (end-to-end view of the fixture unit bug): a synthetic ±4% wave cannot move 24h price by >100%."""
    vals = [p["feature_pack"]["change_24h_pct"] for p in full_run.payloads["technical"]]
    assert all(v is None or abs(v) < 100 for v in vals), vals
