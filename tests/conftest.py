"""Shared pytest fixtures.

The embedding-model fixture loads the real default SentenceTransformer once per
session. This is the one place a test is permitted to reach the network — to
download the model on first run (T15/T16); it is cached on disk thereafter and
every later run is offline (RALPH testing standards / tasks.md T15).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jobfinder.score import load_model
from jobfinder.settings import DEFAULT_EMBED_MODEL

if TYPE_CHECKING:
    from jobfinder.score import Encoder


@pytest.fixture(scope="session")
def embed_model() -> Encoder:
    """The real default embedding model, loaded once and reused across tests."""
    return load_model(DEFAULT_EMBED_MODEL)
