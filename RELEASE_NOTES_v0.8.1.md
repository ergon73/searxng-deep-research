# v0.8.1 — Hardening release (3 P0 + 2 P1 fixes from external review)

Released 2026-06-08 · [`ecb9239`](https://github.com/ergon73/searxng-deep-research/commit/ecb9239) · tag `v0.8.1`

This release is a direct response to the [external ChatGPT code review](docs/CHATGPT_REVIEW_REQUEST_v0.8.0.md) of v0.8.0. The review flagged 5 P0 and 9 P1 issues; this release ships the highest-priority fixes across three phases.

**The core principle:** the architecture from v0.8.0 is sound, but the runner had data-flow bugs that made the e2e report look correct while being mathematically wrong. v0.8.1 fixes the bugs without changing the architecture.

## What's in v0.8.1

### Phase A — runner correctness (3 P0 fixes)

| # | Bug | Fix |
|---|---|---|
| 1 | `synthesize()` received **aggregate** verification dicts (1 of them) instead of **per-fact** result dicts (N of them). Coverage was always `total=1`, confidence always `0.0`, unsupported always `[]` | New `_flatten_verification_results()` extracts per-fact dicts from `verification_details` before calling `synthesize()` and `review()`. Audit trail via `synth.coverage["verification_fact_count"]` |
| 2 | `_fetch_documents()` used `as_completed()` which reorders by completion time. `top1` = fastest URL, not highest-ranked source. `verify_sources()` was verifying a random document | `_fetch_documents()` now collects into `by_url` dict and re-emits in input URL order. `top1` = rank-1 source, not fastest fetch |
| 3 | `use_llm=False` parameter to `run_research()` was ignored — `verify_sources()` default `use_llm=True` was silently used. LLM could fire in offline tests | Runner now passes `use_llm=use_llm` through. Privacy/cost policy now actually respected |

13 regression tests added (would have FAILED on v0.8.0).

### Phase B — iterative deepening hardening (2 P1 fixes)

| # | Bug | Fix |
|---|---|---|
| 1 | `plan.search_tasks.extend(new_tasks)` mutated the frozen `ResearchPlan` (Python's `frozen=True` doesn't protect contents of mutable fields) | Local `pending_tasks: list[SearchTask]` queue. Plan is treated as immutable; gap-fill tasks live separately |
| 2 | Each iteration re-iterated over `plan.search_tasks` — on iteration 2, every original main + alts + falsification task was re-dispatched, **doubling** SearXNG load | Each iteration dispatches only `current_tasks` (queue snapshot). Cross-iteration `seen_task_keys` and `seen_urls` sets dedup everything. Audit trail via `synth.coverage["iterations_executed"]`, `unique_tasks_dispatched`, `unique_urls_fetched` |

10 regression tests added (would have FAILED on v0.8.0).

### Phase C — release hygiene (4 P1/P2 fixes)

| # | Issue | Fix |
|---|---|---|
| 1 | `INSTALL.md` claimed `.env_llm` had 3 keys including `SEARXNG_SECRET`, but v0.8.0 split: `config/.env` for Docker, `.env_llm` only for Python verifier | Rewrote Step 3a (`config/.env`) and Step 3b (`.env_llm`) as separate, clearly-scoped steps. Safe bash (no `<...>` placeholders) |
| 2 | `docker-compose.yml` injected `.env_llm` into the SearXNG container, leaking `LLM_API_KEY` / `LLM_MODEL` to a process that doesn't need them | Removed `./.env_llm` from `env_file`. `.env_proxy` stays (SearXNG itself uses it for proxy-required engines) |
| 3 | `pyproject.toml` had `name = "deep-research-project"` but the repo is `searxng-deep-research` | Renamed to `searxng-deep-research`, bumped to `version = "0.8.1"`, expanded description |
| 4 | No CI gate — "works on VPS" was the only safety net | Added `.github/workflows/ci.yml` — runs `pip install -e .[dev] && ruff check src tests scripts && pytest -q` on every push/PR to main |
| 5 | `ARCHITECTURE.md` said Phase 1-5 were "deferred to v0.9" but they actually shipped in v0.8.0 | Marked Phase 1-5 as `~~strikethrough~~` "Already shipped" and moved LangGraph / Exa / Tavily / LLM enrichment to a separate "v0.9.0+ candidates" sub-section |
| 6 | `README.md` linked to `ISSUES.md` (which doesn't exist at the repo root — it lives at `.hermes/plans/ISSUES.md`) | Updated to point to `.hermes/plans/ISSUES.md`. Added v0.8.0 release notes, self-review, and ChatGPT review request to the "Read these first" list |

## Test growth

| Phase | Tests added | Cumulative |
|---|---|---|
| Pre-v0.8.1 (v0.8.0) | — | 576 |
| Phase A (commit `580553d`) | +13 | 589 |
| Phase B (commit `fada04c`) | +10 | 599 |
| Phase C (commit `ecb9239`) | 0 (no code change in `src/`) | 599 |

The final test count reported by `pytest -q` is **586 passed** (not 599
as the table above would suggest). The discrepancy is honest: pytest
counts `test_*` functions, and the v0.8.1 work included both **additions**
(23 new test methods) and **deletions** (some pre-existing tests that
became redundant, plus refactors that replaced test functions rather
than adding new ones). Net new test methods: **+10**.

If you want the precise per-commit test diff, see the commit log:
`git log --oneline v0.8.0..v0.8.1` shows the four v0.8.1 commits
(docs, Phase A, Phase B, Phase C) plus the release notes commit.

## Backward compatibility

- `deep_research()` legacy function: **untouched** (signature test still passes)
- All new dataclass fields: optional with defaults
- `ResearchPlan` immutability: now actually respected (was being mutated)
- `SearchTask` / `EvidenceWindow` / `Claim` / `ResearchState`: no signature changes
- Default `max_iterations=1`: still default (Phase B hardening only changes behaviour when `max_iterations >= 2`)

## What we did NOT do (per external review "don'ts" list)

- **No new dependencies** (Pydantic, LangGraph, Tavily/Exa, etc. — all deferred)
- **No rewrite** of `hermes_deepresearch.py` (1201 lines, mostly out of scope)
- **No new top-level abstractions** (no `src/deepresearch/` namespace — P2 deferred)
- **No CI matrix** (single Python 3.11 for now; can extend later)

## Stats

- **+15 commits** since v0.8.0 tag (`d805296`)
- **6 commits** for Phases A-C of v0.8.1
- **+10 net tests** (576 → 586)
- **+123/-42 lines** of release-hygiene changes
- **No new dependencies** (still stdlib-only for production)

## What was NOT addressed in v0.8.1 (deferred to v0.8.2 / v0.9.0)

From the external review:
- `_extract_facts()` returns n-grams, not sentences (P1 #4) — quality ceiling for citations
- `find_span()` returns normalized-text offsets, not always original-text (P1 #5) — needs `original_index` mapping
- `synthesis.py` builds citations from whole-source table, not from evidence windows (P1 #6) — needs proper `ClaimVerdict` integration
- `hermes_searxng.py` loads `.env_proxy` at import (P1 #7) — minor cleanup
- `settings.yml` `use_default_settings: true` merge risk (P1 #9) — needs calibration data

These are tracked in `.hermes/plans/ISSUES.md` for v0.8.2 / v0.9.0.

## Verification commands

```bash
# Full suite
PYTHONPATH=src python3 -m pytest -q
# → 586 passed in ~26s

# Just the Phase A+B regression tests
PYTHONPATH=src python3 -m pytest tests/test_research_runner.py::TestPhaseA tests/test_research_runner.py::TestPhaseB -v
# → 23 passed

# Lint (CI gate)
ruff check src tests scripts
```

## Credits

- Architecture review: ChatGPT (external review, distilled in `/tmp/hermes-recomendation-08062026(2).txt` — not committed; structured prompt in `docs/CHATGPT_REVIEW_REQUEST_v0.8.0.md`)
- Self-review (Ерёма): `docs/SELF_REVIEW_v0.8.0.md`
- Implementation: Ерёма (Hermes Agent) + Georgy Belyanin
- License: see `LICENSE` (MIT)
