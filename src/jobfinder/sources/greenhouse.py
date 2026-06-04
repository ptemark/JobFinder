"""Greenhouse Job Board API adapter (LLD §3.3).

Fetches the full active board for each configured Greenhouse company token and
returns the postings as :class:`RawPosting`s, **dropping anything older than
``max_age_days`` before returning** — the board API exposes no server-side date
filter, so the recency gate is applied here so stale postings never reach
normalization, embedding, or scoring (spec §5, HLD §3.1).

Every field access is guarded: a malformed posting is skipped and counted, and
one board failing (HTTP error, bad JSON, unexpected shape) is isolated so the
other configured boards still return (per-board bulkhead, RALPH No-Shortcut
rules). The verbatim provider object is preserved on each ``RawPosting`` for
normalization (LLD §4) and debugging.
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

SOURCE_NAME = "greenhouse"

# Public board API: full active board for a company token, no auth (LLD §3.3).
GREENHOUSE_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
# ``content=true`` is required for the HTML job body to be included (LLD §3.3).
GREENHOUSE_PARAMS = {"content": "true"}


class GreenhouseSource:
    """Greenhouse board adapter implementing the :class:`Source` protocol."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        companies: list[CompanyEntry],
        client: HttpClient,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._companies = companies
        self._client = client
        # Injectable clock so the recency pre-filter is deterministic in tests.
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def fetch(self, *, max_age_days: int, throttle_s: float) -> SourceResult:
        """Fetch every configured board, dropping stale postings (LLD §3.3).

        ``throttle_s`` is honored by the shared HTTP client's per-host throttle
        (it is built from the same setting), so politeness holds without this
        adapter re-implementing it.
        """
        result = SourceResult(source=self.name)
        now = self._now()
        for company in self._companies:
            self._fetch_board(company, now=now, max_age_days=max_age_days, result=result)
        return result

    def _fetch_board(
        self,
        company: CompanyEntry,
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> None:
        url = GREENHOUSE_BOARD_URL.format(token=company.token)
        try:
            payload = self._client.get_json(url, params=GREENHOUSE_PARAMS)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Per-board bulkhead: one board failing must not lose the others.
            note = f"{self.name}:{company.token}: fetch failed: {exc!r}"
            logger.warning(note)
            result.errors.append(note)
            return

        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            note = f"{self.name}:{company.token}: unexpected payload shape (no jobs list)"
            logger.warning(note)
            result.errors.append(note)
            return

        for job in jobs:
            self._consume_job(job, company, now=now, max_age_days=max_age_days, result=result)

    def _consume_job(
        self,
        job: object,
        company: CompanyEntry,
        *,
        now: datetime,
        max_age_days: int,
        result: SourceResult,
    ) -> None:
        result.fetched += 1
        if not isinstance(job, dict):
            result.errors.append(f"{self.name}:{company.token}: skipped non-object posting")
            return
        raw_id = job.get("id")
        if raw_id is None:
            result.errors.append(f"{self.name}:{company.token}: skipped posting with no id")
            return

        posted_at = parse_date(job.get("updated_at"), self.name)
        # A posting with no parseable date is kept (flagged date_unknown
        # downstream) rather than silently dropped (spec §7, LLD §5).
        if posted_at is not None and (now - posted_at).days > max_age_days:
            return  # stale: excluded before normalize/embed (LLD §3.3)

        result.kept_after_recency += 1
        result.raw.append(
            RawPosting(
                source=self.name,
                source_id=str(raw_id),
                payload=job,
                # company_name is usually in the payload; the configured name/token
                # is the documented fallback for normalize (LLD §3.3 field map).
                company_hint=company.name or company.token,
            )
        )


def build_greenhouse_source(settings: Settings) -> GreenhouseSource:
    """Construct the adapter from settings: its boards + the shared HTTP client."""
    companies = load_companies(settings.config_dir / "companies.yaml").greenhouse
    return GreenhouseSource(companies=companies, client=get_default_client())


register_source(SOURCE_NAME, build_greenhouse_source)


__all__ = [
    "GREENHOUSE_BOARD_URL",
    "GREENHOUSE_PARAMS",
    "SOURCE_NAME",
    "GreenhouseSource",
    "build_greenhouse_source",
]
