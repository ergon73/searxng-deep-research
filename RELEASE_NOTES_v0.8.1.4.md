# Release Notes — v0.8.1.4

**Date:** 9 June 2026
**Tag:** `v0.8.1.4` → `05e7554`
**Type:** Pre-v0.8.2 bugfix (eval correctness)
**Diff vs v0.8.1.3:** 1 commit on main (`05e7554`), no research logic change

---

## Summary

`v0.8.1.4` closes a critical bug in `scripts/eval.py` online fetch loop that
was identified by the third external ChatGPT review. The bug made the
online eval pipeline effectively skip nearly every normal URL because
the dedup logic checked canonical equality against a set that already
contained the raw URL.

**No new pipeline stages, no new providers, no new dependencies.**

One commit:

| SHA | Title | Purpose |
|---|---|---|
| `05e7554` | fix(eval): v0.8.1.4 canonical-dedup regression | Split `seen` into `seen_raw` + `seen_canon`; dedup checks before `seen.add(...)`; 5 new regression tests |

---

## Bug

In `scripts/eval.py::_run_online_pipeline()` (v0.8.1.2 and earlier), the
fetch loop used a single `seen: set[str]` for both raw and canonical URL
dedup, but called `seen.add(url)` **before** the canonical check:

```python
seen.add(url)                  # raw URL added FIRST
try:
    canon = canonical_url(url)
    if canon in seen:          # if canon == url, this is True → skip!
        result.urls_skipped_duplicate += 1
        continue
    seen.add(canon)
except Exception:
    result.urls_skipped_canonical += 1
```

For any URL where `canonical_url(url) == url` (the common case for URLs
without `utm_*` tracking parameters), `canon in seen` returned `True` and
the URL was skipped before `fetch_url()` was ever called.

### Impact

- Online eval (`--online` mode) could fetch **zero** sources for typical
  search result sets
- `urls_skipped_duplicate` counter would inflate misleadingly
- The 11 silent-skip counters added in v0.8.1.3 Batch B would have masked
  the regression behind a stable Quality Score

### Fix

Split into two sets — `seen_raw` (for raw URL dedup) and `seen_canon` (for
canonical URL dedup). Dedup checks happen **before** the `seen.add(...)`
calls, so:

- Normal URL where `canonical_url(url) == url` is fetched (not skipped)
- URL A and URL A?utm_source=x are treated as one canonical duplicate
- `urls_skipped_duplicate` increments only for true duplicates
- `urls_skipped_canonical` increments when `canonical_url()` raises

---

## Tests

`tests/test_eval_canonical_dedup.py` (new file, 5 cases, all pass):

1. `test_normal_url_without_tracking_is_fetched` — **primary regression
   test**; was failing before fix, now passes
2. `test_utm_duplicate_is_skipped` — A and A?utm_source=x = 1 canonical
   dedup
3. `test_distinct_urls_are_fetched` — distinct canonical URLs both fetch
4. `test_canonical_url_exception_increments_canonical_skip` — bad URL
   counted under `urls_skipped_canonical`
5. `test_deny_pattern_increments_deny_skip` — `_URL_DENY_PATTERNS`
   matched and counted

All tests use `monkeypatch.setattr` on `hermes_searxng.web_search` and
`hermes_deepresearch.fetch_url` (the late imports in `_run_online_pipeline`
read module-level attributes at call time, so monkeypatch works correctly).

---

## Release notes consistency fix

While here, also updated `RELEASE_NOTES_v0.8.1.3.md` to reflect actual
`v0.8.1.3` contents (2 commits: `1aa69a8` + `a15bd3c`) instead of the
stale "1 commit / `76f498b`" reference left over from when the file
was first created during Batch A.

Per the **"no stale release notes inside same version"** rule from the
third external review: when a release notes file is created before
all commits in that version are in place, the next version's release
must update the previous file's "Tag" and "Known issues" sections to
match the eventual final state.

This release notes file itself is the documentation for the v0.8.1.4
bug fix and the release-notes-consistency fix.

---

## Verification

| Check | Result |
|---|---|
| `ruff check src tests scripts` (local) | All checks passed |
| `ruff format --check src scripts` (local) | 22 already formatted |
| `pytest -q` (local) | 650/653 passed (3 env-dep pre-existing fails, not regression) |
| `pytest -q` GitHub Actions | success — [run #27216097000](https://github.com/ergon73/searxng-deep-research/actions/runs/27216097000) (via re-trigger `fba0d30` after first run `27215289451` stalled in queue) |
| `scripts/eval.py --no-network --dry-run` (local) | 0 errors, 8/8 queries loaded, routing accuracy 100% |
| `tests/test_eval_canonical_dedup.py` (new) | 5/5 passed |

---

## Known issues

Closed in v0.8.1.4:
- **Bug** — eval.py canonical-dedup regression: online eval was
  skipping nearly every normal URL (fixed in `05e7554`)

Still open (out of v0.8.1.4 scope, deferred per external review):
- **P1.4** — `llm_verifier.py` hardcoded `/opt/searxng/.env_llm` →
  `LLM_ENV_FILE` env var (v0.8.1.5 mini)
- **v0.8.2** — verification correctness (numeric matcher scan all
  occurrences, LLM source_urls, prompt wording "evidence sources")
- **v0.8.3** — evidence-bound synthesis
- **v0.9** — ranking/routing (search_votes, news/docs routing)
- **#001**–#015** — see `.hermes/plans/ISSUES.md`
- **#016** — Node 24 warning remains (LOW/monitor)

See `.hermes/plans/ISSUES.md` for the full tracker.

---

## Backwards compatibility

- No breaking changes to public API (`run_research()` / `deep_research_v2()` /
  `deep_research()` all unchanged)
- `pyproject.toml` version: `0.8.1.3` → `0.8.1.4`
- No new dependencies
- No new env vars required
- `eval.py` `aggregate_results()` and `format_report()` output now
  includes `skip_counters` dict (added in v0.8.1.3 Batch B) — backward
  compatible: existing keys unchanged, new dict is additive
