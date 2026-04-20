"""BFS web crawler that drives ``web_extractor`` against vendor doc sites.

Yields ``WebPage`` objects one at a time (async generator) so the caller can
index them as they arrive and persist a dedup store incrementally. The caller
owns the Playwright browser — we just reuse the instance they pass in.

Guardrails:
* ``robots.txt`` is fetched once per host and cached.
* Allow/block regex patterns filter URLs.
* URL is normalised (fragment stripped, trailing slash trimmed).
* Obvious non-HTML URLs (``.pdf``, image/video/archive/etc.) are dropped.
* Rate limiting via ``rate_limit_rps`` gate before each fetch.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urldefrag, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from ..extractors.web_extractor import WebPage, extract_page

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "q-orca-kb-crawler/0.1"


@dataclass
class CrawlConfig:
    site_key: str
    seeds: list[str]
    allow_patterns: list[str]
    block_patterns: list[str]
    wing: str
    room: str
    max_pages: int = 500
    depth_limit: int = 6
    rate_limit_rps: float = 1.5


def new_progress(site_key: str = "") -> dict[str, Any]:
    """Fresh progress dict — caller mutates this in place."""
    return {
        "site_key": site_key,
        "queued": 0,
        "fetched": 0,
        "indexed": 0,
        "skipped": 0,
        "errors": 0,
        "current_url": "",
    }


# ---------------------------------------------------------------------------
# URL filters
# ---------------------------------------------------------------------------

_SKIP_EXT = re.compile(
    r"\.(pdf|zip|tar|gz|7z|rar|mp4|mp3|wav|avi|mov|mkv|png|jpe?g|gif|svg|webp|"
    r"ico|css|js|mjs|woff2?|ttf|eot|dmg|exe|whl)(\?|$)",
    re.IGNORECASE,
)
_SKIP_SUBSTRINGS = ("/api/", "/cdn-cgi/")


def normalize_url(url: str) -> str:
    """Drop fragment; strip trailing slash from path (except root)."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    path = p.path
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, ""))


def is_crawlable(url: str) -> bool:
    if url.startswith(("mailto:", "javascript:", "tel:")):
        return False
    if _SKIP_EXT.search(url):
        return False
    if any(s in url for s in _SKIP_SUBSTRINGS):
        return False
    return urlparse(url).scheme in ("http", "https")


def matches_config(url: str, config: CrawlConfig) -> bool:
    if any(re.search(p, url) for p in config.block_patterns):
        return False
    if not config.allow_patterns:
        return True
    return any(re.search(p, url) for p in config.allow_patterns)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


async def fetch_robots(host: str, scheme: str = "https") -> RobotFileParser | None:
    """Fetch ``<scheme>://<host>/robots.txt`` and return a parser (None on error)."""
    url = f"{scheme}://{host}/robots.txt"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    except Exception as exc:
        log.info("robots fetch failed for %s: %s", host, exc)
        return None
    if r.status_code >= 400:
        return None
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


RobotsFetcher = Callable[[str], Awaitable["RobotFileParser | None"]]
Extractor = Callable[..., Awaitable["WebPage | None"]]


async def crawl(
    browser: Any,
    config: CrawlConfig,
    progress: dict[str, Any],
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    extractor: Extractor | None = None,
    robots_fetcher: RobotsFetcher | None = None,
) -> AsyncIterator[WebPage]:
    """BFS crawl ``config.seeds``; yield each successfully extracted page."""
    extractor = extractor or extract_page
    robots_fetcher = robots_fetcher or (lambda host: fetch_robots(host))

    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for seed in config.seeds:
        seed_n = normalize_url(seed)
        if seed_n not in seen and is_crawlable(seed_n) and matches_config(seed_n, config):
            seen.add(seed_n)
            queue.append((seed_n, 0))
            progress["queued"] += 1

    robots_cache: dict[str, RobotFileParser | None] = {}
    last_request = 0.0
    min_interval = 1.0 / config.rate_limit_rps if config.rate_limit_rps > 0 else 0.0

    while queue and progress["fetched"] < config.max_pages:
        url, depth = queue.popleft()
        progress["current_url"] = url

        if depth > config.depth_limit:
            continue

        host = urlparse(url).netloc
        if host not in robots_cache:
            robots_cache[host] = await robots_fetcher(host)
        robots = robots_cache[host]
        if robots is not None and not robots.can_fetch(user_agent, url):
            log.info("robots.txt disallows %s", url)
            continue

        elapsed = time.monotonic() - last_request
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        last_request = time.monotonic()

        try:
            page = await extractor(browser, url)
        except Exception as exc:
            log.warning("extract failed for %s: %s", url, exc)
            progress["errors"] += 1
            continue

        if page is None:
            progress["errors"] += 1
            continue

        progress["fetched"] += 1
        yield page

        if depth + 1 > config.depth_limit:
            continue

        for link in page.links:
            link_n = normalize_url(link)
            if link_n in seen:
                continue
            if not is_crawlable(link_n):
                continue
            if not matches_config(link_n, config):
                continue
            seen.add(link_n)
            queue.append((link_n, depth + 1))
            progress["queued"] += 1
