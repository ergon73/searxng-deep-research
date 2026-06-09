"""Tests for eval.py online fetch dedup logic (v0.8.1.4).

Regression test for canonical-dedup bug: previously, a single `seen` set
was populated with `seen.add(url)` BEFORE the canonical check, so any URL
where `canonical_url(url) == url` (the common case for URLs without utm_*)
was incorrectly skipped as a duplicate. The fetch loop never executed for
normal URLs.

Bug reported by external review 2026-06-09 (recommendation file
`hermes-recomendation-09062026(4).txt`, section 2).
"""

import sys
from pathlib import Path

import pytest

# Ensure src/ is on sys.path (conftest already does this, but be explicit
# for direct test execution: `pytest tests/test_eval_canonical_dedup.py`).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def _make_result():
    """Build a fresh QueryResult for testing."""
    from eval import QueryResult

    return QueryResult(
        query_id="t1",
        query="test",
        expected_route="general",
        category="test",
        main_query="",
        needs_confirmation=False,
        dropped_terms=[],
        route_predicted="general",
        route_match=True,
    )


def _make_hit(url: str) -> dict:
    return {"url": url, "title": f"title of {url}", "engine": "test"}


def _stub_web_search(search_results):
    """Returns a stub that mimics hermes_searxng.web_search."""

    def stub(query, **kwargs):  # noqa: ARG001
        return list(search_results)

    return stub


def _stub_fetch_url(calls_log):
    """Returns a stub fetch_url that logs calls and returns a doc."""

    def stub(url, **kwargs):  # noqa: ARG001
        calls_log.append(url)
        return {"text": f"text for {url}", "url": url}

    return stub


class TestCanonicalDedup:
    """Regression tests for the canonical-dedup bug fixed in v0.8.1.4."""

    def test_normal_url_without_tracking_is_fetched(self, monkeypatch):
        """The bug: a URL where canonical_url(url) == url was skipped as
        a duplicate. After the fix it should be fetched normally."""
        from eval import _run_online_pipeline

        result = _make_result()
        hits = [_make_hit("https://example.com/article")]
        calls_log: list[str] = []
        monkeypatch.setattr(
            "hermes_searxng.web_search", _stub_web_search(hits)
        )
        monkeypatch.setattr(
            "hermes_deepresearch.fetch_url", _stub_fetch_url(calls_log)
        )

        _run_online_pipeline(result)

        # The fix: this URL must be fetched, not skipped.
        assert calls_log == ["https://example.com/article"], (
            f"normal URL was not fetched (regression!): calls_log={calls_log}"
        )
        assert result.urls_total == 1
        assert result.urls_skipped_duplicate == 0
        assert result.fetch_errors == 0
        assert result.sources_fetched == 1

    def test_utm_duplicate_is_skipped(self, monkeypatch):
        """URL A and URL A?utm_source=x must be treated as one duplicate
        via canonical dedup."""
        from eval import _run_online_pipeline

        result = _make_result()
        hits = [
            _make_hit("https://example.com/article"),
            _make_hit("https://example.com/article?utm_source=x"),
        ]
        calls_log: list[str] = []
        monkeypatch.setattr(
            "hermes_searxng.web_search", _stub_web_search(hits)
        )
        monkeypatch.setattr(
            "hermes_deepresearch.fetch_url", _stub_fetch_url(calls_log)
        )

        _run_online_pipeline(result)

        assert calls_log == ["https://example.com/article"], (
            f"utm duplicate should not be fetched: calls_log={calls_log}"
        )
        assert result.urls_total == 2
        assert result.urls_skipped_duplicate == 1

    def test_distinct_urls_are_fetched(self, monkeypatch):
        """Two distinct canonical URLs should both be fetched."""
        from eval import _run_online_pipeline

        result = _make_result()
        hits = [
            _make_hit("https://example.com/a"),
            _make_hit("https://example.com/b"),
        ]
        calls_log: list[str] = []
        monkeypatch.setattr(
            "hermes_searxng.web_search", _stub_web_search(hits)
        )
        monkeypatch.setattr(
            "hermes_deepresearch.fetch_url", _stub_fetch_url(calls_log)
        )

        _run_online_pipeline(result)

        assert sorted(calls_log) == [
            "https://example.com/a",
            "https://example.com/b",
        ]
        assert result.urls_total == 2
        assert result.urls_skipped_duplicate == 0
        assert result.sources_fetched == 2

    def test_canonical_url_exception_increments_canonical_skip(self, monkeypatch):
        """If canonical_url() raises, urls_skipped_canonical must be
        incremented and the URL must NOT be fetched."""
        from eval import _run_online_pipeline

        result = _make_result()
        hits = [_make_hit("https://example.com/article")]

        def boom_canonical(url):
            raise ValueError("unparseable URL")

        calls_log: list[str] = []
        monkeypatch.setattr(
            "hermes_searxng.web_search", _stub_web_search(hits)
        )
        monkeypatch.setattr(
            "hermes_deepresearch.fetch_url", _stub_fetch_url(calls_log)
        )
        monkeypatch.setattr("hermes_deepresearch.canonical_url", boom_canonical)

        _run_online_pipeline(result)

        assert calls_log == [], "URL with bad canonical should not be fetched"
        assert result.urls_total == 1
        assert result.urls_skipped_canonical == 1
        assert result.urls_skipped_duplicate == 0

    def test_deny_pattern_increments_deny_skip(self, monkeypatch):
        """URLs matching _URL_DENY_PATTERNS should be skipped and counted."""
        from eval import _run_online_pipeline

        result = _make_result()
        hits = [
            _make_hit("https://vk.com/wall-12345"),  # deny pattern
            _make_hit("https://example.com/ok"),
        ]
        calls_log: list[str] = []
        monkeypatch.setattr(
            "hermes_searxng.web_search", _stub_web_search(hits)
        )
        monkeypatch.setattr(
            "hermes_deepresearch.fetch_url", _stub_fetch_url(calls_log)
        )

        _run_online_pipeline(result)

        assert calls_log == ["https://example.com/ok"]
        assert result.urls_total == 2
        assert result.urls_skipped_deny_pattern == 1
        assert result.sources_fetched == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
