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
from dataclasses import dataclass
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


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection to ``db_path`` with the LLD §7.1 PRAGMAs applied.

    Pass ``":memory:"`` for tests. Rows are returned as :class:`sqlite3.Row`
    so callers can address columns by name. The parent directory is created if
    it does not already exist (a real file path; ``:memory:`` is left alone).

    ``check_same_thread=False`` lets the connection be used from a thread other
    than the one that created it. The web layer needs this: FastAPI runs the
    per-request dependency and the endpoint on separate threadpool threads, so a
    same-thread connection would raise ``ProgrammingError`` on use. Safe because
    each request owns its own short-lived connection (no concurrent sharing).
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
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


# --- Job queries for the dashboard (LLD §7.3 / §9) --------------------------


@dataclass
class JobFilters:
    """Dashboard list filters (LLD §9.1). Every field is optional; ``None`` means
    "don't constrain on this column". ``include_ineligible`` defaults to ``False``
    so filtered-out jobs stay hidden behind the debug toggle (LLD §5, §9.2)."""

    bucket: str | None = None
    source: str | None = None
    seniority: str | None = None
    min_score: float | None = None
    status: str | None = None
    max_age_days: int | None = None
    include_ineligible: bool = False


# Shared FROM/JOIN for every list/detail query: jobs left-joined with their score
# and status so unscored (ineligible) jobs and jobs the user hasn't touched still
# appear (status defaults to 'new' via COALESCE). Aliases keep the row keys stable
# for the API mapping (LLD §9.2).
_JOB_FROM = (
    "FROM jobs j LEFT JOIN scores s ON s.job_id = j.id LEFT JOIN status st ON st.job_id = j.id"
)
_JOB_COLUMNS = (
    "j.*, s.final AS final, s.semantic AS semantic, s.skill AS skill, "
    "s.location AS location, s.recency AS recency, s.scored_at AS scored_at, "
    "st.state AS status_state"
)

# Status states used in the WHERE clause. An untouched job has no status row and
# reads as 'new' (LLD §9.2); a dismissed or applied job is excluded from the default
# listing (spec §7: an eligible posting is "not already marked dismissed or applied" —
# §13 DoD: dismissing "hides it and persists across restart"; M7/T30 hides applied too,
# surfacing it under the Applied tab via an explicit status=applied query). Mirror
# models.Status values.
_DEFAULT_STATE = "new"  # models.Status.NEW — implicit status of an untouched job
_DISMISSED_STATE = "dismissed"  # models.Status.DISMISSED — hidden unless asked for
_APPLIED_STATE = "applied"  # models.Status.APPLIED — hidden from default list (M7/T30)

# Sort orders (LLD §9.2). NULLS LAST keeps unscored / undated jobs at the bottom
# rather than the top (SQLite ≥ 3.30 honours the clause).
_SORT_ORDERS = {
    "best": "s.final DESC NULLS LAST, j.posted_at DESC NULLS LAST",
    "newest": "j.posted_at DESC NULLS LAST, s.final DESC NULLS LAST",
}

# Flat-column equivalents of _SORT_ORDERS, for use *outside* the j./s. join: the
# per-company cap wraps the joined query in a subquery whose output columns are
# unqualified (the table aliases don't survive the wrap).
_FLAT_SORT_ORDERS = {
    "best": "final DESC NULLS LAST, posted_at DESC NULLS LAST",
    "newest": "posted_at DESC NULLS LAST, final DESC NULLS LAST",
}


def _ranked_by_company(base_sql: str) -> str:
    """Wrap ``base_sql`` (a joined SELECT) so each row gains a ``_rn`` rank within
    its company, most applicable first (highest final score, then most recent).

    A blank/NULL company partitions by ``id`` instead, so an unknown employer is
    never collapsed with unrelated postings — each stands alone (rank 1) and is
    always kept by the cap.
    """
    return (
        "SELECT *, ROW_NUMBER() OVER ("
        "PARTITION BY COALESCE(NULLIF(company, ''), id) "
        f"ORDER BY {_FLAT_SORT_ORDERS['best']}) AS _rn FROM ({base_sql})"
    )


