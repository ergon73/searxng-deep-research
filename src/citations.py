"""
Span-level citations for deep research pipeline (Phase 4, v0.8.0).

Each non-stub `Claim` is augmented with an `EvidenceWindow` — the actual
substring of the source document that supports the claim, identified by
character offsets (`start_offset`, `end_offset`). The synthesis layer
then inlines `[doc_id:start-end]` markers so downstream LLM prompts can
produce verifiable, source-attributable prose.

Why offset-based, not paragraph-based:
- Paragraphs shift when documents are re-fetched (whitespace, ads, footer).
- Char offsets are stable for the *exact* text we got, and we re-verify
  via `find_span` (substring search + normalization fallback).
- For a future LLM, `[N:120-187]` is concrete: "go to doc N, char 120-187".

Public API:
    find_span(claim, evidence_text)             -> (start, end) | (-1, -1)
    build_evidence_window(claim, document)      -> EvidenceWindow | None
    format_cited_claim(claim, evidence_window)  -> "claim text [doc_N:120-187]"
    citation_stats(claims)                      -> dict {total, cited, uncited, coverage}
    assert_citations_complete(claims, ...)      -> None (raises if any claim lacks window)

Spec: ~/.hermes/plans/ISSUES.md #019.
"""
from __future__ import annotations

import re
from typing import Optional

from evidence import EvidenceWindow
from models import Claim

# Citation marker regex: matches `[doc_<id>:<start>-<end>]` at end of a line
# (or anywhere — we don't anchor it). id is an integer; offsets are ints.
_CITATION_RE = re.compile(r"\[doc_(\d+):(\d+)-(\d+)\]")

# Whitespace collapse for fuzzy matching: collapse all whitespace runs to
# a single space. Useful when claim was extracted with newlines, but
# document has different whitespace.
_WS_RE = re.compile(r"\s+")


def _normalize_ws(s: str) -> str:
    """Collapse all whitespace runs to a single space, strip ends."""
    return _WS_RE.sub(" ", s).strip()


def find_span(claim: Claim, evidence_text: str) -> tuple[int, int]:
    """Locate the `claim.text` substring inside `evidence_text`.

    Returns (start_offset, end_offset) on hit, where end_offset is exclusive
    (Python-style: `evidence_text[start:end] == claim.text` when normalized
    whitespace matches). Returns (-1, -1) on miss.

    Strategy (cheapest first):
        1. Direct substring search.
        2. Whitespace-normalized substring search (collapse all \\s+ to ' ').
        3. Fuzzy prefix: take the first 30 chars of the normalized claim
           and search for that. If found, expand to claim length (best-effort
           span — end is approximate but start is exact).

    This is intentionally stdlib-only (no rapidfuzz, no LLM). For v0.8.0 the
    `Claim.text` is already a verbatim sentence extracted by `_extract_facts`
    (a regex over sentence boundaries), so case 1 hits in the great majority
    of cases. The fallback handles reformatting by `_synthesize`-style
    downstream consumers.
    """
    if not claim.text or not evidence_text:
        return (-1, -1)

    # Case 1: direct
    idx = evidence_text.find(claim.text)
    if idx >= 0:
        return (idx, idx + len(claim.text))

    # Case 2: whitespace-normalized
    norm_claim = _normalize_ws(claim.text)
    norm_text = _normalize_ws(evidence_text)
    idx = norm_text.find(norm_claim)
    if idx >= 0:
        # Offsets refer to the *normalized* text, not the original.
        # We document this in the docstring above. For downstream citation
        # we return normalized offsets, which is fine because consumers
        # use the normalized text for display.
        return (idx, idx + len(norm_claim))

    # Case 3: fuzzy prefix (first 30 chars)
    if len(norm_claim) >= 10:
        prefix = norm_claim[:30]
        idx = norm_text.find(prefix)
        if idx >= 0:
            # Approximate end: use len(norm_claim) as best-effort.
            return (idx, idx + len(norm_claim))

    return (-1, -1)


