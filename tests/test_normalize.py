"""Tests for normalization helpers (T09, LLD §4.3).

Covers ``html_to_text`` (tag/entity stripping, whitespace collapse) and
``parse_date`` (ISO8601-with-offset, epoch-ms, and failure -> None). Pure
functions, no network — deterministic by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobfinder.normalize import html_to_text, parse_date

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
