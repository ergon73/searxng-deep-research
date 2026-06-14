# Release Notes — v0.8.3

**Date:** 14 June 2026
**Tag:** `v0.8.3` → this release commit (see `git tag -l 'v0.8.*'`)
**Type:** Synthesis hardening — user/audit split, span-level evidence markers, dual-numbering UX
**Diff vs v0.8.2:** reviewed v0.8.3 synthesis hardening chain (B, B1, C1, C1b, C1c, C1d, C2-data, C3, C3b, D0 audit, D1); see `git log v0.8.2..v0.8.3` for the exact commit list
**CI status:** all release commits green on main (see CI section below)

---

## Summary

`v0.8.3` is a synthesis-hardening release that closes the user-facing
answer contract on `run_research()` and brings span-level evidence
provenance into the user-facing `answer_markdown`. It does **not**
change pipeline shape, providers, or external APIs.

The release is structured as a concern-driven chain — each
`B` / `B1` / `C1` / `C1b` / `C1c` / `C1d` / `C2-data` / `C3` /
`C3b` / `D0` (audit) / `D1` sub-batch was reviewed and accepted
before the next one started. The audit-only steps (`D0`) are
documented as part of the chain even though they shipped no code,
because the data they exposed is what made `D1` safe to ship.

> **No new pipeline stages, no new providers, no new dependencies.**

The release covers, in order:

1. **`v0.8.3-B`** — split user-facing `answer_markdown` from the
   per-claim `audit_markdown` in `Synthesis`. The 6-section
   structured answer replaces the old "## Ответ + Детали по
   утверждениям" layout.
2. **`v0.8.3-B1`** — stabilize the 6-section user-facing answer
   contract (always-rendered, `_нет_` placeholders for empty
   buckets, short-answer synopsis derived from rendered bullet
   counts).
3. **`v0.8.3-C1`** — surface span-level evidence markers
   `[doc_<i>:<start>-<end>]` on confirmed bullets in
   `answer_markdown`. Wire `research_runner.py` to build
   per-fact markers and pass them through to the synthesizer via
   a new `inline_span_markers` kwarg.
4. **`v0.8.3-C1b / C1c / C1d`** — strict doc-index resolution:
   `inline_span_markers` return `None` (never `[doc_0:start-end]`
   as a fallback) when `_doc_index_for_window` cannot resolve the
   window's `source_url` to a document. Mirrored on
   `coverage["inline_citations"]` provenance. `C1d` is a
   docstring-only follow-up to keep the helper's docs honest
   after the `or 0` fallback was removed in `C1c`.
5. **`v0.8.3-C2-data`** — span-level refuting / numeric-mismatch
   evidence windows in the data layer (`hermes_deepresearch.py`).
   The producer now emits `refuting_evidence_windows` and
   `numeric_mismatch_evidence_windows` per fact; the consumer
   (synthesis) ignores them for now. Empty-list defaults so
   legacy code paths keep working; LLM branch stays URL-only.
6. **`v0.8.3-C3 / C3b`** — render contradiction / refutation
   span markers in the `## Противоречия / расхождения` section
   of `answer_markdown`. `C3b` makes the candidate-list
   selection verdict-specific: REFUTES only consults
   `refuting_evidence_windows`, NUMERIC_MISMATCH only
   `numeric_mismatch_evidence_windows`, CONFLICTING tries
   refuting first and may fall back to mismatch.
7. **`v0.8.3-D0` (audit, no code shipped)** — read-only audit
   of the dual marker format (`[N]` 1-based citation table id
   vs `[doc_M:start-end]` 0-based document span). Audit
   recommended Option A: do not change the marker format in
   v0.8.3, document the dual numbering instead.
8. **`v0.8.3-D1`** — append a short provenance note to the
   `## Источники` section of `answer_markdown` when at least
   one span marker is rendered. The note explains the dual
   numbering for the user; it is purely additive and is
   suppressed when no span marker is present (legacy answers
   stay byte-identical to `v0.8.3-B1`).

