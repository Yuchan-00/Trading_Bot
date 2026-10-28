"""journal.py 적대적 리뷰 테스트 — 각 테스트는 리뷰에서 찾은 결함 하나를 재현한다(오프라인)."""
from __future__ import annotations

import math

from tradingbot import journal as J
from tradingbot.agents.schemas import ActionItem, JournalDaily
from tradingbot.llm.backend import AgentResult
from tradingbot.models import ClosedTrade

HOUR = 3_600_000
DAY = 86_400_000
T0 = 1_760_000_000_000


def _trade(*, symbol="ETH/USDT", pnl_pct=0.01, opened_ts=T0, hold_ms=4 * HOUR, reason="take_profit",
           quote=1000.0, base_amount=None, entry=100.0, opened_cycle=10, bars=16) -> ClosedTrade:
    exit_price = entry * (1 + pnl_pct) if math.isfinite(pnl_pct) else entry
    pnl_quote = quote * pnl_pct
    return ClosedTrade(
        symbol=symbol, base_amount=base_amount if base_amount is not None else quote / entry,
        entry_price=entry, exit_price=exit_price, quote_spent=quote, quote_received=quote + pnl_quote,
        fee_quote_total=2.0, pnl_quote=pnl_quote, pnl_pct=pnl_pct, reason=reason, opened_cycle=opened_cycle,
        closed_cycle=opened_cycle + bars, opened_ts=opened_ts, closed_ts=opened_ts + hold_ms,
        thesis="x", confidence=0.7, exit_order_id="x1",
    )


class _Runner:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def run(self, role_id, payload, *, output_model=None):
        self.calls.append((role_id, payload, output_model))
        return AgentResult(role="journal", ok=True, output=self.output)


# R1 ---------------------------------------------------------------------------------
def test_missing_verdicts_do_not_fabricate_channel_disagreement():
    """entry_verdicts 가 없으면(상태 유실·재시작) channel_agreement 는 '모름'(null)이어야 한다.

    현재는 {technical: False, flow: False, news: False} 를 만들어 '세 채널 모두 진입 시 롱이 아니었다'는
    사실을 날조한다 → 프롬프트 4단계가 각 채널을 near_miss_rules 로 쓴다(SPEC v2.1: 없는 값은 null;
    prompts/journal.md: "A null verdicts channel is not agreement").
    """
    p = J.build_journal_post_trade(_trade(), None, [], None)
    ca = p["verdicts"]["channel_agreement"]
    assert ca is None or all(v is None for v in ca.values()), ca


def test_missing_channel_agreement_key_not_coerced_to_false():
    """verdicts 에 channel_agreement 가 아예 없을 때도 False 로 채워서는 안 된다."""
    verdicts = {"technical": {"setup_type": "breakout", "confidence": 0.7}}
    p = J.build_journal_post_trade(_trade(), None, [], verdicts)
    ca = p["verdicts"]["channel_agreement"]
    assert ca is None or all(v is None for v in ca.values()), ca


# R2 ---------------------------------------------------------------------------------
def test_daily_review_enforces_do_not_change_yet_below_min_sample():
    """closed_count < 20 이면 action_items 는 비어야 하고 do_not_change_yet=True 여야 한다
    (DESIGN §6 '20건 미만이면 표본 부족으로 보고만', prompts/journal.md 10·11단계 — 결정적 규칙).
    LLM 이 규칙을 어긴 출력을 내도 코드가 그대로 통과시킨다."""
    bad = JournalDaily(
        summary_bullets=["a", "b", "c"], what_worked=[], what_failed=[], risks=["r1", "r2", "r3"],
        action_items=[ActionItem(param="confidence_floor", from_value="0.65", to_value="0.55", reason="완화")],
        abstain_reason=None, do_not_change_yet=False, telegram_text="변경 1건",
    )
    state = J.JournalState(closed_count=5)
    out = J.daily_review(_Runner(bad), state, [], {"returns": {}, "trade_stats": {}})
    assert out is not None
    assert out.do_not_change_yet is True
    assert out.action_items == []


# R3 ---------------------------------------------------------------------------------
def test_nan_pnl_pct_is_null_not_break_even_in_payload():
    """pnl_pct 를 알 수 없으면 payload 는 null 이어야 한다(프롬프트: 'null → 손익 확인 불가, 본전 아님').
    현재 _pnl_pct 가 NaN 을 0.0 으로 바꿔 본전 거래를 날조한다."""
    t = _trade(pnl_pct=float("nan"))
    p = J.build_journal_post_trade(t, None, [], None)
    assert p["trade"]["pnl_pct"] is None


