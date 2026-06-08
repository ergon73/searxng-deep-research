# v0.8.0 — Serious deep research pipeline (Phases 0–5)

Released 2026-06-08 · [`9133c2f`](https://github.com/ergon73/searxng-deep-research/commit/9133c2f) · tag `v0.8.0`

This release ships the full pipeline that the external ChatGPT review
recommended for "serious deep research": **typed state**, **planner with
falsification**, **runner with confirmation gate**, **span-level citations**,
and **iterative deepening** — all built on a self-hosted SearXNG backend
with no paid dependencies.

## What's new

### Phase 0 — Release hygiene ✅
- `pyproject.toml` bumped to `0.8.0`, real source URL
- `ARCHITECTURE.md` rewritten with honest baseline (no fabricated "94%→96%" numbers)
- Portable scripts: `Path(__file__).resolve().parents[1] / "src"` everywhere
- `.gitignore` covers `DR-*.txt`, `hermes-explore-*.txt`, `.env*`, caches, venv
- `config/.env.example` added with `SEARXNG_SECRET` + `SEARXNG_SETTINGS_PATH`
- `eval.py` QS weights: `0.45 coverage / 0.22 citation_density / 0.22 verification / 0.11 relevance` (no penalty for `needs_confirmation`)

### Phase 1 (#016) — Typed state ✅
New `src/models.py` (stdlib `dataclasses`, no Pydantic):
- `SearchTask` (frozen) — one unit of search work
- `Claim` (frozen) — atomic assertion extracted from a source
- `ResearchState` (mutable) — pipeline container with `search_tasks`, `search_hits`, `documents`, `claims`, `evidence`, `verdicts`, `gaps`, `iterations`
- `EvidenceWindow` reused from `evidence.py` (no duplication)
- **Minimum 4 classes** (not 9 as the review suggested — the other 5 had no consumers)
- 15 unit tests in `tests/test_models.py`

### Phase 2 (#017) — Planner with falsification ✅
New `src/planner.py`:
- `build_research_plan(query) -> ResearchPlan` (frozen, hashable)
- Composes `adapt_query()` (main + 3 alts) + `classify_intent()` (variants) into typed `SearchTask` list
- Priorities: 100=main, 80=alt_queries, 70=route_variants
- **Falsification tasks** (priority 40) for `news` / `security` / `product` / `technical` routes only (deterministic hash-based term rotation, no LLM, no random)
- `plan_to_state(plan)` wires plan into mutable `ResearchState`
- Confirmation gate: `True` if `adapted.needs_confirmation OR intent.routing_warning`
- 27 unit tests in `tests/test_planner.py` (8 test classes)

### Phase 3 (#018) — Runner with confirmation gate ✅
New `src/research_runner.py`:
- `run_research(query, *, approved_plan=False, max_iterations=1, use_llm=False) -> ResearchResult`
- `deep_research_v2()` alias matches the contract from the external review
- Strangler refactor — legacy `deep_research()` is **NOT modified** (test verifies signature unchanged)
- Status enum: `needs_confirmation` / `done` / `error`
- Composition: `planner → web_search → fetch_url → verify_sources → synthesize → review`
- All side-effect functions monkeypatch-able for offline tests
- Typed `ResearchResult` with `to_dict()` JSON serialisation
- 31 unit tests in `tests/test_research_runner.py` (9 test classes)
- Live smoke verified for 5 scenarios (confirmation gate, error handling, alias, JSON round-trip)

### Phase 4 (#019) — Span-level citations ✅
New `src/citations.py`:
- `find_span(claim, text)` — 3-level search: direct → whitespace-normalized → fuzzy-prefix
- `build_evidence_window(claim, doc)` — attaches `EvidenceWindow` with `offset_start` / `offset_end` + `source_url` / `source_title` / `score`
- `format_cited_claim(claim, win, idx)` — emits `"<text> [doc_N:start-end]"` markers
- `citation_stats(claims)` — `{total, cited, uncited, stub, coverage, non_stub_coverage}`
- `assert_citations_complete(claims, ...)` — invariant enforcer
- `EvidenceWindow` extended (backward-compat: `source_url` / `source_title` / `score` with defaults)
- `Claim` extended (backward-compat: `is_stub` + `evidence_window`; frozen, set via `dataclasses.replace`)
- Runner integration: `_extract_typed_claims_with_citations()` runs alongside legacy string extraction
- After synthesis, runner decorates `synth.coverage` with `citation_stats` / `inline_citations` / `unverified_claims`
- 38 unit tests in `tests/test_citations.py` (8 test classes) + 8 integration tests
- Live smoke: 4/4 claims cited at 100% coverage with correct `[doc_0:start-end]` markers

### Phase 5 (#020) — Gap analysis + iterative deepening ✅
New `src/gap_analysis.py`:
- `analyze_gaps(state) -> list[ResearchGap]` — detects 6 gap kinds:
  - `too_few_sources` (docs < 3)
  - `no_search_results` (0 hits; **not** retried — same query would fail again)
  - `low_source_diversity` (unique domains < 2)
  - `too_many_unsupported_claims` (ratio > 40%)
  - `contradictions_unresolved` (CONFLICTING or rate < 20%)
  - `low_confidence` (top-1 source_score < 0.5)
- `gaps_to_search_tasks(gaps, ...)` — maps to priority-50 retry tasks
- Runner loop: after each pass, run `analyze_gaps`; if gaps and `max_iterations` not reached, add gap-fill tasks and continue; early-exit when no gaps
- Thresholds centralised: `MIN_DOCUMENTS=3`, `MIN_UNIQUE_DOMAINS=2`, `MIN_TOP1_CONFIDENCE=0.5`, `MAX_UNSUPPORTED_CLAIM_RATIO=0.4`
- 40 unit tests in `tests/test_gap_analysis.py` (6 test classes) + 4 runner-integration tests
- Live smoke: 3 gaps detected, 3 gap-fill tasks generated with priorities and rationale

## Test growth

| Phase | Tests added | Cumulative |
|---|---|---|
| Pre-Phases (legacy) | — | 404 |
| Phase 0 | 0 (refactor only) | 404 |
| Phase 1 (#016) | +15 | 419 |
| Phase 2 (#017) | +27 | 446 |
| Phase 3 (#018) | +31 | 477 |
| Phase 4 (#019) | +46 (38 unit + 8 integration) | 523 |
| Phase 5 (#020) | +40 | **563** |

**Total runtime**: ~26s for full suite. Portable (`cp -a` + tmp) round-trip clean.

## Backward compatibility

- `deep_research()` legacy function: **untouched** (signature test verifies)
- `Claim`, `EvidenceWindow`: new fields are **optional with defaults** — every existing call site keeps working
- `SearchTask.engine_preference`: not added in v0.8.0 (will arrive with Phase 6 / Exa-Tavily integration in v0.9.0)
- Default `max_iterations=1` — Phase 5 deepening is opt-in, no behaviour change for legacy callers
- Default `engine_preference=["searxng"]` will arrive in v0.9.0 (no v0.8.0 call site changes)

## Stats

- **+3,793 / -64** lines across 23 files since `16e67d8` (initial snapshot)
- **+159 tests** since pre-Phase-0 baseline
- **6 commits** for Phases 0-5, all on `main`, all pushed to `ergon73/searxng-deep-research`
- **Dependencies**: stdlib only (no Pydantic, no httpx, no new requirements)
- **No paid APIs** — entirely self-hosted

## What's next (v0.9.0 candidates)

- **Exa + Tavily providers** (keys verified working, integration pending) — `SearchTask.engine_preference` will allow per-task provider selection
- **LLM enrichment** for synthesis (`use_llm=True` already in runner contract, wire-up deferred)
- **E2E benchmark** with real SearXNG + real queries (eval currently runs against synthetic baseline)
- **WONTFIX** (per `ISSUES.md`): Tavily/Exa/Firecrawl in v0.8.0 (premature), Qdrant/Neo4j (no consumer), multi-agent roles as separate processes (overkill), 5-split eval (premature)

## Verification commands

```bash
# Full suite
PYTHONPATH=src python3 -m pytest -q
# → 563 passed in ~26s

# Just citations
PYTHONPATH=src python3 -m pytest tests/test_citations.py -v
# → 38 passed in ~0.2s

# Live smoke (offline, requires monkeypatched network)
PYTHONPATH=src python3 scripts/e2e_falcon9.py
```

## Credits

- Architecture review: ChatGPT (external review, distilled in `/tmp/hermes-recomendation-08062026.txt` — not committed)
- Implementation: Ерёма (Hermes Agent) + Georgy Belyanin
- License: see `LICENSE` (inherits project default)
