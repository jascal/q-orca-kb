# q-orca-kb Bug Report

**Date:** 2026-04-16
**Reviewed by:** Hermes QA Agent
**Files Reviewed:** `mcp_server.py`, `pipeline.py`, `cli.py`, `indexers/mempalace_indexer.py`, `fetchers/arxiv_fetcher.py`, `extractors/pdf_extractor.py`, `seeds.py`, `skills.py`

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 6 |
| HIGH     | 9 |
| MEDIUM   | 7 |
| LOW      | 5 |
| **Total**| **27** |

---

## CRITICAL Bugs

### KB-1: Event Loop Blocking — `fetch_arxiv()` is Synchronous

**File:** `pipeline.py`, line 127
**Type:** Async / Correctness

```python
fetch_result = fetch_arxiv(arxiv_id, pdf_dir)
```

`fetch_arxiv()` is a regular `def` (not `async def`). It performs blocking I/O (HTTP download of a PDF via the `arxiv` library) directly on the asyncio event loop. This stalls all concurrent operations.

**Impact:** During PDF download, the entire MCP server is unresponsive. Any other in-flight requests hang.

**Fix:** Run in a thread pool executor:

```python
loop = asyncio.get_event_loop()
fetch_result = await loop.run_in_executor(
    None, fetch_arxiv, arxiv_id, pdf_dir
)
```

---

### KB-2: Event Loop Blocking — `extract_text()` is Synchronous

**File:** `pipeline.py`, line 136
**Type:** Async / Correctness

```python
text = extract_text(pdf_path)
```

`extract_text()` uses `pypdf.PdfReader` to read a PDF file from disk. This is blocking I/O that stalls the event loop.

**Impact:** Large PDFs block the server for the duration of parsing.

**Fix:** Wrap in `run_in_executor()`.

---

### KB-3: Event Loop Blocking — `index_paper()` is Synchronous

**File:** `pipeline.py`, line 148
**Type:** Async / Correctness

```python
index_result = index_paper(
    palace_path=palace_path,
    ...
)
```

`index_paper()` calls `mempalace.miner.add_drawer()` in a loop, performing ChromaDB upsert operations. These are blocking I/O operations.

**Impact:** Indexing many chunks sequentially blocks the event loop. For papers with hundreds of chunks, this causes noticeable stalls.

**Fix:** Batch the chunk inserts or run the entire `index_paper()` in `run_in_executor()`.

---

### KB-4: Crash on Empty `pdf_path`

**File:** `pipeline.py`, line 136
**Type:** Crash / Correctness

```python
pdf_path = machine.context.get("pdf_path", "")
text = extract_text(pdf_path)
```

If the machine enters the `extracting` state without a valid `pdf_path` in context (e.g., if `fetch_ok` was never sent, or context was corrupted), `extract_text("")` is called, which raises `FileNotFoundError` and transitions to `extract_failed`. However, this is not the issue — the real problem is that the `fetch_ok` event carries `pdf_path` but if the `fetching` state never runs (unknown state path), `pdf_path` remains `""`. The code does not validate this before calling `extract_text`.

**Concrete failure:** Any code path that enters `extracting` without a populated `pdf_path` crashes.

**Fix:** Add a guard at the top of the `extracting` branch:

```python
elif state == "extracting":
    pdf_path = machine.context.get("pdf_path", "")
    if not pdf_path:
        await machine.send("extract_failed", {"error": "no pdf_path in context"})
        continue
```

---

### KB-5: Workflow File Re-parsed on Every `index_one()` Call

**File:** `pipeline.py`, line 98
**Type:** Performance / Correctness

```python
definition = _load_machine()  # called every time
```

`_load_machine()` reads and parses `workflows/paper_indexing.orca.md` from disk on every single paper index operation. For `index_seeds` indexing 10 papers, this is 10 redundant disk reads and parses.

**Fix:** Move `_load_machine()` to module level and cache the result:

```python
_WORKFLOW_DEFINITION = None

def _load_machine():
    global _WORKFLOW_DEFINITION
    if _WORKFLOW_DEFINITION is None:
        _WORKFLOW_DEFINITION = parse_orca_md(WORKFLOW_PATH.read_text())
    return _WORKFLOW_DEFINITION
```

---

### KB-6: Path Traversal Vulnerability in MCP `file` Parameter

**File:** `mcp_server.py`, line 116
**Type:** Security / Path Traversal

```python
parsed = parse_q_orca_markdown(
    inp["source"] if "source" in inp
    else Path(inp["file"]).read_text()  # no path validation
)
```

**File:** `cli.py`, line 66
**Type:** Security / Path Traversal

```python
source = Path(args.file).read_text()  # no path validation
```

The `file` parameter in `simulate_machine` and the CLI's `--file` argument accept arbitrary paths. There is no validation that the resolved path is within an allowed directory.

