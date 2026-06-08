# ChatGPT Review Request: searxng-deep-research v0.8.0

**Date:** 2026-06-08
**Tag:** `v0.8.0` (commit `df32f22`)
**Repository:** https://github.com/ergon73/searxng-deep-research
**Archive:** `searxng-deep-research-v0.8.0.tar.gz` (238KB, sha256 `57651cfc37a485b4994a4170e6a2c4532ba9921dd88d5a3c48b428109b0b339f`)

---

## How to access

### Option A: Download the archive (preferred)

```bash
curl -L -o /tmp/review.tar.gz https://github.com/ergon73/searxng-deep-research/archive/refs/tags/v0.8.0.tar.gz
sha256sum /tmp/review.tar.gz
# Expected: 57651cfc37a485b4994a4170e6a2c4532ba9921dd88d5a3c48b428109b0b339f
mkdir -p /tmp/review && tar xzf /tmp/review.tar.gz -C /tmp/review
ls /tmp/review/searxng-deep-research-v0.8.0/
```

The archive contains the **exact** state of the repo at the v0.8.0 tag —
no JS, no auth, no missing files.

### Option B: Browse the repo

- Code: https://github.com/ergon73/searxng-deep-research/tree/v0.8.0
- Release notes: https://raw.githubusercontent.com/ergon73/searxng-deep-research/v0.8.0/RELEASE_NOTES_v0.8.0.md
- Issue tracker: https://github.com/ergon73/searxng-deep-research/blob/v0.8.0/.hermes/plans/ISSUES.md

---

## What's in v0.8.0 (Phases 0–5)

The release ships the full pipeline from the original external review:

