"""Shared pytest fixtures.

The embedding-model fixture loads the real default SentenceTransformer once per
session. This is the one place a test is permitted to reach the network — to
download the model on first run (T15/T16); it is cached on disk thereafter and
every later run is offline (RALPH testing standards / tasks.md T15). CI caches
the HuggingFace dir between runs so the download happens at most once.

The first (cold-cache) download can transiently hit HTTP 429 (HF rate-limit) or
a network blip; the fixture retries that single sanctioned fetch with bounded
exponential backoff so a flaky cold run does not fail the suite. The error is
narrowed to ``requests`` failures (HfHubHTTPError subclasses requests' HTTPError),
never a broad catch.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from requests.exceptions import RequestException

from jobfinder.score import load_model
from jobfinder.settings import DEFAULT_EMBED_MODEL

if TYPE_CHECKING:
    from jobfinder.score import Encoder

# Bounded retry for the one sanctioned model download (cold cache only): rides out
# a transient HF 429 / network blip. 5 attempts, 4s·2**n backoff → ≤60s worst case.
_MODEL_DOWNLOAD_RETRIES = 5
_MODEL_DOWNLOAD_BASE_DELAY_S = 4.0


@pytest.fixture(scope="session")
def embed_model() -> Encoder:
    """The real default embedding model, loaded once and reused across tests."""
    last_error: RequestException | None = None
    for attempt in range(_MODEL_DOWNLOAD_RETRIES):
        try:
            return load_model(DEFAULT_EMBED_MODEL)
        except RequestException as error:  # transient HF rate-limit / network blip
            last_error = error
            if attempt < _MODEL_DOWNLOAD_RETRIES - 1:
                time.sleep(_MODEL_DOWNLOAD_BASE_DELAY_S * 2**attempt)
    raise RuntimeError(
        f"could not download embedding model {DEFAULT_EMBED_MODEL!r} after "
        f"{_MODEL_DOWNLOAD_RETRIES} attempts"
    ) from last_error
