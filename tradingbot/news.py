"""RSS/Atom 뉴스 수집 (SPEC §7).

- ``fetcher(url) -> raw xml`` 을 주입할 수 있다(테스트). 기본은 urllib(10초, User-Agent).
- 파싱은 feedparser. 피드 하나가 깨져도 **절대 예외를 올리지 않는다**: 로그 후 건너뛴다.
- 심볼 매칭: base 티커(BTC, ETH…)와 별칭(bitcoin, ethereum…)을 단어 경계 기준, 대소문자 무시.
- 위험 키워드(``RED_FLAGS``)는 대소문자 무시로 제목+요약에서 찾는다.
- tier: 1 = coindesk/cointelegraph/theblock/bloomberg/reuters, 2 = 기타 알려진 매체, 3 = 나머지.
- 결과는 published 내림차순(없으면 뒤), 심볼당 최대 15개. 어떤 심볼에도 안 걸리지만 위험
  키워드가 있는 기사는 ``symbols=[]`` 로 "시장 전체 경고"로 최대 15개 포함한다.
- 빈 결과는 "조용한 시장"이 아니라 "정보 없음"이다 — 호출자가 ``news_available=False`` 를 LLM에 전달한다.
- ``cache_path`` 를 주면 URL별 원문 XML을 JSON으로 캐시하고 10분 안에는 다시 받지 않는다.
  받기에 실패하면 오래된 캐시라도 쓴다.
"""
from __future__ import annotations

import calendar
import html
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser

from .models import NewsItem

log = logging.getLogger("tradingbot.news")

RED_FLAGS = (
    "hack", "exploit", "halt", "delist", "lawsuit", "sec ", "ban", "insolvency", "bankrupt",
    "depeg", "outage", "liquidat",
)

# base 티커 -> 별칭(소문자). 티커 자체도 항상 매칭된다.
SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "xbt"),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "BNB": ("binance coin", "bnb chain", "bsc"),
    "XRP": ("ripple",),
    "DOGE": ("dogecoin",),
    "ADA": ("cardano",),
    "AVAX": ("avalanche",),
    "LINK": ("chainlink",),
    "DOT": ("polkadot",),
    "MATIC": ("polygon", "pol"),
    "LTC": ("litecoin",),
    "TRX": ("tron",),
    "ATOM": ("cosmos",),
    "UNI": ("uniswap",),
    "SHIB": ("shiba inu", "shiba"),
    "TON": ("toncoin", "the open network"),
    "SUI": ("sui network",),
    "ARB": ("arbitrum",),
    "OP": ("optimism",),
}

TIER1_DOMAINS = ("coindesk", "cointelegraph", "theblock", "bloomberg", "reuters")
TIER2_DOMAINS = (
    "decrypt", "bitcoinmagazine", "cryptoslate", "cryptonews", "beincrypto", "dlnews", "blockworks",
    "thedefiant", "messari", "wsj", "ft.com", "cnbc", "forbes", "binance", "coinbase", "kraken",
    "cryptobriefing", "newsbtc", "ambcrypto", "u.today", "cryptopotato", "bitcoinist",
    "coinmarketcap", "coingecko", "protos", "unchained", "watcher.guru", "cryptoquant", "glassnode",
    "defillama", "bitcoin.com", "news.bitcoin", "theverge", "wired", "techcrunch", "nytimes", "wsj",
)

MAX_PER_SYMBOL = 15
CACHE_TTL_S = 10 * 60
DEFAULT_TIMEOUT_S = 10.0
USER_AGENT = "tradingbot/0.1 (+rss; python-urllib)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _now_s() -> float:
    """현재 시각(epoch 초). 테스트가 monkeypatch 한다."""
    return time.time()


# ---------------------------------------------------------------- 매칭
def _flag_pattern(flag: str) -> re.Pattern[str]:
    core = flag.strip()
    if flag.endswith(" "):            # "sec " -> 독립 단어 SEC (second/secure 제외)
        return re.compile(r"\b" + re.escape(core) + r"\b", re.IGNORECASE)
    if core == "ban":                 # bank/band/banner 제외
        return re.compile(r"\bban(?:s|ned|ning)?\b", re.IGNORECASE)
    return re.compile(r"\b" + re.escape(core), re.IGNORECASE)  # 접두 매칭: hacked, halted, liquidation…


_FLAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (f.strip(), _flag_pattern(f)) for f in RED_FLAGS
)


