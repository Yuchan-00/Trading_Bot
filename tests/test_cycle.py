"""cycle.py (SPEC §15·§22, v2.1) — paper 사이클 end-to-end (전부 오프라인).

가짜 MarketData·PaperBroker·MockBackend·가짜 텔레그램 전송·주입 시계로 진입/청산/게이트/예산/기한을 검증한다.
"""
from __future__ import annotations

import json
import math
import random
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingbot import cycle as C
from tradingbot.agents.roles import ROLES
from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    Debias,
    DeathPath,
    FlowOutput,
    FlowSymbolView,
    HeadDecision,
    HeadOutput,
    RedLine,
    RiskCandidateVerdict,
    RiskGlobal,
    RiskOutput,
    RiskPositionView,
    TechnicalOutput,
    Thesis,
    ThesisPoint,
)
from tradingbot.config import ConfigError, load_settings
from tradingbot.exchange import InsufficientFunds, PaperBroker
from tradingbot.features import FeaturePack, FlowPack, RegimePack, RiskPack, SentimentComposite
from tradingbot.journal import JournalState, load_journal_state
from tradingbot.llm.backend import AgentResult, MockBackend
from tradingbot.llm.cost import BudgetExceeded, CostLedger
from tradingbot.models import Candle, FuturesContext, OrderBookSummary, Position, QualityReport, Ticker
from tradingbot.notify import Notifier
from tradingbot.portfolio import CorruptStateError, Portfolio
from tradingbot.scanner import Candidate, LiquidityGate, ScanResult, Scanner, Thresholds
from tradingbot.skills import SkillRegistry

SECRET = "sk-ant-SuperSecretValue-1234567890"
START = datetime(2026, 10, 28, 10, 0, 5, tzinfo=timezone.utc)     # 펀딩창 밖
H = 3_600_000


# ======================================================================= 가짜 부품
class Clock:
    def __init__(self, dt: datetime = START) -> None:
        self.dt = dt
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.dt

    def ms(self) -> int:
        return int(self.dt.timestamp() * 1000)

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.dt += timedelta(seconds=seconds)
        self.mono += seconds


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


class FakeMarket:
    """티커만 필요한 가짜 시세(브로커 체결·보조 마크용)."""

    def __init__(self, clock: Clock, prices: dict[str, float] | None = None) -> None:
        self.clock = clock
        self.prices = dict(prices or {"ETH/USDT": 100.0, "SOL/USDT": 100.0, "BTC/USDT": 100.0})
        self.ticker_calls: list[str] = []

    def fetch_ticker(self, symbol: str) -> Ticker:
        self.ticker_calls.append(symbol)
        p = self.prices[symbol]
        return Ticker(symbol=symbol, last=p, bid=p * 0.9999, ask=p * 1.0001, quote_volume_24h=2e9,
                      change_24h_pct=1.0, ts=self.clock.ms())

    def fetch_ohlcv(self, symbol, timeframe, limit):  # pragma: no cover - 가짜 스캐너 사용
        return []

    def fetch_orderbook_summary(self, symbol, depth=50):  # pragma: no cover
        raise NotImplementedError

    def fetch_futures_context(self, symbol):  # pragma: no cover
        return None

    def server_time_ms(self) -> int:
        return self.clock.ms()


def fp_of(sym: str, price: float, now_ms: int) -> FeaturePack:
    return FeaturePack(symbol=sym, ts=now_ms, price=price, mtf_agreement=True, atr_pct_1h=0.5,
                       noise_floor_stop_pct=0.5, noise_ok_2pct=True, hv_pct_rank=40.0,
                       last_closes_1h=[price * (1 + i * 0.0005) for i in range(20)], change_24h_pct=1.0)


def risk_pack_of(sym: str) -> RiskPack:
    return RiskPack(symbol=sym, var95_1h_pct=0.8, cvar95_1h_pct=1.0, daily_vol_pct=2.0, corr_to_btc_1h=0.5,
                    corr_to_open_positions={}, inverse_vol_size_pct=10.0, cvar_size_pct=10.0, spread_bps=2.0,
                    depth_taker_usdt=10_000_000.0, impact_bps_est=0.1, order_notional_vs_15m_volume_share=0.0001,
                    stop_vs_atr_noise_ok=True, exec_cost_pct=0.21)


class FakeScanner:
    """``Scanner.scan`` 대역: 시세는 FakeMarket 가격, 후보는 ``self.candidates``."""

    def __init__(self, market: FakeMarket, candidates=("ETH/USDT",), *, funding_window: bool = False,
                 hook=None, fail: Exception | None = None) -> None:
        self.market = market
        self.candidates = list(candidates)
        self.funding_window = funding_window
        self.hook = hook
        self.fail = fail
        self.calls: list[dict] = []

    def scan(self, state, now_ms, *, headline_polarity=None, planned_notional=None) -> ScanResult:
        self.calls.append({"now_ms": now_ms, "held": sorted(state.positions), "polarity": headline_polarity})
        if self.hook is not None:
            self.hook()
        if self.fail is not None:
            raise self.fail
        held = sorted(state.positions)
        syms = list(dict.fromkeys([*self.candidates, *held, "BTC/USDT"]))
        px = {s: self.market.prices[s] for s in syms}
        return ScanResult(
            ts=now_ms, universe=syms,
            candidates=[Candidate(symbol=s, rank=i + 1, composite_z=1.0 - 0.1 * i, factor_table={"momentum_4h": 1.0},
                                  reasons=["momentum_4h 상위"]) for i, s in enumerate(self.candidates)],
            skipped=[],
            regime=RegimePack(avg_pairwise_corr_1h=0.4, density=0.2, fused=False, btc_regime_4h="bull",
                              btc_shock=False, name_alarm_symbols=[], verdict="normal"),
            sentiment=SentimentComposite(score_0_100=50.0, label="neutral", components={"fear_greed": 50.0}),
            funding_window=self.funding_window,
            feature_packs={s: fp_of(s, px[s], now_ms) for s in syms},
            flow_packs={s: FlowPack(symbol=s, funding_now=0.0001, funding_regime="neutral", cascade_risk="low",
                                    data_confidence="high", spread_bps=2.0) for s in syms},
            risk_packs={s: risk_pack_of(s) for s in syms},
            liquidity={s: LiquidityGate(symbol=s, ok=True, reasons=[], spread_bps=2.0, quote_volume_24h=2e9,
                                        depth_1pct_usdt=1e7) for s in syms},
            quality={s: QualityReport(symbol=s, ok=True, reasons=[]) for s in syms},
            news=[], news_available=True, marks=px, fetched={s: 820 for s in syms},
        )


