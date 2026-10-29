"""System review — operations lens (CLI, loop, dashboard, backtest, docs).

Each test here is a proof of a defect: it fails against the current code and should pass once fixed.
All tests are offline.
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

from tradingbot import backtest as BT
from tradingbot import main as M
from tradingbot.config import load_settings
from tradingbot.exchange import CcxtMarketData
from tradingbot.models import BotState, Candle, DailyStats
from tradingbot.portfolio import Portfolio

SECRET = "LiveSecretKeyValue-ABCDEFG123456"


# ----------------------------------------------------------------------------- helpers
def _env(tmp_path: Path, **extra) -> dict:
    env = {"AGENT_BACKEND": "mock", "MODE": "paper", "DATA_DIR": str(tmp_path / "data"), "SYMBOLS": "ETH/USDT"}
    env.update(extra)
    return env


class _Notifier:
    def __init__(self) -> None:
        self.events: list = []

    def notify(self, event) -> None:
        self.events.append(event)

    def flush(self, timeout_s: float = 5.0) -> None:
        pass


class _Summary:
    def __init__(self, n: int) -> None:
        self.cycle = n
        self.errors: list[str] = []


class _Cycle:
    def __init__(self) -> None:
        self.n = 0

    def run_once(self):
        self.n += 1
        return _Summary(self.n)


class _Portfolio:
    def __init__(self, settings) -> None:
        self.state = BotState(mode=settings.mode, cycle=0, positions={}, paper_quote_balance=10_000.0,
                              peak_equity=10_000.0, daily=DailyStats(date="2026-10-28", start_equity=10_000.0))

    def equity(self, marks, quote_balance=None):
        return self.state.paper_quote_balance


class _Factory:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs: dict = {}

    def __call__(self, settings, *, lock=None, **kw):
        self.calls += 1
        self.kwargs = dict(kw)
        return M.Components(settings=settings, market=None, broker=None, portfolio=_Portfolio(settings),
                            backend=None, ledger=None, notifier=_Notifier(), skills=None, cycle=_Cycle())


# ----------------------------------------------------------------------------- live confirmation
def test_live_confirmation_refuses_non_interactive_stdin(tmp_path, monkeypatch):
    """MODE=live + CONFIRM_LIVE=YES + keys, and 'LIVE' arrives on a *pipe* (e.g. ``echo LIVE | ... run``).

    The third factor is meant to be a human typing at the console; a non-interactive stdin must be refused,
    otherwise the confirmation is scriptable and no longer a human-presence check.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO("LIVE\n"))   # not a TTY
    env = _env(tmp_path, MODE="live", CONFIRM_LIVE="YES", BINANCE_LIVE_API_KEY=SECRET,
               BINANCE_LIVE_API_SECRET=SECRET + "x")
    fac = _Factory()
    out, err = io.StringIO(), io.StringIO()
    code = M.main(["run", "--once"], env=env, out=out, err=err, components_factory=fac,
                  stop_event=threading.Event())
    assert code == M.EXIT_REFUSED
    assert fac.calls == 0


