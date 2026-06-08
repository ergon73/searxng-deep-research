"""
Query adaptation for deep research pipeline.

Decomposes long or multi-aspect user queries into 1-3 short SearXNG-friendly
search queries (3-10 words each), extracting main entities and composing
query variants.

Spec: ~/.hermes/skills/research/query-adaptation/SKILL.md (v1.0.0)

Designed 2026-06-06 after 33-run eval revealed:
- Median top-1 score dropped from 1.0 (short, 3-8 words) to 0.48 (long, 200-400 words)
- Sub-aspect coverage dropped from 100% to 0% for 4-aspect long queries
- 1/10 long queries returned 0 sources; 3/8 returned Instagram/Facebook as top-1
  for technical questions
"""
from __future__ import annotations

import json
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import Optional

# Reuse entity extraction from hermes_deepresearch
_HERMES_RESEARCH = Path(__file__).parent
sys.path.insert(0, str(_HERMES_RESEARCH))

try:
    from hermes_deepresearch import _extract_facts
    _HAS_HERMES = True
except ImportError:
    _HAS_HERMES = False

# Skill 6.3: retrieval routing. Pure-function classifier, safe to import.
try:
    from routing import classify_intent, should_warn_about_routing
    _HAS_ROUTING = True
except ImportError:
    _HAS_ROUTING = False


# ====================================================================
# Language detection
# ====================================================================

def detect_language(query: str) -> str:
    """Word-level language detection for mixed-script queries.

    Routes by the dominant script per-word, not by global character ratio.
    For 'Gemma 4 12B на русском' (3 Latin words + 1 Russian word), the
    answer is 'en' because most words are Latin.

    Returns 'ru' if ≥50% of alpha-words contain Cyrillic letters, else 'en'.
    For empty/all-non-alpha input, defaults to 'en'.
    """
    # Split on whitespace; keep only words with letters
    words = [w for w in query.split() if any(c.isalpha() for c in w)]
    if not words:
        return 'en'
    cyrillic_words = sum(1 for w in words if any('\u0400' <= c <= '\u04FF' for c in w))
    # Strict majority (>50%): "Gemma 4 12B на русском" has 2 cyrillic of 4
    # alpha-words = 50% exactly, which is ambiguous. We default to 'en'
    # in the tie, since most Russian users writing in mixed-script do
    # so for product codes (English). Only declare 'ru' if cyrillic
    # words are a clear majority.
    return 'ru' if cyrillic_words / len(words) > 0.5 else 'en'


# ====================================================================
# Entity extraction
# ====================================================================

# Pre-process: find candidate entities like "Gemma 4 12B", "iPhone 17 Pro",
# "24GB" (where the trailing unit letter is uppercase, e.g. GB, MB, KB, TB).
# [A-ZА-ЯЁ] поддерживает и латиницу, и кириллицу (re.UNICODE по умолчанию в Python 3).
# FIX 2026-06-07 (e2e Falcon 9): оригинальный regex был [A-Z] only,
# из-за чего все русские capitalized слова (Сколько, Ступеней, Ракеты, Году)
# не извлекались → main_query терял critical content words.
_PRODUCT_ENTITY_RE = re.compile(
    r'\b([A-ZА-ЯЁ][a-zа-яё]{2,}|\d+[A-ZА-ЯЁ]+[a-zа-яё]*|[a-zа-яё]*[A-ZА-ЯЁ][a-zа-яё]+)'  # Capital (Gemma/Сколько), num+unit (24GB, 16MB), or camelCase (iPhone)
    r'(?:\s+\d+[A-ZА-ЯЁ]?(?:\.\d+)?[a-zа-яё]*)*'   # Version-like segments
    r'(?:\s+[A-ZА-ЯЁ][a-zа-яё]{2,})?'              # Optional trailing Capitalized word (Pro, Plus)
    r'(?:\s+\d+[A-ZА-ЯЁ]?)?'                       # Optional trailing version
)


