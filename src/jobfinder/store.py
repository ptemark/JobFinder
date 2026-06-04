"""SQLite data-access layer for Job Finder (LLD §7).

This module owns the database connection and schema. :func:`connect` opens a
connection with the operational PRAGMAs (LLD §7.1) applied, and :func:`init_db`
runs the idempotent DDL (LLD §7.2). The data-access operations — job upsert,
score/status writes, poll-run bookkeeping, company read/write, and prune —
implement LLD §7.3.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobfinder.models import Job, ScoreBreakdown, Status

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


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Return the stored ``jobs`` row for ``job_id``, or ``None`` if absent.

    The pipeline reads ``content_hash`` and ``embedding`` from this row to decide
    whether a re-seen posting needs re-embedding (LLD §6.4 / §8).
    """
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _now(now: datetime | None) -> datetime:
    """Resolve an injectable ``now`` to a concrete UTC datetime.

    Callers inject ``now`` in tests for determinism (RALPH testing standards);
    production callers leave it ``None`` and get the wall clock.
    """
    return now if now is not None else datetime.now(UTC)


# --- Scores (LLD §7.3) ------------------------------------------------------

# scores has a PRIMARY KEY on job_id, so re-scoring a job (e.g. its content
# changed) upserts onto the one row rather than erroring or duplicating.
_SAVE_SCORE = """
INSERT INTO scores (job_id, final, semantic, skill, location, recency, scored_at)
VALUES (:job_id, :final, :semantic, :skill, :location, :recency, :scored_at)
ON CONFLICT(job_id) DO UPDATE SET
  final = excluded.final,
  semantic = excluded.semantic,
  skill = excluded.skill,
  location = excluded.location,
  recency = excluded.recency,
  scored_at = excluded.scored_at
"""


def save_score(conn: sqlite3.Connection, job_id: str, sb: ScoreBreakdown) -> None:
    """Persist a job's :class:`ScoreBreakdown`, upserting on re-score (LLD §7.3).

    The row is owned by ``jobs`` via ``ON DELETE CASCADE`` (LLD §7.2): deleting
    the job removes its score.
    """
    conn.execute(
        _SAVE_SCORE,
        {
            "job_id": job_id,
            "final": sb.final,
            "semantic": sb.semantic,
            "skill": sb.skill,
            "location": sb.location,
            "recency": sb.recency,
            "scored_at": _iso(sb.scored_at),
        },
    )
    conn.commit()


# --- Status (LLD §7.3) ------------------------------------------------------

_SET_STATUS = """
INSERT INTO status (job_id, state, updated_at)
VALUES (:job_id, :state, :updated_at)
ON CONFLICT(job_id) DO UPDATE SET
  state = excluded.state,
  updated_at = excluded.updated_at
"""


def set_status(
    conn: sqlite3.Connection,
    job_id: str,
    state: Status,
    *,
    now: datetime | None = None,
) -> None:
    """Set a job's user-facing status (new/interested/applied/dismissed), upserting
    onto its single status row and stamping ``updated_at`` (LLD §7.3)."""
    conn.execute(
        _SET_STATUS,
        {"job_id": job_id, "state": str(state), "updated_at": _iso(_now(now))},
    )
    conn.commit()


# --- Poll runs (LLD §7.3) ---------------------------------------------------


def start_run(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Open a poll-run row stamped with ``started_at`` and return its id (LLD §8).

    ``finished_at``/``per_source_json`` stay NULL until :func:`finish_run`.
    """
    cur = conn.execute("INSERT INTO poll_runs (started_at) VALUES (?)", (_iso(_now(now)),))
    conn.commit()
    run_id = cur.lastrowid
    assert run_id is not None  # AUTOINCREMENT PK always yields a rowid on INSERT
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    per_source: dict[str, object],
    *,
    now: datetime | None = None,
) -> None:
    """Close a poll-run row: stamp ``finished_at`` and store the per-source count
    funnel as JSON (LLD §8 / §12)."""
    conn.execute(
        "UPDATE poll_runs SET finished_at = ?, per_source_json = ? WHERE id = ?",
        (_iso(_now(now)), json.dumps(per_source), run_id),
    )
    conn.commit()


# --- Companies (LLD §7.3) ---------------------------------------------------

# Discovery appends *unverified* board tokens, deduping against what's already
# known (LLD §3.6 / T23). ON CONFLICT DO NOTHING preserves the existing row —
# notably it never downgrades a verified entry back to unverified.
_ADD_COMPANY = """
INSERT INTO companies (ats, token, name, verified, added_at)
VALUES (:ats, :token, :name, :verified, :added_at)
ON CONFLICT(ats, token) DO NOTHING
"""


def add_company(
    conn: sqlite3.Connection,
    ats: str,
    token: str,
    *,
    name: str | None = None,
    verified: bool = False,
    now: datetime | None = None,
) -> None:
    """Append a company board token, deduping on ``(ats, token)`` (LLD §7.3).

    An existing row is left untouched, so re-running discovery is idempotent and
    a previously verified entry is never clobbered.
    """
    conn.execute(
        _ADD_COMPANY,
        {
            "ats": ats,
            "token": token,
            "name": name,
            "verified": int(verified),
            "added_at": _iso(_now(now)),
        },
    )
    conn.commit()


def get_companies(conn: sqlite3.Connection, *, ats: str | None = None) -> list[sqlite3.Row]:
    """Return stored companies, optionally filtered to one ATS (LLD §7.3)."""
    if ats is not None:
        return conn.execute(
            "SELECT * FROM companies WHERE ats = ? ORDER BY token", (ats,)
        ).fetchall()
    return conn.execute("SELECT * FROM companies ORDER BY ats, token").fetchall()


# --- Pruning (LLD §7.3) -----------------------------------------------------


def prune(conn: sqlite3.Connection, *, not_seen_days: int, now: datetime | None = None) -> int:
    """Delete jobs not seen in ``not_seen_days`` and return how many were removed.

    Stale rows are those whose ``last_seen_at`` predates the cutoff. Scores and
    status rows cascade away with the job (LLD §7.2 ``ON DELETE CASCADE``). The
    comparison is lexicographic on the ISO-8601 ``last_seen_at`` text, which is
    correct because every timestamp is stored as a UTC ``isoformat`` string.
    """
    cutoff = _iso(_now(now) - timedelta(days=not_seen_days))
    cur = conn.execute("DELETE FROM jobs WHERE last_seen_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


__all__ = [
    "connect",
    "init_db",
    "upsert_job",
    "get_job",
    "save_score",
    "set_status",
    "start_run",
    "finish_run",
    "add_company",
    "get_companies",
    "prune",
]
