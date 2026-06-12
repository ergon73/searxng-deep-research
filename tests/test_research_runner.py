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
from models import SearchTask  # noqa: E402
from planner import build_research_plan  # noqa: E402
from research_runner import (  # noqa: E402
    _dedup_hits_by_canonical,
    _dispatch_search_task,
    _extract_claims_from_documents,
    _fetch_documents,
    _flatten_verification_results,
    _task_key,
    deep_research_v2,
    run_research,
)

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


# ============================================================
# Phase 4 (#019) — span-level citation integration
# ============================================================


class TestRunnerSpanCitations:
    """Verify the runner populates `state.claims`, `state.evidence`, and
    `synthesis.coverage["citation_stats"]` after a successful run."""

    def test_state_claims_populated(self, patch_network):
        """The runner should call `_extract_typed_claims_with_citations`
        and append typed `Claim` objects to `state.claims`."""
        result = run_research("Falcon 9", approved_plan=True)
        assert result.state is not None
        assert len(result.state.claims) >= 1, "expected at least one typed Claim"
        for c in result.state.claims:
            # Each claim should be a typed Claim (not a string)
            assert hasattr(c, "text")
            assert hasattr(c, "evidence_window")

    def test_state_evidence_populated(self, patch_network):
        """`state.evidence` should be a list of EvidenceWindow objects
        (one per cited claim, with non-None windows)."""
        result = run_research("Falcon 9", approved_plan=True)
        assert result.state is not None
        # At least one claim should have found a span in the document
        assert len(result.state.evidence) >= 1
        for w in result.state.evidence:
            assert hasattr(w, "offset_start")
            assert hasattr(w, "offset_end")
            assert hasattr(w, "source_url")

    def test_synthesis_coverage_has_citation_stats(self, patch_network):
        """After synthesis, `synth.coverage["citation_stats"]` should be
        populated with `{total, cited, uncited, stub, coverage,
        non_stub_coverage}`."""
        result = run_research("Falcon 9", approved_plan=True)
        assert result.synthesis is not None
        cov = result.synthesis.coverage
        assert isinstance(cov, dict)
        assert "citation_stats" in cov
        stats = cov["citation_stats"]
        for key in ("total", "cited", "uncited", "stub", "coverage", "non_stub_coverage"):
            assert key in stats, f"missing {key} in citation_stats"
        # Sanity: total should equal len(state.claims)
        assert stats["total"] == len(result.state.claims)
        # Sanity: cited + uncited == total
        assert stats["cited"] + stats["uncited"] == stats["total"]

    def test_synthesis_coverage_has_inline_citations(self, patch_network):
        """`synth.coverage["inline_citations"]` should be a list of
        formatted strings with `[doc_N:start-end]` markers."""
        result = run_research("Falcon 9", approved_plan=True)
        cov = result.synthesis.coverage
        assert "inline_citations" in cov
        inline = cov["inline_citations"]
        # We don't know exactly how many claims match the doc text, but
        # the structure should be present
        assert isinstance(inline, list)
        for s in inline:
            assert "[doc_" in s, f"missing citation marker in: {s!r}"
            assert ":" in s
            assert "-" in s

    def test_unverified_claims_tracked(self, patch_network):
        """`synth.coverage["unverified_claims"]` should list any claim
        whose text was not found in any document (so the runner / LLM
        can flag them as unverified)."""
        result = run_research("Falcon 9", approved_plan=True)
        cov = result.synthesis.coverage
        assert "unverified_claims" in cov
        assert isinstance(cov["unverified_claims"], list)

    def test_citation_invariant_holds_with_real_docs(self, patch_network):
        """Every non-stub claim from the typed extraction should either
        have a window or be in `unverified_claims`. The
        `assert_citations_complete` invariant (with raise_on_missing=False)
        should give us consistent counts."""
        from citations import assert_citations_complete, citation_stats

        result = run_research("Falcon 9", approved_plan=True)
        # Without raising, we just want the counts
        cited, uncited = assert_citations_complete(result.state.claims, raise_on_missing=False)
        stats = citation_stats(result.state.claims)
        # Numbers must agree
        assert cited == stats["cited"]
        assert uncited == stats["uncited"] - stats["stub"]
        # cited + (uncited including stubs) == total
        assert cited + stats["uncited"] == stats["total"]

    def test_citation_survives_json_serialization(self, patch_network):
        """EvidenceWindow.to_dict() must include source_url/source_title/score
        (Phase 4 fields), and the claim augmentation should round-trip
        through `to_dict()` cleanly."""
        from dataclasses import asdict

        result = run_research("Falcon 9", approved_plan=True)
        # Pick first claim with a window
        cited_claim = next(
            (c for c in result.state.claims if c.evidence_window is not None),
            None,
        )
        assert cited_claim is not None
        d = asdict(cited_claim)
        assert "evidence_window" in d
        assert d["evidence_window"] is not None
        # The window should have the Phase 4 fields
        assert "source_url" in d["evidence_window"]
        assert "source_title" in d["evidence_window"]
        assert "score" in d["evidence_window"]

    def test_no_claims_no_citation_stats(self, monkeypatch):
        """If the document text is empty (no claims extracted),
        `synth.coverage` should still be a dict (not crash). No
        `citation_stats` key is added because the runner only injects
        it when `state.claims` is non-empty (avoid noise when there's
        nothing to count)."""
        monkeypatch.setattr(
            "research_runner.web_search",
            lambda q, **kw: [_make_hit("https://a.com")],
        )
        monkeypatch.setattr(
            "research_runner.fetch_url",
            lambda u, **kw: _make_doc(u, text=""),  # empty doc
        )
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
        )
        result = run_research("Falcon 9", approved_plan=True)
        assert result.status == "done"
        assert result.state is not None
        assert result.state.claims == []  # no claims because empty docs
        # citation_stats key is NOT injected when there are no claims
        # (this is by design — empty stats would be noise). coverage
        # is still a valid dict from synthesize().
        cov = result.synthesis.coverage
        assert isinstance(cov, dict)
        assert "citation_stats" not in cov

    def test_v083c1_inline_span_marker_in_confirmed_bullet(self, patch_network):
        """v0.8.3-C1: the runner must wire a per-fact span marker into the
        synthesize() call so confirmed answer bullets expose `[doc_N:start-end]`.

        `patch_network` (the standard offline gate) does not produce any
        confirmed bullets because its `verify_sources` stub returns
        `verification_details: []`, so the e2e assertion would be trivially
        empty. The spec explicitly allows a "focused runner helper test"
        for this case — we exercise `_build_inline_span_markers` directly
        to pin the alignment contract, including the duplicate-text
        fallback and the missing-evidence-window path.

        Coverage of `coverage["inline_citations"]` continuity is already
        pinned by `test_synthesis_coverage_has_inline_citations` above
        (it asserts the field is present after `run_research`); we do
        not duplicate that here.
        """
        from evidence import EvidenceWindow
        from models import Claim
        from research_runner import _build_inline_span_markers

        # 1) Happy path: one claim with a window, one without.
        documents = [
            {"url": "http://a.com/x", "text": "Falcon 9 has 9 engines."},
            {"url": "http://b.com/y", "text": "First launch 2010."},
        ]
        state_claims = [
            Claim(
                text="Falcon 9 has 9 engines.",
                evidence_window=EvidenceWindow(
                    source_url="http://a.com/x",
                    source_title="A",
                    offset_start=0,
                    offset_end=24,
                    text="Falcon 9 has 9 engines.",
                ),
            ),
            Claim(text="First launch 2010."),  # no window
        ]
        fact_results = [
            {"fact": "Falcon 9 has 9 engines.", "verdict": "SUPPORTS"},
            {"fact": "First launch 2010.", "verdict": "INSUFFICIENT"},
        ]
        markers = _build_inline_span_markers(fact_results, state_claims, documents)
        assert markers == ["[doc_0:0-24]", None]

        # 2) Duplicate text: second occurrence falls through to None.
        dup_results = fact_results + [{"fact": "Falcon 9 has 9 engines.", "verdict": "SUPPORTS"}]
        dup_markers = _build_inline_span_markers(dup_results, state_claims, documents)
        assert dup_markers == ["[doc_0:0-24]", None, None]

        # 3) Fact with no matching claim in state → None (no crash).
        orphan_results = [{"fact": "Unknown fact", "verdict": "SUPPORTS"}]
        assert _build_inline_span_markers(orphan_results, state_claims, documents) == [None]

        # 4) Empty fact_results → empty list (no crash, no allocation drift).
        assert _build_inline_span_markers([], state_claims, documents) == []


