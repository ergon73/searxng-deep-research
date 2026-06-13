"""
v0.8.3-C2-data: span-level refuting / numeric-mismatch evidence windows.

`verify_sources()` now emits three new optional fields on each
`verification_details[i]` dict:

  - `supporting_evidence_windows: list[dict]`
  - `refuting_evidence_windows: list[dict]`
  - `numeric_mismatch_evidence_windows: list[dict]`

Each window is `{source_url, quote, offset_start, offset_end, method}`.
The legacy URL-level fields (`supporting_sources`, `refuting_sources`,
`numeric_mismatch_sources`) are kept intact — backward-compat is
preserved.

Hard rules:
  - Offsets are *only* emitted when the helper regex can localise the
    refuting / mismatching phrase in the source text. The helpers
    return None on miss; the caller keeps the URL-only entry.
  - No LLM calls — these tests use the deterministic 4-level path.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hermes_deepresearch import (  # noqa: E402
    _find_negation_span,
    _find_num_mismatch_span,
    verify_sources,
)

# === 1. Helper-level tests: never invent offsets ======================


def test_find_negation_span_returns_none_on_empty_inputs():
    """Helpers must be defensive: empty fact/text → None, no exception."""
    assert _find_negation_span("", "some text") is None
    assert _find_negation_span("fact", "") is None
    assert _find_negation_span("", "") is None


def test_find_negation_span_localises_negation_after():
    """`не <...> <fact>` pattern → span with method=negation_after."""
    text = "Сообщается, что не было сбито 5 дронов вчера."
    span = _find_negation_span("5 дронов", text)
    assert span is not None
    off_s, off_e, method = span
    assert method == "negation_after"
    assert 0 <= off_s < off_e <= len(text)
    # Quote extracted from text must contain the fact phrase.
    assert "5 дронов" in text[off_s:off_e].lower()


def test_find_num_mismatch_span_localises_first_conflict():
    """`5 дронов` vs `7 дронов` → first mismatching occurrence span."""
    text = "По данным источника, 7 дронов были сбиты, а не 5 дронов."
    span = _find_num_mismatch_span("5 дронов", text)
    assert span is not None
    off_s, off_e, method = span
    assert method == "num_mismatch"
    assert 0 <= off_s < off_e <= len(text)
    # Quote should contain the mismatching number, not the fact's number.
    assert "7" in text[off_s:off_e]
    assert "5" not in text[off_s:off_e].split("дронов")[0]


def test_find_num_mismatch_span_returns_none_on_no_mismatch():
    """`5 дронов` vs `5 беспилотников` (same number) → no span emitted."""
    text = "По данным, 5 дронов были сбиты."
    span = _find_num_mismatch_span("5 дронов", text)
    assert span is None, f"should not emit span on same-number match: {span}"


# === 2. End-to-end tests: verify_sources() integration ================


def _make_top1(fact: str, text: str) -> dict:
    """Build a minimal top-1 with a single fact-bearing sentence."""
    return {"url": "https://example.com/top1", "text": text, "title": "Top"}


def _make_other(url: str, text: str) -> dict:
    return {"url": url, "text": text, "title": "Other"}


def test_refutes_adds_refuting_evidence_window():
    """AC #2: REFUTES via negation in another source → refuting window
    with the negation span, source_url, and a quote that contains the
    refuting phrase."""
    top1 = _make_top1(
        "5 дронов сбиты",
        "По предварительным данным, 5 дронов сбиты над Москвой.",
    )
    # Other source has the negation phrase, which forces REFUTES.
    other = _make_other(
        "https://example.com/other1",
        "Официальный представитель заявил, что не было сбито 5 дронов.",
    )
    out = verify_sources(
        top1=top1,
        other_sources=[other],
        query="БПЛА",
        use_llm=False,
        max_facts=5,
    )
    details = out["verification_details"]
    assert len(details) == 1, f"expected one detail, got {details!r}"
    d = details[0]
    assert d["verdict"] == "REFUTES", f"expected REFUTES, got {d['verdict']}"
    # Legacy URL field preserved.
    assert d["refuting_sources"] == ["https://example.com/other1"]
    # New window populated.
    assert d["refuting_evidence_windows"], (
        f"expected refuting_evidence_windows, got {d['refuting_evidence_windows']!r}"
    )
    w = d["refuting_evidence_windows"][0]
    assert w["source_url"] == "https://example.com/other1"
    assert 0 <= w["offset_start"] < w["offset_end"] <= len(other["text"])
    assert "5 дронов" in w["quote"].lower()
    assert w["method"].startswith("negation_")


def test_numeric_mismatch_adds_mismatch_evidence_window():
    """AC #3: NUMERIC_MISMATCH → mismatch window with the mismatching
    number span and the source URL. No window when phrase not located."""
    # Use a fact that contains a number + a stem, so NUM_UNIT_RE matches.
    top1 = _make_top1(
        "5 беспилотников сбиты",
        "Все 5 беспилотников сбиты средствами ПВО.",
    )
    # Other source has same stem, different number → NUMERIC_MISMATCH.
    other = _make_other(
        "https://example.com/other1",
        "По уточнённым данным, 7 беспилотников сбиты ночью.",
    )
    out = verify_sources(
        top1=top1,
        other_sources=[other],
        query="БПЛА",
        use_llm=False,
        max_facts=5,
    )
    details = out["verification_details"]
    assert len(details) == 1, f"expected one detail, got {details!r}"
    d = details[0]
    assert d["verdict"] == "NUMERIC_MISMATCH", (
        f"expected NUMERIC_MISMATCH, got {d['verdict']}"
    )
    # Legacy URL tuple preserved.
    assert len(d["numeric_mismatch_sources"]) == 1
    assert d["numeric_mismatch_sources"][0][0] == "https://example.com/other1"
    # New window populated.
    assert d["numeric_mismatch_evidence_windows"], (
        f"expected numeric_mismatch_evidence_windows, got "
        f"{d['numeric_mismatch_evidence_windows']!r}"
    )
    w = d["numeric_mismatch_evidence_windows"][0]
    assert w["source_url"] == "https://example.com/other1"
    assert w["method"] == "num_mismatch"
    assert 0 <= w["offset_start"] < w["offset_end"] <= len(other["text"])
    # Quote contains the mismatching number, not the fact's number.
    assert "7" in w["quote"]
    assert "5" not in w["quote"].split("беспилотник")[0]


def test_no_window_when_text_not_found():
    """AC defensive: if `text` is empty, no window is emitted; the URL
    field stays as before. We force this case by giving a fact that
    cannot be matched numerically and no negation either."""
    top1 = _make_top1("5 беспилотников", "5 беспилотников сбиты.")
    # Other source has the fact in support form (no negation, same number).
    other = _make_other(
        "https://example.com/other1",
        "По сообщениям, 5 беспилотников сбиты.",
    )
    out = verify_sources(
        top1=top1,
        other_sources=[other],
        query="БПЛА",
        use_llm=False,
        max_facts=5,
    )
    d = out["verification_details"][0]
    assert d["verdict"] == "SUPPORTS", f"expected SUPPORTS, got {d['verdict']}"
    # No refuting / mismatch windows when verdict is SUPPORTS.
    assert d["refuting_evidence_windows"] == []
    assert d["numeric_mismatch_evidence_windows"] == []
    # supporting_evidence_windows is intentionally empty in this batch
    # (out of scope per C2-data AC #5).
    assert d["supporting_evidence_windows"] == []


def test_insufficient_has_no_refuting_or_mismatch_windows():
    """AC #4 (INSUFFICIENT branch): no refuting / mismatch windows."""
    top1 = _make_top1(
        "5 дронов сбиты",
        "5 дронов сбиты, подробности уточняются.",
    )
    # Other source does NOT contain the fact and has no negation/mismatch
    # → verdict=INSUFFICIENT.
    other = _make_other(
        "https://example.com/other1",
        "Ситуация развивается, детали будут позже.",
    )
    out = verify_sources(
        top1=top1,
        other_sources=[other],
        query="БПЛА",
        use_llm=False,
        max_facts=5,
    )
    d = out["verification_details"][0]
    assert d["verdict"] == "INSUFFICIENT", f"expected INSUFFICIENT, got {d['verdict']}"
    assert d["refuting_evidence_windows"] == []
    assert d["numeric_mismatch_evidence_windows"] == []
    assert d["supporting_evidence_windows"] == []


