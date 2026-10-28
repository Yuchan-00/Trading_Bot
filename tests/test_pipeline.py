"""pipeline.py (SPEC §20.5, v2.1 §20.3a) 테스트 — MockBackend 시나리오(오프라인).

ScanResult 는 features/scanner 모델로 직접 만든다(네트워크 없음).
"""
from __future__ import annotations

import json
import threading

import pytest

from tradingbot.agents import payloads as P
from tradingbot.agents.pipeline import (
    HEALTH_FLAG_MAX,
    CandidateContext,
    NewsStore,
    PipelineResult,
    effective_gate,
    flag_position,
    run_pipeline,
)
from tradingbot.agents.roles import ROLES
from tradingbot.agents.runner import AgentRunner
from tradingbot.agents.schemas import (
    BearOutput,
    BullOutput,
    Debias,
    DeathPath,
    FlowOutput,
    FlowSymbolView,
    HeadDecision,
    HeadOutput,
    NewsEvent,
    NewsOutput,
    RedLine,
    RiskCandidateVerdict,
    RiskGlobal,
    RiskOutput,
    RiskPositionView,
    TechnicalOutput,
    Thesis,
    ThesisPoint,
)
from tradingbot.config import load_settings
from tradingbot.features import FeaturePack, FlowPack, RegimePack, RiskPack, SentimentComposite
from tradingbot.gates import GateDecision
from tradingbot.journal import JournalState
from tradingbot.llm.backend import AgentResult, MockBackend
from tradingbot.llm.cost import BudgetExceeded
from tradingbot.models import BotState, DailyStats, NewsItem, Position, QualityReport
from tradingbot.scanner import Candidate, LiquidityGate, ScanResult
from tradingbot.skills import SkillRegistry

NOW = 1_793_000_000_000          # 2026-10 근처 epoch ms
H = 3_600_000
EQUITY = 10_000.0


# ======================================================================= 픽스처 빌더
def fp_of(sym: str, price: float = 100.0, **kw) -> FeaturePack:
    base = dict(symbol=sym, ts=NOW, price=price, mtf_agreement=True, atr_pct_1h=0.5,
                noise_floor_stop_pct=0.5, noise_ok_2pct=True, hv_pct_rank=40.0,
                last_closes_1h=[price * (1 + i * 0.001) for i in range(20)], change_24h_pct=1.0)
    base.update(kw)
    return FeaturePack(**base)


def flow_of(sym: str, conf: str = "high") -> FlowPack:
    return FlowPack(symbol=sym, funding_now=0.0001, funding_regime="neutral", cascade_risk="low",
                    data_confidence=conf, spread_bps=2.0)


def risk_pack_of(sym: str) -> RiskPack:
    return RiskPack(symbol=sym, var95_1h_pct=0.8, cvar95_1h_pct=1.0, daily_vol_pct=2.0, corr_to_btc_1h=0.5,
                    corr_to_open_positions={}, inverse_vol_size_pct=10.0, cvar_size_pct=10.0, spread_bps=2.0,
                    depth_taker_usdt=10_000_000.0, impact_bps_est=0.1, order_notional_vs_15m_volume_share=0.0001,
                    stop_vs_atr_noise_ok=True, exec_cost_pct=0.21)


def liq_of(sym: str) -> LiquidityGate:
    return LiquidityGate(symbol=sym, ok=True, reasons=[], spread_bps=2.0, quote_volume_24h=2_000_000_000.0,
                         depth_1pct_usdt=10_000_000.0)


def cand_of(sym: str, rank: int = 1) -> Candidate:
    return Candidate(symbol=sym, rank=rank, composite_z=1.5 - rank * 0.1,
                     factor_table={"momentum_4h": 1.0, "rs_vs_btc": 0.5}, reasons=["momentum_4h 상위"])


def news_item(title: str, ts: int | None = NOW - H, sym: str = "ETH/USDT") -> NewsItem:
    return NewsItem(title=title, source="coindesk.com", url=f"https://example.com/{abs(hash(title))}",
                    published_ts=ts, symbols=[sym], tier=1)


def make_scan(cands=("ETH/USDT",), held=(), news=(), flow_conf="high", quality_bad=(), extra_fp=None,
              news_available=True) -> ScanResult:
    syms = list(dict.fromkeys([*cands, *held, "BTC/USDT"]))
    fps = {s: fp_of(s) for s in syms if s not in quality_bad}
    if extra_fp:
        fps.update(extra_fp)
    return ScanResult(
        ts=NOW, universe=syms, candidates=[cand_of(s, i + 1) for i, s in enumerate(cands)], skipped=[],
        regime=RegimePack(avg_pairwise_corr_1h=0.4, density=0.2, fused=False, btc_regime_4h="bull",
                          btc_shock=False, name_alarm_symbols=[], verdict="normal"),
        sentiment=SentimentComposite(score_0_100=50.0, label="neutral", components={"fear_greed": 50.0}),
        funding_window=False, feature_packs=fps,
        flow_packs={s: flow_of(s, flow_conf) for s in syms},
        risk_packs={s: risk_pack_of(s) for s in syms},
        liquidity={s: liq_of(s) for s in syms},
        quality={s: QualityReport(symbol=s, ok=s not in quality_bad, reasons=["stale"] if s in quality_bad else [])
                 for s in syms},
        news=list(news), news_available=news_available, marks={s: 100.0 for s in syms},
        fetched={s: 820 for s in syms},
    )