def _extract_candidate_entities(query: str) -> list[str]:
    """Extract candidate entities using product code patterns.

    This catches things like 'Gemma 4 12B', 'iPhone 17 Pro' that the generic
    _extract_facts regex might miss. Filters out stop-word-only candidates
    ('for', 'the', 'want', etc.) to avoid polluting the entity list.
    """
    candidates = []
    for match in _PRODUCT_ENTITY_RE.finditer(query):
        ent = match.group(0).strip()
        # Filter: must have at least one letter, at least 2 chars,
        # and not be all stop words
        if (len(ent) >= 2
                and any(c.isalpha() for c in ent)
                and not all(_is_stop_word(t) for t in ent.split())):
            candidates.append(ent)
    return candidates


def _extract_entities_combined(query: str) -> list[str]:
    """Combine product-code extraction with _extract_facts results.

    Также добавляем lowercase content nouns (RU/EN, ≥ 4 chars, не stop-words)
    для factual queries типа 'Сколько ступеней у ракеты Falcon 9'.
    Без этого top-2 entities (только capitalized) теряют 'ступеней', 'ракеты',
    'году', 'запуск' → main_query обрезается, critical content теряется.
    """
    entities = []

    # 1. Product code candidates (Gemma 4 12B, iPhone 17 Pro, Сколько)
    for e in _extract_candidate_entities(query):
        if e not in entities:
            entities.append(e)

    # 2. From _extract_facts (FACT_RE_ENTITY pattern)
    if _HAS_HERMES:
        try:
            facts = _extract_facts(query)
            for f in facts:
                # _extract_facts returns short facts; filter to noun-phrase-like
                if 1 <= len(f.split()) <= 4 and f not in entities:
                    entities.append(f)
        except Exception:
            pass

    # 3. Lowercase content nouns (FIX 2026-06-07 e2e Falcon 9).
    # Без этого русские factual queries теряют 'ступеней', 'ракеты', 'году'.
    for noun in _extract_content_nouns(query):
        if noun not in entities and len(entities) < 12:
            entities.append(noun)

    return entities


# Lowercase content noun extraction (substantive words, не stop-words)
_CONTENT_NOUN_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}")

# Words которые встречаются в factual queries, но не несут topic-meaning.
# Дополнение к _STOP_WORDS (которые слишком агрессивно вырезают).
_CONTENT_NOUN_BLACKLIST = {
    # Question words
    "сколько", "когда", "где", "кто", "что", "какой", "какая", "какое", "какие",
    "почему", "зачем", "как", "чем", "каким", "какую",
    # Verbs / auxiliary
    "есть", "было", "была", "были", "будет", "будут", "является",
    # Prepositions / particles
    "также", "пожалуйста", "вообще", "именно",
    # Демонстративы / указатели
    "этот", "эта", "это", "эти", "тот", "та", "те",
    # "Сейчас" / time refs
    "сейчас", "сегодня", "вчера", "завтра", "недавно", "давно",
}


def _extract_content_nouns(query: str) -> list[str]:
    """Extract lowercase content nouns (≥ 4 chars, non-stop, non-question).

    For "Сколько ступеней у ракеты Falcon 9 и в каком году первый запуск":
      → ['ступеней', 'ракеты', 'запуск']  (skip: сколько, году — question/year)
    """
    if not query:
        return []
    nouns: list[str] = []
    for m in _CONTENT_NOUN_RE.finditer(query):
        word = m.group(0).lower()
        if word in _STOP_WORDS:
            continue
        if word in _CONTENT_NOUN_BLACKLIST:
            continue
        if word in nouns:
            continue
        nouns.append(word)
    return nouns


# ====================================================================
# Scoring
# ====================================================================

_STOP_WORDS = {
    # English
    "the", "a", "an", "and", "or", "but", "for", "of", "to", "in", "on",
    "is", "are", "was", "were", "be", "been", "this", "that", "it", "i",
    "you", "we", "they", "my", "your", "our", "their", "with", "from",
    "as", "by", "at", "do", "does", "did", "have", "has", "had", "can",
    "could", "would", "should", "will", "may", "might", "must", "need",
    "want", "know", "tell", "explain", "understand", "looking", "find",
    "some", "any", "all", "how", "what", "why", "when", "where", "which",
    "more", "most", "best", "good", "need", "see", "use", "using", "used",
    # Russian
    "и", "в", "на", "с", "по", "для", "не", "что", "это", "как", "или",
    "из", "за", "к", "у", "о", "от", "до", "при", "так", "его", "её",
    "их", "мы", "вы", "он", "она", "они", "быть", "был", "была", "было",
    "мне", "тебе", "нам", "вам", "есть", "нет", "да", "могу", "может",
    "хочу", "надо", "нужно", "можно", "также", "ещё", "еще", "которые",
    "этот", "эта", "эти", "тот", "та", "те", "который", "которая",
}