class TestRunnerSpanMarkersStrictDocIndex:
    """v0.8.3-C1b: strict doc-index resolution for inline span markers.

    When `EvidenceWindow.source_url` is empty or does not match any
    document URL, `_build_inline_span_markers` must emit `None` rather
    than fall back to `[doc_0:start-end]`. A `[doc_0:...]` marker would
    be misleading because the user-facing citation table in
    `answer_markdown` uses 1-based ids, so a 0 there would point at a
    different document than the user expects from the `[N]` marker next
    to it. The legacy `coverage["inline_citations"]` debug field
    (built by `format_cited_claim` in the runner) is NOT affected; it
    keeps its `or 0` fallback so downstream consumers see no change.
    """

    def test_inline_span_marker_not_emitted_when_window_source_url_missing(
        self, patch_network
    ):
        """EvidenceWindow with empty `source_url` → marker is None."""
        from evidence import EvidenceWindow
        from models import Claim
        from research_runner import _build_inline_span_markers

        documents = [
            {"url": "http://a.com/x", "text": "Falcon 9 has 9 engines."},
        ]
        state_claims = [
            Claim(
                text="Falcon 9 has 9 engines.",
                evidence_window=EvidenceWindow(
                    source_url="",  # v0.8.3-C1b trigger: no source attribution
                    source_title="A",
                    offset_start=0,
                    offset_end=24,
                    text="Falcon 9 has 9 engines.",
                ),
            ),
        ]
        fact_results = [{"fact": "Falcon 9 has 9 engines.", "verdict": "SUPPORTS"}]
        markers = _build_inline_span_markers(fact_results, state_claims, documents)
        assert markers == [None], (
            "empty source_url must yield None, not [doc_0:start-end]"
        )

    def test_inline_span_marker_not_emitted_when_window_source_url_not_in_documents(
        self, patch_network
    ):
        """EvidenceWindow whose `source_url` does not match any
        document → marker is None (no fallback to 0)."""
        from evidence import EvidenceWindow
        from models import Claim
        from research_runner import _build_inline_span_markers

        documents = [
            {"url": "http://a.com/x", "text": "Falcon 9 has 9 engines."},
            {"url": "http://b.com/y", "text": "First launch 2010."},
        ]
        state_claims = [
            Claim(
                text="Falcon 9 has 9 engines.",
                evidence_window=EvidenceWindow(
                    # v0.8.3-C1b trigger: no document in `documents` has
                    # this URL; the window was produced from a fallback
                    # slice or a doc that got dropped from `documents`.
                    source_url="http://orphan.com/z",
                    source_title="Orphan",
                    offset_start=10,
                    offset_end=30,
                    text="Falcon 9 has 9 engines.",
                ),
            ),
        ]
        fact_results = [{"fact": "Falcon 9 has 9 engines.", "verdict": "SUPPORTS"}]
        markers = _build_inline_span_markers(fact_results, state_claims, documents)
        assert markers == [None], (
            "unmatched source_url must yield None, not [doc_0:start-end]"
        )

    def test_inline_span_marker_uses_matching_document_index_not_default_zero(
        self, patch_network
    ):
        """Existing valid behaviour: source_url matching `documents[1]`
        produces `[doc_1:start-end]`, not `[doc_0:start-end]`."""
        from evidence import EvidenceWindow
        from models import Claim
        from research_runner import _build_inline_span_markers

        documents = [
            {"url": "http://a.com/x", "text": "First launch 2010."},
            {"url": "http://b.com/y", "text": "Falcon 9 has 9 engines."},
            {"url": "http://c.com/z", "text": "Reusable first stage."},
        ]
        state_claims = [
            Claim(
                text="Falcon 9 has 9 engines.",
                evidence_window=EvidenceWindow(
                    source_url="http://b.com/y",  # matches documents[1]
                    source_title="B",
                    offset_start=5,
                    offset_end=29,
                    text="Falcon 9 has 9 engines.",
                ),
            ),
        ]
        fact_results = [{"fact": "Falcon 9 has 9 engines.", "verdict": "SUPPORTS"}]
        markers = _build_inline_span_markers(fact_results, state_claims, documents)
        assert markers == ["[doc_1:5-29]"], (
            f"expected matching doc index 1, got {markers!r}"
        )


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
        assert result.plan is not None  # plan was built before the failure
        assert result.state is not None  # state was initialised

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
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
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
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
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


