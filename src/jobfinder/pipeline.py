"""Poll orchestration: fetch → normalize → filter → score → persist (LLD §8).

:func:`run_poll` ties the whole pipeline together for one poll. It builds the
profile vector once, then iterates the enabled sources — each wrapped in a
**bulkhead** so one source failing can never abort the run (LLD §8, RALPH error
handling) — normalizing, filtering, embedding+scoring the eligible/changed jobs,
and upserting every posting (ineligible ones are stored flagged, not dropped,
LLD §5). It prunes stale rows and records per-source counts so the run summary
and the dashboard can report the ``fetched → kept → eligible → scored`` funnel
(LLD §12).

Re-embedding is skipped when a job's content is unchanged and it already has a
stored embedding (LLD §6.4): the existing embedding is preserved on the upsert
and the prior score is kept, so a re-poll only pays the model cost for new or
changed postings. The poll is idempotent — re-running over the same data updates
rows in place rather than duplicating them (HLD §4.4).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jobfinder.filters import is_eligible
from jobfinder.normalize import normalize
from jobfinder.score import (
    build_profile_vector,
    embed_job,
    extract_resume,
    load_model,
    score_job,
)
from jobfinder.settings import Settings, load_profile, load_weights
from jobfinder.sources.base import build_sources
from jobfinder.store import (
    connect,
    finish_run,
    get_job,
    init_db,
    prune,
    save_score,
    start_run,
    upsert_job,
)

if TYPE_CHECKING:
    import sqlite3

    from numpy.typing import NDArray

    from jobfinder.models import Job, RawPosting, ScoreBreakdown
    from jobfinder.score import Encoder
    from jobfinder.settings import Profile, Weights
    from jobfinder.sources.base import Source

logger = logging.getLogger(__name__)


@dataclass
class SourceSummary:
    """Per-source funnel counts for one poll (LLD §12).

    ``error`` is set only when the source's ``fetch`` raised — the bulkhead
    isolates it (LLD §8) — in which case no postings were processed. ``errors``
    collects the source's own non-fatal notes (skipped malformed postings, a
    board that 404'd, a missing-secret skip).
    """

    fetched: int = 0
    kept_after_recency: int = 0
    eligible: int = 0
    scored: int = 0
    errors: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """JSON-serializable view stored in ``poll_runs.per_source_json`` (LLD §8)."""
        return {
            "fetched": self.fetched,
            "kept_after_recency": self.kept_after_recency,
            "eligible": self.eligible,
            "scored": self.scored,
            "errors": self.errors,
            "error": self.error,
        }


@dataclass
class RunSummary:
    """The outcome of one poll, returned by :func:`run_poll` (LLD §8)."""

    run_id: int
    started_at: datetime
    finished_at: datetime
    per_source: dict[str, SourceSummary]
    pruned: int


def run_poll(
    settings: Settings,
    *,
    sources: list[Source] | None = None,
    model: Encoder | None = None,
    now: datetime | None = None,
    run_id: int | None = None,
) -> RunSummary:
    """Run one poll end-to-end and return its :class:`RunSummary` (LLD §8).

    Args:
        settings: operational settings (paths, throttle, embed model, retention).
        sources: the source adapters to poll; defaults to every enabled adapter
            built from ``settings`` (injectable so tests run offline).
        model: the embedding model; defaults to the one named by
            ``settings.embed_model`` (injectable to reuse a pre-loaded model).
        now: the poll timestamp, injected for deterministic tests; defaults to
            the current UTC time. A single instant is used for recency, filtering,
            and the run/seen stamps so the poll is internally consistent.
        run_id: an already-reserved ``poll_runs`` id to finish. The dashboard's
            ``POST /api/poll`` opens the run row, returns its id, then spawns this
            poll to complete that same row (LLD §9.1). Defaults to ``None``, in
            which case the poll opens its own run (the cron/CLI path).
    """
    now = now if now is not None else datetime.now(UTC)
    profile = load_profile(settings.config_dir / "profile.yaml")
    weights = load_weights(settings.config_dir / "weights.yaml")

    # Build the profile vector once per poll (LLD §6.2). A missing résumé or a bad
    # config fails the whole poll fast — scoring is impossible without them.
    resume_text = extract_resume(settings.base_dir / profile.resume_path)
    model = model if model is not None else load_model(settings.embed_model)
    profile_vec = build_profile_vector(profile, resume_text, model=model)

    if sources is None:
        sources = build_sources(settings)

    conn = connect(settings.db_path)
    try:
        init_db(conn)
        run_id = start_run(conn, now=now) if run_id is None else run_id
        per_source: dict[str, SourceSummary] = {}
        for src in sources:
            per_source[src.name] = _poll_source(
                src,
                conn,
                profile=profile,
                weights=weights,
                profile_vec=profile_vec,
                model=model,
                throttle_s=settings.throttle_s,
                now=now,
            )
        # Retention is an operational setting (LLD §8 / §11.4); recency uses the
        # profile's max_age_days (LLD §6.3). Both default to their doc values.
        pruned = prune(conn, not_seen_days=settings.retention_days, now=now)
        finish_run(conn, run_id, {name: s.as_dict() for name, s in per_source.items()}, now=now)
    finally:
        conn.close()

    return RunSummary(
        run_id=run_id,
        started_at=now,
        finished_at=now,
        per_source=per_source,
        pruned=pruned,
    )


def _poll_source(
    src: Source,
    conn: sqlite3.Connection,
    *,
    profile: Profile,
    weights: Weights,
    profile_vec: NDArray,
    model: Encoder,
    throttle_s: float,
    now: datetime,
) -> SourceSummary:
    """Fetch and process one source, isolated by the per-source bulkhead (LLD §8)."""
    summary = SourceSummary()
    try:
        result = src.fetch(max_age_days=profile.max_age_days, throttle_s=throttle_s)
    except Exception as exc:  # bulkhead: one source must never abort the poll (LLD §8)
        logger.exception("source %s failed; isolating", src.name)
        summary.error = repr(exc)
        return summary

    summary.fetched = result.fetched
    summary.kept_after_recency = result.kept_after_recency
    summary.errors = list(result.errors)
    for raw in result.raw:
        _process_posting(
            raw,
            conn,
            summary=summary,
            profile=profile,
            weights=weights,
            profile_vec=profile_vec,
            model=model,
            now=now,
        )
    logger.info(
        "%s funnel: fetched=%d kept=%d eligible=%d scored=%d",
        src.name,
        summary.fetched,
        summary.kept_after_recency,
        summary.eligible,
        summary.scored,
    )
    return summary


def _process_posting(
    raw: RawPosting,
    conn: sqlite3.Connection,
    *,
    summary: SourceSummary,
    profile: Profile,
    weights: Weights,
    profile_vec: NDArray,
    model: Encoder,
    now: datetime,
) -> None:
    """Normalize, classify, optionally embed+score, and upsert one posting (LLD §8)."""
    job = normalize(raw, company_hint=raw.company_hint, now=now)
    eligible, reason = is_eligible(job, profile=profile, now=now)
    job.eligible, job.ineligible_reason = eligible, reason
    job.content_hash = _content_hash(job)

    score: ScoreBreakdown | None = None
    if eligible:
        summary.eligible += 1
        existing = get_job(conn, job.id)
        if _needs_embedding(existing, job.content_hash):
            # New or content-changed eligible job: embed and score (LLD §6.3–§6.4).
            job_vec = embed_job(job, model=model)
            job.embedding = job_vec.tobytes()
            score = score_job(job, profile_vec, job_vec, profile=profile, weights=weights, now=now)
            summary.scored += 1
        else:
            # Unchanged re-see: keep the stored embedding so the upsert doesn't
            # null it, and keep the prior score (skip re-embedding, LLD §6.4).
            job.embedding = existing["embedding"]

    # Upsert the job first so the score's FK to jobs(id) is satisfied (LLD §7.2).
    upsert_job(conn, job)
    if score is not None:
        save_score(conn, job.id, score)


def _content_hash(job: Job) -> str:
    """Stable hash of the scored content (title + description) for change detection.

    Gates re-embedding (LLD §6.4): a re-seen posting whose title/description are
    unchanged keeps its embedding and score rather than paying the model cost.
    """
    return hashlib.sha1(f"{job.title}\n{job.description}".encode()).hexdigest()


def _needs_embedding(existing: sqlite3.Row | None, content_hash: str) -> bool:
    """True if a job must be (re-)embedded: it is new, changed, or lacks an embedding."""
    if existing is None:
        return True
    return existing["content_hash"] != content_hash or existing["embedding"] is None


def main(argv: list[str] | None = None) -> None:
    """Run one poll from the command line.

    This is the spawnable entry point the dashboard's ``POST /api/poll`` uses
    (``python -m jobfinder.pipeline --run-id N``) so the heavy poll runs
    out-of-process and the request returns immediately (LLD §9.1). ``--run-id``
    finishes a run row the caller already opened; omitting it opens a fresh run
    (the bare cron invocation). Settings come from the environment so the child
    shares the parent's paths.
    """
    parser = argparse.ArgumentParser(
        prog="jobfinder.pipeline", description="Run one Job Finder poll."
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="finish an already-reserved poll_runs id instead of opening a new run",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run_poll(Settings(), run_id=args.run_id)


__all__ = ["RunSummary", "SourceSummary", "main", "run_poll"]


if __name__ == "__main__":  # pragma: no cover
    main()
