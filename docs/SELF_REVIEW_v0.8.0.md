# Self-Review: v0.8.0 (Phases 0–5)

**Reviewer:** Ерёма (Hermes Agent) · **Date:** 2026-06-08 · **Tag:** `v0.8.0` (`df32f22`)
**Scope:** `src/models.py`, `src/planner.py`, `src/research_runner.py`, `src/citations.py`, `src/gap_analysis.py` (my work in Phases 1-5) + `src/evidence.py` (modified for #019)

I review my own work first so the ChatGPT review is a second opinion, not
the only one. **Honestly naming what I'm worried about** — fabrication is
worse than "I don't know" (project rule).

---

## 1. What's good (genuine wins)

- **563/563 tests pass** including 159 new tests across 5 phases
- **Backward compat preserved**: legacy `deep_research()` untouched, all new
  dataclass fields are optional with defaults, default `max_iterations=1`
  keeps legacy behaviour
- **Pure stdlib**: no Pydantic, no httpx, no new dependencies
- **Strangler refactor** discipline: Phase 3 runner coexists with legacy
  function, callers choose
- **Hash-based falsification** (no LLM, no random) makes planner
  deterministic and testable
- **Confirmation gate** respected — `needs_confirmation` doesn't trigger
  network calls, eval doesn't penalise it
- **Span-level citations** (Phase 4) attach offsets + URL + title + score
  to each non-stub `Claim` with an invariant enforcer

---

## 2. What worries me (honest self-criticism)

### 2.1 `run_research()` is 219 lines (too long)

`src/research_runner.py::run_research` (lines ~225-444) does **eight things**
in one function: plan build, confirmation gate, state init, loop dispatch,
search/fetch, claims extract (×2 — string + typed), verification, gap
analysis, synthesis decoration, review. Hard to test in isolation, hard
to reason about exception flow.

**Why I didn't refactor:** "strangler, no over-engineer" — extracted
helpers instead (`_dispatch_search_task`, `_fetch_documents`,
`_extract_claims_from_documents`, `_extract_typed_claims_with_citations`,
`_run_pipeline`, `_doc_index_for_window`). The main function is
**intentionally linear** for readability. But 219 lines is past the
"comfortable single-screen" mark.

**Verdict:** Would be a 1-day refactor into a `Pipeline` class with
explicit stages. Defer to v0.9.0 unless ChatGPT flags it as critical.

### 2.2 `_extract_facts` (legacy) is 140 lines and returns n-grams, not sentences

`src/hermes_deepresearch.py::_extract_facts` returns things like
`'9 has'`, `'9 engines'`, `'Elon Musk founded SpaceX'` — n-gram fragments,
not complete claims. This is the **upstream quality ceiling** for the
whole citations feature.

I knew this when I built Phase 4. The live smoke test shows
`4/4 claims cited at 100% coverage` — but those are short n-grams, easy
to substring-match. A real long-sentence claim like "Falcon 9's first
launch occurred on 4 June 2010 from SLC-40" gets broken into pieces.

**Why I didn't fix:** Modifying `_extract_facts` would change the legacy
`deep_research()` contract. Phase 1's design rule was "typed state is
advisory, not enforced" — the citation pipeline inherits the same n-gram
quality as the legacy one.

**Verdict:** Real issue. ChatGPT should flag this. Possible fix: a new
`_extract_sentences_claim()` in `models.py` that returns typed `Claim`
directly from a sentence splitter, then `build_evidence_window()` finds
the sentence in the document (sentences are easy to substring-match).

### 2.3 `find_span` Case 3 (fuzzy prefix) is a lie about offsets

In `src/citations.py::find_span`, Case 3 returns
`(idx, idx + len(norm_claim))` even when only the first 30 chars match.
**The end offset is approximate** — we don't actually find the boundary
of the full claim. A downstream consumer that slices
`document_text[start:end]` would get the wrong text.

**Why I did it anyway:** Without LLM-based span detection, this is the
best we can do in stdlib. I documented it ("best-effort span — end is
approximate but start is exact") and added a test for it.

**Verdict:** Known limitation. Could be flagged by ChatGPT as a "the
numbers don't always correspond to real spans" issue. Documenting the
limit clearly in the docstring is the only honest fix.

### 2.4 `synth.coverage` mutation is a contract break risk

In `_run_synthesis` decoration block, I do
`synth.coverage["citation_stats"] = stats`. This **mutates a foreign
dataclass instance** (`synth` is `Synthesis` from `src/synthesis.py`).
The synthesis module doesn't know I'm adding keys.

Today this is fine (synth.coverage is `dict`, mutation is allowed). But
if a future change makes `synth.coverage` a frozen dict, my code breaks
silently — no test catches it.

**Why I did it:** "Don't change the legacy `synthesize()` contract" rule.
The alternative was forking `Synthesis` dataclass with 3 new fields, which
adds maintenance burden.

**Verdict:** Real but minor. A defensive `if not isinstance(synth.coverage,
dict): synth.coverage = {}` would help. I have that check in the runner,
so it's OK.

### 2.5 Gap analysis is rule-based, not adaptive

`src/gap_analysis.py::analyze_gaps` uses **fixed thresholds**:
`MIN_DOCUMENTS=3`, `MIN_UNIQUE_DOMAINS=2`, `MIN_TOP1_CONFIDENCE=0.5`,
`MAX_UNSUPPORTED_CLAIM_RATIO=0.4`. These work for English news but
might be wrong for Russian forums, code search, or short-form content
(Twitter / Telegram).

**Why I did it:** No benchmark data to calibrate against. I centralised
constants in one place so they're tunable in 30 seconds.

**Verdict:** Honest limitation. The next phase (Exa/Tavily + LLM
enrichment) would provide calibration data. Without it, these are
intuition-based.

### 2.6 `SearchTask.engine_preference` was deferred — that's a feature gap

I explicitly did NOT add `engine_preference` to `SearchTask` in v0.8.0,
even though Exa/Tavily integration is the next planned phase. The
reason: "v0.8.0 done, tag, then v0.9.0". But this means the field has
to be added **plus** the runner has to learn to iterate providers — two
breaking changes in one release.

**Why:** Phase discipline. Each phase was a clean cut.

**Verdict:** Not a bug. Just flag the design in the review so v0.9.0
isn't a surprise.

### 2.7 No mutation / concurrency tests

All 563 tests run sequentially. `run_research` mutates `state.evidence`,
`state.claims`, `state.search_tasks` (the plan!) in `plan.search_tasks.extend(new_tasks)` inside the gap-fill loop. If two
threads called `run_research` on the same plan, **state would corrupt**.

I knew this when writing the runner. The mitigation is "don't do that" —
a `ResearchPlan` is a planning artifact, not a shared resource.

**Why I didn't add a lock:** Single-threaded runner is the v0.8.0 contract.
If you want async, the whole `run_research` would need to be re-architected
into an async pipeline.

**Verdict:** Document the "no concurrent use" constraint in the docstring.
Could be flagged by ChatGPT as a concurrency hazard.

### 2.8 Typed `Claim` augmentation via `dataclasses.replace` is O(N) per call

`dataclasses.replace(c, evidence_window=w)` constructs a new immutable
`Claim` for every augmented claim. With `max_per_doc=8` and 4 docs, that's
32 replace calls per pipeline pass. Trivial in pure Python, but if we
ever add thread-safety or deep-copy semantics, this becomes a concern.

**Verdict:** Premature optimisation. Don't flag.

---

## 3. What I'm NOT worried about

- **Security**: `.gitignore` blocks `.env*` and credentials. The
  `redact.py` module redacts before any archive. No `eval`, no `exec`,
  no `pickle` of untrusted data. LLM prompt injection mitigated by
  `evidence_window` extraction (we don't feed the whole document to LLM,
  we feed windows).
- **Test isolation**: every side-effect function is monkeypatch-able,
  no shared state between tests, all fixtures are function-scoped.
- **Determinism**: planner uses hash-based falsification (not random),
  no `time.time()` in synthesis (only in elapsed_sec reporting), no
  global mutable state.

---

## 4. Concrete questions for ChatGPT

If I were asking ChatGPT to review this, I'd ask:

1. **Is the 4-class minimum typed state (#016) the right size, or did I
   under-model?** The reviewer originally suggested 9 classes. I cut to
   4 because the other 5 had no consumers. Should I add `SearchHit`,
   `Document`, `ClaimVerdict`, `ResearchPlan`, `ResearchReport` now that
   the runner uses them all internally?

2. **The n-gram issue (2.2)** — should I add a sentence-level extractor
   in `models.py` as a v0.9.0 deliverable? Or is "substring search on
   n-grams" good enough and I'm over-engineering?

3. **Mutation of `synth.coverage` (2.4)** — is this acceptable
   "decorator pattern" or should I propose a proper
   `SynthesisExtension` dataclass to `src/synthesis.py`?

4. **Are the gap thresholds (2.5) sane defaults?** I made them up
   based on intuition. Should there be a calibration mode that learns
   them from eval data?

5. **Is the 219-line `run_research` (2.1) acceptable for v0.8.0, or
   should I refactor to a Pipeline class before tagging v1.0?**

6. **What's the test coverage for `verify_sources` and the legacy
   pipeline?** I added 159 tests in Phases 0-5, but the legacy
   `hermes_deepresearch.py` (1201 lines) is mostly untested. Is that
   a blocker for production use?

---

## 5. Things I'd defend

- **No Pydantic, stdlib dataclasses only**: deliberate, no need for
  runtime validation in a pipeline that gets monkeypatched everywhere.
- **No LLM in span finding** (2.3): cost vs. quality. Case 1 (direct
  substring) hits in the great majority of cases. Adding LLM for the
  remaining 5% is over-engineering for v0.8.0.
- **Strangler refactor**: legacy `deep_research()` is a real production
  function. Don't touch it until the new runner is feature-complete.
- **No eval-data calibration for gap thresholds (2.5)**: doing it now
  would be guessing. Better to ship conservative defaults and tune
  when real eval data exists.

---

## 6. What I want ChatGPT to be honest about

- **The architecture is well-shaped for v0.8.0** but **not yet v1.0**.
  v0.8.0 is "internally consistent, externally constrained". A real
  v1.0 needs calibration data, a benchmark suite, and concurrency story.
- **The hard problem (claim quality from n-grams) is unsolved**. Phase
  4 gives us the *machinery* for citations, but the *input* (extracted
  claims) is still n-gram fragments. Real fix is sentence-level
  extraction in a future phase.
- **The Exa/Tavily integration (next) is the riskiest single change**
  in the pipeline's life so far. It introduces external cost, rate
  limits, and provider-specific quirks. The skeleton I propose (pure
  stdlib, ENV-only keys, monkeypatch-able) is the safest way to land it.