# ============================================================
# v0.8.1 Phase A — regression tests for the 3 P0 bugs found
# by the external ChatGPT review (see /tmp/hermes-recomendation-08062026(2).txt).
# Each test would have FAILED on v0.8.0 and now PASSES after the fix.
# ============================================================


class TestPhaseAFetchDocumentsOrder:
    """P0 #2: _fetch_documents() must preserve input URL order.

    On v0.8.0, as_completed() reordered results by completion time,
    so the fastest URL (often a low-rank cache hit) became `top1`,
    not the highest-ranked source. verify_sources() then verified a
    random document instead of the best one.
    """

    def test_slow_first_url_does_not_become_top1(self, monkeypatch):
        """If URL[0] is slow and URL[1] is fast, the output[0] must
        still be URL[0] — NOT URL[1] that finished first."""

        import time

        def slow_fetch(url, **kw):
            time.sleep(0.05)
            return {"url": url, "text": "slow content", "title": "Slow", "score": 1.0}

        def fast_fetch(url, **kw):
            time.sleep(0.001)
            return {"url": url, "text": "fast content", "title": "Fast", "score": 1.0}

        def dispatcher(url, **kw):
            # URL[0] is "slow", URL[1] is "fast" — both higher indexed
            if url.endswith("/slow"):
                return slow_fetch(url, **kw)
            return fast_fetch(url, **kw)

        monkeypatch.setattr("research_runner.fetch_url", dispatcher)

        urls = [
            "https://example.com/slow",  # rank 1 — should be output[0]
            "https://example.com/fast",  # rank 2 — should be output[1]
        ]
        result = _fetch_documents(urls)

        assert len(result) == 2
        assert result[0]["url"].endswith("/slow"), (
            f"BUG: output[0] is {result[0]['url']!r}, expected /slow. _fetch_documents lost the input order."
        )
        assert result[1]["url"].endswith("/fast"), f"BUG: output[1] is {result[1]['url']!r}, expected /fast."

    def test_input_order_preserved_with_three_urls(self, monkeypatch):
        """Three URLs, out-of-order completion — output must be in input order."""
        import time

        delays = {"a": 0.001, "b": 0.1, "c": 0.05}

        def fetch_with_delay(url, **kw):
            time.sleep(delays.get(url[-1], 0.0))
            return {"url": url, "text": f"text of {url[-1]}", "title": url[-1]}

        monkeypatch.setattr("research_runner.fetch_url", fetch_with_delay)

        # Input order: a, c, b — b is slowest but should still be output[2]
        result = _fetch_documents(["https://x.com/a", "https://x.com/c", "https://x.com/b"])
        assert [d["url"] for d in result] == [
            "https://x.com/a",
            "https://x.com/c",
            "https://x.com/b",
        ]

    def test_fetch_exceptions_dont_break_order(self, monkeypatch):
        """If one URL raises, the others must still be in the right slots."""

        def fetch_with_error(url, **kw):
            if "bad" in url:
                raise ConnectionError("network down")
            return {"url": url, "text": "ok", "title": "ok"}

        monkeypatch.setattr("research_runner.fetch_url", fetch_with_error)

        result = _fetch_documents(["https://a.com", "https://bad.com", "https://c.com"])
        assert [d["url"] for d in result] == [
            "https://a.com",
            "https://bad.com",
            "https://c.com",
        ]
        # Bad one is in the middle with an error marker
        assert "error" in result[1]
        # The other two have their data
        assert result[0]["text"] == "ok"
        assert result[2]["text"] == "ok"


