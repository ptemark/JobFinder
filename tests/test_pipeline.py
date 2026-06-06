"""Tests for the poll pipeline orchestration (LLD §8, task T17).

The pipeline is exercised end-to-end over the committed Greenhouse and Lever
fixtures with the real default embedding model (the session ``embed_model``
fixture — the one sanctioned, cached-after-first-run model load). HTTP is served
by ``httpx.MockTransport`` so no network is touched. These assert that the poll
stores ranked, scored, eligible jobs; isolates a failing source; keeps ineligible
postings flagged rather than dropping them; and re-runs idempotently while
skipping re-embedding for unchanged postings (LLD §6.4 / §8).
"""

from __future__ import annotations

import json
import logging
import random
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobfinder.models import RawPosting
from jobfinder.pipeline import run_poll
from jobfinder.settings import CompanyEntry, Settings
from jobfinder.sources.base import SourceResult
from jobfinder.sources.greenhouse import GreenhouseSource
from jobfinder.sources.http import HttpClient
from jobfinder.sources.lever import LeverSource
from jobfinder.store import connect, init_db, latest_run, start_run

FIXTURES = Path(__file__).parent / "fixtures"
# Same reference instant as the source tests: the fresh fixtures sit within the
# 21-day window, the 2026-01 ones are stale, so each source yields 2 postings.
_NOW = datetime(2026, 6, 3, tzinfo=UTC)
_NEXT_DAY = datetime(2026, 6, 4, tzinfo=UTC)


# --- Config + source scaffolding -------------------------------------------