def _is_stop_word(token: str) -> bool:
    return token.lower() in _STOP_WORDS


def _score_entity(entity: str, query: str) -> float:
    """Score an entity by frequency, position, and capitalization.

    Returns a float score; higher = more salient.
    """
    score = 0.0
    query_lower = query.lower()
    entity_lower = entity.lower()

    # Frequency (count occurrences in query)
    score += query_lower.count(entity_lower) * 2.0

    # Position weight (earlier = more important)
    pos = query_lower.find(entity_lower)
    if pos >= 0:
        third = len(query) / 3
        if pos < third:
            score += 3.0
        elif pos < 2 * third:
            score += 2.0
        else:
            score += 1.0

    # Capitalization bonus (proper noun)
    if entity[0].isupper():
        score += 1.0

    # Penalty for being mostly stop words
    tokens = entity.split()
    if tokens and all(_is_stop_word(t) for t in tokens):
        score -= 5.0

    # Bonus for product code (has both letters and numbers)
    has_letters = any(c.isalpha() for c in entity)
    has_numbers = any(c.isdigit() for c in entity)
    if has_letters and has_numbers:
        score += 2.0

    return score


# Narrative-noise filter (skill 6.1: search-intent-confirmation).
# Filters out phrases that look like entities by `_extract_candidate_entities`
# or `_extract_facts` but are actually user-intro filler or context, not
# search topics. Without this filter, "5 человек", "Расскажи подробно",
# "Specifically" bubble up to main_query and corrupt the search plan.
#
# See audit 2026-06-07, section 5, P0/P1: narrative noise.
_NARRATIVE_INTRO_WORDS_RU = {
    "расскажи", "подробно", "опиши", "опишите", "сравни", "сравните",
    "найди", "найдите", "покажи", "покажите", "хочу", "хотел", "хотела",
    "хотелось", "можешь", "может", "давай", "давайте",
    "про", "об",  # prepositions that always signal context, not topic
}
_NARRATIVE_INTRO_WORDS_EN = {
    "specifically", "generally", "basically", "actually", "really",
    "please", "kindly", "describe", "explain", "tell", "show", "find",
    "want", "wanted", "looking", "give", "gives", "overview",
}
_NARRATIVE_INTRO_WORDS = _NARRATIVE_INTRO_WORDS_RU | _NARRATIVE_INTRO_WORDS_EN

# Units / words that make "<number> <unit>" a narrative context, not a
# search target. Hardware specs like "24GB GPU" are NOT in this list.
_NARRATIVE_SIZE_UNITS = {
    # Russian
    "человек", "людей", "люди", "месяц", "месяца", "месяцев",
    "неделя", "недели", "недель", "год", "года", "лет",
    "день", "дня", "дней", "час", "часа", "часов",
    "минут", "минута", "минуты",
    "вариант", "варианта", "вариантов", "способ", "способа", "способов",
    "пункт", "пункта", "пунктов", "пример", "примера", "примеров",
    "проект", "проекта", "проектов", "задач", "задача", "задачи",
    "шаг", "шага", "шагов", "этап", "этапа", "этапов",
    "раз", "раза", "раз", "штук", "штука", "штуки",
    # English — generic time / count / size words that signal project
    # context, not search topic.
    "days", "day", "weeks", "week", "months", "month", "years", "year",
    "hours", "hour", "minutes", "minute", "seconds", "second",
    "times", "items", "units", "steps", "lines", "examples",
    "people", "person", "users", "user", "customers", "customer",
    "projects", "project", "tasks", "task", "stories", "story",
    "options", "option", "ways", "way", "methods", "method",
    "points", "point", "things", "thing",
}
# Phrase patterns: "5 человек", "3 месяца", "2 недели", "10 пунктов".
# Detected by "<digits><space><size-unit>".
_NARRATIVE_NUMERIC_PHRASE_RE = re.compile(
    r"^\d+\s+(?:" + "|".join(sorted(_NARRATIVE_SIZE_UNITS)) + r")$",
    re.IGNORECASE,
)