def test_weak_support_has_no_refuting_or_mismatch_windows():
    """AC #4 (WEAK_SUPPORT branch): no refuting / mismatch windows.

    We force WEAK_SUPPORT by patching the LLM stub to return
    SUPPORTS+empty source_urls (the B1 down-grade path)."""
    from unittest.mock import patch as _patch  # noqa: PLC0415

    from llm_verifier import LLMVerifier  # noqa: PLC0415

    top1 = _make_top1(
        "5 дронов сбиты",
        "5 дронов сбиты, верификация требуется.",
    )
    other = _make_other(
        "https://example.com/other1",
        "Ситуация развивается, детали будут позже.",
    )

    def fake_batch(self, facts, source_candidates):
        return [
            {
                "fact": f,
                "verdict": "SUPPORTS",
                "source_urls": [],  # no valid citations → WEAK_SUPPORT
                "reasoning": "stub",
                "llm_verified": False,
                "llm_refuted": False,
                "llm_error": None,
            }
            for f in facts
        ]

    with _patch.object(LLMVerifier, "verify_facts_batch", fake_batch):
        out = verify_sources(
            top1=top1,
            other_sources=[other],
            query="БПЛА",
            use_llm=True,
            max_facts=5,
        )
    d = out["verification_details"][0]
    assert d["verdict"] == "WEAK_SUPPORT", f"expected WEAK_SUPPORT, got {d['verdict']}"
    assert d["refuting_evidence_windows"] == []
    assert d["numeric_mismatch_evidence_windows"] == []


def test_legacy_fields_remain_backward_compatible():
    """AC #1: existing URL fields are preserved exactly (no rename,
    no shape change). The new *_evidence_windows fields are additive
    and default to empty lists for non-applicable verdicts."""
    # A SUPPORTS case with one supporting source — no refuting/mismatch
    # anywhere. The new fields are present but empty.
    top1 = _make_top1(
        "Falcon 9",
        "Ракета Falcon 9 успешно стартовала.",
    )
    other = _make_other(
        "https://example.com/other1",
        "Запуск Falcon 9 прошёл штатно, аппарат вышел на орбиту.",
    )
    out = verify_sources(
        top1=top1,
        other_sources=[other],
        query="Falcon 9",
        use_llm=False,
        max_facts=5,
    )
    d = out["verification_details"][0]
    # Legacy fields — exact shape preserved.
    assert "supporting_sources" in d
    assert "refuting_sources" in d
    assert "numeric_mismatch_sources" in d
    # New fields — always present, even when empty.
    assert d.get("supporting_evidence_windows") == []
    assert d.get("refuting_evidence_windows") == []
    assert d.get("numeric_mismatch_evidence_windows") == []
    # Legacy URL field is still a tuple (or list of tuples) — not
    # replaced by windows.
    assert isinstance(d["supporting_sources"], list)
