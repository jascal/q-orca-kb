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

Long-running operations (index_paper, index_seeds, batch_index, index_local_pdf)
run as background tasks and return a job_id immediately. Poll job_status or
list_jobs to track progress. Jobs are persisted to data/jobs.json and survive
server restarts; jobs older than 7 days are pruned on startup.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .extractors.pdf_extractor import extract_text
from .indexers.mempalace_indexer import index_paper as mp_index_paper
from .indexers.mempalace_indexer import search as mp_search
from .pipeline import index_one
from .seeds import SEEDS, Seed

# --- silence library noise on stderr so it doesn't pollute MCP logs ---
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger(__name__)
for noisy in ("arxiv", "httpx", "chromadb", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

DEFAULT_PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PALACE = os.environ.get(
    "Q_ORCA_KB_PALACE", str(DEFAULT_PROJECT_ROOT / "data" / "palace")
)
DEFAULT_PDF_DIR = os.environ.get(
    "Q_ORCA_KB_PDF_DIR", str(DEFAULT_PROJECT_ROOT / "data" / "pdfs")
)
JOBS_PATH = DEFAULT_PROJECT_ROOT / "data" / "jobs.json"
JOB_TTL_DAYS = 7


# ---------------------------------------------------------------------------
# Persistent job store
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}


def _load_jobs() -> None:
    """Load jobs from disk on startup; mark stale running jobs as interrupted."""
    global _JOBS
    if not JOBS_PATH.exists():
        return
    try:
        raw = json.loads(JOBS_PATH.read_text())
        cutoff = time.time() - JOB_TTL_DAYS * 86400
        kept = {}
        for jid, job in raw.items():
            if job.get("started_at", 0) < cutoff:
                continue  # expired
            if job.get("state") == "running":
                job["state"] = "interrupted"
                job["error"] = "Server restarted while job was running"
            kept[jid] = job
        _JOBS = kept
    except Exception:
        log.warning("Could not load jobs from %s", JOBS_PATH, exc_info=True)


def _save_jobs() -> None:
    """Persist current job store to disk."""
    try:
        JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        JOBS_PATH.write_text(json.dumps(_JOBS, indent=2, default=str))
    except Exception:
        log.warning("Could not save jobs to %s", JOBS_PATH, exc_info=True)


def _new_job(tool: str, label: str, args: dict[str, Any]) -> dict[str, Any]:
    """Create a new job record, persist it, and return it."""
    job_id = f"{tool}-{int(time.time())}-{label[:24]}"
    job: dict[str, Any] = {
        "job_id": job_id,
        "tool": tool,
        "label": label,
        "state": "running",
        "started_at": time.time(),
        "finished_at": None,
        "elapsed": None,
        "args": args,
        "result": None,
        "error": None,
    }
    _JOBS[job_id] = job
    _save_jobs()
    return job


def _finish_job(job: dict[str, Any], result: Any = None, error: str | None = None) -> None:
    """Mark a job done or errored, persist."""
    now = time.time()
    job["state"] = "error" if error else "done"
    job["finished_at"] = now
    job["elapsed"] = round(now - job["started_at"], 1)
    job["result"] = result
    job["error"] = error
    _save_jobs()


