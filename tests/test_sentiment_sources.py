"""tradingbot.sentiment_sources: FearGreedFetcher (네트워크 없음, http_get 주입)."""
from __future__ import annotations

import json

import pytest

from tradingbot.sentiment_sources import FearGreedFetcher, parse_fear_greed


def _body(value) -> str:
    return json.dumps({"name": "Fear and Greed Index", "data": [{"value": value, "value_classification": "Fear", "timestamp": "1"}]})


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout_s: float) -> str:
        self.calls.append((url, timeout_s))
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_success_returns_value_and_writes_cache(tmp_path):
    path = tmp_path / "sub" / "fng.json"
    http = FakeHttp([_body("34")])
    clock = Clock()
    f = FearGreedFetcher(path, http_get=http, clock=clock, timeout_s=3.5)
    assert f() == 34
    assert http.calls == [("https://api.alternative.me/fng/?limit=1", 3.5)]
    doc = json.loads(path.read_text())
    assert doc == {"value": 34, "fetched_ts": clock.t}


def test_memory_cache_hit_within_ttl():
    http = FakeHttp([_body("50"), _body("60")])
    clock = Clock()
    f = FearGreedFetcher(None, ttl_s=3600, http_get=http, clock=clock)
    assert f() == 50
    clock.t += 3599
    assert f() == 50
    assert len(http.calls) == 1
    clock.t += 2
    assert f() == 60
    assert len(http.calls) == 2


def test_file_cache_hit_across_instances(tmp_path):
    path = tmp_path / "fng.json"
    clock = Clock()
    assert FearGreedFetcher(path, http_get=FakeHttp([_body("12")]), clock=clock)() == 12
    http2 = FakeHttp([])  # 호출되면 IndexError -> 실패로 처리되지만 캐시로 응답해야 함
    clock.t += 100
    f2 = FearGreedFetcher(path, http_get=http2, clock=clock)
    assert f2() == 12
    assert http2.calls == []


def test_stale_cache_fallback_on_network_error(tmp_path):
    path = tmp_path / "fng.json"
    clock = Clock()
    path.write_text(json.dumps({"value": 77, "fetched_ts": clock.t - 5 * 3600}))
    http = FakeHttp([OSError("boom")])
    f = FearGreedFetcher(path, ttl_s=3600, http_get=http, clock=clock)
    assert f() == 77
    assert len(http.calls) == 1


def test_stale_cache_older_than_24h_is_not_used(tmp_path):
    path = tmp_path / "fng.json"
    clock = Clock()
    path.write_text(json.dumps({"value": 77, "fetched_ts": clock.t - 25 * 3600}))
    f = FearGreedFetcher(path, http_get=FakeHttp([TimeoutError()]), clock=clock)
    assert f() is None


@pytest.mark.parametrize("text", ["not json", "", "[]", "{}", '{"data": []}', '{"data": [{}]}',
                                  '{"data": [{"value": "abc"}]}', '{"data": "x"}', "null",
                                  '{"data": [{"value": true}]}', '{"data": [{"value": "34.5"}]}'])
def test_garbage_json_returns_none_without_cache(text):
    f = FearGreedFetcher(None, http_get=FakeHttp([text]), clock=Clock())
    assert f() is None


def test_garbage_json_falls_back_to_cache():
    clock = Clock()
    f = FearGreedFetcher(None, ttl_s=10, http_get=FakeHttp([_body("40"), "garbage"]), clock=clock)
    assert f() == 40
    clock.t += 60
    assert f() == 40


@pytest.mark.parametrize("value", ["-1", "101", "1000", -5, 101, "nan", "inf"])
def test_out_of_range_rejected(value, tmp_path):
    path = tmp_path / "fng.json"
    f = FearGreedFetcher(path, http_get=FakeHttp([_body(value)]), clock=Clock())
    assert f() is None
    assert not path.exists()


@pytest.mark.parametrize("value,expected", [("0", 0), ("100", 100), (55, 55), (" 7 ", 7), (20.0, 20)])
def test_boundaries_accepted(value, expected):
    assert parse_fear_greed(_body(value)) == expected


def test_no_cache_and_error_returns_none():
    f = FearGreedFetcher(None, http_get=FakeHttp([RuntimeError("x")]), clock=Clock())
    assert f() is None


def test_corrupt_cache_file_ignored(tmp_path):
    path = tmp_path / "fng.json"
    path.write_text("{{{ broken")
    f = FearGreedFetcher(path, http_get=FakeHttp([ValueError("x")]), clock=Clock())
    assert f() is None
    path.write_text(json.dumps({"value": 500, "fetched_ts": 1_000_000.0}))
    f2 = FearGreedFetcher(path, http_get=FakeHttp([_body("9")]), clock=Clock())
    assert f2() == 9


def test_never_raises_even_if_clock_breaks():
    def bad_clock():
        raise RuntimeError("clock")
    f = FearGreedFetcher(None, http_get=FakeHttp([_body("1")]), clock=bad_clock)
    assert f() is None


def test_unwritable_cache_path_still_returns_value(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    f = FearGreedFetcher(blocker / "fng.json", http_get=FakeHttp([_body("33")]), clock=Clock())
    assert f() == 33


def test_default_http_get_used_when_not_injected(monkeypatch):
    import tradingbot.sentiment_sources as ss
    seen = {}

    def fake(url, timeout_s):
        seen["args"] = (url, timeout_s)
        return _body("21")
    monkeypatch.setattr(ss, "default_http_get", fake)
    assert ss.FearGreedFetcher(None, clock=Clock())() == 21
    assert seen["args"] == (ss.FEAR_GREED_URL, 10.0)


def test_failure_is_backed_off_then_retried():
    """받기 실패 뒤 retry_after_failure_s 동안은 다시 받지 않는다(멈춘 엔드포인트가 매 사이클 스캔을 붙잡지 않게)."""
    clock = Clock()
    http = FakeHttp([TimeoutError(), _body("50")])
    f = FearGreedFetcher(None, http_get=http, clock=clock, retry_after_failure_s=600)
    assert f() is None
    clock.t += 599
    assert f() is None and len(http.calls) == 1
    clock.t += 2
    assert f() == 50 and len(http.calls) == 2


def test_default_http_get_bounds_total_time(monkeypatch):
    """기본 받기는 전체 시간을 timeout_s 로 자른다(소켓 연산별 타임아웃만으로는 부족)."""
    import threading
    import time

    import tradingbot.sentiment_sources as ss

    release = threading.Event()

    def stalled(url, timeout_s, deadline):
        release.wait(5)
        return _body("1")

    monkeypatch.setattr(ss, "_urllib_get", stalled)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        ss.default_http_get("http://127.0.0.1:9/", 0.2)
    assert time.monotonic() - t0 < 1.0
    release.set()


def test_default_http_get_propagates_worker_error(monkeypatch):
    import tradingbot.sentiment_sources as ss

    def boom(url, timeout_s, deadline):
        raise ConnectionRefusedError("x")

    monkeypatch.setattr(ss, "_urllib_get", boom)
    with pytest.raises(ConnectionRefusedError):
        ss.default_http_get("http://127.0.0.1:9/", 1.0)
