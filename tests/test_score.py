"""Tests for résumé extraction (T14, LLD §6.5) and embeddings (T15, LLD §6.1–§6.3).

Extraction and the chunk/pool math run fully offline against committed fixtures
and a deterministic fake encoder. The dimension / unit-norm / determinism checks
use the real model via the session-scoped ``embed_model`` fixture (downloaded
once, then offline) since those properties are model-specific.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from jobfinder.models import Job, LocationBucket, Seniority
from jobfinder.score import (
    _chunk_text,
    build_profile_vector,
    embed_job,
    extract_resume,
    load_model,
    render_targeting,
)
from jobfinder.settings import load_profile

_FIXTURES = Path(__file__).parent / "fixtures"
_PROFILE_PATH = _FIXTURES / "config" / "profile.yaml"


class _RecordingEncoder:
    """Deterministic offline stand-in for SentenceTransformer.

    Records the exact text it was asked to encode so chunking can be asserted,
    and derives each vector from a stable hash so output is reproducible.
    """

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.encoded: list[list[str]] = []

    def encode(
        self, sentences: str | list[str], *, normalize_embeddings: bool = False
    ) -> np.ndarray:
        single = isinstance(sentences, str)
        items = [sentences] if single else list(sentences)
        self.encoded.append(items)
        vecs = np.stack([self._vec(text) for text in items])
        if normalize_embeddings:
            vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs[0] if single else vecs

    def _vec(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha1(text.encode()).digest()[:4], "big")
        return np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)


def _make_job(*, title: str, description: str) -> Job:
    now = datetime(2026, 6, 4, tzinfo=UTC)
    return Job(
        id="job1",
        source="greenhouse",
        source_id="1",
        company="Acme",
        title=title,
        description=description,
        location_raw="Remote",
        is_remote=True,
        location_bucket=LocationBucket.REMOTE,
        seniority=Seniority.SENIOR,
        url="https://example.com/job/1",
        posted_at=now,
        date_unknown=False,
        first_seen_at=now,
        last_seen_at=now,
    )


# The fixture résumés are a senior-backend CV carrying every must-have skill, so
# T15/T16 scoring tests can reuse them (LLD §6.2). Assert the stack survives
# extraction in each format.
_EXPECTED_SKILLS = ("Java", "Kotlin", "Python", "AWS")


@pytest.mark.parametrize("filename", ["resume.txt", "resume.md", "resume.docx", "resume.pdf"])
def test_extract_resume_each_format_yields_skills(filename: str) -> None:
    text = extract_resume(_FIXTURES / filename)

    assert text.strip(), f"{filename} extracted empty text"
    for skill in _EXPECTED_SKILLS:
        assert skill in text, f"{skill} missing from extracted {filename}"


def test_extract_docx_includes_table_cells() -> None:
    # The docx fixture stores "Primary languages" in a table; the extractor must
    # walk table cells, not just paragraphs (LLD §6.5).
    text = extract_resume(_FIXTURES / "resume.docx")

    assert "Primary languages" in text


def test_extract_resume_accepts_str_path() -> None:
    text = extract_resume(str(_FIXTURES / "resume.txt"))

    assert "Backend" in text


def test_extract_pdf_falls_back_to_pdfplumber_when_pypdf_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate pypdf returning no text (empty/garbled layout) so the pdfplumber
    # fallback runs against the real fixture and recovers the content (LLD §6.5).
    class _EmptyPage:
        def extract_text(self) -> str:
            return ""

    class _EmptyReader:
        def __init__(self, _path: str) -> None:
            self.pages = [_EmptyPage()]

    monkeypatch.setattr("pypdf.PdfReader", _EmptyReader)

    text = extract_resume(_FIXTURES / "resume.pdf")

    assert "Java" in text  # recovered by the pdfplumber fallback, not pypdf


def test_extract_resume_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError, match="résumé file not found"):
        extract_resume(_FIXTURES / "does_not_exist.pdf")


def test_extract_resume_unsupported_existing_file(tmp_path: Path) -> None:
    bad = tmp_path / "resume.rtf"
    bad.write_text("not a résumé format we support", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported résumé format"):
        extract_resume(bad)


# --- T15: embeddings & profile vector (LLD §6.1–§6.3) ---


def test_chunk_text_splits_into_windows_and_preserves_tail() -> None:
    words = [f"w{i}" for i in range(400)]
    chunks = _chunk_text(" ".join(words), 180)

    assert len(chunks) == 3  # 180 + 180 + 40
    assert "w399" in chunks[-1]  # the tail word survives, not truncated
    assert " ".join(chunks).split() == words  # full sequence round-trips


def test_chunk_text_empty_returns_single_chunk() -> None:
    assert _chunk_text("", 180) == [""]


def test_render_targeting_includes_role_skills_seniority() -> None:
    block = render_targeting(load_profile(_PROFILE_PATH))

    assert "backend" in block
    for skill in ("java", "kotlin", "python", "aws"):
        assert skill in block
    assert "senior" in block


def test_build_profile_vector_chunks_long_resume_tail_not_truncated() -> None:
    # A résumé well past the model's input limit must be split so its tail still
    # reaches the encoder (LLD §6.2) — asserted via a fake that records chunks.
    profile = load_profile(_PROFILE_PATH)
    long_resume = " ".join(f"word{i}" for i in range(1000))
    encoder = _RecordingEncoder()

    vec = build_profile_vector(profile, long_resume, model=encoder)

    assert len(encoder.encoded) == 1  # one batched encode call
    chunks = encoder.encoded[0]
    assert len(chunks) > 1  # split, not a single truncated pass
    assert any("word999" in chunk for chunk in chunks)  # tail included
    assert vec.shape == (encoder.dim,)
    assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)  # re-normalized


def test_build_profile_vector_dim_and_unit_norm(embed_model) -> None:
    profile = load_profile(_PROFILE_PATH)
    resume_text = extract_resume(_FIXTURES / "resume.txt")

    vec = build_profile_vector(profile, resume_text, model=embed_model)

    assert vec.shape == (embed_model.get_sentence_embedding_dimension(),)
    assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)


def test_build_profile_vector_is_deterministic(embed_model) -> None:
    profile = load_profile(_PROFILE_PATH)
    resume_text = extract_resume(_FIXTURES / "resume.txt")

    first = build_profile_vector(profile, resume_text, model=embed_model)
    second = build_profile_vector(profile, resume_text, model=embed_model)

    assert np.array_equal(first, second)


def test_embed_job_unit_norm_and_deterministic(embed_model) -> None:
    job = _make_job(
        title="Senior Backend Engineer",
        description="Build distributed services in Java, Kotlin and Python on AWS.",
    )

    first = embed_job(job, model=embed_model)
    second = embed_job(job, model=embed_model)

    assert first.shape == (embed_model.get_sentence_embedding_dimension(),)
    assert np.isclose(np.linalg.norm(first), 1.0, atol=1e-5)
    assert np.array_equal(first, second)


def test_embed_job_char_caps_pathological_description(embed_model) -> None:
    # A megabyte description must not break embedding; it is char-capped first.
    job = _make_job(title="Backend Engineer", description="Java " * 5000)

    vec = embed_job(job, model=embed_model)

    assert vec.shape == (embed_model.get_sentence_embedding_dimension(),)


def test_load_model_caches_per_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from the session model cache and avoid any real download.
    import jobfinder.score as score_mod

    monkeypatch.setattr(score_mod, "_MODEL_CACHE", {})
    constructed: list[str] = []

    class _FakeSentenceTransformer:
        def __init__(self, name: str) -> None:
            constructed.append(name)

        def encode(self, *args: object, **kwargs: object) -> np.ndarray:
            return np.zeros(3, dtype=np.float32)

    monkeypatch.setattr("sentence_transformers.SentenceTransformer", _FakeSentenceTransformer)

    first = load_model("tiny-test-model")
    second = load_model("tiny-test-model")

    assert first is second
    assert constructed == ["tiny-test-model"]  # constructed once, then cached
