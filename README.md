# searxng-deep-research

Local SearXNG-based research fetcher with 4-level fact verification and optional LLM cross-check.

**Version:** v0.8 (5 June 2026)
**Status:** working prototype, see `ISSUES.md` and `SECURITY.md` for current state.

## What this is

- `src/hermes_deepresearch.py::deep_research()` — entry point
- `src/evidence.py`, `src/routing.py`, `src/synthesis.py`, `src/critical_review.py`, `src/llm_verifier.py` — pipeline stages
- `src/models.py` — typed state skeleton (Phase 1, v0.8.0: `SearchTask`, `Claim`, `EvidenceWindow`, `ResearchState`)
- `src/planner.py` — research plan builder (Phase 2, v0.8.0: `build_research_plan()` composes `adapt_query()` + `classify_intent()` into typed `SearchTask`s with falsification for news/security/product/technical)
- `src/research_runner.py` — pipeline orchestrator (Phase 3, v0.8.0: `run_research()` / `deep_research_v2()` — strangler refactor of `deep_research()` using typed `ResearchPlan` + `ResearchState`, with confirmation gate)
- `src/citations.py` — span-level citation machinery (Phase 4, v0.8.0: `find_span()` locates `Claim.text` inside document, `build_evidence_window()` produces an `EvidenceWindow` with `[start,end]` offsets + source URL/title/score, `format_cited_claim()` emits `[doc_N:start-end]` markers, `citation_stats()` reports coverage, `assert_citations_complete()` enforces the invariant that every non-stub claim has an `evidence_window`)
- `src/gap_analysis.py` — gap detection + iterative deepening (Phase 5, v0.8.0: `analyze_gaps()` detects too_few_sources / low_diversity / low_confidence / contradictions / unsupported_claims; `gaps_to_search_tasks()` converts to priority-50 retry tasks)
- `src/redact.py` — secret redaction (mandatory before archive/chat)
- `src/hermes_searxng.py` — SearXNG JSON helper
- `scripts/e2e_falcon9.py` — 8-stage end-to-end smoke (~2s)
- `scripts/eval.py` + `data/eval_set.json` — synthetic eval set
- `tests/` — 397 portable tests, run with `PYTHONPATH=src python3 -m pytest`

## Read these first

- `AGENTS.md` — project rules and security policy for any coding agent
- `SECURITY.md` — threat model, hard rules, secret redaction policy
- `ARCHITECTURE.md` — pipeline diagram and data flow
- `ISSUES.md` — known gaps, open/closed issues, verification commands
- `INSTALL.md` — how to run the SearXNG + Valkey stack locally

## Quick start (for review)

```bash
# Tests (offline, no LLM calls)
PYTHONPATH=src python3 -m pytest

# End-to-end smoke
PYTHONPATH=src python3 scripts/e2e_falcon9.py

# Eval run (offline)
PYTHONPATH=src python3 scripts/eval.py
```

## Privacy

This is a **public repository**. It contains a development snapshot of
the project. The `.gitignore` excludes all real secrets (`.env_llm`, `.env_proxy`),
local caches, and external review snapshots. Only example configs and synthetic
eval data are committed.
