"""Seed list of arXiv papers relevant to the q-orca project.

Each seed is (arxiv_id, wing, room, short_title).

Wings:
  q-orca-physics         — foundational quantum information & algorithms
  q-orca-implementations — circuits, hardware, formal methods
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Seed:
    arxiv_id: str
    wing: str
    room: str
    title: str


SEEDS: list[Seed] = [
    # --- physics / foundations ---
    Seed(
        arxiv_id="quant-ph/9508027",
        wing="q-orca-physics",
        room="oracle-algorithms",
        title="Shor: Polynomial-time factoring on a quantum computer",
    ),
    Seed(
        arxiv_id="quant-ph/9605043",
        wing="q-orca-physics",
        room="error-correction",
        title="Steane: Multiple-particle interference and quantum error correction",
    ),
    Seed(
        arxiv_id="quant-ph/9508018",
        wing="q-orca-physics",
        room="error-correction",
        title="Calderbank-Shor: Good quantum error-correcting codes exist",
    ),
    Seed(
        arxiv_id="quant-ph/9602019",
        wing="q-orca-physics",
        room="oracle-algorithms",
        title="Grover: A fast quantum mechanical algorithm for database search",
    ),
    Seed(
        arxiv_id="1304.3061",
        wing="q-orca-physics",
        room="vqe",
        title="Peruzzo et al.: A variational eigenvalue solver on a quantum processor",
    ),
    Seed(
        arxiv_id="1411.4028",
        wing="q-orca-physics",
        room="vqe",
        title="Farhi-Goldstone-Gutmann: A Quantum Approximate Optimization Algorithm (QAOA)",
    ),
    # --- implementations ---
    Seed(
        arxiv_id="1208.0928",
        wing="q-orca-implementations",
        room="error-correction",
        title="Fowler et al.: Surface codes — towards practical large-scale quantum computation",
    ),
    Seed(
        arxiv_id="1704.05018",
        wing="q-orca-implementations",
        room="hardware",
        title="Kandala et al.: Hardware-efficient variational quantum eigensolver",
    ),
    Seed(
        arxiv_id="1707.03429",
        wing="q-orca-implementations",
        room="circuits",
        title="Cross et al.: Open Quantum Assembly Language (OpenQASM)",
    ),
    Seed(
        arxiv_id="1411.6024",
        wing="q-orca-implementations",
        room="formal-methods",
        title="Green-Lumsdaine-Ross-Selinger-Valiron: Quipper — a scalable quantum programming language",
    ),
]