def _is_narrative_intro_phrase(entity: str) -> bool:
    """True if the entity is a single intro / filler word that should
    not be a search topic.

    Examples: 'Specifically', 'Расскажи', 'Подробно', 'Найди'.

    Also matches multi-token entities where the MAJORITY of tokens are
    intro / context words, e.g. 'Расскажи подробно про мобильное' (3/4 are
    intro: расскажи, подробно, про). These bubble up from
    `_extract_facts` because the regex is greedy on Capitalized starts
    or digit-then-word runs.
    """
    tokens = [t.lower().strip(".,!?;:") for t in entity.split() if t.strip()]
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in _NARRATIVE_INTRO_WORDS
    # Multi-token: count what fraction is intro / context words.
    intro_count = sum(1 for t in tokens if t in _NARRATIVE_INTRO_WORDS)
    # If >= 50% of tokens are intro words, the phrase is mostly filler.
    if intro_count / len(tokens) >= 0.5:
        return True
    return False


def _is_narrative_numeric_phrase(entity: str) -> bool:
    """True if the entity is a '<digits> <size-unit>' context phrase.

    Examples: '5 человек', '3 месяца', '2 недели', '10 пунктов'.
    The number+unit is a project context, not a search topic.
    Hardware specs (24GB) and version numbers (v12) are NOT matched.
    """
    return bool(_NARRATIVE_NUMERIC_PHRASE_RE.match(entity.strip()))


def _is_narrative_entity(entity: str) -> bool:
    """Master filter: True if the entity is narrative noise, not a topic."""
    if _is_narrative_intro_phrase(entity):
        return True
    if _is_narrative_numeric_phrase(entity):
        return True
    return False


# ====================================================================
# Composition
# ====================================================================

# URLs to strip from main_query (adversarial: skill 6.2).
# URLs in queries are references, not search topics themselves.
_URL_IN_TEXT_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _strip_urls(text: str) -> str:
    """Remove URLs from a text string, collapse whitespace."""
    if not text:
        return text
    cleaned = _URL_IN_TEXT_RE.sub("", text)
    # Collapse multiple spaces from removed URLs
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _compose_main_query(scored_entities: list[tuple[str, float]],
                        max_words: int = 8) -> str:
    """Compose main_query from top-scored entities.

    FIX 2026-06-07 (e2e Falcon 9): top-2 слишком узкое для factual queries.
    Берём top-3 entities + добавляем content nouns пока не дойдём до 5-6 слов
    или не исчерпаем candidates. Это устраняет dropped_critical_terms
    для типичных RU factual queries ('Сколько ступеней у ракеты Falcon 9').
    """
    if not scored_entities:
        return ""

    # Take top 3 entities (раньше top-2)
    top_three = [e for e, _ in scored_entities[:3]]
    main = " ".join(top_three)
    # Strip URLs — they are references, not search topics (skill 6.2).
    main = _strip_urls(main)
    if not main:
        return ""
    words = main.split()

    if len(words) > max_words:
        words = words[:max_words]

    return " ".join(words)


def _compose_alt_queries(scored_entities: list[tuple[str, float]],
                         main_query: str,
                         original_query: str,
                         max_alts: int = 3) -> list[str]:
    """Compose 1-3 alt queries from entity pairs (orthogonal angles)."""
    alts = []
    entities = [e for e, _ in scored_entities]

    # Take top 3 entities; try pairs (2-combinations) of these
    top_3 = entities[:3]
    for e1, e2 in combinations(top_3, 2):
        if e1 == e2:
            continue
        candidate = f"{e1} {e2}"
        if candidate != main_query and candidate not in alts:
            alts.append(candidate)
            if len(alts) == max_alts:
                break

    return alts


# ====================================================================
# LLM fallback (lazy import to avoid heavy dep when not needed)
# ====================================================================

_LLM_IMPORT_TRIED = False
_LLM_AVAILABLE = None


