"""
Tests for src/gap_analysis.py — gap detection (Phase 5, v0.8.0).

Acceptance criteria (from ISSUES.md #020):
1. `analyze_gaps(state)` returns a list of `ResearchGap` for known issues.
2. `gaps_to_search_tasks(gaps, ...)` converts gaps to `SearchTask`s.
3. The runner loop calls these after each pass and adds tasks for the next pass.
4. Pure stdlib — no LLM, no network (verified by monkeypatch test).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make src importable
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from gap_analysis import (  # noqa: E402
    MAX_UNSUPPORTED_CLAIM_RATIO,
    MIN_DOCUMENTS,
    MIN_TOP1_CONFIDENCE,
    MIN_UNIQUE_DOMAINS,
    ResearchGap,
    _count_unique_domains,
    _domain_of,
    _has_contradiction,
    _top1_confidence,
    analyze_gaps,
    gaps_to_search_tasks,
)
from models import Claim, ResearchState  # noqa: E402


# ----------------------------------------------------------- helpers


def _doc(url: str, text: str = "Some text.", source_score: float = 0.8, error: str | None = None) -> dict:
    return {
        "url": url,
        "title": "T",
        "text": text,
        "length": len(text),
        "source_score": source_score,
        "error": error,
    }


def _verdict(verified: int = 0, total: int = 0, rate: float | None = None,
             details: list | None = None) -> dict:
    if rate is None:
        rate = verified / max(1, total)
    return {
        "verified_facts": verified,
        "total_facts": total,
        "verification_rate": rate,
        "verification_details": details or [],
        "llm_enhanced": False,
        "llm_verified_count": 0,
        "llm_latency": 0.0,
        "llm_error": None,
    }


# ============================================================== _domain_of


class TestDomainOf:
    def test_extracts_host(self):
        assert _domain_of("https://en.wikipedia.org/wiki/X") == "en.wikipedia.org"

    def test_strips_www(self):
        assert _domain_of("https://www.example.com/foo") == "example.com"

    def test_lowercases(self):
        assert _domain_of("https://EN.Wikipedia.org") == "en.wikipedia.org"

    def test_handles_empty(self):
        assert _domain_of("") == ""

    def test_handles_garbage(self):
        # Should not raise; returns "" or partial parse
        result = _domain_of("not a url")
        assert isinstance(result, str)


# ============================================================== _count_unique_domains


class TestCountUniqueDomains:
    def test_distinct_domains(self):
        docs = [
            _doc("https://en.wikipedia.org/a"),
            _doc("https://www.bbc.com/b"),
            _doc("https://example.com/c"),
        ]
        assert _count_unique_domains(docs) == 3

    def test_same_domain_www_stripped(self):
        docs = [
            _doc("https://www.example.com/a"),
            _doc("https://example.com/b"),
        ]
        assert _count_unique_domains(docs) == 1  # www. stripped

    def test_empty_list(self):
        assert _count_unique_domains([]) == 0

    def test_empty_urls_excluded(self):
        docs = [_doc(""), _doc("https://example.com/a")]
        assert _count_unique_domains(docs) == 1


# ============================================================== analyze_gaps


class TestAnalyzeGapsEmpty:
    def test_no_gaps_for_empty_state(self):
        state = ResearchState(original_query="q")
        gaps = analyze_gaps(state)
        # empty state → at least "too_few_sources" and "no_search_results"
        kinds = {g.kind for g in gaps}
        assert "too_few_sources" in kinds
        assert "no_search_results" in kinds

    def test_no_gaps_when_well_filled(self):
        """A state with 3+ docs from 2+ domains and good confidence = no gaps."""
        state = ResearchState(
            original_query="q",
            documents=[
                _doc("https://a.com/1", text="Falcon 9 has 9 engines.", source_score=0.8),
                _doc("https://b.com/1", text="First launch was 2010.", source_score=0.8),
                _doc("https://c.com/1", text="Built by SpaceX.", source_score=0.8),
            ],
            verdicts=[_verdict(verified=3, total=3)],
        )
        gaps = analyze_gaps(state)
        # Should be empty (or near-empty) for a well-filled state
        kinds = {g.kind for g in gaps}
        assert "too_few_sources" not in kinds
        assert "low_source_diversity" not in kinds
        assert "low_confidence" not in kinds


class TestAnalyzeGapsTooFewSources:
    def test_detects_too_few_sources(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1")],
            search_hits=[{"url": "https://a.com/1"}],
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "too_few_sources" for g in gaps)

    def test_below_threshold(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1"), _doc("https://b.com/1")],
        )
        gaps = analyze_gaps(state)
        # 2 < 3 (MIN_DOCUMENTS)
        assert any(g.kind == "too_few_sources" for g in gaps)

    def test_at_threshold_no_gap(self):
        state = ResearchState(
            original_query="q",
            documents=[
                _doc("https://a.com/1"),
                _doc("https://b.com/1"),
                _doc("https://c.com/1"),
            ],
        )
        gaps = analyze_gaps(state)
        assert not any(g.kind == "too_few_sources" for g in gaps)


class TestAnalyzeGapsLowDiversity:
    def test_detects_low_diversity(self):
        state = ResearchState(
            original_query="q",
            documents=[
                _doc("https://en.wikipedia.org/a"),
                _doc("https://en.wikipedia.org/b"),
            ],
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "low_source_diversity" for g in gaps)

    def test_www_treated_as_same_domain(self):
        state = ResearchState(
            original_query="q",
            documents=[
                _doc("https://www.example.com/a"),
                _doc("https://example.com/b"),
            ],
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "low_source_diversity" for g in gaps)


class TestAnalyzeGapsLowConfidence:
    def test_detects_low_confidence(self):
        state = ResearchState(
            original_query="q",
            documents=[
                _doc("https://a.com/1", source_score=0.2),  # below MIN_TOP1_CONFIDENCE=0.5
                _doc("https://b.com/1", source_score=0.3),
                _doc("https://c.com/1", source_score=0.4),
            ],
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "low_confidence" for g in gaps)

    def test_uses_legacy_confidence_key(self):
        """Older docs may have 'confidence' instead of 'source_score'."""
        docs = [
            {"url": "https://a.com/1", "text": "t", "confidence": 0.2},
            {"url": "https://b.com/1", "text": "t"},
            {"url": "https://c.com/1", "text": "t"},
        ]
        state = ResearchState(original_query="q", documents=docs)
        gaps = analyze_gaps(state)
        assert any(g.kind == "low_confidence" for g in gaps)

    def test_high_confidence_no_gap(self):
        state = ResearchState(
            original_query="q",
            documents=[
                _doc("https://a.com/1", source_score=0.9),
                _doc("https://b.com/1", source_score=0.8),
                _doc("https://c.com/1", source_score=0.7),
            ],
        )
        gaps = analyze_gaps(state)
        assert not any(g.kind == "low_confidence" for g in gaps)


class TestAnalyzeGapsContradictions:
    def test_detects_conflicting_method(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1")],
            verdicts=[_verdict(details=[{"method": "conflicting", "fact": "x"}])],
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "contradictions_unresolved" for g in gaps)

    def test_detects_very_low_verification_rate(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1")],
            verdicts=[_verdict(verified=1, total=100)],  # 1% rate
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "contradictions_unresolved" for g in gaps)

    def test_no_contradiction_for_high_rate(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1")],
            verdicts=[_verdict(verified=8, total=10)],  # 80% rate
        )
        gaps = analyze_gaps(state)
        assert not any(g.kind == "contradictions_unresolved" for g in gaps)


class TestAnalyzeGapsUnsupportedClaims:
    def test_detects_unsupported_claims(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1", text="Only this text is here.")],
            claims=[Claim(text="UNSUPPORTED_FACT_THAT_DOES_NOT_APPEAR_ANYWHERE")],
        )
        gaps = analyze_gaps(state)
        assert any(g.kind == "too_many_unsupported_claims" for g in gaps)

    def test_supported_claims_no_gap(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1", text="Falcon 9 first launch 2010.")],
            claims=[Claim(text="Falcon 9 first launch 2010")],
        )
        gaps = analyze_gaps(state)
        assert not any(g.kind == "too_many_unsupported_claims" for g in gaps)

    def test_no_claims_no_gap(self):
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1", text="Some text.")],
        )
        gaps = analyze_gaps(state)
        assert not any(g.kind == "too_many_unsupported_claims" for g in gaps)


class TestAnalyzeGapsDeterministic:
    def test_gaps_sorted_by_kind(self):
        """Returned gaps should be deterministically ordered."""
        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1", source_score=0.2)],
            verdicts=[_verdict(details=[{"method": "conflicting"}])],
            claims=[Claim(text="UNSUPPORTED_FACT_X_Y_Z")],
        )
        gaps = analyze_gaps(state)
        kinds = [g.kind for g in gaps]
        assert kinds == sorted(kinds), f"Gaps not sorted: {kinds}"


# ============================================================== gaps_to_search_tasks


class TestGapsToSearchTasks:
    def test_empty_gaps_empty_tasks(self):
        assert gaps_to_search_tasks([], original_query="q") == []

    def test_too_few_sources_creates_task(self):
        gaps = [ResearchGap(kind="too_few_sources", detail="x")]
        tasks = gaps_to_search_tasks(gaps, original_query="Falcon 9", route="general")
        assert len(tasks) == 1
        assert tasks[0].query == "Falcon 9"
        assert tasks[0].priority == 50
        assert "too_few_sources" in tasks[0].rationale

    def test_low_diversity_drops_engine_filter(self):
        gaps = [ResearchGap(kind="low_source_diversity", detail="x")]
        tasks = gaps_to_search_tasks(gaps, original_query="q", route="news")
        assert len(tasks) == 1
        assert tasks[0].engines is None  # explicitly cleared to broaden

    def test_contradictions_creates_review_query(self):
        gaps = [ResearchGap(kind="contradictions_unresolved", detail="x")]
        tasks = gaps_to_search_tasks(gaps, original_query="X", route="general")
        assert len(tasks) == 1
        assert "review" in tasks[0].query

    def test_low_confidence_seeks_authoritative(self):
        gaps = [ResearchGap(kind="low_confidence", detail="x")]
        tasks = gaps_to_search_tasks(gaps, original_query="X", route="general")
        assert "official" in tasks[0].query or "documentation" in tasks[0].query

    def test_no_search_results_not_retried(self):
        """`no_search_results` gap → NO task. More queries won't help."""
        gaps = [ResearchGap(kind="no_search_results", detail="x")]
        tasks = gaps_to_search_tasks(gaps, original_query="q", route="general")
        assert tasks == []

    def test_multiple_gaps_create_multiple_tasks(self):
        gaps = [
            ResearchGap(kind="too_few_sources", detail="x"),
            ResearchGap(kind="low_confidence", detail="y"),
        ]
        tasks = gaps_to_search_tasks(gaps, original_query="q", route="general")
        assert len(tasks) == 2

    def test_duplicate_kinds_dedup(self):
        """Same gap kind twice → only 1 task (avoid 2 retries of the same kind)."""
        gaps = [
            ResearchGap(kind="too_few_sources", detail="x"),
            ResearchGap(kind="too_few_sources", detail="x again"),
        ]
        tasks = gaps_to_search_tasks(gaps, original_query="q", route="general")
        assert len(tasks) == 1

    def test_route_and_language_propagated(self):
        gaps = [ResearchGap(kind="too_few_sources", detail="x")]
        tasks = gaps_to_search_tasks(
            gaps, original_query="q", route="news", language="ru"
        )
        assert tasks[0].route == "news"
        assert tasks[0].language == "ru"


