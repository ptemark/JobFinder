"""Tests for the Google Sheet application-tracker sync (LLD §16, M7).

All tests run offline via ``httpx.MockTransport`` with a faked OAuth token —
no network, no real service-account key (Testing Standards: fixtures only,
deterministic, free).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jobfinder import sheets
from jobfinder.sheets import SHEETS_APPLIED_RGB, SyncResult, sync_applied
from jobfinder.sources.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class FakeJob:
    """Minimal job shape the sync needs (LLD §16 AppliedJob)."""

    company: str
    title: str
    url: str


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _settings(*, enabled: bool = True, gid: int | None = None) -> SimpleNamespace:
    """A stand-in for Settings exposing only the sheets attributes (T32 adds them
    to the real Settings; the sync is duck-typed so it tests in isolation here)."""
    return SimpleNamespace(
        sheets_enabled=enabled,
        google_sheets_credentials=Path("config/fake-service-account.json"),
        job_tracker_sheet_id="sheet-abc",
        job_tracker_sheet_gid=gid,
    )


def _client(handler) -> HttpClient:
    return HttpClient(
        cache_dir=Path("/tmp/jobfinder-sheets-test-cache"),
        throttle_s=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _dt: None,
        rng=random.Random(0),
    )


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the service-account token mint with a fixed fake token."""
    monkeypatch.setattr(sheets, "_access_token", lambda _settings: "fake-token")
    sheets._creds_cache.clear()


class Recorder:
    """A MockTransport handler that records requests and serves canned responses."""

    def __init__(self, *, values: dict | None = None, append_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self._values = values if values is not None else _load("sheets_values.json")
        self._append_status = append_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith(":batchUpdate"):
            return httpx.Response(self._append_status, json={"replies": [{}]})
        if "/values/" in path:
            return httpx.Response(200, json=self._values)
        # Spreadsheet metadata GET.
        return httpx.Response(200, json=_load("sheets_metadata.json"))

    @property
    def append_request(self) -> httpx.Request | None:
        for req in self.requests:
            if req.method == "POST":
                return req
        return None


def test_unconfigured_skips_with_no_network() -> None:
    rec = Recorder()
    result = sync_applied(
        FakeJob("Acme", "Backend Engineer", "https://x/1"),
        settings=_settings(enabled=False),
        client=_client(rec),
    )
    assert result.status == "skipped"
    assert rec.requests == []  # zero network calls when unconfigured


def test_append_builds_correct_request() -> None:
    rec = Recorder()
    job = FakeJob("Acme Corp", "Senior Backend Engineer", "https://jobs.example.com/new")
    result = sync_applied(job, settings=_settings(), client=_client(rec))

    assert result.status == "appended"
    req = rec.append_request
    assert req is not None
    assert req.headers["Authorization"] == "Bearer fake-token"

    body = json.loads(req.content)
    cells = body["requests"][0]["appendCells"]
    assert cells["sheetId"] == 0  # first sheet (no gid configured)
    assert cells["fields"] == "userEnteredValue,userEnteredFormat.backgroundColor"
    values = cells["rows"][0]["values"]
    assert values[0]["userEnteredValue"]["stringValue"] == "Acme Corp"
    assert values[1]["userEnteredValue"]["stringValue"] == "Senior Backend Engineer"
    # Response cell: no value, yellow background only (spec §15).
    assert "userEnteredValue" not in values[2]
    assert values[2]["userEnteredFormat"]["backgroundColor"] == SHEETS_APPLIED_RGB
    assert values[3]["userEnteredValue"]["stringValue"] == "https://jobs.example.com/new"


def test_existing_link_is_duplicate_no_append() -> None:
    rec = Recorder()
    # The fixture's Link column already contains this URL.
    job = FakeJob("Acme", "Backend Engineer", "https://jobs.example.com/existing")
    result = sync_applied(job, settings=_settings(), client=_client(rec))

    assert result.status == "duplicate"
    assert rec.append_request is None  # idempotency: no append issued


def test_gid_selects_matching_worksheet() -> None:
    rec = Recorder(values={"values": []})  # empty Link column → not a duplicate
    job = FakeJob("Acme", "Backend Engineer", "https://jobs.example.com/new")
    result = sync_applied(job, settings=_settings(gid=123456), client=_client(rec))

    assert result.status == "appended"
    body = json.loads(rec.append_request.content)
    assert body["requests"][0]["appendCells"]["sheetId"] == 123456


def test_sheets_http_error_is_caught() -> None:
    rec = Recorder(append_status=500)
    job = FakeJob("Acme", "Backend Engineer", "https://jobs.example.com/new")
    result = sync_applied(job, settings=_settings(), client=_client(rec))

    assert result.status == "error"  # caught, never raised
    assert isinstance(result, SyncResult)
