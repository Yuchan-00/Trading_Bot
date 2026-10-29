"""tradingbot.news 테스트 (SPEC §7). 네트워크 없음: fetcher 주입 + 인라인 RSS/Atom XML."""
from __future__ import annotations

import json
import logging
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingbot import news
from tradingbot.models import NewsItem

NOW_S = 1_700_000_000.0  # 고정 now (epoch s)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(news, "_now_s", lambda: NOW_S)


def _dt(hours_ago: float) -> datetime:
    return datetime.fromtimestamp(NOW_S, tz=timezone.utc) - timedelta(hours=hours_ago)


def rfc822(hours_ago: float) -> str:
    return format_datetime(_dt(hours_ago))


def iso(hours_ago: float) -> str:
    return _dt(hours_ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def rss(items: list[tuple[str, str, float, str]], feed_title: str = "CoinDesk") -> str:
    """items: (title, link, hours_ago, description)"""
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><pubDate>{rfc822(h)}</pubDate><description>{d}</description></item>"
        for t, l, h, d in items
    )
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>{feed_title}</title>{body}</channel></rss>'


def atom(items: list[tuple[str, str, float, str]], feed_title: str = "The Block") -> str:
    body = "".join(
        f'<entry><title>{t}</title><link href="{l}"/><id>{l}</id><updated>{iso(h)}</updated><summary>{d}</summary></entry>'
        for t, l, h, d in items
    )
    return f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>{feed_title}</title>{body}</feed>'


COINDESK = "https://www.coindesk.com/arc/outboundfeeds/rss/"
THEBLOCK = "https://www.theblock.co/rss.xml"
DECRYPT = "https://decrypt.co/feed"
UNKNOWN = "https://random-blog.example.org/feed"


def make_fetcher(mapping: dict[str, str]):
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        if url in mapping:
            v = mapping[url]
            if isinstance(v, Exception):
                raise v
            return v
        raise KeyError(url)

    fetcher.calls = calls  # type: ignore[attr-defined]
    return fetcher


# ---------------------------------------------------------------- 상수/도우미
def test_red_flags_constant_matches_spec():
    assert news.RED_FLAGS == (
        "hack", "exploit", "halt", "delist", "lawsuit", "sec ", "ban", "insolvency", "bankrupt",
        "depeg", "outage", "liquidat",
    )


def test_symbol_aliases_cover_required_bases():
    for base in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"):
        assert base in news.SYMBOL_ALIASES
        assert news.SYMBOL_ALIASES[base]