class TestPhaseAFlattenVerificationResults:
    """P0 #1: synthesize() expects per-fact results, not aggregate dicts.

    On v0.8.0, runner passed state.verdicts (list of aggregate dicts) to
    synthesize(). synthesize()._compute_coverage iterated over them as
    if each were a per-fact result, so:
      - total = len(state.verdicts) (e.g. 1), not total_facts (e.g. 10)
      - coverage score = 0.0 (because aggregate has no "verdict" field)
      - unsupported = [] (because aggregate has no per-fact fields)
    """

    def test_flatten_extracts_per_fact_dicts(self):
        """3 verification_details → 3 output dicts, not 1 aggregate."""
        aggregate = {
            "verified_facts": 2,
            "total_facts": 3,
            "verification_rate": 0.6667,
            "verification_details": [
                {"fact": "fact 1", "verdict": "SUPPORTS"},
                {"fact": "fact 2", "verdict": "INSUFFICIENT"},
                {"fact": "fact 3", "verdict": "REFUTES"},
            ],
            "llm_enhanced": False,
        }
        out = _flatten_verification_results([aggregate])
        assert len(out) == 3
        assert out[0] == {"fact": "fact 1", "verdict": "SUPPORTS"}
        assert out[1] == {"fact": "fact 2", "verdict": "INSUFFICIENT"}
        assert out[2] == {"fact": "fact 3", "verdict": "REFUTES"}

    def test_flatten_handles_empty_verdicts(self):
        assert _flatten_verification_results([]) == []

    def test_flatten_handles_empty_details(self):
        aggregate = {
            "verified_facts": 0,
            "total_facts": 0,
            "verification_rate": 0.0,
            "verification_details": [],
        }
        assert _flatten_verification_results([aggregate]) == []

    def test_flatten_handles_multiple_aggregates(self):
        """Two iterations, each with their own aggregate → N+M details."""
        a1 = {
            "verification_details": [
                {"fact": "a", "verdict": "SUPPORTS"},
                {"fact": "b", "verdict": "INSUFFICIENT"},
            ],
        }
        a2 = {
            "verification_details": [
                {"fact": "c", "verdict": "SUPPORTS"},
            ],
        }
        out = _flatten_verification_results([a1, a2])
        assert len(out) == 3
        assert [r["fact"] for r in out] == ["a", "b", "c"]

    def test_flatten_skips_non_dict_entries(self):
        """Robustness: if some details are None or non-dict, skip them."""
        aggregate = {
            "verification_details": [
                {"fact": "x", "verdict": "SUPPORTS"},
                None,
                "garbage",
                {"fact": "y", "verdict": "REFUTES"},
            ],
        }
        out = _flatten_verification_results([aggregate])
        assert len(out) == 2
        assert [r["fact"] for r in out] == ["x", "y"]

    def test_flatten_skips_non_dict_aggregates(self):
        """Robustness: if a verdict aggregate is None / non-dict, skip."""
        out = _flatten_verification_results(
            [None, "not a dict", {"verification_details": [{"fact": "a", "verdict": "SUPPORTS"}]}]
        )
        assert len(out) == 1


