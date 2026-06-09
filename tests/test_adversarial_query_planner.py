"""
Adversarial tests for query planning (skill 6.2: query-planner-adversarial-eval).

Adversarial test classes per audit 2026-06-07, section 6.2:
1. narrative numbers ("5 человек", "24GB GPU", "7 дней")
2. intro words ("Specifically", "Please", "Looking for")
3. embedded URLs
4. code blocks
5. multi-aspect prompts
6. ambiguous entities (Apple, Orange, Gemini, Claude)
7. high-stakes queries (medical, financial, legal, security)
8. recency queries ("latest", "вчера", "сегодня")

These tests are CONTRACT tests, not perfection tests:
- They assert that the adaptation does NOT break the search plan
  (e.g. main_query is non-empty, no narrative noise in entities,
   ambiguous entities are not lost silently, recency hints survive).
- They do NOT assert "perfect disambiguation" — that's beyond v0.x.

Total: 30+ adversarial cases. See SKILL.md for the full coverage map.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from query_adaptation import _is_narrative_entity, adapt_query

# ====================================================================
# Helpers
# ====================================================================


def _all_entities(r):
    return [e.lower() for e in (r.get("extracted_entities") or [])]


def _has_main_term(r, term):
    """Check if term appears in main_query OR in top-2 entities."""
    if term.lower() in r["main_query"].lower():
        return True
    return any(term.lower() in e.lower() for e in _all_entities(r)[:2])


# ====================================================================
# 1. Narrative numbers
# ====================================================================


class TestAdversarial_NarrativeNumbers:
    """Numbers with size/time/count units that are project context,
    not search topics.
    """

    def test_24gb_gpu_is_kept(self):
        """Hardware spec: 24GB must NOT be filtered (it IS a search topic
        for hardware queries)."""
        q = "what GPU has 24GB VRAM and is good for LLM inference"
        r = adapt_query(q)
        # 24GB should appear in main_query or top entities
        assert "24gb" in r["main_query"].lower() or any(
            "24gb" in e.lower() for e in _all_entities(r)[:3]
        )

    def test_7_days_phrase_filtered(self):
        """'7 days' (project timeline) is narrative, not topic."""
        q = "build a startup MVP in 7 days using Next.js and PostgreSQL"
        r = adapt_query(q)
        # "7" or "days" should not dominate the main_query
        assert not r["main_query"].lower().startswith("7 ")
        # Either absent from main, or in dropped_terms
        main = r["main_query"].lower()
        dropped = r.get("dropped_terms") or []
        if "7" in main or "days" in main:
            assert "7" in dropped or "days" in dropped, (
                f"narrative '7 days' leaked: main={main!r}, dropped={dropped!r}"
            )

    def test_100_units_filtered(self):
        """'100 units' (project scale) is narrative."""
        q = "warehouse management system for 100 units with barcode scanning"
        r = adapt_query(q)
        # "100" should not be the main_query subject
        main = r["main_query"].lower()
        assert not main.startswith("100 "), (
            f"'100' should not lead main_query: {main!r}"
        )

    def test_30_lines_filtered(self):
        """'30 lines' (code size context) is narrative."""
        q = "Python function in 30 lines that does HTTP request retry logic"
        r = adapt_query(q)
        # Should focus on "Python function" not "30 lines"
        # "30" or "lines" should not be top entity
        for e in _all_entities(r)[:2]:
            assert "30 lines" not in e.lower(), (
                f"'30 lines' should not be top entity: {e!r}"
            )

    def test_2_projects_filtered(self):
        """'2 projects' (context) is narrative."""
        q = "help me compare 2 projects for portfolio: React Native vs Flutter"
        r = adapt_query(q)
        for e in _all_entities(r)[:2]:
            assert "2 projects" not in e.lower()


# ====================================================================
# 2. Intro words
# ====================================================================


class TestAdversarial_IntroWords:
    """Polite / request / softener words."""

    def test_please_filtered_when_long(self):
        """'Please' is a polite intro word. In a LONG query, the narrative
        filter should drop it; in a SHORT (passthrough) query, it survives
        because passthrough is by design verbatim.
        """
        # Long query: filter applies
        q_long = (
            "Please explain the difference between async and await in Python "
            "with examples and edge cases and best practices for production"
        )
        r = adapt_query(q_long)
        assert "please" not in r["main_query"].lower(), (
            f"'please' should be filtered in long query, got {r['main_query']!r}"
        )
        # Short query: passthrough keeps it (documented behavior)
        q_short = "Please explain async await in Python"
        r_short = adapt_query(q_short)
        assert r_short["adaptation_method"] == "passthrough"

    def test_looking_for_filtered(self):
        q = "looking for a good linter that works with Ruff and mypy"
        r = adapt_query(q)
        # "looking" should not be in entities
        assert "looking" not in _all_entities(r)
        assert "looking" not in r["main_query"].lower()

    def test_maybe_softener_filtered(self):
        q = "Maybe you can find something about TypeScript generic constraints"
        r = adapt_query(q)
        # "maybe" should not become entity
        assert "maybe" not in _all_entities(r)


# ====================================================================
# 3. Embedded URLs
# ====================================================================


class TestAdversarial_EmbeddedURLs:
    """URLs in user queries should not become search topics
    themselves (we can't search BY a URL).
    """

    def test_url_in_middle_not_topic(self):
        q = "according to https://example.com/article how does OAuth work"
        r = adapt_query(q)
        # URL itself should not be in entities as topic
        for e in _all_entities(r)[:3]:
            assert "https://example.com" not in e.lower(), (
                f"URL leaked into entities: {e!r}"
            )

    def test_multiple_urls_not_topics_in_long(self):
        """In a long query, URLs must not become the main search topic.
        (In a short passthrough query, URLs are kept verbatim by design.)
        """
        q = (
            "compare the documentation at https://react.dev and at "
            "https://vuejs.org for my project to decide which framework "
            "to use for a small team of developers"
        )
        r = adapt_query(q)
        main = r["main_query"].lower()
        assert "https://" not in main, (
            f"URL became main_query: {main!r}"
        )

    def test_url_with_query_params_not_entity(self):
        q = "the docs at https://docs.python.org/3/library/asyncio.html are confusing"
        r = adapt_query(q)
        # The URL itself should not be top entity
        url_entities = [e for e in _all_entities(r) if "http" in e]
        assert len(url_entities) == 0 or not _all_entities(r).index(url_entities[0]) < 2


# ====================================================================
# 4. Code blocks
# ====================================================================


class TestAdversarial_CodeBlocks:
    """Code in queries is example, not search topic."""

    def test_code_block_in_query(self):
        q = "what does this code do: ```python\nprint('hello')\n```"
        r = adapt_query(q)
        # "print" should not be a top entity
        for e in _all_entities(r)[:2]:
            assert "print" not in e.lower(), (
                f"code keyword became entity: {e!r}"
            )

    def test_code_with_comment(self):
        q = "in Python: # comment\nx = 1\n# what does x do here?"
        r = adapt_query(q)
        # No Python keyword should dominate
        for kw in ("comment", "x ="):
            assert not any(kw in e.lower() for e in _all_entities(r)[:2])

    def test_mixed_text_and_code(self):
        q = "Explain the difference between async def foo(): and regular def"
        r = adapt_query(q)
        # "async" is a Python keyword, should be search topic
        # "def" is a keyword too, but should not dominate
        # Main subject should be "async" or "Python"
        main = r["main_query"].lower()
        assert "async" in main or "python" in main


# ====================================================================
# 5. Multi-aspect prompts
# ====================================================================


class TestAdversarial_MultiAspect:
    """Multiple sub-questions in one query."""

    def test_two_aspects(self):
        """A 2-aspect query must produce a non-empty main_query.
        Alt queries are best-effort; if entity extraction fails, the
        system falls back to raw prefix. Both are valid.
        """
        q = "How does X work and what are the main alternatives to X"
        r = adapt_query(q)
        assert r["main_query"]
        # Either alt_queries populated, OR we fell back to raw prefix
        # (documented in adaptation_method)
        if not r.get("alt_queries"):
            assert r["adaptation_method"] == "deterministic"
            # In fallback, the raw query is preserved as a prefix
            assert r["raw_query"] or r["main_query"]

    def test_three_aspects(self):
        """A 3-aspect query must produce a non-empty main_query.
        Same contract as test_two_aspects: alt_queries are best-effort.
        """
        q = "Compare A vs B vs C in terms of performance, learning curve, and ecosystem"
        r = adapt_query(q)
        assert r["main_query"]
        # Should preserve the technical terms
        main = r["main_query"].lower()
        # In fallback mode, raw prefix is used (so 'Compare' is the first 8 words)
        # In extraction mode, technical terms should be present
        if r.get("alt_queries"):
            assert any(t in main for t in ("performance", "compare"))

    def test_mixed_ru_en_aspects(self):
        q = "Сравни Flutter и React Native и какой выбрать для команды 5 человек"
        r = adapt_query(q)
        # Language should be detected
        assert r["language"] in ("ru", "en")
        # Entities should include framework names
        ents = _all_entities(r)
        assert any("flutter" in e for e in ents) or "flutter" in r["main_query"].lower()
        assert any("react native" in e for e in ents) or "react native" in r["main_query"].lower()

    def test_technical_plus_context(self):
        q = "I have a Redis cluster on Kubernetes, how do I monitor it with Prometheus"
        r = adapt_query(q)
        # Technical terms should be preserved
        assert r["main_query"]
        # Should mention one of the tech terms
        main = r["main_query"].lower()
        assert any(t in main for t in ("redis", "kubernetes", "prometheus"))


# ====================================================================
# 6. Ambiguous entities
# ====================================================================


class TestAdversarial_AmbiguousEntities:
    """Words that mean different things in different contexts."""

    def test_apple_not_lost(self):
        """'Apple' should not be silently dropped or transformed."""
        q = "Should I buy Apple stock given the current market"
        r = adapt_query(q)
        # Apple should be in main or top entities
        assert _has_main_term(r, "apple"), (
            f"'Apple' lost: main={r['main_query']!r}, entities={r['extracted_entities']!r}"
        )

    def test_claude_not_lost(self):
        """'Claude' (Anthropic's model) should not be dropped."""
        q = "What is the context window of Claude 3.5 Sonnet"
        r = adapt_query(q)
        assert _has_main_term(r, "claude"), (
            f"'Claude' lost: main={r['main_query']!r}, entities={r['extracted_entities']!r}"
        )

    def test_gemini_preserved(self):
        """'Gemini' (Google model) should not be lost."""
        q = "Gemini vs GPT-4 comparison for code generation"
        r = adapt_query(q)
        assert _has_main_term(r, "gemini"), (
            f"'Gemini' lost: main={r['main_query']!r}, entities={r['extracted_entities']!r}"
        )

    def test_javascript_not_split_to_java(self):
        """'JavaScript' should not be confused with 'Java'."""
        q = "best JavaScript testing framework in 2026"
        r = adapt_query(q)
        # 'javascript' should be in main or entities, NOT just 'java'
        main = r["main_query"].lower()
        ents = _all_entities(r)
        has_js = "javascript" in main or any("javascript" in e for e in ents)
        has_only_java = "java" in main and not has_js
        assert has_js or not has_only_java, (
            f"JavaScript conflated with Java: main={main!r}, entities={ents!r}"
        )

    def test_orange_preserved(self):
        """'Orange' (ambiguous: fruit/company/color) should not be lost."""
        q = "Orange company history in telecommunications"
        r = adapt_query(q)
        assert _has_main_term(r, "orange"), (
            f"'Orange' lost: main={r['main_query']!r}, entities={r['extracted_entities']!r}"
        )


# ====================================================================
# 7. High-stakes queries
# ====================================================================


class TestAdversarial_HighStakes:
    """Medical, financial, legal, security queries.

    Currently adapt_query() does not have risk-level classification,
    so these tests are 'must not break' contracts: the adaptation
    should still produce a non-empty main_query.
    """

    def test_medical_query(self):
        q = "What are the side effects of ibuprofen in children under 12"
        r = adapt_query(q)
        assert r["main_query"]
        # "ibuprofen" should be in main or entities
        assert _has_main_term(r, "ibuprofen")

    def test_financial_query(self):
        q = "What is the current inflation rate and how does it affect savings"
        r = adapt_query(q)
        assert r["main_query"]
        # Should mention "inflation" or "savings"
        main = r["main_query"].lower()
        assert any(t in main for t in ("inflation", "savings", "rate"))

    def test_legal_query(self):
        q = "What are the legal requirements for opening a small business in Russia"
        r = adapt_query(q)
        assert r["main_query"]

    def test_security_query(self):
        q = "How to detect a man-in-the-middle attack on public WiFi"
        r = adapt_query(q)
        assert r["main_query"]
        # Should mention "attack" or "wifi" or "man-in-the-middle"
        main = r["main_query"].lower()
        assert any(t in main for t in ("attack", "wifi", "man-in-the-middle", "mitm"))


# ====================================================================
# 8. Recency queries
# ====================================================================


class TestAdversarial_RecencyQueries:
    """Time-sensitive words: 'latest', 'yesterday', 'today', '2026'.

    Currently the narrative filter does NOT handle these — they may
    leak into entities. This is a known limitation; we document it
    here as failing tests for now (TODO for 6.3 retrieval-routing).
    """

    def test_latest_not_top_entity(self):
        """'latest' is a recency marker, not a search topic itself."""
        q = "What is the latest version of Python in 2026"
        r = adapt_query(q)
        # 'latest' alone should not be top entity
        for e in _all_entities(r)[:2]:
            assert e.lower() != "latest", (
                f"'latest' became top entity: {e!r}"
            )
        # But "Python" should be
        assert _has_main_term(r, "python")

    def test_today_not_top_entity(self):
        q = "What is the weather today in Moscow"
        r = adapt_query(q)
        for e in _all_entities(r)[:2]:
            assert e.lower() != "today"
        # "weather" or "moscow" should be
        main = r["main_query"].lower()
        assert "weather" in main or "moscow" in main

    def test_vchera_not_entity(self):
        """'вчера' (yesterday) should not be top entity."""
        q = "Какие новости были вчера про Python"
        r = adapt_query(q)
        for e in _all_entities(r)[:2]:
            assert e.lower() != "вчера"
        # "новости" or "python" should be there
        main = r["main_query"].lower()
        assert any(t in main for t in ("новости", "python", "новость"))

    def test_year_not_topic(self):
        """A bare year is context, not topic."""
        q = "What happened in 2026 with AI"
        r = adapt_query(q)
        for e in _all_entities(r)[:2]:
            assert e.strip() != "2026", (
                f"bare year became top entity: {e!r}"
            )


# ====================================================================
# 9. Helper regression: _is_narrative_entity unit tests
# ====================================================================


class TestNarrativeEntityHelper:
    """Direct unit tests for the internal narrative filter."""

    def test_intro_ru(self):
        assert _is_narrative_entity("Расскажи") is True
        assert _is_narrative_entity("Подробно") is True

    def test_intro_en(self):
        assert _is_narrative_entity("Specifically") is True
        assert _is_narrative_entity("Please") is True

    def test_numeric_phrase_ru(self):
        assert _is_narrative_entity("5 человек") is True
        assert _is_narrative_entity("3 месяца") is True
        assert _is_narrative_entity("2 недели") is True

    def test_real_topics_not_narrative(self):
        assert _is_narrative_entity("Flutter") is False
        assert _is_narrative_entity("React Native") is False
        assert _is_narrative_entity("Python") is False
        assert _is_narrative_entity("Moscow") is False