@pytest.mark.parametrize("text,expected", [
    ("Bitcoin jumps 5%", ["BTC"]),
    ("BTC and ETH rally; Solana follows", ["BTC", "ETH", "SOL"]),
    ("ripple wins case", ["XRP"]),
    ("Dogecoin, Cardano, Avalanche, Chainlink, Polkadot", ["ADA", "AVAX", "DOGE", "DOT", "LINK"]),
    ("Binance Coin surges", ["BNB"]),
    ("nothing related here", []),
    ("ETHEREUM upper case", ["ETH"]),
    ("", []),
])
def test_match_symbols(text, expected):
    assert news.match_symbols(text, ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT"]) == expected


def test_match_symbols_word_boundaries():
    # "ADA" 는 "Canada" 안에서, "DOT" 는 "dotcom" 안에서 매칭되면 안 된다
    assert news.match_symbols("Canada regulators and dotcom bubble", ["ADA", "DOT"]) == []
    assert news.match_symbols("ADA: price", ["ADA"]) == ["ADA"]
    assert news.match_symbols("$BTC!", ["BTC"]) == ["BTC"]
    assert news.match_symbols("LINK's new oracle", ["LINK"]) == ["LINK"]


def test_match_symbols_only_requested():
    assert news.match_symbols("Bitcoin and Ethereum", ["ETH"]) == ["ETH"]
    assert news.match_symbols("Bitcoin and Ethereum", []) == []


def test_normalize_base():
    assert news.normalize_base("btc/usdt") == "BTC"
    assert news.normalize_base("ETH") == "ETH"
    assert news.normalize_base(" sol/USDT ") == "SOL"


@pytest.mark.parametrize("text,expected", [
    ("Exchange hacked for $100M", ["hack"]),
    ("Protocol exploit drains funds", ["exploit"]),
    ("Withdrawals halted", ["halt"]),
    ("Token delisted from exchange", ["delist"]),
    ("Class-action lawsuit filed", ["lawsuit"]),
    ("SEC sues exchange", ["sec"]),
    ("The SEC's new rule", ["sec"]),
    ("sec approves ETF", ["sec"]),
    ("second quarter results, secure wallet", []),
    ("China bans mining", ["ban"]),
    ("Banned in India", ["ban"]),
    ("Bank of America, banana, banner, band", []),
    ("Firm faces insolvency", ["insolvency"]),
    ("Lender files for bankruptcy", ["bankrupt"]),
    ("Stablecoin depegs", ["depeg"]),
    ("Major outage hits exchange", ["outage"]),
    ("$500M liquidated; liquidation cascade", ["liquidat"]),
    ("HACK and LIQUIDATIONS", ["hack", "liquidat"]),
    ("calm market", []),
    ("", []),
])
def test_find_red_flags(text, expected):
    assert news.find_red_flags(text) == expected


def test_find_red_flags_order_is_red_flags_order():
    flags = news.find_red_flags("liquidation after hack; SEC lawsuit; exchange halts")
    assert flags == ["hack", "halt", "lawsuit", "sec", "liquidat"]


@pytest.mark.parametrize("domain,tier", [
    ("coindesk.com", 1), ("www.cointelegraph.com", 1), ("theblock.co", 1), ("bloomberg.com", 1), ("reuters.com", 1),
    ("decrypt.co", 2), ("bitcoinmagazine.com", 2), ("cnbc.com", 2), ("blockworks.co", 2),
    ("random-blog.example.org", 3), ("", 3),
])
def test_source_tier(domain, tier):
    assert news.source_tier(domain) == tier


def test_source_domain():
    assert news.source_domain("https://www.coindesk.com/a/b") == "coindesk.com"
    assert news.source_domain("https://decrypt.co/feed") == "decrypt.co"
    assert news.source_domain("not a url") == ""
    assert news.source_domain("") == ""


# ---------------------------------------------------------------- fetch_news 기본
def test_fetch_rss_basic():
    xml = rss([
        ("Bitcoin hits record high", "https://www.coindesk.com/1", 1, "BTC up"),
        ("Ethereum upgrade shipped", "https://www.coindesk.com/2", 2, ""),
        ("Unrelated stock market news", "https://www.coindesk.com/3", 3, ""),
    ])
    f = make_fetcher({COINDESK: xml})
    out = news.fetch_news([COINDESK], ["BTC/USDT", "ETH/USDT"], fetcher=f)
    assert f.calls == [COINDESK]
    assert [i.title for i in out] == ["Bitcoin hits record high", "Ethereum upgrade shipped"]
    assert all(isinstance(i, NewsItem) for i in out)
    btc = out[0]
    assert btc.symbols == ["BTC"]
    assert btc.url == "https://www.coindesk.com/1"
    assert btc.source == "CoinDesk"
    assert btc.tier == 1
    assert btc.red_flags == []
    assert btc.published_ts == int(NOW_S * 1000) - 3_600_000


def test_fetch_atom_basic():
    xml = atom([("Solana outage lasts hours", "https://www.theblock.co/x", 0.5, "sol down")])
    out = news.fetch_news([THEBLOCK], ["SOL"], fetcher=lambda u: xml)
    assert len(out) == 1
    it = out[0]
    assert it.symbols == ["SOL"]
    assert it.red_flags == ["outage"]
    assert it.tier == 1
    assert it.source == "The Block"
    assert it.published_ts == int(NOW_S * 1000) - 1_800_000


def test_fetcher_receives_bytes_ok():
    xml = rss([("Bitcoin news", "https://www.coindesk.com/1", 1, "")]).encode("utf-8")
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)  # type: ignore[arg-type]
    assert len(out) == 1


def test_symbols_from_summary_too():
    xml = rss([("Market wrap", "https://www.coindesk.com/1", 1, "Bitcoin and Ethereum moved")])
    out = news.fetch_news([COINDESK], ["BTC", "ETH", "SOL"], fetcher=lambda u: xml)
    assert out[0].symbols == ["BTC", "ETH"]


