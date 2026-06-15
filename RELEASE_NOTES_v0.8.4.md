# Release Notes — v0.8.4

**Date:** 15 June 2026
**Tag:** `v0.8.4` → this release commit (see `git tag -l 'v0.8.*'`)
**Type:** Marker / provenance hardening — defensive substring detection cleanup
**Diff vs v0.8.3:** reviewed marker/provenance cleanup chain (A1); see `git log v0.8.3..v0.8.4` for the exact commit list
**CI status:** all release commits green on main (see CI section below)

---

## Summary

`v0.8.4` is a marker / provenance hardening release that replaces a
raw substring check for span markers in `answer_markdown` with a
validated regex match. It is a no-runtime-format-change cleanup
shipped to defend against false positives in the dual-marker
provenance note introduced in `v0.8.3-D1`.

The release is structured as a single code-only sub-batch
(`v0.8.4-A1`) plus the release-prep docs that this file is part of.
The D0 audit items that motivated `A1` (consolidate the two
span-marker regexes, centralize the `[doc_N:start-end]` format
string, drop the dead `Citation.source_index` field, replace the
raw `"[doc_"` detection) are deferred to a future `D2 / doc-cleanup`
batch — they are real, but not in `v0.8.4` scope.

> **No runtime marker format change. No citations.py / marker
> centralization yet. No new pipeline stages, no new providers, no
> new dependencies.**

The release covers:

1. **`v0.8.4-A1`** — replace `any("[doc_" in b for b in ...)` with
   `_SPAN_MARKER_RE.search(...)` in the `has_span_markers` detection
   path of `_render_user_markdown`. This stops the provenance note
   from firing on prose that *happens* to contain the literal
   `[doc_` (for example a URL like
   `http://example.com/[doc_fake]/x` whose `[doc_fake]` fragment
   survives the `_md_escape` pass, since URLs are not escaped in
   the citation table). Real `[doc_N:start-end]` markers still
   trigger the provenance note. The A1 batch also fixes a cosmetic
   docstring typo in `_build_contradiction_markers` (a stale
   `f"[doc_{i}:{start-end}]"` placeholder with a double hyphen,
   no runtime impact).

Pinned contracts carried into this release from the prior
`v0.8.3` series:

- The span-marker format `[doc_N:start-end]` is unchanged.
- `v0.8.3-D1` provenance note semantics are unchanged: append
  the note only when a real span marker is present, stay
  byte-identical to `v0.8.3-B1` otherwise.
- `verify_sources()` return shape and verdict semantics from
  `v0.8.2-B1 / B2` are unchanged.
- `source_urls` whitelist semantics and canonical-match URL
  storage from `v0.8.2-B2` are unchanged.
- `Synthesis.audit_markdown` is unchanged.

---

## Known deferred cleanup items (NOT in this release)

Surfaced by the `v0.8.3-D0` audit and tracked in
`.hermes/plans/ISSUES.md` as `DEFERRED→D2`:

- **Consolidate `_SPAN_MARKER_RE` / `_CITATION_RE`.** Two regexes
  for the same shape live in `src/synthesis.py` and
  `src/citations.py`. Drift risk if either side updates without
  the other.
- **Centralize `[doc_N:start-end]` formatting.** Three code paths
  emit the same `f"[doc_{N}:{start}-{end}]"` format string:
  `_build_inline_span_markers` / `_build_contradiction_markers`
  (in `src/research_runner.py`) and `format_cited_claim` (in
  `src/citations.py`). Extract a single
  `format_span_marker(doc_index, start, end)` helper.
- **Decide on `Citation.source_index`.** Field is defined and
  populated (= `id - 1`, 0-based index in dedup'd
  `source_candidates`) in `src/synthesis.py::Citation`, but not
  exported in `to_dict()`. Decision needed: remove (dead) or
  wire it (expose for `eval.py` / downstream consumers).
- **Marker-format centralization** (the D2 batch as a whole) is
  held for a future version per the `v0.8.2-C4` "no scope creep"
  discipline.

> The current marker format `[doc_N:start-end]` is kept as-is for
> `v0.8.4`. The D2 batch will not change the format either — it
> will only centralize the helpers that produce it.

---

## CI

All release-prep commits are green on main. For the current
list of CI runs, see the GitHub Actions tab:
https://github.com/ergon73/searxng-deep-research/actions

The most recent CI run on `main` corresponds to the commit
pinned by the `v0.8.4` annotated tag. The exact run id is not
hardcoded here (it would become stale on the next push) —
open the Actions tab above and look for the most recent green
run with `v0.8.4` in the commit message.

---

## Verification (copy-paste runnable)

```bash
# clone and checkout the release tag
git clone https://github.com/ergon73/searxng-deep-research.git
cd searxng-deep-research
git checkout v0.8.4   # after the tag is published (see user-approval gate)

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
  specific number.
- `eval --dry-run` → 8/8 queries, routing accuracy 100%,
  0 errors.
- `ruff check` → all checks passed.
- `ruff format --check` → all files already formatted.

---

## Backward compatibility

- **No breaking changes to the public Python API.** `A1`
  replaces an internal detection helper only; no public
  signature changes, no new `synthesize()` / `_render_user_markdown`
  kwargs, no schema changes.
- **No new pipeline stages, no new providers, no new
  dependencies.** `requirements.txt` is unchanged. `pyproject.toml`
  version bumps from `0.8.3` to `0.8.4`; all other
  `pyproject.toml` fields are unchanged.
- **Existing tag users:** `v0.8.3 → v0.8.4` is a
  non-breaking upgrade. `pip install -e ".[dev]"` from
  `v0.8.4` works exactly as it did from `v0.8.3`.
- **Marker format:** `[N]` and `[doc_M:start-end]` keep the
  same shape as in v0.8.3-C1 / C3. No change to the regexes
  themselves, to the field names, or to the per-bucket
  rendering rules. The `A1` swap is invisible at the
  `answer_markdown` byte level for inputs that contain real
  span markers.
- **Provenance note:** trigger conditions are unchanged from
  `v0.8.3-D1`. Real markers → note appears; no markers →
  no note (byte-identical to `v0.8.3-B1`).

---

## Next-phase candidates (post-tag, NOT in this release)

Listed for reviewer context only. **Not started in v0.8.4**
per release-prep scope discipline.

- **D2 / doc-cleanup batch** — address the four P2 items
  listed above (regex dedup, centralize span-marker
  formatting, `Citation.source_index` decision, raw
  `"[doc_"` detection → validated regex). The `A1` batch is
  the smallest defensible subset of `D2`; the rest follows
  in a future release.
- **LLM_ENV_FILE portability** — review-time work for a
  future version. Not in `v0.8.4` scope.
- **Ranking / routing follow-ups** — held for a future
  version per the `v0.8.2-C4` "no scope creep" discipline.
- **LangGraph / Tavily / Exa / Firecrawl** provider surface
  — out of scope for `v0.8.x`. Held for `v0.9.0+` per the
  external review.

---

## References

- `.hermes/plans/ISSUES.md` — open/closed issues tracker.
  `v0.8.4` P2 items are recorded there as a project-state
  artefact, independent of the Git tag.
- `release-workflow-tag-and-notes` skill
  (`references/tag-target-self-ref-trap.md` and
  `references/pre-tag-readonly-review.md`) — explains why
  this file intentionally does not hardcode the release
  commit SHA, and how the `release-prep / code-only /
  tag-publication / next-phase` four-phase sequence is
  enforced.
