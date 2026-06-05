"""Adzuna aggregator adapter (LLD §3.6) — optional, keyed source.

Adzuna is a job-search aggregator queried for Canadian backend roles. Unlike the
direct ATS feeds it requires free-tier API credentials (``app_id``/``app_key``
from ``.env``): when either is absent the source **skips cleanly**, returning an
empty :class:`SourceResult` with a note rather than raising (HLD §5.1, spec §5).

The endpoint applies recency server-side via ``max_days_old=max_age_days``; this
adapter *also* drops anything older than ``max_age_days`` using each result's
``created`` date as a backstop and so it can report the
``fetched → kept_after_recency`` funnel (LLD §12). Results are paged through the
``/search/{page}`` path; to stay within the free tier the walk is hard-capped at
:data:`ADZUNA_MAX_PAGES` pages and leans on the shared HTTP client's per-host
throttle and on-disk cache (LLD §3.2). Every field access is guarded; a malformed
result is skipped and counted, and a page error stops paging without losing the
results already gathered.
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
from jobfinder.sources.base import SourceResult, register_source
from jobfinder.sources.http import get_default_client

if TYPE_CHECKING:
    from jobfinder.settings import Settings
    from jobfinder.sources.http import HttpClient

logger = logging.getLogger(__name__)

SOURCE_NAME = "adzuna"

# Canadian job search; the page number is the final path segment (LLD §3.6).
ADZUNA_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/ca/search/{page}"
# Adzuna's per-request maximum (and our page size); kept small to respect the
# free tier (LLD §3.6: "throttle hard, cache aggressively, stay within free tier").
ADZUNA_RESULTS_PER_PAGE = 50
# Hard cap on pages walked per poll so a large result set can't exhaust the quota.
ADZUNA_MAX_PAGES = 3


class AdzunaSource:
    """Adzuna aggregator adapter implementing the :class:`Source` protocol."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        app_id: str | None,
        app_key: str | None,
        what: str,
        where: str | None,
        category: str | None,
        client: HttpClient,
        max_pages: int = ADZUNA_MAX_PAGES,
        results_per_page: int = ADZUNA_RESULTS_PER_PAGE,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._what = what
        self._where = where
        self._category = category
        self._client = client
        self._max_pages = max_pages
        self._results_per_page = results_per_page
        # Injectable clock so the recency backstop is deterministic in tests.
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch Canadian backend postings, skipping cleanly if keys are absent.

        ``throttle_s`` is honored by the shared HTTP client's per-host throttle
        (built from the same setting), so politeness holds without this adapter
        re-implementing it.
        """
        result = SourceResult(source=self.name)
        if not self._app_id or not self._app_key:
            # Optional keyed source: missing credentials disable it without error.
            note = f"{self.name}: skipped (Adzuna API credentials not set)"
            logger.info(note)
            result.errors.append(note)
            return result

        now = self._now()
        base_params = self._base_params(max_age_days)
        for page in range(1, self._max_pages + 1):
            count = self._fetch_page(
                page, base_params, now=now, max_age_days=max_age_days, result=result
            )
            if count is None or count < self._results_per_page:
                # A page error (None) or a short/empty page means no more results.
                break
        return result

    def _base_params(self, max_age_days: int) -> dict[str, str]:
        """Build the shared query params (credentials + targeting, LLD §3.6)."""
        params = {
            "app_id": self._app_id or "",
            "app_key": self._app_key or "",
            "what": self._what,
            "results_per_page": str(self._results_per_page),
            # Source-side recency filter so stale postings aren't even returned.
            "max_days_old": str(max_age_days),
        }
        if self._where:
            params["where"] = self._where
        if self._category:
            params["category"] = self._category
        return params

    def _fetch_page(
        self,
        page: int,
        base_params: dict[str, str],
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> int | None:
        """Fetch one page; return its result count, or ``None`` on error."""
        url = ADZUNA_SEARCH_URL.format(page=page)
        try:
            payload = self._client.get_json(url, params=base_params)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            note = f"{self.name}: page {page} fetch failed: {exc!r}"
            logger.warning(note)
            result.errors.append(note)
            return None

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            note = f"{self.name}: page {page} unexpected payload shape (no results list)"
            logger.warning(note)
            result.errors.append(note)
            return None

        for posting in results:
            self._consume_posting(posting, now=now, max_age_days=max_age_days, result=result)
        return len(results)

    def _consume_posting(
        self,
        posting: object,
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> None:
        result.fetched += 1
        if not isinstance(posting, dict):
            result.errors.append(f"{self.name}: skipped non-object posting")
            return
        raw_id = posting.get("id")
        if raw_id is None:
            result.errors.append(f"{self.name}: skipped posting with no id")
            return

        # ``created`` is ISO8601; a posting with no parseable date is kept
        # (flagged date_unknown downstream) rather than dropped (spec §7, LLD §5).
        posted_at = parse_date(posting.get("created"), self.name)
        if posted_at is not None and (now - posted_at).days > max_age_days:
            return  # stale: excluded before normalize/embed (LLD §3.6)

        result.kept_after_recency += 1
        result.raw.append(
            RawPosting(
                source=self.name,
                source_id=str(raw_id),
                payload=posting,
                # Adzuna carries the company name in-payload; no hint needed.
                company_hint=None,
            )
        )


def build_adzuna_source(settings: Settings) -> AdzunaSource:
    """Construct the adapter from settings: its credentials, query, and client."""
    return AdzunaSource(
        app_id=settings.adzuna_app_id,
        app_key=settings.adzuna_app_key,
        what=settings.adzuna_what,
        where=settings.adzuna_where,
        category=settings.adzuna_category,
        client=get_default_client(),
    )


register_source(SOURCE_NAME, build_adzuna_source)


__all__ = [
    "ADZUNA_MAX_PAGES",
    "ADZUNA_RESULTS_PER_PAGE",
    "ADZUNA_SEARCH_URL",
    "SOURCE_NAME",
    "AdzunaSource",
    "build_adzuna_source",
]
