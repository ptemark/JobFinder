"""SQLite data-access layer for Job Finder (LLD §7).

This module owns the database connection and schema. :func:`connect` opens a
connection with the operational PRAGMAs (LLD §7.1) applied, and :func:`init_db`
runs the idempotent DDL (LLD §7.2). Higher-level operations (upsert, scores,
status, runs, prune) are added by later tasks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Connection PRAGMAs (LLD §7.1). WAL + NORMAL synchronous give crash-safe,
# concurrent-reader-friendly writes for the single-writer poll; busy_timeout
# avoids spurious "database is locked" under the dashboard's concurrent reads;
# foreign_keys=ON makes the scores/status cascade deletes (LLD §7.2) actually
# fire (SQLite leaves FK enforcement off by default).
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

# Full schema (LLD §7.2). Every statement is IF NOT EXISTS so init_db is safe to
# run on every startup / re-run without dropping data.
_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL, source_id TEXT NOT NULL,
  company TEXT, title TEXT NOT NULL, description TEXT,
  location_raw TEXT, is_remote INTEGER, location_bucket TEXT,
  seniority TEXT, url TEXT,
  posted_at TEXT, date_unknown INTEGER DEFAULT 0,
  eligible INTEGER DEFAULT 1, ineligible_reason TEXT,
  content_hash TEXT,
  embedding BLOB,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  raw_json TEXT,
  UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS scores (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  final REAL, semantic REAL, skill REAL, location REAL, recency REAL,
  scored_at TEXT
);
CREATE TABLE IF NOT EXISTS status (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'new', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS poll_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, per_source_json TEXT
);
CREATE TABLE IF NOT EXISTS companies (
  ats TEXT NOT NULL, token TEXT NOT NULL, name TEXT,
  verified INTEGER DEFAULT 0, added_at TEXT,
  PRIMARY KEY (ats, token)
);
CREATE INDEX IF NOT EXISTS ix_jobs_posted ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS ix_jobs_bucket ON jobs(location_bucket);
CREATE INDEX IF NOT EXISTS ix_jobs_elig   ON jobs(eligible);
CREATE INDEX IF NOT EXISTS ix_scores_final ON scores(final);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection to ``db_path`` with the LLD §7.1 PRAGMAs applied.

    Pass ``":memory:"`` for tests. Rows are returned as :class:`sqlite3.Row`
    so callers can address columns by name. The parent directory is created if
    it does not already exist (a real file path; ``:memory:`` is left alone).
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes (LLD §7.2). Idempotent: safe to re-run."""
    conn.executescript(_DDL)
    conn.commit()


__all__ = ["connect", "init_db"]
