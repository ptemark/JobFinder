"""Tests for résumé extraction (T14, LLD §6.5) and embeddings (T15, LLD §6.1–§6.3).

Extraction and the chunk/pool math run fully offline against committed fixtures
and a deterministic fake encoder. The dimension / unit-norm / determinism checks
use the real model via the session-scoped ``embed_model`` fixture (downloaded
once, then offline) since those properties are model-specific.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from jobfinder.models import Job, LocationBucket, ScoreBreakdown, Seniority
from jobfinder.score import (
    _chunk_text,
    build_profile_vector,
    embed_job,
    extract_resume,
    load_model,
    render_targeting,
    score_job,
)
from jobfinder.settings import Weights, load_profile

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


_NOW = datetime(2026, 6, 4, tzinfo=UTC)


def _make_job(
    *,
    title: str,
    description: str,
    location_bucket: LocationBucket = LocationBucket.REMOTE,
    seniority: Seniority = Seniority.SENIOR,
    posted_at: datetime | None = _NOW,
    date_unknown: bool = False,
) -> Job:
    return Job(
        id="job1",
        source="greenhouse",
        source_id="1",
        company="Acme",
        title=title,
        description=description,
        location_raw="Remote",
        is_remote=location_bucket == LocationBucket.REMOTE,
        location_bucket=location_bucket,
        seniority=seniority,
        url="https://example.com/job/1",
        posted_at=posted_at,
        date_unknown=date_unknown,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
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


# --- T16: scoring math & weights (LLD §6.3–§6.4) ---

# The default weights (config/weights.yaml.example). They happen to sum to 1.0,
# so a separate test uses off-sum weights to prove the §6.4 normalization.
_DEFAULT_WEIGHTS = Weights(semantic=0.35, skill=0.30, location=0.20, recency=0.15)

# A unit vector reused where the semantic component is not under test; passing
# the same array as profile and job vector yields cosine 1.0.
_UNIT_VEC = np.array([1.0, 0.0, 0.0], dtype=np.float32)


def test_score_components_full_match() -> None:
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(
        title="Senior Backend Engineer",
        description="Java, Kotlin, Python and AWS at scale.",
        location_bucket=LocationBucket.REMOTE,
        posted_at=_NOW,
    )

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.semantic == 1.0
    assert sb.skill == 1.0  # all four must-have skills present
    assert sb.location == 1.0
    assert sb.recency == 1.0  # posted now → age 0
    assert sb.final == 100.0


def test_semantic_clamped_to_zero_for_opposed_vectors() -> None:
    profile = load_profile(_PROFILE_PATH)
    opposed = np.array([-1.0, 0.0, 0.0], dtype=np.float32)
    job = _make_job(title="Backend Engineer", description="Java Kotlin Python AWS")

    sb = score_job(job, _UNIT_VEC, opposed, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.semantic == 0.0  # cosine -1 clamped up to 0


def test_skill_score_word_boundary_and_partial() -> None:
    # "JavaScript" must not satisfy "java"; only "Python" is a real hit → 1 of 4.
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(title="Engineer", description="We build with JavaScript and Python.")

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.skill == 0.25


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        (LocationBucket.REMOTE, 1.0),
        (LocationBucket.VANCOUVER, 0.85),
        (LocationBucket.TORONTO, 0.7),
        (LocationBucket.OTHER_CANADA, 0.4),
        (LocationBucket.OTHER, 0.0),
    ],
)
def test_location_bonus_per_bucket(bucket: LocationBucket, expected: float) -> None:
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(title="Backend Engineer", description="Java", location_bucket=bucket)

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.location == expected


def test_recency_decays_linearly_within_window() -> None:
    profile = load_profile(_PROFILE_PATH)  # max_age_days 21
    job = _make_job(
        title="Backend Engineer", description="Java", posted_at=_NOW - timedelta(days=10)
    )

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.recency == pytest.approx(1.0 - 10 / 21)


def test_recency_zero_at_cutoff() -> None:
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(
        title="Backend Engineer", description="Java", posted_at=_NOW - timedelta(days=21)
    )

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.recency == 0.0


def test_recency_date_unknown_gets_fixed_score() -> None:
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(title="Backend Engineer", description="Java", posted_at=None, date_unknown=True)

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert sb.recency == 0.3


def test_final_is_weight_normalized_and_rounded() -> None:
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(
        title="Backend Engineer",
        description="Java and AWS only.",  # skill 2 of 4 = 0.5
        location_bucket=LocationBucket.TORONTO,  # 0.7
        posted_at=_NOW - timedelta(days=10),  # recency 1 - 10/21
    )

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    expected = round(100 * (0.35 * 1.0 + 0.30 * 0.5 + 0.20 * 0.7 + 0.15 * (1 - 10 / 21)), 1)
    assert sb.skill == 0.5
    assert sb.final == expected


def test_final_normalizes_by_weight_sum() -> None:
    # Weights summing to 2.0; a full-match job must still score 100, proving final
    # is divided by the weight sum (LLD §6.4), not just the raw weighted sum.
    profile = load_profile(_PROFILE_PATH)
    weights = Weights(semantic=0.7, skill=0.6, location=0.4, recency=0.3)
    job = _make_job(
        title="Backend Engineer",
        description="Java Kotlin Python AWS",
        location_bucket=LocationBucket.REMOTE,
        posted_at=_NOW,
    )

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=weights, now=_NOW)

    assert sb.final == 100.0


def test_score_job_returns_full_breakdown() -> None:
    profile = load_profile(_PROFILE_PATH)
    job = _make_job(title="Backend Engineer", description="Java Kotlin Python AWS")

    sb = score_job(job, _UNIT_VEC, _UNIT_VEC, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW)

    assert isinstance(sb, ScoreBreakdown)
    assert sb.scored_at == _NOW
    assert 0.0 <= sb.final <= 100.0


def test_skill_weight_beats_higher_semantic_off_stack() -> None:
    # The load-bearing skill-dominance check (tasks.md T16): an off-stack role
    # with the *higher* semantic match still loses to a Java/AWS role because the
    # skill weight steers the ranking. Vectors are hand-built so the comparison is
    # deterministic and offline; both jobs share location + recency so only the
    # semantic-vs-skill trade-off decides the order.
    profile = load_profile(_PROFILE_PATH)
    off_stack = _make_job(
        title="Frontend Designer",
        description="Craft delightful UI with React, Figma and CSS.",
    )
    off_stack_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)  # cosine 1.0
    java_aws = _make_job(
        title="Backend Engineer",
        description="Build services in Java, Kotlin and Python on AWS.",
    )
    java_aws_vec = np.array([0.6, 0.8, 0.0], dtype=np.float32)  # cosine 0.6

    off_sb = score_job(
        off_stack, _UNIT_VEC, off_stack_vec, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW
    )
    java_sb = score_job(
        java_aws, _UNIT_VEC, java_aws_vec, profile=profile, weights=_DEFAULT_WEIGHTS, now=_NOW
    )

    assert off_sb.semantic > java_sb.semantic  # off-stack is the better semantic match
    assert java_sb.skill > off_sb.skill
    assert java_sb.final > off_sb.final  # skill weight flips the ranking


def test_senior_remote_java_aws_outranks_junior_onsite_frontend(embed_model) -> None:
    # The load-bearing ordering check (tasks.md T16 / spec M3 acceptance b), run
    # end-to-end through the real model: a senior remote Java/AWS backend role must
    # outrank a junior onsite frontend role.
    profile = load_profile(_PROFILE_PATH)
    resume_text = extract_resume(_FIXTURES / "resume.txt")
    profile_vec = build_profile_vector(profile, resume_text, model=embed_model)

    senior = _make_job(
        title="Senior Backend Engineer",
        description=(
            "Design and operate distributed backend services in Java, Kotlin and "
            "Python on AWS. Own reliability, scaling and on-call for core systems."
        ),
        location_bucket=LocationBucket.REMOTE,
        seniority=Seniority.SENIOR,
    )
    junior = _make_job(
        title="Junior Frontend Developer",
        description=(
            "Entry-level role building UI components with React, HTML and CSS. "
            "Focus on visual design and accessibility in the browser."
        ),
        location_bucket=LocationBucket.OTHER,
        seniority=Seniority.JUNIOR,
    )

    senior_sb = score_job(
        senior,
        profile_vec,
        embed_job(senior, model=embed_model),
        profile=profile,
        weights=_DEFAULT_WEIGHTS,
        now=_NOW,
    )
    junior_sb = score_job(
        junior,
        profile_vec,
        embed_job(junior, model=embed_model),
        profile=profile,
        weights=_DEFAULT_WEIGHTS,
        now=_NOW,
    )

    assert senior_sb.final > junior_sb.final