def _try_llm_call(query: str) -> Optional[dict]:
    """Try LLM fallback. Returns None if LLM unavailable or fails."""
    global _LLM_IMPORT_TRIED, _LLM_AVAILABLE

    if not _LLM_IMPORT_TRIED:
        _LLM_IMPORT_TRIED = True
        try:
            # Reuse the LLM verifier infrastructure
            from hermes_deepresearch import _get_llm_verifier
            _LLM_AVAILABLE = _get_llm_verifier
        except (ImportError, Exception):
            _LLM_AVAILABLE = None

    if _LLM_AVAILABLE is None:
        return None

    # LLM call is intentionally NOT implemented in v1.0.0 — falls back to
    # raw prefix if deterministic fails. The skill spec lists LLM as
    # 'future enhancement'. See SKILL.md Algorithm step 10.
    return None


# ====================================================================
# Main entry point
# ====================================================================

def adapt_query(query: str) -> dict:
    """Adapt a user query for the deep research pipeline.

    Args:
        query: The raw user query (any length, any language).

    Returns:
        A dict with keys:
            main_query (str): 3-8 words, SearXNG-friendly
            alt_queries (list[str]): 0-3 orthogonal angle queries, each 3-10 words
            language (str): 'ru' or 'en'
            extracted_entities (list[str]): 1-5 noun phrases
            rationale (str): human-readable explanation
            adaptation_method (str): 'passthrough' | 'deterministic' | 'llm_fallback'
    """
    # 1. Passthrough for short queries
    word_count = len(query.split())
    if word_count <= 10:
        return _enrich_with_confirmation({
            "raw_query": query,  # used by build_search_plan_preview()
            "main_query": query.strip(),
            "alt_queries": [],
            "language": detect_language(query),
            "extracted_entities": [],
            "rationale": f"Query is short ({word_count} words), no adaptation needed",
            "adaptation_method": "passthrough",
        }, query)

    # 2. Extract entities
    entities = _extract_entities_combined(query)

    # 2. Score entities
    scored = [(e, _score_entity(e, query)) for e in entities]
    # Filter out entities with non-positive scores
    scored = [(e, s) for e, s in scored if s > 0]
    # Filter out stop-word-only entities (e.g. "I'm", "we", "I")
    scored = [(e, s) for e, s in scored if not all(_is_stop_word(t) for t in e.split())]
    # Filter out narrative-noise entities (skill 6.1).
    # Examples: "5 человек", "Расскажи подробно", "Specifically".
    # These are user-intro/context fillers, not search topics.
    scored = [(e, s) for e, s in scored if not _is_narrative_entity(e)]
    # Sort by score descending
    scored.sort(key=lambda x: -x[1])
    # Keep top 10 (FIX 2026-06-07: было 5→8→10 для длинных RU factual queries).
    # 10 entities покрывает: 'Falcon 9' + 'Сколько' + 'Сколько ступеней' +
    # 'ступеней' + 'ракеты' + 'falcon' + 'каком' + 'году' + 'первый' + 'запуск'
    # = 10 entities, все critical content nouns на месте.
    scored = scored[:10]
    entities_kept = [e for e, _ in scored]

    # 4. If 0 viable entities, fall back to raw prefix
    if len(entities_kept) == 0:
        llm_result = _try_llm_call(query)
        if llm_result is not None:
            llm_result = _enrich_with_confirmation(llm_result, query)
            llm_result.setdefault("raw_query", query)
            return llm_result
        # Graceful fallback: take first 8 words of query, stripping URLs.
        first_words = " ".join(query.split()[:8])
        first_words = _strip_urls(first_words)
        if not first_words:
            # All first 8 words were URLs — use remaining content
            first_words = _strip_urls(query)
        # Take first 8 words again after URL-strip
        first_words = " ".join(first_words.split()[:8])
        return _enrich_with_confirmation({
            "raw_query": query,
            "main_query": first_words,
            "alt_queries": [],
            "language": detect_language(query),
            "extracted_entities": entities_kept,
            "rationale": (
                f"Deterministic extraction found 0 entities; "
                f"used raw query prefix"
            ),
            "adaptation_method": "deterministic",  # degraded, not llm
        }, query)

    # 5. Compose main_query (works for 1+ entities)
    main_query = _compose_main_query(scored)

    # 6. Compose alt_queries
    alt_queries = _compose_alt_queries(scored, main_query, query)

    # 7. Filter alt_queries by length
    alt_queries = [a for a in alt_queries if 1 <= len(a.split()) <= 10]
    # Cap at 3
    alt_queries = alt_queries[:3]

    return _enrich_with_confirmation({
        "raw_query": query,
        "main_query": main_query,
        "alt_queries": alt_queries,
        "language": detect_language(query),
        "extracted_entities": entities_kept,
        "rationale": (
            f"Extracted {len(entities_kept)} entities from {word_count}-word query; "
            f"composed main_query from top 2 by score"
        ),
        "adaptation_method": "deterministic",
    }, query)