def test_html_in_summary_is_stripped():
    xml = rss([("Market wrap", "https://www.coindesk.com/1", 1, "&lt;p&gt;Bitcoin &amp;amp; friends&lt;/p&gt;")])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert out and out[0].symbols == ["BTC"]


def test_red_flags_case_insensitive_in_title():
    xml = rss([("BITCOIN EXCHANGE HACKED, WITHDRAWALS HALTED", "https://www.coindesk.com/1", 1, "")])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert out[0].red_flags == ["hack", "halt"]


def test_general_red_flag_item_included_with_empty_symbols():
    xml = rss([
        ("Major exchange halts withdrawals", "https://www.coindesk.com/1", 1, ""),
        ("Nice weather today", "https://www.coindesk.com/2", 1, ""),
    ])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert [i.title for i in out] == ["Major exchange halts withdrawals"]
    assert out[0].symbols == []
    assert out[0].red_flags == ["halt"]


def test_no_symbols_requested_only_red_flags_survive():
    xml = rss([("Bitcoin up", "https://www.coindesk.com/1", 1, ""), ("SEC sues", "https://www.coindesk.com/2", 1, "")])
    out = news.fetch_news([COINDESK], [], fetcher=lambda u: xml)
    assert [i.title for i in out] == ["SEC sues"]


# ---------------------------------------------------------------- 정렬/제한/중복
def test_sorted_by_published_desc():
    xml = rss([
        ("Bitcoin old", "https://www.coindesk.com/old", 10, ""),
        ("Bitcoin new", "https://www.coindesk.com/new", 0.1, ""),
        ("Bitcoin mid", "https://www.coindesk.com/mid", 5, ""),
    ])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert [i.title for i in out] == ["Bitcoin new", "Bitcoin mid", "Bitcoin old"]


def test_items_without_date_kept_and_sorted_last():
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>CoinDesk</title>'
        "<item><title>Bitcoin undated</title><link>https://www.coindesk.com/u</link></item>"
        f"<item><title>Bitcoin dated</title><link>https://www.coindesk.com/d</link><pubDate>{rfc822(20)}</pubDate></item>"
        "</channel></rss>"
    )
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert [i.title for i in out] == ["Bitcoin dated", "Bitcoin undated"]
    assert out[1].published_ts is None


def test_max_age_hours_filters_old_items():
    xml = rss([
        ("Bitcoin fresh", "https://www.coindesk.com/1", 23, ""),
        ("Bitcoin stale", "https://www.coindesk.com/2", 25, ""),
    ])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert [i.title for i in out] == ["Bitcoin fresh"]
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml, max_age_hours=48)
    assert len(out) == 2
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml, max_age_hours=1)
    assert out == []


def test_max_per_symbol_15():
    xml = rss([(f"Bitcoin story {i}", f"https://www.coindesk.com/{i}", i * 0.1, "") for i in range(40)])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert len(out) == 15
    assert [i.title for i in out] == [f"Bitcoin story {i}" for i in range(15)]  # 최신 15개


def test_per_symbol_cap_is_independent_per_symbol():
    items = [(f"Bitcoin story {i}", f"https://www.coindesk.com/b{i}", i * 0.1, "") for i in range(20)]
    items += [(f"Ethereum story {i}", f"https://www.coindesk.com/e{i}", 5 + i * 0.1, "") for i in range(5)]
    out = news.fetch_news([COINDESK], ["BTC", "ETH"], fetcher=lambda u: rss(items))
    assert sum(1 for i in out if "BTC" in i.symbols) == 15
    assert sum(1 for i in out if "ETH" in i.symbols) == 5


def test_general_red_flag_cap_15():
    xml = rss([(f"Exchange hack number {i}", f"https://www.coindesk.com/{i}", i * 0.1, "") for i in range(30)])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert len(out) == 15
    assert all(i.symbols == [] for i in out)


