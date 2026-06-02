"""Command-line entry point for Job Finder.

Exposes the ``app`` callable wired to the ``jobfinder`` console script declared
in ``pyproject.toml``. Concrete commands (``poll``, ``serve``, ``add-company``,
``export``, ``init``) are implemented in T24 per LLD §10. For now ``app`` is a
no-op Typer application so the entry point imports cleanly and ``jobfinder
--help`` runs.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="jobfinder",
    help="Discover, filter and rank recent backend software-engineering job postings, locally.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Job Finder.

    A no-op root callback so the Typer app forms a valid command group and
    ``jobfinder --help`` works while no subcommands exist yet. The ``poll``,
    ``serve``, ``add-company``, ``export`` and ``init`` commands are added in
    T24 (LLD §10).
    """


if __name__ == "__main__":  # pragma: no cover
    app()
