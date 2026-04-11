"""q-orca-kb command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .indexers.mempalace_indexer import search
from .pipeline import index_one
from .seeds import SEEDS, Seed

DEFAULT_PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PALACE = str(DEFAULT_PROJECT_ROOT / "data" / "palace")
DEFAULT_PDF_DIR = str(DEFAULT_PROJECT_ROOT / "data" / "pdfs")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _find_seed(arxiv_id: str) -> Seed | None:
    for s in SEEDS:
        if s.arxiv_id == arxiv_id:
            return s
    return None


async def _index_seed(seed: Seed, palace: str, pdf_dir: str) -> None:
    print(f"\n[indexing] {seed.arxiv_id} -> {seed.wing}/{seed.room}")
    print(f"           {seed.title}")
    result = await index_one(
        arxiv_id=seed.arxiv_id,
        wing=seed.wing,
        room=seed.room,
        palace_path=palace,
        pdf_dir=pdf_dir,
    )
    if result.final_state == "done":
        print(
            f"  ✓ done — {result.indexed_count}/{result.chunk_count} drawers indexed"
        )
    else:
        print(
            f"  ✗ {result.final_state} after {result.attempts} attempts: {result.error}"
        )


def cmd_init(args: argparse.Namespace) -> int:
    """Create the palace + pdf directories."""
    os.makedirs(args.palace, exist_ok=True)
    os.makedirs(args.pdf_dir, exist_ok=True)
    # Touch the chromadb collection so the palace dir is initialized
    from mempalace import palace as palace_mod

    palace_mod.get_collection(args.palace)
    print(f"palace initialized at: {args.palace}")
    print(f"pdf cache at:          {args.pdf_dir}")
    print(f"seeds available:       {len(SEEDS)}")
    return 0


def cmd_fetch_seeds(args: argparse.Namespace) -> int:
    async def _run() -> None:
        for seed in SEEDS[: args.limit] if args.limit else SEEDS:
            await _index_seed(seed, args.palace, args.pdf_dir)

    asyncio.run(_run())
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    seed = _find_seed(args.arxiv_id)
    if seed is None:
        print(
            f"warning: {args.arxiv_id} is not in the seed list; "
            f"using wing={args.wing} room={args.room}",
            file=sys.stderr,
        )
        if not args.wing or not args.room:
            print("error: --wing and --room are required for non-seed papers", file=sys.stderr)
            return 2
        seed = Seed(
            arxiv_id=args.arxiv_id,
            wing=args.wing,
            room=args.room,
            title=args.arxiv_id,
        )
    asyncio.run(_index_seed(seed, args.palace, args.pdf_dir))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    response = search(
        palace_path=args.palace,
        query=args.query,
        wing=args.wing,
        room=args.room,
        n_results=args.n,
    )
    if "error" in response:
        print(f"error: {response['error']}", file=sys.stderr)
        if "hint" in response:
            print(f"hint:  {response['hint']}", file=sys.stderr)
        return 1
    hits = response.get("results", [])
    if not hits:
        print("(no results)")
        return 0
    for i, hit in enumerate(hits, start=1):
        wing = hit.get("wing", "?")
        room = hit.get("room", "?")
        src = hit.get("source_file", "?")
        sim = hit.get("similarity", 0.0)
        print(f"\n[{i}] sim={sim:.3f}  {wing}/{room}  {src}")
        snippet = hit.get("text", "").strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:280] + "..."
        print(f"    {snippet}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from mempalace import palace as palace_mod

    if not Path(args.palace).exists():
        print(f"palace not initialized: {args.palace}")
        return 1
    coll = palace_mod.get_collection(args.palace)
    count = coll.count()
    print(f"palace:   {args.palace}")
    print(f"drawers:  {count}")
    print(f"seeds:    {len(SEEDS)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-parse the workflow file to verify it loads."""
    from orca_runtime_python import parse_orca_md

    workflow = DEFAULT_PROJECT_ROOT / "workflows" / "paper_indexing.orca.md"
    md = parse_orca_md(workflow.read_text())
    print(f"workflow:    {workflow}")
    print(f"machine:     {md.name}")
    print(f"states:      {len(md.states)}")
    print(f"events:      {len(md.events)}")
    print(f"transitions: {len(md.transitions)}")
    print(f"effects:     {len(md.effects)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="q-orca-kb")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--palace", default=DEFAULT_PALACE)
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR)

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialize the palace directories")

    fs = sub.add_parser("fetch-seeds", help="fetch and index all seed papers")
    fs.add_argument("--limit", type=int, default=0, help="only index the first N seeds")

    f = sub.add_parser("fetch", help="fetch and index one paper by arxiv id")
    f.add_argument("arxiv_id")
    f.add_argument("--wing")
    f.add_argument("--room")

    s = sub.add_parser("search", help="semantic search over the palace")
    s.add_argument("query")
    s.add_argument("--wing")
    s.add_argument("--room")
    s.add_argument("-n", type=int, default=5)

    sub.add_parser("status", help="show palace stats")
    sub.add_parser("verify", help="parse and report on the orca workflow")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    handlers = {
        "init": cmd_init,
        "fetch-seeds": cmd_fetch_seeds,
        "fetch": cmd_fetch,
        "search": cmd_search,
        "status": cmd_status,
        "verify": cmd_verify,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
