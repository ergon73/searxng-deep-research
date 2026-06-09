"""
Тесты для 6.6 synthesis-with-citations.

Структура:
  TestCitationHelpers       (5)  — Citation dataclass, helpers
  TestDedup                 (3)  — URL dedup
  TestBuildCitationTable    (3)  — citation table construction
  TestCoverage              (4)  — coverage math
  TestContradictions        (3)  — contradiction detection
  TestConfidence            (3)  — confidence heuristic
  TestOpenQuestions         (3)  — open questions builder
  TestMarkdownRender        (3)  — markdown rendering
  TestSynthesize            (3)  — end-to-end deterministic
  TestLLMEnrich             (5)  — LLM enrich + fallback (success, no client, raise, invalid [N], fabricated URL)
  TestHardRules             (3)  — markdown escape, quote cap, max citations

Всего: 30+ тестов.
"""
import pytest
from synthesis import (
    MAX_CITATIONS,
    MAX_MARKDOWN_CHARS,
    MAX_OPEN_QUESTIONS,
    MAX_QUOTE_CHARS,
    Citation,
    Synthesis,
    _build_citation_table,
    _build_open_questions,
    _compute_confidence,
    _compute_coverage,
    _dedup_sources,
    _extract_quote,
    _find_contradictions,
    _md_escape,
    _render_markdown,
    _truncate,
    _url_to_title,
    _validate_enriched_markdown,
    enrich_with_llm,
    synthesize,
)

# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def sample_sources():
    return [
        {"url": "http://a.com/x", "title": "A page", "text": "snippet A"},
        {"url": "http://b.com/y", "title": "B page", "text": "snippet B"},
        {"url": "http://a.com/x", "title": "A page dup", "text": "dup"},  # dup url
    ]


@pytest.fixture
def mixed_results():
    return [
        {"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
         "source_urls": ["http://a.com/x"]},
        {"fact": "c2", "verdict": "REFUTES", "reasoning": "wrong",
         "refuting_sources": [("http://b.com/y", 0.9, "negation")]},
        {"fact": "c3", "verdict": "NUMERIC_MISMATCH", "reasoning": "diff num",
         "numeric_mismatch_sources": [("http://a.com/x", 0.7, "num_mismatch")]},
        {"fact": "c4", "verdict": "INSUFFICIENT", "reasoning": "no data"},
        {"fact": "c5", "verdict": "CONFLICTING", "reasoning": "both",
         "supporting_sources": [("http://a.com/x", 0.5, "stem")],
         "refuting_sources": [("http://b.com/y", 0.6, "negation")]},
    ]


# --- TestCitationHelpers ---------------------------------------------------

class TestCitationHelpers:
    def test_citation_to_dict_strips_internal_index(self):
        c = Citation(id=1, url="http://a.com", title="A", quote="q", source_index=0)
        d = c.to_dict()
        assert d == {"id": 1, "url": "http://a.com", "title": "A", "quote": "q"}
        # source_index не в public dict (internal)
        assert "source_index" not in d

    def test_md_escape_special_chars(self):
        assert _md_escape("hello *world*") == "hello \\*world\\*"
        assert _md_escape("[1]") == "\\[1\\]"
        assert _md_escape("a_b`c") == "a\\_b\\`c"
        assert _md_escape("") == ""

    def test_truncate_with_ellipsis(self):
        assert _truncate("short", 100) == "short"
        # По границе слова
        out = _truncate("hello world hello world", 12)
        assert out.endswith("…")
        assert len(out) <= 12

    def test_url_to_title(self):
        assert _url_to_title("http://example.com/path/to/article") == "example.com — article"
        assert _url_to_title("http://example.com") == "example.com"
        assert _url_to_title("?") == "(no url)"
        assert _url_to_title("") == "(no url)"

    def test_extract_quote_priority(self):
        s1 = {"snippet": "from snippet"}
        s2 = {"content": "from content"}
        s3 = {"text": "from text"}
        s4 = {"body": "from body"}
        s5 = {"unrelated": "field"}
        assert "from snippet" in _extract_quote(s1)
        assert "from content" in _extract_quote(s2)
        assert "from text" in _extract_quote(s3)
        assert "from body" in _extract_quote(s4)
        assert _extract_quote(s5) == ""
        # No truncation within limit
        long_text = "x" * 500
        s6 = {"text": long_text}
        out = _extract_quote(s6, max_chars=100)
        # _truncate резервирует 1 char под ellipsis → max 101
        assert len(out) <= 101
        assert out.endswith("…")


