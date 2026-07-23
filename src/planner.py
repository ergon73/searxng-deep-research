"""
Research planner for deep research pipeline (Phase 2, v0.8.0).

Composes `adapt_query()` (query_adaptation) + `classify_intent()` (routing) into
a typed list of `SearchTask`s that a runner can dispatch.

This module is **advisory**: it does not call SearXNG or fetch anything.
Callers can inspect the plan, show it to the user, gate it on confirmation,
and only then dispatch.

Design notes:
- We deliberately do NOT include `SearchHit` / `Document` / `ClaimVerdict` here
  (those are pipeline outputs, not plan inputs).
- `ResearchPlan` is a lightweight wrapper around `ResearchState`-style data
  (the same shape we use at runtime, minus hits/claims which haven't been
  produced yet). A future runner will copy this plan into `state.search_tasks`.
- Falsification tasks are **opt-in per route** (news/security/product/technical)
  because adding "criticism" to a general "Falcon 9" query would be noise.
- Confirmation gate: `needs_confirmation=True` if EITHER `adapted.needs_confirmation`
  OR `intent.routing_warning`. The runner can choose to skip dispatch on this.

Spec: ~/.hermes/plans/ISSUES.md #017.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import ResearchState, SearchTask
from query_adaptation import adapt_query
from routing import classify_intent, should_warn_about_routing

# Routes for which we add falsification tasks. These are the routes where
# opposite-evidence (criticism / debunk / опровержение) is most useful:
# - news: current events have corrections and counter-claims
# - security: advisories and CVEs get disputed / retracted
# - product: products have reviews saying "X is bad"
# - technical: frameworks have "why X is wrong" blog posts
# Other routes (general, docs, academic, github, reviews, forums, wiki) get
# NO falsification — the query itself is already a "find the answer" task.
_FALSIFICATION_ROUTES = frozenset({"news", "security", "product", "technical"})

# Falsification terms. We append one of these (rotated) to the original query
# to surface dissenting / corrective sources. Rotating prevents all variants
# from being identical; we only add ONE falsification task per plan (Phase 2
# budget — more can be added in Phase 5+ with iterative deepening).
_FALSIFICATION_TERMS = (
    " criticism",
    " controversy",
    " debunked OR false",
    " опровержение",
)


@dataclass(frozen=True)
class ResearchPlan:
    """A typed plan that a runner can dispatch.

    Fields:
        original_query: echo of the input query.
        adapted: full output of `adapt_query()` — kept verbatim so callers
                 can show the user what we understood the query to mean.
        intent: the `classify_intent()` result (routing decision).
        search_tasks: ordered list of `SearchTask` (priority descending).
        needs_confirmation: True if EITHER adapted.needs_confirmation OR
                            intent.routing_warning. Runner can gate on this.
        confirmation_reasons: list of human-readable reasons (from adapted
                              and/or intent).
    """

    original_query: str
    adapted: dict[str, Any]
    intent: Any  # routing.Intent — avoid circular import
    search_tasks: list[SearchTask]
    needs_confirmation: bool
    confirmation_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "adapted": self.adapted,
            "intent": {
                "route": self.intent.route,
                "confidence": self.intent.confidence,
                "engines": self.intent.engines,
                "categories": self.intent.categories,
                "time_range": self.intent.time_range,
                "query_variants": list(self.intent.query_variants),
                "routing_warning": self.intent.routing_warning,
            },
            "search_tasks": [t.to_dict() for t in self.search_tasks],
            "needs_confirmation": self.needs_confirmation,
            "confirmation_reasons": list(self.confirmation_reasons),
        }


def _falsification_query_for(base_query: str, route: str) -> str:
    """Build ONE falsification query for a plan. Rotates through terms to
    diversify across plans (purely deterministic — no LLM)."""
    # Hash the query+route to a stable index. No randomness; tests must be
    # deterministic.
    idx = hash((base_query, route)) % len(_FALSIFICATION_TERMS)
    return f"{base_query}{_FALSIFICATION_TERMS[idx]}"


def build_research_plan(query: str) -> ResearchPlan:
    """Build a typed `ResearchPlan` from a raw user query.

    Pure function. No network, no LLM call, no filesystem access. Safe in
    dry-run / preview mode.

    Args:
        query: the raw user query (any length, any language).

    Returns:
        ResearchPlan with 1+ SearchTasks. If `needs_confirmation` is True,
        the runner should not dispatch without explicit user approval.

    Plan composition (priority ordering, highest first):
        100  main query (from adapt_query)
         80  alt queries (from adapt_query, up to 3)
         70  route-specific variants (from classify_intent, route-dependent)
         40  falsification query (only for news/security/product/technical)

    Confirmation gate: True if EITHER adapted.needs_confirmation OR
    intent.routing_warning.
    """
    # 1. Run both upstream stages. These are independent and pure.
    adapted = adapt_query(query)
    intent = classify_intent(query)

    # 2. Extract the data we need from adapted. Fallback to empty if the
    #    adapt_query contract ever changes (defensive).
    main_query = adapted.get("main_query", "").strip()
    alt_queries = list(adapted.get("alt_queries", []) or [])
    language = adapted.get("language", "en")
    adapted_needs = bool(adapted.get("needs_confirmation", False))
    adapted_reasons = list(adapted.get("confirmation_reason", []) or [])

    # 3. Routing data
    intent_warning = should_warn_about_routing(intent)
    route = intent.route
    engines = intent.engines
    categories = intent.categories
    time_range = intent.time_range
    query_variants = list(intent.query_variants or [])

    if route == "llm_release":
        # The generic query adapter optimizes ordinary fact questions and can
        # remove words such as "LLM", "new" and "last 48 hours". Those are
        # hard constraints for Radar, so keep the raw request and let the
        # vertical source variants provide breadth instead of generic alts.
        main_query = query.strip()
        alt_queries = []
        adapted_reasons = [
            reason for reason in adapted_reasons if not str(reason).startswith("dropped_critical_terms:")
        ]
        adapted_needs = bool(adapted_reasons)

    # 4. Build SearchTasks. Order matters: priority desc, and we want main
    #    first for human-readable display.
    tasks: list[SearchTask] = []

    if main_query:
        tasks.append(
            SearchTask(
                query=main_query,
                route=route,
                language=language,
                engines=engines,
                categories=categories,
                time_range=time_range,
                priority=100,
                rationale="main adapted query (from adapt_query)",
            )
        )

    for alt in alt_queries:
        alt = alt.strip()
        if not alt:
            continue
        tasks.append(
            SearchTask(
                query=alt,
                route=route,
                language=language,
                engines=engines,
                categories=categories,
                time_range=time_range,
                priority=80,
                rationale="alt query (orthogonal angle from adapt_query)",
            )
        )

    for variant in query_variants:
        variant = variant.strip()
        if not variant:
            continue
        tasks.append(
            SearchTask(
                query=variant,
                route=route,
                language=language,
                engines=engines,
                categories=categories,
                time_range=time_range,
                priority=70,
                rationale="route-specific variant (from classify_intent)",
            )
        )

    # 5. Falsification: only for routes where opposite-evidence is useful.
    if route in _FALSIFICATION_ROUTES and main_query:
        fals_q = _falsification_query_for(main_query, route)
        tasks.append(
            SearchTask(
                query=fals_q,
                route=route,
                language=language,
                engines=engines,
                categories=categories,
                time_range=time_range,
                priority=40,
                rationale=f"falsification for route={route} (criticism/опровержение)",
            )
        )

    # 6. Confirmation gate: True if EITHER side asks for it. Reasons are
    #    merged so the runner can show the user the full picture.
    needs_confirmation = adapted_needs or intent_warning
    reasons: list[str] = []
    reasons.extend(adapted_reasons)
    if intent_warning and "routing_warning" not in reasons:
        reasons.append("routing_warning")

    # 7. Edge case: no tasks at all (empty query, all-empty inputs).
    #    We still return a valid plan with needs_confirmation=True so the
    #    runner doesn't dispatch an empty search.
    if not tasks:
        needs_confirmation = True
        reasons.append("planner produced no tasks (empty query or no viable entities)")

    return ResearchPlan(
        original_query=query,
        adapted=adapted,
        intent=intent,
        search_tasks=tasks,
        needs_confirmation=needs_confirmation,
        confirmation_reasons=reasons,
    )


def plan_to_state(plan: ResearchPlan) -> ResearchState:
    """Convert a `ResearchPlan` into an initial `ResearchState` for a runner.

    Convenience helper. Runner typically does:
        plan = build_research_plan(query)
        state = plan_to_state(plan)
        # ... run pipeline stages, mutating state ...
    """
    return ResearchState(
        original_query=plan.original_query,
        adapted=plan.adapted,
        search_tasks=list(plan.search_tasks),
    )
