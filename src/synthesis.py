"""
synthesis.py — Stage 6 финального pipeline: deterministic synthesis layer с
опциональным LLM enrich + post-validation + automatic fallback.

См. audit 2026-06-07, раздел 5 (Stage 6) и 6.6 (synthesis-with-citations).

Архитектура:
  - synthesize():          deterministic builder (без LLM, pure stdlib)
  - enrich_with_llm():     optional LLM layer, post-validate, fallback на deterministic

Формат входа (от LLMVerifier.verify_facts_batch / verify_claim_against_evidence
или от hermes_deepresearch.verify_sources()):

  query:               str  — original user query
  claims:              list[str]  — extracted facts
  results:             list[dict]  — VerificationResult per fact
                        каждый dict имеет:
                          - fact: str
                          - verdict: "SUPPORTS" | "REFUTES" | "INSUFFICIENT"
                                     | "CONFLICTING" | "NUMERIC_MISMATCH" | None
                          - reasoning: str
                          - source_urls: list[str] (опционально)
                          - supporting_sources / refuting_sources /
                            numeric_mismatch_sources: list[(url, score, method)]
                            (опционально, приходят из hermes_deepresearch)
  source_candidates:   list[dict]  — [{"url": str, "text": str, "title"?: str, ...}]

Формат выхода:
  Synthesis(
    answer_markdown: str,         # финальный markdown с inline [1][2]
    citations: list[Citation],    # таблица цитат
    coverage: dict,               # {supported, total, score, unsupported[]}
    contradictions: list[dict],   # [{fact, urls[], type}]
    confidence: float,            # ∈ [0, 1]
    open_questions: list[str],    # вопросы без ответа
  )

Hard rules (из audit 6.6 + 6.8 prompt-injection defense):
  - Citations берутся ТОЛЬКО из source_candidates (никаких выдуманных URL)
  - quote ≤ MAX_QUOTE_CHARS (200) — защита от prompt injection
  - confidence ∈ [0, 1]
  - Markdown escape для special chars в claims
  - LLM failure / timeout / invalid output → automatic fallback to deterministic
  - post-validate LLM markdown: все [N] ссылки и URLs должны быть в original
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- public constants -------------------------------------------------------

# Verdict types (mirror hermes_deepresearch.LLMVerifier)
VERDICT_SUPPORTS = "SUPPORTS"
VERDICT_REFUTES = "REFUTES"
VERDICT_INSUFFICIENT = "INSUFFICIENT"
VERDICT_CONFLICTING = "CONFLICTING"
VERDICT_NUMERIC_MISMATCH = "NUMERIC_MISMATCH"

VERDICTS_POSITIVE = {VERDICT_SUPPORTS}
VERDICTS_NEGATIVE = {VERDICT_REFUTES}
VERDICTS_NEUTRAL = {VERDICT_INSUFFICIENT, None}
VERDICTS_CONFLICT = {VERDICT_CONFLICTING, VERDICT_NUMERIC_MISMATCH}

# Hard limits (prompt-injection defense)
MAX_QUOTE_CHARS = 200
MAX_MARKDOWN_CHARS = 60_000
MAX_CITATIONS = 50  # верхний предел citation table
MAX_OPEN_QUESTIONS = 20
MAX_CLAIM_PREVIEW_CHARS = 200  # в Coverage / Open Questions


# --- exceptions -------------------------------------------------------------


class SynthesisError(Exception):
    """Базовая ошибка synthesis layer."""


# --- dataclasses ------------------------------------------------------------


@dataclass
class Citation:
    """Один entry в citation table.

    id:        стабильный integer 1..N, используется в markdown как [N]
    url:       URL источника (всегда из source_candidates, never fabricated)
    title:     title (если есть) или производный от URL
    quote:     короткий excerpt из source text (≤ MAX_QUOTE_CHARS)
    source_index: оригинальный индекс в source_candidates (для traceability)
    """

    id: int
    url: str
    title: str
    quote: str
    source_index: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "quote": self.quote,
        }


@dataclass
class Synthesis:
    """Финальный structured output synthesis layer."""

    answer_markdown: str
    citations: list[Citation] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    contradictions: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    open_questions: list[str] = field(default_factory=list)

    # meta — не для пользователя, но полезно для debugging / audit
    enriched_by_llm: bool = False
    llm_fallback_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "answer_markdown": self.answer_markdown,
            "citations": [c.to_dict() for c in self.citations],
            "coverage": self.coverage,
            "contradictions": self.contradictions,
            "confidence": self.confidence,
            "open_questions": self.open_questions,
            "enriched_by_llm": self.enriched_by_llm,
            "llm_fallback_reason": self.llm_fallback_reason,
        }


# --- markdown utilities ----------------------------------------------------

# Эти символы в claims могут сломать markdown структуру, если их не escape'ить
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>])")


def _md_escape(text: str) -> str:
    """Escape markdown special chars в text. Безопасно для inline insertion."""
    if not text:
        return ""
    # Не escape'им backslash первым (избегаем double-escape)
    return _MD_SPECIAL.sub(r"\\\1", text)


def _truncate(text: str, limit: int) -> str:
    """Truncate text до ≤ limit chars (включая ellipsis).

    Режет по границе последнего пробела и добавляет '…' (1 char).
    Гарантия: len(result) <= limit.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    # Резервируем 1 char под ellipsis
    budget = limit - 1
    if budget <= 0:
        return "…"
    cut = text[:budget]
    # По границе последнего пробела, чтобы не рвать слово
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _url_to_title(url: str) -> str:
    """Best-effort title из URL: domain + path tail."""
    if not url or url == "?":
        return "(no url)"
    try:
        m = re.match(r"https?://([^/]+)(/[^?#]*)?", url)
        if not m:
            return url[:60]
        domain = m.group(1)
        path = m.group(2) or "/"
        # Берём последний осмысленный кусок пути
        parts = [p for p in path.split("/") if p and len(p) > 2]
        tail = parts[-1] if parts else ""
        if tail:
            return f"{domain} — {tail[:40]}"
        return domain
    except Exception:
        return url[:60]


