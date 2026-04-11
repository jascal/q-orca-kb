"""PDF text extractor (pypdf-backed)."""

from __future__ import annotations

from pypdf import PdfReader


def extract_text(pdf_path: str) -> str:
    """Read all pages of a PDF and concatenate the text."""
    reader = PdfReader(pdf_path)
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)