# ---------------------------------------------------------------- mock 응답
def tech_long(p: dict, **kw) -> TechnicalOutput:
    d = dict(symbol=p["symbol"], direction="long", setup_type="trend_continuation", timing="now", mtf_agreement=True,
             score=4, confidence=0.8, stop_candidate_price=99.0, stop_pct=1.0, sl_reference="atr",
             target_candidate_price=102.25, tp_pct=2.25, invalidation="1h 종가가 kijun 아래", noise_check=True,
             exit_flag=False, exit_reason=None, evidence=["per_tf.1h.vote: trend_up"])
    d.update(kw)
    return TechnicalOutput(**d)


def flow_ok(p: dict) -> FlowOutput:
    views = [FlowSymbolView(symbol=s, posture="neutral", crowding_risk="low", squeeze_direction="none",
                            cascade_risk="low", nearest_long_liq_cluster_pct_below=8.0, stop_inside_cascade=False,
                            washout_setup=False, block_new_longs=False, block_reason=None, flow_bias="long",
                            confidence=0.6, evidence=["flow_packs.x.funding_regime: neutral"]) for s in p["symbols"]]
    return FlowOutput(per_symbol=views, market_wide_note="중립", data_confidence="high")


def bull_ok(p: dict) -> BullOutput:
    return BullOutput(symbol=p["symbol"], case_strength=4,
                      thesis_points=[ThesisPoint(claim="추세 지속", evidence_field="technical.setup_type")],
                      catalysts=["listing"], underreaction_evidence="feature_extract.cgo_60 낮음",
                      entry_trigger="지금", expected_horizon_bars=16, invalidation_level=99.0,
                      disconfirming_evidence=["a", "b"], main_risk_to_bull_case="BTC 급락",
                      what_would_disprove="1h 종가 99 아래", confidence=0.7)


def bear_ok(p: dict) -> BearOutput:
    paths = [DeathPath(family=f, description="d", observable_trigger="t", probability_band="low",
                       time_to_impact_bars=None) for f in ("liquidation_cascade", "news_shock", "regime_flip")]
    return BearOutput(symbol=p["symbol"], death_paths=paths, greed_flags=[], disconfirming_evidence=["x"],
                      strongest_point="BTC 의존도 높음", what_would_disprove_bear="거래량 증가", verdict="survivable",
                      monitoring_conditions=["btc_return_1h_pct < -1"], confidence=0.6)


def risk_with(verdict: str = "approve", *, pos_action: str = "hold", pos_health: int = 7):
    def _f(p: dict) -> RiskOutput:
        cands = [RiskCandidateVerdict(symbol=c["symbol"], verdict=verdict, veto_reasons=[], conditions=["BTC 감시"],
                                      max_size_pct=8.0, stop_pct=1.0, take_profit_pct=2.25, rr_net_estimate=1.8,
                                      bull_point_reliability=[], required_monitoring=["btc"], correlation_note="낮음")
                 for c in p["candidates"]]
        poss = [RiskPositionView(symbol=x["symbol"], assumption_status=["holding", "weakening"], red_lines_triggered=[],
                                 health=pos_health, action=pos_action, reason="테스트") for x in p["positions"]]
        return RiskOutput(candidates=cands, positions=poss,
                          **{"global": RiskGlobal(entries_paused=False, pause_reason=None, confidence_floor=0.65,
                                                  exposure_cap_pct=30.0, portfolio_note="정상")},
                          risk_note="리스크 정상", confidence=0.7)
    return _f


def _debias() -> Debias:
    return Debias(anchored_to_entry=False, ignored_bearish_news=False, overtrading=False, chasing_spike=False,
                  acting_on_sentiment_alone=False)


def head_with(conf: float = 0.8):
    def _f(p: dict) -> HeadOutput:
        decisions = []
        for a in p["approved"]:
            decisions.append(HeadDecision(
                symbol=a["symbol"], action="BUY", confidence=conf, size_pct=5.0, stop_pct=1.0, tp_pct=2.25,
                thesis=Thesis(reasons=["추세 지속"], assumptions=["BTC 안정", "거래량 유지"],
                              red_lines=[RedLine(metric="btc_return_1h_pct", op="<", value=-2.0)], horizon_bars=16),
                disagreement_class="horizon", losing_side_strongest_point="BTC 의존", decisive_factor="mtf",
                rationale="다중 시간대 일치 추세"))
        for f in p["flagged_positions"]:
            decisions.append(HeadDecision(
                symbol=f["symbol"], action="SELL", confidence=0.8, size_pct=0.0, stop_pct=1.0, tp_pct=2.0,
                thesis=Thesis(reasons=["청산"], assumptions=[], red_lines=[], horizon_bars=0),
                disagreement_class="none", losing_side_strongest_point="-", decisive_factor="risk_exit",
                rationale="논지 훼손"))
        return HeadOutput(decisions=decisions, debias=_debias(), reevaluation_triggers=[], hold_reasons=[],
                          cycle_note="테스트")
    return _f


def happy(**over) -> dict:
    r = {"technical": tech_long, "derivatives_flow": flow_ok, "bull": bull_ok, "bear": bear_ok,
         "risk_manager": risk_with("approve"), "head": head_with()}
    r.update(over)
    return r


