"""Tests for eligibility filtering (T13, LLD §5).

Every gate is exercised on both paths: each rejection reason fires for the
matching defect, an otherwise-eligible role passes, and ``date_unknown`` passes
the recency gate. Pure functions, no network — deterministic by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobfinder.filters import (
    REASON_LOCATION_OUT,
    REASON_NOT_BACKEND_ROLE,
    REASON_SENIORITY_OUT,
    REASON_STALE,
    is_eligible,
)
from jobfinder.models import Job, LocationBucket, Seniority
from jobfinder.settings import Profile

_NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)


def _profile(**overrides: object) -> Profile:
    base: dict[str, object] = {
        "role_keywords": ["backend", "software engineer", "developer"],
        "must_have_skills": ["java", "kotlin", "python", "aws"],
        "seniority_include": ["mid", "senior", "staff"],
        "seniority_exclude": ["junior", "intern", "manager"],
        "locations_priority": ["remote", "vancouver", "toronto", "other_canada"],
        "max_age_days": 21,
    }
    base.update(overrides)
    return Profile.model_validate(base)


def _job(**overrides: object) -> Job:
    """Build a job that is eligible by default; override one field per test."""
    base: dict[str, object] = {
        "source": "greenhouse",
        "source_id": "1",
        "company": "Acme",
        "title": "Senior Backend Engineer",
        "description": "Build backend services in Java and AWS.",
        "location_raw": "Remote - Canada",
        "is_remote": True,
        "location_bucket": LocationBucket.REMOTE,
        "seniority": Seniority.SENIOR,
        "url": "https://example.com/job",
        "posted_at": datetime(2026, 6, 1, tzinfo=UTC),
        "date_unknown": False,
        "first_seen_at": _NOW,
        "last_seen_at": _NOW,
    }
    base.update(overrides)
    source = str(base["source"])
    source_id = str(base["source_id"])
    return Job(id=Job.make_id(source, source_id), **base)  # type: ignore[arg-type]


def test_eligible_role_passes() -> None:
    ok, reason = is_eligible(_job(), profile=_profile(), now=_NOW)
    assert ok is True
    assert reason is None


def test_stale_rejected() -> None:
    stale = _job(posted_at=datetime(2026, 5, 1, tzinfo=UTC))  # 36 days before _NOW
    ok, reason = is_eligible(stale, profile=_profile(), now=_NOW)
    assert ok is False
    assert reason == REASON_STALE


def test_date_unknown_passes_recency() -> None:
    unknown = _job(posted_at=None, date_unknown=True)
    ok, reason = is_eligible(unknown, profile=_profile(), now=_NOW)
    assert ok is True
    assert reason is None


def test_non_backend_role_rejected() -> None:
    frontend = _job(title="Senior UX Designer", description="Design delightful UIs.")
    ok, reason = is_eligible(frontend, profile=_profile(), now=_NOW)
    assert ok is False
    assert reason == REASON_NOT_BACKEND_ROLE


def test_role_keyword_matched_in_description() -> None:
    # Title alone lacks a keyword, but the description carries "backend".
    role = _job(title="Senior Engineer", description="Own our backend platform.")
    ok, _ = is_eligible(role, profile=_profile(), now=_NOW)
    assert ok is True


def test_role_gate_skipped_when_not_required() -> None:
    off_role = _job(title="Senior UX Designer", description="Design delightful UIs.")
    ok, reason = is_eligible(off_role, profile=_profile(role_keyword_required=False), now=_NOW)
    assert ok is True
    assert reason is None


def test_out_of_location_rejected() -> None:
    abroad = _job(
        location_raw="New York, NY", is_remote=False, location_bucket=LocationBucket.OTHER
    )
    ok, reason = is_eligible(abroad, profile=_profile(), now=_NOW)
    assert ok is False
    assert reason == REASON_LOCATION_OUT


def test_junior_rejected() -> None:
    junior = _job(title="Junior Backend Developer", seniority=Seniority.JUNIOR)
    ok, reason = is_eligible(junior, profile=_profile(), now=_NOW)
    assert ok is False
    assert reason == REASON_SENIORITY_OUT


def test_people_manager_rejected() -> None:
    # Manager titles infer to UNKNOWN seniority, so the explicit gate must catch them.
    manager = _job(title="Engineering Manager, Backend", seniority=Seniority.UNKNOWN)
    ok, reason = is_eligible(manager, profile=_profile(), now=_NOW)
    assert ok is False
    assert reason == REASON_SENIORITY_OUT


def test_staff_ic_passes_despite_principal_in_title() -> None:
    # "Principal Engineer" is a clear IC role, not a people-manager.
    staff = _job(title="Principal Engineer, Backend", seniority=Seniority.STAFF)
    ok, reason = is_eligible(staff, profile=_profile(), now=_NOW)
    assert ok is True
    assert reason is None


def test_recency_gate_short_circuits_before_role() -> None:
    # A stale job that would also fail the role gate reports "stale" (cheapest first).
    stale_offrole = _job(
        title="Senior UX Designer",
        description="Design UIs.",
        posted_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    ok, reason = is_eligible(stale_offrole, profile=_profile(), now=_NOW)
    assert ok is False
    assert reason == REASON_STALE