Pinned contracts carried into this release from the prior
`v0.8.2` series:

- `verify_sources()` return shape and verdict semantics from
  `v0.8.2-B1 / B2` are unchanged.
- `source_urls` whitelist semantics and canonical-match URL
  storage from `v0.8.2-B2` are unchanged.
- `TestEnvLlm` hygiene from `v0.8.2-C1` is unchanged (clean
  checkouts still pass `pytest -q` without fabricating a
  repo-root `.env_llm`).

---

## B — user-facing answer vs audit-markdown split

### Change

`Synthesis` (in `src/synthesis.py`) now exposes two markdown
fields:

- `answer_markdown`: the user-facing structured answer (the
  6-section layout the user reads).
- `audit_markdown`: the old per-claim breakdown, preserved for
  review / debugging. Not shown to end users.

The old single-field layout (a mix of user-facing summary and
per-claim details in one block) was useful for review but
unusable for end users. The split is a **shape-only** change —
`Synthesis` consumers that only read `answer_markdown` see a
cleaner, more readable answer; consumers that need the old
breakdown now read `audit_markdown`.

### Impact

- Public Python API: `Synthesis.answer_markdown` is the new
  primary field. `Synthesis.audit_markdown` is a new field,
  defaulting to `""` for backward compatibility with callers
  constructed by other code paths.
- Pipeline shape: unchanged.
- Configuration: unchanged.

---

## B1 — stable 6-section user-facing answer

### Change

`answer_markdown` now always renders the same 6 sections, in
this fixed order, with `_нет_` placeholders for empty buckets:

1. `## Краткий ответ` — short-answer synopsis, derived from
   rendered bullet counts.
2. `## Подтверждено источниками` — confirmed bullets.
3. `## Слабые или неподтверждённые сигналы` — weak bullets.
4. `## Противоречия / расхождения` — contradiction bullets.
5. `## Что не удалось проверить` — unverifiable bullets.
6. `## Источники` — the citation table.

The order is part of the contract. Reviewers and downstream
LLM prompts can rely on the section ordering and on the
presence of every section (even if the bucket is empty, the
section appears with `_нет_`).

### Impact

- Verdict routing (B-series from v0.8.2) is unchanged.
- Coverage / confidence / contradictions are unchanged in
  semantics; only the user-facing layout changed.
- The `_нет_` placeholder is a string literal that downstream
  tests can pattern-match against.

---

## C1 / C1b / C1c / C1d — confirmed-claim span markers

### C1 — surface span markers in confirmed bullets

`run_research()` builds per-fact `[doc_<i>:<start>-<end>]`
markers by aligning each `fact_result` with the
matching `Claim` in `state.claims` and resolving the Claim's
`evidence_window` through `_doc_index_for_window`. The
synthesizer accepts the new `inline_span_markers` kwarg and
appends the marker to the corresponding confirmed bullet,
**only** when the bucket routed the fact to the confirmed
section. Weak, contradiction, and unverifiable bullets never
get a span marker from this path.

### C1b — strict doc-index resolution (no `[doc_0:start-end]`
fallback)

The C1 helper initially accepted a `0` fallback when
`_doc_index_for_window` returned `None`. The fallback was
removed: an empty `source_url`, an unmatched URL, or a
malformed window all yield `None` for the marker, **never**
`[doc_0:start-end]`. The fabrication ban is the same one that
birthed the C1b concern in the first place — a `[doc_0:...]`
marker would be misleading because the user-facing citation
table in `answer_markdown` uses 1-based ids, so a `0` there
refers to a *different* document than `documents[0]`.

### C1c — no fabricated `doc_0` in `coverage["inline_citations"]`

The runner's `coverage["inline_citations"]` provenance data
follows the same strict rule. When a Claim's
`evidence_window.source_url` is empty or unmatched, the
runner skips the entry entirely — no `[doc_0:start-end]`
synthesized as a fallback.

### C1d — docstring cleanup

