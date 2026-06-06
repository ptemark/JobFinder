"""Tests for the CLI: the T01 scaffold smoke checks plus the T24 commands.

The T24 tests exercise ``init``/``add-company``/``export`` for real against a
temp config+DB tree and patch the heavy/transport seams of ``poll``/``serve``
(``run_poll``, ``build_sources``, ``configure_default_client``, ``uvicorn.run``)
so the suite stays offline, model-free and deterministic (RALPH testing
standards). Each command builds ``Settings()`` from the environment, so the
``base`` fixture points ``JOBFINDER_base_dir`` at a temp dir.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import jobfinder
from jobfinder import cli
from jobfinder.cli import app
from jobfinder.models import Job, LocationBucket, ScoreBreakdown, Seniority
from jobfinder.pipeline import RunSummary, SourceSummary
from jobfinder.settings import load_companies
from jobfinder.store import connect, init_db, save_score, upsert_job

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_FILES = (
    "config/profile.yaml.example",
    "config/companies.yaml.example",
    "config/weights.yaml.example",
    ".env.example",
)
_NOW = datetime(2026, 6, 5, tzinfo=UTC)


# --- T01 scaffold smoke tests -----------------------------------------------


def test_package_version_is_set() -> None:
    assert jobfinder.__version__ == "0.1.0"


def test_entry_point_app_is_typer() -> None:
    assert isinstance(app, typer.Typer)


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "jobfinder" in result.output.lower()


# --- T24 fixtures & helpers -------------------------------------------------


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp base dir wired to ``Settings`` via ``JOBFINDER_base_dir``."""
    monkeypatch.setenv("JOBFINDER_base_dir", str(tmp_path))
    return tmp_path


def _copy_examples(base_dir: Path) -> None:
    """Mimic a fresh clone: only the committed ``*.example`` files present."""
    (base_dir / "config").mkdir(parents=True, exist_ok=True)
    for rel in _EXAMPLE_FILES:
        shutil.copy(_REPO_ROOT / rel, base_dir / rel)


def _scaffold_config(base_dir: Path) -> None:
    """Produce a runnable config tree (profile/weights/companies) from the examples."""
    _copy_examples(base_dir)
    cfg = base_dir / "config"
    for stem in ("profile", "companies", "weights"):
        (cfg / f"{stem}.yaml").write_text((cfg / f"{stem}.yaml.example").read_text())


def _make_job() -> Job:
    return Job(
        id=Job.make_id("greenhouse", "1"),
        source="greenhouse",
        source_id="1",
        company="Acme",
        title="Senior Backend Engineer",
        description="Java and AWS backend role.",
        location_raw="Remote",
        is_remote=True,
        location_bucket=LocationBucket.REMOTE,
        seniority=Seniority.SENIOR,
        url="https://example.com/1",
        posted_at=_NOW,
        date_unknown=False,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        eligible=True,
    )


def _fake_summary(run_id: int = 1, per_source: dict | None = None) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        started_at=_NOW,
        finished_at=_NOW,
        per_source=per_source if per_source is not None else {},
        pruned=0,
    )


# --- init -------------------------------------------------------------------


def test_init_scaffolds_config_and_db(base: Path) -> None:
    _copy_examples(base)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    for stem in ("profile", "companies", "weights"):
        assert (base / "config" / f"{stem}.yaml").exists()
    assert (base / ".env").exists()
    assert (base / "data" / "jobs.db").exists()


def test_init_does_not_overwrite_existing_config(base: Path) -> None:
    _copy_examples(base)
    runner.invoke(app, ["init"])
    profile = base / "config" / "profile.yaml"
    profile.write_text("role_keywords: [custom-edit]\n")  # a user edit
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "custom-edit" in profile.read_text()  # preserved, not clobbered
    assert "kept" in result.output


# --- add-company ------------------------------------------------------------


def test_add_company_writes_verified_entry(base: Path) -> None:
    _scaffold_config(base)
    result = runner.invoke(app, ["add-company", "greenhouse", "acme", "--name", "Acme"])
    assert result.exit_code == 0, result.output
    config = load_companies(base / "config" / "companies.yaml")
    entry = next(e for e in config.greenhouse if e.token == "acme")
    assert entry.verified is True
    assert entry.name == "Acme"


