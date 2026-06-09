"""
Evidence window extraction (skill 6.4: evidence-window-extraction).

Instead of feeding the LLM verifier the first 500 chars of a source,
find the *relevant* quote windows around a claim and feed those.

Audit 2026-06-07, section 5, Stage 4:
> "Извлекать короткие quote windows вокруг claim/entity, а не отдавать
>  LLM первые 500 символов страницы."

Spec: ~/.hermes/skills/research/evidence-window-extraction/SKILL.md

Design principles:
- Pure function, no network, no LLM. Safe to call anywhere.
- Backward-compatible with LLM verifier that gets truncated text.
- Hard cap on window size to prevent prompt injection amplification.
- Returns offsets relative to the original text (so callers can
  highlight quotes or cross-check).
- Idempotent: extracting twice yields the same windows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ====================================================================
# Constants
# ====================================================================

# Default window size: how many chars before+after the match to include.
# 300 chars (~50-80 words) is enough to give LLM context without
# overwhelming the prompt.
DEFAULT_WINDOW_SIZE = 300

# Hard cap to prevent prompt-injection amplification. If a source has
# a 1MB text with claim scattered, we still cap each window.
MAX_WINDOW_SIZE = 600

# Maximum number of windows to return per (text, claim) pair.
# 3 is enough for typical claims; more would bloat the LLM prompt.
DEFAULT_MAX_WINDOWS = 3

# Minimum match score for a window to be included. If the best window
# has a score below this, we fall back to a generic first-window
# (but capped at MAX_WINDOW_SIZE).
MIN_MATCH_SCORE = 0.1

# Word boundary characters for tokenisation.
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


# ====================================================================
# Data model
# ====================================================================


@dataclass
class EvidenceWindow:
    """A quote window extracted from a source text.

    Fields:
        text: The window content (may include leading/trailing ellipsis
              if the window was clipped from the source).
        offset_start: Start offset in the original text (inclusive).
        offset_end: End offset in the original text (exclusive).
        match_terms: Terms from the claim that were found in this window.
        match_score: 0.0-1.0; higher = more claim-terms matched.
                     A score of 0.0 means "fallback" (no claim found).
        source_url: URL of the source (Phase 4, #019 — for span-level
                    citations). Empty string if unknown.
        source_title: Human-readable title of the source. Empty if unknown.
        score: Source-level score (e.g. SearXNG ranking) carried through.
    """

    text: str
    offset_start: int
    offset_end: int
    match_terms: list[str] = field(default_factory=list)
    match_score: float = 0.0
    # Phase 4 (#019) — span-level citations: source provenance + score.
    # Backward-compatible defaults so existing call sites keep working.
    source_url: str = ""
    source_title: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "offset_start": self.offset_start,
            "offset_end": self.offset_end,
            "match_terms": self.match_terms,
            "match_score": self.match_score,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "score": self.score,
        }


# ====================================================================
# Tokenisation
# ====================================================================


def _tokenize(s: str) -> list[str]:
    """Tokenize a string into lowercase word tokens."""
    return [t.lower() for t in _WORD_RE.findall(s)]


def _claim_terms(claim: str, max_terms: int = 12) -> list[str]:
    """Extract significant search terms from a claim.

    - Lowercase
    - Deduplicated
    - Length >= 3 (skip tiny noise)
    - Capped at max_terms to keep extraction bounded
    """
    tokens = _tokenize(claim)
    # Dedup while preserving order
    seen = set()
    out = []
    for t in tokens:
        if len(t) < 3:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


def _find_term_positions(text_lower: str, term: str) -> list[int]:
    """Find all start positions of term in text_lower (case-insensitive)."""
    if not term:
        return []
    positions = []
    start = 0
    while True:
        idx = text_lower.find(term, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    return positions


# ====================================================================
# Window extraction
# ====================================================================


def extract_windows(
    text: str,
    claim: str,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    max_windows: int = DEFAULT_MAX_WINDOWS,
) -> list[EvidenceWindow]:
    """Extract up to max_windows evidence windows around a claim.

    Strategy:
    1. Tokenize claim, keep significant terms (length >= 3, unique).
    2. For each term, find all positions in text.
    3. Cluster nearby positions into "match clusters" (within window_size).
    4. Score each cluster by total matched terms.
    5. Take top max_windows clusters and emit a window per cluster.

    If no terms found (e.g. claim is too generic), fall back to a
    single window at the start of the text (truncated to window_size).

    Args:
        text: The full source text to search within.
        claim: The claim/fact to find evidence for.
        window_size: Half-window: ±window_size chars around the match.
        max_windows: Maximum number of windows to return.

    Returns:
        List of EvidenceWindow, sorted by match_score descending.
        Always returns at least one window (fallback to first chunk).
    """
    if not text:
        return []

    # Hard caps
    ws = min(max(50, int(window_size)), MAX_WINDOW_SIZE)
    mw = max(1, int(max_windows))

    text_lower = text.lower()
    terms = _claim_terms(claim)

    if not terms:
        # Generic / empty claim: return a single starting window.
        return [_make_fallback_window(text, ws)]

    # For each term, find positions.
    term_positions: dict[str, list[int]] = {}
    for term in terms:
        positions = _find_term_positions(text_lower, term)
        if positions:
            term_positions[term] = positions

    if not term_positions:
        # None of the terms found in text: fallback.
        return [_make_fallback_window(text, ws)]

    # Cluster all positions: any two positions within `ws` are in
    # the same cluster.
    all_positions: list[tuple[int, str]] = []  # (position, term)
    for term, positions in term_positions.items():
        for p in positions:
            all_positions.append((p, term))
    all_positions.sort(key=lambda x: x[0])

    clusters: list[dict] = []  # {start, end, terms: set, score}
    for pos, term in all_positions:
        placed = False
        for cluster in clusters:
            if abs(pos - cluster["center"]) <= ws:
                cluster["positions"].append((pos, term))
                cluster["terms"].add(term)
                cluster["center"] = sum(p for p, _ in cluster["positions"]) / len(cluster["positions"])
                cluster["score"] = len(cluster["terms"]) / len(terms)
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "positions": [(pos, term)],
                    "terms": {term},
                    "center": float(pos),
                    "score": 1.0 / len(terms),
                }
            )

    # Sort by score desc, then by cluster center (earlier first)
    clusters.sort(key=lambda c: (-c["score"], c["center"]))

    # Build windows from top clusters.
    windows: list[EvidenceWindow] = []
    for cluster in clusters[:mw]:
        center = int(cluster["center"])
        start = max(0, center - ws)
        end = min(len(text), center + ws)
        # Adjust start to word boundary if possible
        if start > 0:
            m = re.search(r"\s", text[start : start + 30])
            if m:
                start = start + m.end()
        # Adjust end to word boundary
        if end < len(text):
            m = re.search(r"\s\S*$", text[max(0, end - 30) : end])
            if m:
                end = end - len(m.group(0))

        # Clip and add ellipsis indicators
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text) else ""
        win_text = prefix + text[start:end].strip() + suffix

        windows.append(
            EvidenceWindow(
                text=win_text,
                offset_start=start,
                offset_end=end,
                match_terms=sorted(cluster["terms"]),
                match_score=cluster["score"],
            )
        )

    if not windows or windows[0].match_score < MIN_MATCH_SCORE:
        # Best match is weak: prepend a fallback window at the start
        # so the LLM still has *some* context.
        fb = _make_fallback_window(text, ws)
        if not windows:
            return [fb]
        return [fb] + windows[: mw - 1]

    return windows


def _make_fallback_window(text: str, window_size: int) -> EvidenceWindow:
    """Return a window at the start of the text (used when no terms match)."""
    end = min(len(text), window_size)
    # Try to end at a word boundary
    if end < len(text):
        m = re.search(r"\s\S*$", text[max(0, end - 30) : end])
        if m:
            end = end - len(m.group(0))
    return EvidenceWindow(
        text=text[:end].strip() + ("..." if end < len(text) else ""),
        offset_start=0,
        offset_end=end,
        match_terms=[],
        match_score=0.0,
    )


# ====================================================================
# Public helper: build a single text blob from windows
# ====================================================================


def windows_to_blob(
    windows: list[EvidenceWindow],
    *,
    max_total_chars: int = 1500,
    separator: str = "\n...\n",
) -> str:
    """Concatenate windows into a single text blob for LLM consumption.

    Hard cap on total chars to prevent prompt bloat.
    """
    if not windows:
        return ""
    parts: list[str] = []
    total = 0
    for w in windows:
        if total + len(w.text) + len(separator) > max_total_chars:
            break
        parts.append(w.text)
        total += len(w.text) + len(separator)
    return separator.join(parts)