# ---------------------------------------------------------------- 조립
class Rig:
    def __init__(self, tmp_path: Path, responses=None, *, candidates=("ETH/USDT",), transport=None, clock=None,
                 market_prices=None, scanner=None, env_extra=None, budget=10.0, funding_window=False,
                 default_factory=None, deadline_s=C.CYCLE_DEADLINE_S, lock=None):
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        env = {"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(self.data),
               "SYMBOLS": "ETH/USDT,SOL/USDT", "ANTHROPIC_API_KEY": SECRET, "LLM_DAILY_BUDGET_USD": str(budget)}
        env.update(env_extra or {})
        self.settings = load_settings(env)
        self.clock = clock or Clock()
        self.market = FakeMarket(self.clock, market_prices)
        self.broker = PaperBroker(self.market, self.settings.paper_start_balance, clock_ms=self.clock.ms)
        self.portfolio = Portfolio.load(self.settings, self.data / "state.json")
        self.portfolio.sleep = lambda s: None
        self.ledger = CostLedger(self.data / "cost_ledger.jsonl", self.settings.llm_daily_budget_usd,
                                 self.settings.llm_monthly_budget_usd, now=self.clock)
        self.backend = MockBackend(responses if responses is not None else happy(), default_factory=default_factory)
        self.transport = transport if transport is not None else FakeTransport()
        self.notifier = Notifier(self.transport, failed_log=self.data / "logs" / "failed.jsonl", async_send=False,
                                 retries=1, retry_delays=[0.0], sleep=lambda s: None)
        prompts = tmp_path / "prompts"
        prompts.mkdir(exist_ok=True)
        for role_id in ROLES:
            (prompts / f"{role_id}.md").write_text(f"# Role: {role_id}\n", encoding="utf-8")
        (tmp_path / "skills").mkdir(exist_ok=True)
        self.skills = SkillRegistry(tmp_path / "skills")
        self.scanner = scanner if scanner is not None else FakeScanner(self.market, candidates,
                                                                       funding_window=funding_window)
        self.cycle = C.Cycle(self.settings, self.market, self.broker, self.portfolio, self.backend, self.ledger,
                             self.notifier, self.skills, now=self.clock, scanner=self.scanner, prompts_dir=prompts,
                             monotonic=self.clock.monotonic, deadline_s=deadline_s, lock=lock)

    @property
    def state(self):
        return self.portfolio.state

    def roles(self) -> list[str]:
        return [c["role"] for c in self.backend.calls]

    def decisions(self) -> list[dict]:
        p = self.data / "logs" / "decisions.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def journal_lines(self) -> list[dict]:
        p = self.data / "logs" / "journal.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def sent_with(self, needle: str) -> list[str]:
        return [m for m in self.transport.sent if needle in m]

    def hold_position(self, sym="ETH/USDT", entry=100.0, base=5.0, opened_cycle=None, sl=None, tp=None):
        st = self.state
        pos = Position(symbol=sym, base_amount=base, entry_price=entry, quote_spent=entry * base, fee_quote=0.0,
                       stop_loss_price=sl if sl is not None else entry * 0.985,
                       take_profit_price=tp if tp is not None else entry * 1.03,
                       opened_cycle=opened_cycle if opened_cycle is not None else st.cycle,
                       opened_ts=self.clock.ms() - 4 * H, thesis="{}", confidence=0.8, last_mark=entry)
        st.positions[sym] = pos
        st.paper_quote_balance -= entry * base
        return pos


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ======================================================================= 진입 → 청산 (SPEC §24)
def test_full_paper_cycle_entry(rig):
    summary = rig.cycle.run_once()
    assert isinstance(summary, C.CycleSummary)
    assert summary.cycle == 1 and summary.g0_passed is True and summary.pipeline_ran and summary.head_called
    assert summary.errors == []
    # 포지션 개설
    pos = rig.state.positions["ETH/USDT"]
    assert pos.opened_cycle == 1
    assert pos.stop_loss_price < pos.entry_price < pos.take_profit_price
    assert (pos.entry_price - pos.stop_loss_price) / pos.entry_price <= 0.02 + 1e-9
    assert (pos.take_profit_price - pos.entry_price) / pos.entry_price <= 0.04 + 1e-9
    assert pos.quote_spent <= 10_000 * 0.10 + 1e-6
    thesis = json.loads(pos.thesis)
    assert thesis["assumptions"] == ["BTC 안정", "거래량 유지"] and thesis["red_lines"][0]["metric"] == "btc_return_1h_pct"
    assert pos.confidence == pytest.approx(0.8)
    assert summary.entries and summary.entries[0]["symbol"] == "ETH/USDT"
    # state.json 기록
    saved = json.loads((rig.data / "state.json").read_text(encoding="utf-8"))
    assert "ETH/USDT" in saved["positions"] and saved["cycle"] == 1
    assert saved["paper_quote_balance"] < 10_000
    # 텔레그램 진입 메시지(심볼·손절·익절)
    entries = rig.sent_with("진입")
    assert len(entries) == 1
    msg = entries[0]
    assert "ETH/USDT" in msg and "손절" in msg and "익절" in msg
    assert "BTC 의존도 높음" in msg     # bear 최강 논점이 근거에 포함
    # 결정 로그
    recs = rig.decisions()
    cand = [r for r in recs if r.get("kind") == "candidate" and r.get("symbol") == "ETH/USDT"]
    assert cand and cand[0]["action"] == "BUY" and cand[0]["executed"] is True
    assert set(cand[0]["roles"]) >= {"technical", "bull", "bear", "risk_manager", "head"}
    cyc = [r for r in recs if r.get("kind") == "cycle"]
    assert cyc and cyc[0]["llm_calls"] == summary.llm_calls > 0 and cyc[0]["cycle"] == 1
    # 사이클 보고서
    assert list((rig.data / "logs" / "cycles").glob("*/cycle_1.json"))
    assert list((rig.data / "logs" / "cycles").glob("*/cycle_1.md"))