def test_duplicate_urls_across_feeds_deduplicated():
    a = rss([("Bitcoin story", "https://www.coindesk.com/same", 1, "")])
    b = atom([("Bitcoin story copy", "https://www.coindesk.com/same", 1, "")])
    out = news.fetch_news([COINDESK, THEBLOCK], ["BTC"], fetcher=make_fetcher({COINDESK: a, THEBLOCK: b}))
    assert len(out) == 1


def test_duplicate_titles_deduplicated():
    a = rss([("Bitcoin story", "https://www.coindesk.com/1", 1, "")])
    b = rss([("bitcoin story", "https://decrypt.co/2", 1, "")], feed_title="Decrypt")
    out = news.fetch_news([COINDESK, DECRYPT], ["BTC"], fetcher=make_fetcher({COINDESK: a, DECRYPT: b}))
    assert len(out) == 1


def test_multiple_feeds_merged_and_tiered():
    out = news.fetch_news(
        [COINDESK, DECRYPT, UNKNOWN],
        ["BTC"],
        fetcher=make_fetcher({
            COINDESK: rss([("Bitcoin A", "https://www.coindesk.com/a", 3, "")]),
            DECRYPT: rss([("Bitcoin B", "https://decrypt.co/b", 2, "")], feed_title="Decrypt"),
            UNKNOWN: rss([("Bitcoin C", "https://random-blog.example.org/c", 1, "")], feed_title="Blog"),
        }),
    )
    assert [(i.title, i.tier) for i in out] == [("Bitcoin C", 3), ("Bitcoin B", 2), ("Bitcoin A", 1)]


def test_tier_from_entry_link_domain_falls_back_to_feed_domain():
    # 링크 없는 항목 -> 피드 도메인(coindesk) 기준 tier 1
    xml = f'<?xml version="1.0"?><rss version="2.0"><channel><title>CoinDesk</title><item><title>Bitcoin no link</title><pubDate>{rfc822(1)}</pubDate></item></channel></rss>'
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert out and out[0].tier == 1 and out[0].url == ""


def test_feed_without_title_uses_domain_as_source():
    xml = f'<?xml version="1.0"?><rss version="2.0"><channel><item><title>Bitcoin x</title><link>https://decrypt.co/1</link><pubDate>{rfc822(1)}</pubDate></item></channel></rss>'
    out = news.fetch_news([DECRYPT], ["BTC"], fetcher=lambda u: xml)
    assert out[0].source == "decrypt.co"


def test_same_timestamp_tiebreak_tier_then_title():
    items = [
        ("Bitcoin z", "https://random-blog.example.org/z", 1, ""),
        ("Bitcoin a", "https://www.coindesk.com/a", 1, ""),
        ("Bitcoin b", "https://www.coindesk.com/b", 1, ""),
    ]
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: rss(items))
    assert [i.title for i in out] == ["Bitcoin a", "Bitcoin b", "Bitcoin z"]


# ---------------------------------------------------------------- 오류 내성
def test_fetcher_exception_is_skipped_not_raised(caplog):
    good = rss([("Bitcoin ok", "https://www.coindesk.com/1", 1, "")])
    f = make_fetcher({COINDESK: good, THEBLOCK: ConnectionError("boom")})
    with caplog.at_level(logging.WARNING, logger="tradingbot.news"):
        out = news.fetch_news([THEBLOCK, COINDESK], ["BTC"], fetcher=f)
    assert [i.title for i in out] == ["Bitcoin ok"]
    assert any("theblock.co" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", ["", "not xml at all", "<html><body>503 Service Unavailable</body></html>", "<rss><channel><item><title>", "\x00\x01binary", "{}"])
def test_bad_feed_never_raises(raw):
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: raw)
    assert out == []


def test_fetcher_returning_none_is_skipped():
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: None)  # type: ignore[return-value]
    assert out == []


def test_all_feeds_fail_returns_empty_list_not_exception():
    def bad(url):
        raise RuntimeError("down")

    assert news.fetch_news([COINDESK, THEBLOCK], ["BTC"], fetcher=bad) == []


def test_no_feeds_returns_empty():
    assert news.fetch_news([], ["BTC"], fetcher=lambda u: pytest.fail("must not fetch")) == []