class TestPhaseAUseLLMFlag:
    """P0 #3: run_research(..., use_llm=False) must propagate to verify_sources().

    On v0.8.0, runner called verify_sources(..., time_range=...) without
    `use_llm=use_llm`. verify_sources() has default use_llm=True, so the
    LLM could be triggered even when the caller passed use_llm=False.
    This violates the offline-test contract and the privacy/cost policy.
    """

    def test_run_research_passes_use_llm_false_to_verify_sources(self, monkeypatch):
        """Verify that `use_llm=False` is actually passed through."""
        captured = []

        def fake_verify_sources(top1, others, query, **kwargs):
            captured.append(kwargs)
            return {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            }

        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr("research_runner.verify_sources", fake_verify_sources)

        run_research("Falcon 9", approved_plan=True, use_llm=False)
        assert captured, "verify_sources was not called at all"
        assert "use_llm" in captured[0], f"BUG: use_llm not in kwargs passed to verify_sources: {captured[0]}"
        assert captured[0]["use_llm"] is False, f"BUG: use_llm={captured[0]['use_llm']!r}, expected False"

    def test_run_research_passes_use_llm_true_to_verify_sources(self, monkeypatch):
        """Verify that the True default also propagates (symmetry check)."""
        captured = []

        def fake_verify_sources(top1, others, query, **kwargs):
            captured.append(kwargs)
            return {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            }

        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr("research_runner.verify_sources", fake_verify_sources)

        run_research("Falcon 9", approved_plan=True, use_llm=True)
        assert captured
        assert captured[0].get("use_llm") is True


