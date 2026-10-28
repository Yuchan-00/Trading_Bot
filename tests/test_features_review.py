"""features.py 적대적 리뷰 — 각 테스트는 리뷰에서 찾은 결함 하나를 재현한다(오프라인·결정적)."""
from __future__ import annotations

from tradingbot import features as F
from tradingbot.models import Candle

H = 3_600_000


def _flat(levels) -> list[Candle]:
    """범위 0 봉(open=high=low=close) — 일목 중간값을 정확히 통제."""
    return [Candle(ts=i * H, open=float(x), high=float(x), low=float(x), close=float(x), volume=10.0)
            for i, x in enumerate(levels)]


def _waypoints(points) -> list[float]:
    out: list[float] = []
    for (b0, p0), (b1, p1) in zip(points, points[1:]):
        for b in range(b0, b1):
            out.append(p0 + (p1 - p0) * (b - b0) / (b1 - b0))
    out.append(points[-1][1])
    return out


def _from_closes(closes, wick=0.2) -> list[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(ts=i * H, open=prev, high=max(prev, c) + wick, low=min(prev, c) - wick,
                          close=c, volume=10.0))
        prev = c
    return out


# 전환선이 기준선 위 → 같아짐(접촉) → 다시 위. 아래에서 위로 넘어간 적이 없다.
TOUCH_LEVELS = [
    100, 99, 100, 100, 100, 99, 99, 99, 100, 101, 102, 102, 103, 103, 103, 103, 104, 105, 105, 104, 105,
    105, 104, 105, 104, 103, 104, 103, 102, 102, 101, 100, 101, 101, 100, 101, 101, 102, 103, 102, 103,
    102, 103, 104, 104, 105, 106, 107, 108, 109, 109, 109, 108, 109, 109, 110, 110, 111, 111, 110, 109,
    109, 109, 110, 109, 109, 109, 108, 107, 107, 106, 107, 106, 107, 106, 106, 107, 108, 109, 109, 108,
    109, 109, 109, 108, 108, 109, 108, 107, 107,
]

# 마지막 봉에서 전환선이 기준선을 아래→위로 교차, 교차 수준(103.75)은 구름(101..104.5) 안.
INSIDE_CROSS_LEVELS = [
    100, 99, 100, 101, 101, 100, 99, 99, 100, 99, 99, 98, 99, 98, 97, 96, 97, 98, 99, 100, 99, 98, 98,
    99, 99, 100, 101, 102, 102, 101, 100, 99, 99, 99, 100, 101, 100, 101, 102, 103, 104, 103, 104, 103,
    103, 104, 104, 103, 104, 103, 103, 104, 104, 105, 106, 105, 106, 105, 104, 105, 105, 105, 106, 106,
    105, 104, 104, 104, 105, 105, 105, 104, 103, 104, 105, 104, 103, 102, 102, 103, 104, 104, 104, 104,
    104, 104, 103, 103, 104, 105,
]


def test_tk_cross_touch_without_crossing_is_not_a_bullish_cross():
    """결함: (전환−기준) 이 +0.5 → 0 → +0.5 로 '접촉 후 이탈'만 해도 tk_cross=bullish 로 라벨된다.
    교차는 아래에서 위로 넘어가는 것이고, technical 프롬프트는 이를 진입 트리거로 쓴다."""
    cs = _flat(TOUCH_LEVELS)
    lines = F.ichimoku_lines(cs)
    t = len(cs) - 1
    d = [lines["tenkan"][i] - lines["kijun"][i] for i in range(t - 3, t + 1)]
    assert d[0] > 0 and d[1] > 0 and d[2] == 0 and d[3] > 0   # 한 번도 아래로 내려간 적 없음
    lab = F.ichimoku(cs)
    assert lab is not None
    assert lab.tk_cross == "none", f"접촉만 했는데 {lab.tk_cross}/{lab.cross_strength} 로 라벨"


def test_bullish_cross_inside_cloud_is_weak_per_ichimoku_skill():
    """결함: 구름 안에서 일어난 강세 교차를 'medium' 으로 라벨한다. skills/ichimoku 는
    'cross_strength weak (it happened below or inside the cloud) is not a trigger' 라고 정의하고,
    technical 프롬프트는 strong/medium 을 트리거로 받는다 → 구름 안 교차가 트리거로 통과."""
    cs = _flat(INSIDE_CROSS_LEVELS)
    lines = F.ichimoku_lines(cs)
    t = len(cs) - 1
    assert lines["tenkan"][t - 1] - lines["kijun"][t - 1] < 0 < lines["tenkan"][t] - lines["kijun"][t]
    cloud = F._cloud_at(lines, t)
    level = (lines["tenkan"][t] + lines["kijun"][t]) / 2.0
    assert min(cloud) < level < max(cloud)          # 교차 수준이 구름 안
    lab = F.ichimoku(cs)
    assert lab is not None and lab.tk_cross == "bullish"
    assert lab.cross_strength == "weak", f"구름 안 교차가 {lab.cross_strength}"


def test_supply_zone_reported_below_price_gives_negative_distance():
    """결함: 하락 돌파 뒤 가격이 공급 존을 되찾아 위로 올라가도(choch_up) 그 존을 계속
    nearest_supply_zone 으로 보고하고 distance_to_supply_pct 를 음수로 준다. skills/smc 는 공급 존을
    'mirror above price' 로 설명하고, technical 프롬프트 4단계는 distance_to_supply_pct < 1.0 →
    wait_pullback, smc 스킬은 tp_pct 를 이 거리로 낮춘다 → 상승 구조에서 음수 목표/불필요한 대기."""
    closes = _waypoints([(0, 105), (8, 100), (16, 110), (24, 96), (30, 95), (44, 115)])
    cs = _from_closes(closes)
    events = F.structure_events(cs)
    assert [k for _, k in events][-2:] == ["bos_down", "choch_up"]
    price = cs[-1].close
    z = F.zones(cs, price)
    assert z["nearest_supply_zone"] is not None
    d = z["distance_to_supply_pct"]
    assert d is None or d >= 0, f"공급 존 {z['nearest_supply_zone']} 이 가격 {price} 아래인데 거리 {d}"


def test_demand_zone_is_bearish_candle_range_low_high():
    """결함(SPEC §17): nearest_demand_zone 은 '[low, high] 마지막 강세 BOS 직전 약세 봉 범위' 인데
    구현은 [low, open](몸통 상단)을 쓴다 → 존 상단과 distance_to_demand_pct 가 SPEC 과 다르다."""
    closes = _waypoints([(0, 100), (8, 105), (16, 95), (24, 110), (30, 112)])
    cs = _from_closes(closes)
    events = F.structure_events(cs)
    up = [t for t, k in events if k in ("bos_up", "choch_up")]
    assert up
    bear = next(cs[j] for j in range(up[-1] - 1, -1, -1) if cs[j].close < cs[j].open)
    z = F.zones(cs, 111.0)
    assert z["nearest_demand_zone"] == [bear.low, bear.high]