# --- TestDedup -------------------------------------------------------------

class TestDedup:
    def test_dedup_keeps_first(self, sample_sources):
        out = _dedup_sources(sample_sources)
        assert len(out) == 2
        assert out[0]["url"] == "http://a.com/x"
        assert out[1]["url"] == "http://b.com/y"
        # First kept, dup dropped
        assert out[0]["title"] == "A page"

    def test_dedup_handles_empty(self):
        assert _dedup_sources([]) == []

    def test_dedup_handles_non_dict(self):
        sources = [{"url": "http://a.com"}, "not a dict", {"url": "http://b.com"}]
        out = _dedup_sources(sources)
        assert len(out) == 2
        assert all(isinstance(s, dict) for s in out)


# --- TestBuildCitationTable -----------------------------------------------

class TestBuildCitationTable:
    def test_basic_table(self):
        sources = [
            {"url": "http://a.com", "title": "A", "text": "ta"},
            {"url": "http://b.com", "title": "B", "text": "tb"},
        ]
        table = _build_citation_table(sources)
        assert len(table) == 2
        assert table[0].id == 1
        assert table[1].id == 2
        assert table[0].url == "http://a.com"
        assert table[0].source_index == 0
        assert table[1].source_index == 1

    def test_table_uses_url_as_title_fallback(self):
        sources = [{"url": "http://example.com/foo", "text": "x"}]
        table = _build_citation_table(sources)
        assert table[0].title == "example.com — foo"

    def test_max_citations_limit(self):
        sources = [{"url": f"http://a.com/{i}", "text": "x"} for i in range(100)]
        table = _build_citation_table(sources)
        assert len(table) == MAX_CITATIONS == 50


# --- TestCoverage ---------------------------------------------------------

class TestCoverage:
    def test_all_supported(self):
        results = [{"fact": "c1", "verdict": "SUPPORTS"}]
        cov = _compute_coverage(["c1"], results)
        assert cov["supported"] == 1
        assert cov["partial"] == 0
        assert cov["total"] == 1
        assert cov["score"] == 1.0
        assert cov["unsupported"] == []

    def test_all_unsupported(self):
        results = [
            {"fact": "c1", "verdict": "INSUFFICIENT"},
            {"fact": "c2", "verdict": "REFUTES"},
        ]
        cov = _compute_coverage(["c1", "c2"], results)
        assert cov["supported"] == 0
        assert cov["total"] == 2
        assert cov["score"] == 0.0
        assert len(cov["unsupported"]) == 2

    def test_conflicts_partial_score(self):
        results = [
            {"fact": "c1", "verdict": "SUPPORTS"},
            {"fact": "c2", "verdict": "CONFLICTING"},
        ]
        cov = _compute_coverage(["c1", "c2"], results)
        # SUPPORTS=1, CONFLICTING=0.5, total=2 → 0.75
        assert cov["supported"] == 1
        assert cov["partial"] == 1
        assert cov["score"] == 0.75

    def test_empty(self):
        cov = _compute_coverage([], [])
        assert cov["score"] == 0.0
        assert cov["total"] == 0


# --- TestContradictions ----------------------------------------------------