def make_state(held=(), cycle: int = 10) -> BotState:
    positions = {
        s: Position(symbol=s, base_amount=5.0, entry_price=100.0, quote_spent=500.0, stop_loss_price=98.5,
                    take_profit_price=103.0, opened_cycle=cycle - 4, opened_ts=NOW - 4 * H, thesis="test",
                    confidence=0.8, last_mark=100.0)
        for s in held
    }
    return BotState(mode="paper", cycle=cycle, positions=positions, paper_quote_balance=EQUITY - 500 * len(held),
                    peak_equity=EQUITY, daily=DailyStats(date="2026-10-28", start_equity=EQUITY))


# ---------------------------------------------------------------- mock 응답 팩토리
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


def news_ok(p: dict) -> NewsOutput:
    events = [NewsEvent(headline_id=h["id"], score=0.33, event_type="listing", symbols=["ETH/USDT"],
                        summary="상장 소식", stale=False) for h in p["headlines"]]
    return NewsOutput(events=events, risk_event=False, risk_event_symbols=[], market_tone=0.1, market_note="중립",
                      feed_health_note="정상")


def bull_ok(p: dict) -> BullOutput:
    return BullOutput(symbol=p["symbol"], case_strength=4,
                      thesis_points=[ThesisPoint(claim="추세 지속", evidence_field="technical.setup_type")],
                      catalysts=["listing"], underreaction_evidence="feature_extract.cgo_60 낮음",
                      entry_trigger="지금", expected_horizon_bars=16, invalidation_level=99.0,
                      disconfirming_evidence=["a", "b"], main_risk_to_bull_case="BTC 급락",
                      what_would_disprove="1h 종가 99 아래", confidence=0.7)


def bear_with(verdict: str):
    def _f(p: dict) -> BearOutput:
        paths = [DeathPath(family=f, description="d", observable_trigger="t", probability_band="low",
                           time_to_impact_bars=None) for f in ("liquidation_cascade", "news_shock", "regime_flip")]
        return BearOutput(symbol=p["symbol"], death_paths=paths, greed_flags=[], disconfirming_evidence=["x"],
                          strongest_point="BTC 의존", what_would_disprove_bear="거래량 증가", verdict=verdict,
                          monitoring_conditions=["btc_return_1h_pct < -1"], confidence=0.6)
    return _f


def risk_with(verdict: str = "approve", *, pos_action: str = "hold", pos_health: int = 7, paused: bool = False):
    def _f(p: dict) -> RiskOutput:
        cands = [RiskCandidateVerdict(symbol=c["symbol"], verdict=verdict,
                                      veto_reasons=["상관 과다"] if verdict == "veto" else [], conditions=[],
                                      max_size_pct=8.0, stop_pct=1.0, take_profit_pct=2.25, rr_net_estimate=1.8,
                                      bull_point_reliability=[], required_monitoring=[], correlation_note="낮음")
                 for c in p["candidates"]]
        poss = [RiskPositionView(symbol=x["symbol"], assumption_status=["holding"], red_lines_triggered=[],
                                 health=pos_health, action=pos_action, reason="테스트") for x in p["positions"]]
        return RiskOutput(candidates=cands, positions=poss,
                          **{"global": RiskGlobal(entries_paused=paused, pause_reason="테스트 중단" if paused else None,
                                                  confidence_floor=0.65, exposure_cap_pct=30.0,
                                                  portfolio_note="정상")},
                          risk_note="리스크 정상", confidence=0.7)
    return _f


def head_buy(p: dict) -> HeadOutput:
    decisions = []
    for a in p["approved"]:
        decisions.append(HeadDecision(
            symbol=a["symbol"], action="BUY", confidence=0.8, size_pct=5.0, stop_pct=1.0, tp_pct=2.25,
            thesis=Thesis(reasons=["추세"], assumptions=["BTC 안정"],
                          red_lines=[RedLine(metric="btc_return_1h_pct", op="<", value=-2.0)], horizon_bars=16),
            disagreement_class="horizon", losing_side_strongest_point="BTC 의존", decisive_factor="mtf",
            rationale="승인"))
    for f in p["flagged_positions"]:
        decisions.append(HeadDecision(
            symbol=f["symbol"], action="SELL", confidence=0.8, size_pct=0.0, stop_pct=1.0, tp_pct=2.0,
            thesis=Thesis(reasons=["청산"], assumptions=[], red_lines=[], horizon_bars=0),
            disagreement_class="none", losing_side_strongest_point="-", decisive_factor="exit_flag",
            rationale="청산"))
    return HeadOutput(decisions=decisions, debias=Debias(anchored_to_entry=False, ignored_bearish_news=False,
                                                         overtrading=False, chasing_spike=False,
                                                         acting_on_sentiment_alone=False),
                      reevaluation_triggers=[], hold_reasons=[], cycle_note="테스트")


