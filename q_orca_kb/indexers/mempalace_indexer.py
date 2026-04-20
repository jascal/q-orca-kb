"""mempalace indexer.

Wraps the mempalace API to chunk and upsert paper text into a palace.

The palace layout:
  palace_path/  -> ChromaDB persistent client directory
  collection    -> 'mempalace_drawers' (default)

Each drawer has metadata: wing, room, source_file, chunk_index, added_by,
filed_at, source_mtime (optional), indexed_at (ISO 8601 UTC), source_type
(arxiv | pdf | web).

We bypass ``mempalace.miner.add_drawer`` because its signature is fixed and
can't carry the two new fields; we replicate its drawer-id scheme verbatim so
upserts remain idempotent with pre-existing drawers.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from mempalace import miner, palace, searcher

DEFAULT_AGENT = "q-orca-kb"

# source_type values are a fixed, small set — document here so callers don't
# invent new ones.
SOURCE_TYPES = ("arxiv", "pdf", "web")

# arXiv ids: old form ``quant-ph/9508027`` OR new form ``1411.4028`` /
# ``2101.02109v2``. Used to infer source_type for pre-existing drawers that
# lack the explicit field.
_ARXIV_ID_RE = re.compile(r"^([a-z\-]+/\d{7}|\d{4}\.\d{4,5}(v\d+)?)$", re.IGNORECASE)


def _utc_now() -> str:
    """ISO 8601 UTC timestamp, seconds precision, ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def infer_source_type(source_file: str) -> str:
    """Guess source_type for drawers lacking the explicit field.

    Returns one of ``arxiv | pdf | web | unknown``. Used only for pre-existing
    drawers — new drawers carry ``source_type`` explicitly.
    """
    if not source_file:
        return "unknown"
    s = source_file.strip()
    if s.startswith("http://") or s.startswith("https://") or "#" in s:
        return "web"
    # Match arXiv id against the raw string first — old-form ids like
    # ``quant-ph/9508027`` include a slash that basename() would strip.
    if _ARXIV_ID_RE.match(s):
        return "arxiv"
    base = os.path.basename(s)
    if _ARXIV_ID_RE.match(base):
        return "arxiv"
    if base.lower().endswith(".pdf"):
        stem = base[:-4]
        if _ARXIV_ID_RE.match(stem):
            return "arxiv"
        return "pdf"
    return "unknown"


@dataclass
class IndexResult:
    arxiv_id: str
    wing: str
    room: str
    chunk_count: int
    indexed_count: int


def index_paper(
    palace_path: str,
    wing: str,
    room: str,
    arxiv_id: str,
    source_file: str,
    text: str,
    source_type: str | None = None,
    agent: str = DEFAULT_AGENT,
) -> IndexResult:
    """Chunk `text` and upsert each chunk as a drawer in the palace.

    ``source_type`` stamps ``source_type`` on every drawer created by this
    call. Callers should pass one of ``arxiv | pdf | web``. Left as ``None``
    for backwards compat (older callers that don't know their provenance); in
    that case the field is omitted from metadata.
    """
    collection = palace.get_collection(palace_path)
    chunks = miner.chunk_text(text, source_file)
    indexed = 0
    indexed_at = _utc_now()
    for chunk in chunks:
        content = chunk["content"] if isinstance(chunk, dict) else str(chunk)
        chunk_index = chunk["chunk_index"] if isinstance(chunk, dict) else indexed
        if not content.strip():
            continue

        drawer_id = (
            f"drawer_{wing}_{room}_"
            f"{hashlib.sha256((source_file + str(chunk_index)).encode()).hexdigest()[:24]}"
        )
        metadata: dict[str, object] = {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "chunk_index": chunk_index,
            "added_by": agent,
            "filed_at": datetime.now().isoformat(),
            "indexed_at": indexed_at,
        }
        if source_type is not None:
            metadata["source_type"] = source_type
        try:
            metadata["source_mtime"] = os.path.getmtime(source_file)
        except OSError:
            pass
        collection.upsert(
            documents=[content],
            ids=[drawer_id],
            metadatas=[metadata],
        )
        indexed += 1
    return IndexResult(
        arxiv_id=arxiv_id,
        wing=wing,
        room=room,
        chunk_count=len(chunks),
        indexed_count=indexed,
    )


def search(
    palace_path: str,
    query: str,
    wing: str | None = None,
    room: str | None = None,
    n_results: int = 5,
) -> dict:
    """Semantic search over the palace via mempalace.searcher."""
    return searcher.search_memories(
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
    )


# ---------------------------------------------------------------------------
# Source enumeration
# ---------------------------------------------------------------------------


def list_sources(
    palace_path: str,
    wing: str | None = None,
    room: str | None = None,
    source_type: str | None = None,
) -> list[dict]:
    """Enumerate distinct sources in the palace.

    Aggregates drawer metadata grouped by ``(wing, room, source_file)`` and
    returns one entry per distinct source, sorted by the tuple ascending.

    Pre-existing drawers lack ``indexed_at`` and ``source_type`` — timestamps
    are reported as ``None`` and ``source_type`` is inferred from
    ``source_file`` shape (see ``infer_source_type``).

    ``source_type`` filter is applied *after* inference so it works on
    pre-existing drawers too.
    """
    if not os.path.exists(palace_path):
        return []
    try:
        collection = palace.get_collection(palace_path)
    except Exception:
        return []

    # ChromaDB collection.get() with no ids returns everything; pulling only
    # metadatas avoids shipping documents and embeddings we don't need.
    got = collection.get(include=["metadatas"])
    metas = got.get("metadatas") or []

    buckets: dict[tuple[str, str, str], dict] = {}
    for m in metas:
        if not m:
            continue
        w = str(m.get("wing") or "")
        r = str(m.get("room") or "")
        src = str(m.get("source_file") or "")
        if not src:
            continue
        if wing is not None and w != wing:
            continue
        if room is not None and r != room:
            continue

        stored_type = m.get("source_type")
        st = str(stored_type) if stored_type else infer_source_type(src)

        key = (w, r, src)
        entry = buckets.get(key)
        if entry is None:
            entry = {
                "wing": w,
                "room": r,
                "source_file": src,
                "source_type": st,
                "drawer_count": 0,
                "first_indexed_at": None,
                "last_indexed_at": None,
            }
            buckets[key] = entry
        entry["drawer_count"] += 1

        ts = m.get("indexed_at")
        if ts:
            ts_str = str(ts)
            if entry["first_indexed_at"] is None or ts_str < entry["first_indexed_at"]:
                entry["first_indexed_at"] = ts_str
            if entry["last_indexed_at"] is None or ts_str > entry["last_indexed_at"]:
                entry["last_indexed_at"] = ts_str

    sources = list(buckets.values())
    if source_type is not None:
        sources = [s for s in sources if s["source_type"] == source_type]
    sources.sort(key=lambda s: (s["wing"], s["room"], s["source_file"]))
    return sources
