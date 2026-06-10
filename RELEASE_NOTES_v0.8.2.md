# Release Notes — v0.8.2

**Date:** 10 June 2026
**Tag:** `v0.8.2` → this release commit (see `git tag -l 'v0.8.*'`)
**Type:** Hardening — verification correctness, source-bound LLM semantics, env-test hygiene
**Diff vs v0.8.1.4:** 4 commits on main, all under the `v0.8.2-*` review chain
**CI status:** all 4 stack commits green on `main` (see CI section below)

---

## Summary

`v0.8.2` is a hardening release that closes four review-driven concerns in
sequence — `A`, `B/B1/B2`, and `C1` — without changing pipeline shape, providers,
or external APIs.

The release fixes a numeric-matcher correctness bug (`A`), introduces a
diagnostic-only `WEAK_SUPPORT` verdict and a source-bound `source_urls` whitelist
for LLM-emitted URLs (`B/B1`), inverts the whitelist so the stored citation
URL is the candidate's original (not the LLM's raw) plus renames "supporting
sources" → "evidence sources" in the LLM prompt surface (`B2`), and rewrites
the test-suite's env-file contract so clean checkouts pass `pytest -q` without
fabricating a repo-root `.env_llm` (`C1`).

**No new pipeline stages, no new providers, no new dependencies.**

Four commits in this release (oldest first):

1. `fix(verify): v0.8.2-A — numeric matcher scans all occurrences before mismatch`
   — numeric-match now scans every numeric occurrence in the source text
   before declaring a mismatch, fixing a false-positive that could
   down-rank well-supported facts.
2. `feat(verify): v0.8.2-B1 — WEAK_SUPPORT semantics + source_urls whitelist`
   — caller-computed `WEAK_SUPPORT` verdict for `SUPPORTS` from LLM without
   valid cited `source_urls`; `verify_sources()` now filters LLM-emitted
   `source_urls` through a canonical-match whitelist against the real
   `source_candidates` (the URLs that were actually fetched).
3. `feat(verify): v0.8.2-B2 — evidence sources wording + candidate-original
   URL storage` — the LLM prompt surface renames "supporting sources" →
   "evidence sources" (block label, header sentence, docstring); the
   whitelist inverts to store the candidate's original URL (not the LLM
   raw URL), so the LLM cannot inject tracking params, fragments, or
   case-variants into the final citation string.
4. `test(env): v0.8.2-C1 — env-test hygiene, drop repo-root .env_llm
   requirement` — `TestEnvLlm` rewritten against `tmp_path`; legacy
   `REPO_ROOT/.env_llm` checks now `skipif` the file is absent; CI no
   longer fabricates a repo-root dummy.

> **Note on tag target:** This file does not hardcode the release commit
> SHA, because the SHA cannot be known until the commit is made. The
> canonical way to identify the release commit is
> `git tag -l 'v0.8.*'` (annotated tag) or
> `git log --grep "v0.8.2"` (search by tag mention in commit messages).
> Prior versions of this file (v0.8.1.3, v0.8.1.4) had the same trap and
> were corrected by removing the hardcoded SHA — see the "tag-target
> self-reference trap" reference for the lesson.

---

## A — numeric matcher scan-all-occurrences

### Bug

The numeric matcher declared a `NUMERIC_MISMATCH` verdict on the first
non-matching number it scanned in the source text, even if a later
occurrence in the same source agreed with the fact. This produced
false-positive numeric contradictions and down-ranked well-supported
facts.

### Fix

`src/hermes_deepresearch.py::_match_numeric` (and its caller path
through `verify_sources`) now scans **all** numeric occurrences in the
source chunk before declaring a mismatch. A fact is considered
numerically contradicted only if **no** numeric occurrence in the chunk
agrees with the fact's number.

### Impact

- `NUMERIC_MISMATCH` verdict becomes significantly rarer, more aligned
  with human review.
- `verified_facts` count rises in some queries where the old behaviour
  was demoting facts that should have been verified.
- `verification_rate` increases moderately; no downstream counter was
  broken.

### Tests