After the `or 0` fallback was removed in `C1c`, the
`_doc_index_for_window` docstring still described the old
behaviour. `C1d` is a docstring-only follow-up that aligns
the docstring with the post-`C1c` semantics, plus an inline
comment that the runner-level coverage builder is also strict.

### Impact

- Public Python API: `synthesize()` and `_render_user_markdown`
  gain `inline_span_markers: list[str | None] | None = None`
  with a default of `None` (byte-identical to `v0.8.3-B1` when
  omitted).
- Confirmed bullets: gain a span marker suffix
  `[doc_<i>:<s>-<e>]` (0-based document index, char span in
  the source text). No bullet is mutated beyond appending the
  marker.
- Coverage: `coverage["inline_citations"]` is now strict
  provenance (no fabricated fallback entries).
- All existing C1 tests, the orphan-URL guard tests, and the
  no-fabrication tests still pass without modification.

---

## C2-data — span-level refuting / numeric-mismatch evidence windows

### Change

`verify_sources()` (in `src/hermes_deepresearch.py`) now
populates two new per-fact fields on each
`verification_details[i]`:

- `refuting_evidence_windows: list[dict]` — span-level
  evidence for refuting matches. Each entry has
  `source_url`, `quote`, `offset_start`, `offset_end`,
  `method`.
- `numeric_mismatch_evidence_windows: list[dict]` — same
  shape, for numeric-mismatch matches.

Both fields default to `[]` for legacy code paths and for
the LLM branch (which is URL-only and does not carry span
info — empty span list for LLM-branch entries is correct).

### Why data-only

The consumer (synthesis) ignores the new fields for now. The
data layer is added first so a future render step can surface
the markers honestly. Empty-list semantics keep legacy code
paths working and make the consumer's "no render yet" branch
trivially correct.

### Helpers — fabrication discipline

Two new private helpers, `_find_negation_span` and
`_find_num_mismatch_span`, return `(offset_start, offset_end,
method) | None` for each match. They reuse the existing
regex from `_is_negated` and `_match_numeric_unit`. **They
return `None` on miss, never `(0, 0, method)`** — fabricating
a `(0, 0)` span would point at byte 0 of the source and
silently mis-cite.

### Impact

- Public Python API: `verify_sources()` return shape grew by
  2 fields per fact. Existing fields unchanged.
- Pipeline shape: unchanged.
- Synthesis: unchanged. The new fields are ignored until
  `v0.8.3-C3` (which surfaces them as span markers in the
  contradiction section).

---

## C3 / C3b — contradiction / refutation span markers

### C3 — render in the contradiction section

A new helper `_build_contradiction_markers(fact_results,
documents)` in `src/research_runner.py` builds per-fact
markers for `REFUTES` / `NUMERIC_MISMATCH` / `CONFLICTING`
verdicts. The synthesizer accepts the new
`inline_contradiction_markers` kwarg and appends the marker to
the corresponding contradiction bullet in
`## Противоречия / расхождения`. Same validation rules as
`C1`: malformed markers or unresolved URLs are silently
dropped, **never** `[doc_0:start-end]`.

### C3b — verdict-specific window selection

`C3` initially pooled `refuting_evidence_windows` and
`numeric_mismatch_evidence_windows` into a single candidate
list per fact, so a `REFUTES` fact could pick a
numeric-mismatch window and vice versa. `C3b` splits the
candidate list by verdict:

- `VERDICT_REFUTES` → `refuting_evidence_windows` only.
- `VERDICT_NUMERIC_MISMATCH` →
  `numeric_mismatch_evidence_windows` only.
- `VERDICT_CONFLICTING` → `refuting_evidence_windows` first,
  `numeric_mismatch_evidence_windows` as fallback.

A `REFUTES` fact whose refuting list is empty or unresolved
no longer picks up a numeric-mismatch window. The symmetric
trap is closed for `NUMERIC_MISMATCH`. `CONFLICTING` keeps
both options open, with refuting preferred.

### Impact

- Public Python API: `synthesize()` and
  `_render_user_markdown` gain
  `inline_contradiction_markers: list[str | None] | None =
  None`, default `None` (byte-identical to `v0.8.3-B1` when
  omitted).
