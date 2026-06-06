"""Normalization helpers: raw provider payloads -> the unified :class:`Job`.

Pure functions, no I/O — fully fixture-testable and deterministic (LLD §4).
Covers the HTML/date helpers, ``bucket_location``/``infer_seniority`` heuristics,
and the top-level ``normalize`` that maps a source's verbatim payload into a
:class:`Job` (LLD §4.1–§4.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape

from selectolax.parser import HTMLParser

from jobfinder.models import Job, LocationBucket, RawPosting, Seniority, make_job_id

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


# --- Location bucketing (LLD §4.1) ------------------------------------------

# Any remote signal in free-text location. ``is_remote`` from the source (e.g.
# Ashby's ``workplaceType``) is OR-ed with this so either path triggers rule 1.
_REMOTE_RE = re.compile(r"remote", re.IGNORECASE)
# Remote roles explicitly scoped to a non-Canada region -> OTHER (LLD §4.1.1).
_REMOTE_NON_CANADA_RE = re.compile(r"remote.*(?:us only|united states only|emea)", re.IGNORECASE)
_VANCOUVER_RE = re.compile(r"vancouver|,\s?bc\b|british columbia", re.IGNORECASE)
_TORONTO_RE = re.compile(r"toronto|,\s?on\b|ontario", re.IGNORECASE)
# Other Canadian metros / explicit Canada (LLD §4.1.4).
_OTHER_CANADA_RE = re.compile(
    r"canada|montr[eé]al|calgary|ottawa|edmonton|winnipeg|qu[eé]bec|halifax|waterloo",
    re.IGNORECASE,
)


def bucket_location(location_raw: str, is_remote: bool) -> tuple[LocationBucket, bool]:
    """Classify a free-text location into a :class:`LocationBucket` (LLD §4.1).

    Rules are applied in priority order; the effective remote flag is returned
    alongside the bucket so a source-level remote signal and a text-only one
    converge on one truth.
    """
    text = location_raw or ""
    remote_signal = is_remote or _REMOTE_RE.search(text) is not None
    if remote_signal:
        # Remote but pinned to a non-Canada region is out of scope; still remote.
        if _REMOTE_NON_CANADA_RE.search(text):
            return LocationBucket.OTHER, True
        # Otherwise treat remote as Canada-eligible (explicit Canada or no
        # country exclusion) per LLD §4.1.1.
        return LocationBucket.REMOTE, True
    if _VANCOUVER_RE.search(text):
        return LocationBucket.VANCOUVER, False
    if _TORONTO_RE.search(text):
        return LocationBucket.TORONTO, False
    if _OTHER_CANADA_RE.search(text):
        return LocationBucket.OTHER_CANADA, False
    return LocationBucket.OTHER, False


# --- Seniority inference (LLD §4.2) -----------------------------------------

# People-manager / executive titles: out of IC scope unless clearly IC below.
_MANAGER_RE = re.compile(r"principal|director|\bvp\b|head of|manager\b", re.IGNORECASE)
# A manager-ish title that is unambiguously an individual contributor.
_IC_OVERRIDE_RE = re.compile(r"staff|principal engineer", re.IGNORECASE)
_STAFF_RE = re.compile(r"\bstaff\b", re.IGNORECASE)
_SENIOR_RE = re.compile(r"senior|\bsr\b\.?|\blead\b", re.IGNORECASE)
_JUNIOR_RE = re.compile(r"intern|new ?grad|graduate|junior|\bjr\b\.?|\bentry\b", re.IGNORECASE)
# Numeric mid-level cues are matched on the title only — far too noisy in prose.
_MID_RE = re.compile(r"\bmid\b|intermediate|\bii\b|\b2\b", re.IGNORECASE)


def is_people_manager(title: str) -> bool:
    """True for a people-manager/executive title that is not clearly an IC role.

    Mirrors the rule in :func:`infer_seniority` (LLD §4.2): a manager/director/
    VP/head-of/manager title is out of individual-contributor scope unless it is
    an unambiguous ``staff``/``principal engineer`` IC title. Exposed so the
    eligibility filter (LLD §5) can reject people-managers without duplicating
    these patterns — such titles infer to ``UNKNOWN`` seniority, which the filter
    keeps by design, so the manager check must be explicit.
    """
    title_text = title or ""
    return bool(_MANAGER_RE.search(title_text)) and not _IC_OVERRIDE_RE.search(title_text)


def infer_seniority(title: str, description: str) -> Seniority:
    """Infer a :class:`Seniority` band from the title, falling back to the body.

    Ordered, first-match-wins (LLD §4.2). A people-manager/executive title is
    mapped to ``UNKNOWN`` (the eligibility filter excludes it separately) unless
    it is clearly an IC ``staff``/``principal engineer`` role. When the title is
    generic, unambiguous seniority words in the description are used as a weaker
    fallback; the noisy numeric ``mid`` cues are never read from prose.
    """
    title_text = title or ""
    if _MANAGER_RE.search(title_text):
        return Seniority.STAFF if _IC_OVERRIDE_RE.search(title_text) else Seniority.UNKNOWN
    band = _band_from_text(title_text, include_mid=True)
    if band is not Seniority.UNKNOWN:
        return band
    return _band_from_text(description or "", include_mid=False)


def _band_from_text(text: str, *, include_mid: bool) -> Seniority:
    """Return the first matching seniority band in ``text``, else ``UNKNOWN``."""
    if _STAFF_RE.search(text):
        return Seniority.STAFF
    if _SENIOR_RE.search(text):
        return Seniority.SENIOR
    if _JUNIOR_RE.search(text):
        return Seniority.JUNIOR
    if include_mid and _MID_RE.search(text):
        return Seniority.MID
    return Seniority.UNKNOWN


# --- Top-level normalize (LLD §4) -------------------------------------------


@dataclass(frozen=True)
class _Fields:
    """Source-specific fields extracted from a raw payload, pre-heuristics."""

    title: str
    description: str
    company: str
    location_raw: str
    is_remote: bool  # explicit source-level remote signal (text handled later)
    url: str
    posted_at: datetime | None


def _extract_greenhouse(payload: dict, company_hint: str | None) -> _Fields:
    """Map a Greenhouse board-API job object to common fields (LLD §3.3)."""
    location = payload.get("location")
    location_raw = location.get("name", "") if isinstance(location, dict) else ""
    # Greenhouse delivers ``content`` as HTML-entity-encoded HTML; unescape once
    # so ``html_to_text`` sees real tags rather than literal ``&lt;p&gt;``.
    content = payload.get("content") or ""
    return _Fields(
        title=payload.get("title") or "",
        description=html_to_text(unescape(content)),
        company=payload.get("company_name") or company_hint or "",
        location_raw=location_raw or "",
        is_remote=False,  # Greenhouse has no explicit remote flag; text decides
        url=payload.get("absolute_url") or "",
        posted_at=parse_date(payload.get("updated_at"), "greenhouse"),
    )


def _extract_lever(payload: dict, company_hint: str | None) -> _Fields:
    """Map a Lever postings-API object to common fields (LLD §3.4)."""
    categories = payload.get("categories")
    location_raw = categories.get("location", "") if isinstance(categories, dict) else ""
    # Lever exposes a plain-text body; fall back to stripping the HTML one.
    description = payload.get("descriptionPlain") or html_to_text(payload.get("description") or "")
    return _Fields(
        title=payload.get("text") or "",
        description=description,
        company=company_hint or "",  # Lever payloads carry no company name
        location_raw=location_raw or "",
        is_remote=False,  # no explicit remote flag in the field map; text decides
        url=payload.get("hostedUrl") or "",
        posted_at=parse_date(payload.get("createdAt"), "lever"),
    )


def ashby_posted_value(payload: dict) -> str | int | float | None:
    """Return Ashby's posting timestamp, preferring ``publishedAt`` (LLD §3.5).

    Ashby exposes both ``publishedAt`` and ``updatedAt`` as ISO8601 strings; the
    publish date is the canonical "posted" time, with the update date as the
    fallback. Shared by the adapter's recency pre-filter and :func:`normalize` so
    the date selection is not duplicated.
    """
    return payload.get("publishedAt") or payload.get("updatedAt")


def _extract_ashby(payload: dict, company_hint: str | None) -> _Fields:
    """Map an Ashby posting-API object to common fields (LLD §3.5)."""
    # Prefer the plain-text body, falling back to stripping the HTML one.
    description = payload.get("descriptionPlain") or html_to_text(
        payload.get("descriptionHtml") or ""
    )
    return _Fields(
        title=payload.get("title") or "",
        description=description,
        company=company_hint or "",  # Ashby payloads carry no company name
        location_raw=payload.get("location") or "",
        # ``workplaceType == "Remote"`` is a strong, explicit remote signal.
        is_remote=payload.get("workplaceType") == "Remote",
        url=payload.get("jobUrl") or "",
        posted_at=parse_date(ashby_posted_value(payload), "ashby"),
    )


def _extract_adzuna(payload: dict, company_hint: str | None) -> _Fields:
    """Map an Adzuna ``ca/search`` result object to common fields (LLD §3.6)."""
    company = payload.get("company")
    company_name = company.get("display_name", "") if isinstance(company, dict) else ""
    location = payload.get("location")
    location_raw = location.get("display_name", "") if isinstance(location, dict) else ""
    return _Fields(
        title=payload.get("title") or "",
        # Adzuna descriptions are snippets that may carry HTML/entities; strip them.
        description=html_to_text(payload.get("description") or ""),
        company=company_name or company_hint or "",
        location_raw=location_raw or "",
        is_remote=False,  # Adzuna has no explicit remote flag; text decides
        # ``redirect_url`` is the public posting link (also scanned for board
        # tokens by discovery, LLD §3.6).
        url=payload.get("redirect_url") or "",
        posted_at=parse_date(payload.get("created"), "adzuna"),
    )


def _extract_remotive(payload: dict, company_hint: str | None) -> _Fields:
    """Map a Remotive ``remote-jobs`` result object to common fields.

    Every Remotive posting is remote, so ``is_remote`` is hard-set; the
    ``candidate_required_location`` (e.g. "Canada", "USA only", "Worldwide")
    becomes the free-text location so the bucketing heuristics can still scope it.
    """
    return _Fields(
        title=payload.get("title") or "",
        # Remotive descriptions are HTML; strip to text.
        description=html_to_text(payload.get("description") or ""),
        company=payload.get("company_name") or company_hint or "",
        location_raw=payload.get("candidate_required_location") or "",
        is_remote=True,  # Remotive is a remote-only board
        url=payload.get("url") or "",
        posted_at=parse_date(payload.get("publication_date"), "remotive"),
    )


def _extract_themuse(payload: dict, company_hint: str | None) -> _Fields:
    """Map a The Muse public-jobs result object to common fields."""
    company = payload.get("company")
    company_name = company.get("name", "") if isinstance(company, dict) else ""
    locations = payload.get("locations")
    location_raw = ""
    if isinstance(locations, list) and locations:
        first = locations[0]
        location_raw = first.get("name", "") if isinstance(first, dict) else ""
    refs = payload.get("refs")
    url = refs.get("landing_page", "") if isinstance(refs, dict) else ""
    return _Fields(
        title=payload.get("name") or "",
        # The Muse delivers the body as HTML in ``contents``.
        description=html_to_text(payload.get("contents") or ""),
        company=company_name or company_hint or "",
        location_raw=location_raw or "",
        # No explicit remote flag; "Flexible / Remote" in the location text decides.
        is_remote=False,
        url=url or "",
        posted_at=parse_date(payload.get("publication_date"), "themuse"),
    )


# Source name -> field extractor. Each adapter's payload shape is mapped here.
_EXTRACTORS = {
    "greenhouse": _extract_greenhouse,
    "lever": _extract_lever,
    "ashby": _extract_ashby,
    "adzuna": _extract_adzuna,
    "remotive": _extract_remotive,
    "themuse": _extract_themuse,
}


def normalize(raw: RawPosting, *, company_hint: str | None, now: datetime) -> Job:
    """Map a source's verbatim payload into the unified :class:`Job` (LLD §4).

    Extracts source-specific fields, then applies the shared HTML/date/location/
    seniority heuristics. ``date_unknown`` is set when no posting date parses, so
    the job is kept and ranked low rather than silently dropped (LLD §5).
    """
    extractor = _EXTRACTORS.get(raw.source)
    if extractor is None:
        raise ValueError(f"no normalizer registered for source {raw.source!r}")
    fields = extractor(raw.payload, company_hint)
    bucket, is_remote = bucket_location(fields.location_raw, fields.is_remote)
    seniority = infer_seniority(fields.title, fields.description)
    return Job(
        id=make_job_id(raw.source, raw.source_id),
        source=raw.source,
        source_id=raw.source_id,
        company=fields.company,
        title=fields.title,
        description=fields.description,
        location_raw=fields.location_raw,
        is_remote=is_remote,
        location_bucket=bucket,
        seniority=seniority,
        url=fields.url,
        posted_at=fields.posted_at,
        date_unknown=fields.posted_at is None,
        first_seen_at=now,
        last_seen_at=now,
        raw=raw.payload,
    )


__all__ = [
    "EPOCH_MS_SOURCES",
    "ashby_posted_value",
    "bucket_location",
    "html_to_text",
    "infer_seniority",
    "is_people_manager",
    "normalize",
    "parse_date",
]