# ----------------------------------------------------------------------------- .env loading
def test_mock_scenario_from_dotenv_is_honoured(tmp_path, monkeypatch):
    """``.env.example`` documents ``MOCK_SCENARIO`` as a .env setting, and load_settings merges .env under
    os.environ. ``main`` however reads MOCK_SCENARIO from ``os.environ`` only, so a value set in .env is
    silently ignored (the run falls back to the conservative scenario)."""
    for k in ("MODE", "AGENT_BACKEND", "MOCK_SCENARIO", "DATA_DIR", "CONFIRM_LIVE", "SYMBOLS", "CYCLE_MINUTES",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text(
        "MODE=paper\nAGENT_BACKEND=mock\nMOCK_SCENARIO=approve\n"
        f"DATA_DIR={tmp_path / 'data'}\nSYMBOLS=ETH/USDT\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    fac = _Factory()
    out, err = io.StringIO(), io.StringIO()
    code = M.main(["run", "--once", "--offline-fixture"], out=out, err=err, components_factory=fac,
                  stop_event=threading.Event())
    assert code == M.EXIT_OK, err.getvalue()
    assert fac.kwargs.get("mock_scenario") == "approve"


# ----------------------------------------------------------------------------- offline fixture isolation
def test_offline_fixture_does_not_pollute_real_paper_state(tmp_path):
    """``--offline-fixture`` (synthetic prices) writes into the same DATA_DIR files as a real paper run:
    synthetic positions/cycle counter land in ``state.json`` and synthetic OI/funding history lands in
    ``cache/scanner_history.json``. The next real ``run`` then marks fake-priced positions at real prices
    and compares real OI against the fake history. The real paper state must be left untouched."""
    data = tmp_path / "data"
    env = _env(tmp_path, SYMBOLS="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT", MOCK_SCENARIO="approve")
    settings = load_settings(env)
    # an existing real paper portfolio (cycle 5, no positions)
    pf = Portfolio.load(settings, data / "state.json")
    pf.state.cycle = 5
    pf.save()
    before = json.loads((data / "state.json").read_text(encoding="utf-8"))

    out, err = io.StringIO(), io.StringIO()
    code = M.main(["run", "--once", "--offline-fixture"], env=env, out=out, err=err,
                  stop_event=threading.Event())
    assert code == M.EXIT_OK, err.getvalue()

    after = json.loads((data / "state.json").read_text(encoding="utf-8"))
    assert after["cycle"] == before["cycle"], "fixture run advanced the real paper cycle counter"
    assert after["positions"] == before["positions"], "fixture run opened synthetic positions in the real state"
    assert not (data / "cache" / "scanner_history.json").exists(), \
        "synthetic flow history persisted into the real scanner cache"


# ----------------------------------------------------------------------------- backtest data fetch
class _BinanceLikeExchange:
    """Mimics ccxt binance spot fetch_ohlcv: ``limit`` is clamped to 1000 (ccxt: maxLimit = 1000 for spot);
    with ``since=None`` it returns the most recent bars; ``since`` pages forward."""

    TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms
        self.options: dict = {}
        self.calls: list[tuple] = []

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self.calls.append((symbol, timeframe, since, limit))
        tf = self.TF_MS[timeframe]
        lim = min(int(limit or 500), 1000)
        last = (self.now_ms // tf) * tf            # the still-open bar
        start = last - (lim - 1) * tf if since is None else (int(since) // tf) * tf
        rows = []
        t = start
        while t <= last and len(rows) < lim:
            rows.append([t, 100.0, 101.0, 99.0, 100.5, 10.0])
            t += tf
        return rows


def test_backtest_fetch_gets_requested_days_from_binance_limit(tmp_path):
    """``main.py backtest --days 90`` (README and CLI default) asks CcxtMarketData.fetch_ohlcv for
    90*96+300 15m bars in a single call; Binance/ccxt clamp spot klines to 1000 per request and the code never
    paginates, so the 'validator' only ever sees ~10 days and always reports NOT VALIDATED for coverage."""
    now = int(time.time() * 1000)
    md = CcxtMarketData(exchange=_BinanceLikeExchange(now), sleep=lambda s: None, clock_ms=lambda: now)
    cfg = BT.BacktestConfig(symbols=["BTC/USDT"], days=90, cache_dir=tmp_path / "cache", out_dir=tmp_path / "out")
    candles = BT._fetch_candles(cfg, md)
    base = candles["BTC/USDT"]["15m"]
    assert len(base) >= 90 * 96, f"only {len(base)} 15m bars (~{len(base) / 96:.1f} days) for --days 90"


def test_backtest_candle_cache_is_not_reused_when_stale(tmp_path):
    """The candle cache key is ``<symbol>_<tf>_<limit>.json`` with no end date and no freshness check:
    a backtest re-run weeks later with the same --days/--symbols silently reuses the old window."""
    tf = 900_000
    now = int(time.time() * 1000)

    class _Market:
        def __init__(self, end_ms: int) -> None:
            self.end = (end_ms // tf) * tf

        def fetch_ohlcv(self, symbol, timeframe, limit):
            step = _BinanceLikeExchange.TF_MS[timeframe]
            end = (self.end // step) * step
            return [Candle(ts=end - (limit - 1 - i) * step, open=100.0, high=101.0, low=99.0, close=100.5,
                           volume=10.0) for i in range(limit)]

    cfg = BT.BacktestConfig(symbols=["BTC/USDT"], days=2, cache_dir=tmp_path / "cache", out_dir=tmp_path / "out")
    BT._fetch_candles(cfg, _Market(now - 30 * 86_400_000))          # a run made 30 days ago
    fresh = BT._fetch_candles(cfg, _Market(now))                      # re-run today
    last_ts = fresh["BTC/USDT"]["15m"][-1].ts
    assert last_ts >= now - 2 * tf, \
        f"re-run used cached candles ending {(now - last_ts) / 86_400_000:.1f} days ago"
