"""The Muse aggregator adapter — keyless (optional key) source.

The Muse (https://www.themuse.com) exposes a public jobs API that works without
credentials at a modest rate limit; an optional ``THEMUSE_API_KEY`` raises that
ceiling but is never required, so unlike Adzuna this source is always enabled.

We query the ``Software Engineering`` category, once per configured location
(Canadian metros plus remote), letting the pipeline's role gate and scorer narrow
to backend roles. Results are paged via the ``page`` param and the response's
``page_count`` bounds the walk; a hard per-location page cap keeps each poll
within the free tier (LLD §3.6). Each location is a separate request (distinct
cache key), so one location erroring never loses another's results. Field access
is guarded, a malformed posting is skipped and counted, and a recency backstop on
``publication_date`` reports the ``fetched → kept_after_recency`` funnel (LLD §12).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
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

SOURCE_NAME = "themuse"

THEMUSE_URL = "https://www.themuse.com/api/public/jobs"
THEMUSE_CATEGORY = "Software Engineering"
# Canadian metros plus The Muse's remote value; covers the targeting buckets
# (remote > vancouver > toronto > other_canada incl. Ottawa/Montreal).
THEMUSE_LOCATIONS: tuple[str, ...] = (
    "Vancouver, Canada",
    "Toronto, Canada",
    "Ottawa, Canada",
    "Montreal, Canada",
    "Flexible / Remote",
)
# Hard cap on pages walked per location so a large result set can't exhaust the
# (especially key-free) rate limit.
THEMUSE_MAX_PAGES = 2


class TheMuseSource:
    """The Muse aggregator adapter implementing the :class:`Source` protocol."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        client: HttpClient,
        api_key: str | None,
        category: str = THEMUSE_CATEGORY,
        locations: Sequence[str] = THEMUSE_LOCATIONS,
        max_pages: int = THEMUSE_MAX_PAGES,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._category = category
        self._locations = tuple(locations)
        self._max_pages = max_pages
        # Injectable clock so the recency backstop is deterministic in tests.
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch Software Engineering postings per location; ``throttle_s`` is
        honored by the shared HTTP client's per-host throttle.
        """
        result = SourceResult(source=self.name)
        now = self._now()
        for location in self._locations:
            for page in range(1, self._max_pages + 1):
                page_count = self._fetch_page(
                    location, page, now=now, max_age_days=max_age_days, result=result
                )
                if page_count is None or page >= page_count:
                    # A page error (None) or the last page bounds the walk.
                    break
        return result

    def _params(self, location: str, page: int) -> dict[str, str]:
        params = {"category": self._category, "location": location, "page": str(page)}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def _fetch_page(
        self,
        location: str,
        page: int,
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> int | None:
        """Fetch one page for one location; return its ``page_count`` or ``None``."""
        try:
            payload = self._client.get_json(THEMUSE_URL, params=self._params(location, page))
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            note = f"{self.name}: {location!r} page {page} fetch failed: {exc!r}"
            logger.warning(note)
            result.errors.append(note)
            return None

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            note = f"{self.name}: {location!r} page {page} unexpected payload shape"
            logger.warning(note)
            result.errors.append(note)
            return None

        for posting in results:
            self._consume_posting(posting, now=now, max_age_days=max_age_days, result=result)
        page_count = payload.get("page_count")
        return page_count if isinstance(page_count, int) else page

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

        # ``publication_date`` is ISO8601; an unparseable date keeps the posting
        # (flagged date_unknown downstream) rather than dropping it (spec §7).
        posted_at = parse_date(posting.get("publication_date"), self.name)
        if posted_at is not None and (now - posted_at).days > max_age_days:
            return  # stale: excluded before normalize/embed (LLD §3.6)

        result.kept_after_recency += 1
        result.raw.append(
            RawPosting(
                source=self.name,
                source_id=str(raw_id),
                payload=posting,
                company_hint=None,  # The Muse carries company.name in-payload
            )
        )


def build_themuse_source(settings: Settings) -> TheMuseSource:
    """Construct the adapter from settings: its (optional) key and the client."""
    return TheMuseSource(client=get_default_client(), api_key=settings.themuse_api_key)


register_source(SOURCE_NAME, build_themuse_source)


__all__ = [
    "SOURCE_NAME",
    "THEMUSE_CATEGORY",
    "THEMUSE_LOCATIONS",
    "THEMUSE_MAX_PAGES",
    "THEMUSE_URL",
    "TheMuseSource",
    "build_themuse_source",
]
