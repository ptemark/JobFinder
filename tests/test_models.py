"""Tests for core data models (T03, LLD §2).

Covers the stable id derivation (the dedupe key — must be deterministic and
collision-distinct), enum string round-tripping, and that the dataclasses hold
their fields as specified.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobfinder.models import (
    JOB_ID_LENGTH,
    Job,
    LocationBucket,
    RawPosting,
    ScoreBreakdown,
    Seniority,
    Status,
    make_job_id,
)

# --- Job.id derivation ------------------------------------------------------


def test_job_id_is_stable_for_same_inputs() -> None:
    first = make_job_id("greenhouse", "12345")
    second = make_job_id("greenhouse", "12345")
    assert first == second


def test_job_id_differs_for_different_inputs() -> None:
    assert make_job_id("greenhouse", "12345") != make_job_id("greenhouse", "12346")
    # Same source_id under a different source must not collide.
    assert make_job_id("greenhouse", "1") != make_job_id("lever", "1")


def test_job_id_length_and_hex() -> None:
    job_id = make_job_id("ashby", "abc-def")
    assert len(job_id) == JOB_ID_LENGTH
    assert all(c in "0123456789abcdef" for c in job_id)


def test_job_classmethod_matches_helper() -> None:
    assert Job.make_id("lever", "xyz") == make_job_id("lever", "xyz")


# --- enum round-trips -------------------------------------------------------


def test_location_bucket_round_trips_to_str() -> None:
    assert LocationBucket("remote") is LocationBucket.REMOTE
    assert LocationBucket.VANCOUVER.value == "vancouver"
    # str-Enum members compare equal to their string value.
    assert LocationBucket.TORONTO == "toronto"


def test_seniority_round_trips_to_str() -> None:
    assert Seniority("staff") is Seniority.STAFF
    assert Seniority.UNKNOWN.value == "unknown"


def test_status_round_trips_to_str() -> None:
    assert Status("dismissed") is Status.DISMISSED
    assert Status.NEW.value == "new"


# --- dataclass shapes -------------------------------------------------------


def test_raw_posting_is_frozen() -> None:
    raw = RawPosting(source="greenhouse", source_id="1", payload={"k": "v"})
    assert raw.payload == {"k": "v"}
    try:
        raw.source = "lever"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError is a subclass of Exception
        assert "cannot assign" in str(exc) or "frozen" in str(exc).lower()
    else:
        raise AssertionError("RawPosting should be frozen")


def test_job_holds_fields_and_defaults() -> None:
    now = datetime(2026, 6, 2, tzinfo=UTC)
    job = Job(
        id=make_job_id("greenhouse", "1"),
        source="greenhouse",
        source_id="1",
        company="Acme",
        title="Senior Backend Engineer",
        description="Java and AWS.",
        location_raw="Remote - Canada",
        is_remote=True,
        location_bucket=LocationBucket.REMOTE,
        seniority=Seniority.SENIOR,
        url="https://example.com/jobs/1",
        posted_at=now,
        date_unknown=False,
        first_seen_at=now,
        last_seen_at=now,
    )
    assert job.embedding is None
    assert job.raw == {}
    assert job.location_bucket is LocationBucket.REMOTE


def test_score_breakdown_holds_components() -> None:
    now = datetime(2026, 6, 2, tzinfo=UTC)
    sb = ScoreBreakdown(
        final=87.5,
        semantic=0.71,
        skill=1.0,
        location=1.0,
        recency=0.9,
        scored_at=now,
    )
    assert sb.final == 87.5
    assert sb.scored_at is now
