# Release Notes — v0.8.1.2

**Date:** 9 June 2026
**Tag:** `v0.8.1.2` → `b84de39` (release commit)
**Follow-up on main:** `76f498b` adds v0.8.1.2 follow-up (pyproject version, ISSUES #008, AGENTS entrypoint, eval docstring, tests ignore shrink). Tagged separately as `v0.8.1.3`.
**Type:** Hygiene + CI fix release on top of v0.8.1.1
**Diff vs v0.8.1.1:** 5 commits in tag `v0.8.1.2` (b84de39), +1 follow-up on main (76f498b) → v0.8.1.3, no research logic change

---

## Summary

`v0.8.1.2` closes gaps surfaced by the external ChatGPT review of
`v0.8.1.1`. **No new pipeline stages, no new providers, no new
dependencies.** The theme is repo hygiene + a clean public CI signal.

Five commits:

| SHA | Title | Purpose |
|---|---|---|
| `72f8b16` | ruff cleanup v0.8.1.2: 207→0 errors | Lint baseline (closes #008) |
| `4a7eef0` | docs: sync v0.8.1.2 | Drift in 6 .md files (project name, version, test count) |
| `2e6d1e7` | fix(ci): auto-fix ruff F401/I001/F541 in tests/ | Tests were excluded from per-file-ignores for F/I; auto-fixed |
| `641d36e` | fix(ci): create dummy .env_llm in CI workflow | test_compose_config.py::TestEnvLlm needs file presence |
| `5d3a1d8` | fix(ci): also create .env_llm in repo root + env var | LLMVerifier reads from env or /opt/searxng/.env_llm |
| `b84de39` | fix(ci): use env: block + python -c | Final form: GitHub Actions env: block + Python file write |

---

## Changed

### Ruff cleanup (`72f8b16`)

- 207 ruff errors → 0 (auto-fix + unsafe-fixes)
- 22 files reformatted via `ruff format src scripts`
- Per-file-ignores added in `pyproject.toml` with documented justification:
  - `tests/*` = `["S101", "S105", "S106", "S108", "E402"]` (initially broader, shrunk in
    follow-up commit per external review recommendation; S105 = test fixture dummy secrets)
  - `scripts/e2e_*.py` = `["S108"]` (intentional `/tmp` smoke trace dirs)
  - `src/hermes_deepresearch.py`, `src/hermes_searxng.py`, `src/llm_verifier.py`
    = `["S310"]` (SSRF guarded at function level by `_is_safe_fetch_url()` /
    `_safe_urlopen()` / `SafeRedirectHandler`; see `tests/test_url_safety.py`)
- 5 surgical `# noqa` comments with justification (eval.py, query_adaptation.py,
  release_packaging.py, redact.py)
- `src/redact.py`: renamed `_SECRET_KEY_NAMES` → `_SECRET_KEY_NAME_PATTERNS`
  (regex patterns, not credentials; ruff S105 false positive)

### Docs sync (`4a7eef0`)

- `README.md`: version `v0.8.1` → `v0.8.1.2`, test count `586` → `648`
- `AGENTS.md`: project name `deep-research-project` → `searxng-deep-research`,
  version `v0.8` → `v0.8.1.2`
- `SECURITY.md`: project name fix
- `README_RELEASE.md`: project name `hermes-deepresearch` → `searxng-deep-research`,
  test count `393` → `648`
- `ARCHITECTURE.md`: version `v0.8.0` → `v0.8.1.2`; new blocks for v0.8.1.1 hotfix
  series and v0.8.1.2 ruff cleanup
- `.hermes/plans/ISSUES.md`: project name fix; verification commands block
  refreshed (404 → 648 passed; ruff state clean)

Historical release notes (`RELEASE_NOTES_v0.8.{0,1,1.1}.md`) and review docs
(`docs/CHATGPT_REVIEW_REQUEST_v0.8.0.md`, `docs/SELF_REVIEW_v0.8.0.md`) left
unchanged — they are immutable snapshots of past state.

### CI fixes (`2e6d1e7`, `641d36e`, `5d3a1d8`, `b84de39`)

- `tests/` ruff errors auto-fixed (F401 unused imports, I001 unsorted imports,
  F541 f-string without placeholder, F841 unused locals)
- `.github/workflows/ci.yml` now creates dummy `.env_llm` in both repo root
  (for `test_compose_config.py::TestEnvLlm`) and `/opt/searxng/` (for
  `src/llm_verifier.py::_load_api_key`), and exports `OPENROUTER_API_KEY` via
  GitHub Actions native `env:` block (avoids bash `export FOO=bar cmd` syntax
  pitfall)
- All placeholder values are obvious dummies; real secrets are set in
  `/opt/searxng/.env_llm` by the operator, never committed, never in CI cache

---

## Verification

| Check | Result |
|---|---|
| `ruff check src tests scripts` (local) | All checks passed |
| `ruff format --check src scripts` (local) | 22 already formatted |
| `pytest -q` (local) | 648/648 passed |
| `pytest -q` (GitHub Actions) | success — see [run #27198068324](https://github.com/ergon73/searxng-deep-research/actions/runs/27198068324) |
| `scripts/eval.py --no-network --dry-run` (local) | 0 errors, 8/8 queries loaded, routing accuracy 100% |

---

## Known issues

Closed in v0.8.1.2:
- **#008** — ruff pre-existing style. Was WONTFIX, now DONE.

Still open (out of v0.8.1.2 scope):
- **#001** — top-1 ranking still uses heuristic, not `search_votes` (P5 deferred)
- **#002** — `_looks_like_news()` not implemented (P5 deferred)
- **#003** — date dedup regression in smoke (P3.1)
- **#004** — БПЛА ↔ беспилотник morphology gap (P5)
- **#013** — `reformulate()` broken (returns None for 100% RU queries)
- **#014** — long-query degradation (MITIGATED, not fully closed)

See `.hermes/plans/ISSUES.md` for the full tracker.

---

## Backwards compatibility

- No breaking changes to public API (`run_research()` / `deep_research_v2()` /
  `deep_research()` all unchanged)
- `pyproject.toml` version: `0.8.1.1` → `0.8.1.2`
- No new dependencies
- No new env vars required (CI works with placeholders; real ops uses
  `/opt/searxng/.env_llm` unchanged)
