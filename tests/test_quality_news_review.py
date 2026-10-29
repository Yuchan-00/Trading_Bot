"""적대적 리뷰 테스트 — tradingbot/news.py (SPEC §7), tradingbot/quality.py (SPEC §6).

각 테스트는 현재 구현에서 **실패**해야 하며, 결함이 고쳐지면 통과한다.
오프라인·네트워크 없음: feedparser의 네트워크 함수는 monkeypatch로 가로채 기록만 한다.
"""
from __future__ import annotations

import os

import json
from pathlib import Path

import feedparser.http as feedparser_http
import pytest

from tradingbot import news

NOW_S = 1_704_067_200.0  # 2024-01-01T00:00:00Z


def _rss(items_xml: str, feed_title: str = "Feed") -> str:
    return f"<rss><channel><title>{feed_title}</title>{items_xml}</channel></rss>"


def _item(title: str, link: str, minute: int = 0) -> str:
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>Mon, 01 Jan 2024 00:{minute:02d}:00 GMT</pubDate></item>"
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(news, "_now_s", lambda: NOW_S)


@pytest.fixture
def no_network(monkeypatch):
    """feedparser가 스스로 네트워크를 타면 기록한다(실제 소켓은 절대 열지 않는다)."""
    calls: list[str] = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return b"<rss><channel><item><title>Bitcoin hacked (fetched by feedparser)</title></item></channel></rss>"

    monkeypatch.setattr(feedparser_http, "get", fake_get)
    return calls


# ---------------------------------------------------------------- F1: feedparser가 피드 본문을 URL로 보고 직접 받아옴
def test_parse_feed_never_fetches_url_embedded_in_feed_body(no_network):
    """fetcher가 돌려준 본문이 'https://...' 로 시작하면 feedparser.parse(str)는 그 문자열을
    URL로 보고 **네트워크 요청**을 한다(타임아웃 없음 → 사이클 정지 가능, 피드가 지정한 임의 호스트 접속).
    '<' 포함 여부 검사는 이를 막지 못한다. 원문은 항상 데이터로만 다뤄야 한다."""
    body = "https://127.0.0.1:1/anything <rss/>"
    items = news.parse_feed(body, "https://feed.example/rss", ["BTC"], now_ms=int(NOW_S * 1000), max_age_hours=24)
    assert no_network == [], f"feedparser가 본문을 URL로 받아감: {no_network}"
    assert all("fetched by feedparser" not in it.title for it in items)


def test_fetch_news_never_fetches_url_embedded_in_feed_body(no_network):
    out = news.fetch_news(
        ["https://feed.example/rss"], ["BTC"],
        fetcher=lambda u: "https://127.0.0.1:1/other-host <rss/>",
    )
    assert no_network == []
    assert out == []


def test_parse_feed_never_reads_local_file_named_in_feed_body(tmp_path: Path, no_network):
    """본문이 로컬 파일 경로이면 feedparser.parse(str)는 그 파일을 열어 읽는다(디스크 읽기 → LLM 입력)."""
    # '<' '>' 는 Windows 파일 이름에 쓸 수 없다 → 그 플랫폼에서는 평범한 이름으로 같은 경로(로컬 파일 읽기)를 검사
    secret_file = tmp_path / ("a<rss>.xml" if os.name != "nt" else "a-rss.xml")
    secret_file.write_text(
        "<rss><channel><item><title>Bitcoin exploit from local disk</title></item></channel></rss>",
        encoding="utf-8",
    )
    items = news.parse_feed(str(secret_file), "https://feed.example/rss", ["BTC"], now_ms=int(NOW_S * 1000), max_age_hours=24)
    assert all("local disk" not in it.title for it in items), "피드 본문으로 지정된 로컬 파일을 읽어 뉴스로 내보냄"


# ---------------------------------------------------------------- F2: 심볼당 최대 15개 초과
def test_per_symbol_cap_15_is_never_exceeded():
    """SPEC §7: 심볼당 최대 15개. 다중 심볼 기사는 한 심볼이 꽉 찼어도 다른 심볼의 여유로 들어가
    꽉 찬 심볼의 기사 수가 15를 넘는다."""
    # BTC 단독 기사 15건이 가장 최신(먼저 BTC를 15개로 채움), 그 뒤 BTC+ETH 기사 15건
    xml = _rss(
        "".join(_item(f"bitcoin only {i}", f"https://x.example/b{i}", 15 + i) for i in range(15))
        + "".join(_item(f"bitcoin and ethereum {i}", f"https://x.example/be{i}", i) for i in range(15))
    )
    out = news.fetch_news(["https://feed.example/rss"], ["BTC/USDT", "ETH/USDT"], fetcher=lambda u: xml)
    per_symbol = {}
    for it in out:
        for s in it.symbols:
            per_symbol[s] = per_symbol.get(s, 0) + 1
    assert all(n <= news.MAX_PER_SYMBOL for n in per_symbol.values()), per_symbol


