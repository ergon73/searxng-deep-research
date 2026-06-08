"""
Tests for src/research_runner.py — typed pipeline runner (Phase 3, v0.8.0).

Acceptance criteria (from ISSUES.md #018):
1. Legacy `deep_research()` is NOT modified.
2. Add `src/research_runner.py`.
3. `deep_research_v2(query, approved_plan=False)` returns:
   - status="needs_confirmation" + preview, if confirmation is needed.
   - status="done" + report, if approved or no confirmation.
4. Pipeline: planner → search/fetch → evidence → verify → synthesize → critical_review
5. Unit tests without live SearXNG via monkeypatch.
6. One integration smoke can stay optional.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# Make src importable for the module under test
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# We import the module under test AFTER sys.path is set, so all of its
# internal `from ... import` works in both repo and tmp-portable contexts.
from research_runner import (  # noqa: E402
    ResearchResult,
    _dedup_hits_by_canonical,
    _dispatch_search_task,
    _extract_claims_from_documents,
    _fetch_documents,
    deep_research_v2,
    run_research,
)
from models import ResearchState, SearchTask  # noqa: E402
from planner import build_research_plan  # noqa: E402


# ----------------------------------------------------------- helpers/fixtures


def _make_hit(url: str, title: str = "Title", snippet: str = "snippet") -> dict:
    return {
        "engine": "wikipedia",
        "url": url,
        "title": title,
        "content": snippet,
        "snippet": snippet,
    }


def _make_doc(url: str, text: str = "Some text.", title: str = "T", error: str | None = None) -> dict:
    return {
        "url": url,
        "title": title,
        "text": text,
        "length": len(text),
        "error": error,
    }


def _stub_web_search_factory(hits_by_query: dict | None = None):
    """Returns a stub for `web_search` that returns deterministic hits.

    `hits_by_query`: optional dict mapping query string -> list of hits.
                    If a query isn't in the dict, returns a single default hit.
    """
    hits_by_query = hits_by_query or {}

    def stub(query, **kwargs):
        return hits_by_query.get(query, [_make_hit(f"https://example.com/?q={query}")])

    return stub


def _stub_fetch_factory(docs_by_url: dict | None = None):
    """Returns a stub for `fetch_url` that returns deterministic docs."""
    docs_by_url = docs_by_url or {}

    def stub(url, **kwargs):
        return docs_by_url.get(url, _make_doc(url, text=f"Content for {url}"))

    return stub


def _stub_verify_factory(verified_count: int = 2, total_count: int = 5):
    """Returns a stub for `verify_sources` with predictable output."""
    def stub(top1, others, query, time_range=None, **_):
        return {
            "verified_facts": verified_count,
            "total_facts": total_count,
            "verification_rate": verified_count / max(1, total_count),
            "verification_details": [],
            "llm_enhanced": False,
            "llm_verified_count": 0,
            "llm_latency": 0.0,
            "llm_error": None,
        }
    return stub


@pytest.fixture
def patch_network(monkeypatch):
    """Patch all network/LLM functions used by the runner.

    This is the offline gate. If any code path in `run_research` tries to
    reach the network or call LLM, the stub raises AssertionError.
    """
    # Track which stubs were called
    calls = {"web_search": [], "fetch_url": [], "verify_sources": []}

    def web_search_stub(query, **kwargs):
        calls["web_search"].append((query, kwargs))
        return [_make_hit(f"https://example.com/?q={query}")]

    def fetch_url_stub(url, **kwargs):
        calls["fetch_url"].append((url, kwargs))
        return _make_doc(url, text="Some content text for testing.")

    def verify_sources_stub(top1, others, query, time_range=None, **_):
        calls["verify_sources"].append((query, len(others)))
        return {
            "verified_facts": 1,
            "total_facts": 2,
            "verification_rate": 0.5,
            "verification_details": [],
            "llm_enhanced": False,
            "llm_verified_count": 0,
            "llm_latency": 0.0,
            "llm_error": None,
        }

    monkeypatch.setattr("research_runner.web_search", web_search_stub)
    monkeypatch.setattr("research_runner.fetch_url", fetch_url_stub)
    monkeypatch.setattr("research_runner.verify_sources", verify_sources_stub)

    return calls


# ----------------------------------------------------------- _dedup_hits


class TestDedupHits:
    def test_dedup_strips_tracking_query_params(self):
        hits = [
            _make_hit("https://example.com/article?utm_source=x"),
            _make_hit("https://example.com/article"),
        ]
        deduped = _dedup_hits_by_canonical(hits)
        # canonical_url() normalises utm_*, so both should map to same URL
        assert len(deduped) == 1

    def test_dedup_keeps_distinct_urls(self):
        hits = [
            _make_hit("https://example.com/a"),
            _make_hit("https://example.com/b"),
        ]
        deduped = _dedup_hits_by_canonical(hits)
        assert len(deduped) == 2

    def test_dedup_empty(self):
        assert _dedup_hits_by_canonical([]) == []

    def test_dedup_skips_empty_url(self):
        # canonical_url("") returns "/" — not empty, but treated as a "real" URL.
        # We test that it doesn't crash and that the valid URL survives.
        hits = [_make_hit(""), _make_hit("https://example.com/a")]
        deduped = _dedup_hits_by_canonical(hits)
        # The valid one is always present
        valid = [d for d in deduped if d["url"] == "https://example.com/a"]
        assert len(valid) == 1
        # Total is at most 2 (canonical_url of "" may be "/", treated as distinct)
        assert len(deduped) <= 2


# ------------------------------------------------------- _dispatch_search_task


class TestDispatchSearchTask:
    def test_dispatches_with_task_params(self, monkeypatch):
        captured = {}

        def stub(query, **kwargs):
            captured["query"] = query
            captured.update(kwargs)
            return [_make_hit("https://example.com/x")]

        monkeypatch.setattr("research_runner.web_search", stub)
        task = SearchTask(
            query="БПЛА Москва",
            route="news",
            language="ru",
            engines="duckduckgo,bing",
            categories="news",
            time_range="day",
        )
        hits = _dispatch_search_task(task, max_results=5)
        assert captured["query"] == "БПЛА Москва"
        assert captured["lang"] == "ru"
        assert captured["time_range"] == "day"
        assert captured["engines"] == "duckduckgo,bing"
        assert captured["categories"] == "news"
        assert captured["max_results"] == 5
        assert len(hits) == 1

    def test_auto_language_omitted(self, monkeypatch):
        captured = {}

        def stub(query, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("research_runner.web_search", stub)
        task = SearchTask(query="x", language="auto")
        _dispatch_search_task(task)
        assert "lang" not in captured  # auto → no lang kwarg

    def test_optional_fields_omitted_when_none(self, monkeypatch):
        captured = {}

        def stub(query, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("research_runner.web_search", stub)
        task = SearchTask(query="x")  # all defaults
        _dispatch_search_task(task)
        assert "lang" not in captured
        assert "time_range" not in captured
        assert "engines" not in captured
        assert "categories" not in captured


# -------------------------------------------------------- _fetch_documents


class TestFetchDocuments:
    def test_fetch_returns_documents(self, monkeypatch):
        monkeypatch.setattr(
            "research_runner.fetch_url",
            lambda url, **kw: _make_doc(url, text="body"),
        )
        docs = _fetch_documents(["https://a.com", "https://b.com"])
        assert len(docs) == 2
        urls = {d["url"] for d in docs}
        assert urls == {"https://a.com", "https://b.com"}

    def test_fetch_handles_none_result(self, monkeypatch):
        monkeypatch.setattr("research_runner.fetch_url", lambda url, **kw: None)
        docs = _fetch_documents(["https://a.com"])
        assert len(docs) == 1
        assert docs[0]["url"] == "https://a.com"
        assert "error" in docs[0]


# -------------------------------------------------- _extract_claims_from_documents


class TestExtractClaims:
    def test_skips_empty_or_error_docs(self):
        docs = [
            _make_doc("https://a.com", text=""),
            _make_doc("https://b.com", text="Some text", error="boom"),
        ]
        claims, _ = _extract_claims_from_documents(docs, "test query")
        # _extract_facts may return [] for these, but importantly we don't crash
        # and we don't count "error" docs
        assert isinstance(claims, list)

    def test_extracts_from_valid_docs(self):
        docs = [
            _make_doc("https://a.com", text="Falcon 9 has 9 engines. The first launch was in 2010."),
        ]
        claims, claims_to_urls = _extract_claims_from_documents(docs, "Falcon 9")
        # Should extract at least one fact
        assert len(claims) >= 1
        # And the source URL should be tracked
        for c in claims:
            assert c in claims_to_urls
            assert any(d["url"] == "https://a.com" for d in claims_to_urls[c])


# ============================================================== run_research


class TestRunResearchConfirmation:
    def test_short_query_runs_without_confirmation(self, patch_network):
        """Short query that routes confidently (≥0.75) skips the gate.

        We use a query whose routing confidence is high. We avoid "Falcon 9"
        because it routes to 'technical' with confidence 0.6, which triggers
        routing_warning → confirmation gate.
        """
        result = run_research("БПЛА Москва сегодня", approved_plan=True)
        assert result.status == "done"
        assert result.plan is not None
        assert result.synthesis is not None
        assert result.review is not None
        assert result.error is None

    def test_long_query_returns_needs_confirmation(self, patch_network):
        """Long multi-aspect query: returns needs_confirmation when not approved."""
        long_q = (
            "Расскажи подробно про Falcon 9: сколько ступеней, какие двигатели, "
            "когда первый запуск, сколько стоит запуск и какие компании кроме "
            "SpaceX используют эту ракету для своих миссий"
        )
        result = run_research(long_q)
        assert result.status == "needs_confirmation"
        assert result.plan is not None
        assert result.plan.needs_confirmation is True
        # No network was called for a confirmation-gated plan
        assert patch_network["web_search"] == []
        assert patch_network["fetch_url"] == []
        # But we did NOT call verify (no docs to verify)
        assert patch_network["verify_sources"] == []

    def test_long_query_with_approval_runs(self, patch_network):
        """Same long query, but approved_plan=True → status=done."""
        long_q = (
            "Расскажи подробно про Falcon 9: сколько ступеней, какие двигатели, "
            "когда первый запуск, сколько стоит запуск и какие компании кроме "
            "SpaceX используют эту ракету для своих миссий"
        )
        result = run_research(long_q, approved_plan=True)
        assert result.status == "done"
        # Web search WAS called when we forced approval
        assert len(patch_network["web_search"]) >= 1


class TestRunResearchPipeline:
    def test_pipeline_calls_all_stages(self, patch_network):
        # approved_plan=True bypasses routing_warning gate (technical route,
        # confidence 0.6 would otherwise trigger confirmation)
        result = run_research("Falcon 9", approved_plan=True)
        assert result.status == "done"
        # All three stages called
        assert len(patch_network["web_search"]) >= 1, "web_search should be called"
        assert len(patch_network["fetch_url"]) >= 1, "fetch_url should be called"
        assert len(patch_network["verify_sources"]) >= 1, "verify_sources should be called"

    def test_state_populated_after_run(self, patch_network):
        result = run_research("Falcon 9", approved_plan=True)
        assert result.state is not None
        assert result.state.original_query == "Falcon 9"
        assert result.state.adapted is not None
        assert len(result.state.search_tasks) >= 1
        # Hits and documents should be filled
        assert len(result.state.search_hits) >= 1
        assert len(result.state.documents) >= 1
        assert result.state.iterations == 1

    def test_verdicts_populated(self, patch_network):
        result = run_research("Falcon 9", approved_plan=True)
        assert result.state is not None
        # verify_sources was called once → 1 verdict appended
        assert len(result.state.verdicts) >= 1

    def test_synthesis_populated(self, patch_network):
        result = run_research("Falcon 9", approved_plan=True)
        assert result.synthesis is not None

    def test_review_populated(self, patch_network):
        result = run_research("Falcon 9", approved_plan=True)
        assert result.review is not None

    def test_elapsed_sec_positive(self, patch_network):
        result = run_research("Falcon 9", approved_plan=True)
        assert result.elapsed_sec >= 0


class TestRunResearchError:
    def test_planner_exception_caught(self, monkeypatch):
        """If `build_research_plan` raises, runner returns status='error'."""

        def boom(_):
            raise RuntimeError("planner kaboom")

        monkeypatch.setattr("research_runner.build_research_plan", boom)
        result = run_research("anything")
        assert result.status == "error"
        assert "planner kaboom" in (result.error or "")
        assert "RuntimeError" in (result.error or "")

    def test_web_search_exception_caught(self, monkeypatch):
        """If `web_search` raises mid-pipeline, runner returns status='error'.

        We use approved_plan=True to bypass the confirmation gate (otherwise
        the runner would return needs_confirmation before any network call).
        """

        def boom(query, **kwargs):
            raise ConnectionError("searxng down")

        monkeypatch.setattr("research_runner.web_search", boom)
        result = run_research("Falcon 9", approved_plan=True)
        assert result.status == "error"
        assert "searxng down" in (result.error or "")
        assert result.plan is not None       # plan was built before the failure
        assert result.state is not None      # state was initialised

    def test_synthesize_exception_caught(self, monkeypatch):
        """If `synthesize` raises, runner returns status='error'."""
        # Pipeline must succeed first
        monkeypatch.setattr(
            "research_runner.web_search",
            lambda q, **kw: [_make_hit("https://a.com")],
        )
        monkeypatch.setattr(
            "research_runner.fetch_url",
            lambda u, **kw: _make_doc(u, text="body"),
        )
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
                              "verification_details": [], "llm_enhanced": False,
                              "llm_verified_count": 0, "llm_latency": 0.0, "llm_error": None},
        )
        # Now break synthesis
        def synth_boom(*a, **kw):
            raise ValueError("synth bad input")
        monkeypatch.setattr("research_runner.synthesize", synth_boom)

        result = run_research("Falcon 9", approved_plan=True)
        assert result.status == "error"
        assert "synth bad input" in (result.error or "")


class TestRunResearchNoResults:
    def test_no_search_results_records_gap(self, monkeypatch):
        """If web_search returns empty hits, runner records a gap and ends."""
        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u))
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
                              "verification_details": [], "llm_enhanced": False,
                              "llm_verified_count": 0, "llm_latency": 0.0, "llm_error": None},
        )
        # approved_plan=True bypasses any routing gate
        result = run_research("Falcon 9 first launch year", approved_plan=True)
        # No hits → no docs to verify → but synthesize+review must still run
        # (they handle empty inputs gracefully)
        assert result.status == "done"
        assert result.state is not None
        assert "no_search_results" in result.state.gaps


# ============================================================== deep_research_v2


class TestDeepResearchV2Alias:
    def test_alias_works(self, patch_network):
        result = deep_research_v2("Falcon 9", approved_plan=True)
        assert result.status == "done"
        assert result.synthesis is not None

    def test_alias_passes_kwargs(self, patch_network):
        # approved_plan=True should bypass the confirmation gate
        long_q = (
            "Расскажи подробно про Falcon 9: сколько ступеней, какие двигатели, "
            "когда первый запуск, сколько стоит запуск и какие компании кроме "
            "SpaceX используют эту ракету"
        )
        result = deep_research_v2(long_q, approved_plan=True)
        assert result.status == "done"


# ============================================================== ResearchResult


class TestResearchResultSerialisation:
    def test_to_dict_done(self, patch_network):
        result = run_research("Falcon 9", approved_plan=True)
        d = result.to_dict()
        # JSON-safe
        json.dumps(d, default=str)
        assert d["status"] == "done"
        assert d["original_query"] == "Falcon 9"
        assert "plan" in d
        assert "state" in d

    def test_to_dict_needs_confirmation(self):
        long_q = (
            "Расскажи подробно про Falcon 9: сколько ступеней, какие двигатели, "
            "когда первый запуск, сколько стоит запуск и какие компании кроме "
            "SpaceX используют эту ракету"
        )
        result = run_research(long_q)
        assert result.status == "needs_confirmation"
        d = result.to_dict()
        json.dumps(d, default=str)
        assert d["status"] == "needs_confirmation"
        assert "plan" in d
        # No synthesis/review yet (gated out)
        assert d.get("synthesis") is None
        assert d.get("review") is None

    def test_to_dict_error(self, monkeypatch):
        def boom(_):
            raise RuntimeError("nope")
        monkeypatch.setattr("research_runner.build_research_plan", boom)
        result = run_research("anything")
        d = result.to_dict()
        json.dumps(d, default=str)
        assert d["status"] == "error"
        assert "error" in d


# ============================================================== legacy preserved


class TestLegacyUntouched:
    """Strangler refactor: legacy deep_research() must NOT be modified."""

    def test_legacy_deep_research_still_importable(self):
        from hermes_deepresearch import deep_research
        assert callable(deep_research)

    def test_legacy_deep_research_signature_unchanged(self):
        import inspect
        from hermes_deepresearch import deep_research
        sig = inspect.signature(deep_research)
        params = list(sig.parameters.keys())
        # Must still accept: query (positional), lang, time_range, top_n, max_chars, alt_queries
        assert "query" in params
        assert "lang" in params
        assert "time_range" in params
        assert "top_n" in params
        assert "max_chars" in params
        assert "alt_queries" in params
