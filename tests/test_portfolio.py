"""portfolio.py 테스트 (SPEC §10): 원자 저장·손상 정책·부분 체결·쿨다운·날짜 롤오버·킬/일시정지."""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingbot import portfolio as pf_mod
from tradingbot.config import HardLimits, load_settings
from tradingbot.models import BotState, ClosedTrade, DailyStats, Fill, Position
from tradingbot.portfolio import PARTIAL_FILL_TOLERANCE, SAVE_RETRIES, CorruptStateError, Portfolio

UTC = timezone.utc
TOKEN = "123456789:AAFakeTelegramTokenXYZ"


# ---------------------------------------------------------------- 도우미
def make_settings(mode: str = "paper", **extra: str):
    env = {"AGENT_BACKEND": "mock", "MODE": mode, "PAPER_START_BALANCE_USDT": "10000"}
    if mode == "testnet":
        env.update(BINANCE_TESTNET_API_KEY="tkey-abcdef", BINANCE_TESTNET_API_SECRET="tsec-abcdef")
    elif mode == "live":
        env.update(CONFIRM_LIVE="YES", BINANCE_LIVE_API_KEY="lkey-abcdef", BINANCE_LIVE_API_SECRET="lsec-abcdef")
    env.update(extra)
    return load_settings(env)


def buy_fill(symbol: str = "BTC/USDT", base: float = 0.01, price: float = 50_000.0, fee: float = 0.5, ts: int = 1_000) -> Fill:
    return Fill(order_id=f"b-{symbol}", symbol=symbol, side="buy", base_amount=base, price=price,
                quote_amount=base * price, fee_quote=fee, ts=ts)


def sell_fill(symbol: str = "BTC/USDT", base: float = 0.01, price: float = 51_000.0, fee: float = 0.5, ts: int = 2_000) -> Fill:
    return Fill(order_id=f"s-{symbol}", symbol=symbol, side="sell", base_amount=base, price=price,
                quote_amount=base * price, fee_quote=fee, ts=ts)


@pytest.fixture
def paper(tmp_path: Path) -> Portfolio:
    pf = Portfolio(make_settings("paper"), tmp_path / "state.json")
    pf.sleep = lambda s: None
    return pf


@pytest.fixture
def testnet(tmp_path: Path) -> Portfolio:
    pf = Portfolio(make_settings("testnet"), tmp_path / "state.json")
    pf.sleep = lambda s: None
    return pf


def populated(pf: Portfolio) -> Portfolio:
    """포지션·거래·쿨다운·킬·일시정지가 모두 든 상태."""
    pf.open_position(buy_fill("BTC/USDT"), 49_000.0, 52_000.0, "추세 추종", 0.8, cycle=3)
    pf.open_position(buy_fill("ETH/USDT", base=0.5, price=3_000.0), 2_940.0, 3_120.0, "반등", 0.7, cycle=4)
    pf.close_position("ETH/USDT", sell_fill("ETH/USDT", base=0.5, price=3_100.0), "take_profit", cycle=6)
    pf.state.cycle = 7
    pf.set_kill("테스트 킬")
    pf.pause_llm(123_456, "auth")
    pf.state.last_cycle_ts = 999
    pf.update_marks({"BTC/USDT": 50_500.0})
    pf.update_peak(10_050.0)
    return pf