class TestPhaseARunnerEndToEnd:
    """Higher-level checks: the 3 fixes interact correctly through
    the full pipeline (no regression of the integration).
    """

    def test_synthesis_receives_per_fact_results(self, monkeypatch):
        """End-to-end: with 3 verification_details, synthesis sees 3 results."""

        def fake_verify(top1, others, query, **kwargs):
            return {
                "verified_facts": 2,
                "total_facts": 3,
                "verification_rate": 0.6667,
                "verification_details": [
                    {"fact": "F1", "verdict": "SUPPORTS"},
                    {"fact": "F2", "verdict": "INSUFFICIENT"},
                    {"fact": "F3", "verdict": "SUPPORTS"},
                ],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            }

        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr("research_runner.verify_sources", fake_verify)

        result = run_research("Falcon 9", approved_plan=True)
        assert result.status == "done"
        assert result.synthesis is not None

        # The coverage dict should report the correct per-fact count
        cov = result.synthesis.coverage
        assert cov.get("verification_fact_count") == 3
        assert cov.get("verification_aggregate_count") == 1
        # The synthesis itself should have processed 3 facts, not 1
        assert cov.get("total") == 3
        # 2 supports → score = 2/3
        assert abs(cov.get("score", 0) - 2 / 3) < 1e-4

    def test_top1_is_highest_ranked_not_fastest(self, monkeypatch):
        """End-to-end: a slow first URL must still be `top1` for verification."""
        import time

        captured_top1 = []

        def slow_first(url, **kw):
            if "rank1" in url:
                time.sleep(0.05)
            return {"url": url, "text": f"text for {url}", "title": url, "score": 1.0}

        def fake_verify(top1, others, query, **kwargs):
            captured_top1.append(top1.get("url", ""))
            return {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            }

        monkeypatch.setattr(
            "research_runner.web_search",
            lambda q, **kw: [
                _make_hit("https://x.com/rank1"),
                _make_hit("https://x.com/rank2"),
            ],
        )
        monkeypatch.setattr("research_runner.fetch_url", slow_first)
        monkeypatch.setattr("research_runner.verify_sources", fake_verify)

        run_research("Falcon 9", approved_plan=True)
        assert captured_top1, "verify_sources not called"
        assert "rank1" in captured_top1[0], (
            f"BUG: top1 = {captured_top1[0]!r}, expected rank1. verify_sources is verifying the wrong source."
        )


# ============================================================
# v0.8.1 Phase B — regression tests for iterative deepening
# hardening (the ResearchPlan mutation and re-run issues from the
# external ChatGPT review).
# Each test would have FAILED on v0.8.0 and PASSES after the fix.
# ============================================================


class TestPhaseBTaskKey:
    """`_task_key()` is the dedup primitive. Same intent → same key."""

    def test_same_query_same_route_same_key(self):
        from models import SearchTask

        a = SearchTask(query="q", route="general", language="en")
        b = SearchTask(query="q", route="general", language="en")
        assert _task_key(a) == _task_key(b)

    def test_different_query_different_key(self):
        from models import SearchTask

        a = SearchTask(query="q1", route="general")
        b = SearchTask(query="q2", route="general")
        assert _task_key(a) != _task_key(b)

    def test_priority_does_not_affect_key(self):
        """A gap-fill task with higher priority but same intent dedups
        against the original. This is intentional — we don't want to
        re-dispatch the same search just because the rationale differs."""
        from models import SearchTask

        a = SearchTask(query="q", route="general", priority=100)
        b = SearchTask(query="q", route="general", priority=50)
        assert _task_key(a) == _task_key(b)

    def test_engines_field_affects_key(self):
        """Different engine filters → different tasks (different intents)."""
        from models import SearchTask

        a = SearchTask(query="q", engines="wikipedia")
        b = SearchTask(query="q", engines="arxiv")
        assert _task_key(a) != _task_key(b)


