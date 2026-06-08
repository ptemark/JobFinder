"""Tests for normalization helpers (T09/T10, LLD §4).

Covers ``html_to_text``/``parse_date`` (T09) plus ``bucket_location``,
``infer_seniority``, and the top-level ``normalize`` (T10). Pure functions, no
network — deterministic by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobfinder.models import LocationBucket, RawPosting, Seniority, make_job_id
from jobfinder.normalize import (
    bucket_location,
    html_to_text,
    infer_seniority,
    normalize,
    parse_date,
)

_NOW = datetime(2026, 6, 6, 0, 0, tzinfo=UTC)

# --- html_to_text -----------------------------------------------------------


def test_html_to_text_strips_tags_and_decodes_entities() -> None:
    html = "<div>Senior <b>Backend</b> Engineer &amp; Lead</div>"
    assert html_to_text(html) == "Senior Backend Engineer & Lead"


def test_html_to_text_drops_script_and_style() -> None:
    html = "<style>.job{color:red}</style><p>Build services</p><script>track('view');</script>"
    assert html_to_text(html) == "Build services"


def test_html_to_text_collapses_whitespace_including_nbsp() -> None:
    html = "<p>Java&nbsp;&amp;&nbsp;AWS</p>\n\n   <p>Remote   role</p>"
    assert html_to_text(html) == "Java & AWS Remote role"


def test_html_to_text_separates_adjacent_blocks() -> None:
    html = "<li>Kotlin</li><li>Python</li>"
    assert html_to_text(html) == "Kotlin Python"


def test_html_to_text_empty_and_whitespace_only_return_empty() -> None:
    assert html_to_text("") == ""
    assert html_to_text("   \n\t ") == ""


# --- parse_date: ISO8601 (greenhouse/ashby) ---------------------------------


def test_parse_date_iso_with_offset_converts_to_utc() -> None:
    result = parse_date("2026-06-01T09:30:00-04:00", "greenhouse")
    assert result == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert result is not None and result.tzinfo == UTC


def test_parse_date_iso_with_z_suffix() -> None:
    result = parse_date("2026-06-01T13:30:00Z", "ashby")
    assert result == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)


def test_parse_date_iso_naive_assumed_utc() -> None:
    result = parse_date("2026-06-01T13:30:00", "greenhouse")
    assert result == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)


# --- parse_date: epoch ms (lever) -------------------------------------------


def test_parse_date_epoch_ms_for_lever() -> None:
    # 1780666200000 ms == 1780666200 s == 2026-06-05T13:30:00Z
    epoch_ms = 1780666200000
    result = parse_date(epoch_ms, "lever")
    assert result == datetime(2026, 6, 5, 13, 30, tzinfo=UTC)


def test_parse_date_epoch_ms_string_for_lever() -> None:
    result = parse_date("1780666200000", "lever")
    assert result == datetime(2026, 6, 5, 13, 30, tzinfo=UTC)


# --- parse_date: failure paths ----------------------------------------------


def test_parse_date_none_returns_none() -> None:
    assert parse_date(None, "greenhouse") is None
    assert parse_date(None, "lever") is None


def test_parse_date_garbage_iso_returns_none() -> None:
    assert parse_date("not-a-date", "greenhouse") is None
    assert parse_date("", "ashby") is None


def test_parse_date_garbage_epoch_returns_none() -> None:
    assert parse_date("not-a-number", "lever") is None


def test_parse_date_bool_is_not_a_timestamp() -> None:
    # bool is an int subclass; must not be read as an epoch value.
    assert parse_date(True, "lever") is None


# --- bucket_location (LLD §4.1) ---------------------------------------------


@pytest.mark.parametrize(
    ("location_raw", "expected_bucket", "expected_remote"),
    [
        ("Remote, Canada", LocationBucket.REMOTE, True),
        ("Remote", LocationBucket.REMOTE, True),
        ("Remote (US only)", LocationBucket.OTHER, True),
        ("Remote - EMEA", LocationBucket.OTHER, True),
        ("Vancouver, BC", LocationBucket.VANCOUVER, False),
        ("Toronto, ON", LocationBucket.TORONTO, False),
        ("Montréal, QC", LocationBucket.OTHER_CANADA, False),
        ("Ottawa, Canada", LocationBucket.OTHER_CANADA, False),
        ("New York, NY", LocationBucket.OTHER, False),
        ("", LocationBucket.OTHER, False),
        # M7 (T29): broad non-Canada matcher buckets any named foreign region OTHER.
        ("Remote — US", LocationBucket.OTHER, True),
        ("Remote (United States)", LocationBucket.OTHER, True),
        ("Remote, EMEA", LocationBucket.OTHER, True),
        ("US-based — Remote", LocationBucket.OTHER, True),
        ("Remote LATAM", LocationBucket.OTHER, True),
        ("Remote, APAC", LocationBucket.OTHER, True),
        ("Remote (UK)", LocationBucket.OTHER, True),
        ("Remote - India", LocationBucket.OTHER, True),
        # M7 (T29): a positive Canada cue or no country named stays REMOTE.
        ("Remote - Canada", LocationBucket.REMOTE, True),
        ("Remote (North America)", LocationBucket.REMOTE, True),
        # A non-remote US location is still out of scope (final OTHER fallthrough).
        ("US-based", LocationBucket.OTHER, False),
    ],
)
def test_bucket_location_branches(
    location_raw: str, expected_bucket: LocationBucket, expected_remote: bool
) -> None:
    assert bucket_location(location_raw, is_remote=False) == (
        expected_bucket,
        expected_remote,
    )


def test_bucket_location_source_remote_flag_overrides_text() -> None:
    # An explicit source-level remote signal (e.g. Ashby workplaceType) buckets
    # remote even when the location text says nothing about it.
    assert bucket_location("", is_remote=True) == (LocationBucket.REMOTE, True)


def test_bucket_location_remote_wins_over_city() -> None:
    # Ordered rules: a remote signal takes priority over a named city.
    assert bucket_location("Remote, Vancouver", is_remote=False) == (
        LocationBucket.REMOTE,
        True,
    )


def test_bucket_location_canada_signal_wins_over_stray_non_canada_token() -> None:
    # M7 (T29) §4.1.1a: when a Canada cue and a non-Canada token co-occur, the
    # Canada signal wins — "Remote - Canada & US" stays REMOTE, not OTHER.
    assert bucket_location("Remote - Canada & US", is_remote=False) == (
        LocationBucket.REMOTE,
        True,
    )


# --- infer_seniority (LLD §4.2) ---------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Staff Software Engineer", Seniority.STAFF),
        ("Principal Engineer", Seniority.STAFF),
        ("Senior Backend Engineer", Seniority.SENIOR),
        ("Sr. Developer", Seniority.SENIOR),
        ("Tech Lead", Seniority.SENIOR),
        ("Junior Developer", Seniority.JUNIOR),
        ("Software Engineering Intern", Seniority.JUNIOR),
        ("Backend Engineer II", Seniority.MID),
        ("Intermediate Developer", Seniority.MID),
        ("Software Engineer", Seniority.UNKNOWN),
        ("Engineering Manager", Seniority.UNKNOWN),
        ("Director of Engineering", Seniority.UNKNOWN),
        ("Principal Product Manager", Seniority.UNKNOWN),
    ],
)
def test_infer_seniority_from_title(title: str, expected: Seniority) -> None:
    assert infer_seniority(title, "") == expected


def test_infer_seniority_falls_back_to_description() -> None:
    # A generic title with a clear seniority cue in the body resolves the band.
    assert infer_seniority("Software Engineer", "We need a senior backend dev.") == (
        Seniority.SENIOR
    )


def test_infer_seniority_ignores_numeric_mid_cues_in_prose() -> None:
    # The noisy numeric mid cue ("2") must not be read out of the description.
    assert (
        infer_seniority("Software Engineer", "You have 2 years of experience.") == Seniority.UNKNOWN
    )


# --- normalize (LLD §4) -----------------------------------------------------


def test_normalize_greenhouse_payload() -> None:
    payload = {
        "id": 12345,
        "title": "Senior Backend Engineer",
        # Greenhouse delivers entity-encoded HTML.
        "content": "&lt;p&gt;Build &lt;b&gt;services&lt;/b&gt; in Java &amp;amp; AWS&lt;/p&gt;",
        "location": {"name": "Remote, Canada"},
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
        "updated_at": "2026-06-01T09:30:00-04:00",
        "company_name": "Acme",
    }
    raw = RawPosting(source="greenhouse", source_id="12345", payload=payload)
    job = normalize(raw, company_hint="board-token", now=_NOW)

    assert job.id == make_job_id("greenhouse", "12345")
    assert job.company == "Acme"  # company_name wins over the hint
    assert job.title == "Senior Backend Engineer"
    assert job.description == "Build services in Java & AWS"
    assert job.location_bucket == LocationBucket.REMOTE
    assert job.is_remote is True
    assert job.seniority == Seniority.SENIOR
    assert job.posted_at == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert job.date_unknown is False
    assert job.first_seen_at == _NOW and job.last_seen_at == _NOW
    assert job.raw is payload


def test_normalize_lever_payload_uses_company_hint() -> None:
    payload = {
        "id": "abc-123",
        "text": "Staff Software Engineer",
        "descriptionPlain": "Work on backend systems in Kotlin and AWS.",
        "categories": {"location": "Vancouver, BC"},
        "hostedUrl": "https://jobs.lever.co/acmeco/abc-123",
        "createdAt": 1780666200000,
    }
    raw = RawPosting(source="lever", source_id="abc-123", payload=payload)
    job = normalize(raw, company_hint="AcmeCo", now=_NOW)

    assert job.company == "AcmeCo"  # Lever carries no company name
    assert job.description == "Work on backend systems in Kotlin and AWS."
    assert job.location_bucket == LocationBucket.VANCOUVER
    assert job.is_remote is False
    assert job.seniority == Seniority.STAFF
    assert job.posted_at == datetime(2026, 6, 5, 13, 30, tzinfo=UTC)


def test_normalize_missing_date_sets_date_unknown() -> None:
    payload = {"id": 7, "title": "Backend Engineer", "location": {"name": "Toronto, ON"}}
    raw = RawPosting(source="greenhouse", source_id="7", payload=payload)
    job = normalize(raw, company_hint="Acme", now=_NOW)

    assert job.posted_at is None
    assert job.date_unknown is True
    assert job.location_bucket == LocationBucket.TORONTO


def test_normalize_unknown_source_raises() -> None:
    raw = RawPosting(source="workday", source_id="1", payload={})
    with pytest.raises(ValueError, match="no normalizer registered"):
        normalize(raw, company_hint=None, now=_NOW)