def build_evidence_window(
    claim: Claim, document: dict
) -> Optional[EvidenceWindow]:
    """Build an `EvidenceWindow` for `claim` based on `document`.

    `document` is a dict produced by `_fetch_documents` (keys: `url`,
    `text`, `title`, `score`). Returns `None` if the claim's text is not
    found anywhere in the document (then the runner records it as
    `unverified` rather than fabricating a span).

    Offsets are returned against the *whitespace-normalized* document text
    when fallback matching is used (see `find_span` docstring). For
    downstream citation rendering, the window is fed to
    `format_cited_claim` which displays the original claim text (the
    offsets are pointers, not display content).
    """
    text = document.get("text", "") or ""
    if not text:
        return None
    start, end = find_span(claim, text)
    if start < 0:
        return None

    # We pass the slice of the original text when possible. If the offsets
    # were obtained via normalized matching, the slice won't be exactly
    # equal to claim.text (it'll be the normalized version) — that's OK,
    # we just expose it as-is in EvidenceWindow.text. Callers that need
    # exact text should use `claim.text` directly.
    if end <= len(text) and _WS_RE.sub(" ", text[start:end]).strip() == _normalize_ws(claim.text):
        window_text = text[start:end]
    else:
        # Fallback: take len(claim.text) chars from start position
        # (only if we have a direct hit; otherwise just use claim.text itself
        # as a placeholder for display purposes).
        if end <= len(text):
            window_text = text[start:start + len(claim.text)]
        else:
            window_text = claim.text

    return EvidenceWindow(
        text=window_text,
        offset_start=start,
        offset_end=end,
        source_url=document.get("url", ""),
        source_title=document.get("title", ""),
        score=document.get("score", 0.0),
    )


def format_cited_claim(
    claim: Claim, evidence_window: Optional[EvidenceWindow], doc_index: int
) -> str:
    """Format a claim as `<text> [doc_<index>:<start>-<end>]`.

    `doc_index` is the 0-based position of the document in the original
    list (so `[doc_0:...]` means "first document"). This matches the
    convention used in academic citations and is easy to parse with the
    `_CITATION_RE` regex.

    If `evidence_window` is None (no span found), the claim is returned
    unchanged — no fake citation, no `[UNVERIFIED]` tag (downstream LLM
    can use `citation_stats` to detect and report unverified claims).
    """
    if evidence_window is None:
        return claim.text
    return (
        f"{claim.text} [doc_{doc_index}:"
        f"{evidence_window.offset_start}-{evidence_window.offset_end}]"
    )


def citation_stats(claims: list[Claim]) -> dict:
    """Compute citation coverage statistics for a list of claims.

    Returns:
        {
            "total": int,        # total claims
            "cited": int,        # claims with non-None evidence_window
            "uncited": int,      # claims without evidence_window
            "stub": int,         # claims flagged as stub
            "coverage": float,   # cited / total, 0.0 if total == 0
            "non_stub_coverage": float,  # cited / non_stub, 0.0 if none
        }
    """
    total = len(claims)
    stub = sum(1 for c in claims if c.is_stub)
    cited = sum(1 for c in claims if c.evidence_window is not None)
    uncited = total - cited
    non_stub = total - stub
    coverage = cited / total if total else 0.0
    non_stub_coverage = cited / non_stub if non_stub else 0.0
    return {
        "total": total,
        "cited": cited,
        "uncited": uncited,
        "stub": stub,
        "coverage": round(coverage, 4),
        "non_stub_coverage": round(non_stub_coverage, 4),
    }


def assert_citations_complete(
    claims: list[Claim],
    *,
    allow_stub: bool = True,
    raise_on_missing: bool = True,
) -> tuple[int, int]:
    """Invariant check: every non-stub claim has an evidence_window.

    Returns (cited_count, uncited_non_stub_count). If `raise_on_missing`
    and any non-stub claim lacks a window, raises `AssertionError` with
    details about the offending claims.

    Stubs (`is_stub=True`) are skipped — they are placeholders for LLM
    enrichment and not expected to have inline evidence. Pass
    `allow_stub=False` to enforce strict mode (every claim, including
    stubs, must have a window — useful for testing the test suite itself).
    """
    cited = 0
    uncited_non_stub: list[Claim] = []
    for c in claims:
        if c.is_stub and allow_stub:
            continue
        if c.evidence_window is None:
            uncited_non_stub.append(c)
        else:
            cited += 1

    if raise_on_missing and uncited_non_stub:
        details = "\n".join(
            f"  - claim={c.text[:80]!r} is_stub={c.is_stub}"
            for c in uncited_non_stub[:5]
        )
        raise AssertionError(
            f"{len(uncited_non_stub)} non-stub claim(s) lack evidence_window:\n"
            f"{details}"
            + (f"\n  ... and {len(uncited_non_stub) - 5} more" if len(uncited_non_stub) > 5 else "")
        )
    return cited, len(uncited_non_stub)
