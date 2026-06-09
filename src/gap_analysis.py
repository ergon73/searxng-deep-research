"""
Gap analysis for iterative deep research (Phase 5, v0.8.0).

After each pipeline pass, `analyze_gaps()` looks at the `ResearchState` and
returns a list of `ResearchGap` describing what is missing or weak. The
runner uses these to decide whether to add more search tasks and run another
iteration.

This module is **pure stdlib** — no LLM, no network, no I/O. Safe to call in
any context, including tests and previews.

Gap types we recognise:
- "too_few_sources" — fewer than 3 documents fetched
- "too_many_unsupported_claims" — claim→source ratio is too low
- "low_source_diversity" — all sources come from the same domain
- "contradictions_unresolved" — verify_sources found contradicting sources
  and the verdict didn't pick a side
- "low_confidence" — top-1 source_score < threshold (default 0.5)
- "no_search_results" — search returned 0 hits (already detected in runner;
  surfaced here for completeness)

Why these specific gaps and not more:
- They're actionable (each can be addressed by adding search tasks)
- They map to clearly bad outcomes a user can see
- They don't overlap with each other
- They're cheap to compute

We deliberately do NOT add:
- "missing_entity_X" (would need entity extraction — premature)
- "user_unsatisfied" (would need feedback loop — out of scope)
- "needs_more_iterations" (that's the runner's call, not gap analysis)

Spec: ~/.hermes/plans/ISSUES.md #020.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

# Local imports (not TYPE_CHECKING — we construct SearchTask at runtime)
from models import SearchTask

if TYPE_CHECKING:
    from models import ResearchState


# ========================================================================
# Thresholds (single source of truth — easy to tune)
# ========================================================================

MIN_DOCUMENTS = 3  # below this: too_few_sources
MIN_UNIQUE_DOMAINS = 2  # below this: low_source_diversity
MAX_UNSUPPORTED_CLAIM_RATIO = 0.4  # above this: too_many_unsupported_claims
MIN_TOP1_CONFIDENCE = 0.5  # below this: low_confidence


@dataclass(frozen=True)
class ResearchGap:
    """A description of something missing or weak in the current research.

    `kind` is a short string code (e.g. "too_few_sources") — caller can
    switch on it. `detail` is a human-readable explanation.
    """

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


# ========================================================================
# Helpers
# ========================================================================


def _domain_of(url: str) -> str:
    """Extract the registrable domain from a URL, safely.

    We use `urlparse` and return the netloc (host) — not a full
    public-suffix-aware split. For "different domain" detection, comparing
    netlocs is sufficient (en.wikipedia.org vs ru.wikipedia.org are
    different netlocs, which is what we want for diversity).
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        # Strip "www." prefix
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _count_unique_domains(documents: list[dict]) -> int:
    """Count unique domains across documents."""
    domains = {_domain_of(d.get("url", "")) for d in documents}
    domains.discard("")  # ignore empty/invalid
    return len(domains)


def _has_contradiction(verdicts: list[dict]) -> bool:
    """Check if any verdict has CONFLICTING or REFUTES at high rate.

    We treat "any CONFLICTING in verification_details" as unresolved —
    the runner should add a falsification task to resolve it.
    """
    for v in verdicts or []:
        for detail in v.get("verification_details", []) or []:
            method = detail.get("method", "")
            if method == "conflicting":
                return True
        # Also: if verification_rate is very low and total_facts > 0,
        # the verifier couldn't reconcile — treat as contradiction.
        if v.get("total_facts", 0) > 0 and v.get("verification_rate", 1.0) < 0.2:
            return True
    return False


def _top1_confidence(documents: list[dict]) -> float:
    """Get the top-1 document's source_score / confidence.

    We use `source_score` if present (newer convention from
    hermes_deepresearch.py), fall back to `confidence` (older).
    """
    if not documents:
        return 0.0
    top = documents[0]
    return float(top.get("source_score", top.get("confidence", 0.0)))


# ========================================================================
# Public API
# ========================================================================