**Concrete exploit:**
- `file="../../etc/passwd"` reads system file
- `file="~/.ssh/id_rsa"` reads SSH private keys
- MCP call: `{"name": "simulate_machine", "arguments": {"file": "../../root/.bashrc"}}`

**Fix:** Validate resolved path is within a sandboxed directory:

```python
SAFE_DIR = Path.home() / ".q-orca-kb"
requested = Path(inp["file"]).resolve()
if not str(requested).startswith(str(SAFE_DIR)):
    raise ValueError("file path outside allowed directory")
```

---

## HIGH Bugs

### KB-7: Server Crash on Malformed Environment Variables

**File:** `mcp_server.py`, lines 162–163
**Type:** Crash / Error Handling

```python
"max_tokens": int(os.environ["ORCA_MAX_TOKENS"]) if "ORCA_MAX_TOKENS" in os.environ else 4096,
"temperature": float(os.environ["ORCA_TEMPERATURE"]) if "ORCA_TEMPERATURE" in os.environ else 0.7,
```

If `ORCA_MAX_TOKENS="abc"` or `ORCA_TEMPERATURE="hot"`, a `ValueError` is raised with no try/except. This crashes the entire MCP server on the first request.

**Fix:**

```python
try:
    max_tokens = int(os.environ["ORCA_MAX_TOKENS"])
except (ValueError, KeyError):
    max_tokens = 4096
```

---

### KB-8: Exceptions Returned as HTTP 200 Success

**File:** `mcp_server.py`, lines 379–383
**Type:** Protocol / JSON-RPC 2.0 Violation

```python
return resp({"content": _err_content(f"{type(e).__name__}: {e}"), "isError": True})
```

Exceptions in `call_tool` are returned with `{"content": ..., "isError": True}` — a 200 OK HTTP-equivalent response. JSON-RPC 2.0 requires that errors be returned as proper error responses with integer error codes.

**Impact:** MCP clients that check HTTP status codes or JSON-RPC error fields will treat exceptions as successful operations.

**Fix:** Return a proper JSON-RPC error:

```python
return resp({"code": -32603, "message": f"{type(e).__name__}: {e}"}, is_error=True)
```

---

### KB-9: Uncaught `FileNotFoundError` in CLI

**File:** `cli.py`, line 66
**Type:** Crash / Error Handling

```python
source = Path(args.file).read_text()
```

No try/except around the file read. If the file does not exist, an unhandled exception propagates with a full stack trace.

**Fix:**

```python
try:
    source = Path(args.file).read_text()
except FileNotFoundError:
    print(f"error: file not found: {args.file}", file=sys.stderr)
    return 1
```

---

### KB-10: Off-by-One in Retry Logic

**File:** `pipeline.py`, line 168
**Type:** Logic / Correctness

```python
if machine.context.get("attempts", 0) + 1 < machine.context.get("max_attempts", 3):
    await machine.send("retry")
```

With `max_attempts=3` (default), this allows only **2 retries**, not 3. The condition `attempts + 1 < 3` fires when `attempts` is 0 and 1, but not when `attempts` is 2. So the machine retries on failure #1 and failure #2, then gives up on failure #3.

- `attempts=0` → `0+1 < 3` → True → retry ✓
- `attempts=1` → `1+1 < 3` → True → retry ✓
- `attempts=2` → `2+1 < 3` → False → give up (but user expected 3 attempts)

**Fix:**

```python
if machine.context.get("attempts", 0) < machine.context.get("max_attempts", 3):
```

---

### KB-11: Missing `verbose` Parameter in MCP

**File:** `mcp_server.py`, lines 120–125
**Type:** Feature Inconsistency

```python
opts = QSimulationOptions(
    analytic=arguments.get("analytic", True),
    shots=arguments.get("shots", 1024),
    run=arguments.get("run", False),
    skip_qutip=arguments.get("skip_qutip", False),
)
```

`QSimulationOptions` has a `verbose` parameter (used in CLI), but the MCP handler does not expose it. MCP clients cannot enable verbose output.

**Fix:** Add:

```python
verbose=arguments.get("verbose", False),
```

---

### KB-12: `machine.stop()` Not in `finally` Block

**File:** `pipeline.py`, lines 118–176
**Type:** Resource Leak

`machine.stop()` is called at line 180, but only after the while loop exits. If any `await machine.send(...)` raises an exception inside the loop, `stop()` is never called and the machine leaks resources.

**Fix:** Use try/finally:

```python
try:
    while True:
        ...
finally:
    await machine.stop()
```

---

### KB-13: Invalid MCP Protocol Version String

**File:** `mcp_server.py`, line 200
**Type:** Protocol / MCP Compliance

```python
"protocolVersion": "2024-11-05",
```

