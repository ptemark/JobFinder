"""Board-token discovery from aggregator URLs (LLD §3.6 / T23).

The aggregator (Adzuna) and any posting URL may reference an ATS board directly —
``boards.greenhouse.io/{token}``, ``jobs.lever.co/{site}``,
``jobs.ashbyhq.com/{board}``. :func:`harvest_tokens` scans a batch of URLs for
those patterns and appends any *previously unknown* board as an **unverified**
company entry, so a later poll can pick it up once a human confirms the token.

It is purely additive and deduped: a token already in ``companies.yaml`` (whether
verified or not) is never re-added or downgraded, which keeps re-running the poll
idempotent (HLD §4.4). Discovered entries are unverified by design — discovery
only *suggests* boards; verification stays a human decision (spec §5).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from jobfinder.settings import CompaniesConfig, CompanyEntry, load_companies, save_companies
from jobfinder.store import add_company

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable
    from datetime import datetime

logger = logging.getLogger(__name__)

# ATS board-URL patterns (LLD §3.6). The capturing group is the board token — the
# first path segment after the host, up to the next ``/``, ``?`` or ``#``. The
# literal ``boards\.greenhouse\.io`` deliberately does **not** match the API host
# ``boards-api.greenhouse.io`` (the ``-api`` breaks the literal-dot match), so a
# greenhouse API URL never yields "v1" as a bogus token.
_TOKEN = r"[A-Za-z0-9_-]+"
_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(rf"boards\.greenhouse\.io/({_TOKEN})"),
    "lever": re.compile(rf"jobs\.lever\.co/({_TOKEN})"),
    "ashby": re.compile(rf"jobs\.ashbyhq\.com/({_TOKEN})"),
}

# Greenhouse path segments that are routes, not board tokens (LLD §3.6). Guards
# against ``boards.greenhouse.io/embed/job_board`` yielding a bogus "embed" token.
_RESERVED_SEGMENTS = frozenset({"embed"})


def extract_tokens(urls: Iterable[str]) -> dict[str, set[str]]:
    """Return the ATS board tokens referenced by ``urls``, keyed by provider.

    Pure (no I/O): each URL is scanned against every provider pattern. Empty or
    ``None`` URLs are skipped and reserved route segments are filtered out.
    """
    found: dict[str, set[str]] = {ats: set() for ats in _PATTERNS}
    for url in urls:
        if not url:
            continue
        for ats, pattern in _PATTERNS.items():
            for match in pattern.finditer(url):
                token = match.group(1)
                if token not in _RESERVED_SEGMENTS:
                    found[ats].add(token)
    return found


def harvest_tokens(
    urls: Iterable[str],
    *,
    companies_path: str | Path,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Discover board tokens in ``urls`` and append the new ones as unverified.

    Loads the existing ``companies.yaml`` (an absent file is treated as empty),
    extracts tokens, and for every ``(ats, token)`` not already listed appends an
    unverified :class:`CompanyEntry` and writes the file back. When ``conn`` is
    given the same new entries are also recorded in the ``companies`` table (a
    persistent ledger; :func:`store.add_company` dedupes there independently and
    never downgrades a verified row). Returns the newly added ``(ats, token)``
    pairs, sorted per provider — empty when nothing new was found.
    """
    found = extract_tokens(urls)
    if not any(found.values()):
        return []

    config = _load_or_empty(companies_path)
    added: list[tuple[str, str]] = []
    for ats, tokens in found.items():
        entries: list[CompanyEntry] = getattr(config, ats)
        known = {entry.token for entry in entries}
        for token in sorted(tokens - known):
            entries.append(CompanyEntry(token=token, verified=False))
            added.append((ats, token))

    if not added:
        return []

    save_companies(companies_path, config)
    if conn is not None:
        for ats, token in added:
            add_company(conn, ats, token, verified=False, now=now)
    logger.info("discovery: added %d new unverified board token(s)", len(added))
    return added


def _load_or_empty(companies_path: str | Path) -> CompaniesConfig:
    """Load ``companies.yaml``, or return an empty config when the file is absent."""
    if not Path(companies_path).exists():
        return CompaniesConfig()
    return load_companies(companies_path)


__all__ = ["extract_tokens", "harvest_tokens"]
