"""tradingbot.dashboard 적대적 리뷰 테스트 (SPEC §14).

각 테스트는 현재 구현의 결함 하나를 재현한다(실패해야 정상). 결함이 고쳐지면 통과한다.
구현 파일은 수정하지 않는다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradingbot.config import HardLimits, load_settings
from tradingbot.dashboard import create_app
from tradingbot.dashboard.app import summarize_state
from tradingbot.models import BotState, DailyStats, Position
from tradingbot.risk import drawdown_level as risk_drawdown_level

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def make_settings(data_dir: Path, **over):
    env = {"AGENT_BACKEND": "mock", "DATA_DIR": str(data_dir)}
    env.update(over)
    return load_settings(env)


def btc_position(mark: float = 81_000.0) -> Position:
    return Position(
        symbol="BTC/USDT", base_amount=0.005, entry_price=80_000.0, quote_spent=400.0, fee_quote=0.4,
        stop_loss_price=79_000.0, take_profit_price=82_000.0, opened_cycle=1, opened_ts=1_700_000_000_000,
        thesis="t", confidence=0.7, last_mark=mark,
    )


def write_state(data_dir: Path, state: BotState) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state.json").write_text(
        json.dumps(state.model_dump(mode="json"), sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------- 결함 1: testnet/live 모드 자본·낙폭이 엉터리
# Portfolio는 testnet/live에서 paper_quote_balance=0 으로 두고(현금은 거래소에 있음) equity(marks, quote_balance)로
# 계산한다. 대시보드 compute_equity는 모드와 무관하게 paper_quote_balance를 현금으로 쓰므로 live에서는
# 자본=포지션 평가액(현금 0), 낙폭=최고자본 대비 ~100% -> 헤더에 "stop_all"이 항상 떠 있게 된다.
@pytest.mark.parametrize("mode", ["live", "testnet"])
def test_live_mode_header_must_not_report_position_value_as_equity(mode: str):
    st = BotState(
        mode=mode, cycle=10, paper_quote_balance=0.0, peak_equity=10_000.0,
        daily=DailyStats(date=TODAY, start_equity=10_000.0),
        positions={"BTC/USDT": btc_position()},
    )
    s = summarize_state(st, now_ms=1_700_000_000_000)
    # 포지션 평가액(405)만을 자본으로 내놓고 96% 낙폭/stop_all 을 표시하면 안 된다.
    assert s["drawdown_level"] != "stop_all", s
    assert s["drawdown_pct"] is None or s["drawdown_pct"] < HardLimits.DRAWDOWN_CAUTION, s
    assert s["equity"] is None or s["equity"] > 405.0, s


@pytest.mark.parametrize("mode", ["live", "testnet"])
def test_live_mode_without_positions_is_not_total_drawdown(mode: str, tmp_path: Path):
    data_dir = tmp_path / "data"
    st = BotState(mode=mode, cycle=3, paper_quote_balance=0.0, peak_equity=10_000.0,
                  daily=DailyStats(date=TODAY, start_equity=10_000.0))
    write_state(data_dir, st)
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    s = c.get("/api/state").json()["summary"]
    assert s["drawdown_level"] != "stop_all", s
    assert s["equity"] is None or s["equity"] > 0.0, s


# ---------------------------------------------------------------- 결함 2: 당일 손익이 전날 미실현 손익을 이중 계산
# daily.start_equity 는 날짜 롤오버 시점의 자본(보유 포지션의 당시 마크 포함)이다. 대시보드는
# daily_pnl = 실현손익 + Σ base*(mark - entry_price) 로 "진입 이후 전체 미실현"을 더하므로,
# 오늘 자본이 시작자본과 같아도(아무 변동 없음) 어제 얻은 미실현 이익이 당일 손익으로 표시된다.
def test_daily_pnl_does_not_double_count_unrealized_from_previous_days():
    # 어제 진입(마크 90,000 -> 미실현 +50). 오늘 시작자본 10,000 = 현금 9,550 + 평가액 450. 오늘 변동 없음.
    pos = btc_position(mark=90_000.0)
    st = BotState(
        mode="paper", cycle=200, paper_quote_balance=9_550.0, peak_equity=10_000.0,
        daily=DailyStats(date=TODAY, start_equity=10_000.0, realized_pnl=0.0),
        positions={"BTC/USDT": pos},
    )
    s = summarize_state(st, now_ms=1_700_000_000_000)
    assert s["equity"] == pytest.approx(10_000.0)
    # 자본이 시작자본과 같으므로 당일 손익은 0이어야 한다 (현재 +50 / +0.5% 로 표시됨)
    assert s["daily_pnl"] == pytest.approx(0.0, abs=1e-6), s
    assert s["daily_pnl_pct"] == pytest.approx(0.0, abs=1e-9), s


# ---------------------------------------------------------------- 결함 3: 낙폭 사다리 단계가 risk.py 와 경계에서 어긋남
# risk.drawdown_level 은 부동소수 잡음을 흡수하려고 경계값에 _EPS 허용오차를 두고(보수적으로 당김),
# 대시보드 drawdown_level 은 그대로 >= 비교만 한다. 같은 (peak, equity) 에 대해 봇은 stop_all/block 로
# 신규 진입을 막는데 헤더는 한 단계 낮은 level 을 보여준다. 어휘도 다르다(risk "normal" vs 대시보드 "none").
_RISK_TO_DASH = {"normal": "none", "caution": "caution", "block": "block", "stop_all": "stop_all"}


@pytest.mark.parametrize("peak,equity", [
    (9_003.5, 8_463.29),   # risk: stop_all, 대시보드: block
    (9_014.0, 8_608.37),   # risk: block,    대시보드: caution
    (9_007.0, 8_736.79),   # risk: caution,  대시보드: none
])
def test_drawdown_level_agrees_with_risk_ladder_at_boundaries(peak: float, equity: float):
    st = BotState(mode="paper", cycle=1, paper_quote_balance=equity, peak_equity=peak,
                  daily=DailyStats(date=TODAY, start_equity=peak))
    s = summarize_state(st, now_ms=0)
    expected = _RISK_TO_DASH[risk_drawdown_level(equity, peak)]
    assert s["drawdown_level"] == expected, (s["drawdown_pct"], s["drawdown_level"], expected)


# ---------------------------------------------------------------- 결함 4: 잘못된 UTF-8 바이트가 있으면 500 / 앱 생성 실패
# _read_jsonl 과 _read_token 은 OSError 만 잡는다. read_text(encoding="utf-8") 가 던지는 UnicodeDecodeError
# (ValueError 하위) 는 그대로 새어 나가 /api/decisions, /api/journal, /api/costs, /api/health 가 500 이 된다.
# 한글 줄이 디스크 풀/크래시로 중간에 잘리면(불완전한 멀티바이트) 이후 모든 폴링이 실패한다.
def test_invalid_utf8_in_jsonl_is_not_500(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    bad_tail = b'{"ts": 1, "cycle": 1, "symbol": "BTC/USDT", "action": "HOLD"}\n{"ts": 2, "reason": "\xed\x95'  # 잘린 '한'
    (data_dir / "logs" / "decisions.jsonl").write_bytes(bad_tail)
    (data_dir / "logs" / "journal.jsonl").write_bytes(b'{"ts": 1}\n\xff\xfe\n')
    (data_dir / "cost_ledger.jsonl").write_bytes(
        json.dumps({"date": TODAY, "model": "m", "role": "r", "cost_usd": 0.5}).encode() + b"\n\xff\n"
    )
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    r = c.get("/api/decisions")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    r = c.get("/api/journal")
    assert r.status_code == 200, r.text
    r = c.get("/api/costs")
    assert r.status_code == 200, r.text
    assert r.json()["today_usd"] == pytest.approx(0.5)


def test_invalid_utf8_token_file_does_not_crash_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "control").mkdir(parents=True)
    (data_dir / "control" / "dashboard_token").write_bytes(b"\xff\xfe\x00")
    app = create_app(make_settings(data_dir), data_dir)  # 현재: UnicodeDecodeError 로 앱 생성 자체가 실패
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200
    r = c.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": "anything"})
    assert r.status_code in (401, 503)
    assert not (data_dir / "control" / "STOP").exists()


# ---------------------------------------------------------------- 결함 5: reason 이 그대로 로그에 들어가 로그 주입 가능
# POST /api/kill 의 reason(사용자 입력) 을 개행 제거 없이 WARNING 로그에 쓴다. 개행을 넣으면 가짜 로그 줄을 만들 수 있다.
def test_kill_reason_control_chars_are_not_written_raw_to_log(tmp_path: Path, caplog):
    data_dir = tmp_path / "data"
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    tok = (data_dir / "control" / "dashboard_token").read_text(encoding="utf-8").strip()
    forged = "x\n2026-10-28 00:00:00 ERROR tradingbot.cycle 킬스위치 해제됨"
    with caplog.at_level(logging.DEBUG, logger="tradingbot.dashboard"):
        r = c.post("/api/kill", json={"reason": forged}, headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200
    for rec in caplog.records:
        assert "\n" not in rec.getMessage(), repr(rec.getMessage())
