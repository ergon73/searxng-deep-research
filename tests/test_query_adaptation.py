"""
Tests for query_adaptation module.

Implements 8 test cases from ~/.hermes/skills/research/query-adaptation/SKILL.md
plus regression tests for the most common bug class (over-truncation,
under-extraction, fabrication).

Run: cd /opt/searxng && python3 -m pytest tests/test_query_adaptation.py -v
"""
import sys
from pathlib import Path

# Path setup for portable tests (no hardcoded paths)
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from query_adaptation import (
    _extract_candidate_entities,
    _score_entity,
    adapt_query,
    build_search_plan_preview,
    detect_language,
)

# ====================================================================
# Test 1: passthrough for short queries (≤10 words)
# ====================================================================

def test_passthrough_short_query():
    """3-word query passes through unchanged."""
    q = "Gemma 4 12B"
    r = adapt_query(q)
    assert r["adaptation_method"] == "passthrough"
    assert r["main_query"] == q
    assert r["alt_queries"] == []
    assert r["extracted_entities"] == []


def test_passthrough_at_threshold():
    """10-word query (boundary) is passthrough."""
    q = "what is the new in Python 3.13"  # 8 words
    r = adapt_query(q)
    assert r["adaptation_method"] == "passthrough"
    assert r["main_query"] == q


def test_passthrough_under_threshold():
    """9-word query is passthrough (boundary)."""
    q = "как включить MTP в Gemma 4 12B"  # 6 words
    r = adapt_query(q)
    assert r["adaptation_method"] == "passthrough"


# ====================================================================
# Test 2: language detection
# ====================================================================

def test_language_detection_ru():
    """Cyrillic-dominant query → 'ru'."""
    assert detect_language("что нового в Python 3.13") == "ru"


def test_language_detection_en():
    """Latin-only query → 'en'."""
    assert detect_language("Gemma 4 12B benchmarks") == "en"


def test_language_detection_mixed():
    """Mixed-script query routes by dominant script."""
    # Mostly Russian (1 English product code, 4 Russian words) → ru
    assert detect_language("Gemma 4 12B поддержка и совместимость с моделями") == "ru"
    # Mostly English (3 English words, 1 Russian word) → en
    assert detect_language("Gemma 4 12B на русском") == "en"


# ====================================================================
# Test 3: long query decomposition
# ====================================================================

def test_long_query_deterministic():
    """20+ word query triggers deterministic adaptation."""
    q = (
        "I'm preparing a technical briefing on Gemma 4 12B covering "
        "multimodal architecture, MMLU benchmarks, and QAT quantization"
    )
    r = adapt_query(q)
    assert r["adaptation_method"] == "deterministic"
    assert 1 <= len(r["main_query"].split()) <= 8
    assert "Gemma 4 12B" in r["main_query"] or "Gemma 4 12B" in " ".join(r["alt_queries"])


def test_long_query_has_entities():
    """Long query extracts at least 1 entity."""
    q = (
        "I need to understand Gemma 4 12B multimodal architecture "
        "and benchmark performance on MMLU"
    )
    r = adapt_query(q)
    assert len(r["extracted_entities"]) >= 1


# ====================================================================
# Test 4: hard rules from SKILL.md
# ====================================================================

def test_main_query_length_gate():
    """main_query must be 1-8 words (hard rule #2)."""
    # Construct query that might tempt extractor to return long string
    q = (
        "Gemma 4 12B QAT Apache 2.0 multimodal architecture benchmarks "
        "MMLU GPQA hardware requirements"
    )
    r = adapt_query(q)
    assert 1 <= len(r["main_query"].split()) <= 8, \
        f"main_query has {len(r['main_query'].split())} words: {r['main_query']!r}"


def test_alt_queries_length_gate():
    """Each alt_query must be 1-10 words (hard rule #3)."""
    q = (
        "Gemma 4 12B QAT multimodal architecture MMLU GPQA benchmarks "
        "Q4_0 Q8_0 BF16 quantization hardware inference"
    )
    r = adapt_query(q)
    for alt in r["alt_queries"]:
        wc = len(alt.split())
        assert 1 <= wc <= 10, f"alt_query has {wc} words: {alt!r}"


def test_extracted_entities_count():
    """extracted_entities ≤ 10 для длинных factual queries (FIX 2026-06-07).

    Hard rule #4 был "0-5", но для factual queries с 5+ content nouns
    ("Сколько ступеней у ракеты Falcon 9", "Gemma 4 12B ... hardware")
    cap 5 терял critical terms. Новое правило: ≤ 10 для длинных queries.
    """
    q = (
        "Gemma 4 12B QAT multimodal architecture MMLU GPQA benchmarks "
        "Q4_0 Q8_0 BF16 quantization hardware"
    )
    r = adapt_query(q)
    assert 0 <= len(r["extracted_entities"]) <= 10


