"""tradingbot.dashboard 테스트 (SPEC §14). TestClient + tmp_path data_dir, 전부 오프라인."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradingbot.config import HardLimits, load_settings
from tradingbot.dashboard import create_app
from tradingbot.dashboard.app import (
    SortedJSONResponse,
    compute_equity,
    drawdown_level,
    summarize_costs,
    summarize_state,
)
from tradingbot.models import BotState, ClosedTrade, DailyStats, Position

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
MONTH = TODAY[:7]


# ---------------------------------------------------------------- 도우미
def make_settings(tmp_path: Path, **over):
    env = {"AGENT_BACKEND": "mock", "DATA_DIR": str(tmp_path), "LLM_DAILY_BUDGET_USD": "10", "LLM_MONTHLY_BUDGET_USD": "200"}
    env.update(over)
    return load_settings(env)


def make_state(**over) -> BotState:
    base = dict(
        mode="paper", cycle=42, paper_quote_balance=9_600.0, peak_equity=10_100.0,
        daily=DailyStats(date=TODAY, start_equity=10_000.0, realized_pnl=-12.5, trades=3, wins=1, losses=2, consecutive_losses=2, llm_cost_usd=0.42),
        positions={
            "BTC/USDT": Position(symbol="BTC/USDT", base_amount=0.005, entry_price=80_000.0, quote_spent=400.0, fee_quote=0.4,
                                 stop_loss_price=79_000.0, take_profit_price=82_000.0, opened_cycle=40, opened_ts=1_700_000_000_000,
                                 thesis="추세", confidence=0.7, entry_order_id="o1", last_mark=81_000.0),
        },
        closed_trades=[
            ClosedTrade(symbol="ETH/USDT", base_amount=0.1, entry_price=3000.0, exit_price=2970.0, quote_spent=300.0, quote_received=297.0,
                        fee_quote_total=0.6, pnl_quote=-3.6, pnl_pct=-0.012, reason="stop_loss", opened_cycle=30, closed_cycle=33,
                        opened_ts=1_699_000_000_000, closed_ts=1_699_003_600_000, thesis="t1", confidence=0.66),
            ClosedTrade(symbol="SOL/USDT", base_amount=2.0, entry_price=150.0, exit_price=154.0, quote_spent=300.0, quote_received=308.0,
                        fee_quote_total=0.6, pnl_quote=7.4, pnl_pct=0.0246, reason="take_profit", opened_cycle=35, closed_cycle=38,
                        opened_ts=1_699_100_000_000, closed_ts=1_699_103_600_000, thesis="t2", confidence=0.8),
            ClosedTrade(symbol="BNB/USDT", base_amount=1.0, entry_price=600.0, exit_price=590.0, quote_spent=600.0, quote_received=590.0,
                        fee_quote_total=1.2, pnl_quote=-11.2, pnl_pct=-0.0187, reason="time_stop", opened_cycle=20, closed_cycle=41,
                        opened_ts=1_699_200_000_000, closed_ts=1_699_300_000_000, thesis="t3", confidence=0.7),
        ],
        kill_switch=False, kill_reason=None, llm_paused_until_ts=None, llm_pause_reason=None, last_cycle_ts=1_700_000_900_000,
    )
    base.update(over)
    return BotState(**base)


def write_state(data_dir: Path, state: BotState) -> None:
    (data_dir / "state.json").write_text(json.dumps(state.model_dump(), sort_keys=True, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, sort_keys=True, ensure_ascii=False)) + "\n")


def write_cycle(data_dir: Path, date: str, n: int, payload: dict) -> Path:
    d = data_dir / "logs" / "cycles" / date
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"cycle_{n}.json"
    p.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return p


def read_token(data_dir: Path) -> str:
    return (data_dir / "control" / "dashboard_token").read_text(encoding="utf-8").strip()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def empty_client(data_dir: Path) -> TestClient:
    """파일이 하나도 없는 data_dir."""
    app = create_app(make_settings(data_dir), data_dir)
    return TestClient(app)


@pytest.fixture
def full_dir(data_dir: Path) -> Path:
    """상태·로그·비용·백테스트가 모두 있는 data_dir."""
    data_dir.mkdir(parents=True)
    write_state(data_dir, make_state())
    write_jsonl(data_dir / "logs" / "decisions.jsonl", [
        {"ts": 1, "cycle": 40, "symbol": "BTC/USDT", "action": "BUY", "confidence": 0.7, "roles": {"analyst": {"summary": "상승"}, "head": {"decision": "BUY"}}},
        "{broken json",
        {"ts": 2, "cycle": 41, "symbol": "ETH/USDT", "action": "HOLD"},
        {"ts": 3, "cycle": 42, "symbol": "SOL/USDT", "action": "HOLD", "한글": "값"},
    ])
    write_jsonl(data_dir / "logs" / "journal.jsonl", [
        {"ts": 10, "trade": {"symbol": "ETH/USDT", "pnl_quote": -3.6, "reason": "stop_loss"}, "review": {"lesson": "뉴스 확인"}},
        {"ts": 11, "trade": {"symbol": "SOL/USDT", "pnl_quote": 7.4, "reason": "take_profit"}, "review": None},
    ])
    write_cycle(data_dir, "20261027", 40, {"ts": 1_700_000_000_000, "cycle": 40, "equity": 10_000.0, "mode": "paper", "decisions": [1, 2]})
    write_cycle(data_dir, "20261027", 41, {"ts": 1_700_000_900_000, "cycle": 41, "equity": 10_020.0, "mode": "paper", "decisions": []})
    write_cycle(data_dir, "20261028", 42, {"ts": 1_700_001_800_000, "cycle": 42, "equity": 10_005.0, "mode": "paper", "decisions": [1], "errors": ["x"]})
    write_cycle(data_dir, "20261028", 9, {"ts": 1_700_001_700_000, "cycle": 9, "equity": 9_990.0})
    (data_dir / "logs" / "cycles" / "20261028" / "cycle_bad.json").write_text("{}", encoding="utf-8")
    (data_dir / "logs" / "cycles" / "notadate").mkdir()
    write_jsonl(data_dir / "cost_ledger.jsonl", [
        {"ts": "x", "date": TODAY, "role": "analyst", "model": "claude-haiku-5-5", "input_tokens": 1000, "cached_tokens": 200, "output_tokens": 100, "cost_usd": 0.01, "request_id": "r1"},
        {"ts": "x", "date": TODAY, "role": "head", "model": "claude-opus-5-5", "input_tokens": 2000, "cached_tokens": 0, "output_tokens": 300, "cost_usd": 0.5, "request_id": "r2"},
        {"ts": "x", "date": f"{MONTH}-01", "role": "head", "model": "claude-opus-5-5", "input_tokens": 100, "cached_tokens": 0, "output_tokens": 10, "cost_usd": 1.25, "request_id": "r3"},
        {"ts": "x", "date": "2000-01-01", "role": "head", "model": "claude-opus-5-5", "input_tokens": 100, "cached_tokens": 0, "output_tokens": 10, "cost_usd": 99.0, "request_id": "r4"},
        {"ts": "x", "date": TODAY, "role": "head", "model": "claude-opus-5-5", "cost_usd": "not-a-number"},
    ])
    bt = data_dir / "backtest_results"
    bt.mkdir()
    (bt / "run_1.json").write_text(json.dumps({
        "verdict": "VALIDATED", "symbols": ["BTC/USDT"], "net_return_pct": 0.12, "trades": [{"a": 1}, {"a": 2}],
        "profit_factor": 1.4, "walk_forward": [{"pf": 1.2}], "monte_carlo": {"mdd_p5": 0.05}, "nested": {"deep": {"x": 1}},
    }), encoding="utf-8")
    (bt / "run_2.json").write_text("not json", encoding="utf-8")
    (bt / "run_3.md").write_text("# md", encoding="utf-8")
    return data_dir


@pytest.fixture
def full_client(full_dir: Path) -> TestClient:
    return TestClient(create_app(make_settings(full_dir), full_dir))


# ---------------------------------------------------------------- create_app / 토큰
def test_create_app_generates_token(data_dir: Path):
    assert not data_dir.exists()
    app = create_app(make_settings(data_dir), data_dir)
    tok = read_token(data_dir)
    assert len(tok) >= 24
    assert app.state.data_dir == data_dir


def test_create_app_keeps_existing_token(data_dir: Path):
    (data_dir / "control").mkdir(parents=True)
    (data_dir / "control" / "dashboard_token").write_text("mytoken-abc\n", encoding="utf-8")
    create_app(make_settings(data_dir), data_dir)
    assert read_token(data_dir) == "mytoken-abc"


def test_create_app_regenerates_empty_token(data_dir: Path):
    (data_dir / "control").mkdir(parents=True)
    (data_dir / "control" / "dashboard_token").write_text("  \n", encoding="utf-8")
    create_app(make_settings(data_dir), data_dir)
    assert len(read_token(data_dir)) >= 24


def test_token_not_in_logs(data_dir: Path, caplog):
    with caplog.at_level(logging.DEBUG, logger="tradingbot.dashboard"):
        create_app(make_settings(data_dir), data_dir)
        tok = read_token(data_dir)
        client = TestClient(create_app(make_settings(data_dir), data_dir))
        client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": "wrong-token-value-123456"})
    text = caplog.text
    assert tok not in text
    assert "wrong-token-value-123456" not in text


def test_no_docs_endpoints(empty_client: TestClient):
    assert empty_client.get("/docs").status_code == 404
    assert empty_client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------- GET /
def test_index_html(empty_client: TestClient):
    r = empty_client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "<canvas" in html
    assert "X-Dashboard-Token" in html
    assert "/api/kill" in html
    assert "10000" in html  # 10초 폴링
    for ep in ("/api/state", "/api/positions", "/api/trades", "/api/decisions", "/api/cycles", "/api/costs", "/api/backtests", "/api/journal"):
        assert ep in html
    # 오프라인: 외부 스크립트/CDN 없음
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html


def test_index_sections_present():
    html = (Path(__file__).resolve().parents[1] / "tradingbot" / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    for word in ("포지션", "최근 결정", "거래 내역", "비용", "백테스트", "STOP", "킬스위치", "낙폭", "LLM"):
        assert word in html


# ---------------------------------------------------------------- 파일 없을 때: 전부 200, 빈 응답
@pytest.mark.parametrize("path", [
    "/api/health", "/api/state", "/api/positions", "/api/trades", "/api/decisions",
    "/api/cycles", "/api/costs", "/api/backtests", "/api/journal",
    "/api/trades?limit=5", "/api/decisions?limit=0", "/api/cycles?limit=1000",
])
def test_endpoints_without_files_return_200(empty_client: TestClient, path: str):
    r = empty_client.get(path)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")


def test_empty_responses_shape(empty_client: TestClient):
    assert empty_client.get("/api/state").json() == {"exists": False, "state": None, "summary": None, "stop_pending": False}
    assert empty_client.get("/api/positions").json() == {"positions": [], "count": 0}
    assert empty_client.get("/api/trades").json() == {"trades": [], "count": 0, "total": 0}
    assert empty_client.get("/api/decisions").json() == {"decisions": [], "count": 0}
    assert empty_client.get("/api/journal").json() == {"journal": [], "count": 0}
    assert empty_client.get("/api/cycles").json() == {"cycles": [], "count": 0, "total": 0}
    assert empty_client.get("/api/backtests").json() == {"backtests": [], "count": 0}
    costs = empty_client.get("/api/costs").json()
    assert costs["today_usd"] == 0.0 and costs["month_usd"] == 0.0 and costs["by_model"] == {}
    assert costs["daily_budget_usd"] == 10.0 and costs["monthly_budget_usd"] == 200.0


def test_health_without_files(empty_client: TestClient):
    h = empty_client.get("/api/health").json()
    assert h["ok"] is True
    assert h["mode"] == "paper"
    assert h["state_exists"] is False and h["state_valid"] is False
    assert h["cycle"] is None and h["last_cycle_ts"] is None and h["last_cycle_age_s"] is None
    assert h["stop_pending"] is False and h["clear_pending"] is False
    assert h["token_configured"] is True
    assert isinstance(h["ts"], int)


def test_cycle_detail_missing_is_404(empty_client: TestClient):
    assert empty_client.get("/api/cycles/20261028/1").status_code == 404
    assert empty_client.get("/api/cycles/not-a-date/1").status_code == 404
    assert empty_client.get("/api/cycles/20261028/abc").status_code == 422
    assert empty_client.get("/api/cycles/..%2F..%2Fetc/1").status_code == 404


def test_corrupt_state_is_not_500(data_dir: Path):
    data_dir.mkdir(parents=True)
    (data_dir / "state.json").write_text("{not json", encoding="utf-8")
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    assert c.get("/api/state").json()["exists"] is False
    assert c.get("/api/positions").json()["count"] == 0
    assert c.get("/api/trades").json()["count"] == 0
    h = c.get("/api/health").json()
    assert h["state_exists"] is True and h["state_valid"] is False


def test_invalid_state_schema_is_not_500(data_dir: Path):
    data_dir.mkdir(parents=True)
    (data_dir / "state.json").write_text(json.dumps({"mode": "paper", "unknown_field": 1}), encoding="utf-8")
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    assert c.get("/api/state").status_code == 200
    assert c.get("/api/state").json()["exists"] is False


def test_state_not_a_dict_is_not_500(data_dir: Path):
    data_dir.mkdir(parents=True)
    (data_dir / "state.json").write_text("[1,2,3]", encoding="utf-8")
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    assert c.get("/api/state").json()["exists"] is False


def test_corrupt_jsonl_and_stop_file_not_500(data_dir: Path):
    data_dir.mkdir(parents=True)
    write_jsonl(data_dir / "logs" / "decisions.jsonl", ["{bad", "[1,2]", "", json.dumps({"ok": 1})])
    write_jsonl(data_dir / "cost_ledger.jsonl", ["garbage", json.dumps({"date": TODAY, "cost_usd": float("nan")})])
    (data_dir / "control").mkdir()
    (data_dir / "control" / "STOP").write_text("not json", encoding="utf-8")
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    assert c.get("/api/decisions").json() == {"decisions": [{"ok": 1}], "count": 1}
    assert c.get("/api/costs").json()["today_usd"] == 0.0
    h = c.get("/api/health").json()
    assert h["stop_pending"] is True
    assert c.get("/api/state").json()["stop_pending"] is True


# ---------------------------------------------------------------- 파일 있을 때
def test_health_with_state(full_client: TestClient):
    h = full_client.get("/api/health").json()
    assert h["state_exists"] is True and h["state_valid"] is True
    assert h["cycle"] == 42
    assert h["last_cycle_ts"] == 1_700_000_900_000
    assert h["last_cycle_age_s"] >= 0
    assert h["kill_switch"] is False
    assert h["cycle_minutes"] == 15


def test_state_endpoint(full_client: TestClient):
    r = full_client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    assert BotState.model_validate(body["state"]).cycle == 42
    s = body["summary"]
    # 자본 = 현금 + 0.005 * 81000(마크)
    assert s["equity"] == pytest.approx(9_600.0 + 0.005 * 81_000.0)
    assert s["cash"] == 9_600.0
    assert s["mode"] == "paper" and s["cycle"] == 42
    assert s["peak_equity"] == 10_100.0
    assert s["drawdown_pct"] == pytest.approx((10_100.0 - 10_005.0) / 10_100.0)
    assert s["drawdown_level"] == "none"
    assert s["daily_realized_pnl"] == -12.5
    assert s["daily_unrealized_pnl"] == pytest.approx(0.005 * 1000.0)
    # 당일 손익 = 자본 - 당일 시작자본 (10,005 - 10,000). 실현+전체 미실현을 더하면 전날 미실현이 이중 계산된다.
    assert s["daily_pnl"] == pytest.approx(10_005.0 - 10_000.0)
    assert s["daily_pnl_pct"] == pytest.approx(5.0 / 10_000.0)
    assert s["positions_count"] == 1 and s["max_positions"] == HardLimits.MAX_POSITIONS
    assert s["kill_switch"] is False and s["llm_paused"] is False
    assert s["consecutive_losses"] == 2 and s["daily_trades"] == 3
    assert s["last_cycle_age_s"] >= 0


def test_state_json_is_sorted_and_unicode(full_client: TestClient):
    raw = full_client.get("/api/decisions").content.decode("utf-8")
    assert "한글" in raw and "\\ud55c" not in raw  # ensure_ascii=False
    body = json.loads(raw)
    keys = list(body.keys())
    assert keys == sorted(keys)
    first = body["decisions"][0]
    assert list(first.keys()) == sorted(first.keys())


def test_state_flags(data_dir: Path):
    data_dir.mkdir(parents=True)
    far = 4_000_000_000_000  # 먼 미래
    write_state(data_dir, make_state(kill_switch=True, kill_reason="drawdown", llm_paused_until_ts=far, llm_pause_reason="budget"))
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    s = c.get("/api/state").json()["summary"]
    assert s["kill_switch"] is True and s["kill_reason"] == "drawdown"
    assert s["llm_paused"] is True and s["llm_pause_reason"] == "budget" and s["llm_paused_until_ts"] == far
    assert c.get("/api/health").json()["kill_switch"] is True


def test_llm_pause_expired(data_dir: Path):
    data_dir.mkdir(parents=True)
    write_state(data_dir, make_state(llm_paused_until_ts=1_000, llm_pause_reason="old"))
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    assert c.get("/api/state").json()["summary"]["llm_paused"] is False


def test_positions_endpoint(full_client: TestClient):
    body = full_client.get("/api/positions").json()
    assert body["count"] == 1 and body["max_positions"] == HardLimits.MAX_POSITIONS
    p = body["positions"][0]
    assert p["symbol"] == "BTC/USDT"
    assert p["mark"] == 81_000.0
    assert p["value_quote"] == pytest.approx(405.0)
    assert p["unrealized_pnl"] == pytest.approx(5.0)
    assert p["unrealized_pnl_pct"] == pytest.approx(5.0 / 400.0)
    assert p["held_seconds"] >= 0
    assert p["stop_loss_price"] == 79_000.0 and p["thesis"] == "추세"


def test_positions_without_mark_use_entry(data_dir: Path):
    data_dir.mkdir(parents=True)
    st = make_state()
    st.positions["BTC/USDT"].last_mark = None
    write_state(data_dir, st)
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    p = c.get("/api/positions").json()["positions"][0]
    assert p["mark"] == 80_000.0
    assert p["unrealized_pnl"] == pytest.approx(400.0 - 400.0)


def test_trades_endpoint_newest_first_and_limit(full_client: TestClient):
    body = full_client.get("/api/trades").json()
    assert body["total"] == 3 and body["count"] == 3
    assert [t["symbol"] for t in body["trades"]] == ["BNB/USDT", "SOL/USDT", "ETH/USDT"]
    assert ClosedTrade.model_validate(body["trades"][0]).reason == "time_stop"
    body = full_client.get("/api/trades?limit=2").json()
    assert body["count"] == 2 and body["total"] == 3
    assert [t["symbol"] for t in body["trades"]] == ["BNB/USDT", "SOL/USDT"]
    assert full_client.get("/api/trades?limit=0").json()["trades"] == []


def test_trades_limit_validation(full_client: TestClient):
    assert full_client.get("/api/trades?limit=-1").status_code == 422
    assert full_client.get("/api/trades?limit=abc").status_code == 422
    assert full_client.get("/api/trades?limit=100000").status_code == 422


def test_decisions_endpoint(full_client: TestClient):
    body = full_client.get("/api/decisions").json()
    assert body["count"] == 3  # 깨진 줄 제외
    assert [d["cycle"] for d in body["decisions"]] == [42, 41, 40]  # 최신 먼저
    assert body["decisions"][2]["roles"]["head"]["decision"] == "BUY"
    body = full_client.get("/api/decisions?limit=1").json()
    assert body["count"] == 1 and body["decisions"][0]["cycle"] == 42
    assert full_client.get("/api/decisions?limit=0").json()["count"] == 0


def test_journal_endpoint(full_client: TestClient):
    body = full_client.get("/api/journal").json()
    assert body["count"] == 2
    assert body["journal"][0]["ts"] == 11
    assert body["journal"][1]["review"] == {"lesson": "뉴스 확인"}
    assert full_client.get("/api/journal?limit=1").json()["journal"][0]["ts"] == 11


def test_cycles_list(full_client: TestClient):
    body = full_client.get("/api/cycles").json()
    assert body["total"] == 4 and body["count"] == 4
    assert [(c["date"], c["cycle"]) for c in body["cycles"]] == [
        ("20261028", 42), ("20261028", 9), ("20261027", 41), ("20261027", 40),
    ]
    first = body["cycles"][0]
    assert first["equity"] == 10_005.0 and first["ts"] == 1_700_001_800_000 and first["mode"] == "paper"
    assert first["decisions"] == 1 and first["errors"] == 1
    assert body["cycles"][1]["decisions"] is None and body["cycles"][1]["mode"] is None
    body = full_client.get("/api/cycles?limit=2").json()
    assert body["count"] == 2 and body["total"] == 4
    assert full_client.get("/api/cycles?limit=0").json()["cycles"] == []


def test_cycle_detail(full_client: TestClient):
    r = full_client.get("/api/cycles/20261027/40")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "20261027" and body["cycle"] == 40
    assert body["payload"]["equity"] == 10_000.0 and body["payload"]["decisions"] == [1, 2]
    assert full_client.get("/api/cycles/20261027/99").status_code == 404
    assert full_client.get("/api/cycles/20261027/-1").status_code == 404


def test_cycle_detail_corrupt_json_is_404(full_dir: Path):
    (full_dir / "logs" / "cycles" / "20261027" / "cycle_7.json").write_text("{bad", encoding="utf-8")
    c = TestClient(create_app(make_settings(full_dir), full_dir))
    assert c.get("/api/cycles/20261027/7").status_code == 404
    # 목록에는 나오되 요약값은 None
    row = [x for x in c.get("/api/cycles").json()["cycles"] if x["cycle"] == 7][0]
    assert row["equity"] is None


def test_costs_endpoint(full_client: TestClient):
    c = full_client.get("/api/costs").json()
    assert c["date"] == TODAY and c["month"] == MONTH
    assert c["today_usd"] == pytest.approx(0.51)
    assert c["month_usd"] == pytest.approx(0.51 + 1.25)
    assert c["calls_today"] == 2 and c["calls_month"] == 3
    assert c["daily_budget_usd"] == 10.0 and c["monthly_budget_usd"] == 200.0
    assert c["today_budget_pct"] == pytest.approx(0.051)
    assert c["month_budget_pct"] == pytest.approx(1.76 / 200.0)
    assert c["remaining_today_usd"] == pytest.approx(10.0 - 0.51)
    assert set(c["by_model"]) == {"claude-haiku-5-5", "claude-opus-5-5"}
    opus = c["by_model"]["claude-opus-5-5"]
    assert opus["calls"] == 2 and opus["cost_usd"] == pytest.approx(1.75)
    assert opus["input_tokens"] == 2100 and opus["output_tokens"] == 310
    assert c["by_model_today"]["claude-opus-5-5"]["cost_usd"] == pytest.approx(0.5)
    assert c["by_model_today"]["claude-haiku-5-5"]["cached_tokens"] == 200
    assert c["by_role"]["head"]["calls"] == 2 and c["by_role"]["analyst"]["calls"] == 1


def test_costs_over_budget_pct(data_dir: Path):
    data_dir.mkdir(parents=True)
    write_jsonl(data_dir / "cost_ledger.jsonl", [{"date": TODAY, "model": "m", "role": "r", "cost_usd": 25.0}])
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    body = c.get("/api/costs").json()
    assert body["today_budget_pct"] == pytest.approx(2.5)
    assert body["remaining_today_usd"] == 0.0


def test_backtests_endpoint(full_client: TestClient):
    body = full_client.get("/api/backtests").json()
    assert body["count"] == 1  # 깨진 json·md 제외
    b = body["backtests"][0]
    assert b["name"] == "run_1.json"
    assert isinstance(b["mtime_ms"], int)
    s = b["summary"]
    assert s["verdict"] == "VALIDATED" and s["symbols"] == ["BTC/USDT"]
    assert s["net_return_pct"] == 0.12 and s["profit_factor"] == 1.4
    assert s["trades_count"] == 2 and "trades" not in s  # 거래 목록은 제외
    assert s["monte_carlo"] == {"mdd_p5": 0.05}
    assert "walk_forward" not in s or isinstance(s["walk_forward"], (list, dict))
    assert "nested" not in s


# ---------------------------------------------------------------- POST /api/kill
def test_kill_requires_token(empty_client: TestClient, data_dir: Path):
    r = empty_client.post("/api/kill", json={"reason": "x"})
    assert r.status_code == 401
    assert not (data_dir / "control" / "STOP").exists()


def test_kill_wrong_token(empty_client: TestClient, data_dir: Path):
    r = empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": "nope"})
    assert r.status_code == 401
    assert not (data_dir / "control" / "STOP").exists()
    # 접두사만 같은 토큰도 거부
    tok = read_token(data_dir)
    r = empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": tok[:-1]})
    assert r.status_code == 401
    r = empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": tok + "x"})
    assert r.status_code == 401
    assert not (data_dir / "control" / "STOP").exists()


def test_kill_right_token_writes_stop(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    r = empty_client.post("/api/kill", json={"reason": "수동 정지"}, headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reason"] == "수동 정지" and isinstance(body["ts"], int)
    stop = data_dir / "control" / "STOP"
    assert stop.is_file()
    data = json.loads(stop.read_text(encoding="utf-8"))
    assert data == {"reason": "수동 정지", "ts": body["ts"]}
    assert not (stop.with_name("STOP.tmp")).exists()
    # 이후 health/state는 stop_pending
    assert empty_client.get("/api/health").json()["stop_pending"] is True
    assert empty_client.get("/api/state").json()["stop_pending"] is True


def test_kill_token_with_whitespace_and_default_reason(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    r = empty_client.post("/api/kill", headers={"X-Dashboard-Token": f"  {tok} \t"})
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "dashboard"
    data = json.loads((data_dir / "control" / "STOP").read_text(encoding="utf-8"))
    assert data["reason"] == "dashboard"


def test_kill_reason_truncated_and_non_string(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    r = empty_client.post("/api/kill", json={"reason": "a" * 2000}, headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200 and len(r.json()["reason"]) == 500
    r = empty_client.post("/api/kill", json={"reason": 123}, headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200 and r.json()["reason"] == "123"
    # 객체가 아닌 본문은 422 (STOP 파일은 바뀌지 않음)
    r = empty_client.post("/api/kill", json=["list"], headers={"X-Dashboard-Token": tok})
    assert r.status_code == 422
    assert json.loads((data_dir / "control" / "STOP").read_text(encoding="utf-8"))["reason"] == "123"
    # 본문 없이도 동작
    r = empty_client.post("/api/kill", headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200 and r.json()["reason"] == "dashboard"


def test_kill_removes_pending_clear(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    (data_dir / "control" / "CLEAR").write_text("{}", encoding="utf-8")
    assert empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": tok}).status_code == 200
    assert not (data_dir / "control" / "CLEAR").exists()


def test_kill_rotated_token_file_is_reread(empty_client: TestClient, data_dir: Path):
    old = read_token(data_dir)
    (data_dir / "control" / "dashboard_token").write_text("rotated-token-value\n", encoding="utf-8")
    assert empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": old}).status_code == 401
    assert empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": "rotated-token-value"}).status_code == 200


def test_kill_missing_token_file_is_503(empty_client: TestClient, data_dir: Path):
    (data_dir / "control" / "dashboard_token").unlink()
    r = empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": "anything"})
    assert r.status_code == 503
    assert empty_client.get("/api/health").json()["token_configured"] is False


def test_kill_overwrites_existing_stop(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    empty_client.post("/api/kill", json={"reason": "first"}, headers={"X-Dashboard-Token": tok})
    empty_client.post("/api/kill", json={"reason": "second"}, headers={"X-Dashboard-Token": tok})
    assert json.loads((data_dir / "control" / "STOP").read_text(encoding="utf-8"))["reason"] == "second"


# ---------------------------------------------------------------- POST /api/kill/clear
def test_kill_clear_requires_token(empty_client: TestClient, data_dir: Path):
    (data_dir / "control" / "STOP").write_text('{"reason":"x","ts":1}', encoding="utf-8")
    assert empty_client.post("/api/kill/clear").status_code == 401
    assert empty_client.post("/api/kill/clear", headers={"X-Dashboard-Token": "bad"}).status_code == 401
    assert (data_dir / "control" / "STOP").exists()
    assert not (data_dir / "control" / "CLEAR").exists()


def test_kill_clear_removes_stop_and_writes_clear(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    assert empty_client.post("/api/kill", json={"reason": "x"}, headers={"X-Dashboard-Token": tok}).status_code == 200
    r = empty_client.post("/api/kill/clear", json={"reason": "해제"}, headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["stop_removed"] is True and body["reason"] == "해제"
    assert not (data_dir / "control" / "STOP").exists()
    clear = data_dir / "control" / "CLEAR"
    assert clear.is_file()
    data = json.loads(clear.read_text(encoding="utf-8"))
    assert data == {"reason": "해제", "ts": body["ts"]}
    h = empty_client.get("/api/health").json()
    assert h["stop_pending"] is False and h["clear_pending"] is True


def test_kill_clear_without_stop(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    r = empty_client.post("/api/kill/clear", headers={"X-Dashboard-Token": tok})
    assert r.status_code == 200
    assert r.json()["stop_removed"] is False and r.json()["reason"] == "dashboard"
    assert (data_dir / "control" / "CLEAR").is_file()


def test_kill_then_clear_then_kill_cycle(empty_client: TestClient, data_dir: Path):
    tok = read_token(data_dir)
    h = {"X-Dashboard-Token": tok}
    empty_client.post("/api/kill", json={"reason": "1"}, headers=h)
    empty_client.post("/api/kill/clear", headers=h)
    empty_client.post("/api/kill", json={"reason": "2"}, headers=h)
    assert (data_dir / "control" / "STOP").exists()
    assert not (data_dir / "control" / "CLEAR").exists()


def test_get_on_kill_is_not_allowed(empty_client: TestClient):
    assert empty_client.get("/api/kill").status_code == 405
    assert empty_client.get("/api/kill/clear").status_code == 405


# ---------------------------------------------------------------- 순수 함수
def test_drawdown_level_thresholds():
    assert drawdown_level(0.0) == "none"
    assert drawdown_level(HardLimits.DRAWDOWN_CAUTION - 1e-9) == "none"
    assert drawdown_level(HardLimits.DRAWDOWN_CAUTION) == "caution"
    assert drawdown_level(HardLimits.DRAWDOWN_BLOCK) == "block"
    assert drawdown_level(HardLimits.DRAWDOWN_STOP_ALL) == "stop_all"
    assert drawdown_level(0.5) == "stop_all"


def test_compute_equity_and_summary_edge_cases():
    st = make_state(positions={}, peak_equity=0.0, daily=DailyStats(date=TODAY, start_equity=0.0))
    assert compute_equity(st) == 9_600.0
    s = summarize_state(st, now_ms=1_700_000_900_000 + 60_000)
    assert s["drawdown_pct"] == 0.0 and s["drawdown_level"] == "none"
    assert s["daily_pnl_pct"] == 0.0
    assert s["peak_equity"] == 9_600.0
    assert s["last_cycle_age_s"] == 60


def test_summary_drawdown_levels():
    # 현금 9400, 포지션 없음, 최고 10000 -> 낙폭 6% -> stop_all
    st = make_state(positions={}, paper_quote_balance=9_400.0, peak_equity=10_000.0)
    assert summarize_state(st)["drawdown_level"] == "stop_all"
    st = make_state(positions={}, paper_quote_balance=9_650.0, peak_equity=10_000.0)
    assert summarize_state(st)["drawdown_level"] == "caution"
    st = make_state(positions={}, paper_quote_balance=9_540.0, peak_equity=10_000.0)
    assert summarize_state(st)["drawdown_level"] == "block"
    # 자본이 최고치를 넘으면 낙폭 0, peak은 현재 자본
    st = make_state(positions={}, paper_quote_balance=12_000.0, peak_equity=10_000.0)
    s = summarize_state(st)
    assert s["drawdown_pct"] == 0.0 and s["peak_equity"] == 12_000.0


def test_summarize_costs_pure(tmp_path: Path):
    settings = make_settings(tmp_path)
    recs = [
        {"date": "2026-10-28", "model": "a", "role": "x", "cost_usd": 1.0, "input_tokens": "bad"},
        {"date": "2026-10-02", "model": "a", "role": "x", "cost_usd": 2.0},
        {"date": "2026-09-30", "model": "b", "role": "y", "cost_usd": 4.0},
        {"model": "b", "cost_usd": 8.0},  # 날짜 없음 -> 무시
        {"date": "2026-10-28", "model": "a", "role": "x", "cost_usd": float("inf")},
    ]
    out = summarize_costs(recs, "2026-10-28", settings)
    assert out["today_usd"] == 1.0 and out["month_usd"] == 3.0
    assert out["by_model"]["a"]["calls"] == 2 and out["by_model"]["a"]["input_tokens"] == 0
    assert "b" not in out["by_model"]


def test_sorted_json_response_nan_and_keys():
    body = SortedJSONResponse({"b": float("nan"), "a": [float("inf"), 1.5], "c": {"z": Path("/x"), "y": "한"}}).body.decode()
    assert body == '{"a":[null,1.5],"b":null,"c":{"y":"한","z":"/x"}}'


def test_settings_secrets_not_exposed(data_dir: Path):
    """응답 어디에도 비밀값이 나오지 않는다."""
    settings = make_settings(data_dir, TELEGRAM_BOT_TOKEN="123456:SECRET-TELEGRAM-TOKEN", TELEGRAM_CHAT_ID="42")
    c = TestClient(create_app(settings, data_dir))
    for path in ("/", "/api/health", "/api/state", "/api/costs"):
        assert "SECRET-TELEGRAM-TOKEN" not in c.get(path).text


def test_live_mode_summary_uses_latest_cycle_report_equity(tmp_path: Path):
    """testnet/live: 현금은 거래소에 있으므로 자본은 최신 사이클 보고서에서, 없으면 None/unknown."""
    data_dir = tmp_path / "live"
    data_dir.mkdir()
    st = make_state(mode="live", paper_quote_balance=0.0, peak_equity=10_100.0)
    write_state(data_dir, st)
    c = TestClient(create_app(make_settings(data_dir), data_dir))
    s = c.get("/api/state").json()["summary"]
    assert s["equity"] is None and s["cash"] is None and s["drawdown_pct"] is None
    assert s["drawdown_level"] == "unknown"
    assert s["daily_pnl"] == -12.5  # 자본을 모르면 실현손익만
    write_cycle(data_dir, "20261028", 42, {"ts": 1_700_001_800_000, "cycle": 42, "equity": 9_900.0, "mode": "live"})
    s = c.get("/api/state").json()["summary"]
    assert s["equity"] == 9_900.0 and s["cash"] is None
    assert s["drawdown_pct"] == pytest.approx(200.0 / 10_100.0)
    assert s["drawdown_level"] == "none"
    assert s["daily_pnl"] == pytest.approx(-100.0)
