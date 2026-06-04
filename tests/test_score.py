"""Tests for résumé extraction (T14, LLD §6.5).

Each supported format has a committed fixture under ``tests/fixtures/`` that
must extract non-empty text carrying the must-have skills; the missing-file and
unsupported-format sad paths raise clear errors. No network — deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder.score import extract_resume

_FIXTURES = Path(__file__).parent / "fixtures"

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
