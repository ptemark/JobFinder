"""Remotive aggregator adapter — keyless, remote-only source.

Remotive (https://remotive.com) publishes a free, key-free JSON feed of remote
jobs. We query the ``software-dev`` category and let the pipeline's role gate and
scorer narrow it to backend roles; every Remotive posting is remote, which maps
cleanly onto the ``remote`` location bucket (great for the remote-Canada target).

The feed returns the full result set in a single request (no paging), so this
adapter makes one call, applies a recency backstop using each job's
``publication_date``, and reports the ``fetched → kept_after_recency`` funnel
(LLD §12). Field access is guarded: a malformed posting is skipped and counted,
and a request error yields an empty result with a note rather than raising.
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

SOURCE_NAME = "remotive"

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
# Remotive's software-development category slug; the pipeline filters to backend.
REMOTIVE_CATEGORY = "software-dev"
# Cap the single-request result set so a huge feed can't balloon a poll.
REMOTIVE_LIMIT = 100


class RemotiveSource:
    """Remotive aggregator adapter implementing the :class:`Source` protocol."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        client: HttpClient,
        category: str = REMOTIVE_CATEGORY,
        limit: int = REMOTIVE_LIMIT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._category = category
        self._limit = limit
        # Injectable clock so the recency backstop is deterministic in tests.
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch remote software-dev postings; ``throttle_s`` is honored by the
        shared HTTP client's per-host throttle, so this adapter needn't re-apply it.
        """
        result = SourceResult(source=self.name)
        params = {"category": self._category, "limit": str(self._limit)}
        try:
            payload = self._client.get_json(REMOTIVE_URL, params=params)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            note = f"{self.name}: fetch failed: {exc!r}"
            logger.warning(note)
            result.errors.append(note)
            return result

        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            note = f"{self.name}: unexpected payload shape (no jobs list)"
            logger.warning(note)
            result.errors.append(note)
            return result

        now = self._now()
        for posting in jobs:
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
                company_hint=None,  # Remotive carries company_name in-payload
            )
        )


def build_remotive_source(settings: Settings) -> RemotiveSource:
    """Construct the adapter. Remotive is keyless, so only the client is needed."""
    return RemotiveSource(client=get_default_client())


register_source(SOURCE_NAME, build_remotive_source)


__all__ = [
    "REMOTIVE_CATEGORY",
    "REMOTIVE_LIMIT",
    "REMOTIVE_URL",
    "SOURCE_NAME",
    "RemotiveSource",
    "build_remotive_source",
]
