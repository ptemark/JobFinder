"""Tests for the source protocol & registry (LLD §3.1, task T08).

These exercise the registry mechanism with in-test fake sources (no network,
no real adapters yet — those land in T11/T12/T21/T22). Each test passes its own
``registry`` dict so the global :data:`SOURCES` is never polluted.
"""

from __future__ import annotations

import pytest

from jobfinder.settings import Settings
from jobfinder.sources.base import (
    Source,
    SourceFactory,
    SourceResult,
    build_sources,
    register_source,
)


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
