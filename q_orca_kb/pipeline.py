"""Orca-driven paper indexing pipeline.

The PaperIndexing state machine (workflows/paper_indexing.orca.md) defines
the topology. This module:
  - loads + parses the machine
  - registers context-update actions
  - drives effects (FetchArxiv / ExtractText / IndexInPalace) externally,
    sending completion events back into the machine until it reaches a
    final state ('done' or 'aborted').
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_runtime_python import OrcaMachine, parse_orca_md

from .extractors.pdf_extractor import extract_text
from .fetchers.arxiv_fetcher import fetch_arxiv
from .indexers.mempalace_indexer import index_paper

log = logging.getLogger(__name__)

WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "paper_indexing.orca.md"


@dataclass
class IndexJobResult:
    arxiv_id: str
    wing: str
    final_state: str
    chunk_count: int
    indexed_count: int
    error: str
    attempts: int


_WORKFLOW_DEFINITION: Any = None


def _load_machine() -> Any:
    global _WORKFLOW_DEFINITION
    if _WORKFLOW_DEFINITION is None:
        _WORKFLOW_DEFINITION = parse_orca_md(WORKFLOW_PATH.read_text())
    return _WORKFLOW_DEFINITION


def _register_actions(machine: OrcaMachine) -> None:
    """Register pure context-update actions. Effects run in the driver."""

    def begin_fetch(ctx, event):
        return {}

    def begin_extract(ctx, event):
        payload = event or {}
        return {"pdf_path": payload.get("pdf_path", ctx.get("pdf_path", ""))}

    def begin_index(ctx, event):
        payload = event or {}
        return {"text": payload.get("text", ctx.get("text", ""))}

    def record_indexed(ctx, event):
        payload = event or {}
        return {
            "chunk_count": payload.get("chunk_count", 0),
            "indexed_count": payload.get("indexed_count", 0),
        }

    def record_error(ctx, event):
        payload = event or {}
        prev = ctx.get("error", "")
        new = payload.get("error", "unknown error")
        return {"error": f"{prev}; {new}" if prev else new}

    def bump_attempts(ctx, event):
        return {"attempts": ctx.get("attempts", 0) + 1, "error": ""}

    def finalize_failure(ctx, event):
        return {}

    machine.register_action("begin_fetch", begin_fetch)
    machine.register_action("begin_extract", begin_extract)
    machine.register_action("begin_index", begin_index)
    machine.register_action("record_indexed", record_indexed)
    machine.register_action("record_error", record_error)
    machine.register_action("bump_attempts", bump_attempts)
    machine.register_action("finalize_failure", finalize_failure)


async def index_one(
    arxiv_id: str,
    wing: str,
    room: str,
    palace_path: str,
    pdf_dir: str,
    max_attempts: int = 3,
) -> IndexJobResult:
    """Run the PaperIndexing machine to index one arXiv paper."""
    os.makedirs(pdf_dir, exist_ok=True)
    os.makedirs(palace_path, exist_ok=True)

    definition = _load_machine()
    machine = OrcaMachine(
        definition=definition,
        context={
            "arxiv_id": arxiv_id,
            "wing": wing,
            "pdf_path": "",
            "text": "",
            "chunk_count": 0,
            "indexed_count": 0,
            "attempts": 0,
            "max_attempts": max_attempts,
            "error": "",
        },
    )
    _register_actions(machine)
    await machine.start()

    await machine.send("start", {"arxiv_id": arxiv_id, "wing": wing})

    try:
        while True:
            state = str(machine.state)
            log.debug("paper=%s state=%s", arxiv_id, state)

            if state in ("done", "aborted"):
                break

            if state == "fetching":
                try:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None, functools.partial(fetch_arxiv, arxiv_id, pdf_dir)
                    )
                    await machine.send("fetch_ok", {"pdf_path": result.pdf_path})
                except Exception as e:
                    log.warning("fetch failed for %s: %s", arxiv_id, e)
                    await machine.send("fetch_failed", {"error": f"fetch: {e}"})

            elif state == "extracting":
                try:
                    pdf_path = machine.context.get("pdf_path", "")
                    loop = asyncio.get_event_loop()
                    text = await loop.run_in_executor(
                        None, extract_text, pdf_path
                    )
                    if not text.strip():
                        raise ValueError("extracted empty text")
                    await machine.send("extract_ok", {"text": text})
                except Exception as e:
                    log.warning("extract failed for %s: %s", arxiv_id, e)
                    await machine.send("extract_failed", {"error": f"extract: {e}"})

            elif state == "indexing":
                try:
                    text = machine.context.get("text", "")
                    pdf_path = machine.context.get("pdf_path", "")
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            index_paper,
                            palace_path=palace_path,
                            wing=wing,
                            room=room,
                            arxiv_id=arxiv_id,
                            source_file=pdf_path,
                            text=text,
                        ),
                    )
                    await machine.send(
                        "index_ok",
                        {
                            "chunk_count": result.chunk_count,
                            "indexed_count": result.indexed_count,
                        },
                    )
                except Exception as e:
                    log.warning("index failed for %s: %s", arxiv_id, e)
                    await machine.send("index_failed", {"error": f"index: {e}"})

            elif state == "failed":
                if machine.context.get("attempts", 0) + 1 < machine.context.get("max_attempts", 3):
                    await machine.send("retry")
                else:
                    await machine.send("give_up")

            else:
                # Unknown state — bail out to avoid an infinite loop
                log.error("paper=%s stuck in unknown state %s", arxiv_id, state)
                break
    finally:
        final_state = str(machine.state)
        ctx = machine.context
        await machine.stop()
    return IndexJobResult(
        arxiv_id=arxiv_id,
        wing=wing,
        final_state=final_state,
        chunk_count=int(ctx.get("chunk_count", 0)),
        indexed_count=int(ctx.get("indexed_count", 0)),
        error=str(ctx.get("error", "")),
        attempts=int(ctx.get("attempts", 0)),
    )
