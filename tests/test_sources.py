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

from jobfinder.models import RawPosting
from jobfinder.normalize import normalize
from jobfinder.settings import CompanyEntry, Settings
from jobfinder.sources.base import (
    Source,
    SourceFactory,
    SourceResult,
    build_sources,
    register_source,
)
from jobfinder.sources.greenhouse import GreenhouseSource
from jobfinder.sources.http import HttpClient

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
