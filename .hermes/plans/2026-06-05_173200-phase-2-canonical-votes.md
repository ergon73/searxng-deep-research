# Phase 2 Implementation Plan — canonical URL dedup + multi-query votes

> **For Hermes:** Use this plan with the `/senior-dev` bundle. TDD-first: failing test → red → minimal patch → green → verify.

**Goal:** Multi-query votes (`search_votes`, `found_by_engines`, `found_by_queries`) correctly aggregate across queries and engines. The same canonical URL found via different `(query, engine)` pairs is treated as one source with high vote count, not as multiple sources with no votes.

**Architecture:** Two surgical edits in `hermes_deepresearch.py::deep_research()` (only — `deep_search()` already uses `canonical_url` correctly, do not touch):
1. **Aggregation site (L812-826):** replace `sources_meta[u] = {...}` (assignment) with `sources_meta.setdefault(canon, {...}).update({...})` pattern, keyed on **canonical URL**, and add `raw_urls: set()` so the original URL is preserved for fetching.
2. **Lookup site (L833-841, L850):** change `top_urls` to contain **canonical** URLs (use `canonical_url(...)` on L836 too), and `meta = sources_meta.get(u, {})` already works once both sides agree on canonical.

No new dependencies. No new public API. `deep_search()` and `deep_research()` signatures unchanged. Output schema extended (adds `raw_urls` to `fr`, but `found_by_engines/queries/search_votes` were already there — they just had wrong values).

**Tech Stack:** Python 3.11, pytest 9, urllib stdlib (no new deps).

**Out of scope:**
- `web_search()` (it's in `hermes_searxng.py`, not touched in Phase 2)
- `verify_sources()` (Phase 4)
- `_search_result_score` ranking weights (Phase 5)
- News routing / `categories=news` (Phase 5)
- Anything in `llm_verifier.py` (Phase 4)

---

## Audit findings (read-only, already done)

The current bug is in `hermes_deepresearch.py` at two sites that disagree on what "the URL" means.

### Site 1: aggregation (L812-826) — `sources_meta` dict

```python
for q in queries:
    qlang = "en" if q == reformulated else lang
    res = web_search(q, lang=qlang, time_range=effective_time_range, max_results=top_n * 2)
    for rank, r in enumerate(res):
        all_search.append({**r, "_query": q, "_lang": qlang, "_rank": rank})
        u = canonical_url(r.get("url", ""))
        if u and u not in seen_urls:
            seen_urls.add(u)
            sources_meta[u] = {                              # ← BUG: assignment, not update
                "engines": {r.get("engine", "")},
                "queries": {q},
                "ranks": [rank],
                "snippets": [r.get("snippet", "")],
                "titles": [r.get("title", "")],
            }
```

When the same canonical URL appears via a second `(query, engine)` pair, the second hit is **silently dropped** because `u in seen_urls` is True. `sources_meta[u] = {...}` would only run if the `if` were False, but the `if` gates the assignment — so aggregation never happens. Result: a URL found 5 times by 2 queries × 3 engines still has `engines={1}, queries={1}`.

### Site 2: top_urls (L833-841) — raw vs canonical mismatch

```python
top_urls = []
seen = set()
for r in all_search:
    u = r.get("url", "").split("#")[0]          # ← BUG: raw URL, not canonical
    if u and u not in seen:
        seen.add(u)
        top_urls.append(u)
        if len(top_urls) >= top_n:
            break
```

Compared to L817 which uses `canonical_url(r.get("url", ""))`. The two URLs can differ when the URL has tracking params (`utm_*`, `fbclid`, etc.) or case-different host. Result: `top_urls[0]` may be `https://example.com/a?utm_source=x` while `sources_meta` is keyed on `https://example.com/a`.

### Site 3: meta lookup (L850)

```python
meta = sources_meta.get(u, {})                    # ← BUG: looks up by raw URL
```

`u` here is the **raw URL from `top_urls`**, not the canonical. So `meta` is `{}` for any URL that was deduped via `canonical_url` on L817 but kept its raw form on L836.

### Confirmed via stub (already done)

I ran a minimal stub with two `web_search` results for the same article (`https://example.com/a?utm_source=x` from bing, `https://example.com/a` from google). Observed:

```
sources_meta['https://example.com/a'] = {engines: {'bing'}, queries: {'orig'}, snippets: ['s1']}
                                                  # google's hit was SKIPPED entirely

top_urls = ['https://example.com/a?utm_source=x', 'https://example.com/a']
meta for first:  {}        # raw URL with utm — sources_meta key is canonical, no match
meta for second: {engines: {'bing'}, queries: {'orig'}, ...}  # found the first hit's meta, not aggregated
```

Three downstream effects:
1. `found_by_engines = []` on the first top-URL (because meta is empty).
2. `search_votes = 0` on the first top-URL.
3. The first top-URL loses `engine`, `search_snippet`, and `title` (if fetch returns title=None).
4. The aggregated stats `stats.unique_sources = len(sources_meta)` is correct, but no caller can use it because per-source vote counts are wrong.

### Test coverage gap

`tests/test_canonical_url.py` (16 tests) covers `canonical_url()` itself: fragment, utm_*, fbclid, gclid, yclid, lowercased host, default ports, etc. It does **not** cover:
- `sources_meta` aggregation across queries/engines.
- `top_urls` using canonical for both storage and lookup.
- Raw URL preservation.

**These are the regression net for Phase 2.**

---

## Tests to write BEFORE patching

All new tests go into a new file `tests/test_deepresearch_votes.py`. They use the existing `conftest.py` (which forces `LLM_DISABLED = True` and adds `/opt/searxng/src` to `sys.path` — note: actual code is in the project root, not `src/`, but `conftest.py` is harmless for import because `hermes_deepresearch.py` is at the project root and is added to path via cwd).

Wait — `conftest.py:13` does `sys.path.insert(0, "/opt/searxng/src")` which **doesn't exist**. The other 5 test files import via `from hermes_deepresearch import ...` and work because pytest's `rootdir` and `testpaths = ["tests"]` together with `pythonpath` somewhere must be doing the right thing. **I won't change conftest in Phase 2** (that's a Phase 8 / docs-sync concern). I'll add `sys.path` to the new test file's docstring and let pytest discover it normally.

