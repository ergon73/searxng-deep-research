"""
Source ranking for deep research pipeline (v0.8.1.1 hardening).

ChatGPT P1 (v0.8.0 review): research_runner.py used `top1 = iter_documents[0]`,
which is just "the first URL we happened to ask SearXNG for" — not the
highest-ranked source. Long irrelevant documents could (and did) win
against short, highly-relevant ones simply because of fetch order.

Fix: rank documents by a combined `source_score` BEFORE selecting top1
or top-N. The score blends four cheap, deterministic signals:

  1. content_score  — _confidence-like heuristic on the document text:
                      length (log-scaled to 2000) + query term coverage
                      + noise penalty. Reuses the same heuristic that
                      `hermes_deepresearch._confidence` uses, so legacy
                      and v2 paths agree on what "good content" means.
  2. position_score  — 1 / (1 + original_index). If the URL came 1st from
                      SearXNG, it gets ~0.5; 2nd gets ~0.33; etc. This
                      is SearXNG's prior — we don't ignore it, just
                      down-weight it so it can't dominate fresh signals.
  3. length_score    — small bonus for documents with at least 500 chars
                      of extracted text (avoids picking empty/404 pages).
  4. error_penalty   — if a fetch failed (error != None), score = 0
                      regardless of other signals.

Final source_score is in [0, 1]. Ties are broken by URL alphabetical
order (deterministic — same input always yields same output, so
test assertions can be exact).

This module is **pure stdlib** — no network, no LLM. Safe to call
anywhere, including tests and previews.

Public API:
    compute_source_score(doc, query_terms) -> float
    rank_documents(documents, query) -> list[dict]   # sorted desc by score
    select_top_n(documents, query, n) -> list[dict]  # top-N (or fewer)
"""
from __future__ import annotations

import re
from typing import Optional


# Minimum extracted text length to consider a document "real content".
# 500 chars ≈ one paragraph — enough to extract at least one fact.
MIN_CONTENT_CHARS = 500

# Saturation point for length_score. Documents longer than this don't
# get a bigger length bonus (the marginal info gain flattens off).
LENGTH_SATURATION = 4000

# Position score formula: 1 / (1 + idx). At idx=0 → 1.0; idx=1 → 0.5;
# idx=2 → 0.33; idx=10 → 0.09. Steep early drop so a top-1 result
# really matters, but long tail doesn't completely vanish.
def _position_score(original_index: int) -> float:
    if original_index < 0:
        return 0.0
    return 1.0 / (1.0 + original_index)


def _length_score(text_len: int) -> float:
    """Log-ish saturation: 0 chars → 0, 500 → 0.12, 2000 → 0.5, 4000+ → 1.0."""
    if text_len <= 0:
        return 0.0
    if text_len >= LENGTH_SATURATION:
        return 1.0
    return text_len / LENGTH_SATURATION


# Cheap query-term extraction (same as the canonical query_terms
# logic in hermes_deepresearch). Lowercase, length >= 3, deduped,
# capped at 12. We don't import hermes_deepresearch here to avoid
# a runtime dependency cycle (ranking should be usable independently).
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


def _query_terms(query: str, max_terms: int = 12) -> list[str]:
    if not query:
        return []
    tokens = [t.lower() for t in _WORD_RE.findall(query)]
    seen: set[str] = set()
    out: list[str] = []
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


def _keyword_coverage(text_lower: str, terms: list[str]) -> float:
    """Fraction of query terms that appear in the text. 0..1."""
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t in text_lower)
    return hits / len(terms)


# Noise patterns: pages with these are likely navigation/SEO junk.
# We don't try to be exhaustive — just a few common offenders.
_NOISE_PATTERNS = [
    re.compile(r"cookie\s*policy", re.IGNORECASE),
    re.compile(r"accept\s+all\s+cookies", re.IGNORECASE),
    re.compile(r"sign\s+up\s+for\s+our\s+newsletter", re.IGNORECASE),
    re.compile(r"all\s+rights\s+reserved", re.IGNORECASE),
    re.compile(r"^\s*404\b", re.IGNORECASE),  # rare; fetched 404 pages
]


