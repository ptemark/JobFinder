"""Tests for board-token discovery (LLD §3.6, task T23).

``extract_tokens`` is a pure regex scan; ``harvest_tokens`` appends the newly
discovered boards to ``companies.yaml`` (and, optionally, the companies table) as
**unverified**, deduping against what is already known. No network is touched —
discovery only reads URLs it is handed.
"""

from __future__ import annotations

from pathlib import Path

from jobfinder.discovery import extract_tokens, harvest_tokens
from jobfinder.settings import CompaniesConfig, CompanyEntry, load_companies, save_companies
from jobfinder.store import connect, get_companies, init_db

# A realistic mix: full posting URLs (greenhouse/lever/ashby), the greenhouse API
# host (must NOT yield a token), a reserved ``embed`` route, and a duplicate.
_URLS = [
    "https://boards.greenhouse.io/acme/jobs/4012001",
    "https://boards.greenhouse.io/acme/jobs/4012002",  # duplicate token
    "https://jobs.lever.co/globex/abc-0001",
    "https://jobs.ashbyhq.com/initech/ash-1001",
    "https://boards-api.greenhouse.io/v1/boards/widgets/jobs",  # API host: ignored
    "https://boards.greenhouse.io/embed/job_board?for=hidden",  # reserved segment
    "",  # empty URL skipped
]


# --- extract_tokens (pure) --------------------------------------------------


def test_extract_tokens_finds_all_providers() -> None:
    found = extract_tokens(_URLS)
    assert found["greenhouse"] == {"acme"}  # duplicate deduped; API host + embed excluded
    assert found["lever"] == {"globex"}
    assert found["ashby"] == {"initech"}


def test_extract_tokens_empty_when_no_matches() -> None:
    found = extract_tokens(["https://www.adzuna.ca/details/ad-1001", ""])
    assert found == {"greenhouse": set(), "lever": set(), "ashby": set()}


# --- harvest_tokens ---------------------------------------------------------


def test_harvest_writes_new_tokens_as_unverified(tmp_path: Path) -> None:
    companies_path = tmp_path / "companies.yaml"

    added = harvest_tokens(_URLS, companies_path=companies_path)

    assert sorted(added) == [
        ("ashby", "initech"),
        ("greenhouse", "acme"),
        ("lever", "globex"),
    ]
    config = load_companies(companies_path)
    assert [(e.token, e.verified) for e in config.greenhouse] == [("acme", False)]
    assert [(e.token, e.verified) for e in config.lever] == [("globex", False)]
    assert [(e.token, e.verified) for e in config.ashby] == [("initech", False)]


def test_harvest_dedups_and_preserves_verified(tmp_path: Path) -> None:
    companies_path = tmp_path / "companies.yaml"
    # acme is already known and human-verified; newco is genuinely new.
    save_companies(
        companies_path,
        CompaniesConfig(greenhouse=[CompanyEntry(token="acme", name="Acme", verified=True)]),
    )

    added = harvest_tokens(
        [
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://boards.greenhouse.io/newco/jobs/2",
        ],
        companies_path=companies_path,
    )

    assert added == [("greenhouse", "newco")]  # acme not re-added
    config = load_companies(companies_path)
    greenhouse = {e.token: e for e in config.greenhouse}
    assert greenhouse["acme"].verified is True  # existing verification preserved
    assert greenhouse["acme"].name == "Acme"
    assert greenhouse["newco"].verified is False
    assert len(config.greenhouse) == 2  # no duplicate acme row


def test_harvest_no_matches_writes_nothing(tmp_path: Path) -> None:
    companies_path = tmp_path / "companies.yaml"

    added = harvest_tokens(
        ["https://www.adzuna.ca/details/ad-1001", "https://example.com/jobs"],
        companies_path=companies_path,
    )

    assert added == []
    assert not companies_path.exists()  # nothing discovered ⇒ no file written


def test_harvest_records_in_companies_table(tmp_path: Path) -> None:
    companies_path = tmp_path / "companies.yaml"
    conn = connect(":memory:")
    try:
        init_db(conn)
        added = harvest_tokens(
            ["https://jobs.lever.co/globex/abc-0001"],
            companies_path=companies_path,
            conn=conn,
        )
        assert added == [("lever", "globex")]
        rows = get_companies(conn, ats="lever")
    finally:
        conn.close()

    assert [(r["token"], r["verified"]) for r in rows] == [("globex", 0)]
    # And it still landed in the YAML the adapters actually read.
    assert any(e.token == "globex" for e in load_companies(companies_path).lever)
