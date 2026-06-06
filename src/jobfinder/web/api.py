"""Dashboard JSON API routers (LLD §9.1).

Read-only job browsing plus per-job status writes, and a manual poll trigger.
Every route opens a short-lived SQLite connection (the :func:`get_conn`
dependency) so the dashboard never holds the DB open across the poll's writes —
``busy_timeout`` (LLD §7.1) covers the brief overlap. Rows from the store layer
are mapped to the wire schemas here, where the request clock and the "new since
last poll" threshold are applied. ``POST /api/poll`` reserves a run row and spawns
the pipeline out-of-process (LLD §9.1) so a slow source never blocks the request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from jobfinder.models import Status
from jobfinder.score import matched_skills
from jobfinder.store import (
    JobFilters,
    connect,
    count_jobs,
    get_job_detail,
    latest_run,
    previous_run_finished_at,
    query_jobs,
    set_status,
    start_run,
)
from jobfinder.web.schemas import (
    JobCard,
    JobDetail,
    JobListResponse,
    PollResponse,
    RunSummaryResponse,
    StatusResponse,
    StatusUpdate,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

    from jobfinder.settings import Settings

router = APIRouter(prefix="/api")


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Yield a per-request SQLite connection to the configured DB, closed after use."""
    conn = connect(request.app.state.settings.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def spawn_poll(settings: Settings, run_id: int) -> None:
    """Spawn the pipeline as a detached, non-blocking subprocess (LLD §9.1).

    The "Poll now" button must return immediately, so the heavy poll (model load,
    network fetch) runs out-of-process: a slow or hanging source can never block
    the dashboard. The child inherits the environment plus an explicit
    ``JOBFINDER_base_dir`` so it resolves the same DB/config the server uses, and
    finishes the ``run_id`` this request already reserved. ``start_new_session``
    detaches it so it outlives a server restart (POSIX; ignored elsewhere).
    """
    env = {**os.environ, "JOBFINDER_base_dir": str(settings.base_dir)}
    # Fixed argv, no shell, no user-supplied input — only the integer run_id.
    subprocess.Popen(
        [sys.executable, "-m", "jobfinder.pipeline", "--run-id", str(run_id)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _parse_dt(value: str | None) -> datetime | None:
    """Parse a stored ISO-8601 timestamp back to an aware datetime (None passes through)."""
    return datetime.fromisoformat(value) if value else None


def _is_new(first_seen_at: str | None, threshold: str | None) -> bool:
    """True if the job was first seen after the previous poll finished (LLD §7.3).

    Both timestamps are UTC ISO-8601 text, so a lexicographic compare is correct.
    With no prior run (``threshold is None``) every job counts as new.
    """
    if first_seen_at is None:
        return False
    if threshold is None:
        return True
    return first_seen_at > threshold


def _to_card(
    row: sqlite3.Row,
    *,
    must_have_skills: list[str],
    now: datetime,
    new_threshold: str | None,
) -> JobCard:
    """Map a joined jobs/scores/status row onto a :class:`JobCard` (LLD §9.2)."""
    posted_at = _parse_dt(row["posted_at"])
    age_days = (now - posted_at).days if posted_at is not None else None
    text = f"{row['title']}\n{row['description'] or ''}"
    return JobCard(
        id=row["id"],
        title=row["title"],
        company=row["company"] or "",
        location_bucket=row["location_bucket"],
        is_remote=bool(row["is_remote"]),
        posted_at=posted_at,
        age_days=age_days,
        date_unknown=bool(row["date_unknown"]),
        # Ineligible jobs are stored unscored (LLD §5); surface 0.0 so the card
        # type stays a plain float and they sort to the bottom.
        score=row["final"] if row["final"] is not None else 0.0,
        matched_skills=matched_skills(text, must_have_skills),
        status=row["status_state"] or Status.NEW.value,
        is_new_since_last_poll=_is_new(row["first_seen_at"], new_threshold),
        url=row["url"] or "",
    )


def _breakdown(row: sqlite3.Row) -> dict[str, float]:
    """Score component breakdown for the detail view; empty when unscored (LLD §9.2)."""
    if row["final"] is None:
        return {}
    return {
        "final": row["final"],
        "semantic": row["semantic"],
        "skill": row["skill"],
        "location": row["location"],
        "recency": row["recency"],
    }


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    bucket: str | None = Query(default=None),
    source: str | None = Query(default=None),
    seniority: str | None = Query(default=None),
    min_score: float | None = Query(default=None),
    status: str | None = Query(default=None),
    max_age_days: int | None = Query(default=None, ge=1),
    sort: Literal["best", "newest"] = Query(default="best"),
    include_ineligible: bool = Query(default=False),
    per_company_limit: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> JobListResponse:
    """Ranked, filtered job list (LLD §9.1). ``total`` is the unpaginated count."""
    now = request.app.state.clock()
    filters = JobFilters(
        bucket=bucket,
        source=source,
        seniority=seniority,
        min_score=min_score,
        status=status,
        max_age_days=max_age_days,
        include_ineligible=include_ineligible,
    )
    rows = query_jobs(
        conn,
        filters=filters,
        sort=sort,
        limit=limit,
        offset=offset,
        per_company_limit=per_company_limit,
        now=now,
    )
    total = count_jobs(conn, filters=filters, per_company_limit=per_company_limit, now=now)
    skills = request.app.state.profile.must_have_skills
    threshold = previous_run_finished_at(conn)
    items = [
        _to_card(row, must_have_skills=skills, now=now, new_threshold=threshold) for row in rows
    ]
    return JobListResponse(items=items, total=total)


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job_view(
    job_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JobDetail:
    """Full detail for one job, including the score breakdown (LLD §9.1)."""
    row = get_job_detail(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    now = request.app.state.clock()
    threshold = previous_run_finished_at(conn)
    skills = request.app.state.profile.must_have_skills
    card = _to_card(row, must_have_skills=skills, now=now, new_threshold=threshold)
    return JobDetail(
        **card.model_dump(),
        description=row["description"] or "",
        breakdown=_breakdown(row),
    )


@router.post("/jobs/{job_id}/status", response_model=StatusResponse)
def update_status(
    job_id: str,
    payload: StatusUpdate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> StatusResponse:
    """Persist a job's status (LLD §9.1). 404 if the job is unknown."""
    if get_job_detail(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    set_status(conn, job_id, payload.state, now=request.app.state.clock())
    return StatusResponse(ok=True)


@router.post("/poll", status_code=202, response_model=PollResponse)
def trigger_poll(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PollResponse:
    """Trigger a poll and return its reserved run id (LLD §9.1), non-blocking.

    The run row is opened here so the dashboard gets a ``run_id`` to watch via
    ``GET /api/runs/latest`` straight away; the spawned pipeline finishes that
    same row. This process never touches the network — the fetch happens in the
    child subprocess (Cost & Safety §1/§5).
    """
    settings = request.app.state.settings
    run_id = start_run(conn, now=request.app.state.clock())
    spawn_poll(settings, run_id)
    return PollResponse(run_id=run_id)


@router.get("/runs/latest", response_model=RunSummaryResponse)
def runs_latest(conn: sqlite3.Connection = Depends(get_conn)) -> RunSummaryResponse:
    """The most recent finished poll-run summary (LLD §9.1). 404 before any poll."""
    row = latest_run(conn)
    if row is None:
        raise HTTPException(status_code=404, detail="no completed runs yet")
    per_source = json.loads(row["per_source_json"]) if row["per_source_json"] else {}
    return RunSummaryResponse(
        run_id=row["id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        per_source=per_source,
    )


__all__ = ["router", "spawn_poll"]
