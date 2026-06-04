"""Résumé extraction and scoring (LLD §6).

This module owns the semantic-matching half of the pipeline. T14 lands the
first piece — :func:`extract_resume`, which reads the user's full résumé from
``config/resume.{pdf,docx,txt,md}`` into plain text for the profile vector
(LLD §6.5). The embedding/scoring functions (`build_profile_vector`,
`embed_job`, `score_job`) are added by T15/T16 and intentionally kept out of
this module's import path so extracting a résumé never pulls in torch.
"""

from __future__ import annotations

from pathlib import Path

# Supported résumé formats, dispatched on the lowercased file suffix (LLD §6.5).
_TEXT_SUFFIXES = frozenset({".txt", ".md"})
_PDF_SUFFIX = ".pdf"
_DOCX_SUFFIX = ".docx"
_SUPPORTED_SUFFIXES = _TEXT_SUFFIXES | {_PDF_SUFFIX, _DOCX_SUFFIX}


def extract_resume(path: str | Path) -> str:
    """Extract the full plain text of a résumé file (LLD §6.5).

    Dispatches on the file extension: ``.pdf`` via pypdf (falling back to
    pdfplumber when pypdf yields no text), ``.docx`` via python-docx
    (paragraphs and tables), ``.txt``/``.md`` read directly as UTF-8.

    Raises:
        FileNotFoundError: the résumé file does not exist.
        ValueError: the file extension is not a supported résumé format.
    """
    resume_path = Path(path)
    if not resume_path.exists():
        raise FileNotFoundError(f"résumé file not found: {resume_path}")

    suffix = resume_path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return resume_path.read_text(encoding="utf-8")
    if suffix == _DOCX_SUFFIX:
        return _extract_docx(resume_path)
    if suffix == _PDF_SUFFIX:
        return _extract_pdf(resume_path)

    supported = ", ".join(sorted(_SUPPORTED_SUFFIXES))
    raise ValueError(
        f"unsupported résumé format {suffix!r} for {resume_path}; supported: {supported}"
    )


def _extract_pdf(path: Path) -> str:
    """Extract PDF text via pypdf, falling back to pdfplumber if empty (LLD §6.5)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if text.strip():
        return text
    # pypdf produced nothing usable (empty/garbled layout) — try the heavier,
    # more layout-tolerant pdfplumber extractor before giving up.
    return _extract_pdf_pdfplumber(path)


def _extract_pdf_pdfplumber(path: Path) -> str:
    """Fallback PDF extraction via pdfplumber for layouts pypdf can't read."""
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _extract_docx(path: Path) -> str:
    """Extract docx paragraphs and table cells in document order (LLD §6.5)."""
    from docx import Document

    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


__all__ = ["extract_resume"]