def analyze_gaps(state: ResearchState) -> list[ResearchGap]:
    """Analyse a ResearchState and return a list of detected gaps.

    Pure function. No side effects, no I/O. Order of returned gaps is
    deterministic (sorted by kind) for stable test assertions.
    """
    gaps: list[ResearchGap] = []
    documents = state.documents or []
    verdicts = state.verdicts or []
    claims = state.claims or []
    search_hits = state.search_hits or []

    # 1. Too few sources
    if len(documents) < MIN_DOCUMENTS:
        gaps.append(
            ResearchGap(
                kind="too_few_sources",
                detail=f"only {len(documents)} documents fetched (min {MIN_DOCUMENTS})",
            )
        )

    # 2. No search results (already detected in runner; surfaced here too)
    if not search_hits and not documents:
        gaps.append(
            ResearchGap(
                kind="no_search_results",
                detail="web_search returned 0 hits for all tasks",
            )
        )

    # 3. Low source diversity
    if documents:  # only meaningful if we have at least 1 source
        unique_domains = _count_unique_domains(documents)
        if unique_domains < MIN_UNIQUE_DOMAINS:
            gaps.append(
                ResearchGap(
                    kind="low_source_diversity",
                    detail=f"only {unique_domains} unique domain(s) (min {MIN_UNIQUE_DOMAINS})",
                )
            )

    # 4. Too many unsupported claims
    # v0.8.1.1: substring match (claim[:50] in document_text) was a
    # trivial check — claims are extracted FROM documents, so the
    # substring would always be there. Real "supported" should mean
    # cross-source: a verification verdict marked SUPPORTS this claim
    # against another source (or, if LLM is on, an LLM verdict).
    #
    # Strategy:
    #   - Build a map: claim.text (first 50 chars, lowered) -> verdict
    #     from state.verdicts[i].verification_details[j].
    #   - A claim is "supported" if a matching verdict has verdict
    #     in {"SUPPORTS"} (or method in {"exact", "fuzzy", "llm"} with
    #     verified=True).
    #   - Fallback to substring check only if state.verdicts is empty
    #     (legacy: pre-verification state) so existing tests still pass.
    if claims:
        # Build (lowered_50char_text -> verdict) map from verdicts.
        verdict_map: dict[str, str] = {}
        for v in verdicts or []:
            for d in v.get("verification_details", []) or []:
                if not isinstance(d, dict):
                    continue
                fact = d.get("fact", "")
                if not fact:
                    continue
                key = fact[:50].lower()
                # Resolve the verdict string: prefer explicit verdict,
                # else fall back to verified=True → "SUPPORTS" / None.
                raw = d.get("verdict")
                if raw is None:
                    raw = "SUPPORTS" if d.get("verified") else None
                if raw is None:
                    continue  # no signal at all, skip
                v_value: str = raw
                # Prefer the strongest verdict if duplicated.
                existing = verdict_map.get(key)
                if v_value == "SUPPORTS" or existing != "SUPPORTS":
                    verdict_map[key] = v_value

        unsupported = 0
        use_verdicts = bool(verdict_map)  # only use verdicts if we have any
        for c in claims:
            needle = c.text[:50].lower() if hasattr(c, "text") else str(c)[:50].lower()
            if not needle:
                continue
            if use_verdicts:
                # New (v0.8.1.1) path: claim supported iff verdict_map says so.
                if verdict_map.get(needle) != "SUPPORTS":
                    unsupported += 1
            else:
                # Legacy fallback: substring in any document. Used when
                # verify_sources() has not run yet (e.g. early-exit state).
                if not any(needle in (d.get("text", "") or "").lower() for d in documents):
                    unsupported += 1
        ratio = unsupported / max(1, len(claims))
        if ratio > MAX_UNSUPPORTED_CLAIM_RATIO:
            verdict_source = "verdicts" if use_verdicts else "substring_fallback"
            gaps.append(
                ResearchGap(
                    kind="too_many_unsupported_claims",
                    detail=(
                        f"{unsupported}/{len(claims)} claims ({ratio:.0%}) "
                        f"unsupported (max {MAX_UNSUPPORTED_CLAIM_RATIO:.0%}, "
                        f"source={verdict_source})"
                    ),
                )
            )

    # 5. Contradictions unresolved
    if _has_contradiction(verdicts):
        gaps.append(
            ResearchGap(
                kind="contradictions_unresolved",
                detail="verification found conflicting sources that weren't reconciled",
            )
        )

    # 6. Low top-1 confidence
    if documents:
        conf = _top1_confidence(documents)
        if conf < MIN_TOP1_CONFIDENCE:
            gaps.append(
                ResearchGap(
                    kind="low_confidence",
                    detail=f"top-1 source_score={conf:.2f} (min {MIN_TOP1_CONFIDENCE})",
                )
            )

    # Sort by kind for deterministic test output
    gaps.sort(key=lambda g: g.kind)
    return gaps


