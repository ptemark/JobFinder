"""Request/response models for the dashboard API (LLD §9.2).

These are the wire contracts the static frontend (T20) consumes: ranked job
cards for the list view, the fuller detail payload with the score breakdown, the
status-update request body, and the latest-run summary. Kept separate from the
domain :mod:`jobfinder.models` so the API surface can evolve independently of the
persisted schema.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from jobfinder.models import Status


class JobCard(BaseModel):
    """One ranked job as shown in the list view (LLD §9.2)."""

    id: str
    title: str
    company: str
    location_bucket: str
    is_remote: bool
    posted_at: datetime | None
    age_days: int | None  # whole days since posted_at; None when date_unknown
    date_unknown: bool
    score: float  # scores.final (0..100); 0.0 for an unscored ineligible job
    matched_skills: list[str]
    status: str  # new | interested | applied | dismissed
    is_new_since_last_poll: bool
    url: str


class JobListResponse(BaseModel):
    """The ``/api/jobs`` list payload: a page of cards plus the unpaginated total."""

    items: list[JobCard]
    total: int


class JobDetail(JobCard):
    """A single job's detail view: the card plus full description and the score
    component breakdown (LLD §9.1/§9.2)."""

    description: str
    breakdown: dict  # {final, semantic, skill, location, recency}; empty if unscored


class StatusUpdate(BaseModel):
    """Body for ``POST /api/jobs/{id}/status`` — validated against the enum so an
    unknown state is a 422, not a silent write."""

    state: Status


class StatusResponse(BaseModel):
    """Acknowledgement for a successful status write."""

    ok: bool


class RunSummaryResponse(BaseModel):
    """The latest poll-run summary for ``/api/runs/latest`` (LLD §9.1)."""

    run_id: int
    started_at: datetime | None
    finished_at: datetime | None
    per_source: dict


__all__ = [
    "JobCard",
    "JobListResponse",
    "JobDetail",
    "StatusUpdate",
    "StatusResponse",
    "RunSummaryResponse",
]
