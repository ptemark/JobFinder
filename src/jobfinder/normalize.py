"""Normalization helpers: raw provider payloads -> clean text and UTC datetimes.

Pure functions, no I/O — fully fixture-testable and deterministic (LLD §4).
This module covers the HTML and date helpers (T09); location bucketing,
seniority inference, and the top-level ``normalize`` land in T10.
"""

from __future__ import annotations

from datetime import UTC, datetime

from selectolax.parser import HTMLParser

# Tags whose contents are markup/asset code, never human-readable job text;
# dropped before extracting text (LLD §4: "drop script/style").
_NON_CONTENT_TAGS = ("script", "style")

# Sources that deliver the posting timestamp as integer epoch milliseconds
# rather than an ISO8601 string (LLD §4.3: Lever's ``createdAt``).
EPOCH_MS_SOURCES = frozenset({"lever"})

# Divisor turning epoch milliseconds into the epoch seconds that
# ``datetime.fromtimestamp`` expects (LLD §4.3).
_MS_PER_SECOND = 1000


def html_to_text(html: str) -> str:
    """Return the human-readable text of an HTML fragment (LLD §4).

    Drops ``<script>``/``<style>`` content, decodes HTML entities, and
    collapses every run of whitespace (including non-breaking spaces) to a
    single space. Returns ``""`` for empty/whitespace-only input.
    """
    if not html or not html.strip():
        return ""
    tree = HTMLParser(html)
    for tag in _NON_CONTENT_TAGS:
        for node in tree.css(tag):
            node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    # selectolax decodes entities in ``.text``; the separator keeps words from
    # adjacent block elements from being glued together. ``str.split`` then
    # collapses all whitespace, including the ``\xa0`` from ``&nbsp;``.
    return " ".join(root.text(separator=" ").split())


def parse_date(value: str | int | float | None, source: str) -> datetime | None:
    """Parse a posting timestamp to a timezone-aware UTC datetime (LLD §4.3).

    Epoch-millisecond sources (Lever) are parsed from a number; all others from
    an ISO8601 string with offset. Any unparseable input returns ``None`` so the
    caller can set ``date_unknown`` rather than crash.
    """
    if value is None:
        return None
    if source in EPOCH_MS_SOURCES:
        return _parse_epoch_ms(value)
    return _parse_iso(value)


def _parse_epoch_ms(value: str | int | float) -> datetime | None:
    """Parse integer/float epoch milliseconds to a UTC datetime, else ``None``."""
    # ``bool`` is an ``int`` subclass but never a valid timestamp.
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / _MS_PER_SECOND, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso(value: str | int | float) -> datetime | None:
    """Parse an ISO8601 string (any offset) to a UTC datetime, else ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A timestamp without an offset is assumed UTC; otherwise convert to UTC.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["EPOCH_MS_SOURCES", "html_to_text", "parse_date"]