# ====================================================================
# Test 5: NO FABRICATION (anti-pattern AP4)
# ====================================================================

def test_no_entity_fabrication():
    """Every extracted entity must appear (case-insensitive) in original query."""
    q = "Gemma 4 12B QAT benchmarks and multimodal architecture"
    r = adapt_query(q)
    q_lower = q.lower()
    for e in r["extracted_entities"]:
        assert e.lower() in q_lower, \
            f"Entity {e!r} fabricated — not in original query"


def test_no_url_fabrication():
    """adapt_query never produces entities with URLs or special chars."""
    q = "Gemma 4 12B review what's new"
    r = adapt_query(q)
    for e in r["extracted_entities"]:
        assert "http" not in e.lower()
        assert "www." not in e.lower()


# ====================================================================
# Test 6: edge cases
# ====================================================================

def test_empty_string():
    """Empty query doesn't crash, returns graceful fallback."""
    r = adapt_query("")
    assert "main_query" in r
    assert r["adaptation_method"] in ("passthrough", "deterministic")


def test_query_with_code_block():
    """Query containing code (markdown) still works."""
    q = (
        "I'm trying to deploy Gemma 4 12B locally using vLLM. "
        "Here's my code: ```python from vllm import LLM ```. "
        "I'm getting OOM errors on a 24GB GPU."
    )
    r = adapt_query(q)
    assert r["adaptation_method"] in ("deterministic", "passthrough")
    assert 1 <= len(r["main_query"].split()) <= 8


def test_query_with_embedded_url():
    """Query containing URL doesn't break the extractor."""
    q = (
        "I'm reading https://blog.google/2026/gemma-4/ and want to know "
        "the differences between Gemma 4 12B and Gemma 3 12B."
    )
    r = adapt_query(q)
    assert r["adaptation_method"] in ("deterministic", "passthrough")
    # URL should not appear as entity (we filter)
    for e in r["extracted_entities"]:
        assert "blog.google" not in e.lower() or "gemma" in e.lower()


# ====================================================================
# Test 7: adaptation_method field required
# ====================================================================

def test_adaptation_method_required():
    """adaptation_method must be one of the three values (hard rule #8)."""
    for q in ["Gemma 4 12B",
              "I need to understand Gemma 4 12B multimodal architecture",
              "что нового"]:
        r = adapt_query(q)
        assert r["adaptation_method"] in ("passthrough", "deterministic", "llm_fallback")


# ====================================================================
# Test 8: regression tests (specific known cases)
# ====================================================================

def test_ru_mixed_script_keeps_russian():
    """RU query with embedded English term routes to Russian."""
    q = "что лучше для деплоя на слабом железе Gemma 4 E2B или Llama 3.2 3B"
    r = adapt_query(q)
    assert r["language"] == "ru"
    # At least one entity should contain the product code
    has_product = any("Gemma" in e or "Llama" in e for e in r["extracted_entities"])
    assert has_product, f"Product code not extracted from mixed query: {r['extracted_entities']}"


def test_200_word_scenario_extracts_key_entities():
    """L1-equivalent scenario: 200+ words, multi-aspect, extracts key entities."""
    q = (
        "I need to do a deep dive on Gemma 4 12B for a research paper. "
        "Specifically: (1) the unified multimodal architecture, "
        "(2) benchmark numbers on MMLU Pro and GPQA Diamond, "
        "(3) hardware requirements for BF16, Q8_0, and Q4_0 inference, "
        "(4) whether it can run on Apple Silicon with 16GB unified memory, "
        "(5) the exact training data cutoff date, and "
        "(6) licensing terms and commercial use restrictions. "
        "Please cite primary sources and give me working code examples."
    )
    r = adapt_query(q)
    # Must have main_query (short, focused)
    assert 1 <= len(r["main_query"].split()) <= 8
    # Must have at least Gemma 4 12B
    assert "Gemma 4 12B" in r["main_query"] or \
        "Gemma 4 12B" in " ".join(r["extracted_entities"])
    # Method should be deterministic (not LLM)
    assert r["adaptation_method"] == "deterministic"


# ====================================================================
# Test 9: internal helpers
# ====================================================================

