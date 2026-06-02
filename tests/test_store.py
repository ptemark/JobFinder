"""Tests for the SQLite schema & connection layer (T04, LLD §7.1–§7.2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobfinder.store import connect, init_db

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
