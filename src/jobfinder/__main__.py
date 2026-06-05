"""Enable ``python -m jobfinder`` to run the CLI.

The README's scheduling examples (cron / launchd / Task Scheduler) invoke the
tool as ``python -m jobfinder poll`` so the interpreter is named explicitly and
no console-script PATH lookup is needed. This module makes that form delegate to
the same Typer ``app`` as the ``jobfinder`` entry point.
"""

from __future__ import annotations

from jobfinder.cli import app

if __name__ == "__main__":
    app()
