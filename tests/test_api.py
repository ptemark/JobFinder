"""Tests for the dashboard JSON API (LLD §9, task T18).

The API is exercised with FastAPI's ``TestClient`` over a temp-file DB seeded
directly through the store layer — no embedding model, no network. These assert
the list filters/sort, the ``include_ineligible`` debug toggle, status writes
that persist across a fresh client, the detail breakdown, the new-since-last-poll
flag, and the latest-run summary.
"""

from __future__ import annotations

import shutil
import sys
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


def test_list_per_company_limit_keeps_top_scored(client: TestClient) -> None:
    # Acme has three eligible jobs (a=90, b=70, c=50); capping to 2 drops the
    # lowest-scored c, and the unpaginated total reflects the cap.
    body = client.get("/api/jobs", params={"per_company_limit": 2}).json()
    assert body["total"] == 2
    assert [i["id"] for i in body["items"]] == [
        make_job_id("greenhouse", "a"),
        make_job_id("greenhouse", "b"),
    ]


def test_list_per_company_limit_is_per_company_not_global(client: TestClient) -> None:
    # With ineligibles shown there are two companies: Acme (a,b,c) and Globex (d).
    # A cap of 2 keeps Acme's top two AND Globex's single job — proving the cap
    # partitions by company rather than truncating the whole list.
    body = client.get(
        "/api/jobs", params={"per_company_limit": 2, "include_ineligible": True}
    ).json()
    assert body["total"] == 3
    ids = {i["id"] for i in body["items"]}
    assert make_job_id("greenhouse", "c") not in ids
    assert make_job_id("lever", "d") in ids


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
    # sheet_synced is False for non-applied states / when the sync is unconfigured
    # (T30 schema field; T32 sets it True on a real Sheets append).
    assert resp.json() == {"ok": True, "sheet_synced": False}

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


def test_dismissed_hidden_from_default_listing(client: TestClient) -> None:
    # spec §7 / §13 DoD: dismissing a job hides it from the default list (no status
    # filter) and the hide persists — but it stays reachable via status=dismissed.
    job_id = make_job_id("greenhouse", "a")
    before = {i["id"] for i in client.get("/api/jobs").json()["items"]}
    assert job_id in before

    client.post(f"/api/jobs/{job_id}/status", json={"state": "dismissed"})

    after = client.get("/api/jobs").json()
    assert job_id not in {i["id"] for i in after["items"]}
    assert after["total"] == len(before) - 1
    # Still retrievable when explicitly asked for.
    only = client.get("/api/jobs", params={"status": "dismissed"}).json()["items"]
    assert [i["id"] for i in only] == [job_id]


