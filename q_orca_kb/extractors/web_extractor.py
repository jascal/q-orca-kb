"""Web page extractor (Playwright + BeautifulSoup4).

Renders a URL via headless Chromium (handles React SPAs), waits for network
idle, then strips structural and named boilerplate and returns a ``WebPage``
with section-based pre-chunking. Designed to be driven by ``web_crawler.py``.

The Playwright browser is injected by the caller so a single Chromium instance
can serve many page fetches — opening a new browser per page would be ~10×
slower.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

_BOILERPLATE_PATTERNS = re.compile(
    r"nav|sidebar|footer|cookie|banner|menu",
    re.IGNORECASE,
)
# Plain ASCII; picked to not collide with doc content AND to not be classified
# as a line separator by ``str.splitlines()`` (which does split on \x1e, \x1c…).
_SECTION_MARKER = "@@Q_ORCA_KB_SECTION_BREAK@@"


@dataclass
class WebPageSection:
    heading: str
    text: str


@dataclass
class WebPage:
    url: str
    title: str
    text: str
    links: list[str]
    content_hash: str
    fetched_at: float
    sections: list[WebPageSection] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def render_html(
    browser: Any, url: str, timeout: float = DEFAULT_TIMEOUT
) -> tuple[str, str] | None:
    """Render `url` with Playwright; return (final_url, html) or None on failure."""
    context = await browser.new_context()
    page = await context.new_page()
    try:
        response = await page.goto(
            url, timeout=int(timeout * 1000), wait_until="domcontentloaded"
        )
        if response is None or response.status >= 400:
            return None
        # networkidle is a best-effort wait for JS-rendered content; cap it well
        # under the per-page timeout so SPAs that never idle (analytics, long
        # polling) don't eat the full budget on every single page.
        networkidle_budget = min(5.0, timeout / 4)
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=int(networkidle_budget * 1000)
            )
        except Exception:
            pass
        html = await page.content()
        return page.url, html
    except Exception as exc:
        log.info("render failed for %s: %s", url, exc)
        return None
    finally:
        await page.close()
        await context.close()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _matches_boilerplate(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        value = " ".join(value)
    return bool(_BOILERPLATE_PATTERNS.search(str(value)))


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    """Remove structural + named boilerplate elements in place."""
    for tag in soup.find_all(
        ["nav", "header", "footer", "aside", "script", "style", "noscript", "form"]
    ):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        # Parent may have been decomposed earlier in the loop; snapshot still
        # references the orphaned child, whose ``.attrs`` is then None.
        if tag.attrs is None:
            continue
        if _matches_boilerplate(tag.get("class")) or _matches_boilerplate(tag.get("id")):
            tag.decompose()


def _flatten_text(soup_or_tag: Any) -> str:
    """Flatten a BS4 tree to text while preserving code/pre whitespace."""
    text = soup_or_tag.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


def _extract_sections(soup: BeautifulSoup) -> list[WebPageSection]:
    """Split the cleaned soup into sections on h2/h3 boundaries.

    Strategy: walk the tree and replace each ``<h2>``/``<h3>`` with a text
    marker; flatten; split on the marker. This avoids the mess of rebuilding
    a document from scattered siblings across deeply nested content trees.
    """
    body = soup.body or soup
    headings = body.find_all(["h2", "h3"])

    if not headings:
        text = _flatten_text(body)
        return [WebPageSection(heading="", text=text)] if text else []

    for h in headings:
        heading_text = h.get_text(" ", strip=True)
        h.insert_before(NavigableString(f"\n\n{_SECTION_MARKER}{heading_text}\n\n"))
        h.decompose()

    flat = _flatten_text(body)
    parts = flat.split(_SECTION_MARKER)
    sections: list[WebPageSection] = []

    # parts[0] is whatever preceded the first h2/h3 (often the H1/intro).
    pre = parts[0].strip()
    if pre:
        sections.append(WebPageSection(heading="", text=pre))

    for part in parts[1:]:
        block = part.strip()
        if not block:
            continue
        lines = block.split("\n", 1)
        heading = lines[0].strip()
        body_text = lines[1].strip() if len(lines) > 1 else ""
        full = f"{heading}\n\n{body_text}".strip() if body_text else heading
        if full:
            sections.append(WebPageSection(heading=heading, text=full))

    return sections


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Return absolute http(s) URLs from ``<a href>``, fragments stripped."""
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        absolute, _frag = urldefrag(urljoin(base_url, href))
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def parse_page(url: str, html: str) -> WebPage:
    """Parse raw rendered HTML into a ``WebPage`` (separated for test use)."""
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    # Links come from the pre-strip soup: nav/sidebar elements carry the real
    # site map on doc sites and we want to crawl those before discarding them.
    links = _extract_links(soup, url)

    _strip_boilerplate(soup)
    sections = _extract_sections(soup)

    text = "\n\n".join(s.text for s in sections).strip()
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return WebPage(
        url=url,
        title=title,
        text=text,
        links=links,
        content_hash=content_hash,
        fetched_at=time.time(),
        sections=sections,
    )


async def extract_page(
    browser: Any, url: str, timeout: float = DEFAULT_TIMEOUT
) -> WebPage | None:
    """Render via Playwright then parse into a ``WebPage``; None on failure."""
    rendered = await render_html(browser, url, timeout=timeout)
    if rendered is None:
        return None
    final_url, html = rendered
    return parse_page(final_url, html)