# ========================================================================
# Runner integration helpers
# ========================================================================


def gaps_to_search_tasks(
    gaps: list[ResearchGap],
    *,
    original_query: str,
    route: str = "general",
    language: str = "en",
) -> list[SearchTask]:
    """Convert detected gaps into additional `SearchTask`s for the next pass.

    This is the bridge between gap analysis and the runner's iteration loop.
    The runner appends these tasks to `state.search_tasks` before the next
    `max_iterations` loop.

    Each gap maps to 0-1 task. If we can't formulate a useful task for a
    gap (e.g. "no_search_results" already failed — adding more queries won't
    help), we return no task and the runner stops iterating.

    Args:
        gaps: output of `analyze_gaps(state)`.
        original_query: the original user query (we expand it).
        route: routing classification to apply to new tasks.
        language: language for new tasks.

    Returns:
        List of `SearchTask` (priority 50 — between alts 80 and falsification 40).
    """
    tasks: list[SearchTask] = []
    seen_kinds: set[str] = set()

    for gap in gaps:
        if gap.kind in seen_kinds:
            continue  # don't add 2 tasks for the same gap kind

        if gap.kind == "too_few_sources":
            seen_kinds.add(gap.kind)
            tasks.append(
                SearchTask(
                    query=original_query,
                    route=route,
                    language=language,
                    priority=50,
                    rationale="gap-fill: too_few_sources → retry with original query",
                )
            )
        elif gap.kind == "low_source_diversity":
            seen_kinds.add(gap.kind)
            # Try to broaden via engines: use no engine filter (let SearXNG pick)
            tasks.append(
                SearchTask(
                    query=original_query,
                    route=route,
                    language=language,
                    engines=None,  # explicitly drop engine filter
                    priority=50,
                    rationale="gap-fill: low_source_diversity → retry without engine filter",
                )
            )
        elif gap.kind == "too_many_unsupported_claims":
            seen_kinds.add(gap.kind)
            # Reformulate to a more specific query
            tasks.append(
                SearchTask(
                    query=f"{original_query} facts evidence sources",
                    route=route,
                    language=language,
                    priority=50,
                    rationale="gap-fill: too_many_unsupported_claims → reformulate to specific",
                )
            )
        elif gap.kind == "contradictions_unresolved":
            seen_kinds.add(gap.kind)
            # Look for the contradicting source explicitly
            tasks.append(
                SearchTask(
                    query=f"{original_query} review",
                    route=route,
                    language=language,
                    priority=50,
                    rationale="gap-fill: contradictions_unresolved → add review query",
                )
            )
        elif gap.kind == "low_confidence":
            seen_kinds.add(gap.kind)
            # Try with a more authoritative source hint
            tasks.append(
                SearchTask(
                    query=f"{original_query} official documentation",
                    route=route,
                    language=language,
                    priority=50,
                    rationale="gap-fill: low_confidence → seek authoritative sources",
                )
            )
        # NOTE: "no_search_results" is intentionally NOT retried — if all
        # queries returned 0, more queries of the same kind won't help.

    return tasks
