"""Tests for the shared HTTP client (LLD §3.2): retry, cache, throttle.

All tests run offline via ``httpx.MockTransport`` and a fake clock — no real
network, no real sleeping (Testing Standards: fixtures only, deterministic).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import pytest

from jobfinder.sources.http import HttpClient


class FakeClock:
    """A monotonic clock whose ``sleep`` advances time, for throttle/backoff tests."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.t += dt


def make_client(tmp_path: Path, handler, *, throttle_s: float = 0.0, **kwargs) -> HttpClient:
    """Build an HttpClient wired to a MockTransport and a no-op sleep by default."""
    kwargs.setdefault("sleep", lambda _dt: None)
    return HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=throttle_s,
        transport=httpx.MockTransport(handler),
        rng=random.Random(0),
        **kwargs,
    )


def test_get_json_parses_body(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hello": "world"})

    with make_client(tmp_path, handler) as client:
        assert client.get_json("https://example.com/jobs") == {"hello": "world"}


def test_retry_on_503_then_succeeds(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    with make_client(tmp_path, handler) as client:
        assert client.get_json("https://example.com/jobs") == {"ok": True}
    assert calls["n"] == 2  # one retry after the 503


def test_retry_exhausted_raises(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_text("https://example.com/jobs")
    assert calls["n"] == 3  # MAX_ATTEMPTS


def test_non_retryable_status_raises_immediately(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_text("https://example.com/missing")
    assert calls["n"] == 1  # 404 is not retried


def test_timeout_retried_then_raised(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.get_text("https://example.com/slow")
    assert calls["n"] == 3  # retried up to MAX_ATTEMPTS, then re-raised


def test_cache_hit_avoids_second_transport_call(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    with make_client(tmp_path, handler) as client:
        first = client.get_json("https://example.com/jobs")
        second = client.get_json("https://example.com/jobs")

    assert calls["n"] == 1  # second call served from disk cache
    assert first == second == {"n": 1}


def test_no_cache_bypasses_cache(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    with make_client(tmp_path, handler, no_cache=True) as client:
        client.get_json("https://example.com/jobs")
        client.get_json("https://example.com/jobs")
    assert calls["n"] == 2  # nothing cached or read


def test_distinct_params_are_cached_separately(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    with make_client(tmp_path, handler) as client:
        client.get_json("https://example.com/jobs", params={"page": 1})
        client.get_json("https://example.com/jobs", params={"page": 2})
    assert calls["n"] == 2  # different query string → different cache key


def test_corrupt_cache_file_is_a_miss(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Seed every possible cache file with garbage so any lookup is corrupt.
    client = make_client(tmp_path, handler)
    path = client._cache_path(httpx.URL("https://example.com/jobs"))
    path.write_text("{not valid json", encoding="utf-8")

    with client:
        assert client.get_json("https://example.com/jobs") == {"ok": True}
    assert calls["n"] == 1  # refetched despite the corrupt file present


def test_throttle_enforces_min_spacing(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=1.0,
        no_cache=True,  # force both calls onto the network path
        transport=httpx.MockTransport(handler),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    with client:
        client.get_json("https://example.com/a")
        client.get_json("https://example.com/a")

    # The second same-host call had to wait the full throttle window.
    assert clock.sleeps == [1.0]
    assert clock.t == pytest.approx(1.0)


def test_throttle_is_per_host(tmp_path: Path) -> None:
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=1.0,
        no_cache=True,
        transport=httpx.MockTransport(handler),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    with client:
        client.get_json("https://host-one.com/a")
        client.get_json("https://host-two.com/a")
    assert clock.sleeps == []  # different hosts never throttle each other


def test_retry_after_header_honored_on_429(tmp_path: Path) -> None:
    clock = FakeClock()
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    client = HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        transport=httpx.MockTransport(handler),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    with client:
        assert client.get_json("https://example.com/jobs") == {"ok": True}
    assert clock.sleeps == [7.0]  # waited exactly the Retry-After delay


def test_expired_cache_entry_refetched(tmp_path: Path) -> None:
    calls = {"n": 0}
    wall = {"t": 1000.0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    client = HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        cache_ttl_s=100,
        transport=httpx.MockTransport(handler),
        sleep=lambda _dt: None,
        wall_clock=lambda: wall["t"],
        rng=random.Random(0),
    )
    with client:
        client.get_json("https://example.com/jobs")
        wall["t"] += 101  # advance past the TTL
        second = client.get_json("https://example.com/jobs")
    assert calls["n"] == 2
    assert second == {"n": 2}


def test_cache_file_shape(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"a": 1})

    with make_client(tmp_path, handler) as client:
        client.get_json("https://example.com/jobs")
        path = client._cache_path(httpx.URL("https://example.com/jobs"))
        entry = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry) == {"fetched_at", "body"}
    assert json.loads(entry["body"]) == {"a": 1}