# ============================================================== runner integration


class TestRunnerIntegration:
    """Test that the runner actually uses gap_analysis and does extra iterations."""

    def test_no_extra_iteration_when_no_gaps(self, monkeypatch):
        """If state is well-filled, runner should NOT add gap-fill tasks.
        Test by counting web_search calls."""
        from research_runner import run_research

        # 3 docs from 3 different domains, high confidence → no gaps
        call_log = []

        def fake_search(query, **kwargs):
            call_log.append(query)
            return [{"url": f"https://example{i}.com/x", "engine": "wikipedia",
                     "title": "T", "content": "t", "snippet": "s"}
                    for i in range(2)]

        def fake_fetch(url, **kwargs):
            return {"url": url, "text": "Falcon 9 first launch 2010",
                    "title": "T", "length": 30, "source_score": 0.9, "error": None}

        def fake_verify(top1, others, query, time_range=None, **_):
            return {"verified_facts": 2, "total_facts": 2, "verification_rate": 1.0,
                    "verification_details": [], "llm_enhanced": False,
                    "llm_verified_count": 0, "llm_latency": 0.0, "llm_error": None}

        monkeypatch.setattr("research_runner.web_search", fake_search)
        monkeypatch.setattr("research_runner.fetch_url", fake_fetch)
        monkeypatch.setattr("research_runner.verify_sources", fake_verify)

        result = run_research("Falcon 9", approved_plan=True, max_iterations=3)
        assert result.status == "done"
        # Planner already gave us 1+ tasks; with no gaps, no extra tasks added.
        # So we should see at most 1 call to fake_search per task per iteration.
        # Just assert no gap-fill "facts evidence" or "official" appeared:
        assert not any("facts evidence" in q for q in call_log)
        assert not any("official documentation" in q for q in call_log)

    def test_extra_iteration_when_gaps(self, monkeypatch):
        """If state is poorly filled (1 doc, low conf), gap-fill should trigger
        and the runner should do a 2nd pass with new tasks."""
        from research_runner import run_research

        call_log = []

        def fake_search(query, **kwargs):
            call_log.append(query)
            return [{"url": "https://example.com/only", "engine": "wikipedia",
                     "title": "T", "content": "t", "snippet": "s"}]

        def fake_fetch(url, **kwargs):
            return {"url": url, "text": "short", "title": "T", "length": 5,
                    "source_score": 0.2, "error": None}  # low conf

        def fake_verify(top1, others, query, time_range=None, **_):
            return {"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
                    "verification_details": [], "llm_enhanced": False,
                    "llm_verified_count": 0, "llm_latency": 0.0, "llm_error": None}

        monkeypatch.setattr("research_runner.web_search", fake_search)
        monkeypatch.setattr("research_runner.fetch_url", fake_fetch)
        monkeypatch.setattr("research_runner.verify_sources", fake_verify)

        result = run_research("Falcon 9", approved_plan=True, max_iterations=2)
        assert result.status == "done"
        assert result.state is not None
        # Iteration 2 was triggered → gap-fill queries like "official documentation"
        # or "facts evidence" should be in the call log
        gap_fills = [q for q in call_log
                     if "facts evidence" in q or "official" in q or "review" in q]
        assert len(gap_fills) >= 1, f"Expected gap-fill queries, got: {call_log}"
        assert result.state.iterations == 2  # ran 2 iterations

    def test_state_gaps_recorded(self, monkeypatch):
        """state.gaps should contain human-readable gap strings."""
        from research_runner import run_research

        def fake_search(query, **kwargs):
            return []

        def fake_fetch(url, **kwargs):
            return None

        def fake_verify(*a, **kw):
            return {"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
                    "verification_details": [], "llm_enhanced": False,
                    "llm_verified_count": 0, "llm_latency": 0.0, "llm_error": None}

        monkeypatch.setattr("research_runner.web_search", fake_search)
        monkeypatch.setattr("research_runner.fetch_url", fake_fetch)
        monkeypatch.setattr("research_runner.verify_sources", fake_verify)

        result = run_research("Falcon 9", approved_plan=True, max_iterations=1)
        assert result.state is not None
        # No hits → no_search_results gap recorded (by runner, not gap_analysis)
        assert any("no_search_results" in g for g in result.state.gaps)

    def test_max_iterations_cap_respected(self, monkeypatch):
        """Even with persistent gaps, runner stops at max_iterations."""
        from research_runner import run_research

        def fake_search(query, **kwargs):
            return [{"url": "https://example.com/x", "engine": "wikipedia",
                     "title": "T", "content": "t", "snippet": "s"}]

        def fake_fetch(url, **kwargs):
            return {"url": url, "text": "short", "title": "T", "length": 5,
                    "source_score": 0.1, "error": None}

        def fake_verify(*a, **kw):
            return {"verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
                    "verification_details": [], "llm_enhanced": False,
                    "llm_verified_count": 0, "llm_latency": 0.0, "llm_error": None}

        monkeypatch.setattr("research_runner.web_search", fake_search)
        monkeypatch.setattr("research_runner.fetch_url", fake_fetch)
        monkeypatch.setattr("research_runner.verify_sources", fake_verify)

        result = run_research("Falcon 9", approved_plan=True, max_iterations=3)
        assert result.state is not None
        # Despite persistent gaps, should stop at 3
        assert result.state.iterations == 3


# ============================================================== pure stdlib


class TestGapAnalysisNoNetwork:
    """gap_analysis must be pure stdlib — no network, no LLM."""

    def test_no_network_or_llm_calls(self, monkeypatch):
        import urllib.request, socket
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **kw: pytest.fail("urlopen called"))
        monkeypatch.setattr(socket, "socket",
                            lambda *a, **kw: pytest.fail("socket called"))

        state = ResearchState(
            original_query="q",
            documents=[_doc("https://a.com/1")],
        )
        # Must complete without raising
        gaps = analyze_gaps(state)
        assert isinstance(gaps, list)