def _make_base_dir(tmp_path: Path) -> Path:
    """Lay down a runnable config/ tree (profile, weights, résumé) under tmp_path."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(FIXTURES / "config" / "profile.yaml", config_dir / "profile.yaml")
    shutil.copy(FIXTURES / "config" / "weights.yaml", config_dir / "weights.yaml")
    # profile.yaml's resume_path points at config/resume.txt (LLD §11.1).
    shutil.copy(FIXTURES / "resume.txt", config_dir / "resume.txt")
    return tmp_path


def _mock_client(tmp_path: Path, handler) -> HttpClient:
    return HttpClient(
        cache_dir=tmp_path / "cache",
        throttle_s=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _dt: None,
        rng=random.Random(0),
    )


def _greenhouse_source(tmp_path: Path) -> GreenhouseSource:
    body = json.loads((FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return GreenhouseSource(
        companies=[CompanyEntry(token="acme", name="Acme")],
        client=_mock_client(tmp_path / "gh", handler),
        now=lambda: _NOW,
    )


def _lever_source(tmp_path: Path) -> LeverSource:
    body = json.loads((FIXTURES / "lever_postings.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("skip", "0"))
        limit = int(request.url.params.get("limit", "100"))
        return httpx.Response(200, json=body[skip : skip + limit])

    return LeverSource(
        companies=[CompanyEntry(token="acme", name="Acme Co")],
        client=_mock_client(tmp_path / "lever", handler),
        now=lambda: _NOW,
    )


class _ListSource:
    """A source that returns a fixed set of already-recency-filtered postings."""

    def __init__(self, name: str, raw: list[RawPosting]) -> None:
        self.name = name
        self._raw = raw

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        return SourceResult(
            source=self.name,
            raw=self._raw,
            fetched=len(self._raw),
            kept_after_recency=len(self._raw),
        )


class _BoomSource:
    """A source whose fetch always raises, to exercise the bulkhead (LLD §8)."""

    name = "boom"

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        raise RuntimeError("provider exploded")


class _KillSource:
    """A source whose fetch raises ``KeyboardInterrupt`` — simulating the process
    being killed mid-poll. The bulkhead deliberately catches only ``Exception``
    (a provider error), so this ``BaseException`` propagates out of ``run_poll``
    just like a real interrupt, leaving the run row unfinished (LLD §8 / §12)."""

    name = "kill"

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        raise KeyboardInterrupt


def _rows(db_path: Path) -> list:
    """All jobs left-joined with their scores, ranked best-first."""
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT j.*, s.final AS final FROM jobs j "
            "LEFT JOIN scores s ON s.job_id = j.id "
            "ORDER BY s.final DESC NULLS LAST"
        ).fetchall()
    finally:
        conn.close()


# --- End-to-end ------------------------------------------------------------


def test_run_poll_stores_ranked_scored_eligible_jobs(tmp_path: Path, embed_model) -> None:
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    summary = run_poll(
        settings,
        sources=[_greenhouse_source(tmp_path), _lever_source(tmp_path)],
        model=embed_model,
        now=_NOW,
    )

    # Each source yields the fresh + date-unknown postings (stale dropped upstream).
    assert summary.per_source["greenhouse"].kept_after_recency == 2
    assert summary.per_source["lever"].kept_after_recency == 2
    assert summary.per_source["greenhouse"].eligible == 2
    assert summary.per_source["lever"].eligible == 2
    assert summary.per_source["greenhouse"].scored == 2
    assert summary.per_source["lever"].scored == 2

    rows = _rows(settings.db_path)
    assert len(rows) == 4
    assert all(row["eligible"] == 1 for row in rows)
    assert all(row["final"] is not None for row in rows)  # every eligible job scored
    assert all(row["embedding"] is not None for row in rows)

    # Ranking: a remote senior Java/AWS role tops the list and outscores the
    # Vancouver Python role (location + skill + recency all favour it).
    top = rows[0]
    assert top["location_bucket"] == "remote"
    vancouver = [r for r in rows if r["location_bucket"] == "vancouver"]
    assert vancouver
    assert top["final"] > max(r["final"] for r in vancouver)


def test_run_poll_isolates_a_failing_source(tmp_path: Path, embed_model) -> None:
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    summary = run_poll(
        settings,
        sources=[_BoomSource(), _greenhouse_source(tmp_path)],
        model=embed_model,
        now=_NOW,
    )

    # The failing source is recorded but the healthy one still completes.
    assert summary.per_source["boom"].error is not None
    assert "provider exploded" in summary.per_source["boom"].error
    assert summary.per_source["boom"].scored == 0
    assert summary.per_source["greenhouse"].eligible == 2

    rows = _rows(settings.db_path)
    assert len(rows) == 2  # only the healthy source's jobs landed


def test_run_poll_keeps_ineligible_postings_flagged(tmp_path: Path, embed_model) -> None:
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)
    # A US-only backend role survives recency but fails the location gate.
    us_only = RawPosting(
        source="greenhouse",
        source_id="us1",
        payload={
            "id": "us1",
            "title": "Senior Backend Engineer",
            "location": {"name": "New York, NY"},
            "content": "Java and AWS backend role.",
            "updated_at": "2026-05-30T00:00:00-04:00",
            "company_name": "Globex",
        },
        company_hint="Globex",
    )

    summary = run_poll(
        settings,
        sources=[_ListSource("greenhouse", [us_only])],
        model=embed_model,
        now=_NOW,
    )

    assert summary.per_source["greenhouse"].eligible == 0
    assert summary.per_source["greenhouse"].scored == 0

    rows = _rows(settings.db_path)
    assert len(rows) == 1  # ineligible job is stored, not dropped (LLD §5)
    assert rows[0]["eligible"] == 0
    assert rows[0]["ineligible_reason"] == "location_out"
    assert rows[0]["final"] is None  # never scored
    assert rows[0]["embedding"] is None


def test_run_poll_is_idempotent_and_skips_reembedding(tmp_path: Path, embed_model) -> None:
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    run_poll(
        settings,
        sources=[_greenhouse_source(tmp_path), _lever_source(tmp_path)],
        model=embed_model,
        now=_NOW,
    )
    first = _rows(settings.db_path)
    first_seen = {row["id"]: row["first_seen_at"] for row in first}

    # Re-poll the same data a day later.
    second_summary = run_poll(
        settings,
        sources=[_greenhouse_source(tmp_path), _lever_source(tmp_path)],
        model=embed_model,
        now=_NEXT_DAY,
    )
    second = _rows(settings.db_path)

    # Idempotent: no duplicate rows.
    assert len(second) == len(first) == 4
    # Unchanged content → re-embedding skipped (LLD §6.4): nothing re-scored.
    assert second_summary.per_source["greenhouse"].scored == 0
    assert second_summary.per_source["lever"].scored == 0
    assert all(row["embedding"] is not None for row in second)  # embeddings preserved

    # first_seen_at preserved (new-since-last-poll derivable), last_seen_at bumped.
    for row in second:
        assert row["first_seen_at"] == first_seen[row["id"]]
        assert row["first_seen_at"] == _NOW.isoformat()
        assert row["last_seen_at"] == _NEXT_DAY.isoformat()


def test_run_poll_prunes_jobs_not_seen_within_retention(tmp_path: Path, embed_model) -> None:
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    # First poll seeds the jobs.
    run_poll(
        settings,
        sources=[_greenhouse_source(tmp_path)],
        model=embed_model,
        now=_NOW,
    )
    assert len(_rows(settings.db_path)) == 2

    # A much later poll that sees nothing prunes the now-stale (unseen) rows
    # (retention_days default 30, LLD §8 / §11.4).
    far_future = datetime(2026, 8, 1, tzinfo=UTC)
    summary = run_poll(
        settings,
        sources=[_ListSource("greenhouse", [])],
        model=embed_model,
        now=far_future,
    )

    assert summary.pruned == 2
    assert _rows(settings.db_path) == []


def test_run_poll_finishes_a_reserved_run_id(tmp_path: Path, embed_model) -> None:
    # The dashboard's POST /api/poll reserves a run row, then spawns the poll to
    # finish that same row (LLD §9.1) — so a passed run_id must be reused, not
    # duplicated.
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    conn = connect(settings.db_path)
    try:
        init_db(conn)
        reserved = start_run(conn, now=_NOW)
    finally:
        conn.close()

    summary = run_poll(
        settings,
        sources=[_ListSource("greenhouse", [])],
        model=embed_model,
        now=_NEXT_DAY,
        run_id=reserved,
    )

    assert summary.run_id == reserved
    conn = connect(settings.db_path)
    try:
        runs = conn.execute("SELECT id, finished_at FROM poll_runs ORDER BY id").fetchall()
    finally:
        conn.close()
    # Exactly one run row — the reserved one — and it is now finished.
    assert [r["id"] for r in runs] == [reserved]
    assert runs[0]["finished_at"] == _NEXT_DAY.isoformat()


def test_run_poll_crash_mid_poll_leaves_db_consistent_and_recovers(
    tmp_path: Path, embed_model
) -> None:
    # T26 hardening: a kill mid-poll (after one source has persisted its jobs)
    # must leave the DB consistent — whole rows, no partials, no duplicates — and
    # an idempotent re-run must recover (LLD §8 / §12). Each store write commits
    # per-job, so the killed poll's committed rows are intact and its run row is
    # left unfinished.
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    with pytest.raises(KeyboardInterrupt):
        run_poll(
            settings,
            sources=[_greenhouse_source(tmp_path), _KillSource()],
            model=embed_model,
            now=_NOW,
        )

    # Greenhouse's 2 eligible jobs are committed whole (scored + embedded); the
    # interrupted run was never finished, so the dashboard sees no "latest" poll.
    partial = _rows(settings.db_path)
    assert len(partial) == 2
    assert all(row["embedding"] is not None for row in partial)
    assert all(row["final"] is not None for row in partial)
    conn = connect(settings.db_path)
    try:
        assert latest_run(conn) is None  # killed run has no finished_at
    finally:
        conn.close()

    # A clean re-run recovers: every source completes, the previously persisted
    # greenhouse jobs are upserted in place (no duplicates, re-embed skipped),
    # and lever's jobs land too.
    summary = run_poll(
        settings,
        sources=[_greenhouse_source(tmp_path), _lever_source(tmp_path)],
        model=embed_model,
        now=_NEXT_DAY,
    )

    recovered = _rows(settings.db_path)
    assert len(recovered) == 4  # 2 greenhouse (idempotent) + 2 lever, no dupes
    assert all(row["final"] is not None for row in recovered)
    assert summary.per_source["greenhouse"].scored == 0  # unchanged → re-embed skipped
    assert summary.per_source["lever"].scored == 2
    conn = connect(settings.db_path)
    try:
        assert latest_run(conn) is not None  # the recovery run finished cleanly
    finally:
        conn.close()


def test_run_poll_logs_the_funnel_per_source(
    tmp_path: Path, embed_model, caplog: pytest.LogCaptureFixture
) -> None:
    # T26: the per-source count funnel (LLD §12) must be logged for each source.
    base = _make_base_dir(tmp_path)
    settings = Settings(base_dir=base)

    with caplog.at_level(logging.INFO, logger="jobfinder.pipeline"):
        run_poll(
            settings,
            sources=[_greenhouse_source(tmp_path)],
            model=embed_model,
            now=_NOW,
        )

    funnels = [r.getMessage() for r in caplog.records if "funnel" in r.getMessage()]
    assert any("greenhouse funnel: fetched=4 kept=2 eligible=2 scored=2" in msg for msg in funnels)