def test_entry_records_journal_thesis_and_entry_verdicts(rig):
    rig.cycle.run_once()
    js = rig.cycle.journal_state
    th = js.thesis["ETH/USDT"]
    assert th.assumptions == ["BTC 안정", "거래량 유지"]
    assert th.red_lines[0].metric == "btc_return_1h_pct"
    v = js.entry_verdicts["ETH/USDT"]
    assert set(v) == {"technical", "bull", "bear", "risk", "head", "channel_agreement"}
    assert v["bear"]["strongest_point"] == "BTC 의존도 높음"
    assert v["risk"]["risk_note"] == "리스크 정상"
    assert v["head"]["disagreement_class"] == "horizon" and v["head"]["debias_chasing_spike"] is False
    assert v["channel_agreement"] == {"technical": True, "flow": True, "news": False}
    assert js.thesis_history["ETH/USDT"] == []
    # 디스크에도 저장
    disk = load_journal_state(rig.data)
    assert "ETH/USDT" in disk.entry_verdicts and "ETH/USDT" in disk.thesis
    assert disk.gate_stats_today.get("candidates_seen", 0) >= 1


def test_entry_stops_rebased_on_fill_price(rig):
    rig.cycle.run_once()
    pos = rig.state.positions["ETH/USDT"]
    # 체결가 = ask*(1+slip) > mark(100) — 손절 거리는 계획(1%)을 유지(넓어지지 않음)
    assert pos.entry_price > 100.0
    assert (pos.entry_price - pos.stop_loss_price) / pos.entry_price == pytest.approx(0.01, rel=1e-6)


def test_second_cycle_stop_loss_exit(rig):
    rig.cycle.run_once()
    pos = rig.state.positions["ETH/USDT"]
    rig.scanner.candidates = []
    rig.market.prices["ETH/USDT"] = pos.stop_loss_price * 0.99
    rig.clock.advance(900)
    calls_before = len(rig.backend.calls)
    summary = rig.cycle.run_once()
    assert "ETH/USDT" not in rig.state.positions
    assert len(rig.state.closed_trades) == 1
    t = rig.state.closed_trades[0]
    assert t.reason == "stop_loss" and t.pnl_quote < 0 and t.closed_cycle == 2
    assert summary.exits and summary.exits[0]["reason"] == "stop_loss"
    assert rig.state.daily.losses == 1 and rig.state.loss_cooldown_until_cycle["ETH/USDT"] == 2 + 8
    exits = rig.sent_with("청산")
    assert exits and "ETH/USDT" in exits[-1] and "손절" in exits[-1]
    saved = json.loads((rig.data / "state.json").read_text(encoding="utf-8"))
    assert saved["positions"] == {} and len(saved["closed_trades"]) == 1
    # 저널: 사후분석(LLM 은 mock 기본 = conservative) 기록, 심볼 항목 정리
    lines = rig.journal_lines()
    assert lines and lines[-1]["symbol"] == "ETH/USDT" and lines[-1]["reason"] == "stop_loss"
    js = rig.cycle.journal_state
    assert "ETH/USDT" not in js.thesis and "ETH/USDT" not in js.entry_verdicts
    assert js.closed_count == 1
    # 후보·보유 없음 → 사후분석(journal) 외 LLM 호출 없음
    assert set(r for r in rig.roles()[calls_before:]) <= {"journal"}


def test_take_profit_exit_gain_message(rig):
    rig.cycle.run_once()
    pos = rig.state.positions["ETH/USDT"]
    rig.scanner.candidates = []
    rig.market.prices["ETH/USDT"] = pos.take_profit_price * 1.01
    rig.clock.advance(900)
    rig.cycle.run_once()
    t = rig.state.closed_trades[-1]
    assert t.reason == "take_profit" and t.pnl_quote > 0
    msg = rig.sent_with("청산")[-1]
    assert "🔵" in msg and "익절" in msg


def test_time_stop_exit(rig):
    rig.hold_position(opened_cycle=-200, sl=50.0, tp=200.0)
    rig.scanner.candidates = []
    s = rig.cycle.run_once()
    assert s.exits and s.exits[0]["reason"] == "time_stop"
    assert rig.state.positions == {}


def test_code_exit_journal_review_uses_llm(rig, tmp_path):
    rig.hold_position(sl=99.0, tp=110.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.scanner.candidates = []
    rig.cycle.run_once()
    js_calls = [c for c in rig.backend.calls if c["role"] == "journal"]
    assert len(js_calls) == 1 and js_calls[0]["payload"]["mode"] == "post_trade"
    assert js_calls[0]["payload"]["trade"]["reason"] == "stop_loss"


# ======================================================================= G0 / 예산
def test_kill_switch_stop_file_zero_llm_calls(rig):
    (rig.data / "control").mkdir(parents=True, exist_ok=True)
    (rig.data / "control" / "STOP").write_text(json.dumps({"reason": "테스트 중지", "ts": rig.clock.ms()}), encoding="utf-8")
    s = rig.cycle.run_once()
    assert rig.backend.calls == []
    assert s.g0_passed is False and s.llm_calls == 0 and not s.pipeline_ran
    assert rig.state.kill_switch is True and "테스트 중지" in rig.state.kill_reason
    assert not (rig.data / "control" / "STOP").exists()
    assert rig.sent_with("킬스위치")
    assert rig.state.positions == {}
    recs = rig.decisions()
    halt = [r for r in recs if r.get("kind") == "cycle"]
    assert halt and halt[0]["action"] == "HALT" and "kill_switch" in halt[0]["reason"]
    assert "kill_switch" in rig.cycle.journal_state.halts_today


def test_kill_switch_still_runs_code_exits(rig):
    rig.hold_position(sl=99.0)
    rig.portfolio.set_kill("manual")
    rig.market.prices["ETH/USDT"] = 90.0
    s = rig.cycle.run_once()
    assert s.exits and s.exits[0]["reason"] == "stop_loss"
    assert [r for r in rig.roles() if r != "journal"] == []


def test_clear_file_releases_kill(rig):
    rig.portfolio.set_kill("manual_stop: x")
    (rig.data / "control").mkdir(parents=True, exist_ok=True)
    (rig.data / "control" / "CLEAR").write_text(json.dumps({"reason": "재개", "ts": rig.clock.ms()}), encoding="utf-8")
    s = rig.cycle.run_once()
    assert rig.state.kill_switch is False
    assert not (rig.data / "control" / "CLEAR").exists()
    assert s.g0_passed is True and "ETH/USDT" in rig.state.positions
    assert rig.sent_with("킬스위치 해제")


def test_stop_then_clear_latest_wins(rig):
    ctl = rig.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "STOP").write_text(json.dumps({"reason": "a", "ts": 1000}), encoding="utf-8")
    (ctl / "CLEAR").write_text(json.dumps({"reason": "b", "ts": 2000}), encoding="utf-8")
    rig.cycle.run_once()
    assert rig.state.kill_switch is False