def _extract_quote(source: dict, max_chars: int = MAX_QUOTE_CHARS) -> str:
    """Извлекает короткий excerpt из source text.

    Priority:
      1. snippet field
      2. content field
      3. text field
      4. first max_chars of any text-like field
    """
    if not isinstance(source, dict):
        return ""
    for key in ("snippet", "quote", "excerpt"):
        v = source.get(key)
        if isinstance(v, str) and v.strip():
            return _truncate(v.strip(), max_chars)
    for key in ("content", "text", "body"):
        v = source.get(key)
        if isinstance(v, str) and v.strip():
            return _truncate(v.strip(), max_chars)
    return ""


# --- core: synthesis --------------------------------------------------------


def _dedup_sources(source_candidates: list[dict]) -> list[dict]:
    """URL dedup: оставляем только первое вхождение каждого URL.

    Returns: list[dict] — уникальные sources в порядке первого появления.
    """
    if not source_candidates:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for src in source_candidates:
        if not isinstance(src, dict):
            continue
        url = src.get("url", "") or ""
        if not url or url == "?":
            # Пустой URL — оставляем только если ещё нет (чтобы не потерять)
            key = f"_empty_{len(out)}"
        else:
            key = url
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
    return out


def _build_citation_table(
    unique_sources: list[dict],
) -> list[Citation]:
    """Строит Citation objects с id 1..N."""
    citations: list[Citation] = []
    for i, src in enumerate(unique_sources, start=1):
        if i > MAX_CITATIONS:
            break
        if not isinstance(src, dict):
            continue
        url = src.get("url", "") or "?"
        title = src.get("title") or _url_to_title(url)
        quote = _extract_quote(src)
        citations.append(
            Citation(
                id=i,
                url=url,
                title=title,
                quote=quote,
                source_index=i - 1,  # 0-based index in unique_sources
            )
        )
    return citations


def _collect_supporting_urls(result: dict) -> list[str]:
    """Извлекает supporting URLs из result dict.

    Поддерживает два формата:
      1. source_urls: list[str] (от LLMVerifier)
      2. supporting_sources: list[(url, score, method)] (от hermes_deepresearch)
    """
    urls: list[str] = []
    if "source_urls" in result and isinstance(result["source_urls"], list):
        for u in result["source_urls"]:
            if isinstance(u, str) and u:
                urls.append(u)
    if "supporting_sources" in result and isinstance(result["supporting_sources"], list):
        for entry in result["supporting_sources"]:
            if isinstance(entry, (list, tuple)) and entry:
                u = entry[0]
                if isinstance(u, str) and u:
                    urls.append(u)
            elif isinstance(entry, str) and entry:
                urls.append(entry)
    return urls