- Contradiction bullets: gain a span marker suffix
  `[doc_<i>:<s>-<e>]` for the verdict-specific window.
- Confirmed / weak / unverifiable bullets: unchanged.

---

## D0 — read-only audit (no code shipped)

### Concern

The user-facing `answer_markdown` carries two different
numbering systems in the same bullet:

- `[N]` — the 1-based citation table id, used in
  `## Источники` and as the citation marker after each
  bullet.
- `[doc_M:start-end]` — the 0-based document index with char
  span offsets in the source text.

A user who tries to map `[1]` against `[doc_0:...]` for the
same source sees numbers that differ by 1. The C1b
fabrication-ban docstring already calls this out as a known
UX limitation; the D0 audit confirmed the limitation and
recommended Option A: **do not change the marker format in
v0.8.3, document the dual numbering instead**.

The D0 audit also surfaced four P2 cleanup items (see "Known
P2 cleanup items" below) for a future doc-cleanup batch.

### Impact

- No code change.
- The audit is included in this release notes as a project
  artefact (the v0.8.3 series ran in concern-driven cadence;
  the audit is part of the chain even though no code shipped).

---

## D1 — provenance note in `## Источники`

### Change

When at least one `[doc_M:start-end]` span marker is rendered
into any bullet, a short italic note is appended to the
`## Источники` section of `answer_markdown`, explaining the
dual numbering. The note is suppressed when no span marker
is present (legacy answers stay byte-identical to
`v0.8.3-B1`).

Wording (verbatim, Russian):

> _Примечание: [N] — номер источника в списке ниже;
> [doc_M:start-end] — технический указатель на фрагмент
> в документе M с символьными offset-ами._

### Where the note lives

Inside the `## Источники` section, after the citation table
list (or after `_нет_` if the table is empty). The note is
the last visible line of the section, before the trailing
empty line.

### Why derived from rendered bullets, not runner input

A new local `has_span_markers` flag in `_render_user_markdown`
is computed from the rendered bullet lists (`confirmed` +
`contradiction_bullets`), not from the runner's input kwargs.
This means the note tracks **what the user actually sees**,
not what the runner intended: a runner that passes
`inline_span_markers=["[doc_0:8-19]"]` for an `INSUFFICIENT`
verdict will not have the marker in any bullet, and the note
will not appear either. This avoids a "note explains a marker
that is nowhere to be found" UX trap, and also keeps the
`C1` contract (`"[doc_" not in md` for unverifiable claims)
intact.

### Impact

- Public Python API: `synthesize()` and
  `_render_user_markdown` unchanged in signature.
- User-facing output: one extra italic line in the
  `## Источники` section, only when at least one span marker
  is present.
- Legacy callers (no `inline_*_markers` kwargs, or explicit
  `None`): byte-identical `answer_markdown` to `v0.8.3-B1`.

---

## Known P2 cleanup items (NOT in this release, tracked
for a future doc-cleanup batch)

Listed for reviewer context only. **None of these were
started in v0.8.3** per release-prep scope discipline. The
D0 audit recommended deferring them to a single
`v0.8.3-D2` (or later) doc-cleanup batch.

- **Duplicate regex:** `_SPAN_MARKER_RE` (in
  `src/synthesis.py`) and `_CITATION_RE` (in
  `src/citations.py`) are two regexes for the same
  `[doc_<int>:<int>-<int>]` shape. Consolidate to a single
  regex in one module.
- **Centralize `[doc_N:start-end]` formatting:** three code
  paths emit the same format string —
  `_build_inline_span_markers`, `_build_contradiction_markers`
  in `src/research_runner.py`, and `format_cited_claim` in
  `src/citations.py`. Drift risk if any one is updated and
  the others aren't. Extract a single
  `format_span_marker(doc_index, start, end)` helper.