def test_drawdown_stop_all_sets_kill(rig):
    rig.state.peak_equity = 11_000.0       # 현재 10,000 → 낙폭 9%
    s = rig.cycle.run_once()
    assert rig.state.kill_switch and rig.state.kill_reason == "drawdown_stop_all"
    assert s.drawdown_level == "stop_all" and s.g0_passed is False
    assert rig.backend.calls == []
    assert rig.sent_with("킬스위치")


def test_clear_after_drawdown_resets_peak(rig):
    rig.state.peak_equity = 11_000.0
    rig.portfolio.set_kill("drawdown_stop_all")
    ctl = rig.data / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "CLEAR").write_text("{}", encoding="utf-8")
    s = rig.cycle.run_once()
    assert rig.state.kill_switch is False
    assert rig.state.peak_equity == pytest.approx(10_000.0, rel=1e-3)
    assert s.drawdown_level == "normal"


def test_budget_exceeded_in_pipeline_pauses_llm(rig):
    def boom(p):
        raise BudgetExceeded("일일 LLM 예산 초과")
    rig.backend.responses["technical"] = boom
    s = rig.cycle.run_once()
    assert s.budget_exceeded is True and s.llm_paused is True
    st = rig.state
    assert st.llm_pause_reason == "budget_exceeded"
    midnight = datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert st.llm_paused_until_ts == int(midnight.timestamp() * 1000)
    assert rig.sent_with("LLM 예산 초과")
    assert st.positions == {}
    assert "head" not in rig.roles()
    # 다음 사이클: G0 가 LLM 일시정지로 막는다 → 호출 0
    n = len(rig.backend.calls)
    rig.clock.advance(900)
    s2 = rig.cycle.run_once()
    assert s2.g0_passed is False and len(rig.backend.calls) == n
    assert any("llm_paused" in r for r in s2.g0_reasons)


def test_budget_already_spent_blocks_at_g0(tmp_path):
    rig = Rig(tmp_path, budget=1.0)
    rig.ledger.record(role="head", model="claude-opus-5-5", input_tokens=1_000_000, cached_tokens=0, output_tokens=0)
    s = rig.cycle.run_once()
    assert rig.backend.calls == []
    assert s.g0_passed is False and rig.state.llm_pause_reason == "budget_exceeded"
    assert rig.sent_with("LLM 예산 초과")


def test_auth_errors_pause_llm_60_minutes(tmp_path):
    fail = lambda role, model, payload: AgentResult(role=role, ok=False, error="auth")  # noqa: E731
    rig = Rig(tmp_path, responses={}, default_factory=fail)
    s = rig.cycle.run_once()
    assert rig.state.llm_pause_reason == "auth_error"
    assert rig.state.llm_paused_until_ts == rig.clock.ms() + 60 * 60_000
    assert rig.sent_with("LLM 일시정지")
    assert s.entries == []


def test_funding_window_downgrades_buy(tmp_path):
    rig = Rig(tmp_path, funding_window=True)
    s = rig.cycle.run_once()
    assert rig.state.positions == {}
    assert s.downgrades and "gate_downgrade" in rig.sent_with("주문 거부")[0]
    assert rig.cycle.journal_state.gate_stats_today.get("g4_downgrade") == 1


def test_g4_downgrade_low_confidence(tmp_path):
    rig = Rig(tmp_path, happy(head=head_with(conf=0.6)))
    s = rig.cycle.run_once()
    assert rig.state.positions == {}
    assert s.downgrades and s.downgrades[0]["symbol"] == "ETH/USDT"
    msgs = rig.sent_with("주문 거부")
    assert msgs and "gate_downgrade" in msgs[0] and "below_confidence_floor" in msgs[0]
    rec = [r for r in rig.decisions() if r.get("kind") == "candidate"][0]
    assert rec["action"] == "HOLD" and "gate_downgrade" in rec["reason"]


def test_bear_decline_no_entry(tmp_path):
    def bear_decline(p):
        return bear_ok(p).model_copy(update={"verdict": "decline"})
    rig = Rig(tmp_path, happy(bear=bear_decline))
    s = rig.cycle.run_once()
    assert rig.state.positions == {} and "head" not in rig.roles()
    assert s.head_skipped_reason == "no_actionable"


def test_order_rejected_on_broker_error(rig, monkeypatch):
    def nope(symbol, quote_amount):
        raise InsufficientFunds("필요 > 가용")
    monkeypatch.setattr(rig.broker, "market_buy", nope)
    s = rig.cycle.run_once()
    assert rig.state.positions == {}
    msgs = rig.sent_with("주문 거부")
    assert msgs and "InsufficientFunds" in msgs[0]
    assert any("buy:ETH/USDT" in e for e in s.errors)