def test_nan_pnl_not_counted_as_noise_trade():
    """손익 불명 거래가 |pnl| < 0.3% '노이즈 거래' 로 집계된다."""
    trades = [_trade(pnl_pct=float("nan"), opened_ts=T0 + i * DAY) for i in range(3)]
    m = J.code_metrics(trades, [100.0, 101.0], None)
    assert m["attribution_buckets"]["noise_trades"] == 0


# R4 ---------------------------------------------------------------------------------
def test_daily_review_from_code_metrics_has_day_pnl():
    """daily_review 는 code_metrics 결과를 받는다고 문서화되어 있지만, 그 경로에서는
    metrics_table.returns.day_pnl_pct 가 항상 null 이다(code_metrics 가 만들지 않고 daily_review 도 계산하지 않음)."""
    trades = [_trade(opened_ts=T0 + i * DAY) for i in range(3)]
    metrics = J.code_metrics(trades, [10000.0, 10100.0, 10200.0], [100.0, 101.0, 102.0])
    runner = _Runner(JournalDaily.conservative_example())
    J.daily_review(runner, J.JournalState(), trades, metrics)
    payload = runner.calls[0][1]
    assert payload["metrics_table"]["returns"]["day_pnl_pct"] is not None


# R5 ---------------------------------------------------------------------------------
def test_daily_review_closed_count_consistent_with_decay_state():
    """state 가 갱신되지 않았을 때 closed_count 는 len(trades)(=25)로 대체되지만 decay_state/bias 는
    state 의 'insufficient_sample' 이 그대로 들어가 payload 가 자기모순이 된다(25건인데 표본 부족)."""
    trades = [_trade(pnl_pct=0.02, opened_ts=T0 + i * DAY) for i in range(25)]
    runner = _Runner(JournalDaily.conservative_example())
    J.daily_review(runner, J.JournalState(), trades, J.code_metrics(trades, [], None))
    p = runner.calls[0][1]
    assert p["closed_count"] == 25
    assert p["metrics_table"]["decay_state"] == J.decay_state(trades)


# R6 ---------------------------------------------------------------------------------
def test_overtrading_counts_entries_not_partial_fill_records():
    """trades_per_day 는 '평균 진입 수'(docstring)인데 부분 청산(reason=partial)으로 같은 진입이
    ClosedTrade 2건이 되면 2번 센다. 하루 5 진입(각각 부분+잔여 청산 = 10 레코드) → 실제 5/일 인데 mild."""
    trades = []
    # 과거 15일: 하루 1건 (표본 확보)
    for i in range(15):
        trades.append(_trade(opened_ts=T0 + i * DAY, pnl_pct=0.02 if i % 2 else -0.01,
                             reason="take_profit" if i % 2 else "stop_loss"))
    # 마지막 날: 5 진입, 각 진입이 partial + 잔여 청산 2 레코드
    last_day = T0 + 30 * DAY
    for k in range(5):
        ots = last_day + k * HOUR
        trades.append(_trade(opened_ts=ots, reason="partial", base_amount=5.0, hold_ms=10 * 60_000))
        trades.append(_trade(opened_ts=ots, reason="take_profit", base_amount=5.0, hold_ms=20 * 60_000))
    assert len(trades) >= 20
    out = J.bias_severities(trades, 15)
    assert out["overtrading"] == "none", J.trades_per_day(trades)


# R7 ---------------------------------------------------------------------------------
def test_btc_excess_uses_aligned_points_when_equity_has_nan():
    """btc_curve 길이 검사는 원본 길이로 하지만 비유한 값을 각각 따로 걸러, 구간이 어긋난다.
    equity=[nan,100,110], btc=[100,110,121] → 같은 구간(1→2) BTC 수익 10% 이므로 초과수익 0% 이어야 한다."""
    m = J.code_metrics([], [float("nan"), 100.0, 110.0], [100.0, 110.0, 121.0])
    assert m["excess_vs_btc_pct"] is not None
    assert abs(m["excess_vs_btc_pct"]) < 1e-9, m["excess_vs_btc_pct"]


# R8 ---------------------------------------------------------------------------------
def test_all_winning_day_profit_factor_not_reported_as_unavailable():
    """손실 없는 날 PF 는 무한대(알려진 값)인데 daily payload 에는 null('없음')로 들어간다.
    프롬프트는 null 을 '데이터 없음' 위험으로 보고 action_items 를 막는다. 정보가 사라지지 않아야 한다
    (예: 큰 유한값 또는 별도 표시 키)."""
    trades = [_trade(pnl_pct=0.02, opened_ts=T0 + i * DAY) for i in range(3)]
    runner = _Runner(JournalDaily.conservative_example())
    J.daily_review(runner, J.JournalState(), trades, J.code_metrics(trades, [], None))
    p = runner.calls[0][1]
    assert p["metrics_table"]["trade_stats"]["profit_factor"] is not None
