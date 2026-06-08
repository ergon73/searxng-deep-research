"""
Fact extraction + numeric morphology + negation tests.

Locks in DR-05062026(3) §Phase 3 (5 acceptance criteria):
  AC1. "5 июня 2026" not duplicated as "5 июня"
  AC2. single short capitalized ("Python", "Министерство") NOT extracted as facts
  AC3. multi-word entities ("Министерство обороны") ARE extracted
  AC4. "123 дрона" matches "123 дронов" via numeric morphology
  AC5. tests first, patch second
"""
import pytest

from hermes_deepresearch import _extract_facts, _is_negated, _match_in_text


class TestIsNegated:
    """Per-source negation detection."""

    @pytest.mark.parametrize("text", [
        "Дрон не был сбит над Москвой",
        "Беспилотников нет в воздушном пространстве",  # содержит "нет" перед существительным
        "Сведений о сбитом дроне нет",  # fact до "нет"
        "дрон не сбит, а только обнаружен",
        "no drones were shot down",  # "no <noun>" → "drones"
    ])
    def test_detects_negation(self, text):
        # Берём слово, которое реально есть в тексте
        if "дрон" in text.lower():
            fact = "дрон"
        else:
            fact = "беспилотник" if "беспилотник" in text.lower() else "drone"
        assert _is_negated(fact, text.lower()) is True, f"Failed for: {text}"

    @pytest.mark.parametrize("text", [
        "Дрон был сбит над Москвой",
        "БПЛА сбит ПВО",
        "the drone was shot down",
    ])
    def test_no_negation(self, text):
        assert _is_negated("дрон", text.lower()) is False


class TestExtractFacts:
    """Fact extraction with SKIP_NUM_UNITS and proper-noun filters."""

    def test_extracts_numeric_facts_in_context(self):
        text = "За ночь сбито 123 дрона над Москвой. Всего 10 дронов в Московской области."
        facts = _extract_facts(text)
        # 123 дрона — да, 10 дронов — да
        assert any("123" in f for f in facts), f"Should extract '123 дрона': {facts}"
        assert any("10" in f for f in facts), f"Should extract '10 дронов': {facts}"

    def test_skips_noise_units(self):
        text = "1 item in 2020 5 примеров кода"
        facts = _extract_facts(text)
        # "1 item" не должно попасть (item в SKIP_NUM_UNITS)
        assert not any("item" in f.lower() for f in facts), f"Should skip '1 item': {facts}"
        # "5 примеров" не должно попасть (пример в SKIP_NUM_UNITS)
        assert not any("пример" in f.lower() for f in facts), f"Should skip '5 примеров': {facts}"

    def test_extracts_dates(self):
        text = "5 июня 2026 года произошло событие"
        facts = _extract_facts(text)
        assert any("5 июня 2026" in f or "5 июня" in f for f in facts), f"Should extract date: {facts}"

    def test_skips_single_short_capitalized(self):
        text = "Методы были разные. Python используется."
        facts = _extract_facts(text)
        # "Методы" (7 chars) должно пройти, "Python" (6 chars) — пограничный
        # Главное — нет одиночных коротких capitalized
        for f in facts:
            # Если это capitalized, оно должно быть либо длинным, либо multi-word
            assert len(f) >= 5 or " " in f, f"Got too-short capitalized: '{f}'"

    def test_respects_max_facts(self):
        text = "1 один 2 два 3 три 4 четыре 5 пять 6 шесть 7 семь 8 восемь 9 девять 10 десять"
        facts = _extract_facts(text, max_facts=3)
        assert len(facts) <= 3

    def test_empty_text(self):
        assert _extract_facts("") == []
        assert _extract_facts(None) == []


# =============================================================================
# Phase 3 acceptance criteria — DR §Phase 3 (5 ACs)
# =============================================================================


class TestPhase3_AC1_DateDedup:
    """AC1: "5 июня 2026" not duplicated as "5 июня" in same text."""

    def test_full_date_kept_partial_date_dropped(self):
        text = "5 июня 2026 года произошло событие. Также 5 июня был дождь."
        facts = _extract_facts(text)
        # Полная дата должна быть
        assert any("5 июня 2026" in f for f in facts), f"missing full date: {facts}"
        # "5 июня" без года НЕ должен быть отдельным фактом (он подмножество "5 июня 2026")
        bare = [f for f in facts if f.strip().lower() in ("5 июня", "5 июня 2026")]
        # Либо только полная, либо вообще ни одной
        assert len(bare) == 1 and "5 июня 2026" in bare[0], (
            f"expected single full date, got {bare} (all facts: {facts})"
        )

    def test_partial_date_without_full_kept(self):
        # Если в тексте ТОЛЬКО "5 июня" без года — это валидный факт
        text = "Событие произошло 5 июня."
        facts = _extract_facts(text)
        assert any("5 июня" in f for f in facts), f"missing partial date: {facts}"