def _collect_refuting_urls(result: dict) -> list[str]:
    urls: list[str] = []
    if "refuting_sources" in result and isinstance(result["refuting_sources"], list):
        for entry in result["refuting_sources"]:
            if isinstance(entry, (list, tuple)) and entry:
                u = entry[0]
                if isinstance(u, str) and u:
                    urls.append(u)
            elif isinstance(entry, str) and entry:
                urls.append(entry)
    return urls


def _collect_mismatch_urls(result: dict) -> list[str]:
    urls: list[str] = []
    if "numeric_mismatch_sources" in result and isinstance(result["numeric_mismatch_sources"], list):
        for entry in result["numeric_mismatch_sources"]:
            if isinstance(entry, (list, tuple)) and entry:
                u = entry[0]
                if isinstance(u, str) and u:
                    urls.append(u)
            elif isinstance(entry, str) and entry:
                urls.append(entry)
    return urls


def _compute_coverage(
    claims: list[str],
    results: list[dict],
) -> dict:
    """Coverage: supported/total + list unsupported.

    Coverage score = (len(VERDICTS_POSITIVE) + 0.5 * len(VERDICTS_CONFLICT)) / total
    Conflicts дают 0.5 — частично подтверждено, но требует review.
    """
    total = len(results) or len(claims)
    if total == 0:
        return {
            "supported": 0,
            "partial": 0,
            "total": 0,
            "score": 0.0,
            "unsupported": [],
        }

    supported = 0
    partial = 0
    unsupported: list[dict] = []

    for i, r in enumerate(results):
        claim = (r.get("fact") or claims[i] if i < len(claims) else "") or ""
        verdict = r.get("verdict")
        if verdict in VERDICTS_POSITIVE:
            supported += 1
        elif verdict in VERDICTS_CONFLICT:
            partial += 1
            unsupported.append(
                {
                    "claim": _truncate(claim, MAX_CLAIM_PREVIEW_CHARS),
                    "reason": "CONFLICTING" if verdict == VERDICT_CONFLICTING else "NUMERIC_MISMATCH",
                }
            )
        else:
            # INSUFFICIENT, REFUTES, None
            unsupported.append(
                {
                    "claim": _truncate(claim, MAX_CLAIM_PREVIEW_CHARS),
                    "reason": verdict or "INSUFFICIENT",
                }
            )

    score = round((supported + 0.5 * partial) / total, 4)
    return {
        "supported": supported,
        "partial": partial,
        "total": total,
        "score": score,
        "unsupported": unsupported,
    }


def _find_contradictions(results: list[dict]) -> list[dict]:
    """Contradictions: REFUTES, CONFLICTING, NUMERIC_MISMATCH verdicts."""
    out: list[dict] = []
    for r in results:
        verdict = r.get("verdict")
        if verdict not in (VERDICT_REFUTES, VERDICT_CONFLICTING, VERDICT_NUMERIC_MISMATCH):
            continue
        if verdict == VERDICT_REFUTES:
            urls = _collect_refuting_urls(r)
        elif verdict == VERDICT_NUMERIC_MISMATCH:
            urls = _collect_mismatch_urls(r)
        else:  # CONFLICTING: supporting + refuting/mismatch (обе стороны)
            urls = _collect_supporting_urls(r) + _collect_refuting_urls(r) + _collect_mismatch_urls(r)
            # Dedupe, сохраняем порядок
            seen = set()
            deduped = []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    deduped.append(u)
            urls = deduped
        out.append(
            {
                "fact": _truncate(r.get("fact", ""), MAX_CLAIM_PREVIEW_CHARS),
                "type": verdict,
                "urls": urls,
            }
        )
    return out


def _compute_confidence(
    coverage: dict,
    contradictions: list[dict],
    num_citations: int,
) -> float:
    """Heuristic confidence ∈ [0, 1].

    base = coverage.score
    penalty = 0.1 per contradiction
    citation_bonus = min(0.1, 0.02 * num_citations)
    """
    base = coverage.get("score", 0.0) or 0.0
    penalty = 0.1 * len(contradictions)
    bonus = min(0.1, 0.02 * num_citations)
    val = base - penalty + bonus
    return round(max(0.0, min(1.0, val)), 4)


