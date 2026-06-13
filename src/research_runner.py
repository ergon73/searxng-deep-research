"""
Research runner for deep research pipeline (Phase 3, v0.8.0).

`deep_research_v2()` — strangler refactor of legacy `deep_research()`. Goes
through the typed `ResearchPlan` + `ResearchState` machinery built in
Phase 1 (`models.py`) and Phase 2 (`planner.py`).

Strangler refactor principle: we do NOT modify the legacy `deep_research()`
function. The new runner reuses the same primitive functions
(`web_search`, `fetch_url`, `_extract_facts`, `verify_sources`,
`synthesize`, `review`) but composes them via the typed plan/state.

Public API:
    run_research(query, *, approved_plan=False, max_iterations=1, use_llm=False)
        -> ResearchResult

`ResearchResult` is a typed dataclass with status, plan, state, synthesis,
review. Status is one of:
    "needs_confirmation" — plan.needs_confirmation and not approved_plan
    "done"               — pipeline completed
    "error"              — exception (caught and wrapped)

Design notes:
- We DO NOT call `web_search` in this module directly during the import
  path; the actual dispatch happens in `_run_pipeline`.
- `approved_plan=True` means the caller has reviewed the plan and OK'd it.
  Without approval, plans with `needs_confirmation=True` return early.
- For testing, we expose `_dispatch_search_task` and `_fetch_documents`
  as separate helpers so they can be monkeypatched in `test_research_runner.py`.
- Iterative deepening is implemented in v0.8.0 (Phase 5, see #020) via
  `gap_analysis.analyze_gaps()` + `gaps_to_search_tasks()`. The runner loop
  detects gaps after each pass and adds gap-fill tasks for the next pass
  (up to `max_iterations`). `max_iterations=1` is the default and what
  the legacy tests use.
- Span-level citations (Phase 4, #019): each non-stub `Claim` is augmented
  with `evidence_window` via `citations.find_span()`. The synthesis layer
  injects `[doc_id:start-end]` markers inline; downstream LLM prompts can
  use these to produce verifiable, source-attributable prose. The invariant
  is enforced via `citations.assert_citations_complete()` in tests.

Spec: ~/.hermes/plans/ISSUES.md #018, #019 (this file) and #020 (gap analysis).
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import Any

from citations import build_evidence_window, citation_stats, format_cited_claim
from critical_review import ReviewResult, Synthesis, review  # type: ignore
from evidence import EvidenceWindow

# Gap analysis (Phase 5, v0.8.0)
from gap_analysis import analyze_gaps, gaps_to_search_tasks

# Primitive functions (legacy module — we re-use, do not modify)
from hermes_deepresearch import (
    MAX_CONCURRENT_FETCH,
    MAX_CONTENT_CHARS,
    _extract_facts,
    canonical_url,
    fetch_url,
    verify_sources,
    web_search,
)

# Local imports (typed state + planner)
from models import Claim, ResearchState, SearchTask

# Planner
from planner import ResearchPlan, build_research_plan, plan_to_state
from ranking import rank_documents  # v0.8.1.1: source ranking

# Newer stages
from synthesis import synthesize

# ========================================================================
# Public result type
# ========================================================================


@dataclass
class ResearchResult:
    """Public output of `run_research()`. Typed, JSON-serialisable."""

    status: str  # "needs_confirmation" | "done" | "error"
    original_query: str
    plan: ResearchPlan | None = None  # set for "needs_confirmation" and "done"
    state: ResearchState | None = None  # final state, set for "done"
    synthesis: Synthesis | None = None  # set for "done"
    review: ReviewResult | None = None  # set for "done"
    error: str | None = None  # set for "error"
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "original_query": self.original_query,
            "elapsed_sec": self.elapsed_sec,
        }
        if self.plan is not None:
            d["plan"] = self.plan.to_dict()
        if self.state is not None:
            d["state"] = self.state.to_dict()
        if self.synthesis is not None:
            # Synthesis has to_dict? Check. Fallback: vars()
            d["synthesis"] = getattr(self.synthesis, "to_dict", lambda: vars(self.synthesis))()
        if self.review is not None:
            d["review"] = getattr(self.review, "to_dict", lambda: vars(self.review))()
        if self.error is not None:
            d["error"] = self.error
        return d


# ========================================================================
# Pipeline stages (separated for testability)
# ========================================================================


def _dispatch_search_task(task: SearchTask, *, max_results: int = 5) -> list[dict]:
    """Send a single SearchTask to SearXNG. Returns raw hits.

    Pulled out as a helper so tests can monkeypatch it.
    """
    # Build kwargs without None values — web_search signature has
    # explicit `lang: str = "ru"` and may not accept None.
    kwargs: dict[str, Any] = {"max_results": max_results}
    if task.language and task.language != "auto":
        kwargs["lang"] = task.language
    if task.time_range is not None:
        kwargs["time_range"] = task.time_range
    if task.engines is not None:
        kwargs["engines"] = task.engines
    if task.categories is not None:
        kwargs["categories"] = task.categories
    return web_search(task.query, **kwargs)


def _task_key(task: SearchTask) -> tuple:
    """Stable identity for a SearchTask, used to dedup across iterations.

    Two tasks are "the same" iff they target the same query+route+language
    with the same engine/category constraints. The priority and rationale
    are deliberately excluded — a gap-fill task with the same intent but
    a different priority should still dedup against the original.
    """
    return (
        task.query,
        task.route,
        task.language,
        task.engines,
        task.categories,
    )


def _fetch_documents(urls: list[str], *, max_chars: int = MAX_CONTENT_CHARS) -> list[dict]:
    """Fetch a list of URLs in parallel, return list of {url, text, title, ...}.

    The returned list preserves the order of the input `urls`. This is
    critical for `verify_sources()`: it relies on `top1 = documents[0]`
    being the highest-ranked source, not "the URL that happened to
    finish fetching first".

    Implementation: dispatch all fetches, collect results into a
    `by_url` dict keyed by URL, then re-emit in the original order.
    """
    by_url: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCH) as ex:
        futures = {ex.submit(fetch_url, u, max_chars=max_chars): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            u = futures[fut]
            try:
                fr = fut.result()
            except Exception as e:
                fr = {"url": u, "error": f"{type(e).__name__}: {e}"}
            if fr is None:
                fr = {"url": u, "error": "fetch returned None"}
            if "url" not in fr:
                fr["url"] = u
            by_url[u] = fr
    # Emit in the original URL order. If a URL is somehow missing from
    # by_url (shouldn't happen with the dict-collect above, but be safe),
    # fall back to a placeholder so the output length matches input.
    return [by_url.get(u, {"url": u, "error": "missing fetch result"}) for u in urls]


def _dedup_hits_by_canonical(hits: list[dict]) -> list[dict]:
    """Deduplicate search hits by canonical URL, preserving first occurrence."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        u = canonical_url(h.get("url", ""))
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({**h, "url": u})
    return out