def test_add_company_creates_file_when_absent(base: Path) -> None:
    (base / "config").mkdir()
    result = runner.invoke(app, ["add-company", "lever", "jobber"])
    assert result.exit_code == 0, result.output
    config = load_companies(base / "config" / "companies.yaml")
    assert any(e.token == "jobber" and e.verified for e in config.lever)


def test_add_company_promotes_existing_to_verified(base: Path) -> None:
    _scaffold_config(base)  # companies.yaml.example seeds shopify as unverified
    result = runner.invoke(app, ["add-company", "greenhouse", "shopify"])
    assert result.exit_code == 0, result.output
    config = load_companies(base / "config" / "companies.yaml")
    tokens = [e.token for e in config.greenhouse]
    assert tokens.count("shopify") == 1  # deduped, not appended
    assert next(e for e in config.greenhouse if e.token == "shopify").verified is True


def test_add_company_rejects_unknown_ats(base: Path) -> None:
    (base / "config").mkdir()
    result = runner.invoke(app, ["add-company", "workday", "acme"])
    assert result.exit_code == 1
    assert "unknown ats" in result.output.lower()


# --- poll -------------------------------------------------------------------


def test_poll_invokes_run_poll(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_config(base)
    captured: dict[str, object] = {}

    def fake_run_poll(settings: object, *, sources: object = None, **_: object) -> RunSummary:
        captured["sources"] = sources
        return _fake_summary(
            run_id=7,
            per_source={
                "greenhouse": SourceSummary(fetched=3, kept_after_recency=2, eligible=1, scored=1)
            },
        )

    monkeypatch.setattr(cli, "run_poll", fake_run_poll)
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 0, result.output
    assert captured["sources"] is None  # no --source ⇒ run_poll builds defaults
    assert "Poll 7 complete" in result.output
    assert "greenhouse: fetched=3 kept=2 eligible=1 scored=1" in result.output


def test_poll_source_selection(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_config(base)
    seen: dict[str, object] = {}

    def fake_build_sources(settings: object, *, only: object = None) -> list:
        seen["only"] = only
        return []

    monkeypatch.setattr(cli, "build_sources", fake_build_sources)
    monkeypatch.setattr(cli, "run_poll", lambda settings, *, sources=None: _fake_summary())
    result = runner.invoke(app, ["poll", "--source", "greenhouse"])
    assert result.exit_code == 0, result.output
    assert seen["only"] == ["greenhouse"]


def test_poll_no_cache_configures_bypass_client(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaffold_config(base)
    installed: dict[str, object] = {}

    monkeypatch.setattr(cli, "run_poll", lambda settings, *, sources=None: _fake_summary())
    monkeypatch.setattr(
        cli, "configure_default_client", lambda client: installed.update(no_cache=client._no_cache)
    )
    result = runner.invoke(app, ["poll", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert installed["no_cache"] is True


def test_poll_missing_config_fails_fast(base: Path) -> None:
    (base / "config").mkdir()  # empty: no profile.yaml/weights.yaml
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 1
    assert "config" in result.output.lower()


# --- serve ------------------------------------------------------------------


def test_serve_runs_uvicorn_on_loopback(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_config(base)
    called: dict[str, object] = {}

    def fake_run(application: object, *, host: str, port: int) -> None:
        called.update(host=host, port=port, app=application)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    result = runner.invoke(app, ["serve", "--port", "9123"])
    assert result.exit_code == 0, result.output
    assert called["host"] == "127.0.0.1"  # loopback default (Cost & Safety §5)
    assert called["port"] == 9123
    assert called["app"] is not None


# --- export -----------------------------------------------------------------


def test_export_writes_csv(base: Path, tmp_path: Path) -> None:
    conn = connect(base / "data" / "jobs.db")
    init_db(conn)
    job = _make_job()
    upsert_job(conn, job)
    save_score(
        conn,
        job.id,
        ScoreBreakdown(
            final=88.5, semantic=0.8, skill=1.0, location=1.0, recency=0.9, scored_at=_NOW
        ),
    )
    conn.close()

    out = tmp_path / "matches.csv"
    result = runner.invoke(app, ["export", "--csv", str(out)])
    assert result.exit_code == 0, result.output

    rows = list(csv.reader(io.StringIO(out.read_text())))
    assert rows[0] == list(cli._EXPORT_COLUMNS)
    data_row = rows[1]
    assert data_row[0] == "88.5"
    assert data_row[1] == "Senior Backend Engineer"
    assert data_row[2] == "Acme"


def test_export_to_stdout_when_no_path(base: Path) -> None:
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("score,")  # header even with no jobs


def _seed_scored_job(
    conn: object, source_id: str, *, bucket: LocationBucket, final: float, eligible: bool = True
) -> Job:
    """Insert one eligible, scored job in ``bucket`` with ``final`` for filter tests."""
    job = Job(
        id=Job.make_id("greenhouse", source_id),
        source="greenhouse",
        source_id=source_id,
        company="Acme",
        title="Senior Backend Engineer",
        description="Java and AWS backend role.",
        location_raw=bucket.value,
        is_remote=bucket is LocationBucket.REMOTE,
        location_bucket=bucket,
        seniority=Seniority.SENIOR,
        url=f"https://example.com/{source_id}",
        posted_at=_NOW,
        date_unknown=False,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        eligible=eligible,
    )
    upsert_job(conn, job)
    save_score(
        conn,
        job.id,
        ScoreBreakdown(
            final=final, semantic=0.8, skill=1.0, location=1.0, recency=0.9, scored_at=_NOW
        ),
    )
    return job


def _export_company_ids(output_csv: str) -> set[str]:
    """Return the set of ``url`` values (data rows) from a captured export CSV."""
    rows = list(csv.reader(io.StringIO(output_csv)))
    url_col = list(cli._EXPORT_COLUMNS).index("url")
    return {row[url_col] for row in rows[1:]}


def test_export_min_score_filters_low_scores(base: Path) -> None:
    conn = connect(base / "data" / "jobs.db")
    init_db(conn)
    _seed_scored_job(conn, "high", bucket=LocationBucket.REMOTE, final=90.0)
    _seed_scored_job(conn, "low", bucket=LocationBucket.REMOTE, final=40.0)
    conn.close()

    result = runner.invoke(app, ["export", "--min-score", "75"])
    assert result.exit_code == 0, result.output
    assert _export_company_ids(result.output) == {"https://example.com/high"}


def test_export_bucket_filter_is_repeatable(base: Path) -> None:
    conn = connect(base / "data" / "jobs.db")
    init_db(conn)
    _seed_scored_job(conn, "remote", bucket=LocationBucket.REMOTE, final=90.0)
    _seed_scored_job(conn, "van", bucket=LocationBucket.VANCOUVER, final=85.0)
    _seed_scored_job(conn, "tor", bucket=LocationBucket.TORONTO, final=80.0)
    conn.close()

    result = runner.invoke(app, ["export", "--bucket", "remote", "--bucket", "toronto"])
    assert result.exit_code == 0, result.output
    assert _export_company_ids(result.output) == {
        "https://example.com/remote",
        "https://example.com/tor",
    }


def test_export_rejects_unknown_bucket(base: Path) -> None:
    result = runner.invoke(app, ["export", "--bucket", "mars"])
    assert result.exit_code == 1
    assert "unknown bucket" in result.output


# --- help documents every command -------------------------------------------


def test_help_documents_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("poll", "serve", "add-company", "export", "init"):
        assert command in result.output


# --- `python -m jobfinder` works (the README scheduling examples rely on it) --


def test_python_m_jobfinder_runs_cli() -> None:
    # The cron/launchd/Task Scheduler examples invoke `python -m jobfinder ...`;
    # validate that module-execution path resolves to the same CLI. Offline:
    # `--help` touches no network and loads no model.
    result = subprocess.run(
        [sys.executable, "-m", "jobfinder", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "poll" in result.stdout
