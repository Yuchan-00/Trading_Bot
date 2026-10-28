"""tradingbot/testing.py — 합성 시세·MockBackend 시나리오 + ``main run --offline-fixture`` end-to-end (오프라인)."""
from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path

import pytest

from tradingbot import main as M
from tradingbot import quality
from tradingbot import testing as T
from tradingbot.agents.schemas import HeadOutput, TechnicalOutput
from tradingbot.config import load_settings
from tradingbot.exchange import ExchangeError
from tradingbot.scanner import in_funding_window

NOW = 1_793_181_605_000   # 2026-10-28 10:00:05 UTC 근방(펀딩창 밖)


def _market(now=NOW) -> T.FakeMarketData:
    return T.FakeMarketData(clock_ms=lambda: now)


# ======================================================================= FakeMarketData
def test_fake_market_deterministic_and_closed_bars():
    a, b = _market(), _market()
    for tf, ms in (("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000), ("1d", 86_400_000)):
        ca, cb = a.fetch_ohlcv("ETH/USDT", tf, 200), b.fetch_ohlcv("ETH/USDT", tf, 200)
        assert ca == cb and len(ca) == 200
        assert ca[-1].ts + ms <= NOW < ca[-1].ts + 2 * ms          # 마지막 = 완료 봉
        assert all(x.ts < y.ts for x, y in zip(ca, ca[1:]))
        assert all(c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high for c in ca)
    assert a.fetch_ticker("BTC/USDT") == b.fetch_ticker("BTC/USDT")
    assert a.fetch_ticker("BTC/USDT").last != a.fetch_ticker("ETH/USDT").last
    assert a.server_time_ms() == NOW


def test_fake_market_passes_quality_and_validates_symbols():
    m = _market()
    candles = {tf: m.fetch_ohlcv("SOL/USDT", tf, n) for tf, n in (("15m", 300), ("1h", 200), ("4h", 200), ("1d", 120))}
    rep = quality.check("SOL/USDT", candles, m.fetch_ticker("SOL/USDT"), NOW, 15)
    assert rep.ok, rep.reasons
    ob = m.fetch_orderbook_summary("SOL/USDT")
    assert ob.best_bid < ob.best_ask and ob.spread_pct == pytest.approx(0.0002)
    assert m.fetch_futures_context("SOL/USDT").funding_rate == pytest.approx(0.0001)
    assert m.fetch_ticker("FOO/USDT").last > 0                      # 미지 기호도 결정적 기준가
    with pytest.raises(ExchangeError):
        m.fetch_ticker("ETH/BTC")
    with pytest.raises(ExchangeError):
        m.fetch_ohlcv("ETH/USDT", "7m", 10)


# ======================================================================= 시나리오
def test_scenarios_and_env_selection(caplog):
    assert T.scenario_responses("conservative") == {}
    assert set(T.scenario_responses("approve")) == {"technical", "derivatives_flow", "bull", "bear",
                                                     "risk_manager", "head"}
    with pytest.raises(ValueError):
        T.scenario_responses("yolo")
    with caplog.at_level(logging.WARNING, logger="tradingbot.testing"):
        be = T.mock_backend_from_env({"MOCK_SCENARIO": "yolo"})
    assert be.responses == {} and "conservative" in caplog.text
    assert set(T.mock_backend_from_env({"MOCK_SCENARIO": "APPROVE"}).responses) >= {"head"}


def test_conservative_factory_echoes_payload_symbol():
    out = T.conservative_factory("technical", TechnicalOutput, {"symbol": "ETH/USDT"})
    assert isinstance(out, TechnicalOutput) and out.symbol == "ETH/USDT" and out.direction == "flat"
    head = T.conservative_factory("head", HeadOutput, {"approved": [{"symbol": "SOL/USDT"}], "flagged_positions": []})
    assert [d.symbol for d in head.decisions] == ["SOL/USDT"] and head.decisions[0].action == "HOLD"


def test_approve_outputs_respect_hard_limits():
    resp = T.scenario_responses("approve")
    tech = resp["technical"]({"symbol": "ETH/USDT", "feature_pack": {"price": 2000.0, "noise_floor_stop_pct": 3.0}})
    assert tech.direction == "long" and tech.stop_pct <= 2.0 and tech.tp_pct <= 4.0
    assert tech.stop_candidate_price < 2000.0 < tech.target_candidate_price


# ======================================================================= main run --offline-fixture
def _run(tmp_path: Path, scenario: str | None = None) -> tuple[int, str, str, Path]:
    data = tmp_path / "data"
    env = {"MODE": "paper", "AGENT_BACKEND": "mock", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
           "DATA_DIR": str(data)}
    if scenario:
        env["MOCK_SCENARIO"] = scenario
    out, err = io.StringIO(), io.StringIO()
    code = M.main(["run", "--once", "--offline-fixture"], env=env, out=out, err=err)
    # 픽스처는 실제 paper 상태를 오염시키지 않도록 <DATA_DIR>/offline_fixture 에 쓴다(main.offline_fixture_settings)
    return code, out.getvalue(), err.getvalue(), data / M.OFFLINE_FIXTURE_DIRNAME


def _decisions(data: Path) -> list[dict]:
    p = data / "logs" / "decisions.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_offline_fixture_conservative_cycle_holds(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        code, _, err, data = _run(tmp_path)
    assert code == 0, err
    assert "Traceback" not in caplog.text
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    assert state["cycle"] == 1 and state["positions"] == {}
    assert _decisions(data)
    assert list((data / "logs" / "cycles").rglob("cycle_1.json"))
    assert "FakeMarketData" in caplog.text
    assert not (data / "bot.lock").exists()


def test_offline_fixture_approve_opens_paper_position(tmp_path, caplog):
    if in_funding_window(int(time.time() * 1000), 120):
        pytest.skip("펀딩 정산 창 — 신규 진입 금지 시간대")
    with caplog.at_level(logging.INFO):
        code, _, err, data = _run(tmp_path, "approve")
    assert code == 0, err
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    assert state["positions"], "approve 시나리오는 paper 포지션을 열어야 한다"
    entry_logs = [r for r in caplog.records if r.name == "tradingbot.notify" and "진입" in r.getMessage()]
    assert entry_logs


def test_offline_fixture_rejected_in_live(tmp_path):
    env = {"MODE": "live", "AGENT_BACKEND": "mock", "DATA_DIR": str(tmp_path / "d"), "CONFIRM_LIVE": "YES",
           "BINANCE_LIVE_API_KEY": "k" * 20, "BINANCE_LIVE_API_SECRET": "s" * 20}
    out, err = io.StringIO(), io.StringIO()
    try:
        load_settings(env)
    except Exception:  # noqa: BLE001 - 설정 단계에서 막히면 그것도 거부
        pytest.skip("live 설정 자체가 이 환경에서 거부됨")
    code = M.main(["run", "--once", "--offline-fixture"], env=env, out=out, err=err, input_fn=lambda p: "LIVE")
    assert code == M.EXIT_USAGE and "paper" in err.getvalue()