### Tests to add (4 tests, all in `tests/test_deepresearch_votes.py`)

```python
"""
tests/test_deepresearch_votes.py — multi-query votes aggregation regression.

Locks in DR-05062026(3) §4 (multi-query votes broken) and §5 (canonical lookup).
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest

from hermes_deepresearch import deep_research


def _fake_search_results(*triples):
    """triples = list of (query, engine, url[, snippet]). Returns a function
    that web_search() can use to dispatch by query."""
    table = {}
    for t in triples:
        q, eng, u = t[:3]
        snip = t[3] if len(t) > 3 else f"snippet for {u}"
        table.setdefault(q, []).append({"url": u, "engine": eng, "title": f"title {u}", "snippet": snip})
    def fake(q, **kwargs):
        return table.get(q, [])
    return fake


class TestMultiQueryVotes:
    def test_same_canonical_url_two_queries_aggregates(self):
        # Same article found via two different queries on two different engines.
        # Canonical URL should aggregate, search_votes should be 2.
        fake = _fake_search_results(
            ("БПЛА Москва",   "bing",   "https://example.com/a?utm_source=x"),
            ("drone Moscow", "google", "https://example.com/a"),
        )
        with patch("hermes_deepresearch.web_search", side_effect=fake), \
             patch("hermes_deepresearch.fetch_url", return_value={"url": "https://example.com/a", "title": "t", "text": "x", "length": 1, "error": None}), \
             patch("hermes_deepresearch.verify_sources", return_value={"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0, "verification_details": [], "llm_enhanced": False, "llm_verified_count": 0, "llm_latency": 0.0}):
            out = deep_research("БПЛА Москва", lang="ru", top_n=5)
        # Only one source survived dedup
        assert len(out["sources"]) == 1, f"expected 1 deduped source, got {len(out['sources'])}"
        s = out["sources"][0]
        # Both engines and both queries should be in the meta
        assert "bing" in s.get("found_by_engines", []), f"missing bing: {s.get('found_by_engines')}"
        assert "google" in s.get("found_by_engines", []), f"missing google: {s.get('found_by_engines')}"
        assert len(s.get("found_by_queries", [])) == 2, f"expected 2 queries, got {s.get('found_by_queries')}"
        # search_votes = engines + queries = 2 + 2 = 4
        assert s.get("search_votes") == 4, f"expected search_votes=4, got {s.get('search_votes')}"

    def test_top_url_is_canonical_not_raw(self):
        # Verify that the URL stored in sources is canonical, not raw-with-utm.
        fake = _fake_search_results(
            ("БПЛА", "bing", "https://Example.com/A?utm_source=x&utm_medium=email"),
        )
        with patch("hermes_deepresearch.web_search", side_effect=fake), \
             patch("hermes_deepresearch.fetch_url", return_value={"url": "https://Example.com/A?utm_source=x&utm_medium=email", "title": "t", "text": "x", "length": 1, "error": None}), \
             patch("hermes_deepresearch.verify_sources", return_value={"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0, "verification_details": [], "llm_enhanced": False, "llm_verified_count": 0, "llm_latency": 0.0}):
            out = deep_research("БПЛА", lang="ru", top_n=5)
        s = out["sources"][0]
        # URL should be canonical (no utm, lowercase host)
        assert s["url"] == "https://example.com/A", f"expected canonical URL, got {s['url']!r}"
        # Raw URL preserved separately
        assert "raw_urls" in s, f"raw_urls missing from source: {list(s.keys())}"
        assert any("utm_source=x" in r for r in s["raw_urls"]), f"raw_urls lost the utm: {s['raw_urls']}"

    def test_no_duplicate_sources_after_dedup(self):
        # Three different raw URLs that all canonicalize to the same thing.
        # Should result in 1 source, not 3.
        fake = _fake_search_results(
            ("q", "bing",   "https://example.com/a"),
            ("q", "google", "https://example.com/a?utm_source=bing"),
            ("q", "duckduckgo", "https://EXAMPLE.com/a"),
        )
        with patch("hermes_deepresearch.web_search", side_effect=fake), \
             patch("hermes_deepresearch.fetch_url", return_value={"url": "x", "title": "t", "text": "x", "length": 1, "error": None}), \
             patch("hermes_deepresearch.verify_sources", return_value={"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0, "verification_details": [], "llm_enhanced": False, "llm_verified_count": 0, "llm_latency": 0.0}):
            out = deep_research("q", lang="ru", top_n=5)
        assert len(out["sources"]) == 1, f"expected 1 deduped source from 3 raw URLs, got {len(out['sources'])}"
        s = out["sources"][0]
        assert s["url"] == "https://example.com/a"
        # 3 engines should be aggregated
        assert sorted(s.get("found_by_engines", [])) == ["bing", "duckduckgo", "google"]

    def test_search_votes_calculation(self):
        # 1 query, 1 engine, 1 URL: search_votes should be 2 (1 + 1).
        # This is the trivial baseline that the broken code "accidentally" gets right.
        fake = _fake_search_results(
            ("q", "bing", "https://example.com/a"),
        )
        with patch("hermes_deepresearch.web_search", side_effect=fake), \
             patch("hermes_deepresearch.fetch_url", return_value={"url": "x", "title": "t", "text": "x", "length": 1, "error": None}), \
             patch("hermes_deepresearch.verify_sources", return_value={"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0, "verification_details": [], "llm_enhanced": False, "llm_verified_count": 0, "llm_latency": 0.0}):
            out = deep_research("q", lang="ru", top_n=5)
        s = out["sources"][0]
        assert s.get("search_votes") == 2
```