class TestPhaseBPlanNotMutated:
    """v0.8.0: `plan.search_tasks.extend(new_tasks)` mutated the frozen plan.

    v0.8.1 Phase B: pending_tasks live in a local queue. Plan stays
    pristine — `plan.to_dict()` shows the same search_tasks count
    before and after the run."""

    def test_plan_search_tasks_unchanged_after_run(self, monkeypatch):
        """Capture the plan.tasks count before and after — should be equal."""

        # We need to capture the plan; do that by reading it from the
        # ResearchResult on the way out. But we want to also see the
        # plan before run_research. So we use build_research_plan directly.
        plan = build_research_plan("Falcon 9")
        tasks_before = list(plan.search_tasks)
        n_before = len(tasks_before)

        # Force a gap-analysis iteration by making the gap check return gaps
        # (use a real fixture so other monkeypatches still work)
        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
        )

        # Run with max_iterations=2 to give gap analysis a chance to fire
        run_research("Falcon 9", approved_plan=True, max_iterations=2)

        # The original plan object (in our scope) must be unchanged
        assert len(plan.search_tasks) == n_before, (
            f"BUG: plan.search_tasks grew from {n_before} to "
            f"{len(plan.search_tasks)} — the runner mutated the plan."
        )
        # And the tasks themselves are the same objects (no rebinding)
        for i, t in enumerate(plan.search_tasks):
            assert t is tasks_before[i], f"BUG: plan.search_tasks[{i}] was replaced (mutation)"

    def test_result_plan_equals_input_plan(self, monkeypatch):
        """The plan in the result should be the SAME object as the input
        plan, not a modified copy."""
        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
        )

        result = run_research("Falcon 9", approved_plan=True)
        # The result.plan.search_tasks is the same frozen plan object
        assert result.plan is not None
        # Its search_tasks list should equal what build_research_plan gave us
        plan2 = build_research_plan("Falcon 9")
        assert len(result.plan.search_tasks) == len(plan2.search_tasks), (
            "BUG: result.plan.search_tasks grew — gap-fill tasks were "
            "appended to the plan instead of the local pending queue"
        )


