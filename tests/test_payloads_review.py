"""적대적 리뷰: tradingbot/agents/payloads.py (SPEC §20.3 / v2.1 §20.3a).

각 테스트는 리뷰에서 찾은 결함 하나를 재현한다(현재 구현에서 실패해야 함).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tradingbot.agents import payloads as P
from tradingbot.agents.schemas import NewsOutput
from tradingbot.news import SYMBOL_ALIASES

NOW = 1_761_609_600_000
H = 3_600_000
BTC = "BTC/USDT"
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- F1
def test_event_age_recomputed_from_ts_not_stale_stored_age_h():
    """레코드에 이전 사이클에 계산된 age_h 가 남아 있어도 ts·now_ms 가 있으면 지금 나이를 써야 한다.

    30시간 전 이벤트가 age_h=1 로 저장돼 있으면 24h 창을 통과해 '신선한' 이벤트로 bull/bear/head 에
    전달되고 event_signal 에 거의 전량 가중된다.
    """
    ev = {"headline_id": "old1", "score": 0.66, "event_type": "listing", "symbols": [BTC],
          "summary": "상장", "stale": False, "ts": NOW - 30 * H, "age_h": 1.0}
    assert P.symbol_news_events(BTC, [ev], NOW) == []
    d = P.build_bull(BTC, None, None, [ev], None, None, None, now_ms=NOW)
    assert d["news_events"] == []
    assert d["event_signal"] == {"score": None, "n_events": 0}


# ---------------------------------------------------------------- F2
def test_head_news_dict_form_newsoutput_keeps_risk_flag():
    """NewsOutput 을 모델로 주든 dict(model_dump)로 주든 risk_event_for_symbol 이 같아야 한다.

    dict 형태는 'events' 키가 있어 '이미 블록 형태'로 오인되고 risk_event_symbols 가 무시된다.
    """
    no = NewsOutput(events=[], risk_event=True, risk_event_symbols=[BTC], market_tone=-0.5,
                    market_note="", feed_health_note="")
    as_model = P.build_head([{"symbol": BTC, "news": no}], [], None, {}, None, [], now_ms=NOW)
    as_dict = P.build_head([{"symbol": BTC, "news": no.model_dump()}], [], None, {}, None, [], now_ms=NOW)
    assert as_model["approved"][0]["news"]["risk_event_for_symbol"] is True
    assert as_dict["approved"][0]["news"]["risk_event_for_symbol"] is True


# ---------------------------------------------------------------- F3
def test_build_flow_legacy_thresholds_arg_never_raises():
    """§20.3 호환용 thresholds 인자는 '받아서 무시'한다고 했지만 Mapping 이 아니면 TypeError 로 사이클을 죽인다."""
    from tradingbot.scanner import Thresholds

    d = P.build_flow({}, [BTC], Thresholds())
    assert d["symbols"] == [BTC]
    assert d["thresholds"] == P.FLOW_THRESHOLDS


# ---------------------------------------------------------------- F4
def test_build_news_aliases_values_are_symbols():
    """payload aliases 는 {alias_lower: symbol}. news.SYMBOL_ALIASES 형태(base → 별칭 튜플)를 넣으면
    값이 "('bitcoin', 'xbt')" 같은 문자열이 되어 news 역할이 쓸 수 없는 '심볼'을 받는다."""
    d = P.build_news([], SYMBOL_ALIASES, NOW, None, [])
    for alias, sym in d["aliases"].items():
        assert "(" not in sym and "'" not in sym, (alias, sym)


# ---------------------------------------------------------------- F5
def test_head_gate_floor_never_below_hard_min_confidence():
    """gate.confidence_floor 는 'G3 이후 실효 하한'. 하드 하한 0.65 보다 낮게 표시되면 총괄이 0.6 BUY 를
    통과한다고 믿게 된다(코드 G4 는 0.65 로 강등)."""
    item = {"symbol": BTC, "gate": {"confidence_floor": 0.5, "size_multiplier": 1.0, "notes": []}}
    h = P.build_head([item], [], None, {}, None, [], now_ms=NOW)
    assert h["approved"][0]["gate"]["confidence_floor"] >= P.HARD_LIMITS_PAYLOAD["min_confidence"]
    item2 = {"symbol": BTC, "gates": [{"size_multiplier": 0.5}]}
    h2 = P.build_head([item2], [], None, {}, None, [], now_ms=NOW)
    fl = h2["approved"][0]["gate"]["confidence_floor"]
    assert fl is not None and fl >= P.HARD_LIMITS_PAYLOAD["min_confidence"]


# ---------------------------------------------------------------- F6
def test_vetoed_stage_always_in_enum():
    """vetoed.stage 는 g1|bear_decline|risk_veto|g3 (§20.3a.6). 알 수 없는 원시 stage 가 그대로 새면 안 된다."""
    h = P.build_head([], [], None, {}, None, [
        {"symbol": BTC, "stage": "g3_exec_cost", "reason": "실행비용"},
        {"symbol": "ETH/USDT", "dropped_at": "G1_noise", "reason": "노이즈"},
    ])
    for row in h["vetoed"]:
        assert row["stage"] in P.VETO_STAGES, row


# ---------------------------------------------------------------- F7
def test_build_flow_symbols_set_is_deterministic_across_hash_seeds():
    """'후보 ∪ 보유' 를 set 으로 넘기면 symbols 순서가 PYTHONHASHSEED 에 따라 바뀐다(결정성 위반)."""
    code = ("import json;from tradingbot.agents import payloads as P;"
            "s={'BTC/USDT','ETH/USDT','SOL/USDT','XRP/USDT','DOGE/USDT','ADA/USDT','LINK/USDT'};"
            "print(json.dumps(P.build_flow({},s)['symbols']))")
    outs = set()
    for seed in ("1", "2", "3", "4", "5", "6"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env=env,
                           cwd=str(ROOT), timeout=60)
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip())
    assert len(outs) == 1, outs


# ---------------------------------------------------------------- F8
def test_event_signal_uses_ts_when_age_h_is_null():
    """공개 도우미 event_signal: age_h 키가 None 이고 ts 가 있으면 ts·now_ms 로 나이를 계산해야 한다.
    지금은 'age_h 키가 있다'는 이유만으로 정규화를 건너뛰어 1시간 전 −1.0 이벤트를 24h(가중 0.25)로 본다."""
    es = P.event_signal([{"score": -1.0, "age_h": None, "ts": NOW - 1 * H}], NOW)
    assert es["score"] is not None and es["score"] < -0.9