### Edge case I considered but won't test

- What if `web_search` returns a URL whose `canonical_url(...)` is `""` (empty string)? The current code's `if u and u not in seen_urls` guards this, so empty canonical URLs are silently dropped. That's fine.
- What if `len(sources_meta) > top_n`? Current code takes `top_n` from `all_search` (which is unsorted by vote count, just by SearXNG order then by `_search_result_score`). Ranking-by-votes is Phase 5.

---

## Task 1: Create test_deepresearch_votes.py with 4 failing tests

**Objective:** Lock the broken aggregation in tests.

**Files:**
- Create: `tests/test_deepresearch_votes.py` (code above)

**Step 1: Write the test file**

```bash
cd /opt/searxng && cat > tests/test_deepresearch_votes.py <<'PYEOF'
[insert the 4 tests above]
PYEOF
```

**Step 2: Run, expect 3 red (1 trivial green baseline)**

```bash
cd /opt/searxng && python3 -m pytest tests/test_deepresearch_votes.py -v --no-header
```

Expected:
- `test_search_votes_calculation` — PASS (1 query, 1 engine = 2, broken code gives 2 too)
- `test_same_canonical_url_two_queries_aggregates` — FAIL (google hit skipped, search_votes=2 not 4)
- `test_top_url_is_canonical_not_raw` — FAIL (url has utm, raw_urls missing)
- `test_no_duplicate_sources_after_dedup` — FAIL (3 sources instead of 1, second/third hit skipped but seen_urls prevents re-adding)