MCP_INSTRUCTIONS = """q-orca-kb MCP server — quantum-computing paper knowledge base.

Built on the Orca PaperIndexing state machine + mempalace vector store. Use this
to ground q-orca / orca-lang work in real arXiv literature on quantum
computation, quantum error correction, VQE/QAOA, surface codes, OpenQASM, etc.

## Workflow

1. `kb_status`   — palace stats: drawer count, location.
2. `list_seeds`  — curated arXiv papers shipped with q-orca-kb.
3. `search_papers { query, wing?, room?, n? }` — semantic search.

### Indexing (all async — return job_id immediately)

4. `index_paper { arxiv_id, wing, room }` — fetch + index one arXiv paper.
5. `batch_index { arxiv_ids, wing, room }` — fetch + index a list of papers.
6. `index_seeds { limit? }` — bulk-index all curated seed papers.
7. `index_local_pdf { filename, wing, room }` — index a PDF already in pdf_dir.

### Job tracking

8. `job_status { job_id }` — check state (running/done/error) + result.
9. `list_jobs { state?, limit? }` — list recent jobs.

## Wings & rooms (the palace topology)

- `q-orca-physics`         rooms: oracle-algorithms, error-correction, vqe, textbook
- `q-orca-implementations` rooms: error-correction, hardware, circuits, formal-methods,
                                  noise-models, cryptography, qram

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
        "description": "Show palace stats: location, drawer count, seed paper count.",
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
                "query": {"type": "string", "description": "Natural-language search query."},
                "wing": {
                    "type": "string",
                    "description": "Filter to one wing (e.g. q-orca-physics, q-orca-implementations).",
                },
                "room": {
                    "type": "string",
                    "description": "Filter to one room (e.g. vqe, error-correction, qram).",
                },
                "n": {"type": "integer", "description": "Number of results (default 5).", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "index_paper",
        "description": (
            "Fetch one arXiv paper and index it into the palace. Returns a job_id "
            "immediately — poll job_status to track completion. Idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "description": "arXiv id, e.g. '1411.4028' or 'quant-ph/9508027'."},
                "wing": {"type": "string", "description": "Wing to file under (omittable for seed papers)."},
                "room": {"type": "string", "description": "Room within the wing (omittable for seed papers)."},
                "max_attempts": {"type": "integer", "description": "Retry budget (default 3).", "default": 3},
            },
            "required": ["arxiv_id"],
        },
    },
    {
        "name": "batch_index",
        "description": (
            "Fetch and index a list of arXiv papers in one call. Returns a job_id "
            "immediately. Poll job_status to see per-paper progress and final counts. "
            "Use this instead of calling index_paper N times."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of arXiv ids to index.",
                },
                "wing": {"type": "string", "description": "Wing to file all papers under."},
                "room": {"type": "string", "description": "Room to file all papers under."},
                "max_attempts": {"type": "integer", "description": "Per-paper retry budget (default 3).", "default": 3},
            },
            "required": ["arxiv_ids", "wing", "room"],
        },
    },
    {
        "name": "index_seeds",
        "description": (
            "Bulk-fetch and index the curated seed papers. Returns a job_id immediately. "
            "Pass `limit` to index only the first N seeds. Idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "If > 0, index only the first N seeds.", "default": 0},
            },
            "required": [],
        },
    },
    {
        "name": "index_local_pdf",
        "description": (
            "Index a local PDF from the pdf_dir into the palace. Use for textbooks or "
            "preprints already placed in data/pdfs/. Returns a job_id immediately. Idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Filename only (no path), e.g. 'nielsen-chuang.pdf'. Must exist in pdf_dir.",
                },
                "wing": {"type": "string", "description": "Wing to file under (e.g. q-orca-physics)."},
                "room": {"type": "string", "description": "Room within the wing (e.g. textbook)."},
                "name": {"type": "string", "description": "Display name (defaults to filename stem)."},
            },
            "required": ["filename", "wing", "room"],
        },
    },
    {
        "name": "job_status",
        "description": (
            "Check the status of any background indexing job. Returns state "
            "(running / done / error / interrupted), elapsed seconds, and full "
            "result when complete. Works for index_paper, batch_index, index_seeds, "
            "and index_local_pdf jobs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job_id returned by any indexing tool."},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "list_jobs",
        "description": (
            "List recent background jobs, newest first. Optionally filter by state. "
            "Jobs older than 7 days are automatically pruned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Filter by state: running, done, error, interrupted. Omit for all.",
                },
                "limit": {"type": "integer", "description": "Max jobs to return (default 20).", "default": 20},
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
# Background task helpers
# ---------------------------------------------------------------------------


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the job with elapsed updated if still running."""
    out = dict(job)
    if out["state"] == "running":
        out["elapsed"] = round(time.time() - out["started_at"], 1)
    return out


async def _run_index_paper(
    job: dict[str, Any],
    arxiv_id: str,
    wing: str,
    room: str,
    max_attempts: int,
) -> None:
    try:
        result = await index_one(
            arxiv_id=arxiv_id,
            wing=wing,
            room=room,
            palace_path=DEFAULT_PALACE,
            pdf_dir=DEFAULT_PDF_DIR,
            max_attempts=max_attempts,
        )
        _finish_job(job, result={
            "arxiv_id": result.arxiv_id,
            "wing": result.wing,
            "final_state": result.final_state,
            "chunk_count": result.chunk_count,
            "indexed_count": result.indexed_count,
            "attempts": result.attempts,
            "error": result.error,
        })
    except Exception as exc:
        _finish_job(job, error=f"{type(exc).__name__}: {exc}")


