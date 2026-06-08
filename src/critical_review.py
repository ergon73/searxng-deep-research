"""
critical_review.py — Stage 7 финального pipeline: adversarial self-check
ПЕРЕД delivery.

См. audit 2026-06-07, раздел 5 (Stage 7) и 6.7 (critical-review-deepresearch).

Что делает:
  Проверяет synthesis на 5 типов red flags:
    1. Numeric consistency      — кросс-source: одинаковый контекст, разные числа
    2. Entity hallucination     — имена/компании/продукты в claims без grounding
    3. Self-contradiction       — SUPPORTS + REFUTES на тот же claim / topic
    4. Citation integrity       — [N] markers без citation, broken URLs
    5. Temporal consistency     — дата в claim, source старше даты (anachronism)

  Аггрегирует в risk_score ∈ [0, 1] и risk_level (low/medium/high),
  выдаёт recommendations и confidence_adjustment.

Архитектура: pure stdlib, deterministic, no LLM, no network.
  - Один input → один output всегда
  - Использует Synthesis + claims + results + source_candidates
  - Не модифицирует synthesis, только выдаёт ReviewResult

Hard rules (mirror 6.4/6.6):
  - Pure stdlib, no LLM, no network
  - Не выдумывает URLs
  - Не модифицирует synthesis
  - severity weights фиксированы: high=1.0, medium=0.5, low=0.1
  - confidence_adjustment ≤ 0 (только понижает)
  - risk_score ∈ [0, 1]
  - risk_level ∈ {"low", "medium", "high"}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from synthesis import Synthesis, Citation, VERDICT_SUPPORTS, VERDICT_REFUTES


# --- public constants -------------------------------------------------------

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

VALID_SEVERITIES = {SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW}

# Risk weights (sum нормализуется в check_risk_score)
SEVERITY_WEIGHTS: dict[str, float] = {
    SEVERITY_HIGH: 1.0,
    SEVERITY_MEDIUM: 0.5,
    SEVERITY_LOW: 0.1,
}

# Risk level thresholds (inclusive lower bound)
RISK_LEVEL_HIGH_THRESHOLD = 0.7
RISK_LEVEL_MEDIUM_THRESHOLD = 0.3

# Max risk_score denominator (caps at this many "high"-weight flags)
RISK_NORMALIZATION = 3.0

# Max confidence adjustment (multiplier on risk_score)
MAX_CONFIDENCE_ADJUSTMENT = 0.3

# Max flags to return per check (defensive)
MAX_FLAGS_PER_CHECK = 20
MAX_FLAGS_TOTAL = 100


# --- categories -------------------------------------------------------------

CAT_NUMERIC_CONSISTENCY = "numeric_consistency"
CAT_ENTITY_HALLUCINATION = "entity_hallucination"
CAT_SELF_CONTRADICTION = "self_contradiction"
CAT_CITATION_INTEGRITY = "citation_integrity"
CAT_TEMPORAL_CONSISTENCY = "temporal_consistency"

VALID_CATEGORIES = {
    CAT_NUMERIC_CONSISTENCY,
    CAT_ENTITY_HALLUCINATION,
    CAT_SELF_CONTRADICTION,
    CAT_CITATION_INTEGRITY,
    CAT_TEMPORAL_CONSISTENCY,
}


# --- exceptions -------------------------------------------------------------

class CriticalReviewError(Exception):
    """Базовая ошибка critical review."""


# --- dataclasses ------------------------------------------------------------

@dataclass
class ReviewFlag:
    """Один red flag от check'а.
    severity:    high | medium | low
    category:    numeric_consistency | entity_hallucination | etc.
    message:     human-readable описание
    claim:       claim string (если применимо)
    source_urls: list of URLs involved
    """
    severity: str
    category: str
    message: str
    claim: str = ""
    source_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "claim": self.claim,
            "source_urls": self.source_urls,
        }


@dataclass
class ReviewResult:
    """Результат critical review aggregation.
    flags:                 list[ReviewFlag]
    risk_score:            ∈ [0, 1]
    risk_level:            "low" | "medium" | "high"
    recommendations:       list[str] — human-readable suggestions
    confidence_adjustment: float ≤ 0 (delta to apply to synthesis.confidence)
    """
    flags: list[ReviewFlag] = field(default_factory=list)
    risk_score: float = 0.0
    risk_level: str = SEVERITY_LOW
    recommendations: list[str] = field(default_factory=list)
    confidence_adjustment: float = 0.0

    def to_dict(self) -> dict:
        return {
            "flags": [f.to_dict() for f in self.flags],
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "recommendations": self.recommendations,
            "confidence_adjustment": self.confidence_adjustment,
        }


# --- text utilities ---------------------------------------------------------

# Числа в тексте: "5", "123", "1.5", "10K" (но не "2024" как год — для year regex отдельно)
_NUMBER_RE = re.compile(r"\b\d{1,3}(?:[.,]\d+)?(?:[KkMm]|%|\u00d7)?\b")
# 4-digit year (1900-2099)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# ISO date YYYY-MM-DD
_ISO_DATE_RE = re.compile(r"\b(19\d{2}|20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
# Capitalized multi-word entity (имена, компании, продукты)
_ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Zа-яА-Я]+(?:\s+[A-Z][a-zA-Zа-яА-Я]+){0,3})\b")
# Русские capitalized entities
_ENTITY_RU_RE = re.compile(r"\b([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,3})\b")
# Citation marker [N]
_CITATION_MARKER_RE = re.compile(r"\[(\d{1,3})\]")
# URL pattern (для citation integrity)
_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def _tokenize_for_numbers(text: str) -> list[str]:
    """Возвращает список чисел в тексте (строковое представление)."""
    if not text:
        return []
    return _NUMBER_RE.findall(text)


def _extract_entities(text: str) -> set[str]:
    """Извлекает capitalized entities (EN + RU), длиной ≥ 2 words.
    Single-word capitals (типа 'Apple', 'Москва') считаются — это может быть
    компания / город / имя. Filter: длина ≥ 2 chars и не isdigit.
    """
    if not text:
        return set()
    entities: set[str] = set()
    for m in _ENTITY_RE.findall(text):
        if len(m) >= 2 and not m.isdigit():
            entities.add(m)
    for m in _ENTITY_RU_RE.findall(text):
        if len(m) >= 2 and not m.isdigit():
            entities.add(m)
    return entities


def _extract_years(text: str) -> set[int]:
    if not text:
        return set()
    return {int(y) for y in _YEAR_RE.findall(text)}


def _source_text_blob(source: dict) -> str:
    """Собирает весь текст из source dict (text/content/body/snippet)."""
    if not isinstance(source, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "content", "body", "snippet"):
        v = source.get(key)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def _source_url(source: dict) -> str:
    if not isinstance(source, dict):
        return ""
    return source.get("url", "") or ""


# --- Check 1: Numeric consistency -------------------------------------------

def check_numeric_consistency(
    claims: list[str],
    source_candidates: list[dict],
) -> list[ReviewFlag]:
    """Cross-source numeric consistency.

    Логика:
      - Извлекаем числа из каждого claim и каждого source text
      - Группируем claims по "контекстным словам" (слова, отличные от чисел)
      - Если для одной context group claims содержат разные числа, и эти
        числа НЕ появляются в соответствующих source text → medium flag
      - Если разные числа И присутствуют в source text → low flag
        (могут быть разные аспекты — flag, но не medium)
    """
    if not claims:
        return []

    flags: list[ReviewFlag] = []

    # Pre-extract source numbers per source URL
    source_nums: dict[str, set[str]] = {}
    for src in source_candidates or []:
        url = _source_url(src)
        if not url:
            continue
        text = _source_text_blob(src)
        source_nums[url] = set(_tokenize_for_numbers(text))

    # Extract numbers per claim
    claim_nums: list[tuple[str, set[str]]] = []
    for c in claims:
        if not c:
            continue
        claim_nums.append((c, set(_tokenize_for_numbers(c))))

    # Build context groups: 2+ claims with same normalized text (lowercase, no numbers)
    def _normalize_context(text: str) -> str:
        return _NUMBER_RE.sub("", text).lower().strip()

    from collections import defaultdict
    groups: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for c, nums in claim_nums:
        ctx = _normalize_context(c)
        if ctx:  # skip claims без context (только числа)
            groups[ctx].append((c, nums))

    # For each group with multiple claims, check number disagreement
    for ctx, items in groups.items():
        if len(items) < 2:
            continue
        # Собираем уникальные numbers per claim
        all_numbers: set[str] = set()
        claim_to_nums: list[tuple[str, set[str]]] = []
        for c, nums in items:
            if nums:
                all_numbers.update(nums)
                claim_to_nums.append((c, nums))

        if len(all_numbers) < 2:
            continue  # все согласны

        # Disagreement: какие-то числа есть в claims, но не в source text
        unsupported_numbers: set[str] = set()
        for num in all_numbers:
            # num не появляется НИ В ОДНОМ source
            if not any(num in src_nums for src_nums in source_nums.values()):
                unsupported_numbers.add(num)

        if unsupported_numbers:
            flags.append(ReviewFlag(
                severity=SEVERITY_MEDIUM,
                category=CAT_NUMERIC_CONSISTENCY,
                message=(
                    f"Numeric disagreement across related claims: "
                    f"{sorted(all_numbers)} (unsupported by sources: "
                    f"{sorted(unsupported_numbers)})"
                ),
                claim=ctx[:120],
            ))

    return flags[:MAX_FLAGS_PER_CHECK]


# --- Check 2: Entity hallucination ------------------------------------------

def check_entity_hallucination(
    claims: list[str],
    source_candidates: list[dict],
) -> list[ReviewFlag]:
    """Entity hallucination: capitalized entity в claim, которой нет ни в
    одном source.

    Логика:
      - Извлекаем capitalized entities (EN + RU) из claims
      - Извлекаем entities из source text
      - Если entity появляется в claim, но НИГДЕ в source text (case-insensitive) →
        medium flag
      - Если в synthesis уже стоит citation [N] на claim — high flag
        (LLM заявил подтверждение, но source ничего не знает)
    """
    if not claims:
        return []

    flags: list[ReviewFlag] = []

    # Соберём все source entities (lowercased, для case-insensitive match)
    source_entities: set[str] = set()
    for src in source_candidates or []:
        text = _source_text_blob(src)
        for ent in _extract_entities(text):
            source_entities.add(ent.lower())

    # Если source text пустой — пропускаем (нечего проверять)
    if not source_entities:
        return []

    for c in claims:
        if not c:
            continue
        claim_entities = _extract_entities(c)
        hallucinated: list[str] = []
        for ent in claim_entities:
            if ent.lower() not in source_entities:
                hallucinated.append(ent)

        if hallucinated:
            # Severity: medium (general case). Если в claim есть [N] — high.
            severity = SEVERITY_MEDIUM
            if _CITATION_MARKER_RE.search(c):
                severity = SEVERITY_HIGH
            flags.append(ReviewFlag(
                severity=severity,
                category=CAT_ENTITY_HALLUCINATION,
                message=(
                    f"Entity not grounded in any source: "
                    f"{hallucinated[:5]}{'…' if len(hallucinated) > 5 else ''}"
                ),
                claim=c[:120],
            ))

    return flags[:MAX_FLAGS_PER_CHECK]


# --- Check 3: Self-contradiction --------------------------------------------

def check_self_contradiction(
    results: list[dict],
) -> list[ReviewFlag]:
    """Self-contradiction: SUPPORTS + REFUTES на тот же claim / topic.

    Логика:
      - Берём verdict per fact
      - Если для одного fact'а есть и SUPPORTS (supporting_sources) и
        REFUTES (refuting_sources) — CONFLICTING verdict уже есть в
        contradictions, но мы хотим дополнительный флаг.
      - Если для разных fact'ов с похожим контекстом — SUPPORTS в одном и
        REFUTES в другом → high flag (синтез противоречит сам себе)
    """
    if not results:
        return []

    flags: list[ReviewFlag] = []

    # 1) Per-fact: supporting + refuting одновременно
    for r in results:
        verdict = r.get("verdict")
        has_support = bool(r.get("supporting_sources")) or bool(r.get("source_urls"))
        has_refute = bool(r.get("refuting_sources"))
        if verdict == "CONFLICTING" or (has_support and has_refute):
            flags.append(ReviewFlag(
                severity=SEVERITY_HIGH,
                category=CAT_SELF_CONTRADICTION,
                message=(
                    f"Both supporting and refuting sources for same claim: "
                    f"{(r.get('fact') or '')[:80]}"
                ),
                claim=(r.get("fact") or "")[:120],
            ))

    # 2) Cross-fact: SUPPORTS для одного claim, REFUTES для другого claim
    # с похожим context
    def _ctx(text: str) -> str:
        return _NUMBER_RE.sub("", (text or "")).lower().strip()[:80]

    supports: dict[str, str] = {}  # context → claim
    refutes: dict[str, str] = {}
    for r in results:
        ctx = _ctx(r.get("fact", ""))
        if not ctx:
            continue
        verdict = r.get("verdict")
        if verdict == VERDICT_SUPPORTS:
            supports[ctx] = r.get("fact", "")
        elif verdict == VERDICT_REFUTES:
            refutes[ctx] = r.get("fact", "")

    overlap = set(supports.keys()) & set(refutes.keys())
    for ctx in list(overlap)[:MAX_FLAGS_PER_CHECK - len(flags)]:
        flags.append(ReviewFlag(
            severity=SEVERITY_MEDIUM,
            category=CAT_SELF_CONTRADICTION,
            message=(
                f"Cross-claim contradiction on same topic: "
                f"SUPPORTS vs REFUTES"
            ),
            claim=ctx,
        ))

    return flags[:MAX_FLAGS_PER_CHECK]


# --- Check 4: Citation integrity --------------------------------------------

def check_citation_integrity(
    synthesis: Synthesis,
) -> list[ReviewFlag]:
    """Citation integrity в synthesis.answer_markdown.

    Логика:
      - Извлекаем все [N] markers из markdown
      - Каждый N должен быть в range(1, len(citations)+1) → если нет, high flag
      - Каждый citation.url должен быть непуст и не "?" → если "?" или пусто,
        medium flag
      - Каждый citation должен иметь title или url → если оба пустые, low flag
    """
    flags: list[ReviewFlag] = []

    # Citation id range
    valid_ids: set[int] = set()
    for c in synthesis.citations:
        if isinstance(c, Citation):
            valid_ids.add(c.id)

    # 1) [N] markers в markdown
    if synthesis.answer_markdown:
        for m in _CITATION_MARKER_RE.finditer(synthesis.answer_markdown):
            try:
                n = int(m.group(1))
            except ValueError:
                flags.append(ReviewFlag(
                    severity=SEVERITY_HIGH,
                    category=CAT_CITATION_INTEGRITY,
                    message=f"Non-integer citation marker: {m.group(0)}",
                ))
                continue
            if n not in valid_ids:
                flags.append(ReviewFlag(
                    severity=SEVERITY_HIGH,
                    category=CAT_CITATION_INTEGRITY,
                    message=f"Unknown citation id: [{n}] (valid: {sorted(valid_ids)})",
                ))

    # 2) Citation URLs: empty / "?"
    for c in synthesis.citations:
        if not isinstance(c, Citation):
            continue
        if not c.url or c.url == "?":
            flags.append(ReviewFlag(
                severity=SEVERITY_MEDIUM,
                category=CAT_CITATION_INTEGRITY,
                message=f"Citation [{c.id}] has empty URL",
                source_urls=[c.url] if c.url else [],
            ))
        elif not c.title and not c.url:
            flags.append(ReviewFlag(
                severity=SEVERITY_LOW,
                category=CAT_CITATION_INTEGRITY,
                message=f"Citation [{c.id}] has neither title nor url",
            ))

    # 3) URLs в markdown: должен существовать в citations
    if synthesis.answer_markdown:
        citation_urls = {c.url for c in synthesis.citations if isinstance(c, Citation)}
        for m in _URL_RE.finditer(synthesis.answer_markdown):
            url = m.group(0).rstrip(".,;:!?)")
            if url in ("?", ""):
                continue
            if url not in citation_urls:
                flags.append(ReviewFlag(
                    severity=SEVERITY_HIGH,
                    category=CAT_CITATION_INTEGRITY,
                    message=f"URL in markdown not in citation table: {url}",
                ))

    return flags[:MAX_FLAGS_PER_CHECK]


# --- Check 5: Temporal consistency ------------------------------------------

def check_temporal_consistency(
    claims: list[str],
    source_candidates: list[dict],
) -> list[ReviewFlag]:
    """Temporal consistency: год в claim, source старше этого года.

    Логика:
      - Извлекаем years из claims и source text
      - Если source не имеет year, skip (нет данных)
      - Если claim mentions 2024, source max year is 2020 → medium flag
        (источник не может знать про будущее)
      - Если claim mentions 2010, source min year is 2020 → low flag
        (возможно обновлено, но unusual)
    """
    if not claims:
        return []

    flags: list[ReviewFlag] = []

    # Source year ranges
    source_year_info: list[tuple[str, set[int]]] = []
    for src in source_candidates or []:
        url = _source_url(src)
        text = _source_text_blob(src)
        years = _extract_years(text)
        if years:
            source_year_info.append((url, years))

    if not source_year_info:
        return []

    for c in claims:
        if not c:
            continue
        claim_years = _extract_years(c)
        if not claim_years:
            continue

        for url, src_years in source_year_info:
            if not src_years:
                continue
            max_src_year = max(src_years)
            min_src_year = min(src_years)

            # Anachronism: claim year > max source year + 1 (будущее)
            for cy in claim_years:
                if cy > max_src_year + 1:
                    flags.append(ReviewFlag(
                        severity=SEVERITY_MEDIUM,
                        category=CAT_TEMPORAL_CONSISTENCY,
                        message=(
                            f"Claim mentions {cy} but source max year is "
                            f"{max_src_year} (anachronism)"
                        ),
                        claim=c[:120],
                        source_urls=[url] if url else [],
                    ))
                    break  # один flag per (claim, source)
                # Past: claim year << min source year (suspicious)
                elif cy < min_src_year - 5:
                    flags.append(ReviewFlag(
                        severity=SEVERITY_LOW,
                        category=CAT_TEMPORAL_CONSISTENCY,
                        message=(
                            f"Claim mentions {cy} but source min year is "
                            f"{min_src_year} (unusually old)"
                        ),
                        claim=c[:120],
                        source_urls=[url] if url else [],
                    ))
                    break

    return flags[:MAX_FLAGS_PER_CHECK]


# --- Aggregation ------------------------------------------------------------

def _compute_risk_score(flags: list[ReviewFlag]) -> float:
    """risk_score = min(1.0, sum(weights) / RISK_NORMALIZATION)."""
    if not flags:
        return 0.0
    total = sum(SEVERITY_WEIGHTS.get(f.severity, 0.1) for f in flags)
    return round(min(1.0, total / RISK_NORMALIZATION), 4)


def _classify_risk_level(risk_score: float) -> str:
    if risk_score >= RISK_LEVEL_HIGH_THRESHOLD:
        return SEVERITY_HIGH
    if risk_score >= RISK_LEVEL_MEDIUM_THRESHOLD:
        return SEVERITY_MEDIUM
    return SEVERITY_LOW


def _build_recommendations(flags: list[ReviewFlag]) -> list[str]:
    """Human-readable suggestions, deduped."""
    recs: list[str] = []
    seen: set[str] = set()

    # Группируем по category, выдаём общую рекомендацию
    cats = {f.category for f in flags}

    if CAT_NUMERIC_CONSISTENCY in cats:
        rec = "Проверьте числовые расхождения между источниками вручную"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)

    if CAT_ENTITY_HALLUCINATION in cats:
        rec = "Уточните у источников имена/компании, упомянутые в claims"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)

    if CAT_SELF_CONTRADICTION in cats:
        rec = "Синтез противоречит сам себе — пересмотрите логику"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)

    if CAT_CITATION_INTEGRITY in cats:
        rec = "Исправьте broken citations / missing URLs"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)

    if CAT_TEMPORAL_CONSISTENCY in cats:
        rec = "Проверьте временные привязки — anachronism"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)

    # Severity-based: если есть HIGH, общая рекомендация
    if any(f.severity == SEVERITY_HIGH for f in flags):
        rec = "HIGH severity: требуется ручная проверка перед delivery"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)
    elif any(f.severity == SEVERITY_MEDIUM for f in flags):
        rec = "MEDIUM severity: рекомендуется review"
        if rec not in seen:
            recs.append(rec)
            seen.add(rec)

    return recs


def _compute_confidence_adjustment(risk_score: float) -> float:
    """delta ≤ 0: confidence -= risk_score * MAX_CONFIDENCE_ADJUSTMENT."""
    return round(-risk_score * MAX_CONFIDENCE_ADJUSTMENT, 4)


# --- main API ---------------------------------------------------------------

def review(
    synthesis: Synthesis,
    claims: list[str] | None = None,
    results: list[dict] | None = None,
    source_candidates: list[dict] | None = None,
) -> ReviewResult:
    """Critical review: запускает все 5 check'ов, агрегирует.

    Args:
        synthesis: Synthesis (от synthesis.synthesize() или enrich_with_llm())
        claims: list[str] — extracted facts (для numeric/entity/temporal)
        results: list[dict] — VerificationResult per fact (для self-contradiction)
        source_candidates: list[dict] — [{"url", "text", "title"?}]

    Returns:
        ReviewResult с flags, risk_score, risk_level, recommendations,
        confidence_adjustment.

    Pure deterministic, no LLM, no network.
    """
    flags: list[ReviewFlag] = []

    # 1) Numeric consistency (needs claims + sources)
    try:
        flags.extend(check_numeric_consistency(claims or [], source_candidates or []))
    except Exception as e:
        flags.append(ReviewFlag(
            severity=SEVERITY_LOW,
            category=CAT_NUMERIC_CONSISTENCY,
            message=f"check_numeric_consistency failed: {type(e).__name__}: {e}",
        ))

    # 2) Entity hallucination (needs claims + sources)
    try:
        flags.extend(check_entity_hallucination(claims or [], source_candidates or []))
    except Exception as e:
        flags.append(ReviewFlag(
            severity=SEVERITY_LOW,
            category=CAT_ENTITY_HALLUCINATION,
            message=f"check_entity_hallucination failed: {type(e).__name__}: {e}",
        ))

    # 3) Self-contradiction (needs results)
    try:
        flags.extend(check_self_contradiction(results or []))
    except Exception as e:
        flags.append(ReviewFlag(
            severity=SEVERITY_LOW,
            category=CAT_SELF_CONTRADICTION,
            message=f"check_self_contradiction failed: {type(e).__name__}: {e}",
        ))

    # 4) Citation integrity (needs synthesis only)
    try:
        flags.extend(check_citation_integrity(synthesis))
    except Exception as e:
        flags.append(ReviewFlag(
            severity=SEVERITY_LOW,
            category=CAT_CITATION_INTEGRITY,
            message=f"check_citation_integrity failed: {type(e).__name__}: {e}",
        ))

    # 5) Temporal consistency (needs claims + sources)
    try:
        flags.extend(check_temporal_consistency(claims or [], source_candidates or []))
    except Exception as e:
        flags.append(ReviewFlag(
            severity=SEVERITY_LOW,
            category=CAT_TEMPORAL_CONSISTENCY,
            message=f"check_temporal_consistency failed: {type(e).__name__}: {e}",
        ))

    # Cap total
    flags = flags[:MAX_FLAGS_TOTAL]

    # Aggregate
    risk_score = _compute_risk_score(flags)
    risk_level = _classify_risk_level(risk_score)
    recommendations = _build_recommendations(flags)
    confidence_adjustment = _compute_confidence_adjustment(risk_score)

    return ReviewResult(
        flags=flags,
        risk_score=risk_score,
        risk_level=risk_level,
        recommendations=recommendations,
        confidence_adjustment=confidence_adjustment,
    )


# --- public API re-exports --------------------------------------------------

__all__ = [
    "ReviewFlag",
    "ReviewResult",
    "CriticalReviewError",
    "review",
    "check_numeric_consistency",
    "check_entity_hallucination",
    "check_self_contradiction",
    "check_citation_integrity",
    "check_temporal_consistency",
    "SEVERITY_HIGH",
    "SEVERITY_MEDIUM",
    "SEVERITY_LOW",
    "CAT_NUMERIC_CONSISTENCY",
    "CAT_ENTITY_HALLUCINATION",
    "CAT_SELF_CONTRADICTION",
    "CAT_CITATION_INTEGRITY",
    "CAT_TEMPORAL_CONSISTENCY",
]
