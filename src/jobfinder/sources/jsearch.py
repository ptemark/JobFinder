"""JSearch aggregator adapter (RapidAPI) — optional, keyed source.

JSearch (https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) aggregates
Google for Jobs — pulling postings sourced from Indeed, LinkedIn, Glassdoor,
ZipRecruiter and more — so it is the broadest-coverage source here. It requires a
RapidAPI key sent as request headers (``X-RapidAPI-Key``/``X-RapidAPI-Host``);
when the key is absent the source **skips cleanly**, returning an empty
:class:`SourceResult` with a note rather than raising (HLD §5.1, spec §5).

The endpoint narrows by recency server-side via ``date_posted`` (the smallest
bucket covering ``max_age_days``); this adapter *also* drops anything older than
``max_age_days`` using each result's ``job_posted_at_datetime_utc`` as a backstop
and so reports the ``fetched → kept_after_recency`` funnel (LLD §12). A single
request fetches ``num_pages`` pages (≈10 results each) to respect the limited free
tier; every field access is guarded so a malformed result is skipped and counted.
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

SOURCE_NAME = "jsearch"

JSEARCH_HOST = "jsearch.p.rapidapi.com"
JSEARCH_SEARCH_URL = f"https://{JSEARCH_HOST}/search"


def _date_posted(max_age_days: int) -> str:
    """Map ``max_age_days`` onto JSearch's coarse ``date_posted`` buckets.

    Picks the smallest bucket that still covers the window; the adapter's own
    recency backstop then trims to the exact ``max_age_days``.
    """
    if max_age_days <= 1:
        return "today"
    if max_age_days <= 3:
        return "3days"
    if max_age_days <= 7:
        return "week"
    if max_age_days <= 31:
        return "month"
    return "all"


class JSearchSource:
    """JSearch aggregator adapter implementing the :class:`Source` protocol."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        api_key: str | None,
        query: str,
        country: str,
        num_pages: int,
        client: HttpClient,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._query = query
        self._country = country
        self._num_pages = num_pages
        self._client = client
        # Injectable clock so the recency backstop is deterministic in tests.
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch aggregated postings, skipping cleanly if the key is absent.

        ``throttle_s`` is honored by the shared HTTP client's per-host throttle.
        """
        result = SourceResult(source=self.name)
        if not self._api_key:
            note = f"{self.name}: skipped (JSearch RapidAPI key not set)"
            logger.info(note)
            result.errors.append(note)
            return result

        params = {
            "query": self._query,
            "country": self._country,
            "page": "1",
            "num_pages": str(self._num_pages),
            "date_posted": _date_posted(max_age_days),
        }
        headers = {"X-RapidAPI-Key": self._api_key, "X-RapidAPI-Host": JSEARCH_HOST}
        try:
            payload = self._client.get_json(JSEARCH_SEARCH_URL, params=params, headers=headers)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            note = f"{self.name}: fetch failed: {exc!r}"
            logger.warning(note)
            result.errors.append(note)
            return result

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            note = f"{self.name}: unexpected payload shape (no data list)"
            logger.warning(note)
            result.errors.append(note)
            return result

        now = self._now()
        for posting in data:
            self._consume_posting(posting, now=now, max_age_days=max_age_days, result=result)
        return result

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
        raw_id = posting.get("job_id")
        if raw_id is None:
            result.errors.append(f"{self.name}: skipped posting with no job_id")
            return

        # ``job_posted_at_datetime_utc`` is ISO8601; an unparseable date keeps the
        # posting (flagged date_unknown downstream) rather than dropping it (spec §7).
        posted_at = parse_date(posting.get("job_posted_at_datetime_utc"), self.name)
        if posted_at is not None and (now - posted_at).days > max_age_days:
            return  # stale: excluded before normalize/embed (LLD §3.6)

        result.kept_after_recency += 1
        result.raw.append(
            RawPosting(
                source=self.name,
                source_id=str(raw_id),
                payload=posting,
                company_hint=None,  # JSearch carries employer_name in-payload
            )
        )


def build_jsearch_source(settings: Settings) -> JSearchSource:
    """Construct the adapter from settings: its key, query, country, and client."""
    return JSearchSource(
        api_key=settings.jsearch_api_key,
        query=settings.jsearch_query,
        country=settings.jsearch_country,
        num_pages=settings.jsearch_num_pages,
        client=get_default_client(),
    )


register_source(SOURCE_NAME, build_jsearch_source)


__all__ = [
    "JSEARCH_HOST",
    "JSEARCH_SEARCH_URL",
    "SOURCE_NAME",
    "JSearchSource",
    "build_jsearch_source",
]