def _extract_claims_from_documents(
    documents: list[dict], query: str, max_per_doc: int = 8
) -> tuple[list[str], dict[str, list[dict]]]:
    """Extract facts from each document. Return (flat_claims, claims_to_source_urls).

    For now we use legacy `_extract_facts(text, max_facts, query)` which returns
    strings. The synthesis pipeline (`synthesize()`, `verify_sources()`) still
    wants string claims, so we keep this signature stable.

    For typed claims with span-level citations, see
    `_extract_typed_claims_with_citations()` (Phase 4, #019). The runner calls
    BOTH helpers: this one feeds synthesis, the typed one populates
    `state.claims` and `state.evidence`.
    """
    all_claims: list[str] = []
    claims_to_source_urls: dict[str, list[dict]] = {}  # claim -> [url, ...]

    for doc in documents:
        text = doc.get("text", "") or ""
        if not text or doc.get("error"):
            continue
        facts = _extract_facts(text, max_facts=max_per_doc, query=query)
        for f in facts:
            all_claims.append(f)
            claims_to_source_urls.setdefault(f, []).append({"url": doc.get("url", "")})

    return all_claims, claims_to_source_urls


def _extract_typed_claims_with_citations(
    documents: list[dict], query: str, max_per_doc: int = 8
) -> list[Claim]:
    """Extract typed `Claim` objects with span-level evidence windows.

    Phase 4 (#019). For each document:
      1. Run legacy `_extract_facts` to get fact strings.
      2. Promote each to a typed `Claim(text=fact_string)`.
      3. Run `build_evidence_window(claim, doc)` to attach a span.
      4. Use `dataclasses.replace` to produce an augmented claim
         (Claim is frozen=True — can't mutate in place).

    Returns the augmented list. Claims whose text is not found in any
    document get `evidence_window=None`; downstream `assert_citations_complete`
    treats these as gaps (so the runner can detect and report them).

    Performance note: this is O(n_facts × n_docs) in the worst case (each
    fact searches the full document text). For v0.8.0 with `max_per_doc=8`
    and a handful of docs, this is well under 1ms per claim in pure-Python.
    """
    augmented: list[Claim] = []
    for _doc_index, doc in enumerate(documents):
        text = doc.get("text", "") or ""
        if not text or doc.get("error"):
            continue
        facts = _extract_facts(text, max_facts=max_per_doc, query=query)
        for f in facts:
            base = Claim(text=f)
            window = build_evidence_window(base, doc)
            if window is not None:
                augmented.append(dc_replace(base, evidence_window=window))
            else:
                # Keep the claim but mark it unverified; runner can decide
                # whether to skip it or include as unverified.
                augmented.append(base)
    return augmented