def test_empty_and_non_string_feed_urls_ignored():
    out = news.fetch_news(["", "   ", None], ["BTC"], fetcher=lambda u: pytest.fail("must not fetch"))  # type: ignore[list-item]
    assert out == []


def test_entry_without_title_skipped():
    xml = f'<?xml version="1.0"?><rss version="2.0"><channel><title>CoinDesk</title><item><link>https://www.coindesk.com/1</link><pubDate>{rfc822(1)}</pubDate><description>Bitcoin</description></item></channel></rss>'
    assert news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml) == []


def test_bad_date_is_tolerated():
    xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>CoinDesk</title><item><title>Bitcoin odd date</title><link>https://www.coindesk.com/1</link><pubDate>not a date</pubDate></item></channel></rss>'
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    assert len(out) == 1 and out[0].published_ts is None


def test_partial_broken_feed_still_yields_parsed_entries():
    xml = rss([("Bitcoin fine", "https://www.coindesk.com/1", 1, "")])
    truncated = xml[: xml.rfind("</item>") + len("</item>")] + "<item><title>broken"  # 닫히지 않음
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: truncated)
    assert any(i.title == "Bitcoin fine" for i in out)


def test_feedparser_parse_exception_is_contained(monkeypatch):
    def boom(_raw):
        raise ValueError("parser crashed")

    monkeypatch.setattr(news.feedparser, "parse", boom)
    assert news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: "<rss/>") == []


def test_default_fetcher_used_when_none_and_errors_are_contained(monkeypatch):
    """fetcher=None 이면 default_fetcher가 쓰인다. 네트워크 대신 실패를 주입해 사이클이 안 죽는지 본다."""
    calls = []

    def fake_default(url, timeout_s=news.DEFAULT_TIMEOUT_S):
        calls.append((url, timeout_s))
        raise OSError("no network in tests")

    monkeypatch.setattr(news, "default_fetcher", fake_default)
    assert news.fetch_news([COINDESK], ["BTC"]) == []
    assert calls == [(COINDESK, 10.0)]


def test_module_constants_for_default_fetcher():
    assert news.DEFAULT_TIMEOUT_S == 10.0
    assert "tradingbot" in news.USER_AGENT


# ---------------------------------------------------------------- 캐시
def test_cache_written_and_reused_within_10_minutes(tmp_path: Path, monkeypatch):
    cache = tmp_path / "sub" / "news_cache.json"
    xml = rss([("Bitcoin cached", "https://www.coindesk.com/1", 1, "")])
    f = make_fetcher({COINDESK: xml})
    out1 = news.fetch_news([COINDESK], ["BTC"], fetcher=f, cache_path=cache)
    assert len(out1) == 1 and f.calls == [COINDESK]
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert COINDESK in data
    assert data[COINDESK]["fetched_ts"] == int(NOW_S)
    assert "Bitcoin cached" in data[COINDESK]["raw"]

    # 9분 뒤: 캐시 사용, fetcher 호출 없음
    monkeypatch.setattr(news, "_now_s", lambda: NOW_S + 9 * 60)
    out2 = news.fetch_news([COINDESK], ["BTC"], fetcher=f, cache_path=cache)
    assert f.calls == [COINDESK]
    assert [i.title for i in out2] == ["Bitcoin cached"]

    # 11분 뒤: 다시 받음
    monkeypatch.setattr(news, "_now_s", lambda: NOW_S + 11 * 60)
    news.fetch_news([COINDESK], ["BTC"], fetcher=f, cache_path=cache)
    assert f.calls == [COINDESK, COINDESK]
    assert json.loads(cache.read_text(encoding="utf-8"))[COINDESK]["fetched_ts"] == int(NOW_S + 11 * 60)


def test_cache_is_deterministic_json(tmp_path: Path):
    cache = tmp_path / "c.json"
    f = make_fetcher({
        THEBLOCK: atom([("Bitcoin 한글 제목", "https://www.theblock.co/1", 1, "")]),
        COINDESK: rss([("Bitcoin a", "https://www.coindesk.com/1", 1, "")]),
    })
    news.fetch_news([THEBLOCK, COINDESK], ["BTC"], fetcher=f, cache_path=cache)
    text = cache.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    assert "한글" in text  # ensure_ascii=False