- New tests in `tests/test_numeric_matcher.py` cover the
  "first number mismatches, second matches" case explicitly.

---

## B / B1 — WEAK_SUPPORT semantics + source_urls whitelist

### B: motivation

Before v0.8.2, when the LLM returned `SUPPORTS` for a fact, that fact
was treated as verified — regardless of whether the LLM actually cited
a real source URL it had seen. This created two risks:

1. **Citation integrity** — the LLM could return any plausible-looking
   URL (or no URL at all) and have it accepted as a citation.
2. **Diagnostic blindness** — when the LLM said "SUPPORTS" but cited
   nothing, the caller had no way to distinguish "confident support"
   from "support-by-hallucination".

### B1: changes

1. **`VERDICT_WEAK_SUPPORT = "WEAK_SUPPORT"`** constant added to
   `src/llm_verifier.py` (note: not part of the strict LLM JSON
   schema enum, which remains `{SUPPORTS, REFUTES, INSUFFICIENT}`).
2. **`verify_sources()` caller-computed downgrade** in
   `src/hermes_deepresearch.py`: when the LLM returns `SUPPORTS` but
   all `source_urls` are rejected by the whitelist, the fact's verdict
   is downgraded to `WEAK_SUPPORT` and `verified = False` is forced.
3. **Counters**:
   - `llm_verified_count` increments **only** on `SUPPORTS + valid cited
     URL`.
   - `llm_weak_count` increments on the downgrade path (new counter,
     returned in the verify dict).
   - `verified_facts` and `verification_rate` are **not** increased by
     `WEAK_SUPPORT` (recomputed after the LLM pass, by design).
4. **REFUTES without source_urls** remains a caller-recorded
   `llm_unlinked_refute_count` counter (uncited diagnostic) — the
   verdict string is preserved for diagnostic purposes, but the URL
   is **not** written to `refuting_sources` (it cannot be cited without
   a real source). `refuting_sources` is the only path that downstream
   citation machinery reads from, so the fact cannot be used as a
   cited refutation.
