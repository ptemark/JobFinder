"""Tests for the source protocol & registry (LLD §3.1, task T08).

These exercise the registry mechanism with in-test fake sources (no network,
no real adapters yet — those land in T11/T12/T21/T22). Each test passes its own
``registry`` dict so the global :data:`SOURCES` is never polluted.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobfinder.models import LocationBucket, RawPosting
from jobfinder.normalize import normalize
from jobfinder.settings import CompanyEntry, Settings
from jobfinder.sources.ashby import AshbySource
from jobfinder.sources.base import (
    Source,
    SourceFactory,
    SourceResult,
    build_sources,
    register_source,
)
from jobfinder.sources.greenhouse import GreenhouseSource
from jobfinder.sources.http import HttpClient
from jobfinder.sources.lever import LeverSource

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSource:
    """A trivial in-test adapter that returns a fixed empty result."""

    def __init__(self, name: str) -> None:
        self.name = name

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        return SourceResult(source=self.name)


class OptionalKeyedSource:
    """Mimics Adzuna (LLD §3.6): constructible without its secret, but ``fetch``
    skips cleanly with a note instead of raising when the key is absent."""

    name = "adzuna"

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.adzuna_enabled

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        if not self._enabled:
            return SourceResult(
                source=self.name,
                errors=["skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not set"],
            )
        return SourceResult(source=self.name, fetched=1)


def _factory(name: str) -> SourceFactory:
    return lambda _settings: FakeSource(name)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(base_dir=tmp_path)


def test_source_result_defaults() -> None:
    res = SourceResult(source="greenhouse")
    assert res.raw == []
    assert res.fetched == 0
    assert res.kept_after_recency == 0
    assert res.errors == []


def test_fake_source_satisfies_protocol() -> None:
    assert isinstance(FakeSource("greenhouse"), Source)


def test_build_sources_single_entry(settings: Settings) -> None:
    registry: dict[str, SourceFactory] = {"greenhouse": _factory("greenhouse")}
    sources = build_sources(settings, registry=registry)
    assert [s.name for s in sources] == ["greenhouse"]


def test_build_sources_returns_all_registered(settings: Settings) -> None:
    registry = {name: _factory(name) for name in ("greenhouse", "lever", "ashby")}
    names = {s.name for s in build_sources(settings, registry=registry)}
    assert names == {"greenhouse", "lever", "ashby"}


def test_build_sources_only_selects_subset(settings: Settings) -> None:
    registry = {name: _factory(name) for name in ("greenhouse", "lever", "ashby")}
    sources = build_sources(settings, only=["lever"], registry=registry)
    assert [s.name for s in sources] == ["lever"]


def test_build_sources_unknown_name_raises(settings: Settings) -> None:
    registry = {"greenhouse": _factory("greenhouse")}
    with pytest.raises(ValueError, match="unknown source"):
        build_sources(settings, only=["nope"], registry=registry)


def test_register_source_global_then_unregister() -> None:
    """The module-level register_source actually mutates the shared registry,
    and re-registering the same name overwrites rather than duplicating."""
    from jobfinder.sources import base

    sentinel = object()
    assert "test_fake" not in base.SOURCES
    try:
        register_source("test_fake", lambda _s: FakeSource("test_fake"))
        assert "test_fake" in base.SOURCES
        register_source("test_fake", lambda _s: sentinel)  # type: ignore[arg-type,return-value]
        assert base.SOURCES["test_fake"](None) is sentinel  # type: ignore[arg-type]
    finally:
        base.SOURCES.pop("test_fake", None)


def test_optional_source_missing_secret_returns_empty(settings: Settings) -> None:
    """A keyed optional source is constructible without its secret and its fetch
    returns an empty result with a note rather than raising (LLD §3.1, §3.6)."""
    assert settings.adzuna_enabled is False  # no keys in a bare Settings
    registry: dict[str, SourceFactory] = {"adzuna": OptionalKeyedSource}
    (source,) = build_sources(settings, registry=registry)
    result = source.fetch(max_age_days=21, throttle_s=1.0)
    assert result.source == "adzuna"
    assert result.raw == []
    assert result.fetched == 0
    assert result.errors  # carries a skip note


def test_optional_source_with_secret_runs(tmp_path) -> None:
    """With both keys present the same optional source no longer self-skips."""
    settings = Settings(base_dir=tmp_path, adzuna_app_id="id", adzuna_app_key="key")
    assert settings.adzuna_enabled is True
    (source,) = build_sources(settings, registry={"adzuna": OptionalKeyedSource})
    result = source.fetch(max_age_days=21, throttle_s=1.0)
    assert result.errors == []
    assert result.fetched == 1


# --- Greenhouse adapter (T11) ----------------------------------------------

# Pins the recency pre-filter clock: fixture has a fresh (2026-05-30), a stale
# (2026-01-05), and a date-unknown posting relative to this "now".
_NOW = datetime(2026, 6, 3, tzinfo=UTC)


def _greenhouse_handler(boards: dict[str, httpx.Response]):
    """A MockTransport handler mapping a board token to a canned response."""

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.path.split("/")[3]  # /v1/boards/{token}/jobs
        if token not in boards:
            return httpx.Response(404, json={"error": "not found"})
        return boards[token]

    return handler


def _greenhouse_client(tmp_path: Path, boards: dict[str, httpx.Response]) -> HttpClient:
    return HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        transport=httpx.MockTransport(_greenhouse_handler(boards)),
        sleep=lambda _dt: None,
        rng=random.Random(0),
    )


def _fixture_response() -> httpx.Response:
    body = (FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8")
    return httpx.Response(200, json=json.loads(body))


def test_greenhouse_fetch_parses_fixture(tmp_path: Path) -> None:
    client = _greenhouse_client(tmp_path, {"acme": _fixture_response()})
    source = GreenhouseSource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert result.source == "greenhouse"
    assert all(isinstance(rp, RawPosting) for rp in result.raw)
    assert all(rp.source == "greenhouse" for rp in result.raw)
    # 4 postings returned by the provider; the id-less one is skipped+noted.
    assert result.fetched == 4
    assert any("no id" in note for note in result.errors)


def test_greenhouse_recency_prefilter_drops_stale(tmp_path: Path) -> None:
    client = _greenhouse_client(tmp_path, {"acme": _fixture_response()})
    source = GreenhouseSource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    kept_ids = {rp.source_id for rp in result.raw}
    # Fresh (4012001) and date-unknown (4012003) survive; stale (4012002) drops.
    assert kept_ids == {"4012001", "4012003"}
    assert result.kept_after_recency == 2


def test_greenhouse_raw_posting_normalizes(tmp_path: Path) -> None:
    """The recency-filtered RawPostings feed normalize cleanly (LLD §4 contract)."""
    client = _greenhouse_client(tmp_path, {"acme": _fixture_response()})
    source = GreenhouseSource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)
    by_id = {rp.source_id: rp for rp in result.raw}

    fresh = normalize(by_id["4012001"], company_hint="Acme", now=_NOW)
    assert fresh.company == "Acme"
    assert fresh.title == "Senior Backend Engineer"
    assert "Java" in fresh.description  # HTML-entity content decoded + stripped
    assert fresh.date_unknown is False

    undated = normalize(by_id["4012003"], company_hint="Acme", now=_NOW)
    assert undated.date_unknown is True  # null updated_at -> kept, flagged


def test_greenhouse_board_error_is_isolated(tmp_path: Path) -> None:
    """One board 404ing is recorded but never loses the healthy board's jobs."""
    client = _greenhouse_client(tmp_path, {"acme": _fixture_response()})  # "boom" absent -> 404
    source = GreenhouseSource(
        companies=[CompanyEntry(token="boom"), CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert {rp.source_id for rp in result.raw} == {"4012001", "4012003"}
    assert any("boom" in note and "fetch failed" in note for note in result.errors)


def test_greenhouse_unexpected_shape_noted(tmp_path: Path) -> None:
    """A payload without a jobs list is noted, not fatal."""
    bad = httpx.Response(200, json={"unexpected": True})
    client = _greenhouse_client(tmp_path, {"acme": bad})
    source = GreenhouseSource(
        companies=[CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert result.raw == []
    assert result.fetched == 0
    assert any("unexpected payload shape" in note for note in result.errors)


# --- Ashby adapter (T21) ----------------------------------------------------


def _ashby_handler(boards: dict[str, httpx.Response]):
    """A MockTransport handler mapping a board token to a canned response."""

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.path.split("/")[3]  # /posting-api/job-board/{token}
        if token not in boards:
            return httpx.Response(404, json={"error": "not found"})
        return boards[token]

    return handler


def _ashby_client(tmp_path: Path, boards: dict[str, httpx.Response]) -> HttpClient:
    return HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        transport=httpx.MockTransport(_ashby_handler(boards)),
        sleep=lambda _dt: None,
        rng=random.Random(0),
    )


def _ashby_fixture_response() -> httpx.Response:
    body = (FIXTURES / "ashby_jobs.json").read_text(encoding="utf-8")
    return httpx.Response(200, json=json.loads(body))


def test_ashby_fetch_parses_fixture(tmp_path: Path) -> None:
    client = _ashby_client(tmp_path, {"acme": _ashby_fixture_response()})
    source = AshbySource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert result.source == "ashby"
    assert all(isinstance(rp, RawPosting) for rp in result.raw)
    assert all(rp.source == "ashby" for rp in result.raw)
    # 4 postings returned by the provider; the id-less one is skipped+noted.
    assert result.fetched == 4
    assert any("no id" in note for note in result.errors)


def test_ashby_recency_prefilter_drops_stale(tmp_path: Path) -> None:
    client = _ashby_client(tmp_path, {"acme": _ashby_fixture_response()})
    source = AshbySource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    kept_ids = {rp.source_id for rp in result.raw}
    # Fresh (ash-1001) and date-unknown (ash-1003) survive; stale (ash-1002,
    # whose only date is a January updatedAt) drops.
    assert kept_ids == {"ash-1001", "ash-1003"}
    assert result.kept_after_recency == 2


def test_ashby_remote_workplace_type_sets_is_remote(tmp_path: Path) -> None:
    """``workplaceType == "Remote"`` is the strong remote signal (LLD §3.5);
    the recency-filtered RawPostings also feed normalize cleanly (LLD §4)."""
    client = _ashby_client(tmp_path, {"acme": _ashby_fixture_response()})
    source = AshbySource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)
    by_id = {rp.source_id: rp for rp in result.raw}

    fresh = normalize(by_id["ash-1001"], company_hint="Acme", now=_NOW)
    assert fresh.company == "Acme"  # supplied hint, not in payload
    assert fresh.title == "Senior Backend Engineer"
    assert fresh.is_remote is True  # workplaceType == "Remote"
    assert fresh.location_bucket is LocationBucket.REMOTE
    assert fresh.description == "Build backend services in Java and AWS."  # descriptionPlain
    assert fresh.url == "https://jobs.ashbyhq.com/acme/ash-1001"
    assert fresh.posted_at is not None  # publishedAt parsed
    assert fresh.date_unknown is False

    undated = normalize(by_id["ash-1003"], company_hint="Acme", now=_NOW)
    assert undated.is_remote is False  # workplaceType == "Hybrid"
    assert undated.date_unknown is True  # both publishedAt/updatedAt null
    assert undated.description == "Python services team."  # descriptionHtml stripped


def test_ashby_board_error_is_isolated(tmp_path: Path) -> None:
    """One board 404ing is recorded but never loses the healthy board's jobs."""
    client = _ashby_client(tmp_path, {"acme": _ashby_fixture_response()})  # "boom" -> 404
    source = AshbySource(
        companies=[CompanyEntry(token="boom"), CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert {rp.source_id for rp in result.raw} == {"ash-1001", "ash-1003"}
    assert any("boom" in note and "fetch failed" in note for note in result.errors)


def test_ashby_unexpected_shape_noted(tmp_path: Path) -> None:
    """A payload without a jobs list is noted, not fatal."""
    bad = httpx.Response(200, json={"unexpected": True})
    client = _ashby_client(tmp_path, {"acme": bad})
    source = AshbySource(
        companies=[CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert result.raw == []
    assert result.fetched == 0
    assert any("unexpected payload shape" in note for note in result.errors)


# --- Lever adapter (T12) ----------------------------------------------------


def _lever_handler(sites: dict[str, list[dict]], calls: list[str] | None = None):
    """A MockTransport handler serving a site's postings array with skip/limit
    pagination, so the adapter's paging is exercised exactly as in production."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        token = request.url.path.split("/")[3]  # /v0/postings/{token}
        if token not in sites:
            return httpx.Response(404, json={"error": "not found"})
        skip = int(request.url.params.get("skip", "0"))
        limit = int(request.url.params.get("limit", "100"))
        return httpx.Response(200, json=sites[token][skip : skip + limit])

    return handler


def _lever_client(
    tmp_path: Path, sites: dict[str, list[dict]], calls: list[str] | None = None
) -> HttpClient:
    return HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        transport=httpx.MockTransport(_lever_handler(sites, calls)),
        sleep=lambda _dt: None,
        rng=random.Random(0),
    )


def _lever_fixture() -> list[dict]:
    body = (FIXTURES / "lever_postings.json").read_text(encoding="utf-8")
    return json.loads(body)


def test_lever_fetch_parses_fixture(tmp_path: Path) -> None:
    client = _lever_client(tmp_path, {"acme": _lever_fixture()})
    source = LeverSource(
        companies=[CompanyEntry(token="acme", name="Acme Co")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert result.source == "lever"
    assert all(isinstance(rp, RawPosting) for rp in result.raw)
    assert all(rp.source == "lever" for rp in result.raw)
    # 4 postings returned by the provider; the id-less one is skipped+noted.
    assert result.fetched == 4
    assert any("no id" in note for note in result.errors)


def test_lever_recency_prefilter_drops_stale(tmp_path: Path) -> None:
    client = _lever_client(tmp_path, {"acme": _lever_fixture()})
    source = LeverSource(
        companies=[CompanyEntry(token="acme", name="Acme Co")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    kept_ids = {rp.source_id for rp in result.raw}
    # Fresh (abc-0001) and date-unknown (abc-0003) survive; stale (abc-0002) drops.
    assert kept_ids == {"abc-0001", "abc-0003"}
    assert result.kept_after_recency == 2


def test_lever_raw_posting_normalizes_with_company_hint(tmp_path: Path) -> None:
    """The recency-filtered RawPostings feed normalize cleanly; the company name
    comes from the configured site (Lever payloads carry none, LLD §3.4)."""
    client = _lever_client(tmp_path, {"acme": _lever_fixture()})
    source = LeverSource(
        companies=[CompanyEntry(token="acme", name="Acme Co")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)
    by_id = {rp.source_id: rp for rp in result.raw}

    fresh = normalize(by_id["abc-0001"], company_hint="Acme Co", now=_NOW)
    assert fresh.company == "Acme Co"  # supplied hint, not in payload
    assert fresh.title == "Senior Backend Engineer"
    assert fresh.description == "Build backend services in Java and AWS."  # descriptionPlain
    assert fresh.url == "https://jobs.lever.co/acme/abc-0001"
    assert fresh.posted_at is not None  # epoch-ms createdAt parsed
    assert fresh.posted_at.year == 2026 and fresh.posted_at.month == 5
    assert fresh.date_unknown is False

    undated = normalize(by_id["abc-0003"], company_hint="Acme Co", now=_NOW)
    assert undated.date_unknown is True  # null createdAt -> kept, flagged
    assert undated.description == "Python services team."  # HTML description stripped


def test_lever_pagination_stops_on_short_page(tmp_path: Path) -> None:
    """With a small page size the adapter walks pages via ``skip`` and stops once
    a page shorter than the limit is returned — no extra request is made."""
    postings = [
        {"id": "p1", "text": "Backend Engineer", "createdAt": 1780150920000},
        {"id": "p2", "text": "Backend Engineer", "createdAt": 1780150920000},
        {"id": "p3", "text": "Backend Engineer", "createdAt": 1780150920000},
    ]
    calls: list[str] = []
    client = _lever_client(tmp_path, {"acme": postings}, calls)
    source = LeverSource(
        companies=[CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
        page_limit=2,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert {rp.source_id for rp in result.raw} == {"p1", "p2", "p3"}
    assert result.fetched == 3
    # Page 0 (skip=0, full) then page 1 (skip=2, short) — exactly two requests.
    assert len(calls) == 2
    assert "skip=0" in calls[0]
    assert "skip=2" in calls[1]


def test_lever_site_error_is_isolated(tmp_path: Path) -> None:
    """One site 404ing is recorded but never loses the healthy site's postings."""
    client = _lever_client(tmp_path, {"acme": _lever_fixture()})  # "boom" absent -> 404
    source = LeverSource(
        companies=[CompanyEntry(token="boom"), CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert {rp.source_id for rp in result.raw} == {"abc-0001", "abc-0003"}
    assert any("boom" in note and "fetch failed" in note for note in result.errors)


def test_lever_unexpected_shape_noted(tmp_path: Path) -> None:
    """A payload that is not a JSON array is noted, not fatal."""
    bad = httpx.Response(200, json={"unexpected": True})
    client = HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        transport=httpx.MockTransport(lambda _req: bad),
        sleep=lambda _dt: None,
        rng=random.Random(0),
    )
    source = LeverSource(
        companies=[CompanyEntry(token="acme")],
        client=client,
        now=lambda: _NOW,
    )

    result = source.fetch(max_age_days=21, throttle_s=1.0)

    assert result.raw == []
    assert result.fetched == 0
    assert any("unexpected payload shape" in note for note in result.errors)