Capture which fail; proceed.

---

## Task 2: Minimal patch — two targeted edits in hermes_deepresearch.py

**Objective:** Make the tests green with the smallest possible diff.

**Files:**
- Modify: `hermes_deepresearch.py` L812-826 (aggregation) and L833-841 (top_urls) and L850 (lookup)

### Edit A: Replace the aggregation block (L812-826)

Find:
```python
    for q in queries:
        qlang = "en" if q == reformulated else lang
        res = web_search(q, lang=qlang, time_range=effective_time_range, max_results=top_n * 2)
        for rank, r in enumerate(res):
            all_search.append({**r, "_query": q, "_lang": qlang, "_rank": rank})
            u = canonical_url(r.get("url", ""))
            if u and u not in seen_urls:
                seen_urls.add(u)
                sources_meta[u] = {
                    "engines": {r.get("engine", "")},
                    "queries": {q},
                    "ranks": [rank],
                    "snippets": [r.get("snippet", "")],
                    "titles": [r.get("title", "")],
                }
```

Replace with:
```python
    for q in queries:
        qlang = "en" if q == reformulated else lang
        res = web_search(q, lang=qlang, time_range=effective_time_range, max_results=top_n * 2)
        for rank, r in enumerate(res):
            all_search.append({**r, "_query": q, "_lang": qlang, "_rank": rank})
            raw_u = r.get("url", "")
            u = canonical_url(raw_u)
            if not u or u in seen_urls:
                # Even if we've seen this canonical, we may want to log the raw URL
                if u:
                    sources_meta.setdefault(u, {}).setdefault("raw_urls", set()).add(raw_u)
                continue
            seen_urls.add(u)
            sources_meta[u] = {
                "engines": {r.get("engine", "")},
                "queries": {q},
                "ranks": [rank],
                "snippets": [r.get("snippet", "")],
                "titles": [r.get("title", "")],
                "raw_urls": {raw_u},
            }
            # Aggregate hits for the same canonical across different (q, engine, rank) tuples
            # Note: only the FIRST occurrence of a (q, engine) pair adds; later ones
            # would be redundant. We just add raw_urls to preserve provenance.
```

Wait — that's getting complex and the comment is misleading. Let me reconsider.

Actually the right pattern is: **don't skip the second hit**, just merge its meta. The `seen_urls` guard exists to prevent the **same canonical URL** from being re-fetched — that's still needed for performance. But the meta can be merged separately.

Simpler version:

Replace with:
```python
    for q in queries:
        qlang = "en" if q == reformulated else lang
        res = web_search(q, lang=qlang, time_range=effective_time_range, max_results=top_n * 2)
        for rank, r in enumerate(res):
            all_search.append({**r, "_query": q, "_lang": qlang, "_rank": rank})
            raw_u = r.get("url", "")
            u = canonical_url(raw_u)
            if not u:
                continue
            # Always merge meta for this canonical URL, even if we've seen it.
            meta = sources_meta.setdefault(u, {
                "engines": set(),
                "queries": set(),
                "ranks": [],
                "snippets": [],
                "titles": [],
                "raw_urls": set(),
            })
            meta["engines"].add(r.get("engine", ""))
            meta["queries"].add(q)
            meta["ranks"].append(rank)
            meta["snippets"].append(r.get("snippet", ""))
            meta["titles"].append(r.get("title", ""))
            meta["raw_urls"].add(raw_u)
            # Track first-seen for top_urls ordering (FIFO across (query, engine))
            if u not in seen_urls:
                seen_urls.add(u)
```

This way:
- `sources_meta` always grows on every hit, keyed by canonical.
- `seen_urls` is just a flag for "first time we saw this canonical" — still used to preserve order for top_urls.
- The `if u not in seen_urls: seen_urls.add(u)` placement means we add to seen_urls on first encounter but still merge meta on every encounter.

### Edit B: Make `top_urls` use canonical (L833-841)

Find:
```python
    # Берём top_n URL
    top_urls = []
    seen = set()
    for r in all_search:
        u = r.get("url", "").split("#")[0]
        if u and u not in seen:
            seen.add(u)
            top_urls.append(u)
            if len(top_urls) >= top_n:
                break
```

Replace with:
```python
    # Берём top_n URL (canonical, чтобы матчить sources_meta keys)
    top_urls = []
    seen = set()
    for r in all_search:
        u = canonical_url(r.get("url", ""))
        if u and u not in seen:
            seen.add(u)
            top_urls.append(u)
            if len(top_urls) >= top_n:
                break
```

### Edit C: lookup (L850) — no change needed

The lookup `meta = sources_meta.get(u, {})` already works once Edit B is in place, because `u` is now the canonical URL that was used as the key in `sources_meta`.

### Edit D (optional, makes new field appear in output)

The test `test_top_url_is_canonical_not_raw` expects `raw_urls` to appear in `fr`. The new meta already has `raw_urls`, so when the fetch loop runs (L849-859), we should expose it on `fr` too:

Find (L849-859):
```python
            fr = fut.result() or {"url": u, "error": "fetch returned None"}
            meta = sources_meta.get(u, {})
            fr["found_by_engines"] = sorted(meta.get("engines", set()))
            fr["found_by_queries"] = sorted(meta.get("queries", set()))
            fr["search_votes"] = len(meta.get("engines", set())) + len(meta.get("queries", set()))
            fr["engine"] = (list(meta.get("engines", set())) or [""])[0]
            fr["search_snippet"] = (meta.get("snippets", [""]) or [""])[0]
            fr["title"] = fr.get("title") or (meta.get("titles", [""]) or [""])[0]
```

After the existing assignments, add:
```python
            fr["raw_urls"] = sorted(meta.get("raw_urls", set()))
```

### Step-by-step

1. Apply Edit A (aggregation rewrite)
2. Apply Edit B (top_urls canonical)
3. Apply Edit D (raw_urls propagation)
4. Run tests — expect all 4 green

```bash
cd /opt/searxng && python3 -m pytest tests/test_deepresearch_votes.py -v --no-header
```

Expected: 4 passed.

---

## Task 3: Run full test suite + ruff

**Objective:** No regression.

```bash
cd /opt/searxng && python3 -m pytest -q
cd /opt/searxng && python3 -m ruff check hermes_deepresearch.py tests/test_deepresearch_votes.py
```

Expected: 89 (Phase 1 baseline) + 4 (Phase 2 new) = 93 passed. Ruff clean.

---

## Files likely to change (summary)

| File | Tasks | Risk |
|---|---|---|
| `tests/test_deepresearch_votes.py` | T1 (new) | low — pure test |
| `hermes_deepresearch.py` | T2 (3 small edits) | medium — touches `deep_research` aggregation; verified by stub; no public API change |