def test_applied_hidden_from_default_listing_and_shown_under_applied_tab(
    client: TestClient, tmp_path: Path
) -> None:
    # M7/T30: marking a job `applied` hides it from the default list (like dismissed)
    # but surfaces it under the Applied tab's explicit status=applied query.
    job_id = make_job_id("greenhouse", "a")
    before = {i["id"] for i in client.get("/api/jobs").json()["items"]}
    assert job_id in before

    resp = client.post(f"/api/jobs/{job_id}/status", json={"state": "applied"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "sheet_synced": False}

    after = client.get("/api/jobs").json()
    assert job_id not in {i["id"] for i in after["items"]}
    assert after["total"] == len(before) - 1

    # The Applied tab's query (status=applied, newest first) returns it.
    applied = client.get("/api/jobs", params={"status": "applied", "sort": "newest"})
    assert [i["id"] for i in applied.json()["items"]] == [job_id]

    # Detail stays reachable, and the hide persists across a fresh client.
    fresh = TestClient(create_app(Settings(base_dir=tmp_path), now=lambda: _NOW))
    assert fresh.get(f"/api/jobs/{job_id}").json()["status"] == "applied"
    assert job_id not in {i["id"] for i in fresh.get("/api/jobs").json()["items"]}


def test_status_invalid_state_422(client: TestClient) -> None:
    job_id = make_job_id("greenhouse", "a")
    assert client.post(f"/api/jobs/{job_id}/status", json={"state": "bogus"}).status_code == 422


def test_status_unknown_job_404(client: TestClient) -> None:
    assert client.post("/api/jobs/deadbeef/status", json={"state": "applied"}).status_code == 404


# --- Applied → Google Sheets sync wiring (T32) ------------------------------


def test_status_applied_invokes_sheets_sync_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Marking `applied` calls sheets.sync_applied exactly once with the job's
    # company/title/url, and a successful append surfaces as sheet_synced=True.
    # No network: the sync is patched (LLD §16 testing rule).
    from jobfinder.sheets import SyncResult

    calls: list[object] = []

    def fake_sync(job: object, *, settings: Settings) -> SyncResult:
        calls.append(job)
        return SyncResult("appended", "ok")

    monkeypatch.setattr("jobfinder.web.api.sync_applied", fake_sync)
    job_id = make_job_id("greenhouse", "a")

    resp = client.post(f"/api/jobs/{job_id}/status", json={"state": "applied"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "sheet_synced": True}

    assert len(calls) == 1
    synced = calls[0]
    assert synced.company == "Acme"
    assert synced.title == "Senior Backend Engineer"
    assert synced.url == "https://example.test/a"


def test_status_applied_sheets_error_still_returns_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A handled Sheets error never 500s: the status write stands and the response
    # is 200 with sheet_synced=False (best-effort side effect, LLD §9.1/§16).
    from jobfinder.sheets import SyncResult

    monkeypatch.setattr(
        "jobfinder.web.api.sync_applied",
        lambda job, *, settings: SyncResult("error", "boom"),
    )
    job_id = make_job_id("greenhouse", "a")

    resp = client.post(f"/api/jobs/{job_id}/status", json={"state": "applied"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "sheet_synced": False}
    # The authoritative status write persisted despite the Sheets failure.
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "applied"


def test_status_non_applied_never_calls_sheets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only `applied` triggers the sync; every other state leaves Sheets untouched
    # and reports sheet_synced=False.
    calls: list[object] = []
    monkeypatch.setattr(
        "jobfinder.web.api.sync_applied",
        lambda job, *, settings: calls.append(job),
    )
    job_id = make_job_id("greenhouse", "a")

    for state in ("dismissed", "interested", "new"):
        resp = client.post(f"/api/jobs/{job_id}/status", json={"state": state})
        assert resp.status_code == 200
        assert resp.json()["sheet_synced"] is False
    assert calls == []


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


# --- Poll trigger (T19) -----------------------------------------------------


def test_poll_returns_202_reserves_run_and_spawns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The spawn is patched so no pipeline subprocess (and no model/network) runs.
    calls: list[tuple[Settings, int]] = []
    monkeypatch.setattr(
        "jobfinder.web.api.spawn_poll",
        lambda settings, run_id: calls.append((settings, run_id)),
    )

    resp = client.post("/api/poll")
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert isinstance(run_id, int)

    # The endpoint reserved exactly that run row and handed it to the spawn.
    assert len(calls) == 1
    spawned_settings, spawned_run_id = calls[0]
    assert spawned_run_id == run_id
    assert spawned_settings.base_dir == tmp_path

    # The reserved run is open (not yet finished), so it is not the "latest" run.
    conn = connect(spawned_settings.db_path)
    try:
        row = conn.execute("SELECT finished_at FROM poll_runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["finished_at"] is None


def test_spawn_poll_invokes_pipeline_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Patch Popen so the helper's command/env is asserted without launching a process.
    from jobfinder.web import api

    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(api.subprocess, "Popen", _FakePopen)
    settings = Settings(base_dir=tmp_path)
    api.spawn_poll(settings, 42)

    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "jobfinder.pipeline", "--run-id", "42"]
    env = captured["kwargs"]["env"]
    assert env["JOBFINDER_base_dir"] == str(tmp_path)


# --- Static SPA (T20) -------------------------------------------------------


def test_serves_static_spa_index(client: TestClient) -> None:
    # The static assets are mounted at "/" (html=True), so the root serves the
    # dashboard shell and the API routes still resolve under /api.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Job Finder" in resp.text


def test_serves_static_spa_with_all_applied_tabs(client: TestClient) -> None:
    # T33: the dashboard shell carries the All/Applied tablist (LLD §9.3).
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'role="tablist"' in resp.text
    assert 'data-tab="all"' in resp.text
    assert 'data-tab="applied"' in resp.text


def test_serves_static_assets(client: TestClient) -> None:
    js = client.get("/app.js")
    assert js.status_code == 200
    assert "/api/jobs" in js.text  # the client talks to the local API
    css = client.get("/styles.css")
    assert css.status_code == 200