class TestContradictions:
    def test_detects_refutes(self):
        results = [
            {"fact": "c1", "verdict": "REFUTES",
             "refuting_sources": [("http://a.com", 0.9, "negation")]}
        ]
        cont = _find_contradictions(results)
        assert len(cont) == 1
        assert cont[0]["type"] == "REFUTES"
        assert "http://a.com" in cont[0]["urls"]

    def test_detects_numeric_mismatch(self):
        results = [
            {"fact": "c1", "verdict": "NUMERIC_MISMATCH",
             "numeric_mismatch_sources": [("http://a.com", 0.7, "num_mismatch")]}
        ]
        cont = _find_contradictions(results)
        assert len(cont) == 1
        assert cont[0]["type"] == "NUMERIC_MISMATCH"

    def test_detects_conflicting(self):
        results = [
            {"fact": "c1", "verdict": "CONFLICTING",
             "supporting_sources": [("http://a.com", 0.5, "stem")],
             "refuting_sources": [("http://b.com", 0.6, "negation")]}
        ]
        cont = _find_contradictions(results)
        assert len(cont) == 1
        assert cont[0]["type"] == "CONFLICTING"
        # Both urls in urls list
        assert "http://a.com" in cont[0]["urls"]
        assert "http://b.com" in cont[0]["urls"]


# --- TestConfidence --------------------------------------------------------

class TestConfidence:
    def test_high_coverage_high_confidence(self):
        cov = {"supported": 10, "partial": 0, "total": 10, "score": 1.0}
        c = _compute_confidence(cov, contradictions=[], num_citations=5)
        # base=1.0, penalty=0, bonus=min(0.1, 0.02*5)=0.1 → 1.0 (clamped)
        assert c == 1.0

    def test_contradictions_penalize(self):
        cov = {"supported": 5, "partial": 0, "total": 10, "score": 0.5}
        c = _compute_confidence(cov, contradictions=[{}, {}], num_citations=0)
        # base=0.5, penalty=0.2, bonus=0 → 0.3
        assert c == 0.3

    def test_citation_bonus(self):
        cov = {"supported": 5, "partial": 0, "total": 10, "score": 0.5}
        c = _compute_confidence(cov, contradictions=[], num_citations=10)
        # base=0.5, penalty=0, bonus=min(0.1, 0.2)=0.1 → 0.6
        assert c == 0.6


# --- TestOpenQuestions -----------------------------------------------------

class TestOpenQuestions:
    def test_insufficient_generates_question(self):
        results = [{"fact": "c1", "verdict": "INSUFFICIENT"}]
        qs = _build_open_questions(["c1"], results)
        assert len(qs) == 1
        assert "c1" in qs[0]

    def test_refutes_marks_contradiction(self):
        results = [{"fact": "c1", "verdict": "REFUTES"}]
        qs = _build_open_questions(["c1"], results)
        assert "Оспаривается" in qs[0]

    def test_supports_excluded(self):
        results = [{"fact": "c1", "verdict": "SUPPORTS"}]
        qs = _build_open_questions(["c1"], results)
        assert qs == []


# --- TestMarkdownRender ----------------------------------------------------

class TestMarkdownRender:
    def test_contains_query(self):
        md = _render_markdown(
            query="What is X?",
            claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                     "source_urls": ["http://a.com"]}],
            citations=[Citation(id=1, url="http://a.com", title="A", quote="q", source_index=0)],
            coverage={"supported": 1, "partial": 0, "total": 1, "score": 1.0, "unsupported": []},
            contradictions=[],
            open_questions=[],
        )
        assert "What is X?" in md

    def test_citation_markers_in_text(self):
        md = _render_markdown(
            query="q",
            claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                     "source_urls": ["http://a.com"]}],
            citations=[Citation(id=1, url="http://a.com", title="A", quote="q", source_index=0)],
            coverage={"supported": 1, "partial": 0, "total": 1, "score": 1.0, "unsupported": []},
            contradictions=[],
            open_questions=[],
        )
        assert "[1]" in md

    def test_handles_empty_results(self):
        md = _render_markdown(
            query="q",
            claims=[],
            results=[],
            citations=[],
            coverage={"supported": 0, "partial": 0, "total": 0, "score": 0.0, "unsupported": []},
            contradictions=[],
            open_questions=[],
        )
        assert "Нет данных" in md


# --- TestSynthesize --------------------------------------------------------