**Files explicitly NOT changed in Phase 2:**
- `hermes_searxng.py` (Phase 6 territory)
- `llm_verifier.py` (Phase 4)
- `web_search()` itself
- `verify_sources()` (Phase 4)
- `_search_result_score` (Phase 5)

---

## Risks, tradeoffs, open questions

1. **`raw_urls` is a new field on `fr`.** Existing callers that iterate `fr.keys()` may break. Quick scan of `hermes_deepresearch.py` shows no such callers. `verify_sources` consumes specific keys (`url`, `title`, `text`, `length`, `error`, `snippet`, `engine`, `source_score`, `found_by_*`, `search_votes`), so adding `raw_urls` is additive. **Risk: low.**

2. **Performance.** Edit A makes `sources_meta.setdefault` run on every hit instead of being skipped via `if u in seen_urls`. For 2 queries × 10 results = 20 hits, the difference is negligible (a dict lookup vs. a set lookup). **No measurable cost.**

3. **`seen_urls` semantics changed.** Before: dedup helper for sources_meta. After: only used for top_urls ordering (FIFO). This is fine because `all_search` (L808) already carries every hit including duplicates — we still iterate them all when building top_urls.

4. **What if `web_search()` returns a result with `engine=""` (empty string)?** The current code adds `""` to `meta["engines"]`, so `found_by_engines` would contain `[""]`. That's a no-op signal (engine unknown). Not changing it in Phase 2 — out of scope. Tracked in Phase 5.

5. **`reformulated` is `None` for non-RU queries.** L813: `qlang = "en" if q == reformulated else lang`. If `lang="en"`, `reformulated=None` and `q` is never equal to `None`, so `qlang=lang` always. That's correct.

6. **Test isolation.** `tests/test_deepresearch_votes.py` patches `hermes_deepresearch.web_search`, `hermes_deepresearch.fetch_url`, and `hermes_deepresearch.verify_sources` at the import path. These are the names `deep_research` actually looks up at call time (they're module-level). Patching the module's own attributes is the standard pattern and matches how the existing 77 tests are written.

7. **`conftest.py` says `sys.path.insert(0, "/opt/searxng/src")`** but `/opt/searxng/src` doesn't exist. Tests still work because pytest's `testpaths` and the project's `pyproject.toml` already add the right path. **Not changing in Phase 2** (Phase 8, docs sync).

---

## Acceptance criteria (Phase 2 done = all of these)

- [x] `python3 -m pytest -q` shows ≥ 93 passed, 0 failed (89 prior + 4 new) — **VERIFIED 2026-06-06: 117 passed** (28 new across P2/P3/P4)
- [x] `python3 -m ruff check hermes_deepresearch.py tests/test_deepresearch_votes.py` clean — **VERIFIED 2026-06-06** (15 pre-existing ruff warnings в hermes_deepresearch.py, не от P2)
- [x] `deep_research()` with two different queries returning the same canonical URL: `search_votes >= 4`, `len(found_by_engines) >= 2`, `len(found_by_queries) == 2` — **VERIFIED 2026-06-05** by `TestPhase3_AC2_TestMultiQueryVotes::test_same_canonical_url_two_queries_aggregates`
- [x] `deep_research()` with three raw URLs canonicalizing to one: `len(sources) == 1`, `len(found_by_engines) == 3` — **VERIFIED 2026-06-05** by `TestPhase3_AC2_TestMultiQueryVotes::test_no_duplicate_sources_after_dedup`
- [x] Each `fr` has `raw_urls` populated (non-empty set, sorted list) — **VERIFIED 2026-06-05** by `TestPhase3_AC2_TestMultiQueryVotes::test_top_url_is_canonical_not_raw`

## What I will NOT do in Phase 2

- Won't touch `web_search()` or `hermes_searxng.py`
- Won't change `verify_sources()` or `llm_verifier.py`
- Won't add new ranking strategies (Phase 5)
- Won't add news routing (Phase 5)
- Won't add proxy integration (Phase 6)
- Won't change any compose / settings / docs (Phase 1, Phase 8)
