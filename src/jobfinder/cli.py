"""Command-line entry point for Job Finder (LLD §10).

Exposes the ``app`` Typer application wired to the ``jobfinder`` console script
declared in ``pyproject.toml`` and implements the five commands from LLD §10:

* ``poll``        — run one poll (fetch → normalize → filter → score → persist).
* ``serve``       — host the dashboard on loopback (Cost & Safety §5).
* ``add-company`` — append a *verified* ATS board token to ``companies.yaml``.
* ``export``      — dump the current ranked matches to CSV.
* ``init``        — scaffold ``config/`` from the committed examples, create
  ``data/``, and run the schema DDL so a fresh clone is runnable.

Every command loads and validates settings first and fails fast with a precise
message rather than half-running on bad input (LLD §10, §12; RALPH conventions).
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer
import uvicorn
import yaml

from jobfinder.pipeline import run_poll
from jobfinder.settings import (
    CompaniesConfig,
    CompanyEntry,
    Settings,
    ValidationError,
    load_companies,
    load_profile,
    load_weights,
)
from jobfinder.sources.base import build_sources
from jobfinder.sources.http import HttpClient, configure_default_client
from jobfinder.store import connect, init_db, query_jobs
from jobfinder.web.app import create_app

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from jobfinder.pipeline import RunSummary

logger = logging.getLogger(__name__)

# The ATS providers a board token can belong to (LLD §11.2). add-company rejects
# anything else so a typo fails fast rather than writing an un-pollable entry.
_VALID_ATS = ("greenhouse", "lever", "ashby")

# Example → target pairs scaffolded by ``init`` (LLD §10). The committed
# ``*.example`` files are the single source of truth; init copies, never invents,
# so the scaffold can't drift from the documented schema. Targets are gitignored
# (Cost & Safety §4) and never overwritten if already present.
_INIT_CONFIG_PAIRS = (
    ("config/profile.yaml.example", "config/profile.yaml"),
    ("config/companies.yaml.example", "config/companies.yaml"),
    ("config/weights.yaml.example", "config/weights.yaml"),
    (".env.example", ".env"),
)

# CSV header for ``export`` (LLD §10). Key columns of a ranked match.
_EXPORT_COLUMNS = (
    "score",
    "title",
    "company",
    "location",
    "seniority",
    "remote",
    "posted_at",
    "url",
    "source",
    "status",
)

app = typer.Typer(
    name="jobfinder",
    help="Discover, filter and rank recent backend software-engineering job postings, locally.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Job Finder — a local, single-user backend-role discovery and ranking tool."""


def _fail(message: str) -> NoReturn:
    """Print ``message`` to stderr and exit non-zero (fail-fast, LLD §12)."""
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _validated_settings(*, require_config: bool = True) -> Settings:
    """Build :class:`Settings` and (optionally) validate the YAML domain config.

    ``require_config`` is ``False`` for commands that run *before* a config tree
    exists or that don't need it (``init``, ``add-company``, ``export``); the
    poll/serve paths set it ``True`` so a missing or malformed ``profile.yaml`` /
    ``weights.yaml`` is reported clearly instead of erroring mid-run.
    """
    settings = Settings()
    if require_config:
        try:
            load_profile(settings.config_dir / "profile.yaml")
            load_weights(settings.config_dir / "weights.yaml")
        except (FileNotFoundError, ValidationError, ValueError) as exc:
            _fail(f"invalid or missing config (run `jobfinder init` first): {exc}")
    return settings


@app.command()
def poll(
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Bypass the on-disk HTTP cache for this poll.")
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Restrict to these source names (repeatable)."),
    ] = None,
) -> None:
    """Run one poll: fetch, normalize, filter, score and persist (LLD §8)."""
    logging.basicConfig(level=logging.INFO)
    settings = _validated_settings()

    if no_cache:
        # Install a cache-bypassing default client *before* the source factories
        # (which call get_default_client) are constructed inside run_poll/build_sources.
        configure_default_client(
            HttpClient(
                cache_dir=settings.cache_dir,
                throttle_s=settings.throttle_s,
                cache_ttl_s=settings.cache_ttl_s,
                no_cache=True,
            )
        )

    try:
        sources = build_sources(settings, only=source) if source else None
        summary = run_poll(settings, sources=sources)
    except (FileNotFoundError, ValidationError, ValueError) as exc:
        _fail(f"poll failed: {exc}")
    _echo_summary(summary)