# ======================================================================= 보유 포지션 / head SELL
def test_head_sell_exit_flagged_position(tmp_path):
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="exit", pos_health=3)), candidates=())
    rig.hold_position(sl=90.0, tp=120.0)
    s = rig.cycle.run_once()
    assert "ETH/USDT" not in rig.state.positions
    t = rig.state.closed_trades[-1]
    assert t.reason == "head_sell"
    assert s.exits[0]["reason"] == "head_sell"
    assert rig.sent_with("청산")


def test_head_sell_trim_sells_half(tmp_path):
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="trim", pos_health=5)), candidates=())
    rig.hold_position(base=4.0, sl=90.0, tp=120.0)
    s = rig.cycle.run_once()
    pos = rig.state.positions["ETH/USDT"]
    assert pos.base_amount == pytest.approx(2.0, rel=1e-6)
    assert rig.state.closed_trades[-1].reason == "partial"
    assert s.exits[0]["partial"] is True
    assert rig.sent_with("부분 청산")
    # 부분 청산이면 저널 심볼 항목은 유지(사후분석 LLM 호출 없음)
    assert "journal" not in rig.roles()


def test_hold_risk_view_no_sell(tmp_path):
    rig = Rig(tmp_path, happy(), candidates=())
    rig.hold_position(sl=90.0, tp=120.0)
    rig.cycle.run_once()
    assert "ETH/USDT" in rig.state.positions
    assert "head" not in rig.roles()          # 플래그 없음 → head 호출 없음


def test_thesis_history_and_prev_assumption_status_recorded(tmp_path):
    rig = Rig(tmp_path, happy(risk_manager=risk_with("approve", pos_action="hold", pos_health=6)), candidates=())
    rig.hold_position(sl=90.0, tp=120.0)
    rig.cycle.run_once()
    js = rig.cycle.journal_state
    hist = js.thesis_history["ETH/USDT"]
    assert len(hist) == 1
    h = hist[0]
    assert h["cycle"] == 1 and h["health"] == 6 and h["action"] == "hold"
    assert h["assumption_status"] == ["holding", "weakening"] and h["exit_flag"] is False
    assert h["unrealized_pct"] == pytest.approx(0.0, abs=1e-9)
    assert js.prev_assumption_status["ETH/USDT"] == ["holding", "weakening"]
    assert js.health_history["ETH/USDT"] == [6]
    # 다음 사이클 risk payload 에 prev_assumption_status 가 실린다
    rig.clock.advance(900)
    rig.cycle.run_once()
    rp = [c["payload"] for c in rig.backend.calls if c["role"] == "risk_manager"][-1]
    assert rp["positions"][0]["prev_assumption_status"] == ["holding", "weakening"]
    assert rp["positions"][0]["health_history"] == [6]


# ======================================================================= 기한·오류 정책
def test_cycle_deadline_skips_llm(tmp_path):
    clock = Clock()
    rig = Rig(tmp_path, clock=clock)
    rig.scanner.hook = lambda: clock.advance(301)
    s = rig.cycle.run_once()
    assert rig.backend.calls == []
    assert s.deadline_hit and not s.pipeline_ran
    assert any("cycle_deadline" in x for x in s.skipped_stages)
    assert rig.sent_with("cycle_deadline")
    assert rig.state.positions == {}


def test_cycle_deadline_after_pipeline_skips_buy_but_keeps_sell(tmp_path):
    clock = Clock()

    def slow_head(p):
        clock.advance(400)
        return head_with()(p)
    rig = Rig(tmp_path, happy(head=slow_head, risk_manager=risk_with("approve", pos_action="exit", pos_health=2)),
              clock=clock, candidates=("SOL/USDT",))
    rig.hold_position("ETH/USDT", sl=90.0, tp=120.0)
    s = rig.cycle.run_once()
    assert s.deadline_hit
    assert "SOL/USDT" not in rig.state.positions          # BUY 는 HOLD
    assert "ETH/USDT" not in rig.state.positions          # SELL 은 수행
    assert rig.state.closed_trades[-1].reason == "head_sell"
    assert any(d["symbol"] == "SOL/USDT" and d["reason"] == "cycle_deadline" for d in s.decisions)


def test_scanner_failure_isolated_and_fallback_marks(tmp_path):
    rig = Rig(tmp_path)
    rig.scanner.fail = RuntimeError(f"boom {SECRET}")
    rig.hold_position(sl=99.0)
    rig.market.prices["ETH/USDT"] = 95.0
    s = rig.cycle.run_once()
    assert any(e.startswith("scan:") for e in s.errors)
    # 보조 시세로 코드 청산 수행
    assert rig.state.closed_trades and rig.state.closed_trades[-1].reason == "stop_loss"
    assert "ETH/USDT" in rig.market.ticker_calls
    assert s.pipeline_ran is False
    # 오류 알림에 비밀값 없음
    assert rig.sent_with("오류")
    assert all(SECRET not in m for m in rig.transport.sent)
    assert all(SECRET not in e for e in s.errors)
    assert (rig.data / "state.json").exists()


def test_transport_failures_do_not_break_cycle(tmp_path):
    rig = Rig(tmp_path, transport=FakeTransport(fail=True))
    s = rig.cycle.run_once()
    assert "ETH/USDT" in rig.state.positions and s.errors == []
    failed = (rig.data / "logs" / "failed.jsonl").read_text(encoding="utf-8")
    assert "entry" in failed


def test_state_save_failure_propagates(rig, monkeypatch):
    def bad_save():
        raise PermissionError(13, "denied")
    monkeypatch.setattr(rig.portfolio, "save", bad_save)
    with pytest.raises(OSError) as ei:
        rig.cycle.run_once()
    assert isinstance(ei.value, C.StateSaveError)


def test_corrupt_state_error_propagates(tmp_path):
    rig = Rig(tmp_path)
    rig.scanner.fail = CorruptStateError("broken")
    with pytest.raises(CorruptStateError):
        rig.cycle.run_once()


def test_config_error_propagates(tmp_path):
    rig = Rig(tmp_path)
    rig.scanner.fail = ConfigError("bad")
    with pytest.raises(ConfigError):
        rig.cycle.run_once()


