"""Tests for q_orca_kb.fetchers.web_crawler.

Everything heavy (Playwright, real HTTP, robots.txt) is stubbed; we exercise
the BFS loop, URL dedup, config filtering and robots gating.
"""

from __future__ import annotations

import time
from urllib.robotparser import RobotFileParser

import pytest

from q_orca_kb.extractors.web_extractor import WebPage
from q_orca_kb.fetchers import web_crawler
from q_orca_kb.fetchers.web_crawler import (
    CrawlConfig,
    crawl,
    is_crawlable,
    matches_config,
    new_progress,
    normalize_url,
)


def _page(url: str, links: list[str]) -> WebPage:
    return WebPage(
        url=url,
        title=f"Page {url}",
        text=f"content of {url}",
        links=links,
        content_hash=f"h-{url}",
        fetched_at=time.time(),
        sections=[],
    )


def _config(**overrides) -> CrawlConfig:
    defaults = dict(
        site_key="test",
        seeds=["https://example.com/docs/"],
        allow_patterns=[r"example\.com/docs"],
        block_patterns=[r"/private"],
        wing="q-orca-implementations",
        room="test",
        max_pages=10,
        depth_limit=3,
        rate_limit_rps=1000.0,  # effectively no rate limit in tests
    )
    defaults.update(overrides)
    return CrawlConfig(**defaults)


async def _no_robots(_host: str) -> RobotFileParser | None:
    return None


def _collect(async_iter):
    """Drive an async generator to completion from a sync test."""
    import asyncio

    async def _run():
        out = []
        async for item in async_iter:
            out.append(item)
        return out

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_strips_fragment_and_trailing_slash():
    assert normalize_url("https://ex.com/a/#frag") == "https://ex.com/a"
    assert normalize_url("https://ex.com/a/b/") == "https://ex.com/a/b"
    assert normalize_url("https://ex.com/") == "https://ex.com/"


def test_is_crawlable_skips_binary_and_api():
    assert not is_crawlable("https://ex.com/file.pdf")
    assert not is_crawlable("https://ex.com/img.png")
    assert not is_crawlable("https://ex.com/api/v1/thing")
    assert not is_crawlable("mailto:a@b.c")
    assert is_crawlable("https://ex.com/guide/overview")


def test_matches_config_allow_and_block():
    cfg = _config()
    assert matches_config("https://example.com/docs/x", cfg)
    assert not matches_config("https://example.com/docs/private/x", cfg)
    assert not matches_config("https://other.com/docs/x", cfg)


# ---------------------------------------------------------------------------
# crawl() behaviour
# ---------------------------------------------------------------------------


def test_robots_disallowed_urls_are_skipped():
    cfg = _config(seeds=["https://example.com/docs/a"])
    robots_txt = "User-agent: *\nDisallow: /docs/a\n"

    async def robots_fetcher(_host):
        rp = RobotFileParser()
        rp.parse(robots_txt.splitlines())
        return rp

    async def fake_extract(_browser, _url):
        raise AssertionError("should not have been called")

    progress = new_progress("test")
    pages = _collect(
        crawl(
            browser=None,
            config=cfg,
            progress=progress,
            extractor=fake_extract,
            robots_fetcher=robots_fetcher,
        )
    )
    assert pages == []
    assert progress["fetched"] == 0


def test_block_patterns_are_not_enqueued():
    cfg = _config(
        seeds=["https://example.com/docs/start"],
        block_patterns=[r"/private"],
    )
    fetched: list[str] = []

    async def fake_extract(_browser, url):
        fetched.append(url)
        if url.endswith("/start"):
            return _page(url, [
                "https://example.com/docs/public",
                "https://example.com/docs/private/secret",
            ])
        return _page(url, [])

    progress = new_progress("test")
    _collect(
        crawl(
            browser=None,
            config=cfg,
            progress=progress,
            extractor=fake_extract,
            robots_fetcher=_no_robots,
        )
    )
    assert "https://example.com/docs/private/secret" not in fetched
    assert "https://example.com/docs/public" in fetched


def test_max_pages_limit_is_respected():
    cfg = _config(
        seeds=["https://example.com/docs/0"],
        max_pages=3,
    )

    async def fake_extract(_browser, url):
        idx = int(url.rsplit("/", 1)[-1])
        return _page(url, [f"https://example.com/docs/{idx + 1}"])

    progress = new_progress("test")
    pages = _collect(
        crawl(
            browser=None,
            config=cfg,
            progress=progress,
            extractor=fake_extract,
            robots_fetcher=_no_robots,
        )
    )
    assert len(pages) == 3
    assert progress["fetched"] == 3


def test_duplicate_urls_only_fetched_once():
    cfg = _config(seeds=["https://example.com/docs/a"])
    fetched: list[str] = []

    async def fake_extract(_browser, url):
        fetched.append(url)
        # Both pages link to /shared so we can confirm it's fetched exactly once.
        if url.endswith("/a"):
            return _page(url, [
                "https://example.com/docs/b",
                "https://example.com/docs/shared",
            ])
        if url.endswith("/b"):
            return _page(url, ["https://example.com/docs/shared/"])  # same after normalise
        return _page(url, [])

    progress = new_progress("test")
    _collect(
        crawl(
            browser=None,
            config=cfg,
            progress=progress,
            extractor=fake_extract,
            robots_fetcher=_no_robots,
        )
    )
    assert fetched.count("https://example.com/docs/shared") == 1


def test_default_robots_fetcher_is_injectable(monkeypatch):
    """If caller omits robots_fetcher, module-level fetch_robots is called."""
    calls: list[str] = []

    async def tracker(host):
        calls.append(host)
        return None

    async def fake_extract(_browser, url):
        return _page(url, [])

    monkeypatch.setattr(web_crawler, "fetch_robots", tracker)
    cfg = _config(seeds=["https://example.com/docs/a"])
    progress = new_progress("test")
    _collect(
        crawl(browser=None, config=cfg, progress=progress, extractor=fake_extract)
    )
    assert "example.com" in calls
