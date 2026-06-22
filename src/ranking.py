"""
Source ranking for deep research pipeline (v0.9-C1).

ChatGPT P1 (v0.8.0 review): research_runner.py used `top1 = iter_documents[0]`,
which is just "the first URL we happened to ask SearXNG for" — not the
highest-ranked source. Long irrelevant documents could (and did) win
against short, highly-relevant ones simply because of fetch order.

Fix: rank documents by a combined `source_score` BEFORE selecting top1
or top-N. The score blends four cheap, deterministic signals:

  1. search_score     — v0.9-B `_search_provenance` search signal. If a
                        fetched document carries provenance from one or more
                        SearchTasks, we combine true per-task SearXNG rank
                        with a small capped query-vote bonus. Falls back to
                        `position_score` for legacy docs that have no
                        provenance.
  2. content_score    — _confidence-like heuristic on the document text:
                        length (log-scaled to 2000) + query term coverage
                        + noise penalty. Reuses the same heuristic that
                        `hermes_deepresearch._confidence` uses, so legacy
                        and v2 paths agree on what "good content" means.
  3. position_score   — 1 / (1 + original_index). At original_index=0 it
                        gets 1.0; index=1 gets 0.5; index=2 gets ~0.33. This
                        is SearXNG's prior — we don't ignore it, just
                        down-weight it so it can't dominate fresh signals.
                        Used only when provenance is absent.
  4. length_score     — small bonus for documents with at least 500 chars
                        of extracted text (avoids picking empty/404 pages).
  5. error_penalty    — if a fetch failed (error != None), score = 0
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


# Query-vote bonus: one query gives no bonus; each additional distinct
# task_query adds 0.10, capped at +0.20. Rank remains the primary search signal.
QUERY_VOTE_BONUS_PER_EXTRA_QUERY = 0.10
QUERY_VOTE_BONUS_MAX = 0.20


def _provenance_entries(doc: dict) -> list[dict]:
    """Return unique v0.9-B `_search_provenance` entries, preserving order."""
    prov = doc.get("_search_provenance") if isinstance(doc, dict) else None
    if not prov:
        return []
    out: list[dict] = []
    for entry in prov:
        if isinstance(entry, dict) and entry not in out:
            out.append(entry)
    return out


def _provenance_rank_score(doc: dict) -> float | None:
    """Best true SearXNG rank from provenance, normalized to [0, 1]."""
    scores: list[float] = []
    for entry in _provenance_entries(doc):
        rank = entry.get("rank")
        if type(rank) is int and rank >= 1:
            # Provenance rank is 1-based; _position_score takes a 0-based index.
            scores.append(_position_score(rank - 1))
    if not scores:
        return None
    return max(scores)


def _provenance_query_vote_bonus(doc: dict) -> float:
    """Small capped bonus from extra distinct task queries in provenance."""
    queries: set[str] = set()
    for entry in _provenance_entries(doc):
        query = entry.get("task_query")
        if isinstance(query, str) and (stripped_query := query.strip()):
            queries.add(stripped_query)
    return min(QUERY_VOTE_BONUS_MAX, QUERY_VOTE_BONUS_PER_EXTRA_QUERY * max(0, len(queries) - 1))


def _provenance_search_score(doc: dict) -> float | None:
    """Combine provenance rank and query-vote signals for v0.9-C1 ranking."""
    rank_score = _provenance_rank_score(doc)
    if rank_score is None:
        return None
    query_vote_bonus = _provenance_query_vote_bonus(doc)
    return min(1.0, rank_score + query_vote_bonus)


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
    query_terms: list[str] | None = None,
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
                        SearXNG hit list (fallback when provenance is absent).

    Returns:
        float in [0, 1]. Higher = better source candidate for top1.

    Failure modes:
        - doc is None or empty → 0.0
        - doc has error or empty text → 0.0
        - short text is allowed; it just receives a smaller length component
    """
    if not doc:
        return 0.0
    if doc.get("error") or not doc.get("text"):
        return 0.0

    text: str = doc["text"]
    text_lower = text.lower()
    text_len: int = doc.get("length") or len(text)

    # 1. Search signal (v0.9-C1).
    #    Use `_search_provenance` when available; otherwise fall back to
    #    the original SearXNG ordinal position. The provenance search
    #    signal uses only original per-task rank plus a small capped
    #    query-vote bonus; task
    #    priority is intentionally not a ranking signal in this batch.
    search = _provenance_search_score(doc)
    if search is None:
        search = _position_score(original_index)

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
    #   - search is provenance/position (0.35) — keep search ranking signal
    #   - content is primary (0.45) — keyword coverage with noise penalty
    #   - length is tiebreaker (0.20) — avoid empty/short wins
    # Note: weights sum to 1.0, and each component is in [0, 1].
    score = 0.35 * search + 0.45 * content + 0.20 * length
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