def test_lock_lost_propagates(tmp_path):
    class LostLock:
        def refresh(self):
            raise C.LockLostError("taken")
    rig = Rig(tmp_path, lock=LostLock())
    with pytest.raises(C.LockLostError):
        rig.cycle.run_once()


def test_lock_refreshed_each_cycle(tmp_path):
    class CountLock:
        n = 0

        def refresh(self):
            self.n += 1
    lk = CountLock()
    rig = Rig(tmp_path, lock=lk)
    rig.cycle.run_once()
    rig.clock.advance(900)
    rig.cycle.run_once()
    assert lk.n == 2


def test_backend_oserror_is_abstain_not_fatal(tmp_path):
    """비용 원장 밖에서 난 OSError 는 runner 가 기권으로 바꾼다 → 사이클은 계속(HOLD)."""
    def disk_full(p):
        raise OSError(28, "No space left")
    rig = Rig(tmp_path, happy(technical=disk_full))
    s = rig.cycle.run_once()
    assert rig.state.positions == {}
    assert any(a.startswith("technical:ETH/USDT:exception") for a in s.abstained), s.abstained


def test_pipeline_oserror_propagates(tmp_path, monkeypatch):
    """파이프라인에서 전파된 OSError(비용 원장 기록 실패 = 상태 저장 실패)는 사이클을 멈춘다."""
    rig = Rig(tmp_path)

    def broken(*a, **kw):
        raise OSError(28, "ledger write failed")
    monkeypatch.setattr(C, "run_pipeline", broken)
    with pytest.raises(OSError):
        rig.cycle.run_once()


# ======================================================================= 롤오버·일일 리뷰
def test_daily_rollover_sends_summary(rig):
    rig.cycle.run_once()
    rig.scanner.candidates = []
    rig.clock.dt = datetime(2026, 10, 29, 0, 1, 0, tzinfo=timezone.utc)
    s = rig.cycle.run_once()
    assert s.daily_rollover == "2026-10-28"
    assert rig.state.daily.date == "2026-10-29"
    msgs = rig.sent_with("일일 요약")
    assert any("2026-10-28" in m for m in msgs)
    # 00:05 이전 → 리뷰 없음
    assert s.daily_review is None


def test_daily_review_after_0005(rig):
    rig.hold_position(sl=99.0, tp=120.0)
    rig.market.prices["ETH/USDT"] = 95.0
    rig.scanner.candidates = []
    rig.cycle.run_once()                                   # 손절 청산(10-28)
    js = rig.cycle.journal_state
    js.last_daily_review_date = "2026-10-26"
    rig.clock.dt = datetime(2026, 10, 29, 0, 6, 0, tzinfo=timezone.utc)
    s = rig.cycle.run_once()
    assert s.daily_review == "2026-10-28"
    daily_calls = [c for c in rig.backend.calls if c["role"] == "journal" and c["payload"]["mode"] == "daily"]
    assert len(daily_calls) == 1
    assert daily_calls[0]["payload"]["metrics_table"]["trade_stats"]["closed_today"] == 1
    assert rig.cycle.journal_state.last_daily_review_date == "2026-10-28"
    assert rig.cycle.journal_state.gate_stats_today == {}
    assert any(r.get("kind") == "daily_review" for r in rig.journal_lines())
    # 같은 날 다시 → 리뷰 없음
    rig.clock.advance(900)
    s3 = rig.cycle.run_once()
    assert s3.daily_review is None


def test_first_run_after_0005_marks_review_without_llm(rig):
    rig.scanner.candidates = []
    rig.cycle.run_once()
    assert rig.cycle.journal_state.last_daily_review_date == "2026-10-27"
    assert "journal" not in rig.roles()


