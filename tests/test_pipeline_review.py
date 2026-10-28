"""pipeline.py 적대적 리뷰 테스트 — 각 테스트는 발견된 결함 하나를 재현한다(현재 실패해야 정상).

test_pipeline.py 의 픽스처·도우미를 재사용한다(같은 tests/ 디렉터리).
"""
from __future__ import annotations

import pytest

from test_pipeline import (  # noqa: F401 - 픽스처 재사용
    EQUITY,
    NOW,
    H,
    FlowOutput,
    FlowSymbolView,
    JournalState,
    NewsEvent,
    NewsOutput,
    NewsStore,
    happy_responses,
    make_runner,
    make_scan,
    make_state,
    news_item,
    payloads_for,
    prompts_dir,
    risk_with,
    roles_called,
    run,
    run_pipeline,
    settings,
    skills,
)
from tradingbot.config import HardLimits
from tradingbot.llm.cost import BudgetExceeded


# ---------------------------------------------------------------- R1: MAX_POSITIONS 초과
def test_r1_head_buys_cannot_exceed_remaining_position_slots(make_runner):
    """보유 2개 + 승인 후보 2개 → 총괄 BUY 2개가 그대로 통과하면 포지션 4개(하드 리밋 3 초과).

    G1 은 후보마다 같은 len(state.positions)=2 로 판정하고, G4 는 포지션 수를 보지 않는다(§19).
    파이프라인(승인 목록 또는 head 정리)이 남은 슬롯 수로 BUY 를 제한해야 한다.
    """
    held = ("XRP/USDT", "ADA/USDT")
    runner, backend = make_runner(happy_responses())
    res = run(runner, make_scan(cands=("ETH/USDT", "SOL/USDT"), held=held), make_state(held=held))
    buys = [d for d in (res.head.decisions if res.head else []) if d.action == "BUY"]
    assert len(held) + len(buys) <= HardLimits.MAX_POSITIONS, [d.symbol for d in buys]


# ---------------------------------------------------------------- R2: 뉴스 위험 심볼 표기 불일치로 G1 우회
def test_r2_news_risk_base_ticker_still_blocks_g1(make_runner):
    """news LLM 이 risk_event_symbols 에 'ETH'(base 티커)를 넣으면 payloads 는 'ETH/USDT' 와 매칭해
    head 에 risk_event_for_symbol=True 를 보여주지만, G1 은 정확 일치만 봐서 차단하지 않는다."""
    def news_risk(p):
        return NewsOutput(events=[NewsEvent(headline_id=p["headlines"][0]["id"], score=-1.0, event_type="hack",
                                            symbols=["ETH"], summary="해킹", stale=False)],
                          risk_event=True, risk_event_symbols=["ETH"], market_tone=-0.5, market_note="악재",
                          feed_health_note="정상")
    runner, backend = make_runner(happy_responses(news_sentiment=news_risk))
    res = run(runner, make_scan(news=[news_item("ETH 해킹")]), store=NewsStore())
    assert res.candidates[0].dropped_at == "g1", (res.candidates[0].dropped_at, res.news_risk_symbols)
    assert "bull" not in roles_called(backend)


# ---------------------------------------------------------------- R3: flow per_symbol 누락이 기권으로 기록되지 않음
def test_r3_missing_flow_view_is_recorded_as_abstain(make_runner):
    """FlowOutput 이 후보 심볼의 per_symbol 을 빠뜨리면(또는 'ETH' 처럼 다른 표기) G1 은 flow 기권으로 보고 통과시키는데,
    abstained 에는 아무것도 남지 않아 head 의 abstained_channels 가 '플로우 정상'처럼 보인다."""
    def flow_missing(p):
        return FlowOutput(per_symbol=[], market_wide_note="중립", data_confidence="high")
    runner, backend = make_runner(happy_responses(derivatives_flow=flow_missing))
    res = run(runner)
    assert any(a.startswith("derivatives_flow:ETH/USDT") for a in res.abstained), res.abstained


def test_r3b_flow_block_with_base_ticker_symbol_not_bypassed(make_runner):
    """flow LLM 이 symbol='ETH' 로 block_new_longs=True 를 내면 정확 일치 매칭 실패 → 차단이 사라진다."""
    def flow_block(p):
        v = FlowSymbolView(symbol="ETH", posture="euphoria", crowding_risk="high", squeeze_direction="none",
                           cascade_risk="high", nearest_long_liq_cluster_pct_below=1.0, stop_inside_cascade=True,
                           washout_setup=False, block_new_longs=True, block_reason="과밀", flow_bias="avoid",
                           confidence=0.8, evidence=["flow_packs.ETH/USDT.funding_regime: extreme_long"])
        return FlowOutput(per_symbol=[v], market_wide_note="과밀", data_confidence="high")
    runner, backend = make_runner(happy_responses(derivatives_flow=flow_block))
    res = run(runner)
    c = res.candidates[0]
    assert c.dropped_at == "g1" or any(a.startswith("derivatives_flow:ETH/USDT") for a in res.abstained), \
        (c.dropped_at, res.abstained)


