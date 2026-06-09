# Release Notes — v0.8.1.3

**Date:** 9 June 2026
**Tag:** `v0.8.1.3` → `76f498b`
**Type:** Hygiene mini-batch on top of v0.8.1.2
**Diff vs v0.8.1.2:** 1 commit on main (`76f498b`), no research logic change

---

## Summary

`v0.8.1.3` is a follow-up hygiene release that closes gaps surfaced by the
second external ChatGPT review of `v0.8.1.2`. The themes are:

1. Issue ledger consistency (ISSUES.md #008 = DONE everywhere, not WONTFIX in detail section)
2. Per-file-ignores alignment between ISSUES.md and `pyproject.toml`
3. Release notes commit count accuracy (was 5, actually 6 in the SHA table)
4. Documentation drift fixes that the first hygiene pass missed

**No new pipeline stages, no new providers, no new dependencies.**

One commit:

| SHA | Title | Purpose |
|---|---|---|
| `76f498b` | docs: v0.8.1.2 follow-up | pyproject version bump, ISSUES #008 → DONE, AGENTS entrypoint, eval docstring formula, tests ignore shrink, release notes scaffold |

This release note file itself is the only addition in `v0.8.1.3` proper
(written at tag time, not on main).

---

## Changed

### `76f498b` — docs: v0.8.1.2 follow-up

- `pyproject.toml`: version `0.8.1.1` → `0.8.1.2` (later bumped to `0.8.1.3` at
  tag time for release notes discipline)
- `.hermes/plans/ISSUES.md`:
  - L37 (Index by severity): `#008` already `DONE 2026-06-09` with full
    ruff-cleanup reference; per-file-ignores text now lists the actual final
    rules (`["S101", "S105", "S106", "S108", "E402"]`) not the older blanket
    `["S", "B", "E402"]`
  - L146 (Detail #008): header `[LOW | WONTFIX | pre-existing]` replaced with
    `[LOW | DONE | 2026-06-09, ref: 72f8b16]`; full "что было / что сделано /
    результат" block with mechanical counts, `# noqa` list, `_SECRET_KEY_NAME_PATTERNS`
    rename, per-file-ignores justification (with reference to
    `tests/test_url_safety.py` validating S310 false-positive)
- `AGENTS.md` L12–L13: recommended entry point now
  `src/research_runner.py::run_research()` / `deep_research_v2()`
  (typed, confirmation-aware, v0.8.0+); legacy `hermes_deepresearch.py::deep_research()`
  explicitly labelled as backward-compat strangler
- `scripts/eval.py` docstring: real formula `0.45 + 0.22 + 0.22 + 0.11 = 1.00`
  documented; `needs_confirmation` explicitly labelled as diagnostic-only
  safety gate (not part of Quality Score)
- `pyproject.toml` `[tool.ruff.lint.per-file-ignores]`: `tests/*` ignores shrunk
  from blanket `["S", "B", "E402"]` to surgical `["S101", "S105", "S106", "S108", "E402"]`
  per external review recommendation; `S105` retained (test fixture dummy
  secrets, not real keys)
- 1 surgical `# noqa: B007` in `tests/test_evidence_windows.py` (loop variable
  not used in body; auto-fix produced no change, so explicit suppression)
- `tests/test_evidence_windows.py` L175: `zip()` call now uses
  `zip(..., strict=False)` per ruff auto-fix
- `RELEASE_NOTES_v0.8.1.2.md` (this file's predecessor) updated to:
  - Add follow-up note that `76f498b` is post-release on main, not in `v0.8.1.2` tag
  - Correct commit count from "5 commits" to "5 in tag, +1 follow-up on main"
  - Add `S105` to listed per-file-ignores for `tests/*`

---

## Why v0.8.1.3 instead of amending v0.8.1.2

- `v0.8.1.2` is already published and review-tagged at `b84de39` with its own
  green CI run (`27198068324`)
- `76f498b` adds post-release hygiene on main, and reviewer noted confusion
  between "release commit" and "follow-up on main"
- Bumping the version number at tag time to `v0.8.1.3` makes the relationship
  explicit: `v0.8.1.2` (release) → `76f498b` (main follow-up) → `v0.8.1.3` (tag)
- This is the same pattern as `v0.8.1.0` → `v0.8.1.1` (4 hotfix commits on main
  before the v0.8.1.1 tag)

---

## Verification

| Check | Result |
|---|---|
| `ruff check src tests scripts` (local) | All checks passed |
| `ruff format --check src scripts` (local) | 22 already formatted |
| `pytest -q` (local) | 648/648 passed |
| `pytest -q` (GitHub Actions) | success — see [run #27198672486](https://github.com/ergon73/searxng-deep-research/actions/runs/27198672486) |
| `scripts/eval.py --no-network --dry-run` (local) | 0 errors, 8/8 queries loaded, routing accuracy 100% |

---

## Known issues

Closed in v0.8.1.3:
- **#008** — ISSUES.md detail section now consistent with index (DONE, not WONTFIX)
- **Drift** — `tests/*` per-file-ignores consistent across `pyproject.toml`,
  `ISSUES.md` (both index and detail), `RELEASE_NOTES_v0.8.1.2.md`

Still open (out of v0.8.1.3 scope, deferred per external review):
- **P1.1** — CI workflow expand (`ruff format --check` + `eval --dry-run` step) — v0.8.1.4 mini
- **P1.2** — Node.js 20 → 24 actions deprecation fix — v0.8.1.4 mini
- **P1.3** — License drift fix (MIT LICENSE file + sync docs) — v0.8.1.4 mini
- **P1.4** — `llm_verifier.py` hardcoded `/opt/searxng/.env_llm` → `LLM_ENV_FILE` env — v0.8.1.4 mini
- **P1.5** — `eval.py` silent skip → counters — v0.8.1.4 mini
- **v0.8.2** — verification correctness (numeric matcher scan all, LLM source_urls, prompt wording)
- **v0.8.3** — evidence-bound synthesis
- **v0.9** — ranking/routing (search_votes, news/docs routing)
- **#001** — top-1 ranking heuristic (P5)
- **#002** — `_looks_like_news()` not implemented (P5)
- **#003** — date dedup regression (P3.1)
- **#004** — БПЛА ↔ беспилотник morphology (P5)
- **#013** — `reformulate()` broken for RU (P5)
- **#014** — long-query degradation (MITIGATED, not fully closed)
- **#015** — narrative entity filtering (LOW)

See `.hermes/plans/ISSUES.md` for the full tracker.

---

## Backwards compatibility

- No breaking changes to public API (`run_research()` / `deep_research_v2()` /
  `deep_research()` all unchanged)
- `pyproject.toml` version: `0.8.1.2` → `0.8.1.3`
- No new dependencies
- No new env vars required
