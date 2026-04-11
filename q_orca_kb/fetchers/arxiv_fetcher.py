"""arXiv paper fetcher.

Wraps the `arxiv` library and downloads PDFs to a local directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import arxiv


@dataclass
class FetchResult:
    arxiv_id: str
    pdf_path: str
    title: str
    summary: str
    authors: list[str]


def fetch_arxiv(arxiv_id: str, dest_dir: str) -> FetchResult:
    """Download a single arXiv paper by id (e.g. '1411.4028' or 'quant-ph/9508027')."""
    os.makedirs(dest_dir, exist_ok=True)
    client = arxiv.Client()
    search = arxiv.Search(id_list=[arxiv_id])
    result = next(client.results(search), None)
    if result is None:
        raise FileNotFoundError(f"arXiv id not found: {arxiv_id}")

    safe_id = arxiv_id.replace("/", "_")
    filename = f"{safe_id}.pdf"
    pdf_path = os.path.join(dest_dir, filename)
    if not os.path.exists(pdf_path):
        result.download_pdf(dirpath=dest_dir, filename=filename)

    return FetchResult(
        arxiv_id=arxiv_id,
        pdf_path=pdf_path,
        title=result.title.strip(),
        summary=result.summary.strip(),
        authors=[a.name for a in result.authors],
    )