def find_red_flags(text: str) -> list[str]:
    """텍스트에서 위험 키워드를 찾는다(RED_FLAGS 순서, 공백 제거된 형태)."""
    return [name for name, pat in _FLAG_PATTERNS if pat.search(text or "")]


def normalize_base(symbol: str) -> str:
    """'BTC/USDT' / 'btc' -> 'BTC'."""
    return symbol.split("/", 1)[0].strip().upper()


def _symbol_pattern(base: str) -> re.Pattern[str]:
    terms = [base.lower(), *SYMBOL_ALIASES.get(base, ())]
    alts = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z0-9])(?:" + alts + r")(?![A-Za-z0-9])", re.IGNORECASE)


def match_symbols(text: str, bases: Iterable[str]) -> list[str]:
    """텍스트에 언급된 base 티커 목록(알파벳순, 중복 없음)."""
    found: set[str] = set()
    for b in bases:
        base = normalize_base(b)
        if not base:
            continue
        if _symbol_pattern(base).search(text or ""):
            found.add(base)
    return sorted(found)


def source_domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def source_tier(domain: str) -> int:
    d = (domain or "").lower()
    if not d:
        return 3
    if any(k in d for k in TIER1_DOMAINS):
        return 1
    if any(k in d for k in TIER2_DOMAINS):
        return 2
    return 3


# ---------------------------------------------------------------- 파싱
def _clean_text(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    s = html.unescape(_TAG_RE.sub(" ", s))
    return _WS_RE.sub(" ", s).strip()


def _entry_ts(entry: Any) -> int | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key) if hasattr(entry, "get") else None
        if st is None:
            continue
        try:
            return int(calendar.timegm(st)) * 1000
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def parse_feed(raw: str | bytes, feed_url: str, bases: Iterable[str], *, now_ms: int, max_age_hours: int) -> list[NewsItem]:
    """원문 XML 하나를 NewsItem 목록으로 바꾼다. 깨진 피드는 빈 목록(예외 없음)."""
    bases = [normalize_base(b) for b in bases]
    domain_feed = source_domain(feed_url)
    # feedparser는 '<'로 시작하지 않는 문자열을 URL/파일 경로로 보고 직접 받으러 갈 수 있다 -> 차단
    head = raw[:4096] if isinstance(raw, (str, bytes)) else ""
    if isinstance(head, bytes):
        head = head.decode("utf-8", errors="replace")
    if "<" not in head:
        log.warning("피드 원문이 XML이 아님 %s", domain_feed)
        return []
    try:
        parsed = feedparser.parse(raw)
    except Exception as e:  # feedparser는 거의 던지지 않지만, 절대 전파하지 않는다
        log.warning("피드 파싱 실패 %s: %s", domain_feed, type(e).__name__)
        return []

    entries = list(getattr(parsed, "entries", None) or [])
    if not entries:
        if getattr(parsed, "bozo", False):
            log.warning("피드 깨짐(항목 없음) %s: %s", domain_feed, type(getattr(parsed, "bozo_exception", None)).__name__)
        else:
            log.info("피드 항목 없음 %s", domain_feed)
        return []

    feed_title = _clean_text(getattr(parsed, "feed", {}).get("title", "")) if hasattr(parsed, "feed") else ""
    cutoff_ms = now_ms - int(max_age_hours) * 3_600_000 if max_age_hours and max_age_hours > 0 else None

    items: list[NewsItem] = []
    for entry in entries:
        try:
            title = _clean_text(entry.get("title", ""))
            if not title:
                continue
            link = entry.get("link") or ""
            if not isinstance(link, str):
                link = ""
            link = link.strip()
            summary = _clean_text(entry.get("summary", "") or entry.get("description", ""))
            ts = _entry_ts(entry)
            if cutoff_ms is not None and ts is not None and ts < cutoff_ms:
                continue
            text = f"{title} {summary}"
            domain = source_domain(link) or domain_feed
            items.append(
                NewsItem(
                    title=title[:300],
                    source=feed_title or domain_feed or domain or "unknown",
                    url=link,
                    published_ts=ts,
                    symbols=match_symbols(text, bases),
                    red_flags=find_red_flags(text),
                    tier=source_tier(domain),  # type: ignore[arg-type]
                )
            )
        except Exception as e:  # 항목 하나가 이상해도 나머지는 살린다
            log.debug("피드 항목 건너뜀 %s: %s", domain_feed, type(e).__name__)
            continue
    return items


