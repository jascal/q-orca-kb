"""Tests for source enumeration: indexer.list_sources + MCP plumbing.

We stub out ``palace.get_collection`` so these tests run without ChromaDB
and without touching the live palace.
"""

from __future__ import annotations

import asyncio

import pytest

from q_orca_kb import mcp_server
from q_orca_kb.indexers import mempalace_indexer as mpi


# ---------------------------------------------------------------------------
# infer_source_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_file,expected",
    [
        # New-form arXiv ids
        ("1411.4028", "arxiv"),
        ("1411.4028v2", "arxiv"),
        ("/tmp/pdfs/2101.02109.pdf", "arxiv"),  # arXiv id takes precedence over .pdf
        ("2109.06493v3.pdf", "arxiv"),
        # Old-form arXiv ids
        ("quant-ph/9508027", "arxiv"),
        # Bare PDFs (no arXiv id shape)
        ("nielsen-chuang.pdf", "pdf"),
        ("/books/quantum-computation-and-quantum-information-nielsen-chuang.pdf", "pdf"),
        # URLs + URL-with-fragment
        ("https://docs.quantum.ibm.com/guides/get-started", "web"),
        ("https://docs.quantum.ibm.com/guides/get-started#setup", "web"),
        ("http://example.com/page", "web"),
        # Fallback
        ("", "unknown"),
        ("random-thing-no-extension", "unknown"),
    ],
)
def test_infer_source_type(source_file, expected):
    assert mpi.infer_source_type(source_file) == expected


# ---------------------------------------------------------------------------
# list_sources aggregation
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self, metadatas):
        self._metadatas = metadatas

    def get(self, include=None):
        return {"metadatas": self._metadatas}


def _install_fake_palace(monkeypatch, metadatas):
    """Patch the palace so list_sources sees our fake collection."""
    fake = _FakeCollection(metadatas)
    monkeypatch.setattr(mpi.palace, "get_collection", lambda _p: fake)
    # list_sources returns [] when the path doesn't exist; lie and say it does.
    monkeypatch.setattr(mpi.os.path, "exists", lambda _p: True)


def test_list_sources_groups_and_counts(monkeypatch):
    metas = [
        # arXiv paper, 2 drawers
        {"wing": "q-orca-physics", "room": "vqe",
         "source_file": "/pdfs/1411.4028.pdf", "chunk_index": 0,
         "source_type": "arxiv", "indexed_at": "2026-03-12T14:22:07Z"},
        {"wing": "q-orca-physics", "room": "vqe",
         "source_file": "/pdfs/1411.4028.pdf", "chunk_index": 1,
         "source_type": "arxiv", "indexed_at": "2026-03-12T14:22:10Z"},
    ]
    # Textbook PDF, 3 drawers at different times (test first/last rollup)
    for i in range(3):
        metas.append({
            "wing": "q-orca-physics", "room": "textbook",
            "source_file": "nielsen-chuang.pdf", "chunk_index": i,
            "source_type": "pdf",
            "indexed_at": f"2026-02-0{i+1}T10:00:00Z",
        })
    # Web page, 1 drawer — no source_type stored (pre-existing)
    metas.append({
        "wing": "q-orca-implementations", "room": "ibm-quantum",
        "source_file": "https://docs.quantum.ibm.com/guides#intro",
        "chunk_index": 0,
    })
    _install_fake_palace(monkeypatch, metas)

    sources = mpi.list_sources(palace_path="/fake")
    assert len(sources) == 3

    by_source = {s["source_file"]: s for s in sources}

    arxiv = by_source["/pdfs/1411.4028.pdf"]
    assert arxiv["drawer_count"] == 2
    assert arxiv["source_type"] == "arxiv"
    assert arxiv["first_indexed_at"] == "2026-03-12T14:22:07Z"
    assert arxiv["last_indexed_at"] == "2026-03-12T14:22:10Z"

    textbook = by_source["nielsen-chuang.pdf"]
    assert textbook["drawer_count"] == 3
    assert textbook["source_type"] == "pdf"
    assert textbook["first_indexed_at"] == "2026-02-01T10:00:00Z"
    assert textbook["last_indexed_at"] == "2026-02-03T10:00:00Z"

    # Pre-existing drawer: no timestamps, inferred source_type
    web = by_source["https://docs.quantum.ibm.com/guides#intro"]
    assert web["drawer_count"] == 1
    assert web["source_type"] == "web"
    assert web["first_indexed_at"] is None
    assert web["last_indexed_at"] is None


def test_list_sources_filters(monkeypatch):
    metas = [
        {"wing": "q-orca-physics", "room": "vqe",
         "source_file": "/pdfs/1411.4028.pdf", "chunk_index": 0,
         "source_type": "arxiv", "indexed_at": "2026-03-12T14:22:07Z"},
        {"wing": "q-orca-physics", "room": "textbook",
         "source_file": "nielsen-chuang.pdf", "chunk_index": 0,
         "source_type": "pdf", "indexed_at": "2026-02-01T10:00:00Z"},
        {"wing": "q-orca-implementations", "room": "ibm-quantum",
         "source_file": "https://docs.quantum.ibm.com/x", "chunk_index": 0,
         "source_type": "web", "indexed_at": "2026-03-01T10:00:00Z"},
    ]
    _install_fake_palace(monkeypatch, metas)

    by_wing = mpi.list_sources("/fake", wing="q-orca-physics")
    assert {s["room"] for s in by_wing} == {"vqe", "textbook"}

    by_room = mpi.list_sources("/fake", room="textbook")
    assert len(by_room) == 1
    assert by_room[0]["source_file"] == "nielsen-chuang.pdf"

    by_type = mpi.list_sources("/fake", source_type="web")
    assert len(by_type) == 1
    assert by_type[0]["source_file"] == "https://docs.quantum.ibm.com/x"

    # Compose: wing + room together
    composed = mpi.list_sources("/fake", wing="q-orca-physics", room="vqe")
    assert len(composed) == 1
    assert composed[0]["room"] == "vqe"