# ---------------------------------------------------------------- F3: tier 1 부분 문자열 스푸핑
@pytest.mark.parametrize(
    "domain",
    ["evilcoindesk.com", "coindesk.evil.com", "reuters-news.fake", "notbloomberg.io", "cointelegraph.scam.xyz"],
)
def test_tier1_is_not_granted_by_substring_of_unrelated_domain(domain):
    """tier는 기사 링크의 도메인으로 정해지는데(피드 내용이 결정), 'coindesk' 같은 부분 문자열만 있으면
    아무 도메인이나 tier 1이 된다. 알려진 도메인(또는 그 서브도메인)만 1이어야 한다."""
    assert news.source_tier(domain) == 3


def test_tier1_real_domains_and_subdomains_still_tier1():
    for d in ("coindesk.com", "www.coindesk.com", "feeds.reuters.com", "cointelegraph.com", "theblock.co", "bloomberg.com"):
        assert news.source_tier(news.source_domain(f"https://{d}/x")) == 1, d


def test_tier2_is_not_granted_by_incidental_substring():
    # 'wsj' ⊂ 'newsjunkie', 'ft.com' ⊂ 'soft.com'
    assert news.source_tier("newsjunkie.com") == 3
    assert news.source_tier("soft.com") == 3


# ---------------------------------------------------------------- F4: 제목의 '<' … '>' 가 HTML 태그로 오인되어 본문이 잘림
def test_angle_brackets_in_plain_title_are_not_stripped_as_tags():
    """feedparser는 이미 엔티티를 풀어 'Bitcoin < $50k and ethereum > $3k' 를 준다.
    그 다음 정규식 태그 제거가 '< $50k and ethereum >' 를 통째로 지워 ETH 언급이 사라지고 제목이 왜곡된다."""
    xml = _rss(_item("Bitcoin &lt; $50k and ethereum &gt; $3k", "https://x.example/1"))
    out = news.fetch_news(["https://feed.example/rss"], ["BTC", "ETH"], fetcher=lambda u: xml)
    assert len(out) == 1
    assert "$50k" in out[0].title, out[0].title
    assert out[0].symbols == ["BTC", "ETH"], out[0].symbols


# ---------------------------------------------------------------- F5: 피드가 아닌 200 응답이 정상 캐시를 덮어씀
def test_non_feed_response_does_not_clobber_good_cache(tmp_path: Path, monkeypatch):
    """모듈 문서: '받기에 실패하면 오래된 캐시라도 쓴다'. 그러나 서버가 피드가 아닌 HTML(예: 점검 페이지)을
    200으로 돌려주면 그 쓰레기가 캐시를 덮어쓰고 결과는 0건 — 직전까지 멀쩡했던 뉴스가 사라진다."""
    cache = tmp_path / "news_cache.json"
    good = _rss(_item("Bitcoin hacked at exchange", "https://x.example/g"))
    clock = [NOW_S]
    monkeypatch.setattr(news, "_now_s", lambda: clock[0])

    first = news.fetch_news(["https://feed.example/rss"], ["BTC"], fetcher=lambda u: good, cache_path=cache)
    assert len(first) == 1

    clock[0] += 3600  # 캐시 TTL(10분) 지난 뒤, 서버가 점검 페이지를 200으로 돌려줌
    second = news.fetch_news(
        ["https://feed.example/rss"], ["BTC"],
        fetcher=lambda u: "<html><body><h1>503 Service Unavailable</h1></body></html>",
        cache_path=cache,
    )
    assert [it.title for it in second] == ["Bitcoin hacked at exchange"], "피드 아닌 응답이 오면 오래된 캐시로 대체해야 한다"
    cached_raw = json.loads(cache.read_text(encoding="utf-8"))["https://feed.example/rss"]["raw"]
    assert "<rss" in cached_raw, "정상 캐시가 HTML 점검 페이지로 덮어써짐"
