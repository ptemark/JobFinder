"""Google Sheet application-tracker sync (LLD §16, M7).

When the user marks a job ``applied`` in the dashboard, the local backend appends
a row to the user's personal tracking spreadsheet (``Company | Position | Response
| Link``), leaving the **Response** cell blank but shaded yellow — the user's
"applied, waiting to hear back" colour convention (spec §15).

This is the tool's only outbound *write*, and it targets the **user's own** sheet —
never an employer's apply endpoint (the §3 no-auto-apply guardrail still holds).
It is deliberately:

* **Opt-in & graceful** — active only when both the service-account key path and the
  sheet id are configured; otherwise it returns ``skipped`` with no network, exactly
  like the absent-Adzuna-key pattern (LLD §3.6/§11.4).
* **Idempotent** — reads the Link column first and skips a row whose URL is already
  present, so re-marking ``applied`` or a retry never duplicates.
* **Best-effort / bulkheaded** — every network failure is caught and returned as an
  ``error`` ``SyncResult``; it never raises into the request handler, because the
  authoritative status write has already committed (LLD §9.1/§16.1, HLD §3.7/D14).

The two REST calls reuse the existing shared :class:`~jobfinder.sources.http.HttpClient`;
only the service-account JWT signing is delegated to ``google-auth`` (HLD §3.7/D13).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account

if TYPE_CHECKING:
    from jobfinder.settings import Settings

logger = logging.getLogger(__name__)

# --- Constants, each sourced from the design docs ---------------------------

# Sheets REST base; the spreadsheet id is appended per call (LLD §16.1).
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
# OAuth scope for read+write of the user's spreadsheet (LLD §16.1 step 2).
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
# Yellow = RGB (255,255,0); the "applied, waiting to hear back" convention
# (spec §15, LLD §16.2). Kept here so the shade is swappable in one place.
SHEETS_APPLIED_RGB = {"red": 1, "green": 1, "blue": 0}
# Column D (0-indexed 3) holds the canonical posting Link; the idempotency read
# scans it (LLD §16.1 step 3 / §16.2: column order Company|Position|Response|Link).
SHEETS_LINK_COLUMN = "D"
# appendCells writes the values + the Response cell background atomically (LLD §16.1).
_APPEND_FIELDS = "userEnteredValue,userEnteredFormat.backgroundColor"


class AppliedJob(Protocol):
    """The minimal shape :func:`sync_applied` needs from a job (LLD §16).

    Both the domain :class:`~jobfinder.models.Job` and the API's stored row expose
    these, so the caller (the status endpoint, T32) can pass either.
    """

    company: str
    title: str
    url: str


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a sync attempt (LLD §16).

    The API maps ``status == "appended"`` → ``sheet_synced=True`` and everything
    else → ``False``; only ``"error"`` is a real failure (logged, never raised).
    """

    status: str  # "appended" | "skipped" | "duplicate" | "error"
    detail: str


# Network/auth failures that must degrade to an ``error`` result, never propagate
# into the request handler (LLD §16.1 step 5 bulkhead). OSError covers a missing or
# unreadable service-account key file; ValueError a malformed metadata response.
_SYNC_ERRORS = (httpx.HTTPError, GoogleAuthError, OSError, ValueError, KeyError)

# Caches the credentials object per key path so repeated ``applied`` writes reuse
# one token (it auto-refreshes near expiry); LLD §16.1 step 2.
_creds_cache: dict[str, service_account.Credentials] = {}


def sync_applied(
    job: AppliedJob,
    *,
    settings: Settings,
    client: HttpClientProto | None = None,
) -> SyncResult:
    """Append an ``applied`` job to the user's tracking sheet (best-effort).

    Returns a :class:`SyncResult`; never raises — the authoritative status write
    has already committed by the time this runs (LLD §16.1).
    """
    if not settings.sheets_enabled:
        # Adzuna-key degradation pattern: unconfigured → no network, clean skip.
        return SyncResult("skipped", "Google Sheets sync not configured")

    http = client if client is not None else _default_client(settings)
    try:
        token = _access_token(settings)
        auth = {"Authorization": f"Bearer {token}"}
        sheet_id, sheet_title = _resolve_target_sheet(settings, http, auth)
        if _link_already_present(settings, http, auth, sheet_title, job.url):
            return SyncResult("duplicate", f"row already present for {job.url}")
        _append_row(settings, http, auth, sheet_id, job)
        return SyncResult("appended", f"appended {job.company} — {job.title}")
    except _SYNC_ERRORS as exc:
        # Bulkhead: log + surface, but the status write stands (HLD §3.7/D14).
        logger.warning("Sheets sync failed for %s: %r", getattr(job, "url", "?"), exc)
        return SyncResult("error", str(exc))