def _doc_index_for_window(window: EvidenceWindow, documents: list[dict]) -> int | None:
    """Find the index of the document whose URL matches `window.source_url`.

    Used to produce `[doc_N:start-end]` markers that downstream LLM
    prompts can use as concrete pointers ("go to doc N, char 120-187").

    Returns `None` when the document index cannot be resolved — either
    `window.source_url` is empty, or no document in `documents` has
    that URL. All callers must treat `None` as "no span marker / no
    inline citation" and skip the entry; they must never fall back to
    `[doc_0:start-end]`. A fabricated `[doc_0:...]` would be misleading
    because the citation table id `[N]` in `answer_markdown` uses
    1-based offsets, so a doc index of 0 there refers to a *different*
    document than `documents[0]`.

    A valid resolved index of 0 is a real, expected value (a
    `source_url` that matches `documents[0]`) and must not be confused
    with the unresolved `None` case.
    """
    if not window.source_url:
        return None
    for i, doc in enumerate(documents):
        if doc.get("url") == window.source_url:
            return i
    return None


def _build_inline_span_markers(
    fact_results: list[dict],
    state_claims: list[Claim],
    documents: list[dict],
) -> list[str | None]:
    """v0.8.3-C1: align span markers to `fact_results` for the synthesizer.

    For each fact_result, find the first unused `Claim` in `state.claims`
    whose `.text` matches the fact's "fact" field exactly. If a match
    exists and the Claim has a non-None `evidence_window`, emit the
    marker string `f"[doc_{i}:{start}-{end}]"` where `i` is the
    document index resolved by `_doc_index_for_window`. Otherwise emit
    None.

    v0.8.3-C1b: when `_doc_index_for_window` returns None (empty
    `source_url` or no match in `documents`), emit None here as well —
    never fall back to `[doc_0:start-end]`, because the user-facing
    citation table in `answer_markdown` uses 1-based ids, so a marker
    of `[doc_0:...]` would point at a different document than the
    user expects from the `[N]` marker next to it.

    Defensive: duplicate claim texts are tolerated (first unused match
    wins; subsequent duplicates fall through to None). Missing claims or
    a missing evidence_window both yield None. The list length always
    matches `fact_results` — synthesize() then validates each entry.
    """
    out: list[str | None] = [None] * len(fact_results)
    used: set[int] = set()
    for i, r in enumerate(fact_results):
        if not isinstance(r, dict):
            continue
        fact_text = r.get("fact", "")
        if not fact_text:
            continue
        for j, c in enumerate(state_claims):
            if j in used:
                continue
            if c.text != fact_text:
                continue
            used.add(j)
            window = c.evidence_window
            if window is None:
                out[i] = None
            else:
                # v0.8.3-C1b: strict doc-index resolution. None means
                # "we cannot attribute this claim to any document" —
                # emit no span marker rather than a misleading
                # `[doc_0:start-end]`.
                doc_index = _doc_index_for_window(window, documents)
                if doc_index is None:
                    out[i] = None
                else:
                    out[i] = f"[doc_{doc_index}:{window.offset_start}-{window.offset_end}]"
            break
    return out


