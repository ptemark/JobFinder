"""Core data models for Job Finder (LLD §2).

The unified :class:`Job` schema is the contract every source adapter normalizes
into; :class:`RawPosting` carries a provider's verbatim payload upstream of
normalization; :class:`ScoreBreakdown` records why a job ranked where it did.
The string enums round-trip cleanly to/from the TEXT columns in the SQLite DDL
(LLD §7.2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Length of the hex-truncated job id. sha1 hex is 40 chars; 16 keeps the id
# short while leaving collision probability negligible at this corpus size
# (LLD §2 / HLD §4.4: id = sha1(f"{source}:{source_id}")[:16]).
JOB_ID_LENGTH = 16


class LocationBucket(StrEnum):
    """Coarse location classification used for filtering and the location bonus
    (LLD §4.1, §6.3)."""

    REMOTE = "remote"
    VANCOUVER = "vancouver"
    TORONTO = "toronto"
    OTHER_CANADA = "other_canada"
    OTHER = "other"


class Seniority(StrEnum):
    """Inferred seniority band (LLD §4.2). ``UNKNOWN`` is kept and ranked low
    rather than wrongly excluded (HLD §3.2)."""

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    UNKNOWN = "unknown"


class Status(StrEnum):
    """User-facing application status persisted per job (LLD §7.2 ``status``)."""

    NEW = "new"
    INTERESTED = "interested"
    APPLIED = "applied"
    DISMISSED = "dismissed"


def make_job_id(source: str, source_id: str) -> str:
    """Return the stable dedupe id for a posting (LLD §2 / HLD §4.4).

    The same ``(source, source_id)`` always maps to the same id, so re-polls
    upsert onto one row.
    """
    digest = hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()
    return digest[:JOB_ID_LENGTH]


@dataclass(frozen=True)
class RawPosting:
    """A provider's verbatim posting object, pre-normalization (LLD §3.1)."""

    source: str
    source_id: str
    payload: dict  # original provider object, kept verbatim for debugging
    # The company name the configured board belongs to, attached by the adapter
    # at fetch time. Some providers (Lever) carry no company in the payload, so
    # the pipeline threads this hint into ``normalize`` (LLD §3.4, §8).
    company_hint: str | None = None


@dataclass
class Job:
    """Unified, normalized job record — the schema every source maps into and
    the shape persisted to the ``jobs`` table (LLD §2, §7.2)."""

    id: str  # sha1(f"{source}:{source_id}")[:16]; see make_job_id
    source: str
    source_id: str
    company: str
    title: str
    description: str  # plain text, HTML stripped
    location_raw: str
    is_remote: bool
    location_bucket: LocationBucket
    seniority: Seniority
    url: str  # canonical posting/apply URL
    posted_at: datetime | None
    date_unknown: bool
    first_seen_at: datetime
    last_seen_at: datetime
    # Eligibility is decided by filters.is_eligible and assigned by the pipeline
    # before upsert (LLD §8); ineligible jobs are stored flagged, not dropped
    # (LLD §5). content_hash gates re-embedding/re-scoring (LLD §6.4 / §7.2).
    # The LLD §2 listing abbreviates these out, but the §7.2 DDL and §8 pipeline
    # require them on the persisted record.
    eligible: bool = True
    ineligible_reason: str | None = None
    content_hash: str | None = None
    embedding: bytes | None = None  # float32 little-endian blob
    raw: dict = field(default_factory=dict)

    @classmethod
    def make_id(cls, source: str, source_id: str) -> str:
        """Convenience alias for :func:`make_job_id`."""
        return make_job_id(source, source_id)


@dataclass
class ScoreBreakdown:
    """Per-job score with its components stored so the dashboard can explain the
    ranking (LLD §6.3–§6.4)."""

    final: float  # 0..100
    semantic: float  # 0..1 cosine
    skill: float  # 0..1
    location: float  # 0..1
    recency: float  # 0..1
    scored_at: datetime


__all__ = [
    "JOB_ID_LENGTH",
    "LocationBucket",
    "Seniority",
    "Status",
    "RawPosting",
    "Job",
    "ScoreBreakdown",
    "make_job_id",
]