# ====================================================================
# Search-intent confirmation (skill 6.1)
# ====================================================================
# Adds five fields to the result of adapt_query():
#   - adaptation_confidence: float (0.0-1.0)
#   - dropped_terms: list[str]
#   - added_terms: list[str]
#   - needs_confirmation: bool
#   - confirmation_reason: list[str]
#
# Plus a public function build_search_plan_preview(adapted) -> str
# that renders a human-readable preview for the chat.
#
# Spec: ~/.hermes/skills/research/search-intent-confirmation/SKILL.md
# ====================================================================

# Hard cap from acceptance criteria #2 of the skill spec
_CONFIRMATION_LONG_QUERY_WORDS = 40

# Confidence thresholds: < this triggers needs_confirmation
_CONFIRMATION_CONFIDENCE_FLOOR = 0.75

# Heuristics for added/dropped term extraction
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


def _tokenize(s: str) -> list[str]:
    """Tokenize a string into lowercase word tokens (letters/digits)."""
    return [t.lower() for t in _WORD_RE.findall(s)]


def _compute_diff_terms(raw_query: str,
                        main_query: str,
                        extracted_entities: list[str]) -> tuple[list[str], list[str]]:
    """Compute added and dropped terms between raw and adapted main_query.

    dropped_terms: content tokens present in raw_query but not in main_query.
                    Used to warn the user about information we discarded.
    added_terms:   content tokens present in main_query but not in raw_query.
                    These are 'fabricated' or inferred — high-risk to surface.

    Stop words are excluded from both sides to avoid noise.
    """
    raw_tokens = set(_tokenize(raw_query))
    adapted_tokens = set(_tokenize(main_query))
    # Also pull in entities (which may differ from main_query)
    for e in extracted_entities:
        adapted_tokens.update(_tokenize(e))

    # Drop stop words from each side
    raw_content = {t for t in raw_tokens if not _is_stop_word(t)}
    adapted_content = {t for t in adapted_tokens if not _is_stop_word(t)}

    dropped = sorted(raw_content - adapted_content)
    added = sorted(adapted_content - raw_content)
    return dropped, added


def _compute_adaptation_confidence(adaptation_method: str,
                                   raw_query: str,
                                   main_query: str,
                                   entities_kept: list[str],
                                   extracted_entities_raw: list[str] | None = None) -> float:
    """Estimate confidence in the adaptation.

    Returns 0.0-1.0. Lower when:
    - method is fallback (degraded deterministic)
    - main_query diverges from raw (many added/dropped terms)
    - few or zero entities survived
    """
    # extracted_entities_raw is accepted but unused for backward compat
    del extracted_entities_raw

    if adaptation_method == "passthrough":
        # Raw passthrough: no rewrite happened, very high confidence
        return 0.95

    if adaptation_method == "llm_fallback":
        # LLM is opaque; we trust it more than raw-prefix fallback
        return 0.80

    # deterministic
    n_entities = len(entities_kept)
    raw_tokens = _tokenize(raw_query)
    main_tokens = _tokenize(main_query)
    raw_content = {t for t in raw_tokens if not _is_stop_word(t)}
    main_content = {t for t in main_tokens if not _is_stop_word(t)}
    if raw_content:
        overlap = len(raw_content & main_content) / max(len(raw_content), 1)
    else:
        overlap = 1.0

    if n_entities == 0:
        # Degraded path: fell back to raw prefix. Low confidence.
        return 0.40
    if n_entities >= 3 and overlap >= 0.5:
        return 0.90
    if n_entities >= 2 and overlap >= 0.3:
        return 0.80
    # 1 entity or low overlap
    return 0.65


