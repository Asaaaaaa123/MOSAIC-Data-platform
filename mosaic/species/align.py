"""Pairwise protein alignment for ortholog similarity (fallback when Ensembl Compara has no answer).

Global Needleman-Wunsch alignment with BLOSUM62, gap open -11 / extend -1 (Biopython
``PairwiseAligner``). Percentages are relative to the HUMAN protein length, so a fragment or a
truncated ortholog cannot look 100% identical:

- identity_pct   = identical aligned residues / human length * 100
- similarity_pct = aligned residue pairs with a positive BLOSUM62 score / human length * 100
- coverage_pct   = human residues aligned to a residue (not a gap) / human length * 100
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

MAX_PROTEIN_LENGTH = 4000
_NON_LETTERS = re.compile(r"[^A-Z]")


@dataclass(frozen=True)
class AlignmentStats:
    identity_pct: float
    similarity_pct: float
    coverage_pct: float
    identical: int
    positives: int
    aligned: int
    human_length: int
    other_length: int
    score: float


@lru_cache(maxsize=1)
def _aligner():
    from Bio.Align import PairwiseAligner, substitution_matrices

    matrix = substitution_matrices.load("BLOSUM62")
    aligner = PairwiseAligner()
    aligner.mode = "global"
    aligner.substitution_matrix = matrix
    aligner.open_gap_score = -11
    aligner.extend_gap_score = -1
    alphabet = set(matrix.alphabet)
    positive = {(a, b) for a in matrix.alphabet for b in matrix.alphabet if matrix[a, b] > 0}
    return aligner, alphabet, positive


def clean_sequence(sequence: str | None) -> str:
    """Uppercase one-letter residues; letters outside BLOSUM62 (U, O, J ...) become X."""
    _, alphabet, _ = _aligner()
    text = _NON_LETTERS.sub("", (sequence or "").upper())
    return "".join(c if c in alphabet else "X" for c in text)


def align_proteins(human: str, other: str, max_length: int = MAX_PROTEIN_LENGTH) -> AlignmentStats | None:
    """Align ``other`` to the human protein; None when a sequence is empty or longer than ``max_length``."""
    h, o = clean_sequence(human), clean_sequence(other)
    if not h or not o or len(h) > max_length or len(o) > max_length:
        return None
    aligner, _, positive = _aligner()
    if h == o:
        n = len(h)
        return AlignmentStats(100.0, 100.0, 100.0, n, n, n, n, n, float(aligner.score(h, o)))
    alignment = aligner.align(h, o)[0]
    identical = positives = aligned = 0
    for (hs, he), (os_, _oe) in zip(*alignment.aligned):
        for i in range(he - hs):
            a, b = h[hs + i], o[os_ + i]
            aligned += 1
            if a == b:
                identical += 1
            if (a, b) in positive:
                positives += 1
    n = len(h)
    return AlignmentStats(
        identity_pct=round(identical / n * 100, 2),
        similarity_pct=round(positives / n * 100, 2),
        coverage_pct=round(aligned / n * 100, 2),
        identical=identical,
        positives=positives,
        aligned=aligned,
        human_length=n,
        other_length=len(o),
        score=float(alignment.score),
    )