# ---------------------------------------------------------------- 가져오기/캐시
def default_fetcher(url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:  # pragma: no cover - 네트워크
    """urllib으로 피드 원문을 받는다(10초, User-Agent)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - 설정된 https URL만
        data = resp.read()
    charset = None
    try:
        charset = resp.headers.get_content_charset()
    except Exception:
        charset = None
    return data.decode(charset or "utf-8", errors="replace")


def _load_cache(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("뉴스 캐시 읽기 실패 %s: %s", path, type(e).__name__)
        return {}


def _save_cache(path: Path | None, cache: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cache, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        log.warning("뉴스 캐시 쓰기 실패 %s: %s", path, type(e).__name__)


def _get_raw(url: str, fetcher: Callable[[str], str], cache: dict[str, Any], now_s: float) -> tuple[str | None, bool]:
    """원문을 캐시 또는 fetcher에서 얻는다. 반환 (raw|None, cache_changed)."""
    entry = cache.get(url)
    if isinstance(entry, dict) and isinstance(entry.get("raw"), str):
        try:
            age = now_s - float(entry.get("fetched_ts", 0))
        except (TypeError, ValueError):
            age = float("inf")
        if 0 <= age < CACHE_TTL_S:
            log.debug("뉴스 캐시 사용 %s (age=%.0fs)", source_domain(url), age)
            return entry["raw"], False
    try:
        raw = fetcher(url)
    except Exception as e:
        log.warning("피드 가져오기 실패 %s: %s", source_domain(url) or url[:40], type(e).__name__)
        raw = None
    if raw is None or not isinstance(raw, (str, bytes)) or not raw:
        if isinstance(entry, dict) and isinstance(entry.get("raw"), str):
            log.info("오래된 캐시로 대체 %s", source_domain(url))
            return entry["raw"], False
        return None, False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    cache[url] = {"fetched_ts": int(now_s), "raw": raw}
    return raw, True


def fetch_news(
    feeds: Iterable[str],
    symbols: Iterable[str],
    *,
    fetcher: Callable[[str], str] | None = None,
    max_age_hours: int = 24,
    cache_path: Path | None = None,
) -> list[NewsItem]:
    """피드들을 읽어 관련 뉴스를 돌려준다. 어떤 피드 오류에도 예외를 던지지 않는다.

    반환: published 내림차순(없으면 뒤), 같은 시각이면 tier 오름차순·제목순. 심볼당 최대 15개,
    심볼 없는 위험 키워드 기사 최대 15개. 빈 목록 = 정보 없음.
    """
    fetch = fetcher if fetcher is not None else default_fetcher
    bases: list[str] = []
    for s in symbols:
        b = normalize_base(s)
        if b and b not in bases:
            bases.append(b)
    now_s = _now_s()
    now_ms = int(now_s * 1000)

    cache = _load_cache(cache_path)
    cache_changed = False
    collected: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    n_feeds = 0

    for url in feeds:
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        n_feeds += 1
        try:
            raw, changed = _get_raw(url, fetch, cache, now_s)
            cache_changed = cache_changed or changed
            if raw is None:
                continue
            for item in parse_feed(raw, url, bases, now_ms=now_ms, max_age_hours=max_age_hours):
                key_url = item.url.strip().lower()
                key_title = item.title.strip().lower()
                if (key_url and key_url in seen_urls) or key_title in seen_titles:
                    continue
                if key_url:
                    seen_urls.add(key_url)
                seen_titles.add(key_title)
                collected.append(item)
        except Exception as e:  # 마지막 방어선: 어떤 피드도 전체를 죽이지 않는다
            log.error("피드 처리 중 예외 %s: %s", source_domain(url) or url[:40], type(e).__name__)
            continue

    if cache_changed:
        _save_cache(cache_path, cache)

    collected.sort(key=lambda it: (it.published_ts is None, -(it.published_ts or 0), it.tier, it.title))

    counts: dict[str, int] = {}
    general = 0
    out: list[NewsItem] = []
    for item in collected:
        if item.symbols:
            if all(counts.get(s, 0) >= MAX_PER_SYMBOL for s in item.symbols):
                continue
            for s in item.symbols:
                counts[s] = counts.get(s, 0) + 1
            out.append(item)
        elif item.red_flags:
            if general >= MAX_PER_SYMBOL:
                continue
            general += 1
            out.append(item)

    log.info("뉴스 %d건 (심볼 %s, 피드 %d개)", len(out), ",".join(bases) or "-", n_feeds)
    return out