def _check_confirmation_triggers(
    raw_query: str,
    main_query: str,
    extracted_entities: list[str],
    added_terms: list[str],
    dropped_terms: list[str],
    adaptation_confidence: float,
    adaptation_method: str,
) -> list[str]:
    """Return list of human-readable reasons why confirmation is needed.

    Empty list = no confirmation needed. Each entry is a short string
    that can be shown to the user or logged.
    """
    reasons: list[str] = []

    word_count = len(raw_query.split())

    # Trigger 1: long multi-aspect query
    if word_count > _CONFIRMATION_LONG_QUERY_WORDS:
        reasons.append(
            f"long_query:{word_count}_words"
        )

    # Trigger 2: zero entities (degraded deterministic)
    if adaptation_method == "deterministic" and len(extracted_entities) == 0:
        reasons.append("zero_entities_extracted")

    # Trigger 3: main_query contains a word that wasn't in raw query
    if added_terms:
        reasons.append(f"added_terms:{','.join(added_terms)}")

    # Trigger 4: main_query lost a critical named entity.
    # Heuristic: if any non-stopword content token from raw_query
    # is missing from main_query AND its length >= 3, treat as dropped critical.
    # Stop words are excluded by _compute_diff_terms already.
    if dropped_terms:
        # Filter to "critical" dropped: length >= 3 (skip tiny noise like "vs")
        critical = [t for t in dropped_terms if len(t) >= 3]
        if critical:
            reasons.append(
                f"dropped_critical_terms:{','.join(critical[:5])}"
            )

    # Trigger 5: low confidence
    if adaptation_confidence < _CONFIRMATION_CONFIDENCE_FLOOR:
        reasons.append(
            f"low_confidence:{adaptation_confidence:.2f}"
        )

    # Trigger 6: time_range/category inferred (NOT YET IMPLEMENTED in v1.0.1).
    # Phase C (retrieval-routing) will populate these fields. Until then,
    # we skip this trigger to avoid false positives on every long query.
    # See: ~/.hermes/skills/research/search-intent-confirmation/SKILL.md
    # § "Known limitations".

    return reasons


def _enrich_with_confirmation(result: dict, raw_query: str) -> dict:
    """Add the five confirmation fields + routing recommendations.

    Mutates and returns the same dict. Pure-side-effect function for
    backward compatibility with the three return sites in adapt_query().

    Adds 5 fields (skill 6.1):
        - adaptation_confidence, dropped_terms, added_terms,
          needs_confirmation, confirmation_reason
    Plus 5 fields (skill 6.3, retrieval-routing):
        - inferred_route, routing_confidence, suggested_engines,
          suggested_categories, suggested_time_range, query_variants,
          routing_warning
    """
    main_query = result.get("main_query", "")
    entities = result.get("extracted_entities", []) or []
    method = result.get("adaptation_method", "deterministic")

    dropped, added = _compute_diff_terms(raw_query, main_query, entities)
    confidence = _compute_adaptation_confidence(
        adaptation_method=method,
        raw_query=raw_query,
        main_query=main_query,
        entities_kept=entities,
    )
    reasons = _check_confirmation_triggers(
        raw_query=raw_query,
        main_query=main_query,
        extracted_entities=entities,
        added_terms=added,
        dropped_terms=dropped,
        adaptation_confidence=confidence,
        adaptation_method=method,
    )

    result["dropped_terms"] = dropped
    result["added_terms"] = added
    result["adaptation_confidence"] = confidence
    result["needs_confirmation"] = bool(reasons)
    result["confirmation_reason"] = reasons

    # Skill 6.3: retrieval routing (advisory only — caller decides).
    if _HAS_ROUTING:
        intent = classify_intent(raw_query)
        result["inferred_route"] = intent.route
        result["routing_confidence"] = intent.confidence
        result["suggested_engines"] = intent.engines
        result["suggested_categories"] = intent.categories
        result["suggested_time_range"] = intent.time_range
        result["query_variants"] = intent.query_variants
        result["all_routes"] = intent.all_routes
        result["routing_warning"] = should_warn_about_routing(intent)
    else:
        result["inferred_route"] = "general"
        result["routing_confidence"] = 0.0
        result["suggested_engines"] = None
        result["suggested_categories"] = None
        result["suggested_time_range"] = None
        result["query_variants"] = []
        result["all_routes"] = []
        result["routing_warning"] = False

    return result