def _flatten_verification_results(verdicts: list[dict] | list[Any]) -> list[dict]:
    """Flatten aggregate verification dicts into per-fact result dicts.

    `verify_sources()` returns an aggregate dict of the form:
        {
            "verified_facts": int,
            "total_facts": int,
            "verification_rate": float,
            "verification_details": [
                {"fact": str, "verdict": str, "supporting_sources": [...], ...},
                ...
            ],
            ...
        }

    But `synthesize()` (and `review()`) expect `results` to be a list of
    per-fact dicts, not a list of aggregate dicts. The runner stores the
    aggregate in `state.verdicts` (useful for audit / coverage), but for
    synthesis we need to flatten.

    This is the data-flow fix from the v0.8.1 review (Phase A #1):
    before this helper, `synthesize()` saw `total=1` aggregate instead of
    `total=N` facts, and coverage/confidence were mathematically wrong.

    The input type is `list[dict] | list[Any]` because in test scenarios
    we may pass malformed aggregates (None, strings) to verify the
    function is robust. The runtime isinstance() check handles both.
    """
    out: list[dict] = []
    for v in verdicts or []:
        if not isinstance(v, dict):
            continue
        details = v.get("verification_details", [])
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict):
                    out.append(d)
    return out


# ========================================================================
# Main entry point
# ========================================================================