| Phase | Module | Purpose |
|---|---|---|
| 0 | (release hygiene) | v0.8.0 baseline, .env examples, .gitignore, ARCHITECTURE.md |
| 1 (#016) | `src/models.py` | Typed state — `SearchTask`, `Claim`, `ResearchState`, `EvidenceWindow` |
| 2 (#017) | `src/planner.py` | `build_research_plan()` — composes `adapt_query()` + `classify_intent()` with falsification tasks |
| 3 (#018) | `src/research_runner.py` | `run_research()` / `deep_research_v2()` — strangler refactor of legacy `deep_research()` |
| 4 (#019) | `src/citations.py` | Span-level citations — `find_span()` + `build_evidence_window()` + `[doc_N:start-end]` markers |
| 5 (#020) | `src/gap_analysis.py` | `analyze_gaps()` detects 6 gap kinds, `gaps_to_search_tasks()` for iterative deepening |

**Test count:** 563 passing in ~26s (was 404 pre-Phases 0–5, +159 new)
**Lines of code:** +3,793 / -64 across 23 files
**Dependencies:** stdlib only (no Pydantic, no httpx, no new requirements)
**External APIs:** SearXNG (self-hosted) only — Exa/Tavily integration pending v0.9.0

The 5 files I'd most like you to read, in priority order:

1. `src/research_runner.py` (505 lines) — the orchestrator
2. `src/citations.py` (236 lines) — Phase 4, the new machinery
3. `src/gap_analysis.py` (306 lines) — Phase 5
4. `src/planner.py` (245 lines) — Phase 2
5. `src/models.py` (135 lines) — Phase 1, the typed state

The legacy code in `src/hermes_deepresearch.py` (1201 lines) is **mostly
untested and out of scope** for this review — it's the strangler that the
new runner coexists with. Focus on Phases 1–5.

---

## What I want from the review

Please be **honest, not nice**. I'd rather hear "this is wrong" than
"looks good, ship it." Fabrication is worse than "I don't know."

### Required output format

For each finding, please use this structure:

```
### Finding N: <one-line summary>
- **Severity:** CRITICAL / HIGH / MEDIUM / LOW / NIT
- **Location:** <file>:<line> or <file>:<function>
- **What:** <one paragraph describing the issue>
- **Why it matters:** <impact on correctness / security / maintainability>
- **Fix:** <concrete patch or refactor, with code snippet if possible>
- **Effort estimate:** <1h / 4h / 1d / 1w>
```

Severity scale:
- **CRITICAL** — security hole, data loss, correctness bug, blocks production
- **HIGH** — design flaw, performance cliff, will block v1.0
- **MEDIUM** — code smell, missing test, documentation gap
- **LOW** — naming, style, comment wording
- **NIT** — bikeshed, not worth fixing

---

## Specific questions (please answer each)

### Q1: Architecture — is the 4-class minimum typed state right?

The original external review suggested 9 typed classes. I cut to 4
(`SearchTask`, `Claim`, `ResearchState`, `EvidenceWindow`) because the
other 5 (`SearchHit`, `Document`, `ClaimVerdict`, `ResearchPlan`,
`ResearchReport`) had no consumers and dicts were fine at the time.

**Now the runner uses most of them internally.** Is it time to promote
them, or are dicts still fine at the current scale?

### Q2: `_extract_facts` (legacy) returns n-grams, not sentences

`src/hermes_deepresearch.py::_extract_facts` returns things like
`'9 has'`, `'9 engines'`. This is the **upstream quality ceiling** for
the citations feature. Live smoke shows 4/4 claims cited at 100% — but
those are short n-grams, easy to substring-match.

Should I add a sentence-level extractor (`_extract_sentences_claim()`)
in `models.py` for v0.9.0, or is "substring search on n-grams" good
enough?

### Q3: `find_span` Case 3 (fuzzy prefix) returns approximate end offsets

`src/citations.py::find_span` Case 3 returns
`(idx, idx + len(norm_claim))` even when only the first 30 chars match.
The end offset is approximate. A downstream consumer slicing
`document_text[start:end]` would get the wrong text.

I documented this in the docstring. Is that honest enough, or should
the function refuse to return a window when the full claim can't be
located?

### Q4: `synth.coverage` mutation is a contract break risk

`_run_synthesis` does `synth.coverage["citation_stats"] = stats`,
mutating a foreign dataclass instance. Acceptable "decorator pattern",
or should I propose a proper `SynthesisExtension` dataclass to
`src/synthesis.py`?

### Q5: Gap thresholds (constants in `gap_analysis.py`)

`MIN_DOCUMENTS=3`, `MIN_UNIQUE_DOMAINS=2`, `MIN_TOP1_CONFIDENCE=0.5`,
`MAX_UNSUPPORTED_CLAIM_RATIO=0.4` — all in one place for tuning but
intuition-based. Should there be a calibration mode that learns them
from eval data, or is centralised constants good enough?

### Q6: `run_research` is 219 lines (long but linear)

It does 8 things in one function: plan build, confirmation gate, state
init, loop dispatch, search/fetch, claims extract (×2), verification,
gap analysis, synthesis decoration, review. I extracted helpers but
kept the main flow linear for readability.

Refactor to a `Pipeline` class with explicit stages, or accept the
"comfortably above single-screen" length for v0.8.0?

### Q7: Concurrency / mutation story

`run_research` mutates `state.evidence`, `state.claims`,
`plan.search_tasks` (in gap-fill loop). Single-threaded contract only.
If two threads called `run_research` on the same plan, state corrupts.

Document the constraint, or add a `threading.Lock`?

### Q8: Test coverage for `verify_sources` and legacy pipeline

I added 159 tests in Phases 0–5. The legacy `hermes_deepresearch.py`
(1201 lines) is mostly untested. Is that a blocker for v1.0?

### Q9: What's the **single biggest** risk in v0.8.0?

If you had to name one thing that's most likely to bite us in
production, what would it be?

### Q10: What's the **single biggest** missed opportunity?

If you had to name one thing we *should* have done but didn't, what
would it be?

---

## Self-review context

I (Ерёма, the author) wrote a self-review at
`/tmp/searxng-review/SELF_REVIEW_v0.8.0.md` (or in the archive at
`/SELF_REVIEW_v0.8.0.md` if I include it). It lists 8 concerns I have
about my own work. **Please don't just confirm them** — push back on
the ones you think are over-worried, and flag any I missed.

---

## What I want ChatGPT to NOT do

- **Don't rewrite the project from scratch.** Phases 0-5 are signed off.
  Suggest deltas, not new architectures.
- **Don't propose dependency additions** (Pydantic, httpx, etc.) unless
  they're strictly necessary. The stdlib-only constraint is a feature.
- **Don't tell me "consider using X" for greenfield tools.** This is
  self-hosted, low-traffic, single-operator. The trade-offs are
  different from a hyperscaler.
- **Don't fabricate metrics.** If you don't have data, say "I don't
  know" or "this is speculation". The user has explicitly asked for
  honesty over impressive numbers.

---

## What I want ChatGPT to be specific about

- **Cite line numbers** when pointing to a specific issue
- **Show code** when proposing a fix
- **Estimate effort** (1h, 4h, 1d, 1w) — I have ~6 hours/week for this
  project
- **Distinguish** "v0.8.0 must fix" from "v0.9.0 nice to have" from
  "v1.0 if ever"

---

## Verification

If you want to run the tests yourself:

```bash
cd searxng-deep-research-v0.8.0
PYTHONPATH=src python3 -m pytest -q
# Expected: 563 passed in ~26s
```

If you want to run the live smoke test (requires monkeypatching
network, no actual SearXNG needed):

```bash
PYTHONPATH=src python3 scripts/e2e_falcon9.py
```

---

## Sign-off

Thanks for the review. I'll read it carefully and act on CRITICAL/HIGH
items immediately, MEDIUM items in v0.9.0, and LOW/NIT items as
encountered.

— Ерёма (Hermes Agent) for Georgy Belyanin