def _access_token(settings: Settings) -> str:
    """Mint (or reuse) an OAuth2 access token from the service-account key.

    Isolated so tests can monkeypatch it with a fake token, avoiding a real key
    file and the network token exchange (LLD §16.2).
    """
    key_path = str(settings.google_sheets_credentials)
    creds = _creds_cache.get(key_path)
    if creds is None:
        creds = service_account.Credentials.from_service_account_file(key_path, scopes=SHEETS_SCOPE)
        _creds_cache[key_path] = creds
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def _resolve_target_sheet(
    settings: Settings, http: HttpClientProto, auth: dict[str, str]
) -> tuple[int, str]:
    """Resolve the target worksheet's ``(sheetId, title)`` (LLD §16.1).

    ``appendCells`` needs the numeric ``sheetId`` and the idempotency read needs
    the title for its A1 range. When ``job_tracker_sheet_gid`` is set we select that
    sheet; otherwise we default to the first sheet on the spreadsheet.
    """
    url = f"{SHEETS_API_BASE}/{settings.job_tracker_sheet_id}"
    meta = http.get_json(
        url,
        params={"fields": "sheets.properties(sheetId,title,index)"},
        headers=auth,
        ttl_s=0,  # never cache: the sheet layout can change between polls
    )
    worksheets = meta.get("sheets") or []
    if not worksheets:
        raise ValueError("spreadsheet has no sheets")

    gid = settings.job_tracker_sheet_gid
    if gid is not None:
        for worksheet in worksheets:
            props = worksheet.get("properties", {})
            if props.get("sheetId") == gid:
                return gid, props["title"]
        raise ValueError(f"no worksheet with gid {gid}")

    first = worksheets[0].get("properties", {})
    return first["sheetId"], first["title"]


def _link_already_present(
    settings: Settings,
    http: HttpClientProto,
    auth: dict[str, str],
    sheet_title: str,
    url: str,
) -> bool:
    """Return True if ``url`` is already in the sheet's Link column (idempotency)."""
    a1_range = f"{sheet_title}!{SHEETS_LINK_COLUMN}:{SHEETS_LINK_COLUMN}"
    values_url = f"{SHEETS_API_BASE}/{settings.job_tracker_sheet_id}/values/{quote(a1_range)}"
    body = http.get_json(values_url, headers=auth, ttl_s=0)
    rows = body.get("values") or []
    return any(row and row[0] == url for row in rows)


def _append_row(
    settings: Settings,
    http: HttpClientProto,
    auth: dict[str, str],
    sheet_id: int,
    job: AppliedJob,
) -> None:
    """Append the four-cell row in one ``appendCells`` batchUpdate (LLD §16.1)."""
    request_body = {
        "requests": [
            {
                "appendCells": {
                    "sheetId": sheet_id,
                    "fields": _APPEND_FIELDS,
                    "rows": [
                        {
                            "values": [
                                {"userEnteredValue": {"stringValue": job.company}},
                                {"userEnteredValue": {"stringValue": job.title}},
                                # Response: blank, yellow background only (spec §15).
                                {
                                    "userEnteredFormat": {
                                        "backgroundColor": dict(SHEETS_APPLIED_RGB)
                                    }
                                },
                                {"userEnteredValue": {"stringValue": job.url}},
                            ]
                        }
                    ],
                }
            }
        ]
    }
    update_url = f"{SHEETS_API_BASE}/{settings.job_tracker_sheet_id}:batchUpdate"
    http.post_json(update_url, json_body=request_body, headers=auth)


def _default_client(settings: Settings) -> HttpClientProto:
    """Build a standalone shared HTTP client for a real (non-test) sync call."""
    from jobfinder.sources.http import HttpClient

    return HttpClient(
        cache_dir=settings.cache_dir,
        throttle_s=settings.throttle_s,
        cache_ttl_s=settings.cache_ttl_s,
    )


class HttpClientProto(Protocol):
    """The slice of :class:`~jobfinder.sources.http.HttpClient` this module uses."""

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = ...,
        headers: dict[str, str] | None = ...,
        ttl_s: int | None = ...,
    ) -> object: ...

    def post_json(
        self,
        url: str,
        *,
        json_body: object,
        headers: dict[str, str] | None = ...,
    ) -> object: ...


__all__ = [
    "AppliedJob",
    "SyncResult",
    "sync_applied",
    "SHEETS_APPLIED_RGB",
    "SHEETS_API_BASE",
    "SHEETS_SCOPE",
]