def happy_responses(**over) -> dict:
    r = {"technical": tech_long, "derivatives_flow": flow_ok, "news_sentiment": news_ok, "bull": bull_ok,
         "bear": bear_with("survivable"), "risk_manager": risk_with("approve"), "head": head_buy}
    r.update(over)
    return r


# ---------------------------------------------------------------- 러너
class SpyRunner(AgentRunner):
    """run_many 배치를 기록한다."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.batches: list[list[str]] = []

    def run_many(self, calls):
        self.batches.append([c[0] for c in calls])
        return super().run_many(calls)


@pytest.fixture
def settings(tmp_path):
    return load_settings({"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "data")})


@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    for role_id in ROLES:
        (d / f"{role_id}.md").write_text(f"# Role: {role_id}\n", encoding="utf-8")
    return d


@pytest.fixture
def skills(tmp_path):
    (tmp_path / "skills").mkdir()
    return SkillRegistry(tmp_path / "skills")


@pytest.fixture
def make_runner(settings, prompts_dir, skills):
    def _mk(responses=None, default_factory=None):
        backend = MockBackend(responses or {}, default_factory=default_factory)
        return SpyRunner(settings, backend, skills, prompts_dir), backend
    return _mk


def roles_called(backend: MockBackend) -> list[str]:
    return [c["role"] for c in backend.calls]


def payloads_for(backend: MockBackend, role: str) -> list[dict]:
    return [c["payload"] for c in backend.calls if c["role"] == role]


def run(runner, scan=None, state=None, js=None, store=None, now=NOW) -> PipelineResult:
    return run_pipeline(scan or make_scan(), state or make_state(), EQUITY, runner, js or JournalState(), now,
                        news_store=store if store is not None else NewsStore())


# ======================================================================= 시나리오 (SPEC §24)
def test_all_abstain_no_head_and_hold(make_runner):
    fail = lambda role, model, payload: AgentResult(role=role, ok=False, error="timeout")  # noqa: E731
    runner, backend = make_runner(default_factory=fail)
    res = run(runner, make_scan(cands=("ETH/USDT", "SOL/USDT"), news=[news_item("헤드라인 A")]))
    roles = roles_called(backend)
    assert "head" not in roles and "bull" not in roles and "bear" not in roles and "risk_manager" not in roles
    assert res.head is None
    assert res.head_skipped_reason is not None
    assert "technical:ETH/USDT:timeout" in res.abstained
    assert "technical:SOL/USDT:timeout" in res.abstained
    assert "derivatives_flow:*:timeout" in res.abstained
    assert "news_sentiment:*:timeout" in res.abstained
    assert all(c.dropped_at == "g1" for c in res.candidates)
    assert all(c.technical is None for c in res.candidates)
    assert res.flow is None and res.news is None
    assert res.approved() == []
    assert res.decision_for("ETH/USDT") is None   # = HOLD
    assert res.budget_exceeded is False


def test_bear_decline_dropped_before_risk(make_runner):
    runner, backend = make_runner(happy_responses(bear=bear_with("decline")))
    res = run(runner)
    c = res.candidates[0]
    assert c.dropped_at == "bear_decline"
    assert c.bear is not None and c.bear.verdict == "decline"
    roles = roles_called(backend)
    assert "risk_manager" not in roles and "head" not in roles
    assert res.gate_counts["g2_bear_decline"] == 1
    assert res.head_skipped_reason == "no_actionable"


def test_bear_decline_one_of_two_risk_sees_only_survivor_and_head_sees_vetoed(make_runner):
    def bear(p):
        return bear_with("decline" if p["symbol"] == "SOL/USDT" else "survivable")(p)
    runner, backend = make_runner(happy_responses(bear=bear))
    res = run(runner, make_scan(cands=("ETH/USDT", "SOL/USDT")))
    risk_p = payloads_for(backend, "risk_manager")
    assert len(risk_p) == 1
    assert [c["symbol"] for c in risk_p[0]["candidates"]] == ["ETH/USDT"]
    head_p = payloads_for(backend, "head")
    assert len(head_p) == 1
    assert [a["symbol"] for a in head_p[0]["approved"]] == ["ETH/USDT"]
    assert {"symbol": "SOL/USDT", "stage": "bear_decline"}.items() <= head_p[0]["vetoed"][0].items()
    assert res.decision_for("ETH/USDT").action == "BUY"
    assert res.decision_for("SOL/USDT") is None


def test_risk_veto_head_not_called(make_runner):
    runner, backend = make_runner(happy_responses(risk_manager=risk_with("veto")))
    res = run(runner)
    c = res.candidates[0]
    assert c.dropped_at == "risk_veto"
    assert c.risk is not None and c.risk.verdict == "veto"
    assert "head" not in roles_called(backend)
    assert res.head is None
    assert res.head_skipped_reason == "no_actionable"
    assert res.gate_counts["g3_risk_veto"] == 1
    assert res.risk_global is not None


def test_approval_path_head_called_once_buy(make_runner):
    runner, backend = make_runner(happy_responses())
    res = run(runner, make_scan(news=[news_item("ETH 상장")]))
    roles = roles_called(backend)
    assert roles.count("head") == 1
    assert roles.count("technical") == 1 and roles.count("derivatives_flow") == 1
    assert roles.count("news_sentiment") == 1
    assert roles.count("bull") == 1 and roles.count("bear") == 1 and roles.count("risk_manager") == 1
    assert res.llm_calls == len(roles) == 7
    head_p = payloads_for(backend, "head")[0]
    assert set(head_p) == set(P.HEAD_KEYS)
    assert head_p["role"] == "head"
    assert head_p["hard_limits"] == P.HARD_LIMITS_PAYLOAD
    a = head_p["approved"][0]
    assert set(a) == set(P.HEAD_APPROVED_KEYS)
    assert a["symbol"] == "ETH/USDT"
    assert set(a["gate"]) == set(P.GATE_KEYS)
    assert a["gate"]["confidence_floor"] >= 0.65
    assert 0.0 <= a["gate"]["size_multiplier"] <= 1.0
    assert set(a["technical"]["labels"]) == set(P.HEAD_TECH_LABEL_KEYS)
    assert a["risk"]["entries_paused"] is False
    assert a["news"] is not None and a["news"]["events"]
    assert head_p["flagged_positions"] == []
    d = res.decision_for("ETH/USDT")
    assert d is not None and d.action == "BUY"
    ctx = res.approved()
    assert [c.symbol for c in ctx] == ["ETH/USDT"]
    assert ctx[0].dropped_at is None
    assert [g.passed for g in ctx[0].gates] == [True, True, True]
    assert res.head_skipped_reason is None
    assert res.gate_counts["head_calls"] == 1
    assert res.risk_note == "리스크 정상"


def test_batches_order_and_single_debate_run_many(make_runner):
    runner, backend = make_runner(happy_responses())
    run(runner, make_scan(cands=("ETH/USDT", "SOL/USDT"), held=("XRP/USDT",), news=[news_item("x")]),
        make_state(held=("XRP/USDT",)))
    b = runner.batches
    assert b[0] == ["technical", "technical", "technical", "derivatives_flow", "news_sentiment"]
    assert b[1] == ["bull", "bear", "bull", "bear"]
    assert b[2] == ["risk_manager"]
    assert b[3] == ["head"]
    assert len(b) == 4


def test_budget_exceeded_midway_no_further_calls(make_runner):
    def bear(p):
        raise BudgetExceeded("일일 예산 초과")
    runner, backend = make_runner(happy_responses(bear=bear))
    res = run(runner)
    assert res.budget_exceeded is True
    assert "budget" in res.abstained
    roles = roles_called(backend)
    assert "risk_manager" not in roles and "head" not in roles
    assert res.head is None
    assert res.head_skipped_reason == "budget_exceeded"
    assert all(c.dropped_at in ("budget", "g1") for c in res.candidates)
    assert res.candidates[0].dropped_at == "budget"
    assert res.approved() == []


def test_budget_exceeded_at_analysts(make_runner):
    def tech(p):
        raise BudgetExceeded("x")
    runner, backend = make_runner(happy_responses(technical=tech))
    res = run(runner, make_scan(cands=("ETH/USDT",), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)))
    assert res.budget_exceeded and res.head is None
    assert set(roles_called(backend)) <= {"technical", "derivatives_flow", "news_sentiment"}
    assert res.candidates[0].dropped_at == "budget"
    assert all(not p.flagged_for_head for p in res.positions)
    assert res.gate_log == []


def test_budget_exceeded_at_head(make_runner):
    def head(p):
        raise BudgetExceeded("x")
    runner, backend = make_runner(happy_responses(head=head))
    res = run(runner)
    assert res.budget_exceeded is True
    assert res.head is None
    assert res.candidates[0].dropped_at == "budget"


def test_news_call_skipped_without_new_headlines(make_runner):
    runner, backend = make_runner(happy_responses())
    run(runner, make_scan(news=[]))
    assert "news_sentiment" not in roles_called(backend)


def test_news_seen_headlines_not_reanalysed_and_store_persists(make_runner, tmp_path):
    path = tmp_path / "news_store.json"
    items = [news_item("ETH 상장"), news_item("ETH 업그레이드")]
    runner, backend = make_runner(happy_responses())
    store = NewsStore(path)
    res1 = run(runner, make_scan(news=items), store=store)
    assert roles_called(backend).count("news_sentiment") == 1
    assert res1.news is not None and len(res1.news.events) == 2
    assert path.exists()
    # 새 저장소 객체(디스크에서 다시 읽음) → 같은 헤드라인은 새것이 아님
    store2 = NewsStore(path)
    assert store2.new_headlines(items) == []
    runner2, backend2 = make_runner(happy_responses())
    run(runner2, make_scan(news=items), store=store2, now=NOW + 15 * 60_000)
    assert "news_sentiment" not in roles_called(backend2)
    # 이전 이벤트는 bull/bear 에 여전히 전달된다(24h 이내)
    bull_p = payloads_for(backend2, "bull")[0]
    assert len(bull_p["news_events"]) == 2
    assert bull_p["news_context"] is None       # 이번 사이클 뉴스 채널 호출 없음
    # 한 개라도 새 헤드라인이면 그것만 보낸다
    runner3, backend3 = make_runner(happy_responses())
    run(runner3, make_scan(news=[*items, news_item("ETH 새 소식")]), store=NewsStore(path), now=NOW + 30 * 60_000)
    news_p = payloads_for(backend3, "news_sentiment")
    assert len(news_p) == 1 and [h["title"] for h in news_p[0]["headlines"]] == ["ETH 새 소식"]
    assert len(news_p[0]["prev_events"]) == 2


def test_news_failure_does_not_mark_seen(make_runner):
    fail_news = lambda p: AgentResult(role="news_sentiment", ok=False, error="parse_error")  # noqa: E731
    runner, backend = make_runner(happy_responses(news_sentiment=fail_news))
    store = NewsStore()
    items = [news_item("A")]
    res = run(runner, make_scan(news=items), store=store)
    assert "news_sentiment:*:parse_error" in res.abstained
    assert store.new_headlines(items) == items


def test_news_risk_event_symbols_persist_and_block_g1(make_runner):
    def news_risk(p):
        return NewsOutput(events=[NewsEvent(headline_id=p["headlines"][0]["id"], score=-1.0, event_type="hack",
                                            symbols=["ETH/USDT"], summary="해킹", stale=False)],
                          risk_event=True, risk_event_symbols=["ETH/USDT"], market_tone=-0.5, market_note="악재",
                          feed_health_note="정상")
    store = NewsStore()
    runner, backend = make_runner(happy_responses(news_sentiment=news_risk))
    res = run(runner, make_scan(news=[news_item("ETH 해킹")]), store=store)
    assert res.news_risk_symbols == ["ETH/USDT"]
    assert res.candidates[0].dropped_at == "g1"
    # 다음 사이클: 새 헤드라인 없음에도 24h 동안 차단 유지
    runner2, backend2 = make_runner(happy_responses())
    res2 = run(runner2, make_scan(news=[]), store=store, now=NOW + 15 * 60_000)
    assert "ETH/USDT" in res2.news_risk_symbols
    assert res2.candidates[0].dropped_at == "g1"
    assert "bull" not in roles_called(backend2)
    # 24h 이후 만료
    res3 = run(make_runner(happy_responses())[0], make_scan(news=[]), store=store, now=NOW + 25 * H)
    assert res3.news_risk_symbols == []


def test_open_position_flagged_by_exit_flag_head_called_without_candidates(make_runner):
    def tech(p):
        if p["position"] is not None:
            return tech_long(p, direction="flat", exit_flag=True, exit_reason="1h 구조 붕괴", confidence=0.3)
        return tech_long(p)
    runner, backend = make_runner(happy_responses(technical=tech))
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)))
    roles = roles_called(backend)
    assert roles.count("head") == 1
    assert "bull" not in roles
    pc = res.positions[0]
    assert pc.symbol == "XRP/USDT" and pc.flagged_for_head
    assert "technical_exit_flag" in (pc.flag_reason or "")
    head_p = payloads_for(backend, "head")[0]
    assert head_p["approved"] == []
    fl = head_p["flagged_positions"][0]
    assert set(fl) == set(P.FLAGGED_KEYS)
    assert fl["technical"] == {"exit_flag": True, "exit_reason": "1h 구조 붕괴"}
    assert res.decision_for("XRP/USDT").action == "SELL"


def test_position_not_flagged_when_healthy_head_skipped(make_runner):
    runner, backend = make_runner(happy_responses())
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)))
    assert "risk_manager" in roles_called(backend)
    assert "head" not in roles_called(backend)
    assert res.positions[0].flagged_for_head is False
    assert res.positions[0].health_history == [7]
    assert res.head_skipped_reason == "no_actionable"


def test_position_flagged_health_two_consecutive(make_runner):
    runner, backend = make_runner(happy_responses(risk_manager=risk_with(pos_health=3)))
    js = JournalState(health_history={"XRP/USDT": [6, 4]})
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)), js)
    pc = res.positions[0]
    assert pc.health_history == [6, 4, 3]
    assert pc.flagged_for_head and "health" in pc.flag_reason
    assert roles_called(backend).count("head") == 1
    # 이전이 5 면 플래그 아님
    runner2, backend2 = make_runner(happy_responses(risk_manager=risk_with(pos_health=3)))
    res2 = run(runner2, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)),
               JournalState(health_history={"XRP/USDT": [5]}))
    assert not res2.positions[0].flagged_for_head


def test_position_flagged_by_risk_action_trim(make_runner):
    runner, backend = make_runner(happy_responses(risk_manager=risk_with(pos_action="trim")))
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)))
    assert res.positions[0].flagged_for_head
    assert "risk_action:trim" in res.positions[0].flag_reason


def test_position_flagged_by_red_line(make_runner):
    runner, backend = make_runner(happy_responses())
    thesis = Thesis(reasons=["r"], assumptions=["a"], red_lines=[RedLine(metric="price", op="<", value=101.0)],
                    horizon_bars=16)
    js = JournalState(thesis={"XRP/USDT": thesis})
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)), js)
    pc = res.positions[0]
    assert pc.red_line_hits and pc.flagged_for_head
    risk_p = payloads_for(backend, "risk_manager")[0]
    assert risk_p["positions"][0]["red_line_hits"] == pc.red_line_hits
    assert risk_p["positions"][0]["thesis"]["red_lines"][0]["metric"] == "price"
    assert roles_called(backend).count("head") == 1


def test_flow_skipped_when_no_flow_data(make_runner):
    runner, backend = make_runner(happy_responses())
    res = run(runner, make_scan(flow_conf="none"))
    assert "derivatives_flow" not in roles_called(backend)
    assert "derivatives_flow:*:no_data" in res.abstained
    assert res.flow is None
    assert res.candidates[0].flow is None


def test_bull_abstain_recorded_pipeline_continues(make_runner):
    fail = lambda p: AgentResult(role="bull", ok=False, error="refusal:other")  # noqa: E731
    runner, backend = make_runner(happy_responses(bull=fail))
    res = run(runner)
    assert "bull:ETH/USDT:refusal:other" in res.abstained
    assert res.candidates[0].bull is None
    assert "risk_manager" in roles_called(backend)
    assert payloads_for(backend, "risk_manager")[0]["candidates"][0]["bull"] is None


def test_bear_abstain_passes_with_raised_floor(make_runner):
    fail = lambda p: AgentResult(role="bear", ok=False, error="timeout")  # noqa: E731
    runner, backend = make_runner(happy_responses(bear=fail))
    res = run(runner)
    assert "bear:ETH/USDT:timeout" in res.abstained
    c = res.candidates[0]
    assert c.dropped_at is None
    g = effective_gate(c, res.risk_global)
    assert g["confidence_floor"] >= 0.65


def test_risk_abstain_drops_candidates_no_head(make_runner):
    fail = lambda p: AgentResult(role="risk_manager", ok=False, error="max_tokens")  # noqa: E731
    runner, backend = make_runner(happy_responses(risk_manager=fail))
    res = run(runner)
    assert "risk_manager:*:max_tokens" in res.abstained
    assert res.candidates[0].dropped_at == "g3"
    assert "head" not in roles_called(backend)
    assert res.risk_global is None


def test_risk_entries_paused_drops_candidates(make_runner):
    runner, backend = make_runner(happy_responses(risk_manager=risk_with(paused=True)))
    res = run(runner)
    assert res.candidates[0].dropped_at == "g3"
    assert any("일시중단" in r for g in res.candidates[0].gates for r in g.reasons)
    assert "head" not in roles_called(backend)


def test_head_failure_is_hold(make_runner):
    fail = lambda p: AgentResult(role="head", ok=False, error="timeout")  # noqa: E731
    runner, backend = make_runner(happy_responses(head=fail))
    res = run(runner)
    assert res.head is None
    assert "head:*:timeout" in res.abstained
    assert res.decision_for("ETH/USDT") is None


def test_head_sanitized_buy_for_unapproved_and_unknown(make_runner):
    def head(p):
        out = head_buy(p)
        extra = [
            HeadDecision(symbol="DOGE/USDT", action="BUY", confidence=0.9, size_pct=5, stop_pct=1, tp_pct=2,
                         thesis=Thesis(reasons=[], assumptions=[], red_lines=[], horizon_bars=1),
                         disagreement_class="none", losing_side_strongest_point="", decisive_factor="",
                         rationale=""),
        ]
        return out.model_copy(update={"decisions": out.decisions + extra})
    runner, backend = make_runner(happy_responses(head=head))
    res = run(runner)
    assert [d.symbol for d in res.head.decisions] == ["ETH/USDT"]


def test_head_buy_on_flagged_position_downgraded_to_hold(make_runner):
    def tech(p):
        return tech_long(p, exit_flag=p["position"] is not None, exit_reason="x" if p["position"] else None)

    def head(p):
        d = head_buy({"approved": [{"symbol": f["symbol"]} for f in p["flagged_positions"]], "flagged_positions": []})
        return d
    runner, backend = make_runner(happy_responses(technical=tech, head=head))
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",)), make_state(held=("XRP/USDT",)))
    d = res.decision_for("XRP/USDT")
    assert d is not None and d.action == "HOLD"


def test_technical_symbol_mismatch_abstains(make_runner):
    runner, backend = make_runner(happy_responses(technical=lambda p: tech_long({"symbol": "BTC/USDT"})))
    res = run(runner)
    assert "technical:ETH/USDT:symbol_mismatch" in res.abstained
    assert res.candidates[0].dropped_at == "g1"


def test_quality_excluded_held_symbol_not_sent_to_llm(make_runner):
    runner, backend = make_runner(happy_responses())
    res = run(runner, make_scan(cands=(), held=("XRP/USDT",), quality_bad=("XRP/USDT",)),
              make_state(held=("XRP/USDT",)))
    assert backend.calls == []
    assert "technical:XRP/USDT:quality_excluded" in res.abstained
    assert res.positions[0].flagged_for_head is False
    assert res.llm_calls == 0


def test_no_candidates_no_positions_zero_calls(make_runner):
    runner, backend = make_runner(happy_responses())
    res = run(runner, make_scan(cands=()))
    assert backend.calls == []
    assert res.llm_calls == 0 and res.head is None
    assert res.head_skipped_reason == "no_candidates_or_positions"


def test_analyst_payload_separation(make_runner):
    runner, backend = make_runner(happy_responses())
    js = JournalState(thesis={"XRP/USDT": Thesis(reasons=["r"], assumptions=["a"], red_lines=[], horizon_bars=4)})
    run(runner, make_scan(cands=("ETH/USDT",), held=("XRP/USDT",), news=[news_item("n")]),
        make_state(held=("XRP/USDT",)), js)
    techs = payloads_for(backend, "technical")
    assert [t["symbol"] for t in techs] == ["ETH/USDT", "XRP/USDT"]
    for t in techs:
        assert set(t) == set(P.TECHNICAL_KEYS)
        assert "thesis" not in json.dumps(t["position"] or {})
        assert "confidence" not in (t["position"] or {})
    assert techs[0]["position"] is None
    assert techs[1]["position"]["bars_held"] == 4
    flow = payloads_for(backend, "derivatives_flow")[0]
    assert flow["symbols"] == ["ETH/USDT", "XRP/USDT"]
    assert set(flow) == set(P.FLOW_KEYS)
    news = payloads_for(backend, "news_sentiment")[0]
    assert set(news) == set(P.NEWS_KEYS)
    bull = payloads_for(backend, "bull")[0]
    bear = payloads_for(backend, "bear")[0]
    assert set(bull) == set(P.BULL_KEYS) and set(bear) == set(P.BEAR_KEYS)
    assert "corr_to_open" not in bull and "bull" not in bear
    assert bull["technical"]["symbol"] == "ETH/USDT"
    risk = payloads_for(backend, "risk_manager")[0]
    assert set(risk) == set(P.RISK_KEYS)
    assert risk["candidates"][0]["bull"] is not None and risk["candidates"][0]["bear"] is not None
    assert [p["symbol"] for p in risk["positions"]] == ["XRP/USDT"]
    assert risk["candidates"][0]["planned_notional_usdt"] == pytest.approx(1000.0)


def test_gate_log_and_counts(make_runner):
    def bear(p):
        return bear_with("decline" if p["symbol"] == "SOL/USDT" else "survivable")(p)

    def tech(p):
        if p["symbol"] == "ADA/USDT":
            return tech_long(p, direction="flat", confidence=0.1)
        return tech_long(p)
    runner, backend = make_runner(happy_responses(bear=bear, technical=tech))
    res = run(runner, make_scan(cands=("ETH/USDT", "SOL/USDT", "ADA/USDT")))
    assert res.gate_counts["candidates_seen"] == 3
    assert res.gate_counts["g1_dropped"] == 1
    assert res.gate_counts["g2_bear_decline"] == 1
    assert res.gate_counts["head_calls"] == 1
    assert all(isinstance(g, GateDecision) for g in res.gate_log)
    assert len(res.gate_log) == 3 + 2 + 1
    by = {c.symbol: c.dropped_at for c in res.candidates}
    assert by == {"ETH/USDT": None, "SOL/USDT": "bear_decline", "ADA/USDT": "g1"}
    vet = payloads_for(backend, "head")[0]["vetoed"]
    assert {(v["symbol"], v["stage"]) for v in vet} == {("SOL/USDT", "bear_decline"), ("ADA/USDT", "g1")}


def test_result_serializable_and_deterministic(make_runner):
    r1 = run(make_runner(happy_responses())[0], make_scan(news=[news_item("a")]))
    r2 = run(make_runner(happy_responses())[0], make_scan(news=[news_item("a")]))
    j1 = json.dumps(r1.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False)
    j2 = json.dumps(r2.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False)
    assert j1 == j2


def test_cost_accumulated(settings, prompts_dir, skills):
    class CostBackend(MockBackend):
        def complete(self, **kw):
            r = super().complete(**kw)
            return r.model_copy(update={"cost_usd": 0.01})
    backend = CostBackend(happy_responses())
    runner = AgentRunner(settings, backend, skills, prompts_dir)
    res = run(runner)
    assert res.llm_calls == 6
    assert res.llm_cost_usd == pytest.approx(0.06)


# ======================================================================= 단위: effective_gate / flag_position
def test_effective_gate_tightens_only():
    ctx = CandidateContext(symbol="ETH/USDT", candidate=cand_of("ETH/USDT"), gates=[
        GateDecision(gate="G1", passed=True, reasons=[], size_multiplier=1.0, confidence_floor=0.65, notes=["a"]),
        GateDecision(gate="G2", passed=True, reasons=[], confidence_floor=0.75, notes=["bear_abstained"]),
        GateDecision(gate="G3", passed=True, reasons=[], size_multiplier=0.5, notes=["a", "fused"]),
    ])
    g = effective_gate(ctx)
    assert g == {"confidence_floor": 0.75, "size_multiplier": 0.5, "notes": ["a", "bear_abstained", "fused"]}
    rg = RiskGlobal(entries_paused=False, pause_reason=None, confidence_floor=0.8, exposure_cap_pct=20,
                    portfolio_note="")
    assert effective_gate(ctx, rg)["confidence_floor"] == 0.8
    empty = CandidateContext(symbol="X/USDT", candidate=cand_of("X/USDT"))
    assert effective_gate(empty) == {"confidence_floor": 0.65, "size_multiplier": 1.0, "notes": []}


def test_flag_position_rules():
    view = RiskPositionView(symbol="X", assumption_status=["holding"], red_lines_triggered=[], health=8,
                            action="hold", reason="")
    assert flag_position(view, [8], None, []) == (False, None)
    assert flag_position(view.model_copy(update={"action": "exit"}), [8], None, [])[0]
    assert flag_position(view, [HEALTH_FLAG_MAX, HEALTH_FLAG_MAX], None, [])[0]
    assert not flag_position(view, [HEALTH_FLAG_MAX + 1, HEALTH_FLAG_MAX], None, [])[0]
    assert not flag_position(view, [2], None, [])[0]
    tech = tech_long({"symbol": "X"}, exit_flag=True, exit_reason="r")
    ok, reason = flag_position(None, [], tech, ["hit"])
    assert ok and "technical_exit_flag:r" in reason and "red_line:hit" in reason


# ======================================================================= 단위: NewsStore
def test_news_store_new_headlines_dedupes():
    s = NewsStore()
    a, b = news_item("A"), news_item("B")
    assert s.new_headlines([a, a, b]) == [a, b]
    s.mark_seen([a], NOW)
    assert s.new_headlines([a, b]) == [b]
    assert s.headline_id(a) == P.headline_id(a)


def test_news_store_events_expire_and_ts_from_headline():
    s = NewsStore()
    ev = NewsEvent(headline_id="abc", score=-0.66, event_type="regulatory", symbols=["ETH/USDT"], summary="s",
                   stale=False)
    s.add_events([ev], NOW, {"abc": NOW - 20 * H})
    act = s.active_events(NOW)
    assert act[0]["ts"] == NOW - 20 * H and "expires_ts" not in act[0]
    assert s.active_events(NOW + 5 * H) == []          # 헤드라인 시각 + 24h 만료
    s.add_events([ev.model_copy(update={"headline_id": "def"})], NOW)
    assert [e["headline_id"] for e in s.active_events(NOW + 5 * H)] == ["def"]
    s.prune(NOW + 5 * H)
    assert set(s.events) == {"def"}


def test_news_store_risk_symbols_expire():
    s = NewsStore()
    s.add_risk_symbols(["ETH/USDT", ""], NOW)
    assert s.risk_symbols(NOW + 23 * H) == {"ETH/USDT"}
    assert s.risk_symbols(NOW + 24 * H) == set()


def test_news_store_save_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "news.json"
    s = NewsStore(p)
    s.mark_seen([news_item("A")], NOW)
    s.add_events([{"headline_id": "x1", "score": 0.33, "event_type": "etf", "symbols": ["BTC/USDT"],
                   "summary": "s", "stale": False}], NOW)
    s.add_risk_symbols(["SOL/USDT"], NOW)
    s.save()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    s2 = NewsStore(p)
    assert s2.to_dict() == s.to_dict()
    assert not list(p.parent.glob(".news_store.*.tmp"))


def test_news_store_corrupt_file_starts_empty(tmp_path, caplog):
    p = tmp_path / "news.json"
    p.write_text("{not json", encoding="utf-8")
    s = NewsStore(p)
    assert s.seen == {} and s.events == {} and s.risk == {}
    p.write_text("[1,2]", encoding="utf-8")
    assert NewsStore(p).seen == {}


def test_news_store_seen_pruned_after_ttl():
    s = NewsStore(seen_ttl_h=1)
    s.mark_seen([news_item("A")], NOW)
    s.prune(NOW + 2 * H)
    assert s.seen == {}


def test_news_store_save_memory_only_returns_none():
    assert NewsStore().save() is None


def test_pipeline_save_failure_does_not_crash(make_runner, tmp_path):
    class BadStore(NewsStore):
        def save(self):
            raise OSError("disk full")
    runner, backend = make_runner(happy_responses())
    res = run(runner, store=BadStore())
    assert res.decision_for("ETH/USDT").action == "BUY"


def test_concurrent_runs_independent(make_runner):
    """두 파이프라인을 동시에 돌려도 결과가 섞이지 않는다(공유 상태 없음)."""
    out = {}

    def go(key, cands):
        runner, _ = make_runner(happy_responses())
        out[key] = run(runner, make_scan(cands=cands))
    t1 = threading.Thread(target=go, args=("a", ("ETH/USDT",)))
    t2 = threading.Thread(target=go, args=("b", ("SOL/USDT",)))
    t1.start(); t2.start(); t1.join(); t2.join()  # noqa: E702
    assert [c.symbol for c in out["a"].approved()] == ["ETH/USDT"]
    assert [c.symbol for c in out["b"].approved()] == ["SOL/USDT"]


def test_invalid_equity_fails_closed_at_g1(make_runner):
    runner, backend = make_runner(happy_responses())
    res = run_pipeline(make_scan(), make_state(), 0.0, runner, JournalState(), NOW, news_store=NewsStore())
    assert res.candidates[0].dropped_at == "g1"
    assert "bull" not in roles_called(backend) and "head" not in roles_called(backend)