# ======================================================================= 실제 Scanner 통합
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class SyntheticMarket:
    """test_scanner 와 같은 방식의 결정적 랜덤워크 캔들(오프라인)."""

    def __init__(self, clock: Clock, symbols) -> None:
        self.clock = clock
        self.p0 = {s: 50.0 + 10 * i for i, s in enumerate(symbols)}
        self.p0["BTC/USDT"] = 60_000.0

    def _walk(self, symbol, tf, n):
        tf_ms = TF_MS[tf]
        now = self.clock.ms()
        end_ts = (now // tf_ms) * tf_ms - tf_ms
        rng = random.Random(zlib.crc32(f"{symbol}|{tf}".encode()))
        s = {"15m": 0.002, "1h": 0.004, "4h": 0.008, "1d": 0.02}[tf]
        out, prev = [], self.p0[symbol]
        for k in range(n):
            close = prev * math.exp(rng.gauss(0, s) + 0.0001)
            out.append(Candle(ts=end_ts - (n - 1 - k) * tf_ms, open=prev, high=max(prev, close) * (1 + 0.3 * s),
                              low=min(prev, close) * (1 - 0.3 * s), close=close, volume=1000.0 * (1 + 0.2 * rng.random())))
            prev = close
        return out

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return self._walk(symbol, timeframe, limit)

    def fetch_ticker(self, symbol):
        last = self._walk(symbol, "15m", 300)[-1].close
        return Ticker(symbol=symbol, last=last, bid=last * 0.9999, ask=last * 1.0001, quote_volume_24h=5e7,
                      change_24h_pct=0.01, ts=self.clock.ms())

    def fetch_orderbook_summary(self, symbol, depth=50):
        last = self._walk(symbol, "15m", 300)[-1].close
        return OrderBookSummary(symbol=symbol, best_bid=last * 0.9999, best_ask=last * 1.0001, spread_pct=0.0002,
                                bid_depth_usdt_1pct=1e6, ask_depth_usdt_1pct=1e6, ts=self.clock.ms())

    def fetch_futures_context(self, symbol):
        return FuturesContext(symbol=symbol, funding_rate=0.0001, open_interest_usdt=1e9, long_short_ratio=1.2,
                              ts=self.clock.ms())

    def server_time_ms(self):
        return self.clock.ms()


def test_real_scanner_integration_default_conservative(tmp_path):
    clock = Clock()
    syms = ("ETH/USDT", "SOL/USDT", "BNB/USDT")
    settings = load_settings({"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "d"),
                              "SYMBOLS": ",".join(syms)})
    market = SyntheticMarket(clock, syms)
    broker = PaperBroker(market, settings.paper_start_balance, clock_ms=clock.ms)
    pf = Portfolio.load(settings, tmp_path / "d" / "state.json")
    ledger = CostLedger(tmp_path / "d" / "cost.jsonl", 10, 200, now=clock)
    backend = MockBackend()                      # 전 역할 conservative_example(HOLD)
    transport = FakeTransport()
    notifier = Notifier(transport, async_send=False)
    (tmp_path / "skills").mkdir()
    scanner = Scanner(settings, market, thresholds=Thresholds(), cache_dir=tmp_path / "d" / "cache")
    cyc = C.Cycle(settings, market, broker, pf, backend, ledger, notifier, SkillRegistry(tmp_path / "skills"),
                  now=clock, scanner=scanner, monotonic=clock.monotonic)   # 실제 prompts/ 사용
    s = cyc.run_once()
    assert s.errors == [], s.errors
    assert s.g0_passed is True and s.pipeline_ran
    roles = [c["role"] for c in backend.calls]
    assert "technical" in roles
    assert pf.state.positions == {}
    assert (tmp_path / "d" / "state.json").exists()
    assert any(r for r in (tmp_path / "d" / "logs" / "decisions.jsonl").read_text(encoding="utf-8").splitlines())


def test_default_components_constructed(tmp_path):
    """scanner/runner/news_store/journal_state 를 넘기지 않아도 기본 객체를 만든다(네트워크 없음)."""
    rig = Rig(tmp_path)
    cyc = C.Cycle(rig.settings, rig.market, rig.broker, rig.portfolio, rig.backend, rig.ledger, rig.notifier,
                  rig.skills)
    assert isinstance(cyc.scanner, Scanner)
    assert cyc.news_store.path == rig.data / "news_store.json"
    assert isinstance(cyc.journal_state, JournalState)
    assert "cycle=" in repr(cyc)


def test_next_utc_midnight():
    assert C.next_utc_midnight_ms(datetime(2026, 10, 28, 23, 59, tzinfo=timezone.utc)) == int(
        datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp() * 1000)
    assert C.next_utc_midnight_ms(datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)) == int(
        datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp() * 1000)


def test_read_control_file_variants(tmp_path):
    p = tmp_path / "STOP"
    assert C.read_control_file(p) is None
    p.write_text("plain reason", encoding="utf-8")
    info = C.read_control_file(p)
    assert info["reason"] == "plain reason" and info["ts"] > 0
    p.write_text(json.dumps({"reason": "r", "ts": 5}), encoding="utf-8")
    assert C.read_control_file(p) == {"reason": "r", "ts": 5}
    p.write_text("", encoding="utf-8")
    assert C.read_control_file(p)["reason"] is None


# ======================================================================= G4 실행비용 재검사 전달 (MONEY-7)
def test_buy_passes_risk_pack_exec_cost_to_g4(tmp_path, monkeypatch):
    seen: list[tuple[str, object]] = []
    orig = C.G.g4_final

    def spy(decision, *a, **kw):
        seen.append((decision.symbol, kw.get("exec_cost_pct", "missing")))
        return orig(decision, *a, **kw)

    monkeypatch.setattr(C.G, "g4_final", spy)
    rig = Rig(tmp_path)
    rig.cycle.run_once()
    # 팩 값 0.21% 가 아니라 G3 가 잰 실효 비용(주문액/깊이 충격 포함, 0.211%)이 팩에 기록돼 G4 로 간다
    assert len(seen) == 1 and seen[0][0] == "ETH/USDT"
    assert seen[0][1] == pytest.approx(0.211) and seen[0][1] >= 0.21
    assert "ETH/USDT" in rig.state.positions          # 0.211% ≤ 0.25 × 최종 손절 → 진입 유지


# ======================================================================= G0 중단/재개 이벤트 종류
def test_g0_halt_and_lift_emit_dedicated_kinds(tmp_path):
    """_notify_halts 는 kind 'halt'(새 중단) / 'halt_lifted'(해제)로 알린다(구버전 error/startup 아님)."""
    events: list = []
    rig = Rig(tmp_path, candidates=())
    orig = rig.notifier.notify
    rig.notifier.notify = lambda e: (events.append(e), orig(e))
    st = rig.state
    st.daily.date = "2026-10-28"
    st.daily.start_equity = 10_000.0
    st.daily.consecutive_losses = 3
    st.daily.trades, st.daily.losses = 3, 3
    st.daily.realized_pnl = -10.0
    rig.cycle.run_once()
    halts = [e for e in events if e.kind == "halt"]
    assert len(halts) == 1 and halts[0].extra["halt_codes"] == ["consecutive_losses"]
    assert halts[0].equity is not None and not [e for e in events if e.kind == "error"]
    assert any(m.startswith("⛔ 신규 진입 중단 [paper]") and "연속 손실" in m for m in rig.transport.sent)
    # 다음 사이클도 같은 중단이면 다시 알리지 않는다
    rig.cycle.run_once()
    assert len([e for e in events if e.kind == "halt"]) == 1
    # 해제 → halt_lifted 한 번
    st.daily.consecutive_losses = 0
    rig.cycle.run_once()
    lifted = [e for e in events if e.kind == "halt_lifted"]
    assert len(lifted) == 1 and lifted[0].extra["halt_codes"] == ["consecutive_losses"]
    assert not [e for e in events if e.kind == "startup"]
    assert any(m.startswith("✅ 신규 진입 재개 [paper]") and "consecutive_losses" in m for m in rig.transport.sent)
