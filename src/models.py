"""
Typed state skeleton for the research pipeline (Phase 1, v0.8.0).

This module defines the *minimum* set of typed objects needed to migrate
`deep_research()` away from dict-soup. We deliberately do NOT mirror the
9-class structure proposed in the external review (see `.hermes/plans/ISSUES.md`
#016) — start with 4, add more when there's a concrete consumer.

Design constraints:
- No new dependencies (use stdlib `dataclasses`, not Pydantic).
- Reuse existing `EvidenceWindow` from `evidence.py` instead of redefining.
- All fields default-constructible (no required args except identity).
- `to_dict()` for JSON serialisation; round-trip is testable.
- `frozen=True` on value-like types (SearchTask, Claim); mutable on container
  (ResearchState, since stages append to its lists).

This module is **advisory**: it does not change any existing function. Stages
that want to adopt typed state can convert at the boundary; the rest of the
pipeline keeps using dicts for now.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from evidence import EvidenceWindow

# Route values intentionally mirror `routing.py` Intent.route vocabulary
# (general, news, llm_release, forums, docs, academic, github, reviews,
# security, product, technical, wiki). Kept as plain str (not Literal) so
# legacy callers can pass
# any value without breaking — strict enum checks happen elsewhere.
Route = str


@dataclass(frozen=True)
class SearchTask:
    """One unit of search work produced by the planner.

    Identical to "what we'd send to SearXNG" plus a rationale explaining why
    this task exists. Frozen because a planned task is immutable once the
    runner has dispatched it.
    """

    query: str
    route: Route = "general"
    language: str = "auto"
    engines: str | None = None  # comma-separated, e.g. "wikipedia,arxiv"
    categories: str | None = None  # SearXNG categories, e.g. "news"
    time_range: str | None = None  # "day" | "week" | "month" | "year" | None
    priority: int = 0  # higher = dispatched earlier
    rationale: str = ""  # why planner added this task

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Claim:
    """Atomic assertion extracted from a source.

    Carries enough structure for numeric / temporal / entity verification.
    `value` is always a string (we do not coerce types here — extraction may
    be approximate; the verifier decides how to interpret it).

    Phase 4 (#019, span-level citations): `evidence_window` is set by the
    runner after extraction (via `dataclasses.replace`) to point at the
    exact quote in the source that supports the claim. `is_stub` flags
    claims that are placeholders for LLM enrichment (these are exempt
    from the "every claim needs evidence" invariant).
    """

    text: str  # original surface form, e.g. "5 июня 2026"
    subject: str | None = None  # e.g. "Магнитная буря"
    predicate: str | None = None  # e.g. "уровень", "температура"
    value: str | None = None  # e.g. "5", "123 дрона", "красный"
    unit: str | None = None  # e.g. "Гц", "°C", "человек"
    date: str | None = None  # ISO date if known
    location: str | None = None
    polarity: str = "unknown"  # "positive" | "negative" | "unknown"
    is_stub: bool = False  # Phase 4: placeholder flag (LLM-only)
    evidence_window: EvidenceWindow | None = None  # Phase 4: span pointer

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict() handles nested EvidenceWindow via its own to_dict()
        # automatically (dataclasses.asdict recurses), but we keep an
        # explicit branch to control the None case shape.
        if self.evidence_window is None:
            d["evidence_window"] = None
        return d


@dataclass
class ResearchState:
    """Mutable container that the runner passes through pipeline stages.

    Lifecycle:
        1. `ResearchState(original_query=...)` — caller constructs.
        2. `adapted` populated by query_adaptation.
        3. `search_tasks` populated by planner (Phase 2).
        4. `search_hits` / `documents` populated by search + fetch.
        5. `claims` populated by extract_facts; `evidence` list per claim.
        6. `verdicts` populated by verifier.
        7. `gaps` populated by gap_analysis (Phase 5).
        8. `iterations` incremented after each pass.

    `search_hits` and `documents` are kept as dicts for now (matches existing
    pipeline contract). We promote them to typed dataclasses in a later phase
    if there's a concrete win — see `.hermes/plans/ISSUES.md` #016.
    """

    original_query: str
    adapted: dict[str, Any] | None = None  # output of adapt_query()
    search_tasks: list[SearchTask] = field(default_factory=list)
    search_events: list[dict[str, Any]] = field(default_factory=list)
    search_hits: list[dict[str, Any]] = field(default_factory=list)  # legacy dict shape
    documents: list[dict[str, Any]] = field(default_factory=list)  # legacy dict shape
    claims: list[Claim] = field(default_factory=list)
    evidence: list[EvidenceWindow] = field(default_factory=list)  # per-claim windows
    verdicts: list[dict[str, Any]] = field(
        default_factory=list
    )  # legacy shape, will gain ClaimVerdict in Phase 3
    gaps: list[str] = field(default_factory=list)  # free-form gap descriptions
    iterations: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly serialisation. Nested SearchTask/Claim/EvidenceWindow
        get their own `to_dict()` so we don't ship dataclass repr to JSON."""
        return {
            "original_query": self.original_query,
            "adapted": self.adapted,
            "search_tasks": [t.to_dict() for t in self.search_tasks],
            "search_events": self.search_events,
            "search_hits": self.search_hits,
            "documents": self.documents,
            "claims": [c.to_dict() for c in self.claims],
            "evidence": [e.to_dict() for e in self.evidence],
            "verdicts": self.verdicts,
            "gaps": list(self.gaps),
            "iterations": self.iterations,
        }
