"""Shared HTTP client: throttle, retry/backoff, and on-disk cache (LLD §3.2).

Every outbound GET in Job Finder goes through this one module so the Cost &
Safety invariants hold uniformly: a single connection-pooled :class:`httpx.Client`
with sane timeouts, a per-host throttle that keeps us under ~1 req/s/source, a
bounded retry on transient failures, and a JSON file cache that lets repeated
polls (and the test suite) avoid the network entirely.

The public surface is the :class:`HttpClient` class (fully injectable for
deterministic, offline tests) plus the module-level :func:`get_json` /
:func:`get_text` helpers from LLD §3.2 that delegate to a process-wide default
client built from :class:`~jobfinder.settings.Settings`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from jobfinder.settings import DEFAULT_CACHE_TTL_S, DEFAULT_THROTTLE_S, Settings

logger = logging.getLogger(__name__)

# --- Tunable constants, each sourced from the design docs -------------------

# LLD §3.2: connect timeout 5s, overall 10s.
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# A descriptive, honest User-Agent (LLD §3.2); no identifying personal data.
USER_AGENT = "jobfinder/0.1 (local personal job-search tool)"
# LLD §3.2: retry on these status codes plus connect/read timeouts.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# LLD §3.2: "max 3 attempts".
MAX_ATTEMPTS = 3
# LLD §3.2: exponential backoff base — sleep ~ 0.5 * 2**n with jitter.
BACKOFF_BASE_S = 0.5
# Jitter is sampled in [0, BACKOFF_BASE_S * JITTER_FRAC) and added to the delay.
JITTER_FRAC = 0.25


class HttpClient:
    """A throttled, retrying, cached wrapper over a single ``httpx.Client``.

    All time/IO seams are injectable so tests run offline and deterministically:

    * ``transport`` — an ``httpx.MockTransport`` to serve fixtures without network.
    * ``monotonic`` / ``sleep`` — drive the per-host throttle and backoff with a
      fake clock instead of real wall-time waits.
    * ``wall_clock`` — stamps cache entries (cache freshness is wall-time based so
      it survives across processes).
    * ``rng`` — seeds the backoff jitter.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        throttle_s: float = DEFAULT_THROTTLE_S,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
        no_cache: bool = False,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._throttle_s = throttle_s
        self._default_ttl_s = cache_ttl_s
        self._no_cache = no_cache
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._rng = rng if rng is not None else random.Random()
        # Last completed request time per host, for the throttle gate.
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            timeout=HTTP_TIMEOUT,
            http2=True,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    # --- Public API (LLD §3.2) ---------------------------------------------

    def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, ttl_s: int | None = None
    ) -> Any:
        """GET ``url`` and parse the body as JSON (used by the JSON ATS feeds)."""
        return json.loads(self._get_text(url, params=params, ttl_s=ttl_s))

    def get_text(
        self, url: str, *, params: dict[str, Any] | None = None, ttl_s: int | None = None
    ) -> str:
        """GET ``url`` and return the raw body text (used for XML feeds)."""
        return self._get_text(url, params=params, ttl_s=ttl_s)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- Internals ----------------------------------------------------------

    def _get_text(self, url: str, *, params: dict[str, Any] | None, ttl_s: int | None) -> str:
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        # Canonicalize the full URL (including query) so the cache key and the
        # throttle host both reflect the exact request.
        full_url = httpx.URL(url, params=params)
        cache_path = self._cache_path(full_url)

        if not self._no_cache:
            cached = self._read_cache(cache_path, ttl)
            if cached is not None:
                return cached  # cache hit: no network, no throttle (LLD §3.2)

        body = self._request_with_retry(full_url)

        if not self._no_cache and ttl > 0:
            self._write_cache(cache_path, body)
        return body

    def _request_with_retry(self, url: httpx.URL) -> str:
        last_timeout: httpx.TimeoutException | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._throttle(url.host)
            try:
                resp = self._client.get(url)
            except httpx.TimeoutException as exc:
                last_timeout = exc
                self._backoff(attempt)
                continue

            if resp.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                self._wait_before_retry(resp, attempt)
                continue

            # 2xx returns; any other 4xx/5xx (or an exhausted retryable status)
            # raises a descriptive HTTPStatusError here.
            resp.raise_for_status()
            return resp.text

        # All attempts exhausted on timeouts.
        assert last_timeout is not None  # loop only exits here via timeout path
        raise last_timeout

    def _throttle(self, host: str) -> None:
        """Block until ``throttle_s`` has elapsed since the last call to ``host``."""
        last = self._last_request.get(host)
        if last is not None:
            wait = self._throttle_s - (self._monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._monotonic()

    def _wait_before_retry(self, resp: httpx.Response, attempt: int) -> None:
        """Honor ``Retry-After`` on 429, else exponential backoff (LLD §3.2)."""
        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after is not None:
                self._sleep(retry_after)
                return
        self._backoff(attempt)

    def _backoff(self, attempt: int) -> None:
        delay = BACKOFF_BASE_S * (2**attempt)
        delay += self._rng.uniform(0.0, BACKOFF_BASE_S * JITTER_FRAC)
        self._sleep(delay)

    def _cache_path(self, url: httpx.URL) -> Path:
        key = hashlib.sha1(str(url).encode("utf-8")).hexdigest()  # LLD §3.2: key = sha1(url)
        return self._cache_dir / f"{key}.json"

    def _read_cache(self, path: Path, ttl_s: int) -> str | None:
        if ttl_s <= 0 or not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt/unreadable cache file is a miss, not a failure — log and
            # refetch rather than crash a poll.
            logger.debug("ignoring unreadable cache %s: %s", path, exc)
            return None
        fetched_at = entry.get("fetched_at")
        body = entry.get("body")
        if not isinstance(fetched_at, int | float) or not isinstance(body, str):
            return None
        if self._wall_clock() - fetched_at >= ttl_s:
            return None
        return body

    def _write_cache(self, path: Path, body: str) -> None:
        entry = {"fetched_at": self._wall_clock(), "body": body}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entry), encoding="utf-8")
        except OSError as exc:
            # Caching is best-effort; a write failure must not abort the fetch.
            logger.warning("could not write HTTP cache %s: %s", path, exc)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` delay-seconds header; ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        # HTTP-date form is unsupported here; fall back to normal backoff.
        return None
    return seconds if seconds >= 0 else None


# --- Process-wide default client (LLD §3.2 module-level helpers) -------------

_default_client: HttpClient | None = None


def get_default_client() -> HttpClient:
    """Return the lazily-built default client configured from ``Settings``."""
    global _default_client
    if _default_client is None:
        settings = Settings()
        _default_client = HttpClient(
            cache_dir=settings.cache_dir,
            throttle_s=settings.throttle_s,
            cache_ttl_s=settings.cache_ttl_s,
        )
    return _default_client


def configure_default_client(client: HttpClient) -> None:
    """Install ``client`` as the process default (e.g. CLI ``--no-cache`` wiring)."""
    global _default_client
    if _default_client is not None and _default_client is not client:
        _default_client.close()
    _default_client = client


def reset_default_client() -> None:
    """Drop and close the default client (used for test isolation)."""
    global _default_client
    if _default_client is not None:
        _default_client.close()
        _default_client = None


def get_json(url: str, *, params: dict[str, Any] | None = None, ttl_s: int | None = None) -> Any:
    """Module-level convenience over the default client (LLD §3.2)."""
    return get_default_client().get_json(url, params=params, ttl_s=ttl_s)


def get_text(url: str, *, params: dict[str, Any] | None = None, ttl_s: int | None = None) -> str:
    """Module-level convenience over the default client (LLD §3.2)."""
    return get_default_client().get_text(url, params=params, ttl_s=ttl_s)


__all__ = [
    "HttpClient",
    "get_json",
    "get_text",
    "get_default_client",
    "configure_default_client",
    "reset_default_client",
]