def _echo_summary(summary: RunSummary) -> None:
    """Print the per-source funnel and prune count for a finished poll (LLD §12)."""
    typer.echo(f"Poll {summary.run_id} complete.")
    for name, src in summary.per_source.items():
        if src.error is not None:
            typer.echo(f"  {name}: ERROR {src.error}")
            continue
        typer.echo(
            f"  {name}: fetched={src.fetched} kept={src.kept_after_recency} "
            f"eligible={src.eligible} scored={src.scored}"
        )
    typer.echo(f"Pruned {summary.pruned} stale job(s).")


@app.command()
def serve(
    host: Annotated[
        str, typer.Option(help="Interface to bind. Keep loopback to stay local (Cost & Safety §5).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="TCP port for the dashboard.")] = 8000,
) -> None:
    """Serve the dashboard locally (LLD §9). Defaults bind loopback only."""
    settings = _validated_settings()
    application = create_app(settings)
    uvicorn.run(application, host=host, port=port)


@app.command(name="add-company")
def add_company(
    ats: Annotated[str, typer.Argument(help="ATS provider: greenhouse, lever or ashby.")],
    token: Annotated[str, typer.Argument(help="Board token / slug as it appears in the feed URL.")],
    name: Annotated[str | None, typer.Option(help="Display name for the company.")] = None,
) -> None:
    """Append a verified board token to ``companies.yaml``, deduping on token (LLD §10)."""
    settings = _validated_settings(require_config=False)
    ats = ats.lower()
    if ats not in _VALID_ATS:
        _fail(f"unknown ats {ats!r}; expected one of {', '.join(_VALID_ATS)}")

    companies_path = settings.config_dir / "companies.yaml"
    try:
        config = load_companies(companies_path) if companies_path.exists() else CompaniesConfig()
    except (ValidationError, ValueError) as exc:
        _fail(f"cannot read {companies_path}: {exc}")

    entries: list[CompanyEntry] = getattr(config, ats)
    existing = next((entry for entry in entries if entry.token == token), None)
    if existing is not None:
        # Idempotent: a re-add promotes the entry to verified (never downgrades).
        existing.verified = True
        if name is not None:
            existing.name = name
    else:
        entries.append(CompanyEntry(token=token, name=name, verified=True))

    settings.config_dir.mkdir(parents=True, exist_ok=True)
    _write_companies(companies_path, config)
    typer.echo(f"Wrote verified {ats} token {token!r} to {companies_path}")


def _write_companies(path: Path, config: CompaniesConfig) -> None:
    """Serialize ``config`` back to ``companies.yaml`` (LLD §11.2)."""
    data = {ats: [entry.model_dump() for entry in getattr(config, ats)] for ats in _VALID_ATS}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@app.command()
def export(
    csv_path: Annotated[
        Path | None,
        typer.Option("--csv", help="Write CSV to this path (defaults to stdout)."),
    ] = None,
) -> None:
    """Export the current ranked, eligible matches as CSV (LLD §10)."""
    settings = _validated_settings(require_config=False)
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        rows = query_jobs(conn, sort="best")
    finally:
        conn.close()

    if csv_path is not None:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            _write_csv(handle, rows)
        typer.echo(f"Exported {len(rows)} job(s) to {csv_path}")
    else:
        _write_csv(sys.stdout, rows)


def _write_csv(handle: object, rows: Iterable[sqlite3.Row]) -> None:
    """Write the ranked matches as CSV (header + one row per job)."""
    writer = csv.writer(handle)
    writer.writerow(_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(_export_row(row))


def _export_row(row: sqlite3.Row) -> list[str]:
    """Map one joined job row onto the :data:`_EXPORT_COLUMNS` order."""
    final = row["final"]
    return [
        "" if final is None else f"{final:.1f}",
        row["title"],
        row["company"] or "",
        row["location_bucket"],
        row["seniority"],
        "yes" if row["is_remote"] else "no",
        row["posted_at"] or "",
        row["url"] or "",
        row["source"],
        row["status_state"] or "new",
    ]


@app.command()
def init() -> None:
    """Scaffold ``config/`` from the examples, create ``data/`` and run the DDL (LLD §10)."""
    settings = Settings()
    base = settings.base_dir
    for example_rel, target_rel in _INIT_CONFIG_PAIRS:
        example, target = base / example_rel, base / target_rel
        if target.exists():
            typer.echo(f"  exists, kept: {target_rel}")
            continue
        if not example.exists():
            typer.echo(f"  WARNING: missing example {example_rel}", err=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        typer.echo(f"  created: {target_rel}")

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    typer.echo(f"  database ready: {settings.db_path}")
    typer.echo("Next: add your résumé, edit config/profile.yaml, then run `jobfinder poll`.")


if __name__ == "__main__":  # pragma: no cover
    app()