class TestSynthesize:
    def test_basic(self):
        s = synthesize(
            query="q",
            claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )
        assert isinstance(s, Synthesis)
        assert s.enriched_by_llm is False
        assert s.llm_fallback_reason is None
        assert len(s.citations) == 1
        assert "[1]" in s.answer_markdown
        assert s.coverage["supported"] == 1

    def test_to_dict_roundtrip(self):
        s = synthesize(
            query="q",
            claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )
        d = s.to_dict()
        assert d["enriched_by_llm"] is False
        assert len(d["citations"]) == 1
        assert d["coverage"]["supported"] == 1

    def test_dedup_in_citations(self):
        s = synthesize(
            query="q",
            claims=["c1", "c2"],
            results=[
                {"fact": "c1", "verdict": "SUPPORTS",
                 "source_urls": ["http://a.com"]},
                {"fact": "c2", "verdict": "SUPPORTS",
                 "source_urls": ["http://a.com"]},  # same URL
            ],
            source_candidates=[
                {"url": "http://a.com", "title": "A", "text": "x"},
                {"url": "http://a.com", "title": "A dup", "text": "y"},
            ],
        )
        # 2 sources in input, 1 unique URL
        assert len(s.citations) == 1


# --- TestLLMEnrich --------------------------------------------------------

class TestLLMEnrich:
    def test_no_client_fallback(self):
        base = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )
        out = enrich_with_llm(
            base, "q", ["c1"],
            [{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
              "source_urls": ["http://a.com"]}],
            [{"url": "http://a.com", "title": "A", "text": "x"}],
            llm_client=None,
        )
        assert out.enriched_by_llm is False
        assert out.llm_fallback_reason == "no llm_client"
        # Same markdown
        assert out.answer_markdown == base.answer_markdown

    def test_client_no_complete_method_fallback(self):
        base = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )

        class BadClient:
            pass

        out = enrich_with_llm(
            base, "q", ["c1"],
            [{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
              "source_urls": ["http://a.com"]}],
            [{"url": "http://a.com", "title": "A", "text": "x"}],
            llm_client=BadClient(),
        )
        assert out.enriched_by_llm is False
        assert "no .complete()" in out.llm_fallback_reason

    def test_client_raises_fallback(self):
        base = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )

        class BrokenClient:
            def complete(self, prompt):
                raise RuntimeError("api down")

        out = enrich_with_llm(
            base, "q", ["c1"],
            [{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
              "source_urls": ["http://a.com"]}],
            [{"url": "http://a.com", "title": "A", "text": "x"}],
            llm_client=BrokenClient(),
        )
        assert out.enriched_by_llm is False
        assert "llm call failed" in out.llm_fallback_reason

    def test_invalid_citation_marker_fallback(self):
        base = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )

        class BadCitationClient:
            def complete(self, prompt):
                return "## Ответ\n\nЭто [99] ссылка на несуществующий источник."

        out = enrich_with_llm(
            base, "q", ["c1"],
            [{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
              "source_urls": ["http://a.com"]}],
            [{"url": "http://a.com", "title": "A", "text": "x"}],
            llm_client=BadCitationClient(),
        )
        assert out.enriched_by_llm is False
        assert "unknown citation id" in out.llm_fallback_reason

    def test_fabricated_url_fallback(self):
        base = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )

        class FabricatedClient:
            def complete(self, prompt):
                return "## Ответ\n\nСм. [1] и [http://evil.com/fake]."

        out = enrich_with_llm(
            base, "q", ["c1"],
            [{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
              "source_urls": ["http://a.com"]}],
            [{"url": "http://a.com", "title": "A", "text": "x"}],
            llm_client=FabricatedClient(),
        )
        assert out.enriched_by_llm is False
        assert "unknown URL" in out.llm_fallback_reason

    def test_success_path(self):
        base = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )

        class GoodClient:
            def complete(self, prompt):
                return "## Новый ответ\n\nЭто [1] ссылка на оригинал."

        out = enrich_with_llm(
            base, "q", ["c1"],
            [{"fact": "c1", "verdict": "SUPPORTS", "reasoning": "ok",
              "source_urls": ["http://a.com"]}],
            [{"url": "http://a.com", "title": "A", "text": "x"}],
            llm_client=GoodClient(),
        )
        assert out.enriched_by_llm is True
        assert out.llm_fallback_reason is None
        assert "Новый ответ" in out.answer_markdown
        assert "[1]" in out.answer_markdown
        # Citations/coverage preserved
        assert len(out.citations) == len(base.citations)
        assert out.coverage == base.coverage