- **Docstring / comment cleanup:**
  - `_build_contradiction_markers` docstring has a
    `f"[doc_{i}:{start-end}]"` placeholder typo (double
    hyphen, not a real `f-string` template). Cosmetic, no
    runtime impact.
  - `Citation.source_index` (in `src/synthesis.py`) is a
    0-based field defined and populated, but **not** exported
    in `to_dict()`. Decision: remove (dead) or wire it
    (expose for `eval.py` / downstream consumers).
- **Use `_SPAN_MARKER_RE` instead of raw `"[doc_"`
  detection:** the D1 `has_span_markers` flag uses
  `any("[doc_" in b for b in ...)`. A safer test would use
  the validated regex (`_SPAN_MARKER_RE.search`) to avoid
  false positives on prose that happens to contain `[doc_`.

---

## CI

All release-prep commits are green on main. For the current
list of CI runs, see the GitHub Actions tab:
https://github.com/ergon73/searxng-deep-research/actions

The most recent CI run on `main` corresponds to the commit
pinned by the `v0.8.3` annotated tag. The exact run id is not
hardcoded here (it would become stale on the next push) —
open the Actions tab above and look for the most recent green
run with `v0.8.3` in the commit message.

---

## Verification (copy-paste runnable)

```bash
# clone and checkout the release tag
git clone https://github.com/ergon73/searxng-deep-research.git
cd searxng-deep-research
git checkout v0.8.3   # after the tag is published (see user-approval gate)

# local gates
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/eval.py --no-network --dry-run
ruff check src tests scripts
ruff format --check src scripts
```

Expected (verified at release-prep time; run the commands
above for the current numbers):

- `pytest -q` → all tests pass; the exact count drifts as
  the project grows, so a green run is the contract — not a
  specific number. New in v0.8.3: 4 tests in
  `tests/test_synthesis_v083d.py` (D1), 4 tests in
  `tests/test_synthesis_v083c3.py` (C3b).
- `eval --dry-run` → 8/8 queries, routing accuracy 100%,
  0 errors.
- `ruff check` → all checks passed.
- `ruff format --check` → 22 files already formatted.

---

## Backward compatibility

- **No breaking changes to the public Python API.** All new
  `synthesize()` / `_render_user_markdown` kwargs have
  defaults of `None` and produce byte-identical
  `answer_markdown` to `v0.8.3-B1` when omitted. The
  `Synthesis.audit_markdown` field is a new field that
  defaults to `""`.
- **No new pipeline stages, no new providers, no new
  dependencies.** `requirements.txt` is unchanged. `pyproject.toml`
  version bumps from `0.8.2` to `0.8.3`; all other
  `pyproject.toml` fields are unchanged.
- **Existing tag users:** `v0.8.2 → v0.8.3` is a
  non-breaking upgrade. `pip install -e ".[dev]"` from
  `v0.8.3` works exactly as it did from `v0.8.2`.
- **Marker format:** `[N]` and `[doc_M:start-end]` keep the
  same shape as in v0.8.3-C1 / C3. No change to the regexes,
  to the field names, or to the per-bucket rendering rules.

---

## Next-phase candidates (post-tag, NOT in this release)

Listed for reviewer context only. **Not started in v0.8.3**
per release-prep scope discipline.

- **D2 / doc-cleanup batch** — address the four P2 items
  listed above (regex dedup, centralize span-marker
  formatting, `Citation.source_index` decision, raw
  `"[doc_"` detection → validated regex).
- **LLM_ENV_FILE portability** — review-time work for a
  future version. Not in `v0.8.3` scope.
- **Ranking / routing follow-ups** — held for a future
  version per the `v0.8.2-C4` "no scope creep" discipline.

---

## References

- `.hermes/plans/ISSUES.md` — open/closed issues tracker.
  v0.8.3 P2 items are recorded there as a project-state
  artefact, independent of the Git tag.
- `release-workflow-tag-and-notes` skill
  (`references/tag-target-self-ref-trap.md` and
  `references/pre-tag-readonly-review.md`) — explains why
  this file intentionally does not hardcode the release
  commit SHA, and how the `release-prep / code-only /
  tag-publication / next-phase` four-phase sequence is
  enforced.
