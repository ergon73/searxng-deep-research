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
- Iterative deepening is intentionally NOT in v0.8.0 (it's #020, Phase 5).
  `max_iterations=1` is the default and what the tests use.

Spec: ~/.hermes/plans/ISSUES.md #018.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Local imports (typed state + planner)
from models import ResearchState, SearchTask

# Primitive functions (legacy module — we re-use, do not modify)
from hermes_deepresearch import (
    fetch_url,
    web_search,
    _extract_facts,
    verify_sources,
    canonical_url,
    MAX_CONCURRENT_FETCH,
    MAX_CONTENT_CHARS,
)

# Newer stages
from synthesis import synthesize
from critical_review import review, Synthesis, ReviewResult  # type: ignore

# Planner
from planner import build_research_plan, ResearchPlan, plan_to_state


# ========================================================================
# Public result type
# ========================================================================


@dataclass
class ResearchResult:
    """Public output of `run_research()`. Typed, JSON-serialisable."""
    status: str                            # "needs_confirmation" | "done" | "error"
    original_query: str
    plan: Optional[ResearchPlan] = None    # set for "needs_confirmation" and "done"
    state: Optional[ResearchState] = None  # final state, set for "done"
    synthesis: Optional[Synthesis] = None  # set for "done"
    review: Optional[ReviewResult] = None  # set for "done"
    error: Optional[str] = None            # set for "error"
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


def _fetch_documents(
    urls: list[str], *, max_chars: int = MAX_CONTENT_CHARS
) -> list[dict]:
    """Fetch a list of URLs in parallel, return list of {url, text, title, ...}.

    Mirrors the parallel-fetch logic in legacy `deep_research()`.
    """
    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCH) as ex:
        futures = {ex.submit(fetch_url, u, max_chars=max_chars): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            u = futures[fut]
            fr = fut.result() or {"url": u, "error": "fetch returned None"}
            if "url" not in fr:
                fr["url"] = u
            out.append(fr)
    return out


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
    strings. In Phase 4 (span-level citations) this becomes structured
    `Claim` objects with subject/predicate/value/unit; we keep the dict shape
    for now to avoid changing the contract of `synthesize()` and `verify_sources()`.
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
    try:
        for iteration in range(max_iterations):
            state.iterations = iteration + 1

            # 4a. Search + fetch for each task
            iteration_hits: list[dict] = []
            for task in plan.search_tasks:
                hits = _dispatch_search_task(task, max_results=top_n * 2)
                for h in hits:
                    h["_task_priority"] = task.priority
                    h["_task_rationale"] = task.rationale
                    iteration_hits.append(h)

            all_hits.extend(iteration_hits)
            deduped_hits = _dedup_hits_by_canonical(iteration_hits)
            top_urls = [h["url"] for h in deduped_hits[:top_n]]

            if not top_urls:
                # Nothing to fetch; record the gap and break the loop.
                state.gaps.append("no_search_results")
                break

            iter_documents = _fetch_documents(top_urls)
            documents.extend(iter_documents)

            # 4b. Extract claims from documents
            all_claims, claims_meta = _extract_claims_from_documents(
                iter_documents, query
            )
            for c in all_claims:
                # We don't promote strings to typed `Claim` here yet (Phase 1
                # #016 deferred `Claim` from being used at runtime; synthesis
                # and verify_sources still want string claims). Tracked in
                # #019 (span-level citations).
                pass

            # 4c. Verification (4-level + conditional LLM)
            if iter_documents:
                top1 = iter_documents[0]
                others = iter_documents[1:]
                verification = verify_sources(
                    top1, others, query,
                    time_range=plan.intent.time_range,
                )
                state.verdicts.append(verification)
            else:
                state.verdicts.append({
                    "verified_facts": 0, "total_facts": 0,
                    "verification_rate": 0.0, "verification_details": [],
                    "llm_enhanced": False, "llm_verified_count": 0,
                    "llm_latency": 0.0, "llm_error": None,
                })
    except Exception as e:
        return ResearchResult(
            status="error",
            original_query=query,
            plan=plan,
            state=state,
            error=f"pipeline failed: {type(e).__name__}: {e}",
            elapsed_sec=time.time() - t0,
        )

    # 5. Stash raw hits in state
    state.search_hits = all_hits
    state.documents = documents

    # 6. Synthesis + critical review
    try:
        # Collect a flat claims list (from last iteration) for synthesis
        flat_claims, _ = _extract_claims_from_documents(documents, query)

        # synthesize() needs (query, claims, results, source_candidates)
        synth = synthesize(
            query=query,
            claims=flat_claims,
            results=state.verdicts,
            source_candidates=documents,
        )

        # review() is deterministic critic; needs synthesis + claims + results + source_candidates
        rev = review(
            synthesis=synth,
            claims=flat_claims,
            results=state.verdicts,
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
