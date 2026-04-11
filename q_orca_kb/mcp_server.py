"""q-orca-kb MCP server.

Exposes the q-orca-kb knowledge-base operations (fetch+index arXiv papers,
semantic search) over the Model Context Protocol via stdio. Uses no external
MCP SDK — speaks JSON-RPC 2.0 directly, the same way q-orca/mcp_server.py does.

Usage:
    python -m q_orca_kb.mcp_server
    # or via installed console_script:
    q-orca-kb-mcp

Environment variables:
    Q_ORCA_KB_PALACE   override palace path (default: <repo>/data/palace)
    Q_ORCA_KB_PDF_DIR  override pdf cache dir (default: <repo>/data/pdfs)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .indexers.mempalace_indexer import search as mp_search
from .pipeline import index_one
from .seeds import SEEDS, Seed

# --- silence library noise on stderr so it doesn't pollute MCP logs ---
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
for noisy in ("arxiv", "httpx", "chromadb", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

DEFAULT_PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PALACE = os.environ.get(
    "Q_ORCA_KB_PALACE", str(DEFAULT_PROJECT_ROOT / "data" / "palace")
)
DEFAULT_PDF_DIR = os.environ.get(
    "Q_ORCA_KB_PDF_DIR", str(DEFAULT_PROJECT_ROOT / "data" / "pdfs")
)

MCP_INSTRUCTIONS = """q-orca-kb MCP server — quantum-computing paper knowledge base.

Built on the Orca PaperIndexing state machine + mempalace vector store. Use this
to ground q-orca / orca-lang work in real arXiv literature on quantum
computation, quantum error correction, VQE/QAOA, surface codes, OpenQASM, etc.

## Workflow

1. `kb_status` — see how many drawers are indexed and where the palace lives.
2. `list_seeds` — see the curated arXiv papers shipped with q-orca-kb.
3. `index_seeds` — bulk-index all (or the first N) seed papers; idempotent.
4. `index_paper { arxiv_id, wing, room }` — index any arbitrary arXiv paper.
5. `search_papers { query, wing?, room?, n? }` — semantic search.

## Wings & rooms (the palace topology)

- `q-orca-physics`         rooms: oracle-algorithms, error-correction, vqe
- `q-orca-implementations` rooms: error-correction, hardware, circuits, formal-methods