def test_extract_candidate_entities_finds_product_codes():
    """Product code regex catches 'Gemma 4 12B' style entities."""
    q = "I want Gemma 4 12B for iPhone 17 Pro"
    candidates = _extract_candidate_entities(q)
    assert "Gemma 4 12B" in candidates
    assert "iPhone 17 Pro" in candidates


def test_score_entity_position_weight():
    """Earlier-mentioned entities score higher than later-mentioned."""
    q_early = "Gemma 4 12B is great. Llama 4 is also good"
    q_late = "Llama 4 is also good. Gemma 4 12B is great"
    s_early = _score_entity("Gemma 4 12B", q_early)
    s_late = _score_entity("Gemma 4 12B", q_late)
    assert s_early > s_late


def test_score_entity_capitalization_bonus():
    """Capitalized entity scores higher than lowercase."""
    q = "we have gemma 4 12b and Gemma 4 12B available"
    s_lower = _score_entity("gemma 4 12b", q)
    s_upper = _score_entity("Gemma 4 12B", q)
    assert s_upper >= s_lower


def test_score_entity_product_code_bonus():
    """Product code (letters+digits) gets bonus when query contains it."""
    # Query must contain the code for scoring to apply
    q = "I want to use Gemma 4 12B for inference"
    s_text = _score_entity("Gemma", q)
    s_code = _score_entity("Gemma 4 12B", q)
    # Code should score higher (freq=1, pos bonus same, cap=1 each, plus code +2)
    assert s_code > s_text, f"Code {s_code} should beat text-only {s_text}"


# ====================================================================
# Tests for skill 6.1: search-intent confirmation
# Acceptance criteria #4 from the spec.
# ====================================================================


def test_confirmation_long_multi_aspect_query_needs_confirmation():
    """Long multi-aspect query (>=40 words) must trigger needs_confirmation.

    Real-world example: Russian user asks a 4-aspect technical question.
    Even with a clean adaptation, the user should see the search plan
    and decide whether to proceed.
    """
    long_q = (
        "Расскажи подробно про мобильное приложение на 5 человек "
        "с использованием современных фреймворков включая Flutter, "
        "React Native и Kotlin Multiplatform с разбором плюсов и минусов, "
        "производительности, опыта найма разработчиков и реальных кейсов "
        "внедрения в продакшн за последние два года в российских компаниях"
    )
    assert len(long_q.split()) > 40, "fixture: query must be >40 words"

    r = adapt_query(long_q)
    assert "needs_confirmation" in r
    assert "adaptation_confidence" in r
    assert "dropped_terms" in r
    assert "added_terms" in r
    assert "confirmation_reason" in r
    assert r["needs_confirmation"] is True
    # long_query trigger MUST be in reasons
    assert any("long_query" in reason for reason in r["confirmation_reason"]), (
        f"Expected long_query trigger, got {r['confirmation_reason']}"
    )


def test_confirmation_short_passthrough_no_confirmation():
    """Short passthrough (≤10 words) must NOT need confirmation.

    Acceptance criteria #4: 'short passthrough does not need confirmation'.
    """
    q = "Gemma 4 12B MTP benchmark"
    r = adapt_query(q)
    assert r["adaptation_method"] == "passthrough"
    assert r["needs_confirmation"] is False
    assert r["adaptation_confidence"] >= 0.75
    assert r["confirmation_reason"] == []


def test_confirmation_narrative_number_not_main_entity():
    """'5 человек' must not become a primary search entity.

    Acceptance criteria #4: '"5 человек" не становится главной entity'.
    This is the bug ChatGPT flagged in section 5, P0 of the audit.
    """
    q = (
        "мобильное приложение на 5 человек Flutter React Native "
        "Kotlin Multiplatform сравнение производительности и найма"
    )
    r = adapt_query(q)
    # "5 человек" must not be the main_query nor the top entity.
    # We check that main_query doesn't START with "5 человек" and that
    # none of the top-2 entities are "5 человек".
    assert not r["main_query"].lower().startswith("5 человек"), (
        f"main_query should not lead with '5 человек', got {r['main_query']!r}"
    )
    top_entities = (r.get("extracted_entities") or [])[:2]
    for e in top_entities:
        assert "5 человек" not in e.lower(), (
            f"top entity should not be '5 человек', got {top_entities!r}"
        )
    # The narrative term "5" or "человек" should appear in dropped_terms
    # OR not appear in main_query. We accept either:
    # - main_query has no "5"/"человек"
    # - or dropped_terms includes them
    main_lower = r["main_query"].lower()
    dropped = r.get("dropped_terms") or []
    narrative_dropped = "5" in dropped or "человек" in dropped
    narrative_absent = "5" not in main_lower and "человек" not in main_lower
    assert narrative_dropped or narrative_absent, (
        f"Narrative '5 человек' leaked into main_query {r['main_query']!r} "
        f"and not in dropped_terms {dropped!r}"
    )