5. **`_filter_source_urls_to_candidates()` whitelist** —
   canonical-form match against the real `source_candidates` (the
   URLs the fetch pipeline actually read). Rejects:
   - non-http(s) schemes (`javascript:`, `file:`, `ftp:`, etc.)
   - URLs with empty / unparsable netloc
   - URLs whose canonical form is empty
   - URLs whose canonical form does not appear in the candidate set
   - secrets-like strings (defence-in-depth is **P2** in the B1
     review; whitelist blocks them implicitly via canonical match
     because attacker URLs rarely match a candidate's canonical form)

### Acceptance criteria (B1 AC table)

1. SUPPORTS + valid `source_urls` from `source_candidates` →
   `verified = True`, `verdict = "SUPPORTS"`, `llm_verified_count` ++,
   `verified_facts` ++, `supporting_sources` populated with real URLs,
   no `"llm_batch"`.
2. SUPPORTS + `source_urls = []` → `verdict = "WEAK_SUPPORT"`,
   `verified = False`, `llm_verified_count` **unchanged**,
   `verified_facts` **unchanged**, `verification_rate` **unchanged**,
   `supporting_sources` stays empty.
3. WEAK_SUPPORT — diagnostic only, not a success, not a cited
   verification, not a cited support.
4. REFUTES + valid `source_urls` → `verdict = "REFUTES"`,
   `refuting_sources` populated, no `"llm_batch"`.
5. REFUTES + `source_urls = []` → `verdict = "REFUTES"` (preserved
   for diagnostic), `refuting_sources` stays empty,
   `llm_unlinked_refute_count` ++. Not used as cited refutation.
6. LLM `source_urls` accepted only if they canonical-match a real
   `source_candidate`.
7. Canonical-tracking match accepted (e.g. `?utm_source=x` matches
   candidate's clean URL).
8. No runtime `"llm_batch"` use anywhere in `src/`.
9. No live OpenRouter calls during `pytest`.

### Tests

`tests/test_weak_support_semantics.py` — 17 tests covering all
9 acceptance criteria above. Tests use `unittest.mock.patch` on
`LLMVerifier.verify_facts_batch` to inject canned LLM results, so the
test suite has no live OpenRouter dependency.

---

## B2 — source-bound wording + candidate-original URL storage

### Why

B1 fixed the structural problem (whitelist accepts only real
candidates) but left two residuals:

1. **Prompt terminology** — the LLM prompt still said "supporting
   sources". Reviewer-9 wanted "evidence sources" (the term used in
   the project's documentation). The downstream data shape
   (`supporting_sources` tuple key in `verify_details`) is unchanged —
   only the prompt surface and its docstring moved.
2. **Citation control** — the whitelist returned the **LLM's raw URL**
   as the accepted URL. That meant after canonical-match, the LLM's
   `utm_*` params, fragments, or case-variants still ended up in the
   final citation string. The LLM was effectively controlling the
   citation URL, only bounded by the canonical-match check.

### Changes

1. **Prompt wording** (`src/llm_verifier.py`):
   - `Supporting sources:` → `Evidence sources:` (block label).
   - `Verify each fact against the supporting sources below.` →
     `Verify each fact against the evidence sources below.`
   - Docstring and param comment for `source_candidates` in
     `verify_facts_batch` also updated.
2. **Whitelist storage** (`src/hermes_deepresearch.py`): the helper
   `_filter_source_urls_to_candidates` now returns the **candidate's
   original URL** (the URL that was actually fetched and stored by the
   search/fetch pipeline) for each accepted canonical-match, not the
   LLM's raw URL. First candidate with a given canonical wins (this
   matches the URL the fetch pipeline actually used).

### Tests

- New `test_canonical_match_returns_candidate_original_url`
  (helper-level).
- New `test_strips_tracking_params_for_match_returns_candidate_original`.
- New `test_canonical_match_integration_stores_candidate_original`
  (integration test asserting the LLM's `utm_source=llm` is **not**
  present in `supporting_sources`).
- New `test_prompt_uses_evidence_sources_wording` — captures the
  actual HTTP body sent to OpenRouter via patched
  `LLMVerifier._call_with_fallback` and asserts `"supporting sources"`
  is absent and `"Evidence sources:"` / `"evidence sources below"` are
  present. Stronger than grep — catches runtime concatenation bugs.
- Renamed `test_refutes_without_source_urls_not_cited` →
  `test_refutes_without_source_urls_is_uncited_diagnostic` for
  clarity of intent.

---

## C1 — env-test hygiene

### Why

`tests/test_compose_config.py::TestEnvLlm` previously hard-required a
real `.env_llm` at `REPO_ROOT` with `mode 0600` and a `SEARXNG_SECRET`.
That made `pytest -q` fail in clean checkouts (no `.env_llm` present)
and forced CI to fabricate a dummy file at `$GITHUB_WORKSPACE/.env_llm`
before running pytest. For a public release, the contract should be:
"if you ship a `.env_llm`, it must look like this" — not "you must
ship a `.env_llm`".

### Changes

1. **`TestEnvLlm` rewritten** to use the `tmp_path` pytest fixture:
   4 tests, each creates a synthetic `.env_llm` and validates the
   contract. Cases covered: mode 0600, `SEARXNG_SECRET` present and
   non-placeholder, non-0600 mode detected as bad, missing
   `SEARXNG_SECRET` detected as bad.
2. **`TestEnvLlmLegacyCompatibility` added** (2 tests): runs only
   `skipif REPO_ROOT/.env_llm` exists. Preserves the regression net
   for dev machines that ship a real file; skips cleanly for clean
   checkouts and CI.
3. **`_parse_env_file()` helper** extracted to remove the parse
   duplication between `TestEnvLlm` and `TestEnvLlmExample`.
4. **CI workflow** (`.github/workflows/ci.yml`): dropped
   `cp /opt/searxng/.env_llm .env_llm` and `chmod 600 .env_llm` lines.
   The `/opt/searxng/.env_llm` fabrication is kept as a no-op safety
   net for the legacy `_load_api_key` path (the primary path is
   `OPENROUTER_API_KEY` env var, set above the `pytest` step).

### Before / after

| Scenario | Before v0.8.2 | After v0.8.2 |
|----------|---------------|--------------|
| `pytest -q` in clean clone | 3 fail (`TestEnvLlm`) | 0 fail (17 + 2 skip) |
| CI requires repo-root dummy | yes | no |
| `TestEnvLlm` requires real file | yes (hard) | no (tmp_path) |
| Legacy `.env_llm` regression net | implicit | explicit `TestEnvLlmLegacyCompatibility` |

---

## CI

All 5 commits in this release pushed to `main`, all CI runs green:

| Commit | Run | Status |
|--------|-----|--------|
| `9bd0bc7` (v0.8.2-A)              | [run 27220266692](https://github.com/ergon73/searxng-deep-research/actions/runs/27220266692) | success |
| `64b7047` (v0.8.2-B1)             | [run 27225501055](https://github.com/ergon73/searxng-deep-research/actions/runs/27225501055) | success |
| `9eb9803` (v0.8.2-B2)             | [run 27257014232](https://github.com/ergon73/searxng-deep-research/actions/runs/27257014232) | success |
| `653b01a` (v0.8.2-C1)             | [run 27269163632](https://github.com/ergon73/searxng-deep-research/actions/runs/27269163632) | success |
| `0e01d2c` (v0.8.2 release prep)   | [run 27270532884](https://github.com/ergon73/searxng-deep-research/actions/runs/27270532884) | success |

The release-prep commit (`0e01d2c`) updates `pyproject.toml`,
`README.md`, `AGENTS.md`, and this release-notes file. It does not
change code, tests, providers, pipeline shape, or runtime behaviour.
The annotated tag `v0.8.2` will be created on this release-prep
commit after explicit user approval (see note on tag target below).

---

## Backward compatibility

- Public Python API: **unchanged**. `verify_sources()` return shape
  grew by 1 field (`llm_weak_count`); existing fields unchanged.
- Pipeline shape: **unchanged**. No new stages, no new providers.
- Configuration: **unchanged**. `config/.env_llm.example` is still the
  portable template; `REPO_ROOT/.env_llm` is now **optional** for
  tests (still recommended for runtime use).
- Existing tag users: `v0.8.1.x → v0.8.2` is a non-breaking upgrade.
  `pip install -e ".[dev]"` from `v0.8.2` works exactly as it did
  from `v0.8.1.4`.

---

## Verification (copy-paste runnable)

```bash
# clone and checkout the release tag
git clone https://github.com/ergon73/searxng-deep-research.git
cd searxng-deep-research
git checkout v0.8.2   # after the tag is published (see user-approval gate)

# local gates
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/eval.py --no-network --dry-run
ruff check src tests scripts
ruff format --check src scripts
```

Expected:
- `pytest -q` → `677 passed, 2 skipped` (the 2 skips are
  `TestEnvLlmLegacyCompatibility` checks that only run on dev
  machines with a real `REPO_ROOT/.env_llm`).
- `eval --dry-run` → 8/8 queries, QS 0.33, routing 100%, 0 errors.
- `ruff check` → all checks passed.
- `ruff format --check` → 22 files already formatted.

---

## Next-phase candidates (post-tag, NOT in this release)

Listed for reviewer context only — **not started in v0.8.2** per
release-prep scope discipline.

- v0.8.3: synthesis, ranking, routing (held until v0.8.2 tags).
- v0.8.2-x: secret-like URL defense-in-depth in the whitelist (B1
  P2 — optional, currently blocked implicitly via canonical match).
- LLM_ENV_FILE portability: review-time work for a future version.

---

## References

- `docs/SELF_REVIEW_v0.8.0.md` — author's pre-review self-criticism
  (still applicable as a baseline).
- `.hermes/plans/ISSUES.md` — open/closed issues tracker, v0.8.2 row
  will be added post-tag.
- Tag-target self-reference trap
  (`processing-inbound-reviews/references/tag-target-self-ref-trap.md`)
  — why this file intentionally does not hardcode the release
  commit SHA.
