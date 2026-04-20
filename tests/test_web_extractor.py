"""Tests for q_orca_kb.extractors.web_extractor.

Playwright is mocked out — we exercise ``parse_page`` directly on HTML fixtures
so we can cover boilerplate stripping, content-hashing and link extraction
without launching a browser.
"""

from __future__ import annotations

from q_orca_kb.extractors.web_extractor import parse_page


BASE = "https://example.com/quantum/guide"


HTML_WITH_BOILERPLATE = """
<html>
  <head><title>Quantum Guide</title></head>
  <body>
    <nav id="site-nav"><a href="/other">Other page</a></nav>
    <header class="top-banner">SITE BANNER CONTENT</header>
    <div id="cookie-consent">We use cookies — accept</div>
    <main>
      <h1>Quantum Guide</h1>
      <h2>Bell states</h2>
      <p>Maximally entangled two-qubit states.</p>
      <pre><code>import qiskit</code></pre>
      <h3>Measurement</h3>
      <p>Measurement collapses the state.</p>
    </main>
    <aside class="sidebar">Related links</aside>
    <footer class="site-footer">Copyright 2026</footer>
  </body>
</html>
"""


def test_boilerplate_is_stripped():
    page = parse_page(BASE, HTML_WITH_BOILERPLATE)
    assert "SITE BANNER CONTENT" not in page.text
    assert "Copyright 2026" not in page.text
    assert "We use cookies" not in page.text
    assert "Related links" not in page.text


def test_content_body_is_preserved():
    page = parse_page(BASE, HTML_WITH_BOILERPLATE)
    assert "Maximally entangled" in page.text
    assert "import qiskit" in page.text
    assert "Measurement collapses" in page.text


def test_sections_split_on_h2_and_h3():
    page = parse_page(BASE, HTML_WITH_BOILERPLATE)
    headings = [s.heading for s in page.sections if s.heading]
    assert "Bell states" in headings
    assert "Measurement" in headings


def test_content_hash_stable_for_same_text():
    a = parse_page(BASE, HTML_WITH_BOILERPLATE)
    b = parse_page(BASE, HTML_WITH_BOILERPLATE)
    assert a.content_hash == b.content_hash


def test_content_hash_changes_when_text_changes():
    a = parse_page(BASE, HTML_WITH_BOILERPLATE)
    altered = HTML_WITH_BOILERPLATE.replace(
        "Maximally entangled", "Minimally entangled"
    )
    b = parse_page(BASE, altered)
    assert a.content_hash != b.content_hash


def test_links_are_absolute_and_exclude_junk_schemes():
    html = """
    <html><body>
      <a href="/a/relative">rel</a>
      <a href="https://other.example.com/page">abs</a>
      <a href="mailto:x@y.com">mail</a>
      <a href="javascript:void(0)">js</a>
      <a href="#anchor">frag</a>
      <a href="/a/relative">dup</a>
    </body></html>
    """
    page = parse_page(BASE, html)
    for link in page.links:
        assert link.startswith("http://") or link.startswith("https://")
        assert "mailto:" not in link
        assert "javascript:" not in link
    assert "https://example.com/a/relative" in page.links
    assert "https://other.example.com/page" in page.links
    assert len(page.links) == len(set(page.links))