Filter searches by wing/room when you know which sub-area you care about; leave
them empty for cross-cutting queries. Search results include similarity scores
(higher = closer match) and the source PDF filename.
"""


# ---------------------------------------------------------------------------
# Tool definitions (MCP "tools/list" payload)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "kb_status",
        "description": (
            "Show palace stats: location, drawer count, seed paper count."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_seeds",
        "description": (
            "List the curated arXiv papers that ship with q-orca-kb, including "
            "their wing/room assignments."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_papers",
        "description": (
            "Semantic search over the indexed papers. Returns the top N "
            "matching chunks with similarity scores, wing/room metadata, and "
            "source PDF filename. Optionally filter by wing and/or room."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "Filter to one wing (e.g. q-orca-physics, "
                        "q-orca-implementations)."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "Filter to one room (e.g. vqe, error-correction, "
                        "oracle-algorithms, hardware, circuits, formal-methods)."
                    ),
                },
                "n": {
                    "type": "integer",
                    "description": "Number of results to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "index_paper",
        "description": (
            "Fetch a paper from arXiv and index it into the palace via the "
            "PaperIndexing Orca state machine. Idempotent — re-running on the "
            "same id will re-upsert the same drawers. Returns the final state "
            "machine state ('done' or 'aborted'), chunk count, and any error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_id": {
                    "type": "string",
                    "description": (
                        "arXiv id, e.g. '1411.4028' or 'quant-ph/9508027'."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "Wing to file the paper under. If the id is in the "
                        "seed list this can be omitted (the seed wing wins)."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "Room within the wing. Omittable for seed papers."
                    ),
                },
                "max_attempts": {
                    "type": "integer",
                    "description": "Retry budget (default 3).",
                    "default": 3,
                },
            },
            "required": ["arxiv_id"],
        },
    },
    {
        "name": "index_seeds",
        "description": (
            "Bulk-fetch and index the curated seed papers. Idempotent. "
            "Pass `limit` to index only the first N seeds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "If > 0, only index the first N seeds.",
                    "default": 0,
                }
            },
            "required": [],
        },
    },
    {
        "name": "server_status",
        "description": "Server version and configuration info.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _find_seed(arxiv_id: str) -> Seed | None:
    for s in SEEDS:
        if s.arxiv_id == arxiv_id:
            return s
    return None


def _palace_drawer_count(palace_path: str) -> int:
    if not Path(palace_path).exists():
        return 0
    try:
        from mempalace import palace as palace_mod

        coll = palace_mod.get_collection(palace_path)
        return int(coll.count())
    except Exception:
        return 0


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "kb_status":
        return {
            "palace_path": DEFAULT_PALACE,
            "pdf_dir": DEFAULT_PDF_DIR,
            "drawer_count": _palace_drawer_count(DEFAULT_PALACE),
            "seed_count": len(SEEDS),
            "version": __version__,
        }

    if name == "list_seeds":
        return {
            "count": len(SEEDS),
            "seeds": [
                {
                    "arxiv_id": s.arxiv_id,
                    "wing": s.wing,
                    "room": s.room,
                    "title": s.title,
                }
                for s in SEEDS
            ],
        }

    if name == "search_papers":
        query = arguments.get("query", "").strip()
        if not query:
            return {"error": "query is required"}
        response = mp_search(
            palace_path=DEFAULT_PALACE,
            query=query,
            wing=arguments.get("wing"),
            room=arguments.get("room"),
            n_results=int(arguments.get("n", 5)),
        )
        if "error" in response:
            return response
        return {
            "query": response.get("query"),
            "filters": response.get("filters"),
            "result_count": len(response.get("results", [])),
            "results": response.get("results", []),
        }

    if name == "index_paper":
        arxiv_id = arguments.get("arxiv_id", "").strip()
        if not arxiv_id:
            return {"error": "arxiv_id is required"}
        seed = _find_seed(arxiv_id)
        wing = arguments.get("wing") or (seed.wing if seed else None)
        room = arguments.get("room") or (seed.room if seed else None)
        if not wing or not room:
            return {
                "error": (
                    f"{arxiv_id} is not in the seed list; wing and room are "
                    "required for non-seed papers."
                )
            }
        result = await index_one(
            arxiv_id=arxiv_id,
            wing=wing,
            room=room,
            palace_path=DEFAULT_PALACE,
            pdf_dir=DEFAULT_PDF_DIR,
            max_attempts=int(arguments.get("max_attempts", 3)),
        )
        return {
            "arxiv_id": result.arxiv_id,
            "wing": result.wing,
            "final_state": result.final_state,
            "chunk_count": result.chunk_count,
            "indexed_count": result.indexed_count,
            "attempts": result.attempts,
            "error": result.error,
        }

    if name == "index_seeds":
        limit = int(arguments.get("limit", 0))
        seeds = SEEDS[:limit] if limit > 0 else SEEDS
        results = []
        for seed in seeds:
            res = await index_one(
                arxiv_id=seed.arxiv_id,
                wing=seed.wing,
                room=seed.room,
                palace_path=DEFAULT_PALACE,
                pdf_dir=DEFAULT_PDF_DIR,
            )
            results.append(
                {
                    "arxiv_id": res.arxiv_id,
                    "wing": res.wing,
                    "final_state": res.final_state,
                    "indexed_count": res.indexed_count,
                    "error": res.error,
                }
            )
        return {
            "indexed": sum(1 for r in results if r["final_state"] == "done"),
            "total": len(results),
            "results": results,
        }

    if name == "server_status":
        return {
            "name": "q-orca-kb",
            "version": __version__,
            "python_version": sys.version,
            "palace_path": DEFAULT_PALACE,
            "pdf_dir": DEFAULT_PDF_DIR,
            "drawer_count": _palace_drawer_count(DEFAULT_PALACE),
            "seed_count": len(SEEDS),
        }

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _content(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]


def _err_content(msg: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": msg}]


async def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method", "")
    params = request.get("params", {}) or {}
    req_id = request.get("id")

    def resp(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
        r: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if is_error:
            r["error"] = payload
        else:
            r["result"] = payload
        return r

    try:
        if method == "initialize":
            return resp(
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "q-orca-kb", "version": __version__},
                    "instructions": MCP_INSTRUCTIONS,
                }
            )

        if method == "tools/list":
            return resp({"tools": TOOLS})

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            try:
                result = await call_tool(tool_name, arguments)
                return resp({"content": _content(result), "isError": False})
            except Exception as e:
                logging.exception("tool %s failed", tool_name)
                return resp(
                    {"content": _err_content(f"{type(e).__name__}: {e}"), "isError": True}
                )

        if method == "ping":
            return resp({"pong": True})

        # Notifications carry no id and don't need a response
        if req_id is None:
            return None
        return resp({"code": -32601, "message": f"Method not found: {method}"}, is_error=True)

    except Exception as e:
        if req_id is None:
            return None
        return resp({"code": -32603, "message": str(e)}, is_error=True)


async def main() -> None:
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )

    while True:
        line = await reader.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Invalid JSON"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue

        result = await handle_request(parsed)
        if result is not None:
            sys.stdout.write(json.dumps(result, default=str) + "\n")
            sys.stdout.flush()


def run() -> None:
    """Synchronous entry point for console_scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