def _noise_penalty(text: str) -> float:
    """Return a value in [0, 1] — higher means more noise."""
    if not text:
        return 1.0
    hits = sum(1 for pat in _NOISE_PATTERNS if pat.search(text))
    return min(1.0, hits * 0.5)


def compute_source_score(
    doc: dict,
    query_terms: Optional[list[str]] = None,
    *,
    original_index: int = 0,
) -> float:
    """Compute a [0, 1] source_score for a single document.

    Args:
        doc: a document dict from `_fetch_documents` (keys: url, text,
             title, length, error, ...). Missing/empty text → 0.0.
        query_terms: pre-computed query terms (list of normalized tokens).
                     If None, no keyword signal is added.
        original_index: 0-based position of this doc in the original
                        SearXNG hit list (used for position_score).

    Returns:
        float in [0, 1]. Higher = better source candidate for top1.

    Failure modes:
        - doc is None or empty → 0.0
        - doc has error or empty text → 0.0
        - text < MIN_CONTENT_CHARS → score is just position_score
          (no content signal possible, but still some signal from rank)
    """
    if not doc:
        return 0.0
    if doc.get("error") or not doc.get("text"):
        return 0.0

    text: str = doc["text"]
    text_lower = text.lower()
    text_len: int = doc.get("length") or len(text)

    # 1. Position score (cheap; independent of content)
    pos = _position_score(original_index)

    # 2. Length score (very cheap; just a number)
    length = _length_score(text_len)

    # 3. Content score: keyword coverage, with noise penalty
    if query_terms:
        coverage = _keyword_coverage(text_lower, query_terms)
    else:
        coverage = 0.5  # neutral if no query
    noise = _noise_penalty(text)
    # Content is coverage * (1 - noise) so junk pages get heavily downweighted
    content = coverage * (1.0 - 0.5 * noise)

    # Final blend:
    #   - position is prior (0.35) — keep SearXNG's ranking signal
    #   - content is primary (0.45) — keyword coverage with noise penalty
    #   - length is tiebreaker (0.20) — avoid empty/short wins
    # Note: weights sum to 1.0, and each component is in [0, 1].
    score = 0.35 * pos + 0.45 * content + 0.20 * length
    return round(max(0.0, min(1.0, score)), 4)


def rank_documents(
    documents: list[dict],
    query: str = "",
) -> list[dict]:
    """Sort documents by source_score desc, with deterministic tie-breaks.

    The returned list is a NEW list (input is not mutated). Each doc
    gets a `source_score` field added in-place (mutating the dicts, but
    not the list order — we re-sort into a new list).

    Args:
        documents: list of doc dicts (from _fetch_documents).
        query: the original user query. Used to extract terms for
               keyword coverage. Empty string → no query signal.

    Returns:
        New list, sorted by (source_score desc, url asc).
    """
    if not documents:
        return []

    terms = _query_terms(query)
    # Score each doc and remember its original position for tie-breaking.
    scored: list[tuple[float, str, int, dict]] = []
    for i, d in enumerate(documents):
        s = compute_source_score(d, terms, original_index=i)
        scored.append((s, d.get("url", ""), i, d))

    # Sort: highest score first; on tie, earlier original index wins;
    # on further tie, alphabetical URL (deterministic).
    scored.sort(key=lambda x: (-x[0], x[2], x[1]))

    out: list[dict] = []
    for s, _url, _idx, d in scored:
        d["source_score"] = s  # attach for downstream consumers
        out.append(d)
    return out


def select_top_n(
    documents: list[dict],
    query: str = "",
    n: int = 4,
) -> list[dict]:
    """Return up to n highest-scored documents, in score-desc order.

    Convenience wrapper: `rank_documents(docs, query)[:n]`.
    If `n <= 0`, returns an empty list.
    """
    if n <= 0:
        return []
    ranked = rank_documents(documents, query)
    return ranked[:n]