# ---------------------------------------------------------------- 새 상태
class TestFreshState:
    def test_missing_file_paper(self, tmp_path: Path):
        pf = Portfolio.load(make_settings("paper"), tmp_path / "state.json")
        s = pf.state
        assert s.mode == "paper"
        assert s.cycle == 0
        assert s.paper_quote_balance == 10_000.0
        assert s.peak_equity == 10_000.0
        assert s.daily.start_equity == 10_000.0
        assert s.daily.date == datetime.now(UTC).date().isoformat()
        assert s.positions == {} and s.closed_trades == []
        assert not s.kill_switch and s.llm_paused_until_ts is None
        assert not (tmp_path / "state.json").exists()  # load는 저장하지 않는다

    def test_missing_file_testnet_has_no_paper_balance(self, tmp_path: Path):
        pf = Portfolio.load(make_settings("testnet"), tmp_path / "state.json")
        assert pf.state.mode == "testnet"
        assert pf.state.paper_quote_balance == 0.0
        assert pf.state.daily.start_equity == 0.0

    def test_mode_property_and_repr_without_secrets(self, tmp_path: Path):
        settings = make_settings("paper", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")
        pf = Portfolio(settings, tmp_path / "state.json")
        assert pf.mode == "paper"
        text = repr(pf)
        assert "Portfolio(" in text and "paper" in text
        assert TOKEN not in text and "AAFake" not in text


# ---------------------------------------------------------------- 저장/로드
class TestSaveLoad:
    def test_round_trip_equality(self, paper: Portfolio):
        populated(paper)
        paper.save()
        loaded = Portfolio.load(make_settings("paper"), paper.path)
        assert loaded.state == paper.state
        assert loaded.state.model_dump() == paper.state.model_dump()
        assert isinstance(loaded.state.positions["BTC/USDT"], Position)
        assert isinstance(loaded.state.closed_trades[0], ClosedTrade)

    def test_saved_json_is_deterministic_and_sorted(self, paper: Portfolio):
        populated(paper)
        paper.save()
        first = paper.path.read_bytes()
        paper.save()
        assert paper.path.read_bytes() == first
        data = json.loads(first.decode("utf-8"))
        assert list(data.keys()) == sorted(data.keys())
        assert "추세 추종" in first.decode("utf-8")  # ensure_ascii=False
        assert data["version"] == 1 and data["mode"] == "paper"

    def test_save_creates_parent_and_leaves_no_tmp(self, tmp_path: Path):
        pf = Portfolio(make_settings("paper"), tmp_path / "nested" / "dir" / "state.json")
        pf.save()
        assert pf.path.exists()
        assert [p.name for p in pf.path.parent.iterdir()] == ["state.json"]

    def test_save_overwrites_existing(self, paper: Portfolio):
        paper.save()
        paper.state.cycle = 42
        paper.save()
        assert json.loads(paper.path.read_text(encoding="utf-8"))["cycle"] == 42

    def test_save_retries_then_succeeds(self, paper: Portfolio, monkeypatch, caplog):
        real_replace = os.replace
        calls = {"n": 0}
        sleeps: list[float] = []

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("디스크 바쁨")
            return real_replace(src, dst)

        monkeypatch.setattr(pf_mod.os, "replace", flaky)
        paper.sleep = sleeps.append
        paper.state.cycle = 5
        with caplog.at_level(logging.WARNING, logger="tradingbot.portfolio"):
            paper.save()
        assert calls["n"] == 3
        assert sleeps == [0.1, 0.1]
        assert json.loads(paper.path.read_text(encoding="utf-8"))["cycle"] == 5
        assert not any(p.name.startswith("state.json.tmp") for p in paper.path.parent.iterdir())
        assert any("재시도" in r.getMessage() for r in caplog.records)

    def test_save_gives_up_after_retries(self, paper: Portfolio, monkeypatch, caplog):
        calls = {"n": 0}
        sleeps: list[float] = []

        def always_fail(src, dst):
            calls["n"] += 1
            raise PermissionError("읽기 전용")

        monkeypatch.setattr(pf_mod.os, "replace", always_fail)
        paper.sleep = sleeps.append
        with caplog.at_level(logging.ERROR, logger="tradingbot.portfolio"):
            with pytest.raises(OSError):
                paper.save()
        assert calls["n"] == 1 + SAVE_RETRIES == 6
        assert len(sleeps) == SAVE_RETRIES
        assert not paper.path.exists()
        assert list(paper.path.parent.iterdir()) == []  # tmp 정리
        assert any("최종 실패" in r.getMessage() for r in caplog.records)

    def test_save_retries_write_failure_too(self, paper: Portfolio, monkeypatch):
        real_open = open
        calls = {"n": 0}

        def flaky_open(file, *a, **kw):
            if str(file).endswith(f".tmp.{os.getpid()}"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("ENOSPC")
            return real_open(file, *a, **kw)

        monkeypatch.setattr("builtins.open", flaky_open)
        paper.save()
        assert calls["n"] == 2 and paper.path.exists()


# ---------------------------------------------------------------- 손상/불일치 정책
class TestCorruptPolicy:
    @pytest.mark.parametrize("content", ["{not json", "", "[1,2,3]", '{"mode": "paper", "bogus": 1}', '{"mode":"paper"}', "\xff\xfe".encode("latin-1").decode("latin-1")])
    def test_paper_quarantines_and_restarts(self, tmp_path: Path, caplog, content: str):
        path = tmp_path / "state.json"
        path.write_text(content, encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="tradingbot.portfolio"):
            pf = Portfolio.load(make_settings("paper"), path)
        assert pf.state.paper_quote_balance == 10_000.0 and pf.state.cycle == 0
        assert not path.exists()
        quarantined = list(tmp_path.glob("state.json.corrupt.*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == content
        assert any(r.levelno == logging.ERROR and "격리" in r.getMessage() for r in caplog.records)

    def test_paper_quarantine_twice_does_not_collide(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(pf_mod, "_now_ms", lambda: 1_700_000_000_000)
        path = tmp_path / "state.json"
        for _ in range(2):
            path.write_text("garbage", encoding="utf-8")
            Portfolio.load(make_settings("paper"), path)
        assert len(list(tmp_path.glob("state.json.corrupt.*"))) == 2

    @pytest.mark.parametrize("mode", ["testnet", "live"])
    @pytest.mark.parametrize("content", ["{not json", "", '{"mode": "testnet", "bogus": 1}'])
    def test_testnet_live_raise(self, tmp_path: Path, mode: str, content: str):
        path = tmp_path / "state.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(CorruptStateError):
            Portfolio.load(make_settings(mode), path)
        assert path.exists() and path.read_text(encoding="utf-8") == content  # 건드리지 않는다
        assert list(tmp_path.glob("state.json.corrupt.*")) == []

    def test_corrupt_error_chains_cause_and_names_file(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("{{{", encoding="utf-8")
        with pytest.raises(CorruptStateError) as ei:
            Portfolio.load(make_settings("live"), path)
        assert isinstance(ei.value.__cause__, json.JSONDecodeError)
        assert "state.json" in str(ei.value)

    def test_unknown_version_is_corrupt(self, tmp_path: Path):
        pf = Portfolio(make_settings("testnet"), tmp_path / "state.json")
        pf.state.version = 2
        pf.save()
        with pytest.raises(CorruptStateError):
            Portfolio.load(make_settings("testnet"), pf.path)

    def test_mode_mismatch_paper_restarts_with_backup(self, tmp_path: Path, caplog):
        other = Portfolio(make_settings("testnet"), tmp_path / "state.json")
        other.state.cycle = 99
        other.save()
        with caplog.at_level(logging.WARNING, logger="tradingbot.portfolio"):
            pf = Portfolio.load(make_settings("paper"), tmp_path / "state.json")
        assert pf.state.mode == "paper" and pf.state.cycle == 0
        assert pf.state.paper_quote_balance == 10_000.0
        backups = list(tmp_path.glob("state.json.mismatch.*"))
        assert len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8"))["cycle"] == 99
        assert list(tmp_path.glob("state.json.corrupt.*")) == []

    @pytest.mark.parametrize("mode", ["testnet", "live"])
    def test_mode_mismatch_testnet_live_raises(self, tmp_path: Path, mode: str):
        Portfolio(make_settings("paper"), tmp_path / "state.json").save()
        with pytest.raises(CorruptStateError, match="mode"):
            Portfolio.load(make_settings(mode), tmp_path / "state.json")
        assert (tmp_path / "state.json").exists()

    def test_read_oserror_propagates(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "state.json"
        path.write_text("{}", encoding="utf-8")

        def boom(self, *a, **kw):
            raise PermissionError("no")

        monkeypatch.setattr(Path, "read_text", boom)
        with pytest.raises(PermissionError):
            Portfolio.load(make_settings("paper"), path)


# ---------------------------------------------------------------- 진입
class TestOpenPosition:
    def test_paper_deducts_quote_plus_fee(self, paper: Portfolio):
        fill = buy_fill(base=0.01, price=50_000.0, fee=0.5)
        pos = paper.open_position(fill, 49_000.0, 52_000.0, "t", 0.9, cycle=3)
        assert paper.state.paper_quote_balance == pytest.approx(10_000.0 - 500.0 - 0.5)
        assert paper.state.positions["BTC/USDT"] is pos
        assert pos.base_amount == 0.01 and pos.entry_price == 50_000.0
        assert pos.quote_spent == 500.0 and pos.fee_quote == 0.5
        assert pos.stop_loss_price == 49_000.0 and pos.take_profit_price == 52_000.0
        assert pos.opened_cycle == 3 and pos.opened_ts == fill.ts
        assert pos.thesis == "t" and pos.confidence == 0.9
        assert pos.entry_order_id == fill.order_id and pos.last_mark == 50_000.0

    def test_testnet_does_not_touch_paper_balance(self, testnet: Portfolio):
        testnet.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        assert testnet.state.paper_quote_balance == 0.0
        assert "BTC/USDT" in testnet.state.positions

    def test_rejects_sell_fill(self, paper: Portfolio):
        with pytest.raises(ValueError):
            paper.open_position(sell_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        assert paper.state.paper_quote_balance == 10_000.0

    def test_rejects_already_held(self, paper: Portfolio):
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        with pytest.raises(ValueError, match="already_held"):
            paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=2)
        assert paper.state.paper_quote_balance == pytest.approx(10_000.0 - 500.5)

    @pytest.mark.parametrize("base", [0.0, -1.0, float("nan"), float("inf")])
    def test_rejects_bad_amount(self, paper: Portfolio, base: float):
        with pytest.raises(ValueError):
            paper.open_position(buy_fill(base=base), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        assert paper.state.positions == {}

    @pytest.mark.parametrize("sl,tp", [(0.0, 52_000.0), (49_000.0, float("nan")), (-1.0, 52_000.0)])
    def test_rejects_bad_stops(self, paper: Portfolio, sl: float, tp: float):
        with pytest.raises(ValueError):
            paper.open_position(buy_fill(), sl, tp, "t", 0.9, cycle=1)

    def test_paper_insufficient_balance_raises_without_mutation(self, paper: Portfolio):
        fill = buy_fill(base=1.0, price=50_000.0)  # 50,000 > 10,000
        with pytest.raises(ValueError, match="잔고"):
            paper.open_position(fill, 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        assert paper.state.paper_quote_balance == 10_000.0 and paper.state.positions == {}

    def test_zero_price_falls_back_to_quote_over_base(self, paper: Portfolio):
        fill = Fill(order_id="x", symbol="BTC/USDT", side="buy", base_amount=0.02, price=0.0, quote_amount=1_000.0, ts=1)
        pos = paper.open_position(fill, 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        assert pos.entry_price == pytest.approx(50_000.0)

    def test_inverted_stops_warn_but_record(self, paper: Portfolio, caplog):
        with caplog.at_level(logging.WARNING, logger="tradingbot.portfolio"):
            paper.open_position(buy_fill(), 52_000.0, 49_000.0, "t", 0.9, cycle=1)
        assert "BTC/USDT" in paper.state.positions
        assert any("어긋남" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 청산 (전량)
class TestCloseFull:
    def test_win_credits_balance_and_stats(self, paper: Portfolio):
        paper.open_position(buy_fill(base=0.01, price=50_000.0, fee=0.5), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        trade = paper.close_position("BTC/USDT", sell_fill(base=0.01, price=51_000.0, fee=0.51), "take_profit", cycle=5)
        assert "BTC/USDT" not in paper.state.positions
        assert paper.state.closed_trades == [trade]
        assert trade.reason == "take_profit"
        assert trade.base_amount == 0.01 and trade.entry_price == 50_000.0 and trade.exit_price == 51_000.0
        assert trade.quote_spent == 500.0 and trade.quote_received == pytest.approx(510.0)
        assert trade.fee_quote_total == pytest.approx(1.01)
        assert trade.pnl_quote == pytest.approx(510.0 - 500.0 - 1.01)
        assert trade.pnl_pct == pytest.approx(trade.pnl_quote / 500.0)
        assert trade.opened_cycle == 1 and trade.closed_cycle == 5
        assert trade.opened_ts == 1_000 and trade.closed_ts == 2_000
        assert trade.thesis == "t" and trade.confidence == 0.9 and trade.exit_order_id == "s-BTC/USDT"
        # 잔고: 10000 - 500.5 + 510 - 0.51
        assert paper.state.paper_quote_balance == pytest.approx(10_000.0 - 500.5 + 510.0 - 0.51)
        d = paper.state.daily
        assert (d.trades, d.wins, d.losses, d.consecutive_losses) == (1, 1, 0, 0)
        assert d.realized_pnl == pytest.approx(trade.pnl_quote)

    def test_loss_stats_and_cooldowns(self, paper: Portfolio):
        paper.open_position(buy_fill(base=0.01, price=50_000.0, fee=0.5), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        trade = paper.close_position("BTC/USDT", sell_fill(base=0.01, price=49_000.0, fee=0.49), "stop_loss", cycle=10)
        assert trade.pnl_quote == pytest.approx(490.0 - 500.0 - 0.99)
        assert trade.pnl_quote < 0
        d = paper.state.daily
        assert (d.trades, d.wins, d.losses, d.consecutive_losses) == (1, 0, 1, 1)
        assert paper.state.cooldown_until_cycle["BTC/USDT"] == 10 + HardLimits.COOLDOWN_CYCLES == 14
        assert paper.state.loss_cooldown_until_cycle["BTC/USDT"] == 10 + HardLimits.REVENGE_WINDOW_CYCLES == 18

    def test_win_sets_only_plain_cooldown(self, paper: Portfolio):
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        paper.close_position("BTC/USDT", sell_fill(price=51_000.0), "take_profit", cycle=7)
        assert paper.state.cooldown_until_cycle["BTC/USDT"] == 11
        assert "BTC/USDT" not in paper.state.loss_cooldown_until_cycle

    def test_consecutive_losses_reset_on_win(self, paper: Portfolio):
        for i in range(3):
            paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=i)
            paper.close_position("BTC/USDT", sell_fill(price=49_500.0), "stop_loss", cycle=i)
        assert paper.state.daily.consecutive_losses == 3 and paper.state.daily.losses == 3
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=9)
        paper.close_position("BTC/USDT", sell_fill(price=51_000.0), "head_sell", cycle=9)
        d = paper.state.daily
        assert d.consecutive_losses == 0 and d.wins == 1 and d.losses == 3 and d.trades == 4
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=20)
        paper.close_position("BTC/USDT", sell_fill(price=49_500.0), "stop_loss", cycle=20)
        assert paper.state.daily.consecutive_losses == 1

    def test_breakeven_counts_trade_but_not_win_or_loss(self, paper: Portfolio):
        paper.open_position(buy_fill(fee=0.0), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        paper.state.daily.consecutive_losses = 2
        trade = paper.close_position("BTC/USDT", sell_fill(price=50_000.0, fee=0.0), "manual", cycle=2)
        assert trade.pnl_quote == 0.0 and trade.pnl_pct == 0.0
        d = paper.state.daily
        assert (d.trades, d.wins, d.losses, d.consecutive_losses) == (1, 0, 0, 2)
        assert "BTC/USDT" not in paper.state.loss_cooldown_until_cycle

    def test_testnet_close_keeps_paper_balance_zero(self, testnet: Portfolio):
        testnet.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        testnet.close_position("BTC/USDT", sell_fill(price=51_000.0), "take_profit", cycle=2)
        assert testnet.state.paper_quote_balance == 0.0
        assert testnet.state.daily.trades == 1

    def test_cooldown_overwritten_by_later_close(self, paper: Portfolio):
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        paper.close_position("BTC/USDT", sell_fill(price=49_000.0), "stop_loss", cycle=2)
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=30)
        paper.close_position("BTC/USDT", sell_fill(price=51_000.0), "take_profit", cycle=31)
        assert paper.state.cooldown_until_cycle["BTC/USDT"] == 35
        assert paper.state.loss_cooldown_until_cycle["BTC/USDT"] == 10  # 손실 창은 승리로 지워지지 않음

    def test_unknown_symbol_raises_keyerror(self, paper: Portfolio):
        with pytest.raises(KeyError):
            paper.close_position("BTC/USDT", sell_fill(), "manual", cycle=1)

    def test_rejects_buy_fill_and_symbol_mismatch(self, paper: Portfolio):
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        with pytest.raises(ValueError):
            paper.close_position("BTC/USDT", buy_fill(), "manual", cycle=2)
        with pytest.raises(ValueError):
            paper.close_position("BTC/USDT", sell_fill(symbol="ETH/USDT"), "manual", cycle=2)
        with pytest.raises(ValueError):
            paper.close_position("BTC/USDT", sell_fill(base=0.0), "manual", cycle=2)
        assert "BTC/USDT" in paper.state.positions and paper.state.daily.trades == 0


# ---------------------------------------------------------------- 청산 (부분)
class TestClosePartial:
    def test_partial_books_proportional_trade_and_keeps_residual(self, paper: Portfolio):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=1.0), 98.0, 104.0, "t", 0.8, cycle=1)
        trade = paper.close_position("BTC/USDT", sell_fill(base=0.4, price=110.0, fee=0.44), "take_profit", cycle=3)
        assert trade.reason == "partial"
        assert trade.base_amount == pytest.approx(0.4)
        assert trade.quote_spent == pytest.approx(40.0)
        assert trade.quote_received == pytest.approx(44.0)
        assert trade.fee_quote_total == pytest.approx(0.4 + 0.44)
        assert trade.pnl_quote == pytest.approx(44.0 - 40.0 - 0.84)
        assert trade.pnl_pct == pytest.approx(trade.pnl_quote / 40.0)
        pos = paper.state.positions["BTC/USDT"]
        assert pos.base_amount == pytest.approx(0.6)
        assert pos.quote_spent == pytest.approx(60.0)
        assert pos.fee_quote == pytest.approx(0.6)
        assert pos.entry_price == 100.0 and pos.stop_loss_price == 98.0 and pos.take_profit_price == 104.0
        assert pos.opened_cycle == 1 and pos.last_mark == 110.0
        assert paper.state.paper_quote_balance == pytest.approx(10_000.0 - 101.0 + 44.0 - 0.44)
        d = paper.state.daily
        assert (d.trades, d.wins, d.losses) == (1, 1, 0)
        assert d.realized_pnl == pytest.approx(trade.pnl_quote)
        assert paper.state.cooldown_until_cycle["BTC/USDT"] == 3 + HardLimits.COOLDOWN_CYCLES

    def test_partial_then_full_close_sums_to_whole(self, paper: Portfolio):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=1.0), 98.0, 104.0, "t", 0.8, cycle=1)
        t1 = paper.close_position("BTC/USDT", sell_fill(base=0.4, price=110.0, fee=0.44), "take_profit", cycle=3)
        t2 = paper.close_position("BTC/USDT", sell_fill(base=0.6, price=90.0, fee=0.54), "stop_loss", cycle=4)
        assert t2.reason == "stop_loss" and "BTC/USDT" not in paper.state.positions
        assert t1.quote_spent + t2.quote_spent == pytest.approx(100.0)
        assert t1.fee_quote_total + t2.fee_quote_total == pytest.approx(1.0 + 0.44 + 0.54)
        assert t2.pnl_quote == pytest.approx(54.0 - 60.0 - 0.6 - 0.54)
        assert paper.state.paper_quote_balance == pytest.approx(10_000.0 - 101.0 + 44.0 - 0.44 + 54.0 - 0.54)
        d = paper.state.daily
        assert (d.trades, d.wins, d.losses, d.consecutive_losses) == (2, 1, 1, 1)
        assert d.realized_pnl == pytest.approx(t1.pnl_quote + t2.pnl_quote)
        assert paper.state.loss_cooldown_until_cycle["BTC/USDT"] == 4 + HardLimits.REVENGE_WINDOW_CYCLES

    def test_partial_loss_counts_as_loss(self, paper: Portfolio):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=1.0), 98.0, 104.0, "t", 0.8, cycle=1)
        trade = paper.close_position("BTC/USDT", sell_fill(base=0.5, price=90.0, fee=0.45), "stop_loss", cycle=2)
        assert trade.reason == "partial" and trade.pnl_quote < 0
        assert paper.state.daily.consecutive_losses == 1 and paper.state.daily.losses == 1
        assert paper.state.loss_cooldown_until_cycle["BTC/USDT"] == 2 + HardLimits.REVENGE_WINDOW_CYCLES

    def test_tolerance_boundary_just_inside_is_full_close(self, paper: Portfolio):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=1.0), 98.0, 104.0, "t", 0.8, cycle=1)
        sold = 1.0 * (1.0 - PARTIAL_FILL_TOLERANCE) + 1e-9  # 0.995 보다 살짝 큼
        trade = paper.close_position("BTC/USDT", sell_fill(base=sold, price=100.0, fee=0.0), "manual", cycle=2)
        assert trade.reason == "manual"
        assert trade.base_amount == 1.0  # 보유 수량 전체로 장부 마감
        assert trade.quote_spent == 100.0 and trade.fee_quote_total == pytest.approx(1.0)
        assert "BTC/USDT" not in paper.state.positions

    def test_tolerance_boundary_just_outside_is_partial(self, paper: Portfolio):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=1.0), 98.0, 104.0, "t", 0.8, cycle=1)
        sold = 1.0 * (1.0 - PARTIAL_FILL_TOLERANCE) - 1e-6
        trade = paper.close_position("BTC/USDT", sell_fill(base=sold, price=100.0, fee=0.0), "manual", cycle=2)
        assert trade.reason == "partial"
        assert paper.state.positions["BTC/USDT"].base_amount == pytest.approx(1.0 - sold)

    def test_overfill_is_clamped_with_warning(self, paper: Portfolio, caplog):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=1.0), 98.0, 104.0, "t", 0.8, cycle=1)
        with caplog.at_level(logging.WARNING, logger="tradingbot.portfolio"):
            trade = paper.close_position("BTC/USDT", sell_fill(base=1.2, price=110.0, fee=1.32), "take_profit", cycle=2)
        assert any("초과 체결" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
        assert trade.reason == "take_profit" and trade.base_amount == 1.0
        assert trade.quote_received == pytest.approx(110.0)  # 132 * (1/1.2)
        assert trade.fee_quote_total == pytest.approx(1.0 + 1.1)
        assert "BTC/USDT" not in paper.state.positions
        assert paper.state.paper_quote_balance == pytest.approx(10_000.0 - 101.0 + 110.0 - 1.1)

    def test_partial_zero_price_uses_quote_over_base(self, paper: Portfolio):
        paper.open_position(buy_fill(base=1.0, price=100.0, fee=0.0), 98.0, 104.0, "t", 0.8, cycle=1)
        fill = Fill(order_id="s", symbol="BTC/USDT", side="sell", base_amount=0.5, price=0.0, quote_amount=55.0, ts=5)
        trade = paper.close_position("BTC/USDT", fill, "manual", cycle=2)
        assert trade.exit_price == pytest.approx(110.0)


# ---------------------------------------------------------------- 자본
class TestEquity:
    def test_paper_balance_plus_marks(self, paper: Portfolio):
        paper.open_position(buy_fill(base=0.01, price=50_000.0, fee=0.5), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        assert paper.equity({}) == pytest.approx(10_000.0 - 500.5 + 0.01 * 50_000.0)  # last_mark=진입가
        assert paper.equity({"BTC/USDT": 60_000.0}) == pytest.approx(10_000.0 - 500.5 + 600.0)
        paper.update_marks({"BTC/USDT": 55_000.0, "ETH/USDT": 1.0})
        assert paper.state.positions["BTC/USDT"].last_mark == 55_000.0
        assert paper.equity({}) == pytest.approx(10_000.0 - 500.5 + 550.0)

    def test_paper_ignores_invalid_marks(self, paper: Portfolio):
        paper.open_position(buy_fill(base=0.01, price=50_000.0, fee=0.5), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        base = 10_000.0 - 500.5 + 500.0
        for bad in (float("nan"), 0.0, -5.0, float("inf")):
            assert paper.equity({"BTC/USDT": bad}) == pytest.approx(base)
        paper.update_marks({"BTC/USDT": float("nan")})
        assert paper.state.positions["BTC/USDT"].last_mark == 50_000.0

    def test_paper_missing_mark_and_last_mark_uses_entry(self, paper: Portfolio, caplog):
        paper.open_position(buy_fill(base=0.01, price=50_000.0, fee=0.5), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        paper.state.positions["BTC/USDT"].last_mark = None
        with caplog.at_level(logging.WARNING, logger="tradingbot.portfolio"):
            eq = paper.equity({})
        assert eq == pytest.approx(10_000.0 - 500.5 + 500.0)
        assert any("진입가" in r.getMessage() for r in caplog.records)

    def test_paper_ignores_quote_balance_argument(self, paper: Portfolio):
        assert paper.equity({}, quote_balance=1.0) == 10_000.0

    def test_testnet_requires_quote_balance(self, testnet: Portfolio):
        testnet.open_position(buy_fill(base=0.01, price=50_000.0), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        with pytest.raises(ValueError):
            testnet.equity({"BTC/USDT": 50_000.0})
        assert testnet.equity({"BTC/USDT": 52_000.0}, quote_balance=1_234.0) == pytest.approx(1_234.0 + 520.0)
        with pytest.raises(ValueError):
            testnet.equity({}, quote_balance=float("nan"))

    def test_update_peak_monotonic_and_ignores_bad(self, paper: Portfolio):
        assert paper.state.peak_equity == 10_000.0
        paper.update_peak(9_000.0)
        assert paper.state.peak_equity == 10_000.0
        paper.update_peak(10_500.0)
        assert paper.state.peak_equity == 10_500.0
        for bad in (float("nan"), float("inf"), -1.0):
            paper.update_peak(bad)
        assert paper.state.peak_equity == 10_500.0


# ---------------------------------------------------------------- 날짜 롤오버
class TestEnsureDay:
    def test_same_day_returns_none(self, paper: Portfolio):
        day = datetime.fromisoformat(paper.state.daily.date).replace(tzinfo=UTC)
        before = paper.state.daily.model_copy()
        assert paper.ensure_day(day + timedelta(hours=23, minutes=59), 9_999.0) is None
        assert paper.state.daily == before

    def test_new_day_returns_finished_and_resets(self, paper: Portfolio):
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        paper.close_position("BTC/USDT", sell_fill(price=49_000.0), "stop_loss", cycle=2)
        paper.state.daily.llm_cost_usd = 1.5
        old_date = paper.state.daily.date
        old = paper.state.daily.model_copy(deep=True)
        assert old.consecutive_losses == 1 and old.trades == 1
        tomorrow = datetime.fromisoformat(old_date).replace(tzinfo=UTC) + timedelta(days=1, minutes=1)
        finished = paper.ensure_day(tomorrow, 9_990.0)
        assert finished == old
        new = paper.state.daily
        assert new.date == tomorrow.date().isoformat()
        assert new.start_equity == 9_990.0
        assert new.realized_pnl == 0.0 and new.trades == 0 and new.wins == 0 and new.losses == 0
        assert new.consecutive_losses == 0 and new.llm_cost_usd == 0.0
        # 롤오버는 쿨다운/거래 내역에 손대지 않는다
        assert paper.state.cooldown_until_cycle["BTC/USDT"] == 6
        assert len(paper.state.closed_trades) == 1
        # 같은 날 다시 호출하면 None
        assert paper.ensure_day(tomorrow + timedelta(hours=1), 5.0) is None
        assert paper.state.daily.start_equity == 9_990.0

    def test_uses_utc_date_for_aware_datetimes(self, paper: Portfolio):
        old_date = paper.state.daily.date
        base = datetime.fromisoformat(old_date).replace(tzinfo=UTC)
        # UTC 기준 23:30 같은 날이지만 KST(+9)로는 다음 날
        kst = timezone(timedelta(hours=9))
        same_day_utc = (base + timedelta(hours=23, minutes=30)).astimezone(kst)
        assert paper.ensure_day(same_day_utc, 1.0) is None
        next_day_utc = (base + timedelta(hours=24, minutes=30)).astimezone(kst)
        assert paper.ensure_day(next_day_utc, 1.0) is not None
        assert paper.state.daily.date == (base + timedelta(days=1)).date().isoformat()

    def test_naive_datetime_treated_as_utc(self, paper: Portfolio):
        base = datetime.fromisoformat(paper.state.daily.date)
        assert paper.ensure_day(base + timedelta(hours=12), 1.0) is None
        assert paper.ensure_day(base + timedelta(days=1), 1.0) is not None

    def test_testnet_first_cycle_fills_start_equity(self, testnet: Portfolio):
        assert testnet.state.daily.start_equity == 0.0
        now = datetime.fromisoformat(testnet.state.daily.date).replace(tzinfo=UTC)
        assert testnet.ensure_day(now, 5_000.0) is None
        assert testnet.state.daily.start_equity == 5_000.0
        assert testnet.ensure_day(now + timedelta(hours=1), 4_000.0) is None
        assert testnet.state.daily.start_equity == 5_000.0  # 한 번 정해지면 고정

    def test_rollover_with_bad_equity_uses_zero(self, paper: Portfolio):
        day = datetime.fromisoformat(paper.state.daily.date).replace(tzinfo=UTC)
        assert paper.ensure_day(day, 10_000.0) is None  # 새 상태: 첫 호출이 날짜를 채택
        assert paper.ensure_day(day + timedelta(days=1), float("nan")) is not None
        assert paper.state.daily.start_equity == 0.0

    def test_fresh_state_adopts_first_caller_date(self, paper: Portfolio):
        # 주입 시계(SPEC §15)가 벽시계와 달라도 첫 호출은 '가짜 전날' 통계를 만들지 않는다
        injected = datetime(2024, 1, 1, 0, 10, tzinfo=UTC)
        assert paper.ensure_day(injected, 10_000.0) is None
        assert paper.state.daily.date == "2024-01-01"
        assert paper.ensure_day(injected + timedelta(days=1), 10_000.0) is not None

    def test_backwards_date_warns_and_keeps_daily_stats(self, paper: Portfolio, caplog):
        day = datetime.fromisoformat(paper.state.daily.date).replace(tzinfo=UTC)
        assert paper.ensure_day(day, 10_000.0) is None
        paper.open_position(buy_fill(), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
        paper.close_position("BTC/USDT", sell_fill(price=49_000.0), "stop_loss", cycle=2)
        before = paper.state.daily.model_copy(deep=True)
        assert before.consecutive_losses == 1
        with caplog.at_level(logging.WARNING, logger="tradingbot.portfolio"):
            assert paper.ensure_day(day - timedelta(days=1), 10.0) is None
        # 날짜 역행은 롤오버가 아니다: 당일 손실 한도·연속 손실 카운터가 유지된다
        assert paper.state.daily == before
        assert any("거꾸로" in r.getMessage() for r in caplog.records)

    def test_rollover_persists_through_save_load(self, paper: Portfolio):
        day = datetime.fromisoformat(paper.state.daily.date).replace(tzinfo=UTC)
        assert paper.ensure_day(day, 10_000.0) is None
        tomorrow = day + timedelta(days=1)
        paper.ensure_day(tomorrow, 123.0)
        paper.save()
        loaded = Portfolio.load(make_settings("paper"), paper.path)
        assert loaded.state.daily.date == tomorrow.date().isoformat()
        assert loaded.state.daily.start_equity == 123.0


# ---------------------------------------------------------------- 킬스위치 / LLM 일시정지
class TestKillAndPause:
    def test_set_and_clear_kill(self, paper: Portfolio, caplog):
        with caplog.at_level(logging.ERROR, logger="tradingbot.portfolio"):
            paper.set_kill("낙폭 6% 초과")
        assert paper.state.kill_switch is True and paper.state.kill_reason == "낙폭 6% 초과"
        assert any("킬스위치" in r.getMessage() for r in caplog.records)
        paper.clear_kill()
        assert paper.state.kill_switch is False and paper.state.kill_reason is None
        paper.clear_kill()  # 멱등
        assert paper.state.kill_switch is False

    def test_kill_survives_round_trip(self, paper: Portfolio):
        paper.set_kill("x")
        paper.save()
        loaded = Portfolio.load(make_settings("paper"), paper.path)
        assert loaded.state.kill_switch and loaded.state.kill_reason == "x"

    def test_pause_llm_window(self, paper: Portfolio):
        assert paper.llm_paused(0) is False
        paper.pause_llm(1_000, "auth")
        assert paper.state.llm_paused_until_ts == 1_000 and paper.state.llm_pause_reason == "auth"
        assert paper.llm_paused(999) is True
        assert paper.llm_paused(1_000) is False
        assert paper.llm_paused(5_000) is False

    def test_pause_llm_persists(self, paper: Portfolio):
        paper.pause_llm(77, "budget")
        paper.save()
        loaded = Portfolio.load(make_settings("paper"), paper.path)
        assert loaded.llm_paused(76) and not loaded.llm_paused(77)
        assert loaded.state.llm_pause_reason == "budget"


# ---------------------------------------------------------------- 통합: 체결 직후 저장 → 재시작 흐름
def test_full_lifecycle_survives_restart(tmp_path: Path):
    settings = make_settings("paper")
    path = tmp_path / "state.json"
    pf = Portfolio.load(settings, path)
    pf.open_position(buy_fill(base=0.01, price=50_000.0, fee=0.5), 49_000.0, 52_000.0, "t", 0.9, cycle=1)
    pf.state.cycle = 1
    pf.save()

    pf2 = Portfolio.load(settings, path)
    assert pf2.state.positions["BTC/USDT"].base_amount == 0.01
    pf2.close_position("BTC/USDT", sell_fill(base=0.01, price=49_000.0, fee=0.49), "stop_loss", cycle=2)
    pf2.state.cycle = 2
    pf2.save()

    pf3 = Portfolio.load(settings, path)
    assert pf3.state.positions == {}
    assert len(pf3.state.closed_trades) == 1 and pf3.state.closed_trades[0].reason == "stop_loss"
    assert pf3.state.daily.consecutive_losses == 1
    assert pf3.state.cooldown_until_cycle["BTC/USDT"] == 6
    assert pf3.state.loss_cooldown_until_cycle["BTC/USDT"] == 10
    assert pf3.state.paper_quote_balance == pytest.approx(10_000.0 - 500.5 + 490.0 - 0.49)
    assert math.isfinite(pf3.equity({}))
