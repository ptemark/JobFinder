"""SQLite data-access layer for Job Finder (LLD §7).

This module owns the database connection and schema. :func:`connect` opens a
connection with the operational PRAGMAs (LLD §7.1) applied, and :func:`init_db`
runs the idempotent DDL (LLD §7.2). Higher-level operations (upsert, scores,
status, runs, prune) are added by later tasks.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobfinder.models import Job

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


def _iso(value: datetime | None) -> str | None:
    """Serialize a datetime to an ISO-8601 string for a TEXT column (None passes through)."""
    return value.isoformat() if value is not None else None


def _job_params(job: Job) -> dict[str, object | None]:
    """Map a :class:`Job` onto the ``jobs`` columns, applying the type coercions
    SQLite needs: bools → int, enums → their str value, datetimes → ISO text,
    and ``raw`` → a JSON string."""
    return {
        "id": job.id,
        "source": job.source,
        "source_id": job.source_id,
        "company": job.company,
        "title": job.title,
        "description": job.description,
        "location_raw": job.location_raw,
        "is_remote": int(job.is_remote),
        "location_bucket": str(job.location_bucket),
        "seniority": str(job.seniority),
        "url": job.url,
        "posted_at": _iso(job.posted_at),
        "date_unknown": int(job.date_unknown),
        "eligible": int(job.eligible),
        "ineligible_reason": job.ineligible_reason,
        "content_hash": job.content_hash,
        "embedding": job.embedding,
        "first_seen_at": _iso(job.first_seen_at),
        "last_seen_at": _iso(job.last_seen_at),
        "raw_json": json.dumps(job.raw) if job.raw else None,
    }


# Upsert SQL (LLD §7.3). ON CONFLICT(source, source_id) keeps the poll idempotent
# (HLD §4.4): a re-seen posting updates onto its existing row. first_seen_at is
# deliberately absent from the UPDATE SET so it is preserved from the original
# insert; last_seen_at is bumped to the incoming value.
_UPSERT_JOB = """
INSERT INTO jobs (
  id, source, source_id, company, title, description,
  location_raw, is_remote, location_bucket, seniority, url,
  posted_at, date_unknown, eligible, ineligible_reason, content_hash,
  embedding, first_seen_at, last_seen_at, raw_json
) VALUES (
  :id, :source, :source_id, :company, :title, :description,
  :location_raw, :is_remote, :location_bucket, :seniority, :url,
  :posted_at, :date_unknown, :eligible, :ineligible_reason, :content_hash,
  :embedding, :first_seen_at, :last_seen_at, :raw_json
)
ON CONFLICT(source, source_id) DO UPDATE SET
  company = excluded.company,
  title = excluded.title,
  description = excluded.description,
  location_raw = excluded.location_raw,
  is_remote = excluded.is_remote,
  location_bucket = excluded.location_bucket,
  seniority = excluded.seniority,
  url = excluded.url,
  posted_at = excluded.posted_at,
  date_unknown = excluded.date_unknown,
  eligible = excluded.eligible,
  ineligible_reason = excluded.ineligible_reason,
  content_hash = excluded.content_hash,
  embedding = excluded.embedding,
  last_seen_at = excluded.last_seen_at,
  raw_json = excluded.raw_json
"""


def upsert_job(conn: sqlite3.Connection, job: Job) -> None:
    """Insert ``job`` or update its existing row on the ``(source, source_id)``
    conflict (LLD §7.3).

    The poll is idempotent: re-seeing a posting preserves ``first_seen_at`` from
    the first insert, bumps ``last_seen_at``, and refreshes the mutable fields
    (including ``embedding``, ``eligible``/``ineligible_reason`` and
    ``content_hash``).
    """
    conn.execute(_UPSERT_JOB, _job_params(job))
    conn.commit()


__all__ = ["connect", "init_db", "upsert_job"]
