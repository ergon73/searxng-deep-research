"""
tests/test_deepresearch_votes.py — multi-query votes aggregation regression.

Locks in DR-05062026(3) §4 (multi-query votes broken) and §5 (canonical lookup).
"""
from __future__ import annotations

from unittest.mock import patch

from hermes_deepresearch import deep_research

# --- helpers ---

def _fake_search_results(*triples):
    """triples = (query, engine, url[, snippet]). Returns a side_effect callable
    that web_search() can dispatch on by query string."""
    table = {}
    for t in triples:
        q, eng, u = t[:3]
        snip = t[3] if len(t) > 3 else f"snippet for {u}"
        title = t[4] if len(t) > 4 else f"title {u}"
        table.setdefault(q, []).append({
            "url": u, "engine": eng, "title": title, "snippet": snip,
        })

    def fake(q, **kwargs):
        return table.get(q, [])

    return fake


def _fetch_side_effect(*args, **kwargs):
    """Mimic real fetch_url(): echoes back the URL it was called with, with
    the title/text we provide. Lets tests check that fr["url"] reflects
    whatever was passed to fetch_url()."""
    url = args[0] if args else kwargs.get("url", "")
    return {
        "url": url,
        "title": "fetched-title",
        "text": "fetched text body",
        "length": 100,
        "error": None,
    }

_VERIFY_EMPTY = {
    "verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
    "verification_details": [], "llm_enhanced": False,
    "llm_verified_count": 0, "llm_latency": 0.0,
}


def _patch_all(fake_search):
    """Returns a list of patches for web_search, fetch_url, verify_sources."""
    return [
        patch("hermes_deepresearch.web_search", side_effect=fake_search),
        patch("hermes_deepresearch.fetch_url", side_effect=_fetch_side_effect),
        patch("hermes_deepresearch.verify_sources", return_value=dict(_VERIFY_EMPTY)),
    ]


# --- tests ---

class TestMultiQueryVotes:
    def test_search_votes_baseline(self):
        """Trivial baseline: 1 query, 1 engine, 1 URL → search_votes == 2.
        The broken code accidentally gets this right; this test ensures the
        patch doesn't break the simple case."""
        fake = _fake_search_results(
            ("БПЛА", "bing", "https://example.com/a"),
        )
        patches = _patch_all(fake)
        for p in patches:
            p.start()
        try:
            out = deep_research("БПЛА", lang="ru", top_n=5)
        finally:
            for p in patches:
                p.stop()
        s = out["sources"][0]
        assert s.get("search_votes") == 2, (
            f"baseline: 1 engine + 1 query = 2, got {s.get('search_votes')}"
        )
        assert s.get("found_by_engines") == ["bing"]
        assert s.get("found_by_queries") == ["БПЛА"]

    def test_same_canonical_url_two_queries_aggregates(self):
        """Same article found via two different queries on two different engines.
        canonical_url() must dedup; meta must aggregate; search_votes == 4."""
        fake = _fake_search_results(
            ("Погода",   "bing",   "https://example.com/a?utm_source=x"),
            ("weather", "google", "https://example.com/a"),
        )
        patches = _patch_all(fake)
        for p in patches:
            p.start()
        try:
            out = deep_research("Погода", lang="ru", top_n=5)
        finally:
            for p in patches:
                p.stop()
        # Only one source survived canonical dedup
        assert len(out["sources"]) == 1, (
            f"expected 1 deduped source, got {len(out['sources'])}: "
            f"{[s.get('url') for s in out['sources']]}"
        )
        s = out["sources"][0]
        # Both engines aggregated
        engines = s.get("found_by_engines", [])
        assert "bing" in engines, f"missing bing: {engines}"
        assert "google" in engines, f"missing google: {engines}"
        # Both queries aggregated
        queries = s.get("found_by_queries", [])
        assert len(queries) == 2, f"expected 2 queries, got {queries}"
        # search_votes = 2 engines + 2 queries = 4
        assert s.get("search_votes") == 4, (
            f"expected search_votes=4, got {s.get('search_votes')} (engines={engines}, queries={queries})"
        )

    def test_top_url_is_canonical_not_raw(self):
        """The URL stored on the source should be canonical, and raw_urls
        should preserve the original URL with utm for provenance."""
        fake = _fake_search_results(
            ("БПЛА", "bing", "https://Example.com/A?utm_source=x&utm_medium=email"),
        )
        patches = _patch_all(fake)
        for p in patches:
            p.start()
        try:
            out = deep_research("БПЛА", lang="ru", top_n=5)
        finally:
            for p in patches:
                p.stop()
        s = out["sources"][0]
        # URL should be canonical (no utm, lowercase host; path stays as-is per
        # current canonical_url() behaviour, which lowercases only scheme/netloc)
        assert s["url"] == "https://example.com/A", (
            f"expected canonical URL https://example.com/A, got {s['url']!r}"
        )
        # Raw URL preserved separately
        assert "raw_urls" in s, f"raw_urls missing from source: {list(s.keys())}"
        assert any("utm_source=x" in r for r in s["raw_urls"]), (
            f"raw_urls lost the utm: {s['raw_urls']}"
        )

    def test_no_duplicate_sources_after_dedup(self):
        """Three different raw URLs that all canonicalize to the same thing.
        Should result in 1 source, not 3."""
        fake = _fake_search_results(
            ("БПЛА", "bing",         "https://example.com/a"),
            ("БПЛА", "google",       "https://example.com/a?utm_source=bing"),
            ("БПЛА", "duckduckgo",   "https://EXAMPLE.com/a"),
        )
        patches = _patch_all(fake)
        for p in patches:
            p.start()
        try:
            out = deep_research("БПЛА", lang="ru", top_n=5)
        finally:
            for p in patches:
                p.stop()
        assert len(out["sources"]) == 1, (
            f"expected 1 deduped source from 3 raw URLs, got {len(out['sources'])}: "
            f"{[s.get('url') for s in out['sources']]}"
        )
        s = out["sources"][0]
        assert s["url"] == "https://example.com/a", (
            f"canonical URL mismatch: {s['url']!r}"
        )
        # 3 engines aggregated
        engines = sorted(s.get("found_by_engines", []))
        assert engines == ["bing", "duckduckgo", "google"], (
            f"expected 3 engines aggregated, got {engines}"
        )
        # raw_urls should contain all 3 raw forms
        assert len(s.get("raw_urls", [])) == 3, (
            f"expected 3 raw_urls, got {s.get('raw_urls')}"
        )