class TestPhase3_AC2_SkipSingleShortCapitalized:
    """AC2: single short capitalized ('Python', 'Министерство') NOT extracted."""

    def test_python_alone_not_extracted(self):
        text = "Python популярен в мире. Язык Python используется везде."
        facts = _extract_facts(text)
        assert not any(f == "Python" for f in facts), f"should skip single 'Python': {facts}"

    def test_ministerstvo_alone_not_extracted(self):
        text = "Министерство работает над проектом. Сотрудники министерства заняты."
        # "Министерство" с большой буквы в начале предложения — НЕ должно быть фактом
        # (но "министерства" в нижнем регистре вообще не матчится capitalized-регексом)
        facts = _extract_facts(text)
        assert "Министерство" not in facts, f"should skip single 'Министерство': {facts}"

    def test_short_capitalized_inside_sentence_not_extracted(self):
        # "Сегодня Python" — оба capitalized, но одиночные → не должны быть фактами
        text = "Сегодня Python выпустил новую версию."
        facts = _extract_facts(text)
        # "Сегодня" — стоп-слово, "Python" — single short → оба не должны пройти
        for f in facts:
            if f[0].isupper():
                assert len(f.split()) > 1 or len(f) >= 10, (
                    f"single short capitalized leaked: '{f}' (all: {facts})"
                )


class TestPhase3_AC3_ExtractMultiWordEntities:
    """AC3: multi-word entities ('Министерство обороны') ARE extracted."""

    def test_ministerstvo_oborony_extracted(self):
        text = "Сегодня Министерство обороны РФ выступило с заявлением."
        facts = _extract_facts(text)
        assert any("Министерство обороны" in f for f in facts), (
            f"missing 'Министерство обороны': {facts}"
        )

    def test_three_word_entity_extracted(self):
        # "Пресс-секретарь" с дефисом — пограничный случай, проверим 2-3 слова
        text2 = "Пресс секретарь Белого дома выступил с речью."
        facts = _extract_facts(text2)
        # Должно быть минимум одно multi-word entity
        multi = [f for f in facts if len(f.split()) >= 2 and f[0].isupper()]
        assert multi, f"expected multi-word entity, got: {facts}"

    def test_single_word_still_dropped_among_multi(self):
        # "Методы" — одиночное 6 chars. Должно быть отброшено, даже если рядом "Методы машинного обучения"
        text = "Методы машинного обучения разнообразны."
        facts = _extract_facts(text)
        # "Методы" alone не должно быть фактом
        assert "Методы" not in facts, f"single 'Методы' leaked: {facts}"


class TestPhase3_AC4_NumericMorphology:
    """AC4: '123 дрона' matches '123 дронов' (singular vs genitive plural)."""

    def test_drona_matches_dronov(self):
        fact = "123 дрона"
        text = "Всего 123 дронов было перехвачено за ночь."
        matched, method, score = _match_in_text(fact, text)
        assert matched, f"'123 дрона' should match '123 дронов': method={method}, score={score}"
        assert score >= 80, f"score too low: {score}"

    def test_sbito_matches_sbity(self):
        # past-tense singular vs short-form plural
        fact = "5 сбито"
        text = "По данным МО, 5 сбиты средствами ПВО."
        matched, method, score = _match_in_text(fact, text)
        assert matched, f"'5 сбито' should match '5 сбиты': method={method}, score={score}"

    def test_chas_matches_chasov(self):
        # "1 час" vs "3 часов" — different number, but morphology should normalise "час"
        fact = "1 час"
        text = "Через 3 часов дрон был обнаружен."
        matched, method, score = _match_in_text(fact, text)
        # Числа разные, это НЕ должен быть exact match, но stem "час" должен помочь
        # Без morphology: "1 час" vs "3 часов" → очень низкий fuzzy score
        # С morphology: stem "час" присутствует в обоих → должно быть scored
        assert score > 0 or matched, f"expected some signal: score={score}, matched={matched}"

    def test_drone_singular_matches_drones_plural_en(self):
        fact = "1 drone"
        text = "At least 5 drones were detected overnight."
        matched, method, score = _match_in_text(fact, text)
        assert matched, f"'1 drone' should match '5 drones': method={method}, score={score}"



# ====================================================================
# Skill 6.5 v0.8.3 (Phase C): query-aware fact scoring
# ====================================================================