def run_research(
    query: str,
    *,
    approved_plan: bool = False,
    max_iterations: int = 1,
    use_llm: bool = False,
    top_n: int = 4,
) -> ResearchResult:
    """Run the deep research pipeline for a user query.

    Args:
        query: the raw user query.
        approved_plan: if False, plans with needs_confirmation=True are
                       returned with status="needs_confirmation" instead
                       of being dispatched. Set True after user OK.
        max_iterations: number of search→evidence→verify passes (default 1;
                        iterative deepening is Phase 5, see #020).
        use_llm: whether to allow LLM-conditional enrichment (default False
                 keeps tests offline; in production this is wired through
                 `synthesize.enrich_with_llm`).
        top_n: max number of unique URLs to fetch per pipeline pass.

    Returns:
        ResearchResult with status:
        - "needs_confirmation": plan requires user approval
        - "done": pipeline finished, synthesis + review populated
        - "error": exception caught and wrapped
    """
    t0 = time.time()

    # 1. Build the plan (no network, no LLM)
    try:
        plan = build_research_plan(query)
    except Exception as e:
        return ResearchResult(
            status="error",
            original_query=query,
            error=f"planner failed: {type(e).__name__}: {e}",
            elapsed_sec=time.time() - t0,
        )

    # 2. Confirmation gate (strangler refactor preserves this from the
    #    proposed deep_research_v2 contract in the external review).
    if plan.needs_confirmation and not approved_plan:
        return ResearchResult(
            status="needs_confirmation",
            original_query=query,
            plan=plan,
            elapsed_sec=time.time() - t0,
        )

    # 3. Initialise state from plan
    state = plan_to_state(plan)
    documents: list[dict] = []
    all_hits: list[dict] = []

    # 4. Pipeline passes (max_iterations; default 1)
    # Iterative deepening loop: after each pass, run gap analysis. If gaps
    # are detected and we have iterations left, add gap-fill tasks and
    # re-run. If no gaps (or max_iterations reached), finalise.
    #
    # v0.8.1 Phase B hardening:
    #   - We do NOT mutate plan.search_tasks. The plan is treated as
    #     immutable. Gap-fill tasks live in a local `pending_tasks` queue.
    #   - We dedup tasks and URLs across iterations so we never re-search
    #     or re-fetch the same work. v0.8.0 had a bug where iteration 2
    #     re-ran every original task, doubling the SearXNG load.
    try:
        # The pending queue: starts with all plan tasks, then accumulates
        # gap-fill tasks across iterations. Each iteration dispatches
        # `current_tasks` (a snapshot of the queue) and then resets the
        # queue to receive the next round of gap-fill tasks.
        pending_tasks: list[SearchTask] = list(plan.search_tasks)
        seen_task_keys: set[tuple] = {_task_key(t) for t in plan.search_tasks}
        seen_urls: set[str] = set()

        for iteration in range(max_iterations):
            state.iterations = iteration + 1

            # Snapshot what to dispatch this iteration. After dispatch,
            # `pending_tasks` will be repopulated with the NEXT round
            # of gap-fill tasks (which we'll only run if there's a
            # next iteration).
            current_tasks = pending_tasks
            pending_tasks = []

            # 4a. Search + fetch for each (new) task only
            iteration_hits: list[dict] = []
            for task in current_tasks:
                hits = _dispatch_search_task(task, max_results=top_n * 2)
                for h in hits:
                    h["_task_priority"] = task.priority
                    h["_task_rationale"] = task.rationale
                    iteration_hits.append(h)

            all_hits.extend(iteration_hits)
            deduped_hits = _dedup_hits_by_canonical(iteration_hits)
            # Cross-iteration URL dedup: only fetch URLs we haven't seen.
            # v0.8.1.1: fetch a wider pool (top_n * 3) so ranking has
            # headroom. The actual top-N selection happens AFTER fetch
            # and rank, so position bias from SearXNG order doesn't
            # silently cap the candidate set.
            candidate_urls: list[str] = []
            for h in deduped_hits:
                u = h.get("url", "")
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    candidate_urls.append(u)
                if len(candidate_urls) >= top_n * 3:
                    break

            if not candidate_urls:
                # Nothing new to fetch; record the gap and break the loop.
                state.gaps.append("no_search_results")
                break

            iter_documents = _fetch_documents(candidate_urls)
            # v0.8.1.1: rank documents by combined source_score before
            # selecting top1 or feeding synthesis. This is the fix for
            # ChatGPT P1 #001 ("top-1 = documents[0]" was just URL order).
            iter_documents = rank_documents(iter_documents, query)
            # Cap to top_n AFTER ranking (was top_n before fetch).
            iter_documents = iter_documents[:top_n]
            documents.extend(iter_documents)

            # 4b. Extract claims from documents
            all_claims, claims_meta = _extract_claims_from_documents(iter_documents, query)
            # Phase 4 (#019) — span-level citations. Augment `state.claims`
            # with typed `Claim` objects + evidence windows. This is a
            # separate pass from the legacy string extraction (which feeds
            # `synthesize()` / `verify_sources()`). It's safe to run both:
            # the typed pass is pure-stdlib and < 1ms per claim.
            typed_claims = _extract_typed_claims_with_citations(iter_documents, query)
            state.claims.extend(typed_claims)
            state.evidence.extend(c.evidence_window for c in typed_claims if c.evidence_window is not None)

            # 4c. Verification (4-level + conditional LLM)
            # v0.8.1.1: iter_documents is now sorted by source_score desc
            # (see rank_documents() above), so top1 is the best source by
            # combined score, NOT "the URL we happened to fetch first".
            if iter_documents:
                top1 = iter_documents[0]
                others = iter_documents[1:]
                verification = verify_sources(
                    top1,
                    others,
                    query,
                    time_range=plan.intent.time_range,
                    use_llm=use_llm,  # v0.8.1 Phase A #3: honour the flag
                )
                state.verdicts.append(verification)
            else:
                state.verdicts.append(
                    {
                        "verified_facts": 0,
                        "total_facts": 0,
                        "verification_rate": 0.0,
                        "verification_details": [],
                        "llm_enhanced": False,
                        "llm_verified_count": 0,
                        "llm_latency": 0.0,
                        "llm_error": None,
                    }
                )

            # 4d. Gap analysis (NEW in Phase 5)
            # Update state with the latest snapshot so analyze_gaps sees
            # the cumulative picture, not just this iteration.
            state.search_hits = all_hits
            state.documents = documents
            gaps = analyze_gaps(state)
            for g in gaps:
                state.gaps.append(f"{g.kind}: {g.detail}")

            # 4e. If we have iterations left AND gaps exist, queue
            #     gap-fill tasks for the NEXT iteration. Dedup against
            #     `seen_task_keys` so we never re-run a task we already
            #     dispatched. Plan is NOT mutated.
            if iteration + 1 < max_iterations and gaps:
                new_tasks = gaps_to_search_tasks(
                    gaps,
                    original_query=query,
                    route=plan.intent.route,
                    language=plan.adapted.get("language", "en"),
                )
                for nt in new_tasks:
                    key = _task_key(nt)
                    if key not in seen_task_keys:
                        seen_task_keys.add(key)
                        pending_tasks.append(nt)
            # else: no more iterations OR no gaps → loop ends naturally
    except Exception as e:
        return ResearchResult(
            status="error",
            original_query=query,
            plan=plan,
            state=state,
            error=f"pipeline failed: {type(e).__name__}: {e}",
            elapsed_sec=time.time() - t0,
        )

    # 5. Stash raw hits in state (if not already done in 4d)
    state.search_hits = all_hits
    state.documents = documents

    # 6. Synthesis + critical review
    try:
        # Collect a flat claims list (from last iteration) for synthesis
        flat_claims, _ = _extract_claims_from_documents(documents, query)

        # Flatten aggregate verification dicts into per-fact result dicts
        # so synthesize() sees N fact-results, not 1 aggregate dict. This
        # is the v0.8.1 Phase A #1 fix (synthesis contract mismatch).
        fact_results = _flatten_verification_results(state.verdicts)

        # synthesize() needs (query, claims, results, source_candidates).
        # claims are aligned with results 1:1 — `claims[i]` corresponds to
        # `results[i]`. When `results` is empty, fall back to the legacy
        # string-claims list (for backward compat with offline smoke tests
        # that don't go through verification).
        synth_claims = [r.get("fact", "") for r in fact_results] if fact_results else flat_claims

        # v0.8.3-C1: build per-fact span markers aligned to `fact_results`.
        # We match by exact fact text against `state.claims` (defensive
        # against duplicates: first unused match wins). For each fact
        # with a matching Claim and a non-None evidence_window, emit a
        # marker string of the form `[doc_<i>:<start>-<end>]`. No match
        # → None. This list is what synthesize() will validate by index
        # and append to confirmed bullets. `coverage["inline_citations"]`
        # is left untouched for downstream consumers.
        span_markers = _build_inline_span_markers(fact_results, state.claims, documents)

        synth = synthesize(
            query=query,
            claims=synth_claims,
            results=fact_results,
            source_candidates=documents,
            inline_span_markers=span_markers,
        )

        # Phase 4 (#019) — span-level citation stats. Decorate the synthesis
        # coverage dict with citation information so downstream consumers
        # (eval.py, e2e_falcon9, LLM prompts) can read it without touching
        # the synthesis contract. We MUTATE `synth.coverage` (a dict) instead
        # of replacing `synth` — `synth` is a foreign dataclass, mutation
        # is the safest extension point.

        # Expose aggregate vs per-fact counts in coverage — always, even
        # when state.claims is empty. This is meta-information about the
        # pipeline shape, not about the claims themselves, so it should
        # be available regardless of whether the extractor found anything.
        if not isinstance(synth.coverage, dict):
            synth.coverage = {}
        synth.coverage["verification_aggregate_count"] = len(state.verdicts)
        synth.coverage["verification_fact_count"] = len(fact_results)
        # v0.8.1 Phase B — audit trail for iterative deepening.
        # `seen_task_keys` may not be defined if the loop never entered
        # (e.g. confirmation gate tripped). Use `get` with default.
        try:
            synth.coverage["iterations_executed"] = state.iterations
            synth.coverage["unique_tasks_dispatched"] = len(seen_task_keys)
            synth.coverage["unique_urls_fetched"] = len(seen_urls)
        except NameError:
            # Loop never ran (confirmation gate tripped); leave coverage
            # without the audit fields.
            pass

        if state.claims:
            stats = citation_stats(state.claims)
            synth.coverage["citation_stats"] = stats
            # Also list inline-formatted cited claims (for debugging /
            # downstream prompt assembly). These look like:
            #   "5 июня 2026 [doc_0:8-19]"
            #
            # v0.8.3-C1c: coverage["inline_citations"] is provenance data,
            # not just a debug field. When the document index cannot be
            # resolved (empty `source_url` or no match in `documents`),
            # skip the entry entirely — never fabricate
            # [doc_0:start-end] as a fallback. This mirrors the strict
            # user-facing span-marker behavior in
            # `_build_inline_span_markers` (C1b); a fabricated [doc_0]
            # would be misleading because the citation table id `[N]` in
            # `answer_markdown` uses 1-based offsets, so a doc index of 0
            # there would point at a *different* document than
            # `documents[0]`. The previous C1b `or 0` fallback is
            # intentionally removed here.
            inline: list[str] = []
            for c in state.claims:
                if c.evidence_window is None:
                    continue
                doc_index = _doc_index_for_window(c.evidence_window, documents)
                if doc_index is None:
                    continue
                inline.append(format_cited_claim(c, c.evidence_window, doc_index=doc_index))
            synth.coverage["inline_citations"] = inline
            synth.coverage["unverified_claims"] = [c.text for c in state.claims if c.evidence_window is None]

        # review() is deterministic critic; needs synthesis + claims + results + source_candidates.
        # Same flatten rule as synthesize() — review expects per-fact results.
        rev = review(
            synthesis=synth,
            claims=synth_claims,
            results=fact_results,
            source_candidates=documents,
        )
    except Exception as e:
        return ResearchResult(
            status="error",
            original_query=query,
            plan=plan,
            state=state,
            error=f"synthesis/review failed: {type(e).__name__}: {e}",
            elapsed_sec=time.time() - t0,
        )

    return ResearchResult(
        status="done",
        original_query=query,
        plan=plan,
        state=state,
        synthesis=synth,
        review=rev,
        elapsed_sec=time.time() - t0,
    )


# ========================================================================
# Convenience: deep_research_v2 — alias with the exact name from the
# external review's contract.
# ========================================================================


def deep_research_v2(
    query: str,
    *,
    approved_plan: bool = False,
    max_iterations: int = 1,
    use_llm: bool = False,
) -> ResearchResult:
    """Alias for `run_research`. Matches the proposed name in the
    external review (`research-runner.md` §Phase 3, function `deep_research_v2`).
    """
    return run_research(
        query,
        approved_plan=approved_plan,
        max_iterations=max_iterations,
        use_llm=use_llm,
    )
