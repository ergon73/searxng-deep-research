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
    """Collapse all whitespace runs to a single space, strip ends.

    Plain normalize. For offset-preserving normalize, see
    `_normalize_with_map()` below — used by `find_span()` to return
    offsets against the **original** text, not the normalized one.
    """
    return _WS_RE.sub(" ", s).strip()


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize whitespace and return an index map from normalized to original.

    Collapses every whitespace run (`\\s+`) to a single space and strips
    leading/trailing whitespace, while remembering the position in the
    original text that each normalized position corresponds to.

    Returns:
        (normalized_text, index_map) where:
        - normalized_text: text with whitespace runs collapsed + stripped
        - index_map: list of len(normalized_text) ints, where
          index_map[i] = position in the original `text` of the i-th
          normalized character.

    Invariants:
        - len(index_map) == len(normalized_text)
        - For every i, normalized_text[i] == original_text[index_map[i]]
          (modulo whitespace collapses: the map points to one specific
          character in the collapsed run, the LITERAL one we keep).
        - If the original had no leading/trailing whitespace, the map
          starts at offset 0. If it did, the map shifts accordingly
          (because the normalized text is also stripped).

    Edge cases:
        - Empty text → ("", []).
        - Pure whitespace → ("", []).
        - Single non-WS char → (char, [0]) (or whichever original offset).
        - Text with only WS runs of varying length — every collapsed
          position points to the FIRST char of the original run.

    This is the building block for `find_span()`'s original-offset
    guarantee (v0.8.1.1 hardening). The map is O(n) memory but only
    built when Case 2/3 (whitespace-normalized match) is needed.
    """
    if not text:
        return "", []

    # Strip leading whitespace first (we don't include it in normalized).
    stripped = text.lstrip()
    leading_offset = len(text) - len(stripped)
    # Strip trailing whitespace.
    stripped = stripped.rstrip()
    if not stripped:
        return "", []

    normalized_parts: list[str] = []
    index_map: list[int] = []
    in_ws_run = False
    orig_pos = leading_offset  # position in the original text

    for ch in stripped:
        if ch.isspace():
            if not in_ws_run:
                # Start of a whitespace run — emit a single normalized space
                # and record the original position of THIS char (the first
                # of the collapsed run).
                normalized_parts.append(" ")
                index_map.append(orig_pos)
                in_ws_run = True
            # else: skip (collapse this WS char into the previous one)
        else:
            normalized_parts.append(ch)
            index_map.append(orig_pos)
            in_ws_run = False
        orig_pos += 1

    return "".join(normalized_parts), index_map


def find_span(claim: Claim, evidence_text: str) -> tuple[int, int]:
    """Locate the `claim.text` substring inside `evidence_text`.

    Returns (start_offset, end_offset) on hit, where end_offset is exclusive
    (Python-style: `evidence_text[start:end]` is the span that supports
    the claim). Returns (-1, -1) on miss.

    IMPORTANT (v0.8.1.1 hardening): all returned offsets refer to the
    **original** `evidence_text`, not any whitespace-normalized version.
    A downstream citation marker `[doc_0:120-187]` is therefore a
    reproducible pointer into the actual document.

    Strategy (cheapest first):
        1. Direct substring search (offsets trivially original).
        2. Whitespace-normalized substring search; convert the
           normalized-space hit back to the original-space offsets
           via the index map from `_normalize_with_map()`.
        3. Fuzzy prefix: take the first 30 chars of the normalized claim
           and search for that in the normalized text; if found, expand
           to claim length and convert both bounds back to original
           offsets (start is exact; end is approximate because we don't
           know exactly where the un-normalized claim ends).

    This is intentionally stdlib-only (no rapidfuzz, no LLM). For v0.8.0
    the `Claim.text` is already a verbatim sentence extracted by
    `_extract_facts` (a regex over sentence boundaries), so case 1
    hits in the great majority of cases. The fallback handles
    reformatting by `_synthesize`-style downstream consumers.

    Failure modes (all return (-1, -1)):
        - Empty claim text.
        - Empty evidence text.
        - No substring / normalized / fuzzy-prefix hit.
    """
    if not claim.text or not evidence_text:
        return (-1, -1)

    # Case 1: direct hit. Offsets are already in original space.
    idx = evidence_text.find(claim.text)
    if idx >= 0:
        return (idx, idx + len(claim.text))

    # Case 2: whitespace-normalized hit. We need to convert the
    # normalized-space offset back to the original-space offset.
    norm_text, norm_to_orig = _normalize_with_map(evidence_text)
    norm_claim = _normalize_ws(claim.text)
    idx = norm_text.find(norm_claim)
    if idx >= 0 and norm_to_orig:
        # Map the start: the i-th normalized char is at original
        # position norm_to_orig[i]. So the hit starts at that original
        # position.
        orig_start = norm_to_orig[idx]
        # End: idx + len(norm_claim) is exclusive in normalized space.
        # If it's within the map, map it back. If it's past the end of
        # the map (shouldn't happen for a find hit, but be safe), fall
        # back to orig_start + len(claim.text) — a best-effort estimate.
        end_in_norm = idx + len(norm_claim)
        if end_in_norm <= len(norm_to_orig):
            # The last normalized char of the hit corresponds to the
            # original position of the LAST char in the collapsed span.
            # We want a Python-style end (exclusive) — so we add 1 to
            # get past that last original char.
            last_orig = norm_to_orig[end_in_norm - 1]
            orig_end = last_orig + 1
        else:
            # Defensive: estimate.
            orig_end = orig_start + len(claim.text)
        return (orig_start, orig_end)

    # Case 3: fuzzy prefix (first 30 chars of normalized claim).
    if len(norm_claim) >= 10:
        prefix = norm_claim[:30]
        idx = norm_text.find(prefix)
        if idx >= 0 and norm_to_orig:
            orig_start = norm_to_orig[idx]
            # Approximate end: best-effort. The claim is len(norm_claim)
            # normalized chars long; we don't know exactly which original
            # positions that covers, so we use len(claim.text) as a
            # safe upper bound for the original offset distance.
            orig_end = orig_start + len(claim.text)
            return (orig_start, orig_end)

    return (-1, -1)


def build_evidence_window(claim: Claim, document: dict) -> EvidenceWindow | None:
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
            window_text = text[start : start + len(claim.text)]
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


def format_cited_claim(claim: Claim, evidence_window: EvidenceWindow | None, doc_index: int) -> str:
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
    return f"{claim.text} [doc_{doc_index}:{evidence_window.offset_start}-{evidence_window.offset_end}]"


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
        details = "\n".join(f"  - claim={c.text[:80]!r} is_stub={c.is_stub}" for c in uncited_non_stub[:5])
        raise AssertionError(
            f"{len(uncited_non_stub)} non-stub claim(s) lack evidence_window:\n"
            f"{details}"
            + (f"\n  ... and {len(uncited_non_stub) - 5} more" if len(uncited_non_stub) > 5 else "")
        )
    return cited, len(uncited_non_stub)
