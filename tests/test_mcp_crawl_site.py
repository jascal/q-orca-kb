"""Tests for the crawl_site / list_crawl_sites MCP tools.

We never launch Playwright; we stub ``_run_crawl`` so the dispatch path and
error handling are what's under test.
"""

from __future__ import annotations

import asyncio

import pytest

from q_orca_kb import mcp_server


def _call(name, arguments):
    return asyncio.run(mcp_server.call_tool(name, arguments))


def test_list_crawl_sites_returns_all_keys():
    out = _call("list_crawl_sites", {})
    keys = {s["site_key"] for s in out["sites"]}
    assert keys == {
        "ibm-quantum-docs",
        "ibm-quantum-learning",
        "msft-azure-quantum",
        "nvidia-cudaqx",
        "orca-lang-wiki",
        "q-orca-lang-wiki",
    }
    for s in out["sites"]:
        assert s["wing"]
        assert s["room"]
        assert isinstance(s["max_pages"], int)
        assert s["seeds"]


def test_crawl_site_invalid_key_returns_structured_error():
    out = _call("crawl_site", {"site_key": "definitely-not-a-site"})
    assert "error" in out
    assert "definitely-not-a-site" in out["error"]
    assert "Available" in out["error"]


def test_crawl_site_requires_site_key():
    out = _call("crawl_site", {})
    assert "error" in out
    assert "site_key" in out["error"]


def test_crawl_site_valid_key_returns_job_id(monkeypatch):
    # Swap _run_crawl for a no-op so no browser starts.
    async def fake_run(job, config, force):
        from q_orca_kb.fetchers.web_crawler import new_progress

        job["result"] = {
            "progress": new_progress(config.site_key),
            "site_key": config.site_key,
        }
        mcp_server._finish_job(job, result=job["result"])

    monkeypatch.setattr(mcp_server, "_run_crawl", fake_run)

    out = _call("crawl_site", {"site_key": "ibm-quantum-docs", "max_pages": 1})
    assert "job_id" in out
    assert out["state"] == "running"
    job_id = out["job_id"]

    # Let the scheduled no-op task run.
    async def _drain():
        await asyncio.sleep(0)

    asyncio.run(_drain())

    status = _call("job_status", {"job_id": job_id})
    assert status["job_id"] == job_id
    assert status["tool"] == "crawl_site"
    assert status["args"]["site_key"] == "ibm-quantum-docs"
    assert status["args"]["max_pages"] == 1