def _job_where(filters: JobFilters, now: datetime) -> tuple[str, dict[str, object]]:
    """Build the parameterized WHERE clause for the dashboard filters (LLD §9.1)."""
    clauses: list[str] = []
    params: dict[str, object] = {}
    if not filters.include_ineligible:
        clauses.append("j.eligible = 1")
    if filters.bucket is not None:
        clauses.append("j.location_bucket = :bucket")
        params["bucket"] = filters.bucket
    if filters.source is not None:
        clauses.append("j.source = :source")
        params["source"] = filters.source
    if filters.seniority is not None:
        clauses.append("j.seniority = :seniority")
        params["seniority"] = filters.seniority
    if filters.status is not None:
        # Untouched jobs have no status row; they read as 'new' (LLD §9.2). An
        # explicit filter still surfaces dismissed jobs (status=dismissed) so the
        # user can review or undo them.
        clauses.append("COALESCE(st.state, :default_state) = :status")
        params["status"] = filters.status
        params["default_state"] = _DEFAULT_STATE
    else:
        # No explicit status filter: hide dismissed *and* applied jobs by default
        # (spec §7, §13; M7/T30 — applied jobs live under their own Applied tab).
        clauses.append(
            "COALESCE(st.state, :default_state) NOT IN (:dismissed_state, :applied_state)"
        )
        params["default_state"] = _DEFAULT_STATE
        params["dismissed_state"] = _DISMISSED_STATE
        params["applied_state"] = _APPLIED_STATE
    if filters.min_score is not None:
        clauses.append("s.final >= :min_score")
        params["min_score"] = filters.min_score
    if filters.max_age_days is not None:
        # date_unknown (NULL posted_at) jobs pass the age filter — they're flagged,
        # never silently dropped (spec §7), mirroring the eligibility recency gate.
        clauses.append("(j.posted_at IS NULL OR j.posted_at >= :age_cutoff)")
        params["age_cutoff"] = _iso(now - timedelta(days=filters.max_age_days))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_jobs(
    conn: sqlite3.Connection,
    *,
    filters: JobFilters | None = None,
    sort: str = "best",
    limit: int | None = None,
    offset: int = 0,
    per_company_limit: int | None = None,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    """Return jobs (joined with score + status) matching ``filters``, ordered by
    ``sort`` (``best``|``newest``) with optional pagination (LLD §9.1–§9.2).

    ``per_company_limit`` keeps only the N most applicable postings per company
    (highest score first); ``None`` keeps them all.
    """
    filters = filters if filters is not None else JobFilters()
    where, params = _job_where(filters, _now(now))
    base = f"SELECT {_JOB_COLUMNS} {_JOB_FROM}{where}"
    if per_company_limit is not None:
        order = _FLAT_SORT_ORDERS.get(sort, _FLAT_SORT_ORDERS["best"])
        sql = (
            f"SELECT * FROM ({_ranked_by_company(base)}) "
            f"WHERE _rn <= :per_company_limit ORDER BY {order}"
        )
        params["per_company_limit"] = per_company_limit
    else:
        order = _SORT_ORDERS.get(sort, _SORT_ORDERS["best"])
        sql = f"{base} ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
    return conn.execute(sql, params).fetchall()


def count_jobs(
    conn: sqlite3.Connection,
    *,
    filters: JobFilters | None = None,
    per_company_limit: int | None = None,
    now: datetime | None = None,
) -> int:
    """Count jobs matching ``filters`` (the unpaginated total for the list view).

    ``per_company_limit`` mirrors :func:`query_jobs` so the total reflects the
    capped set the user actually sees.
    """
    filters = filters if filters is not None else JobFilters()
    where, params = _job_where(filters, _now(now))
    if per_company_limit is not None:
        base = f"SELECT {_JOB_COLUMNS} {_JOB_FROM}{where}"
        params["per_company_limit"] = per_company_limit
        return conn.execute(
            f"SELECT COUNT(*) FROM ({_ranked_by_company(base)}) WHERE _rn <= :per_company_limit",
            params,
        ).fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) {_JOB_FROM}{where}", params).fetchone()[0]


def get_job_detail(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Return one job joined with its score + status, or ``None`` (LLD §9.1)."""
    return conn.execute(
        f"SELECT {_JOB_COLUMNS} {_JOB_FROM} WHERE j.id = :id",
        {"id": job_id},
    ).fetchone()


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the most recent finished poll-run row, or ``None`` (LLD §9.1)."""
    return conn.execute(
        "SELECT * FROM poll_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def previous_run_finished_at(conn: sqlite3.Connection) -> str | None:
    """Return the ``finished_at`` of the run *before* the latest finished one.

    A job is "new since last poll" when its ``first_seen_at`` is later than this
    threshold — i.e. it was first seen during the most recent poll (LLD §7.3).
    Returns ``None`` when there is no prior run, in which case every job counts as
    new (the very first poll).
    """
    row = conn.execute(
        "SELECT finished_at FROM poll_runs WHERE finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    return row["finished_at"] if row is not None else None


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
    "JobFilters",
    "query_jobs",
    "count_jobs",
    "get_job_detail",
    "latest_run",
    "previous_run_finished_at",
]
