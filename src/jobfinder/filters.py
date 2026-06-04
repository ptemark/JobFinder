"""Eligibility filtering — cheapest-first gates run before embedding (LLD §5).

``is_eligible`` applies the ordered gates (recency → role keyword → location →
seniority/people-manager) and returns ``(ok, reason)``. The reason is a stable
machine string so the pipeline can persist it (``jobs.ineligible_reason``) and
the dashboard debug toggle can surface false negatives. Ineligible jobs are
**kept** by the pipeline (stored flagged), never dropped here — this function
only classifies. Pure: no I/O, no global state, fully fixture-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobfinder.models import LocationBucket, Seniority
from jobfinder.normalize import is_people_manager

if TYPE_CHECKING:
    from datetime import datetime

    from jobfinder.models import Job
    from jobfinder.settings import Profile

# Stable ineligibility reasons persisted to ``jobs.ineligible_reason`` (LLD §5).
REASON_STALE = "stale"
REASON_NOT_BACKEND_ROLE = "not_backend_role"
REASON_LOCATION_OUT = "location_out"
REASON_SENIORITY_OUT = "seniority_out"

# Seniority bands that are out of scope (LLD §5 step 4). Mirrors the IC-relevant
# entries of ``profile.seniority_exclude``: "intern" infers to JUNIOR, and the
# "manager"/"director" entries are handled by the explicit people-manager check
# (those titles infer to UNKNOWN, which is otherwise kept). MID/SENIOR/STAFF and
# UNKNOWN pass — UNKNOWN is ranked low, never wrongly excluded (HLD §3.2).
_EXCLUDED_SENIORITY = frozenset({Seniority.JUNIOR})


def is_eligible(job: Job, *, profile: Profile, now: datetime) -> tuple[bool, str | None]:
    """Return ``(eligible, reason)`` for a normalized job (LLD §5).

    Gates are ordered cheapest-first and short-circuit before any embedding.
    ``reason`` is ``None`` when eligible, otherwise one of the ``REASON_*``
    constants. A ``date_unknown`` job (``posted_at is None``) passes the recency
    gate so it is kept and ranked low rather than silently dropped.
    """
    # 1. Recency. date_unknown (posted_at is None) passes by design.
    if job.posted_at is not None and (now - job.posted_at).days > profile.max_age_days:
        return False, REASON_STALE
    # 2. Role-keyword pre-check (the semantic gate happens later in scoring).
    if profile.role_keyword_required and not _matches_role_keyword(job, profile.role_keywords):
        return False, REASON_NOT_BACKEND_ROLE
    # 3. Location: anything outside Canada/remote-Canada is out of scope.
    if job.location_bucket == LocationBucket.OTHER:
        return False, REASON_LOCATION_OUT
    # 4. Seniority: explicit junior bands and people-managers are out of scope.
    if job.seniority in _EXCLUDED_SENIORITY or is_people_manager(job.title):
        return False, REASON_SENIORITY_OUT
    return True, None


def _matches_role_keyword(job: Job, role_keywords: list[str]) -> bool:
    """True if any configured role keyword appears in the title or description.

    Case-insensitive substring match — the keywords are short phrases
    (e.g. "software engineer") for which substring containment is the intended
    pre-check (LLD §5 / §11.1).
    """
    haystack = f"{job.title}\n{job.description}".lower()
    return any(keyword.lower() in haystack for keyword in role_keywords)


__all__ = [
    "REASON_LOCATION_OUT",
    "REASON_NOT_BACKEND_ROLE",
    "REASON_SENIORITY_OUT",
    "REASON_STALE",
    "is_eligible",
]