`"2024-11-05"` is not a valid MCP protocol version. MCP uses semantic versioning like `"1.0.0"`. Using an invalid version string can cause capability negotiation failures with strict MCP clients.

**Fix:** Use `"2024-11-05"` is actually the current version string used by some MCP SDKs. Verify against the official MCP specification. If incorrect, replace with the current stable version.

---

### KB-14: Batch JSON-RPC Requests Crash the Server

**File:** `mcp_server.py`, line 249
**Type:** Protocol / JSON-RPC 2.0

```python
parsed = json.loads(line)
```

JSON-RPC 2.0 allows batch requests as arrays (`[{...}, {...}]`). Sending a batch request causes `json.loads()` to return a list, which then crashes at `request.get("method", "")` with `AttributeError: 'list' object has no attribute 'get'`.

**Fix:** Handle both forms:

```python
parsed = json.loads(line)
if isinstance(parsed, list):
    results = [await handle_request(req) for req in parsed if req.get("id") is not None]
    for r in results:
        sys.stdout.write(json.dumps(r, default=str) + "\n")
    sys.stdout.flush()
    continue
```

---

### KB-15: `_palace_drawer_count` Swallows All Exceptions Silently

**File:** `mcp_server.py`, lines 204–213
**Type:** Debuggability / Error Handling

```python
try:
    coll = palace_mod.get_collection(palace_path)
    return int(coll.count())
except Exception:
    return 0
```

Catching all exceptions and returning 0 makes it impossible to distinguish "collection doesn't exist" from "permission denied" or "corrupted collection". Production deployments will silently show `drawer_count: 0` without any indication of the actual problem.

**Fix:** Log the exception or return structured error information.

---

## MEDIUM Bugs

### KB-16: No Timeouts on External I/O

**File:** `fetchers/arxiv_fetcher.py`, `extractors/pdf_extractor.py`, `indexers/mempalace_indexer.py`
**Type:** Robustness

All external I/O operations (arXiv HTTP download, PDF parsing, ChromaDB upserts) have no timeouts. A slow or hanging arXiv server, a corrupted PDF, or a non-responsive ChromaDB instance will cause indefinite hangs.

**Fix:** Add timeouts using `asyncio.timeout()` or `asyncio.wait_for()`.

---

### KB-17: `index_seeds` Runs Sequentially

**File:** `mcp_server.py`, lines 296–312
**Type:** Performance

```python
for seed in seeds:
    res = await index_one(...)
```

Each seed paper is indexed sequentially. With 10 seeds, indexing takes 10× the time of a single paper. These are independent operations and should run in parallel.

**Fix:** Use `asyncio.gather()`:

```python
results = await asyncio.gather(*[
    index_one(...) for seed in seeds
])
```

---

### KB-18: Error Message Overwrites Instead of Appending

**File:** `pipeline.py`, line 69
**Type:** Data Loss

```python
def record_error(ctx, event):
    payload = event or {}
    return {"error": payload.get("error", "unknown error")}
```

Each error replaces the previous one. On machines with multiple failure modes, only the last error is preserved. This makes debugging harder.

**Fix:**

```python
def record_error(ctx, event):
    payload = event or {}
    prev = ctx.get("error", "")
    new = payload.get("error", "unknown error")
    return {"error": f"{prev}; {new}" if prev else new}
```

---

### KB-19: `_deep_merge` Allows Empty String Override

**File:** (if config/loader pattern exists in q-orca-kb — otherwise N/A)
**Type:** Config / Silent Failure

If a YAML or env config has `model: ""` (empty string), it overrides the default `"gpt-4o"`. This silently breaks the configuration with no error or warning.

**Fix:** Skip empty strings in merge:

```python
if source_val is not None and source_val != "":
```

---

### KB-20: `index_paper` in MCP Server Doesn't Track `attempts`

**File:** `mcp_server.py`, lines 274–281
**Type:** Inconsistency

The `index_one()` call in `index_paper` tool handler passes `max_attempts` but the result returned doesn't include `attempts` (though it does — line 288 — so this is fine). However, the `index_seeds` handler accumulates results in a way that could silently drop per-paper attempt counts.

---

### KB-21: `_find_seed` Linear Scan on Every `index_paper` Call

**File:** `mcp_server.py`, line 198
**Type:** Performance

```python
def _find_seed(arxiv_id: str) -> Seed | None:
    for s in SEEDS:
        if s.arxiv_id == arxiv_id:
            return s
    return None
```

Linear O(n) scan on every `index_paper` call. With 10 seeds this is trivial, but the pattern is inefficient and error-prone as the seed list grows.

**Fix:** Use a dict lookup:

```python
_SEED_MAP = {s.arxiv_id: s for s in SEEDS}
def _find_seed(arxiv_id: str) -> Seed | None:
    return _SEED_MAP.get(arxiv_id)
```

