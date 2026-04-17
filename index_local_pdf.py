#!/usr/bin/env python3
"""Index a local PDF file into the q-orca-kb palace.

Usage:
    python index_local_pdf.py <path-to-pdf> <wing> <room> [--name <display-name>]

Example:
    python index_local_pdf.py data/pdfs/nielsen-chuang.pdf q-orca-physics textbook \
        --name "Nielsen & Chuang - Quantum Computation and Quantum Information"

The PDF is chunked and upserted into the palace. Re-running is idempotent.
"""

import argparse
import sys
from pathlib import Path

# ── resolve project root so imports work from any cwd ───────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from q_orca_kb.extractors.pdf_extractor import extract_text
from q_orca_kb.indexers.mempalace_indexer import index_paper

PALACE_PATH = str(ROOT / "data" / "palace")
PDF_DIR     = str(ROOT / "data" / "pdfs")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a local PDF into q-orca-kb.")
    parser.add_argument("pdf_path",  help="Path to the PDF file (absolute or relative to this script)")
    parser.add_argument("wing",      help="Wing to file under  (e.g. q-orca-physics)")
    parser.add_argument("room",      help="Room to file under  (e.g. textbook)")
    parser.add_argument("--name",    default=None,
                        help="Display name / arxiv_id substitute (defaults to filename stem)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.is_absolute():
        pdf_path = ROOT / pdf_path
    if not pdf_path.exists():
        print(f"ERROR: PDF not found at {pdf_path}", file=sys.stderr)
        sys.exit(1)

    display_name = args.name or pdf_path.stem
    print(f"Extracting text from: {pdf_path}")
    text = extract_text(str(pdf_path))
    if not text.strip():
        print("ERROR: extracted empty text — PDF may be scanned/image-only.", file=sys.stderr)
        sys.exit(1)
    print(f"Extracted {len(text):,} characters.")

    print(f"Indexing into palace  wing={args.wing!r}  room={args.room!r} ...")
    result = index_paper(
        palace_path=PALACE_PATH,
        wing=args.wing,
        room=args.room,
        arxiv_id=display_name,
        source_file=pdf_path.name,
        text=text,
    )
    print(f"Done. {result.indexed_count} chunks indexed  (total chunks: {result.chunk_count}).")


if __name__ == "__main__":
    main()