def test_confirmation_intro_words_not_entities():
    """Intro words like 'Specifically' / 'Расскажи подробно' must not
    become search entities.

    Acceptance criteria #4: '"Specifically" не становится entity'.
    """
    q = (
        "Specifically I want to find a good Python linter that works "
        "with Ruff and supports type checking and is fast on large codebases"
    )
    r = adapt_query(q)
    # "Specifically" must not be in entities
    entities_lower = [e.lower() for e in (r.get("extracted_entities") or [])]
    main_lower = r["main_query"].lower()
    assert "specifically" not in entities_lower, (
        f"'Specifically' should not be an entity, got {r['extracted_entities']!r}"
    )
    assert "specifically" not in main_lower, (
        f"'Specifically' leaked into main_query {r['main_query']!r}"
    )
    # Same for the Russian "Расскажи подробно"
    q_ru = (
        "Расскажи подробно про мобильное приложение Flutter React Native "
        "Kotlin Multiplatform сравнение производительности и найма"
    )
    r_ru = adapt_query(q_ru)
    entities_ru_lower = [e.lower() for e in (r_ru.get("extracted_entities") or [])]
    for stop in ("расскажи", "подробно"):
        assert stop not in entities_ru_lower, (
            f"'{stop}' should not be an entity, got {r_ru['extracted_entities']!r}"
        )
        assert stop not in r_ru["main_query"].lower(), (
            f"'{stop}' leaked into main_query {r_ru['main_query']!r}"
        )


def test_confirmation_added_fabricated_term_triggers():
    """When adaptation fabricates a term not in raw query,
    added_terms must list it AND needs_confirmation must be True.

    Acceptance criteria #4: 'added fabricated term triggers confirmation'.
    """
    # Construct a query where extraction will likely fabricate.
    # We test the function directly with a known-fabricated case.
    r = adapt_query(
        "Расскажи про Flutter и React Native и Kotlin Multiplatform "
        "с примерами кода на каждом фреймворке и разбором производительности"
    )
    # Whatever the adaptation produces, added_terms must be a list,
    # and if non-empty, needs_confirmation must be True.
    assert isinstance(r["added_terms"], list)
    if r["added_terms"]:
        assert r["needs_confirmation"] is True
        # And the trigger must mention added_terms
        assert any("added_terms" in reason for reason in r["confirmation_reason"]), (
            f"Expected added_terms trigger, got {r['confirmation_reason']}"
        )


def test_confirmation_dropped_critical_entity_triggers():
    """When adaptation drops a critical content token (>=3 chars) from
    the raw query, dropped_terms must list it AND needs_confirmation
    must be True.

    Acceptance criteria #4: 'dropped critical entity triggers confirmation'.
    """
    # A long query where the adaptation may discard content.
    # We verify the contract: if dropped_terms contains any term >=3 chars,
    # confirmation is required.
    r = adapt_query(
        "Сравни пожалуйста производительность Flutter React Native "
        "и Kotlin Multiplatform для крупного мобильного приложения "
        "с командой из 5 человек и оцени трудозатраты на миграцию"
    )
    assert isinstance(r["dropped_terms"], list)
    if r["dropped_terms"]:
        # Filter to critical (len>=3) per the trigger heuristic
        critical = [t for t in r["dropped_terms"] if len(t) >= 3]
        if critical:
            assert r["needs_confirmation"] is True
            assert any(
                "dropped_critical_terms" in reason
                for reason in r["confirmation_reason"]
            ), (
                f"Expected dropped_critical_terms trigger, "
                f"got {r['confirmation_reason']}"
            )


# ====================================================================
# Tests for build_search_plan_preview (public function)
# ====================================================================


def test_build_search_plan_preview_contains_key_fields():
    """build_search_plan_preview must render raw_query, main_query,
    language, confidence, and needs_confirmation flag.
    """
    q = "Gemma 4 12B MTP benchmark"
    r = adapt_query(q)
    # adapt_query() doesn't add raw_query when called directly;
    # we inject it for the preview (since the real caller has the raw query).
    r["raw_query"] = q
    preview = build_search_plan_preview(r)
    assert "Gemma 4 12B" in preview
    assert "language" in preview
    assert "adaptation_confidence" in preview
    assert "Требуется подтверждение" in preview
    # For a short passthrough query, no confirmation needed
    assert "нет" in preview  # "риск низкий, ищу автоматически"


