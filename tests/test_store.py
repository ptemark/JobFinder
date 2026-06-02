"""Tests for the SQLite schema & connection layer (T04, LLD §7.1–§7.2) and the
job upsert/dedupe DAL (T05, LLD §7.3)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobfinder.models import Job, LocationBucket, Seniority, make_job_id
from jobfinder.store import connect, init_db, upsert_job

# Tables and indexes the DDL must create (LLD §7.2).
EXPECTED_TABLES = {"jobs", "scores", "status", "poll_runs", "companies"}
EXPECTED_INDEXES = {
    "ix_jobs_posted",
    "ix_jobs_bucket",
    "ix_jobs_elig",
    "ix_scores_final",
}


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {r["name"] for r in rows}


def test_connect_applies_pragmas(tmp_path: Path) -> None:
    """A file-backed connection has the LLD §7.1 PRAGMAs in effect."""
    conn = connect(tmp_path / "jobs.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # ON
    finally:
        conn.close()


def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    """connect creates a missing parent directory for the db file."""
    db_path = tmp_path / "nested" / "data" / "jobs.db"
    conn = connect(db_path)
    try:
        assert db_path.parent.is_dir()
    finally:
        conn.close()


def test_init_db_creates_all_tables_and_indexes() -> None:
    """init_db materializes every table and index from the DDL."""
    conn = connect(":memory:")
    try:
        init_db(conn)
        assert EXPECTED_TABLES <= _names(conn, "table")
        assert EXPECTED_INDEXES <= _names(conn, "index")
    finally:
        conn.close()


def test_init_db_is_idempotent_and_preserves_data() -> None:
    """Re-running init_db raises nothing and keeps existing rows."""
    conn = connect(":memory:")
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO jobs (id, source, source_id, title, "
            "first_seen_at, last_seen_at) VALUES "
            "('abc', 'greenhouse', '1', 'Backend Engineer', 't0', 't0')"
        )
        conn.commit()

        init_db(conn)  # second run must not drop the table or the row

        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# --- T05: upsert_job --------------------------------------------------------


def _make_job(*, first_seen: datetime, last_seen: datetime, **overrides: object) -> Job:
    """Build a Job with sensible defaults; override any field per test."""
    fields: dict[str, object] = {
        "id": make_job_id("greenhouse", "1"),
        "source": "greenhouse",
        "source_id": "1",
        "company": "Acme",
        "title": "Senior Backend Engineer",
        "description": "Java and AWS.",
        "location_raw": "Remote - Canada",
        "is_remote": True,
        "location_bucket": LocationBucket.REMOTE,
        "seniority": Seniority.SENIOR,
        "url": "https://example.com/jobs/1",
        "posted_at": first_seen,
        "date_unknown": False,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
    }
    fields.update(overrides)
    return Job(**fields)  # type: ignore[arg-type]


def test_upsert_inserts_new_row() -> None:
    """A first upsert persists the job, coercing bools/enums/datetimes/blob."""
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    conn = connect(":memory:")
    try:
        init_db(conn)
        job = _make_job(
            first_seen=now,
            last_seen=now,
            eligible=False,
            ineligible_reason="stale",
            content_hash="hash-1",
            embedding=b"\x01\x02\x03",
            raw={"k": "v"},
        )
        upsert_job(conn, job)

        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
        assert row["source"] == "greenhouse"
        assert row["is_remote"] == 1
        assert row["location_bucket"] == "remote"
        assert row["seniority"] == "senior"
        assert row["eligible"] == 0
        assert row["ineligible_reason"] == "stale"
        assert row["content_hash"] == "hash-1"
        assert row["embedding"] == b"\x01\x02\x03"
        assert row["raw_json"] == '{"k": "v"}'
        assert row["first_seen_at"] == now.isoformat()
    finally:
        conn.close()


def test_upsert_same_job_twice_keeps_one_row_and_preserves_first_seen() -> None:
    """Re-seeing a posting upserts onto the same row: one row, first_seen_at
    preserved, last_seen_at advanced (LLD §7.3 idempotency)."""
    t0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    conn = connect(":memory:")
    try:
        init_db(conn)
        upsert_job(conn, _make_job(first_seen=t0, last_seen=t0))
        # Second sighting: same (source, source_id), later last_seen, new title.
        upsert_job(
            conn,
            _make_job(first_seen=t1, last_seen=t1, title="Staff Backend Engineer"),
        )

        rows = conn.execute("SELECT * FROM jobs").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["first_seen_at"] == t0.isoformat()  # preserved
        assert row["last_seen_at"] == t1.isoformat()  # bumped
        assert row["title"] == "Staff Backend Engineer"  # mutable field updated
    finally:
        conn.close()


def test_jobs_unique_source_constraint() -> None:
    """The UNIQUE(source, source_id) constraint rejects duplicates."""
    conn = connect(":memory:")
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO jobs (id, source, source_id, title, "
            "first_seen_at, last_seen_at) VALUES "
            "('id1', 'lever', '42', 'Eng', 't0', 't0')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (id, source, source_id, title, "
                "first_seen_at, last_seen_at) VALUES "
                "('id2', 'lever', '42', 'Eng dup', 't0', 't0')"
            )
    finally:
        conn.close()
