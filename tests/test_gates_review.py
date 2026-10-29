"""적대적 리뷰: gates.py 결함 재현 테스트 (구현 수정 전에는 실패해야 한다)."""
from __future__ import annotations

import math

from tradingbot import features
from tradingbot.features import RiskPack
from tradingbot.gates import g1_candidate, g3_risk

from tests.test_gates import (  # noqa: F401 - 빌더 재사용
    SYM,
    g1,
    g3,
    make_liq,
    make_rp,
)

NAN = float("nan")


def test_g3_volume_share_fallback_uses_pack_notional_not_planned_notional():
    """24h 거래대금이 유동성 게이트에 없고 risk_pack 의 주문액/15분 거래대금 비율만 있을 때,
    그 비율은 리스크 팩을 만들 때의 계획 주문액(여기선 500)으로 계산된 값이다. G3 의 planned_notional(2000)
    에 그대로 적용하면 허용액이 4배 부풀려져 5% 한도를 넘는 주문이 축소 없이 통과한다."""
    qv = 960_000.0                          # 15분 평균 거래대금 = 10_000 → 5% 한도 = 500 USDT
    pack_notional = 500.0
    depth = 1_000_000.0
    share = features.order_notional_vs_15m_volume_share(pack_notional, qv)   # = 0.05 (한도 경계)
    impact = features.impact_bps_est(pack_notional, depth)
    rp = make_rp(order_notional_vs_15m_volume_share=share, impact_bps_est=impact, depth_taker_usdt=depth,
                 exec_cost_pct=features.exec_cost_pct(impact, 3.0))
    liq = make_liq(quote_volume_24h=None, depth_1pct_usdt=depth)
    planned = 2_000.0
    d = g3(risk_pack=rp, liquidity=liq, planned_notional=planned)
    allowed = 0.05 * qv / 96.0
    if d.passed:
        # 실효 주문액(planned × 배수)이 거래대금 5% 한도를 넘으면 안 된다.
        assert planned * d.size_multiplier <= allowed + 1e-6, (d.size_multiplier, d.notes)


def test_g1_nan_correlation_to_open_position_fails_open():
    """모듈 원칙: 무효(NaN) 입력은 가장 보수적인 쪽(탈락). 보유 종목과의 상관이 NaN 이면
    '같은 베팅' 여부를 판단할 수 없는데도 G1 이 통과시킨다."""
    d = g1(corr_to_open={"ETH/USDT": NAN}, position_count=1, exposure_pct=10.0)
    assert d.passed is False, d.notes


def test_g3_missing_risk_pack_passes_without_cvar_cap():
    """리스크 팩이 통째로 없으면(pipeline 은 RiskPack(symbol=s) 빈 팩을 넘긴다) CVaR·실행비용 원천이 전부 미상인데도
    G3 가 통과한다(CVaR 사이즈 상한 미적용) — fail-closed 원칙 위반."""
    d = g3(risk_pack=RiskPack(symbol=SYM))
    assert d.passed is False, (d.notes, d.size_multiplier)


def test_g3_risk_pack_and_liquidity_symbol_not_checked():
    """다른 종목의 risk_pack/유동성 게이트가 섞여 들어와도 G3 는 그대로 통과시킨다
    (verdict 의 symbol 은 검사하면서 팩의 symbol 은 검사하지 않음)."""
    rp = make_rp(symbol="BTC/USDT")
    liq = make_liq(symbol="BTC/USDT")
    d = g3(risk_pack=rp, liquidity=liq)
    assert d.passed is False, d.notes


def test_g3_exec_cost_fallback_omits_spread_term():
    """risk_pack.exec_cost_pct 가 없을 때 G3 는 impact/100 + 0.20 만 쓰고 v2.1 실행비용 정의의
    spread_bps/200 항을 빠뜨린다 → 실행비용 과소평가로 0.25·손절거리 한도를 넘는 후보가 통과한다."""
    spread = 9.0
    notional, depth = 1_000.0, 40_000.0
    impact = features.impact_bps_est(notional, depth)                 # 2.5
    true_cost = features.exec_cost_pct(impact, spread)                # 0.025 + 0.045 + 0.20 = 0.27
    stop = 1.0                                                        # 한도 0.25%
    assert true_cost > 0.25 * stop
    rp = make_rp(exec_cost_pct=None, spread_bps=spread, impact_bps_est=impact, depth_taker_usdt=depth)
    liq = make_liq(spread_bps=spread, depth_1pct_usdt=depth)
    d = g3(risk_pack=rp, liquidity=liq, planned_notional=notional, stop_pct=stop)
    assert d.passed is False, d.notes