def _build_open_questions(
    claims: list[str],
    results: list[dict],
) -> list[str]:
    """Open questions: claims с verdict ∈ (INSUFFICIENT, REFUTES, None).

    Format: "<claim> — нужно больше источников.""
    """
    out: list[str] = []
    for i, r in enumerate(results):
        verdict = r.get("verdict")
        if verdict in (VERDICT_INSUFFICIENT, VERDICT_REFUTES, None):
            claim = (r.get("fact") or claims[i] if i < len(claims) else "") or ""
            if not claim:
                continue
            if verdict == VERDICT_REFUTES:
                q = f"Оспаривается: {_truncate(claim, MAX_CLAIM_PREVIEW_CHARS)}"
            else:
                q = f"Не подтверждено: {_truncate(claim, MAX_CLAIM_PREVIEW_CHARS)}"
            out.append(q)
        if len(out) >= MAX_OPEN_QUESTIONS:
            break
    return out


# --- markdown rendering -----------------------------------------------------


def _build_url_to_id(citations: list[Citation]) -> dict[str, int]:
    """Map url → citation id (для inline markers)."""
    return {c.url: c.id for c in citations if c.url and c.url != "?"}


def _format_citation_markers(
    urls: list[str],
    url_to_id: dict[str, int],
) -> str:
    """Превращает list[url] в строку вида '[1][3]'."""
    ids: list[int] = []
    for u in urls:
        cid = url_to_id.get(u)
        if cid is not None and cid not in ids:
            ids.append(cid)
    if not ids:
        return ""
    return "".join(f"[{cid}]" for cid in sorted(ids))


def _render_markdown(
    query: str,
    claims: list[str],
    results: list[dict],
    citations: list[Citation],
    coverage: dict,
    contradictions: list[dict],
    open_questions: list[str],
) -> str:
    """Build final answer_markdown. Pure deterministic."""
    if not results and not claims:
        return "_Нет данных для ответа._"

    url_to_id = _build_url_to_id(citations)

    # --- section 1: short answer
    parts: list[str] = []
    parts.append(f"## Ответ\n\n{_md_escape(query)}\n")

    # --- section 2: per-claim breakdown
    parts.append("\n## Детали по утверждениям\n")
    for i, r in enumerate(results):
        claim = (r.get("fact") or claims[i] if i < len(claims) else "") or ""
        verdict = r.get("verdict") or "INSUFFICIENT"
        reasoning = r.get("reasoning", "")

        supporting = _collect_supporting_urls(r)
        refuting = _collect_refuting_urls(r)
        mismatch = _collect_mismatch_urls(r)

        markers = _format_citation_markers(supporting + refuting + mismatch, url_to_id)

        verdict_label = {
            VERDICT_SUPPORTS: "✅ Подтверждено",
            VERDICT_REFUTES: "❌ Опровергнуто",
            VERDICT_INSUFFICIENT: "⚠️ Недостаточно данных",
            VERDICT_CONFLICTING: "⚡ Противоречие",
            VERDICT_NUMERIC_MISMATCH: "🔢 Числовое расхождение",
            None: "❓ Нет вердикта",
        }.get(verdict, f"❓ {verdict}")

        parts.append(
            f"\n### {i + 1}. {_md_escape(_truncate(claim, 120))}\n"
            f"**Вердикт:** {verdict_label}{(' ' + markers) if markers else ''}\n"
        )
        if reasoning:
            parts.append(f"> {_md_escape(_truncate(reasoning, MAX_QUOTE_CHARS))}\n")
        if refuting:
            parts.append(f"Опрошено: {_format_citation_markers(refuting, url_to_id)}\n")
        if mismatch:
            parts.append(f"Числовое расхождение: {_format_citation_markers(mismatch, url_to_id)}\n")

    # --- section 3: coverage
    if coverage.get("total", 0) > 0:
        parts.append(
            f"\n## Покрытие\n\n"
            f"- Подтверждено: {coverage.get('supported', 0)}/"
            f"{coverage.get('total', 0)}\n"
            f"- Частично: {coverage.get('partial', 0)}/"
            f"{coverage.get('total', 0)}\n"
            f"- Score: {coverage.get('score', 0.0):.2%}\n"
        )
        unsupported = coverage.get("unsupported") or []
        if unsupported:
            parts.append("\n**Не подтверждено:**\n")
            for u in unsupported[:MAX_OPEN_QUESTIONS]:
                claim = u.get("claim", "")
                reason = u.get("reason", "")
                parts.append(f"- {_md_escape(claim)} — _{reason}_\n")

    # --- section 4: contradictions
    if contradictions:
        parts.append("\n## Противоречия\n")
        for c in contradictions:
            f = c.get("fact", "")
            t = c.get("type", "")
            urls = c.get("urls", [])
            markers = _format_citation_markers(urls, url_to_id)
            parts.append(f"- **{t}**: {_md_escape(f)}{(' ' + markers) if markers else ''}\n")

    # --- section 5: open questions
    if open_questions:
        parts.append("\n## Открытые вопросы\n")
        for q in open_questions:
            parts.append(f"- {_md_escape(q)}\n")

    # --- section 6: sources
    if citations:
        parts.append("\n## Источники\n")
        for c in citations:
            title = _md_escape(c.title or c.url)
            url = c.url
            parts.append(f"- [{c.id}] [{title}]({url})\n")
            if c.quote:
                parts.append(f"  > {_md_escape(_truncate(c.quote, MAX_QUOTE_CHARS))}\n")

    md = "".join(parts)
    if len(md) > MAX_MARKDOWN_CHARS:
        md = md[:MAX_MARKDOWN_CHARS] + "\n\n_… truncated для безопасности._"
    return md


