"""Lever Postings API adapter (LLD §3.4).

Fetches the active postings for each configured Lever site and returns them as
:class:`RawPosting`s, **dropping anything older than ``max_age_days`` before
returning** — the postings API has no server-side date filter, so the recency
gate is applied here so stale postings never reach normalization, embedding, or
scoring (spec §5, HLD §3.1).

Unlike Greenhouse, Lever returns a bare JSON **array** and paginates via
``skip``/``limit`` rather than handing back the whole board at once, so this
adapter walks pages until a short (final) page is returned. Every field access
is guarded: a malformed posting is skipped and counted, and one site failing
(HTTP error, bad JSON, unexpected shape) is isolated so the other configured
sites still return (per-site bulkhead, RALPH No-Shortcut rules). The verbatim
provider object is preserved on each ``RawPosting`` for normalization (LLD §4)
and debugging; the company name is supplied at normalize time from
``companies.yaml`` (Lever payloads carry no company field, LLD §3.4).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from jobfinder.models import RawPosting
from jobfinder.normalize import parse_date
from jobfinder.settings import load_companies
from jobfinder.sources.base import SourceResult, register_source
from jobfinder.sources.http import get_default_client

if TYPE_CHECKING:
    from jobfinder.settings import CompanyEntry, Settings
    from jobfinder.sources.http import HttpClient

logger = logging.getLogger(__name__)

SOURCE_NAME = "lever"

# Public postings API: active postings for a site, no auth (LLD §3.4).
LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/{site}"
# ``mode=json`` returns structured objects rather than rendered HTML (LLD §3.4).
LEVER_MODE = "json"
# Page size for the ``skip``/``limit`` pagination (LLD §3.4 documents ``limit``).
LEVER_PAGE_LIMIT = 100
# Defensive cap on pages walked per site so a misbehaving feed that never
# returns a short page cannot loop forever (≥ LEVER_PAGE_LIMIT * cap postings).
LEVER_MAX_PAGES = 50


class LeverSource:
    """Lever postings adapter implementing the :class:`Source` protocol."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        companies: list[CompanyEntry],
        client: HttpClient,
        now: Callable[[], datetime] | None = None,
        page_limit: int = LEVER_PAGE_LIMIT,
    ) -> None:
        self._companies = companies
        self._client = client
        # Injectable clock so the recency pre-filter is deterministic in tests.
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        # Injectable page size keeps pagination testable without 100+ fixtures.
        self._page_limit = page_limit

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch every configured site, dropping stale postings (LLD §3.4).

        ``throttle_s`` is honored by the shared HTTP client's per-host throttle
        (it is built from the same setting), so politeness holds without this
        adapter re-implementing it.
        """
        result = SourceResult(source=self.name)
        now = self._now()
        for company in self._companies:
            self._fetch_site(company, now=now, max_age_days=max_age_days, result=result)
        return result

    def _fetch_site(
        self,
        company: CompanyEntry,
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> None:
        url = LEVER_POSTINGS_URL.format(site=company.token)
        for skip in range(0, self._page_limit * LEVER_MAX_PAGES, self._page_limit):
            page = self._fetch_page(url, company, skip=skip, result=result)
            if page is None:
                return  # fetch/shape error already noted; abandon this site
            for posting in page:
                self._consume_posting(
                    posting, company, now=now, max_age_days=max_age_days, result=result
                )
            if len(page) < self._page_limit:
                return  # short page = last page (LLD §3.4)

    def _fetch_page(
        self,
        url: str,
        company: CompanyEntry,
        *,
        skip: int,
        result: SourceResult,
    ) -> list | None:
        params = {"mode": LEVER_MODE, "limit": str(self._page_limit), "skip": str(skip)}
        try:
            payload = self._client.get_json(url, params=params)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Per-site bulkhead: one site failing must not lose the others.
            note = f"{self.name}:{company.token}: fetch failed: {exc!r}"
            logger.warning(note)
            result.errors.append(note)
            return None

        if not isinstance(payload, list):
            note = f"{self.name}:{company.token}: unexpected payload shape (not a list)"
            logger.warning(note)
            result.errors.append(note)
            return None
        return payload

    def _consume_posting(
        self,
        posting: object,
        company: CompanyEntry,
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> None:
        result.fetched += 1
        if not isinstance(posting, dict):
            result.errors.append(f"{self.name}:{company.token}: skipped non-object posting")
            return
        raw_id = posting.get("id")
        if raw_id is None:
            result.errors.append(f"{self.name}:{company.token}: skipped posting with no id")
            return

        posted_at = parse_date(posting.get("createdAt"), self.name)
        # A posting with no parseable date is kept (flagged date_unknown
        # downstream) rather than silently dropped (spec §7, LLD §5).
        if posted_at is not None and (now - posted_at).days > max_age_days:
            return  # stale: excluded before normalize/embed (LLD §3.4)

        result.kept_after_recency += 1
        result.raw.append(RawPosting(source=self.name, source_id=str(raw_id), payload=posting))


def build_lever_source(settings: Settings) -> LeverSource:
    """Construct the adapter from settings: its sites + the shared HTTP client."""
    companies = load_companies(settings.config_dir / "companies.yaml").lever
    return LeverSource(companies=companies, client=get_default_client())


register_source(SOURCE_NAME, build_lever_source)


__all__ = [
    "LEVER_MAX_PAGES",
    "LEVER_MODE",
    "LEVER_PAGE_LIMIT",
    "LEVER_POSTINGS_URL",
    "SOURCE_NAME",
    "LeverSource",
    "build_lever_source",
]