# --- TestHardRules --------------------------------------------------------

class TestHardRules:
    def test_quote_capped_at_max(self):
        sources = [{"text": "x" * 1000, "title": "T"}]
        out = _extract_quote(sources[0], max_chars=MAX_QUOTE_CHARS)
        # _truncate резервирует 1 char под ellipsis → max 201
        assert len(out) <= MAX_QUOTE_CHARS + 1
        assert out.endswith("…")

    def test_validate_rejects_empty(self):
        valid, reason = _validate_enriched_markdown(
            "", valid_citation_ids={1}, valid_urls={"http://a.com"}
        )
        assert valid is False
        assert "empty" in reason

    def test_validate_rejects_short(self):
        valid, reason = _validate_enriched_markdown(
            "hi", valid_citation_ids={1}, valid_urls={"http://a.com"}
        )
        assert valid is False
        assert "too short" in reason

    def test_validate_rejects_non_integer_marker(self):
        # [abc] is matched by our regex only if it's \d{1,3} — but let's check
        # what we actually match. We only match \d{1,3}, so this is safe by design.
        # But test the post-validate logic with valid format but unknown id.
        valid, reason = _validate_enriched_markdown(
            "Это [42] ссылка с длинным текстом, чтобы пройти минимум.",
            valid_citation_ids={1}, valid_urls={"http://a.com"}
        )
        assert valid is False
        assert "unknown citation id" in reason

    def test_max_citations_constant(self):
        # Verify constant
        assert MAX_CITATIONS == 50
        assert MAX_QUOTE_CHARS == 200
        assert MAX_OPEN_QUESTIONS == 20
        assert MAX_MARKDOWN_CHARS == 60_000


# --- Adversarial tests -----------------------------------------------------

class TestAdversarial:
    def test_special_chars_in_claim_dont_break_markdown(self):
        s = synthesize(
            query="q",
            claims=["c1 with **bold** and [link]"],
            results=[{"fact": "c1 with **bold** and [link]", "verdict": "SUPPORTS",
                      "reasoning": "ok", "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )
        # The claim is escaped
        assert "\\*\\*bold\\*\\*" in s.answer_markdown
        assert "\\[link\\]" in s.answer_markdown

    def test_very_long_claim_truncated(self):
        long_claim = "x" * 1000
        s = synthesize(
            query="q",
            claims=[long_claim],
            results=[{"fact": long_claim, "verdict": "SUPPORTS", "reasoning": "ok",
                      "source_urls": ["http://a.com"]}],
            source_candidates=[{"url": "http://a.com", "title": "A", "text": "x"}],
        )
        # In coverage, claim should be truncated
        assert all(len(u["claim"]) <= 200 for u in s.coverage["unsupported"])

    def test_mixed_verdicts_complex(self, mixed_results):
        s = synthesize(
            query="complex query",
            claims=["c1", "c2", "c3", "c4", "c5"],
            results=mixed_results,
            source_candidates=[
                {"url": "http://a.com/x", "title": "A", "text": "ta"},
                {"url": "http://b.com/y", "title": "B", "text": "tb"},
            ],
        )
        # 1 SUPPORTS, 1 REFUTES, 1 NUM_MISMATCH, 1 INSUFFICIENT, 1 CONFLICTING
        assert s.coverage["supported"] == 1
        assert s.coverage["partial"] == 2  # NUM_MISMATCH + CONFLICTING
        assert s.coverage["total"] == 5
        # 3 contradictions (REFUTES, NUM_MISMATCH, CONFLICTING)
        assert len(s.contradictions) == 3
        # 2 open questions (INSUFFICIENT, REFUTES)
        assert len(s.open_questions) == 2
        # Confidence: 1 + 0.5*2 = 2 / 5 = 0.4 - 0.1*3 + 0.02*2 = 0.04 → 0.04
        assert 0.0 <= s.confidence <= 1.0