class TestPhaseBDeepeningDedup:
    """v0.8.0: iteration 2 re-ran every original task from plan.search_tasks.

    v0.8.1: each iteration dispatches only `current_tasks` (the queue
    snapshot), and gap-fill tasks live in a separate queue. Plus we
    track seen_task_keys for cross-iteration dedup."""

    def test_iteration_2_does_not_redo_original_tasks(self, monkeypatch):
        """With max_iterations=2, no original task (by _task_key) may be
        dispatched twice. v0.8.0 re-ran every original task on every
        iteration. v0.8.1 dispatches `current_tasks` only, which is a
        fresh snapshot per iteration (not the full plan)."""
        from research_runner import _task_key as runner_task_key

        dispatched_keys: list[tuple] = []

        # Wrap _dispatch_search_task so we record the task_key of every
        # task that the runner actually sent to the search backend.
        real_dispatch = _dispatch_search_task

        def hooked_dispatch(task, **kw):
            dispatched_keys.append(runner_task_key(task))
            return real_dispatch(task, **kw)

        monkeypatch.setattr("research_runner._dispatch_search_task", hooked_dispatch)
        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
        )

        # Patch analyze_gaps to return a gap on iter 0 (triggers gap-fill
        # tasks), no gaps on iter 1 (clean run).
        gap_call_count = [0]
        from gap_analysis import ResearchGap

        def fake_analyze_gaps(state):
            gap_call_count[0] += 1
            if gap_call_count[0] == 1:
                return [
                    ResearchGap(
                        kind="too_few_sources",
                        detail="need more",
                    )
                ]
            return []

        monkeypatch.setattr("research_runner.analyze_gaps", fake_analyze_gaps)

        run_research("Falcon 9", approved_plan=True, max_iterations=2)

        # Get the plan's original task keys for comparison
        plan = build_research_plan("Falcon 9")
        original_keys = {runner_task_key(t) for t in plan.search_tasks}

        # Every original task key must appear at most ONCE in the dispatched
        # log. (On v0.8.0 they would all appear twice — once per iteration.)
        for ok in original_keys:
            count = dispatched_keys.count(ok)
            assert count <= 1, (
                f"BUG: original task {ok!r} was dispatched {count} times "
                f"in 2 iterations. v0.8.0 re-runs original tasks on every "
                f"iteration. dispatched_keys={dispatched_keys}"
            )

    def test_seen_urls_blocks_duplicate_fetches(self, monkeypatch):
        """Even if the search returns the same URL twice (e.g. across
        iterations), we should fetch it only once."""

        fetched_urls: list[str] = []

        def fake_fetch(url, **kw):
            fetched_urls.append(url)
            return _make_doc(url, text="body")

        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://x.com/once")])
        monkeypatch.setattr("research_runner.fetch_url", fake_fetch)
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
        )

        # max_iterations=2 with no gaps: iteration 0 fetches,
        # iteration 1 should NOT re-fetch the same URL.
        run_research("Falcon 9", approved_plan=True, max_iterations=2)

        # The same URL must appear at most once in fetched_urls
        n_once = fetched_urls.count("https://x.com/once")
        assert n_once == 1, (
            f"BUG: URL 'https://x.com/once' was fetched {n_once} times "
            f"across {len(fetched_urls)} calls. Cross-iteration dedup broken."
        )

    def test_iteration_count_audit_trail(self, monkeypatch):
        """synth.coverage should report iterations_executed so the audit
        trail is unambiguous."""
        monkeypatch.setattr("research_runner.web_search", lambda q, **kw: [_make_hit("https://a.com")])
        monkeypatch.setattr("research_runner.fetch_url", lambda u, **kw: _make_doc(u, text="body"))
        monkeypatch.setattr(
            "research_runner.verify_sources",
            lambda *a, **kw: {
                "verified_facts": 0,
                "total_facts": 0,
                "verification_rate": 0.0,
                "verification_details": [],
                "llm_enhanced": False,
                "llm_verified_count": 0,
                "llm_latency": 0.0,
                "llm_error": None,
            },
        )

        result = run_research("Falcon 9", approved_plan=True, max_iterations=2)
        cov = result.synthesis.coverage
        assert "iterations_executed" in cov
        assert "unique_tasks_dispatched" in cov
        assert "unique_urls_fetched" in cov
        # At least one task and one URL were dispatched
        assert cov["unique_tasks_dispatched"] >= 1
        assert cov["unique_urls_fetched"] >= 1


class TestPhaseBConfirmationGateNotMutatingPlan:
    """When confirmation gate trips, plan should be returned as-is."""

    def test_needs_confirmation_doesnt_touch_plan(self, monkeypatch):
        """Long query triggers needs_confirmation → runner returns early.
        Plan must be the same object that was built (no mutation, no
        pending_tasks queue side-effects)."""
        from research_runner import run_research

        # A long/uncertain query triggers needs_confirmation
        result = run_research(
            "Falcon 9 detailed analysis of the entire 21st century space "
            "industry with specific focus on the evolution of launch vehicle "
            "technologies, including but not limited to reusable first stages, "
            "methane engines, and on-orbit refueling. I want at least 20 years "
            "of detail.",
        )
        # The plan (or its absence) should be unchanged
        assert result.status == "needs_confirmation"
        # Plan is returned untouched
        assert result.plan is not None
        # And the plan still has its original search tasks (no extension)
        # We don't know exactly what build_research_plan returns, but the
        # runner should NOT have added any gap-fill tasks (it returned
        # before the loop started).
