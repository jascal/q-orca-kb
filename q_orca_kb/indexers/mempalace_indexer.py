"""mempalace indexer.

Wraps the mempalace API to chunk and upsert paper text into a palace.

The palace layout:
  palace_path/  -> ChromaDB persistent client directory
  collection    -> 'mempalace_drawers' (default)

Each drawer has metadata: wing, room, source_file, chunk_index, added_by, filed_at.
"""

from __future__ import annotations

from dataclasses import dataclass

from mempalace import miner, palace, searcher

DEFAULT_AGENT = "q-orca-kb"


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
    agent: str = DEFAULT_AGENT,
) -> IndexResult:
    """Chunk `text` and upsert each chunk as a drawer in the palace."""
    collection = palace.get_collection(palace_path)
    chunks = miner.chunk_text(text, source_file)
    indexed = 0
    for chunk in chunks:
        # mempalace.miner.chunk_text returns dicts {content, chunk_index}
        content = chunk["content"] if isinstance(chunk, dict) else str(chunk)
        chunk_index = chunk["chunk_index"] if isinstance(chunk, dict) else indexed
        if not content.strip():
            continue
        miner.add_drawer(
            collection=collection,
            wing=wing,
            room=room,
            content=content,
            source_file=source_file,
            chunk_index=chunk_index,
            agent=agent,
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