def test_list_sources_source_type_filter_works_on_inferred(monkeypatch):
    """Pre-existing drawers (no stored source_type) are still filterable."""
    metas = [
        {"wing": "w", "room": "r",
         "source_file": "https://x.example/page", "chunk_index": 0},
        {"wing": "w", "room": "r",
         "source_file": "book.pdf", "chunk_index": 0},
    ]
    _install_fake_palace(monkeypatch, metas)

    web = mpi.list_sources("/fake", source_type="web")
    assert [s["source_file"] for s in web] == ["https://x.example/page"]

    pdf = mpi.list_sources("/fake", source_type="pdf")
    assert [s["source_file"] for s in pdf] == ["book.pdf"]


def test_list_sources_sorted(monkeypatch):
    metas = [
        {"wing": "b-wing", "room": "r", "source_file": "c", "chunk_index": 0},
        {"wing": "a-wing", "room": "r", "source_file": "z", "chunk_index": 0},
        {"wing": "a-wing", "room": "r", "source_file": "a", "chunk_index": 0},
    ]
    _install_fake_palace(monkeypatch, metas)
    out = mpi.list_sources("/fake")
    assert [(s["wing"], s["source_file"]) for s in out] == [
        ("a-wing", "a"),
        ("a-wing", "z"),
        ("b-wing", "c"),
    ]


def test_list_sources_missing_palace_returns_empty(monkeypatch):
    monkeypatch.setattr(mpi.os.path, "exists", lambda _p: False)
    assert mpi.list_sources("/nonexistent") == []


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


def _call(name, arguments):
    return asyncio.run(mcp_server.call_tool(name, arguments))


def test_list_sources_mcp_dispatch(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "mp_list_sources",
        lambda palace_path, wing=None, room=None, source_type=None: [
            {"wing": "w", "room": "r", "source_file": "s",
             "source_type": "arxiv", "drawer_count": 7,
             "first_indexed_at": None, "last_indexed_at": None},
        ],
    )
    out = _call("list_sources", {"wing": "w", "source_type": "arxiv"})
    assert out["count"] == 1
    assert out["sources"][0]["drawer_count"] == 7


def test_kb_status_includes_source_count(monkeypatch):
    monkeypatch.setattr(mcp_server, "_palace_drawer_count", lambda _p: 42)
    monkeypatch.setattr(
        mcp_server,
        "mp_list_sources",
        lambda palace_path, wing=None, room=None, source_type=None: [
            {"source_file": "a"}, {"source_file": "b"}, {"source_file": "c"},
        ],
    )
    out = _call("kb_status", {})
    assert out["drawer_count"] == 42
    assert out["source_count"] == 3
    # seed_count should still be present (not removed)
    assert "seed_count" in out


def test_list_sources_in_tools_list():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert "list_sources" in names


# ---------------------------------------------------------------------------
# index_paper stamps indexed_at + source_type
# ---------------------------------------------------------------------------


class _UpsertCapture:
    def __init__(self):
        self.calls = []

    def upsert(self, documents, ids, metadatas):
        self.calls.append({"documents": documents, "ids": ids, "metadatas": metadatas})


def test_index_paper_stamps_indexed_at_and_source_type(monkeypatch):
    capture = _UpsertCapture()
    monkeypatch.setattr(mpi.palace, "get_collection", lambda _p: capture)

    result = mpi.index_paper(
        palace_path="/fake",
        wing="w",
        room="r",
        arxiv_id="1411.4028",
        source_file="/tmp/1411.4028.pdf",
        text="a" * 900,
        source_type="arxiv",
    )
    assert result.indexed_count >= 1
    assert capture.calls, "expected at least one upsert call"
    meta = capture.calls[0]["metadatas"][0]
    assert meta["source_type"] == "arxiv"
    # ISO 8601 UTC with Z suffix, to the second
    assert meta["indexed_at"].endswith("Z")
    assert "T" in meta["indexed_at"]
    assert len(meta["indexed_at"]) == 20  # YYYY-MM-DDTHH:MM:SSZ


def test_index_paper_omits_source_type_when_not_provided(monkeypatch):
    """Backwards compat: callers that don't know source_type don't set it."""
    capture = _UpsertCapture()
    monkeypatch.setattr(mpi.palace, "get_collection", lambda _p: capture)

    mpi.index_paper(
        palace_path="/fake",
        wing="w",
        room="r",
        arxiv_id="x",
        source_file="/tmp/x",
        text="b" * 900,
    )
    meta = capture.calls[0]["metadatas"][0]
    assert "source_type" not in meta
    assert "indexed_at" in meta