# ---------------------------------------------------------------- R4: 40개 초과 헤드라인이 분석 없이 seen 처리
def test_r4_headlines_truncated_from_payload_are_not_marked_seen(make_runner):
    """build_news 는 최신 40개만 보내는데 mark_seen 은 new_heads 전부를 seen 으로 만든다 →
    잘린 헤드라인(예: ts 없는 새 해킹 기사)은 영영 채점되지 않는다."""
    items = [news_item(f"ETH 기사 {i}", ts=NOW - (i + 1) * 60_000) for i in range(45)]
    store = NewsStore()
    runner, backend = make_runner(happy_responses())
    run(runner, make_scan(news=items), store=store)
    sent = {h["id"] for h in payloads_for(backend, "news_sentiment")[0]["headlines"]}
    assert len(sent) == 40
    unsent = [it for it in items if store.headline_id(it) not in sent]
    assert len(unsent) == 5
    assert store.new_headlines(unsent) == unsent, "보내지 않은 헤드라인이 seen 으로 표시됨"


# ---------------------------------------------------------------- R5: 새 헤드라인 없는 사이클에 head news 가 null
def test_r5_head_news_block_not_null_when_store_has_events(make_runner):
    """새 헤드라인이 없는 사이클(대부분)에 head 의 approved[].news 는 null('news abstained' 의미, head.md)이 되어
    저장소의 24h 이벤트(확신도 감점 대상 등)가 총괄에게 보이지 않는다. abstained_channels 에도 news 기권은 없다."""
    store = NewsStore()
    runner1, _ = make_runner(happy_responses())
    run(runner1, make_scan(news=[news_item("ETH 상장")]), store=store)
    runner2, backend2 = make_runner(happy_responses())
    res2 = run(runner2, make_scan(news=[]), store=store, now=NOW + 15 * 60_000)
    assert not any(a.startswith("news_sentiment") for a in res2.abstained)
    bull_p = payloads_for(backend2, "bull")[0]
    assert bull_p["news_events"], "전제: 저장소 이벤트는 bull 에게 전달됨"
    head_p = payloads_for(backend2, "head")[0]
    a = head_p["approved"][0]
    assert a["news"] is not None and a["news"]["events"], a["news"]


# ---------------------------------------------------------------- R6: 리스크 관리자 exposure_cap_pct 무시
def test_r6_risk_exposure_cap_enforced(make_runner):
    """risk_manager global.exposure_cap_pct 는 head 프롬프트가 'exposure_pct ≥ cap → HOLD' 로만 다룬다.
    entries_paused 는 코드가 강제하는데 같은 종류의 조이기인 exposure_cap 은 코드가 보지 않아 head BUY 가 통과한다."""
    base = risk_with("approve")

    def risk_capped(p):
        out = base(p)
        g = out.global_.model_copy(update={"exposure_cap_pct": 5.0})
        return out.model_copy(update={"global_": g})
    held = ("XRP/USDT",)                       # 5 × 100 = 500 USDT = 자본의 5% → 남은 한도 0
    runner, backend = make_runner(happy_responses(risk_manager=risk_capped))
    res = run(runner, make_scan(cands=("ETH/USDT",), held=held), make_state(held=held))
    buys = [d for d in (res.head.decisions if res.head else []) if d.action == "BUY"]
    assert res.approved() == [] or buys == [], [d.symbol for d in buys]


# ---------------------------------------------------------------- R7: SPEC §20.5 BudgetExceeded 전파 (선언된 편차)
def test_r7_budget_exceeded_signalled_to_cycle(make_runner):
    """SPEC §20.5 는 '예외는 cycle 로 전파'라 하지만, 같은 SPEC 의 'HOLD 처리 + abstained "budget"' 결과와
    원래 테스트(test_pipeline.py::test_budget_exceeded_*: 결과를 반환해야 함)가 함께 성립하려면 예외 대신
    ``PipelineResult.budget_exceeded=True`` 로 알려야 한다. cycle 은 이 플래그로 pause_llm 을 호출해야 한다.
    여기서는 그 신호가 빠짐없이 실리는지(어느 단계든)와 이후 LLM 호출이 없는지를 고정한다."""
    def bear(p):
        raise BudgetExceeded("일일 예산 초과")
    runner, backend = make_runner(happy_responses(bear=bear))
    res = run(runner)
    assert res.budget_exceeded is True and "budget" in res.abstained
    assert res.head is None and res.head_skipped_reason == "budget_exceeded"
    assert "risk_manager" not in roles_called(backend) and "head" not in roles_called(backend)