# --- main: deterministic synthesize() ---------------------------------------


def synthesize(
    query: str,
    claims: list[str],
    results: list[dict],
    source_candidates: list[dict],
) -> Synthesis:
    """Deterministic synthesis builder.

    Pure stdlib, no LLM, no network. Безопасен для unit-тестов.

    Args:
        query: original user query
        claims: list of fact strings (used as fallback when result missing)
        results: list of VerificationResult dicts
        source_candidates: list of {"url", "text", "title"?} dicts

    Returns:
        Synthesis with deterministic markdown, citations, coverage, etc.
    """
    # 1. Dedup sources
    unique_sources = _dedup_sources(source_candidates or [])

    # 2. Citation table
    citations = _build_citation_table(unique_sources)

    # 3. Coverage
    coverage = _compute_coverage(claims or [], results or [])

    # 4. Contradictions
    contradictions = _find_contradictions(results or [])

    # 5. Confidence
    confidence = _compute_confidence(coverage, contradictions, len(citations))

    # 6. Open questions
    open_questions = _build_open_questions(claims or [], results or [])

    # 7. Markdown
    answer_markdown = _render_markdown(
        query=query or "",
        claims=claims or [],
        results=results or [],
        citations=citations,
        coverage=coverage,
        contradictions=contradictions,
        open_questions=open_questions,
    )

    return Synthesis(
        answer_markdown=answer_markdown,
        citations=citations,
        coverage=coverage,
        contradictions=contradictions,
        confidence=confidence,
        open_questions=open_questions,
        enriched_by_llm=False,
        llm_fallback_reason=None,
    )


# --- LLM enrich layer (optional) -------------------------------------------

# Regex для поиска [N] в LLM-enriched markdown
_CITATION_MARKER_RE = re.compile(r"\[(\d{1,3})\]")
# Regex для поиска URL в LLM-enriched markdown (чтобы поймать выдуманные)
_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def _validate_enriched_markdown(
    enriched: str,
    valid_citation_ids: set[int],
    valid_urls: set[str],
) -> tuple[bool, str]:
    """Post-validate LLM-enriched markdown.

    Returns:
        (is_valid, reason). is_valid=False → fallback to deterministic.
    """
    if not enriched or not enriched.strip():
        return False, "empty markdown"

    # Check 1: every [N] must be a valid citation id
    for m in _CITATION_MARKER_RE.finditer(enriched):
        try:
            n = int(m.group(1))
        except ValueError:
            return False, f"non-integer citation marker: {m.group(0)}"
        if n not in valid_citation_ids:
            return False, f"unknown citation id: [{n}]"

    # Check 2: every URL must be in valid_urls (no fabrication)
    for m in _URL_RE.finditer(enriched):
        url = m.group(0).rstrip(".,;:!?)")
        # Empty URL placeholder is not considered a "real" URL
        if url in ("?", ""):
            continue
        if url not in valid_urls:
            return False, f"unknown URL: {url}"

    # Check 3: markdown should not be empty / whitespace-only after parsing
    if len(enriched.strip()) < 10:
        return False, "markdown too short"

    return True, "ok"