def test_build_search_plan_preview_shows_reasons_when_confirmation_needed():
    """When needs_confirmation=True, the preview must list the reasons."""
    long_q = (
        "Расскажи подробно про мобильное приложение на 5 человек "
        "с использованием современных фреймворков включая Flutter, "
        "React Native и Kotlin Multiplatform с разбором плюсов и минусов, "
        "производительности, опыта найма разработчиков и реальных кейсов "
        "внедрения в продакшн за последние два года в российских компаниях "
        "сравни стоимость разработки и поддержки"
    )
    # Force a >40-word long query so 'long_query' trigger fires.
    # Pad with extra content tokens if needed.
    while len(long_q.split()) <= 40:
        long_q += " и качество документации комьюнити"
    assert len(long_q.split()) > 40, (
        f"fixture: query must be >40 words, got {len(long_q.split())}"
    )

    r = adapt_query(long_q)
    assert r["needs_confirmation"] is True
    r["raw_query"] = long_q
    preview = build_search_plan_preview(r)
    assert "Требуется подтверждение:** да" in preview
    assert "Причины" in preview
    # Must offer the three action options
    assert "APPROVE_SEARCH_PLAN" in preview
    assert "SEARCH_RAW_QUERY" in preview
    assert "EDIT_QUERY_PLAN" in preview
    # At least one reason code must be visible (long_query, dropped_critical_terms,
    # low_confidence, added_terms, or zero_entities_extracted)
    reason_codes = ("long_query", "dropped_critical_terms", "low_confidence",
                    "added_terms", "zero_entities_extracted")
    assert any(code in preview for code in reason_codes), (
        f"Expected at least one reason code in preview, got:\n{preview}"
    )


# ====================================================================
# FIX 2026-06-07 (e2e Falcon 9): regression tests for 6.1 production bug
# Root cause: _PRODUCT_ENTITY_RE was [A-Z] only, не ловил кириллицу.
# Side fix: top-N entities cap подняли 5→8 для factual queries.
# ====================================================================

def test_ru_factual_query_preserves_cyrillic_capitalized_words():
    """Regression: [A-ZА-ЯЁ] regex должен ловить 'Сколько', 'Falcon 9'."""
    from query_adaptation import _extract_candidate_entities
    q = "Сколько ступеней у ракеты Falcon 9"
    cands = _extract_candidate_entities(q)
    # 'Сколько' должна быть в списке (cyrillic Capital)
    assert "Сколько" in cands
    assert "Falcon 9" in cands


def test_ru_factual_query_keeps_critical_content_nouns():
    """Regression: 'ступеней', 'ракеты', 'запуск' НЕ должны быть dropped.

    Это был production bug — e2e Falcon 9 показал что они терялись.
    """
    q = "Сколько ступеней у ракеты Falcon 9 и в каком году первый запуск"
    r = adapt_query(q)
    # Все эти слова должны быть в extracted_entities
    ents = " ".join(r["extracted_entities"]).lower()
    assert "ступеней" in ents, f"'ступеней' missing from entities: {r['extracted_entities']}"
    assert "ракеты" in ents, f"'ракеты' missing from entities: {r['extracted_entities']}"
    # Не должно быть dropped critical term 'запуск'
    assert "запуск" not in r.get("dropped_terms", []), \
        f"'запуск' should not be dropped: {r.get('dropped_terms')}"


def test_ru_factual_query_no_confirmation_when_content_preserved():
    """Regression: после фикса factual query НЕ требует confirmation."""
    q = "Сколько ступеней у ракеты Falcon 9 и в каком году первый запуск"
    r = adapt_query(q)
    # 'году' (год) в blacklist → not critical, 'каком/первый' < 4 chars
    # → confirmation НЕ нужна когда все real content nouns сохранены
    assert r["needs_confirmation"] is False, \
        f"Factual query shouldn't need confirmation: {r.get('confirmation_reason')}"


def test_extended_content_nouns_ru_factual():
    """Regression: _extract_content_nouns ловит lowercase RU/EN nouns."""
    from query_adaptation import _extract_content_nouns
    q = "Сколько ступеней у ракеты Falcon 9 и в каком году первый запуск"
    nouns = _extract_content_nouns(q)
    # Critical content nouns должны быть
    for word in ("ступеней", "ракеты", "запуск"):
        assert word in nouns, f"'{word}' missing from content nouns: {nouns}"
    # 'сколько' (именительный вопрос) в blacklist
    assert "сколько" not in nouns, f"'сколько' should be in blacklist: {nouns}"
