"""Tests for the dashboard JSON API (LLD §9, task T18).

The API is exercised with FastAPI's ``TestClient`` over a temp-file DB seeded
directly through the store layer — no embedding model, no network. These assert
the list filters/sort, the ``include_ineligible`` debug toggle, status writes
that persist across a fresh client, the detail breakdown, the new-since-last-poll
flag, and the latest-run summary.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobfinder.models import Job, LocationBucket, ScoreBreakdown, Seniority, make_job_id
from jobfinder.settings import Settings
from jobfinder.store import (
    connect,
    finish_run,
    init_db,
    save_score,
    start_run,
    upsert_job,
)
from jobfinder.web.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"

_NOW = datetime(2026, 6, 4, tzinfo=UTC)
# Two prior poll runs; the previous one finished at _RUN1_FIN, which is the
# new-since-last-poll threshold (LLD §7.3).
_RUN1_FIN = datetime(2026, 6, 2, tzinfo=UTC)
_RUN2_FIN = datetime(2026, 6, 4, tzinfo=UTC)


def _job(
    source_id: str,
    *,
    source: str = "greenhouse",
    company: str = "Acme",
    title: str = "Senior Backend Engineer",
    description: str = "Java and AWS backend role.",
    bucket: LocationBucket = LocationBucket.REMOTE,
    seniority: Seniority = Seniority.SENIOR,
    is_remote: bool = True,
    posted_at: datetime | None = _NOW,
    date_unknown: bool = False,
    eligible: bool = True,
    ineligible_reason: str | None = None,
    first_seen: datetime = _NOW,
) -> Job:
    return Job(
        id=make_job_id(source, source_id),
        source=source,
        source_id=source_id,
        company=company,
        title=title,
        description=description,
        location_raw="Remote",
        is_remote=is_remote,
        location_bucket=bucket,
        seniority=seniority,
        url=f"https://example.test/{source_id}",
        posted_at=posted_at,
        date_unknown=date_unknown,
        first_seen_at=first_seen,
        last_seen_at=_NOW,
        eligible=eligible,
        ineligible_reason=ineligible_reason,
        content_hash="h",
    )


def _score(final: float) -> ScoreBreakdown:
    return ScoreBreakdown(
        final=final,
        semantic=0.5,
        skill=0.5,
        location=1.0,
        recency=0.9,
        scored_at=_NOW,
    )


# Seeded jobs: id -> (Job, score|None, status|None)
def _seed(settings: Settings) -> None:
    conn = connect(settings.db_path)
    try:
        init_db(conn)

        # A: top-scored remote senior Java/AWS, posted yesterday, newly seen.
        a = _job(
            "a",
            title="Senior Backend Engineer",
            description="Java and AWS.",
            bucket=LocationBucket.REMOTE,
            posted_at=datetime(2026, 6, 3, tzinfo=UTC),
            first_seen=datetime(2026, 6, 3, tzinfo=UTC),
        )
        # B: mid-scored Vancouver Python, posted 10 days ago, seen before last poll.
        b = _job(
            "b",
            title="Senior Python Developer",
            description="Python backend.",
            bucket=LocationBucket.VANCOUVER,
            is_remote=False,
            posted_at=datetime(2026, 5, 25, tzinfo=UTC),
            first_seen=datetime(2026, 5, 20, tzinfo=UTC),
        )
        # C: low-scored Toronto, posted 3 days ago, newly seen.
        c = _job(
            "c",
            title="Backend Engineer",
            description="Java backend.",
            bucket=LocationBucket.TORONTO,
            is_remote=False,
            posted_at=datetime(2026, 6, 1, tzinfo=UTC),
            first_seen=datetime(2026, 6, 3, tzinfo=UTC),
        )
        # D: ineligible (US-only), unscored, on a different source.
        d = _job(
            "d",
            source="lever",
            company="Globex",
            title="Backend Engineer",
            description="US only backend role.",
            bucket=LocationBucket.OTHER,
            is_remote=False,
            eligible=False,
            ineligible_reason="location_out",
            posted_at=datetime(2026, 6, 1, tzinfo=UTC),
            first_seen=datetime(2026, 5, 20, tzinfo=UTC),
        )

        for job in (a, b, c, d):
            upsert_job(conn, job)
        save_score(conn, a.id, _score(90.0))
        save_score(conn, b.id, _score(70.0))
        save_score(conn, c.id, _score(50.0))

        # Two finished runs so previous_run_finished_at = _RUN1_FIN (LLD §7.3).
        r1 = start_run(conn, now=datetime(2026, 6, 1, tzinfo=UTC))
        finish_run(conn, r1, {"greenhouse": {"fetched": 1}}, now=_RUN1_FIN)
        r2 = start_run(conn, now=datetime(2026, 6, 4, tzinfo=UTC))
        finish_run(conn, r2, {"greenhouse": {"fetched": 3}, "lever": {"fetched": 1}}, now=_RUN2_FIN)
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(FIXTURES / "config" / "profile.yaml", config_dir / "profile.yaml")
    settings = Settings(base_dir=tmp_path)
    _seed(settings)
    app = create_app(settings, now=lambda: _NOW)
    return TestClient(app)


# --- List: filters & sort ---------------------------------------------------


def test_list_default_hides_ineligible_and_totals(client: TestClient) -> None:
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    # D (ineligible) is hidden by default; A, B, C remain.
    assert body["total"] == 3
    ids = [item["id"] for item in body["items"]]
    assert make_job_id("lever", "d") not in ids


def test_list_sort_best_orders_by_score(client: TestClient) -> None:
    items = client.get("/api/jobs", params={"sort": "best"}).json()["items"]
    assert [i["score"] for i in items] == [90.0, 70.0, 50.0]


def test_list_sort_newest_orders_by_posted_date(client: TestClient) -> None:
    items = client.get("/api/jobs", params={"sort": "newest"}).json()["items"]
    # A (06-03), C (06-01), B (05-25).
    assert [i["id"] for i in items] == [
        make_job_id("greenhouse", "a"),
        make_job_id("greenhouse", "c"),
        make_job_id("greenhouse", "b"),
    ]


def test_list_filter_bucket(client: TestClient) -> None:
    items = client.get("/api/jobs", params={"bucket": "vancouver"}).json()["items"]
    assert [i["id"] for i in items] == [make_job_id("greenhouse", "b")]


def test_list_filter_min_score(client: TestClient) -> None:
    items = client.get("/api/jobs", params={"min_score": 60}).json()["items"]
    assert {i["id"] for i in items} == {
        make_job_id("greenhouse", "a"),
        make_job_id("greenhouse", "b"),
    }


def test_list_filter_source(client: TestClient) -> None:
    # Lever's only job is ineligible, so it appears only with the debug toggle.
    body = client.get("/api/jobs", params={"source": "lever", "include_ineligible": True}).json()
    assert [i["id"] for i in body["items"]] == [make_job_id("lever", "d")]


def test_include_ineligible_toggle_surfaces_filtered_job(client: TestClient) -> None:
    body = client.get("/api/jobs", params={"include_ineligible": True}).json()
    assert body["total"] == 4
    d = next(i for i in body["items"] if i["id"] == make_job_id("lever", "d"))
    assert d["score"] == 0.0  # unscored ineligible job surfaces as 0.0


def test_list_age_days_and_matched_skills(client: TestClient) -> None:
    items = client.get("/api/jobs", params={"sort": "best"}).json()["items"]
    top = items[0]
    assert top["age_days"] == 1  # posted 06-03, now 06-04
    assert set(top["matched_skills"]) == {"java", "aws"}


def test_list_new_since_last_poll_flag(client: TestClient) -> None:
    items = client.get("/api/jobs", params={"include_ineligible": True}).json()["items"]
    flags = {i["id"]: i["is_new_since_last_poll"] for i in items}
    # First-seen after the previous run finished (_RUN1_FIN = 06-02) → new.
    assert flags[make_job_id("greenhouse", "a")] is True
    assert flags[make_job_id("greenhouse", "c")] is True
    assert flags[make_job_id("greenhouse", "b")] is False
    assert flags[make_job_id("lever", "d")] is False


# --- Detail -----------------------------------------------------------------


def test_detail_returns_description_and_breakdown(client: TestClient) -> None:
    resp = client.get(f"/api/jobs/{make_job_id('greenhouse', 'a')}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["description"] == "Java and AWS."
    assert body["breakdown"]["final"] == 90.0
    assert set(body["breakdown"]) == {"final", "semantic", "skill", "location", "recency"}


def test_detail_unknown_job_404(client: TestClient) -> None:
    assert client.get("/api/jobs/deadbeef").status_code == 404


def test_detail_ineligible_has_empty_breakdown(client: TestClient) -> None:
    body = client.get(f"/api/jobs/{make_job_id('lever', 'd')}").json()
    assert body["breakdown"] == {}
    assert body["score"] == 0.0


# --- Status -----------------------------------------------------------------


def test_status_post_persists_across_fresh_client(client: TestClient, tmp_path: Path) -> None:
    job_id = make_job_id("greenhouse", "a")
    resp = client.post(f"/api/jobs/{job_id}/status", json={"state": "dismissed"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # A brand-new app/client over the same DB sees the persisted status.
    fresh = TestClient(create_app(Settings(base_dir=tmp_path), now=lambda: _NOW))
    detail = fresh.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "dismissed"


def test_status_filter_and_dismissed_hidden_from_new(client: TestClient) -> None:
    job_id = make_job_id("greenhouse", "a")
    client.post(f"/api/jobs/{job_id}/status", json={"state": "dismissed"})

    dismissed = client.get("/api/jobs", params={"status": "dismissed"}).json()["items"]
    assert [i["id"] for i in dismissed] == [job_id]

    # Untouched jobs read as 'new'; the dismissed one is excluded.
    new = client.get("/api/jobs", params={"status": "new"}).json()["items"]
    new_ids = {i["id"] for i in new}
    assert job_id not in new_ids
    assert make_job_id("greenhouse", "b") in new_ids


def test_status_invalid_state_422(client: TestClient) -> None:
    job_id = make_job_id("greenhouse", "a")
    assert client.post(f"/api/jobs/{job_id}/status", json={"state": "bogus"}).status_code == 422


def test_status_unknown_job_404(client: TestClient) -> None:
    assert client.post("/api/jobs/deadbeef/status", json={"state": "applied"}).status_code == 404


# --- Runs -------------------------------------------------------------------


def test_runs_latest_returns_most_recent_run(client: TestClient) -> None:
    body = client.get("/api/runs/latest").json()
    assert body["per_source"] == {"greenhouse": {"fetched": 3}, "lever": {"fetched": 1}}
    assert body["finished_at"].startswith("2026-06-04")


def test_runs_latest_404_when_no_runs(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(FIXTURES / "config" / "profile.yaml", config_dir / "profile.yaml")
    settings = Settings(base_dir=tmp_path)
    # create_app initializes the schema but seeds no runs.
    client = TestClient(create_app(settings, now=lambda: _NOW))
    assert client.get("/api/runs/latest").status_code == 404