def enrich_with_llm(
    base: Synthesis,
    query: str,
    claims: list[str],
    results: list[dict],
    source_candidates: list[dict],
    llm_client: Any | None = None,
) -> Synthesis:
    """Optional LLM enrich layer.

    Args:
        base: deterministic Synthesis (от synthesize())
        query: original user query
        claims: list of fact strings
        results: list of VerificationResult dicts
        source_candidates: original source candidates
        llm_client: optional LLM client с .complete(prompt, ...) интерфейсом
                     Если None или вызов падает → fallback to base.

    Returns:
        Synthesis with enriched_markdown (если LLM ok) ИЛИ base (если fallback).

    Hard rules:
      - llm_client=None → base
      - LLM call raises / timeout / empty → base (llm_fallback_reason set)
      - LLM markdown с invalid [N] / unknown URL → base
      - LLM markdown ok → новый Synthesis с тем же citations/coverage/etc,
        но enriched_by_llm=True и другим answer_markdown
    """
    # Failure 1: no client
    if llm_client is None:
        base.llm_fallback_reason = "no llm_client"
        return base

    # Failure 2: client без метода complete
    complete = getattr(llm_client, "complete", None)
    if not callable(complete):
        base.llm_fallback_reason = "llm_client has no .complete()"
        return base

    # Build prompt
    valid_citation_ids = {c.id for c in base.citations}
    valid_urls = {c.url for c in base.citations if c.url and c.url != "?"}

    facts_brief = "\n".join(f"- {i + 1}. {c}" for i, c in enumerate(claims[:20]))
    citations_brief = "\n".join(f"[{c.id}] {c.title} — {c.url}" for c in base.citations[:30])
    prompt = (
        "You are a research synthesis writer. Rewrite the following "
        "deterministic answer into a clean, well-structured Russian "
        "markdown report.\n\n"
        f"## User query\n{query}\n\n"
        f"## Verified facts\n{facts_brief}\n\n"
        f"## Citations (use ONLY these)\n{citations_brief}\n\n"
        f"## Deterministic answer (rewrite, don't copy)\n"
        f"{base.answer_markdown}\n\n"
        "Rules:\n"
        "- Use only the citation ids listed above. Format: [1][2]\n"
        "- Do not invent URLs or sources.\n"
        "- Keep inline citations where the deterministic answer had them.\n"
        "- Add a short intro (1-2 sentences) and a clear conclusion.\n"
        "- Length: roughly the same as deterministic answer (±30%).\n"
        "- Return ONLY the new markdown, no preamble."
    )

    # Failure 3: LLM call itself
    try:
        enriched_raw = complete(prompt)
    except Exception as e:
        base.llm_fallback_reason = f"llm call failed: {type(e).__name__}: {e}"
        return base

    if not enriched_raw or not isinstance(enriched_raw, str):
        base.llm_fallback_reason = "llm returned non-string / empty"
        return base

    enriched = enriched_raw.strip()

    # Failure 4: post-validate
    valid, reason = _validate_enriched_markdown(enriched, valid_citation_ids, valid_urls)
    if not valid:
        base.llm_fallback_reason = f"post-validation failed: {reason}"
        return base

    # Success
    return Synthesis(
        answer_markdown=enriched,
        citations=base.citations,
        coverage=base.coverage,
        contradictions=base.contradictions,
        confidence=base.confidence,
        open_questions=base.open_questions,
        enriched_by_llm=True,
        llm_fallback_reason=None,
    )


# --- public API re-exports --------------------------------------------------

__all__ = [
    "Citation",
    "Synthesis",
    "SynthesisError",
    "synthesize",
    "enrich_with_llm",
    "VERDICT_SUPPORTS",
    "VERDICT_REFUTES",
    "VERDICT_INSUFFICIENT",
    "VERDICT_CONFLICTING",
    "VERDICT_NUMERIC_MISMATCH",
    "MAX_QUOTE_CHARS",
    "MAX_MARKDOWN_CHARS",
    "MAX_CITATIONS",
    "MAX_OPEN_QUESTIONS",
]