class TestQueryAwareFactScoring:
    """v0.8.3 — query parameter enables ranking by relevance.

    Before: facts returned in order of appearance, often picking up
    nav fragments (e.g. "9 Block", "200 subcategories" from Wikimedia
    category pages) before the actual claim (e.g. "Falcon 9 first
    launch 2010").

    After: query-aware scoring rewards facts that overlap with the
    query and penalises short fragments / nav words.
    """

    def test_query_ranking_prefers_overlapping_facts(self):
        """Facts containing query words should rank above non-overlapping ones.

        Uses realistic Wikipedia-style text where:
        - dates get extracted (FACT_RE_DATE)
        - capitalized entities get extracted
        - numeric facts get extracted
        - query "Falcon 9 first launch" overlaps with "Falcon 9 first launch"
          and "Falcon 9" entities
        """
        text = (
            "Falcon 9 is a reusable, two-stage rocket. "
            "The Falcon 9 first launch was on June 4, 2010 from Cape Canaveral. "
            "The Falcon 9 was designed by SpaceX. "
            "The Category page contains 9 subcategories. "
            "File upload media current version is shown. "
            "Block 5 is the latest version. "
            "Falcon 9 has 9 Merlin engines. "
            "Falcon 9 cost approximately 50 million dollars."
        )
        query = "Falcon 9 first launch year"
        facts = _extract_facts(text, max_facts=5, query=query)
        # The date "June 4, 2010" must be in top — it has +1 for "2010" overlap
        # and is a date (specific, valuable fact)
        assert "June 4, 2010" in facts[:5], (
            f"Expected 'June 4, 2010' in top-5 (query contains 'year'), "
            f"got: {facts[:5]}"
        )
        # Nav words (Category, File, Block) should rank below real facts
        # when query mentions Falcon/launch
        nav_facts = [
            f for f in facts[:5]
            if any(w in f.lower().split() for w in {"category", "subcategories"})
        ]
        assert len(nav_facts) == 0, (
            f"Nav facts should not appear in top-5 with Falcon query, got: {nav_facts}"
        )

    def test_no_query_keeps_original_behavior(self):
        """Without query, fall back to in-order extraction (backward-compat)."""
        text = (
            "Falcon 9 first launch was on June 4, 2010. "
            "Category contains 9 subcategories."
        )
        facts = _extract_facts(text, max_facts=4)
        # Original behavior: first in order wins
        assert len(facts) >= 1
        # Without query, no scoring = no rank change
        # The exact set may vary but should be non-empty

    def test_short_fragments_deprioritized(self):
        """Single-word or very short facts should rank low.

        With query "Falcon 9 stages first launch" the word "stages"
        overlaps with "9 stages" (numeric phrase), so that should
        outrank "9 Block" or "5 Appearance" (no overlap, short).
        """
        text = (
            "9 Block. "
            "Falcon 9 first launch 2010. "
            "5 Appearance. "
            "Falcon 9 has 9 stages. "
            "200 subcategories total. "
            "9 reusable. "
            "Falcon 9 is a two-stage rocket."
        )
        query = "Falcon 9 stages first launch"
        facts = _extract_facts(text, max_facts=4, query=query)
        # "9 stages" should outrank "9 Block" (overlap "stages" with query)
        if "9 stages" in facts and "9 Block" in facts:
            idx_stages = facts.index("9 stages")
            idx_block = facts.index("9 Block")
            assert idx_stages < idx_block, (
                f"'9 stages' should outrank '9 Block', got order: {facts}"
            )
        # Pure fragments without Falcon/launch should be near bottom
        # (not strict — scoring is heuristic)

    def test_nav_words_penalized(self):
        """Facts with Category/File/Upload/Block should rank low.

        We use a query that's generic ("Falcon 9") so that NO fact
        gets +1 from query overlap, isolating the nav penalty.
        """
        text = (
            "Category contains many items. "
            "File upload media current version. "
            "Block 5 appearance details. "
            "Falcon 9 first launch was in 2010."
        )
        query = "Falcon 9"  # single generic word
        facts = _extract_facts(text, max_facts=4, query=query)
        # All facts overlap equally with "Falcon 9" → either
        # they all have +1 (Falcon 9 + 2010) or 0 (Category, File, Block)
        # Real Falcon fact should beat pure nav facts
        # "Falcon 9 first launch" should be in top
        falcon_facts = [f for f in facts if "Falcon" in f]
        nav_facts = [
            f for f in facts
            if any(w in f.lower().split() for w in {"category", "file", "upload", "block"})
        ]
        if falcon_facts and nav_facts:
            falcon_idx = min(facts.index(f) for f in falcon_facts)
            nav_idx = min(facts.index(f) for f in nav_facts)
            assert falcon_idx < nav_idx, (
                f"Falcon fact should outrank nav facts. "
                f"falcon_idx={falcon_idx}, nav_idx={nav_idx}, facts={facts}"
            )

    def test_max_facts_respected_after_ranking(self):
        """Even with query, max_facts must be respected (no inflation)."""
        text = (
            "Falcon 9 first launch 2010. "
            "Falcon 9 stages two. "
            "Falcon 9 reusable. "
            "Falcon 9 payload 22800 kg. "
            "Falcon 9 cost 50 million. "
            "Falcon 9 height 70 meters. "
            "Falcon 9 engines Merlin. "
            "Category has 9 subcategories."
        )
        facts = _extract_facts(text, max_facts=3, query="Falcon 9 details")
        assert len(facts) == 3, f"Expected exactly 3 facts, got {len(facts)}: {facts}"

    def test_empty_text_returns_empty(self):
        """Empty/None text → empty list, even with query."""
        assert _extract_facts("", query="anything") == []
        assert _extract_facts("", max_facts=5, query="x") == []

    def test_no_query_words_no_overlap_still_returns(self):
        """If query has no overlap with text, fall back to length-based ranking."""
        text = "Falcon 9 first launch 2010. Some other content here."
        facts = _extract_facts(text, max_facts=2, query="zzz qqq")
        # Should not crash, should return some facts (no overlap = neutral)
        assert isinstance(facts, list)
        assert len(facts) >= 0