def build_search_plan_preview(adapted: dict) -> str:
    """Render a human-readable preview of the search plan.

    Suitable for sending to the user before invoking SearXNG. Shows:
    - original raw query
    - main_query and alt_queries
    - language
    - extracted entities
    - confidence + needs_confirmation flag with reasons
    - dropped/added terms (the high-risk bits)

    Pure function: does not touch network or files. Safe to call in
    dry-run / preview contexts.
    """
    raw = adapted.get("raw_query", "(missing)")
    main = adapted.get("main_query", "")
    alts = adapted.get("alt_queries", []) or []
    lang = adapted.get("language", "en")
    entities = adapted.get("extracted_entities", []) or []
    confidence = adapted.get("adaptation_confidence")
    needs = adapted.get("needs_confirmation", False)
    reasons = adapted.get("confirmation_reason", []) or []
    dropped = adapted.get("dropped_terms", []) or []
    added = adapted.get("added_terms", []) or []
    method = adapted.get("adaptation_method", "deterministic")

    lines: list[str] = []
    lines.append("Я понял задачу так:")
    lines.append("")
    lines.append(f"**Цель:** найти источники по запросу пользователя.")
    lines.append(f"**Исходный запрос:** {raw}")
    lines.append("")
    lines.append(f"**Основной запрос:** {main or '(пусто)'}")
    if alts:
        lines.append("")
        lines.append("**Дополнительные запросы:**")
        for i, a in enumerate(alts, 1):
            lines.append(f"{i}. {a}")
    lines.append("")
    lines.append("**Параметры поиска:**")
    lines.append(f"- language: {lang}")
    lines.append(f"- adaptation_method: {method}")
    if confidence is not None:
        lines.append(f"- adaptation_confidence: {confidence:.2f}")
    if entities:
        lines.append(f"- extracted_entities: {entities}")
    if dropped:
        # Show only the first ~5 to keep preview short
        preview_dropped = dropped[:5]
        more = f" (+{len(dropped) - 5})" if len(dropped) > 5 else ""
        lines.append("")
        lines.append(f"**Что было отброшено из исходного запроса:** {', '.join(preview_dropped)}{more}")
    if added:
        preview_added = added[:5]
        more = f" (+{len(added) - 5})" if len(added) > 5 else ""
        lines.append("")
        lines.append(f"**Что было добавлено (нет в исходном запросе):** {', '.join(preview_added)}{more}")

    # Skill 6.3: routing recommendations (advisory)
    route = adapted.get("inferred_route")
    rconf = adapted.get("routing_confidence")
    rengines = adapted.get("suggested_engines")
    rcats = adapted.get("suggested_categories")
    rtime = adapted.get("suggested_time_range")
    rvariants = adapted.get("query_variants") or []
    rwarning = adapted.get("routing_warning", False)
    if route and route != "general":
        lines.append("")
        lines.append("**Рекомендованный маршрут поиска (advisory):**")
        lines.append(f"- inferred_route: {route}")
        if rconf is not None:
            lines.append(f"- routing_confidence: {rconf:.2f}")
        if rengines:
            lines.append(f"- suggested_engines: {rengines}")
        if rcats:
            lines.append(f"- suggested_categories: {rcats}")
        if rtime:
            lines.append(f"- suggested_time_range: {rtime}")
        if rvariants:
            lines.append("- query_variants:")
            for v in rvariants[:4]:
                lines.append(f"  - {v}")
        if rwarning:
            lines.append("- routing_warning: да (рекомендую подтвердить маршрут)")

    if needs:
        lines.append("")
        lines.append(f"**Требуется подтверждение:** да")
        lines.append(f"**Причины:** {'; '.join(reasons)}")
        lines.append("")
        lines.append("Действия:")
        lines.append("1. APPROVE_SEARCH_PLAN")
        lines.append("2. SEARCH_RAW_QUERY")
        lines.append("3. EDIT_QUERY_PLAN: <новые параметры>")
    else:
        lines.append("")
        lines.append(f"**Требуется подтверждение:** нет (риск низкий, ищу автоматически).")

    return "\n".join(lines)


# ====================================================================
# Convenience: standalone CLI for testing
# ====================================================================

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("Usage: python query_adaptation.py '<query>'")
        _sys.exit(1)
    q = " ".join(_sys.argv[1:])
    result = adapt_query(q)
    print(json.dumps(result, ensure_ascii=False, indent=2))
