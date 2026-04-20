"""Target site configurations for ``crawl_site``.

Each entry is a ``CrawlConfig`` keyed by ``site_key``. The wing/room pair
determines where indexed pages land in the mempalace taxonomy.
"""

from __future__ import annotations

from ..fetchers.web_crawler import CrawlConfig

SITE_CONFIGS: dict[str, CrawlConfig] = {
    "ibm-quantum-docs": CrawlConfig(
        site_key="ibm-quantum-docs",
        seeds=[
            "https://quantum.cloud.ibm.com/docs/en/guides",
            "https://docs.quantum.ibm.com/",
        ],
        allow_patterns=[
            r"quantum\.cloud\.ibm\.com/docs",
            r"docs\.quantum\.ibm\.com",
        ],
        block_patterns=[r"/api/", r"\.json$", r"/changelog"],
        wing="q-orca-implementations",
        room="ibm-quantum",
        max_pages=600,
    ),
    "ibm-quantum-learning": CrawlConfig(
        site_key="ibm-quantum-learning",
        seeds=[
            "https://quantum.cloud.ibm.com/learning/en",
            "https://learning.quantum.ibm.com/",
        ],
        allow_patterns=[
            r"quantum\.cloud\.ibm\.com/learning",
            r"learning\.quantum\.ibm\.com",
        ],
        block_patterns=[r"/login", r"/account"],
        wing="q-orca-implementations",
        room="ibm-quantum",
        max_pages=400,
    ),
    "msft-azure-quantum": CrawlConfig(
        site_key="msft-azure-quantum",
        seeds=["https://learn.microsoft.com/en-us/azure/quantum/"],
        allow_patterns=[r"learn\.microsoft\.com/en-us/azure/quantum"],
        block_patterns=[r"/api/", r"view="],
        wing="q-orca-implementations",
        room="azure-quantum",
        max_pages=400,
    ),
    "nvidia-cudaqx": CrawlConfig(
        site_key="nvidia-cudaqx",
        seeds=[
            "https://nvidia.github.io/cuda-quantum/latest/",
            "https://developer.nvidia.com/cuda-quantum",
        ],
        allow_patterns=[
            r"nvidia\.github\.io/cuda-quantum",
            r"developer\.nvidia\.com/cuda-q",
        ],
        block_patterns=[r"/release-notes/", r"\.pdf$"],
        wing="q-orca-implementations",
        room="nvidia-cudaqx",
        max_pages=300,
    ),
}