async def _run_batch_index(
    job: dict[str, Any],
    arxiv_ids: list[str],
    wing: str,
    room: str,
    max_attempts: int,
) -> None:
    papers: list[dict[str, Any]] = [
        {"arxiv_id": aid, "state": "queued", "indexed_count": 0, "error": ""} for aid in arxiv_ids
    ]
    job["result"] = {"papers": papers, "done": 0, "total": len(papers)}
    _save_jobs()
    for entry in papers:
        entry["state"] = "running"
        _save_jobs()
        try:
            result = await index_one(
                arxiv_id=entry["arxiv_id"],
                wing=wing,
                room=room,
                palace_path=DEFAULT_PALACE,
                pdf_dir=DEFAULT_PDF_DIR,
                max_attempts=max_attempts,
            )
            entry["state"] = result.final_state
            entry["indexed_count"] = result.indexed_count
            entry["error"] = result.error
            if result.final_state == "done":
                job["result"]["done"] += 1
        except Exception as exc:
            entry["state"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        _save_jobs()
    errors = [p for p in papers if p["state"] not in ("done",)]
    _finish_job(
        job,
        result=job["result"],
        error=(f"{len(errors)} paper(s) failed" if errors else None),
    )


async def _run_index_seeds(job: dict[str, Any], seeds: list[Seed]) -> None:
    papers: list[dict[str, Any]] = [
        {"arxiv_id": s.arxiv_id, "wing": s.wing, "room": s.room,
         "state": "queued", "indexed_count": 0, "error": ""}
        for s in seeds
    ]
    job["result"] = {"papers": papers, "done": 0, "total": len(papers)}
    _save_jobs()
    for entry, seed in zip(papers, seeds):
        entry["state"] = "running"
        _save_jobs()
        try:
            result = await index_one(
                arxiv_id=seed.arxiv_id,
                wing=seed.wing,
                room=seed.room,
                palace_path=DEFAULT_PALACE,
                pdf_dir=DEFAULT_PDF_DIR,
            )
            entry["state"] = result.final_state
            entry["indexed_count"] = result.indexed_count
            entry["error"] = result.error
            if result.final_state == "done":
                job["result"]["done"] += 1
        except Exception as exc:
            entry["state"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        _save_jobs()
    errors = [p for p in papers if p["state"] not in ("done",)]
    _finish_job(
        job,
        result=job["result"],
        error=(f"{len(errors)} paper(s) failed" if errors else None),
    )


async def _run_index_local_pdf(
    job: dict[str, Any],
    pdf_path: str,
    display_name: str,
    filename: str,
    wing: str,
    room: str,
) -> None:
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, extract_text, pdf_path)
        if not text.strip():
            raise ValueError("extracted empty text — PDF may be scanned/image-only")
        result = await loop.run_in_executor(
            None,
            functools.partial(
                mp_index_paper,
                palace_path=DEFAULT_PALACE,
                wing=wing,
                room=room,
                arxiv_id=display_name,
                source_file=filename,
                text=text,
            ),
        )
        _finish_job(job, result={
            "filename": filename,
            "name": display_name,
            "wing": wing,
            "room": room,
            "chunk_count": result.chunk_count,
            "indexed_count": result.indexed_count,
        })
    except Exception as exc:
        _finish_job(job, error=f"{type(exc).__name__}: {exc}")


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
        log.warning("could not read palace drawer count at %s", palace_path, exc_info=True)
        return 0


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:

    # --- fast synchronous tools ---

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
                {"arxiv_id": s.arxiv_id, "wing": s.wing, "room": s.room, "title": s.title}
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

    if name == "job_status":
        job_id = arguments.get("job_id", "").strip()
        if not job_id:
            return {"error": "job_id is required"}
        job = _JOBS.get(job_id)
        if job is None:
            return {"error": f"No job found with id '{job_id}'. It may have expired (>7 days)."}
        return _job_summary(job)

    if name == "list_jobs":
        state_filter = arguments.get("state")
        limit = int(arguments.get("limit", 20))
        jobs = sorted(_JOBS.values(), key=lambda j: j["started_at"], reverse=True)
        if state_filter:
            jobs = [j for j in jobs if j["state"] == state_filter]
        jobs = jobs[:limit]
        return {
            "count": len(jobs),
            "jobs": [
                {
                    "job_id": j["job_id"],
                    "tool": j["tool"],
                    "label": j["label"],
                    "state": j["state"],
                    "elapsed": round(time.time() - j["started_at"], 1) if j["state"] == "running"
                               else j.get("elapsed"),
                    "error": j.get("error"),
                }
                for j in jobs
            ],
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
            "active_jobs": sum(1 for j in _JOBS.values() if j["state"] == "running"),
        }

    # --- async background tools ---

    if name == "index_paper":
        arxiv_id = arguments.get("arxiv_id", "").strip()
        if not arxiv_id:
            return {"error": "arxiv_id is required"}
        seed = _find_seed(arxiv_id)
        wing = arguments.get("wing") or (seed.wing if seed else None)
        room = arguments.get("room") or (seed.room if seed else None)
        if not wing or not room:
            return {"error": f"{arxiv_id} is not in the seed list; wing and room are required."}
        max_attempts = int(arguments.get("max_attempts", 3))
        job = _new_job("index_paper", arxiv_id, {"arxiv_id": arxiv_id, "wing": wing, "room": room})
        asyncio.ensure_future(_run_index_paper(job, arxiv_id, wing, room, max_attempts))
        return {
            "job_id": job["job_id"],
            "state": "running",
            "message": f"Indexing {arxiv_id} in background. Poll job_status with job_id='{job['job_id']}'.",
        }

    if name == "batch_index":
        arxiv_ids = arguments.get("arxiv_ids", [])
        wing = arguments.get("wing", "").strip()
        room = arguments.get("room", "").strip()
        if not arxiv_ids:
            return {"error": "arxiv_ids is required and must be non-empty"}
        if not wing or not room:
            return {"error": "wing and room are required"}
        max_attempts = int(arguments.get("max_attempts", 3))
        label = f"{len(arxiv_ids)}-papers-{wing}/{room}"
        job = _new_job("batch_index", label, {"arxiv_ids": arxiv_ids, "wing": wing, "room": room})
        asyncio.ensure_future(_run_batch_index(job, arxiv_ids, wing, room, max_attempts))
        return {
            "job_id": job["job_id"],
            "state": "running",
            "total": len(arxiv_ids),
            "message": (
                f"Batch indexing {len(arxiv_ids)} papers in background. "
                f"Poll job_status with job_id='{job['job_id']}' to track per-paper progress."
            ),
        }

    if name == "index_seeds":
        limit = int(arguments.get("limit", 0))
        seeds = SEEDS[:limit] if limit > 0 else SEEDS
        label = f"{len(seeds)}-seeds"
        job = _new_job("index_seeds", label, {"limit": limit})
        asyncio.ensure_future(_run_index_seeds(job, list(seeds)))
        return {
            "job_id": job["job_id"],
            "state": "running",
            "total": len(seeds),
            "message": (
                f"Indexing {len(seeds)} seed papers in background. "
                f"Poll job_status with job_id='{job['job_id']}'."
            ),
        }

    if name == "index_local_pdf":
        filename = arguments.get("filename", "").strip()
        wing = arguments.get("wing", "").strip()
        room = arguments.get("room", "").strip()
        if not filename:
            return {"error": "filename is required"}
        if not wing or not room:
            return {"error": "wing and room are required"}
        pdf_path = Path(DEFAULT_PDF_DIR) / filename
        if not pdf_path.exists():
            return {"error": f"PDF not found at {pdf_path}"}
        display_name = arguments.get("name") or pdf_path.stem
        job = _new_job("index_local_pdf", filename,
                       {"filename": filename, "wing": wing, "room": room, "name": display_name})
        asyncio.ensure_future(
            _run_index_local_pdf(job, str(pdf_path), display_name, filename, wing, room)
        )
        return {
            "job_id": job["job_id"],
            "state": "running",
            "message": (
                f"Indexing {filename} in background. "
                f"Poll job_status with job_id='{job['job_id']}'."
            ),
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

        if req_id is None:
            return None
        return resp({"code": -32601, "message": f"Method not found: {method}"}, is_error=True)

    except Exception as e:
        if req_id is None:
            return None
        return resp({"code": -32603, "message": str(e)}, is_error=True)


async def main() -> None:
    _load_jobs()  # restore persisted jobs on startup

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
                json.dumps({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Invalid JSON"}}) + "\n"
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
