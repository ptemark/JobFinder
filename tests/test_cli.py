"""Smoke tests for the T01 scaffold: package metadata and CLI entry point.

These verify the deliverable of T01 — the package imports and the ``jobfinder``
console-script target (``jobfinder.cli:app``) loads into a usable Typer app with
a working ``--help`` — without depending on any later milestone.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

import jobfinder
from jobfinder.cli import app

runner = CliRunner()


def test_package_version_is_set() -> None:
    assert jobfinder.__version__ == "0.1.0"


def test_entry_point_app_is_typer() -> None:
    assert isinstance(app, typer.Typer)


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "jobfinder" in result.output.lower()