def test_cache_fallback_when_fetch_fails(tmp_path: Path, monkeypatch):
    cache = tmp_path / "c.json"
    xml = rss([("Bitcoin from cache", "https://www.coindesk.com/1", 1, "")])
    news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml, cache_path=cache)

    monkeypatch.setattr(news, "_now_s", lambda: NOW_S + 30 * 60)  # TTL 지남

    def failing(url):
        raise TimeoutError("slow")

    out = news.fetch_news([COINDESK], ["BTC"], fetcher=failing, cache_path=cache)
    assert [i.title for i in out] == ["Bitcoin from cache"]


def test_corrupt_cache_file_is_ignored(tmp_path: Path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json", encoding="utf-8")
    xml = rss([("Bitcoin x", "https://www.coindesk.com/1", 1, "")])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml, cache_path=cache)
    assert len(out) == 1
    assert json.loads(cache.read_text(encoding="utf-8"))[COINDESK]["raw"] == xml


def test_cache_entries_for_other_urls_preserved(tmp_path: Path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"https://other/feed": {"fetched_ts": 1, "raw": "<rss/>"}}), encoding="utf-8")
    news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: rss([("Bitcoin", "https://www.coindesk.com/1", 1, "")]), cache_path=cache)
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert set(data) == {"https://other/feed", COINDESK}


def test_cache_write_failure_does_not_raise(tmp_path: Path, monkeypatch):
    cache = tmp_path / "c.json"

    def bad_replace(src, dst):
        raise PermissionError("ro")

    monkeypatch.setattr(news.os, "replace", bad_replace)
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: rss([("Bitcoin", "https://www.coindesk.com/1", 1, "")]), cache_path=cache)
    assert len(out) == 1


def test_no_cache_file_created_when_cache_path_none(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: rss([("Bitcoin", "https://www.coindesk.com/1", 1, "")]))
    assert list(tmp_path.iterdir()) == []


def test_cache_not_written_when_nothing_fetched(tmp_path: Path):
    cache = tmp_path / "c.json"

    def failing(url):
        raise RuntimeError("down")

    news.fetch_news([COINDESK], ["BTC"], fetcher=failing, cache_path=cache)
    assert not cache.exists()


# ---------------------------------------------------------------- 기타
def test_symbols_iterable_accepts_full_pairs_and_bases_and_dedupes():
    xml = rss([("Bitcoin and ether", "https://www.coindesk.com/1", 1, "")])
    out = news.fetch_news([COINDESK], ("btc/usdt", "BTC", "ETH/USDT"), fetcher=lambda u: xml)
    assert out[0].symbols == ["BTC", "ETH"]


def test_result_items_serialize_sorted_json():
    xml = rss([("Bitcoin hack", "https://www.coindesk.com/1", 1, "")])
    out = news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: xml)
    s = json.dumps([i.model_dump() for i in out], sort_keys=True, ensure_ascii=False)
    assert '"red_flags": ["hack"]' in s


def test_no_print_and_logger_name(caplog):
    with caplog.at_level(logging.DEBUG, logger="tradingbot.news"):
        news.fetch_news([COINDESK], ["BTC"], fetcher=lambda u: "")
    assert caplog.records and all(r.name == "tradingbot.news" for r in caplog.records)


def test_parse_feed_direct():
    items = news.parse_feed(rss([("Bitcoin", "https://www.coindesk.com/1", 1, "")]), COINDESK, ["BTC"], now_ms=int(NOW_S * 1000), max_age_hours=24)
    assert len(items) == 1 and items[0].symbols == ["BTC"]
    assert news.parse_feed("garbage", COINDESK, ["BTC"], now_ms=int(NOW_S * 1000), max_age_hours=24) == []


def test_runs_fast_with_many_entries():
    items = [(f"Bitcoin and Ethereum story {i} hack", f"https://www.coindesk.com/{i}", i * 0.01, "solana too") for i in range(500)]
    xml = rss(items)
    t0 = time.perf_counter()
    out = news.fetch_news([COINDESK] * 3, ["BTC", "ETH", "SOL"], fetcher=lambda u: xml)
    assert time.perf_counter() - t0 < 2.0
    assert len(out) == 15