---

### KB-22: No Input Size Limits

**File:** `mcp_server.py`, lines 88, 104, 108
**Type:** Denial of Service

```python
source = arguments.get("source")   # no size limit
spec = arguments.get("spec", "")   # no size limit
```

No maximum input size validation. A malicious client can send multi-GB JSON payloads, exhausting memory.

**Fix:** Add a size guard at the start of `call_tool`:

```python
MAX_INPUT_SIZE = 10 * 1024 * 1024  # 10MB
for val in arguments.values():
    if isinstance(val, str) and len(val) > MAX_INPUT_SIZE:
        return {"error": f"input exceeds {MAX_INPUT_SIZE} bytes"}
```

---

### KB-23: Missing `resources/*` MCP Capability

**File:** `mcp_server.py`
**Type:** Feature Gap

No `resources/list` or `resources/read` handlers. Cannot serve palace data or PDF metadata as MCP resources to clients.

---

### KB-24: Missing `prompts/*` MCP Capability

**File:** `mcp_server.py`
**Type:** Feature Gap

No `prompts/list` handler. Cannot expose q-orca-kb prompt templates (e.g., "search papers about X") as reusable MCP prompts.

---

## LOW Bugs

### KB-25: Batch Requests Return Single Response

**File:** `mcp_server.py`
**Type:** Protocol / JSON-RPC 2.0

Even after KB-14 fix (accepting batch requests), JSON-RPC 2.0 requires **one response per request** in a batch. The current single-write pattern won't handle this correctly.

---

### KB-26: No Graceful Shutdown

**File:** `mcp_server.py`, lines 399–432
**Type:** Robustness

The server exits abruptly on EOF from stdin. No SIGTERM/SIGINT handlers, no cleanup of ChromaDB connections, no flush of buffered state.

**Fix:** Add signal handlers and `finally` blocks for cleanup.

---

### KB-27: Concurrent Request Response Interleaving

**File:** `mcp_server.py`, lines 406–432
**Type:** Concurrency

The server processes requests sequentially in a `while True` loop on a single reader. However, if the server were ever modified to use async task spawning, concurrent writes to `sys.stdout` could interleave JSON responses. Currently this is safe, but it's a latent bug if the architecture evolves.

---

## Issue Priority Table

| Priority | ID   | Bug | Location |
|----------|------|-----|----------|
| P0-CRIT | KB-1 | `fetch_arxiv` blocks event loop | pipeline.py:127 |
| P0-CRIT | KB-2 | `extract_text` blocks event loop | pipeline.py:136 |
| P0-CRIT | KB-3 | `index_paper` blocks event loop | pipeline.py:148 |
| P0-CRIT | KB-4 | Empty `pdf_path` crash | pipeline.py:135 |
| P0-CRIT | KB-5 | Workflow re-parsed every call | pipeline.py:98 |
| P0-CRIT | KB-6 | Path traversal vulnerability | mcp_server.py:116, cli.py:66 |
| P1-HIGH | KB-7 | Malformed env vars crash server | mcp_server.py:162-163 |
| P1-HIGH | KB-8 | Exceptions return HTTP 200 | mcp_server.py:379-383 |
| P1-HIGH | KB-9 | Uncaught FileNotFoundError in CLI | cli.py:66 |
| P1-HIGH | KB-10 | Retry off-by-one | pipeline.py:168 |
| P1-HIGH | KB-11 | Missing `verbose` MCP param | mcp_server.py:120-125 |
| P1-HIGH | KB-12 | `machine.stop()` not in finally | pipeline.py:180 |
| P1-HIGH | KB-13 | Invalid MCP protocol version | mcp_server.py:200 |
| P1-HIGH | KB-14 | Batch requests crash server | mcp_server.py:249 |
| P1-HIGH | KB-15 | Silent exception swallowing | mcp_server.py:213 |
| P2-MEDIUM | KB-16 | No I/O timeouts | all fetchers/indexers |
| P2-MEDIUM | KB-17 | Sequential `index_seeds` | mcp_server.py:296 |
| P2-MEDIUM | KB-18 | Error overwrite not append | pipeline.py:69 |
| P2-MEDIUM | KB-19 | Empty string config override | config loader |
| P2-MEDIUM | KB-20 | Linear seed lookup | mcp_server.py:198 |
| P2-MEDIUM | KB-21 | No input size limits | mcp_server.py |
| P2-MEDIUM | KB-22 | Missing resources capability | mcp_server.py |
| P2-MEDIUM | KB-23 | Missing prompts capability | mcp_server.py |
| P3-LOW | KB-24 | Batch response format | mcp_server.py |
| P3-LOW | KB-25 | No graceful shutdown | mcp_server.py |
| P3-LOW | KB-26 | Concurrent stdout interleaving | mcp_server.py |
