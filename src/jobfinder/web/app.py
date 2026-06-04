"""FastAPI application factory for the dashboard (LLD §9).

:func:`create_app` wires the JSON API router and (once T20 lands) the static SPA,
and stashes the resolved settings, the validated targeting profile, and an
injectable clock on ``app.state`` for the routes to read. The schema is created
on startup so serving before the first poll yields an empty list rather than an
error. Binding to loopback is the server's job (uvicorn ``host=127.0.0.1``, wired
by the ``serve`` CLI in T24); the app itself is transport-agnostic and never
talks to anything but the local DB (Cost & Safety §5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from jobfinder.settings import Settings, load_profile
from jobfinder.store import connect, init_db
from jobfinder.web.api import router

if TYPE_CHECKING:
    from collections.abc import Callable

# The static SPA lives here (built in T20). Mounting is guarded on existence so
# the API is fully usable before the frontend assets land.
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the dashboard app.

    Args:
        settings: operational settings (DB path, config dir). Defaults to
            environment-driven :class:`Settings` for production ``serve``.
        now: injectable clock returning the current UTC time, overridden in tests
            for deterministic age/recency calculations; defaults to the wall clock.
    """
    settings = settings if settings is not None else Settings()
    app = FastAPI(title="Job Finder")
    app.state.settings = settings
    # Fail fast on a missing/malformed profile (we need must_have_skills for the
    # matched-skill chips) rather than erroring on the first request.
    app.state.profile = load_profile(settings.config_dir / "profile.yaml")
    app.state.clock = now if now is not None else (lambda: datetime.now(UTC))

    conn = connect(settings.db_path)
    try:
        init_db(conn)
    finally:
        conn.close()

    app.include_router(router)
    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app


__all__ = ["create_app"]
