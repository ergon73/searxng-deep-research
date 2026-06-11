"""
v0.8.3-B — user-facing answer contract tests.

Concern: synthesis output separates user answer (answer_markdown) from
the old per-claim audit breakdown (audit_markdown).

These tests pin the v0.8.3-B contract:
  1. answer_markdown has the 6 user sections in order
  2. audit_markdown preserves the old per-claim breakdown
  3. Verdict routing (SUPPORTS / WEAK_SUPPORT / REFUTES / NUMERIC_MISMATCH / INSUFFICIENT)
  4. Citation rule: confirmed bullets must have at least one [N] marker

Pure stdlib + synthesis module — no LLM, no network.
"""

import re

import pytest
from synthesis import (
    VERDICT_CONFLICTING,
    VERDICT_INSUFFICIENT,
    VERDICT_NUMERIC_MISMATCH,
    VERDICT_REFUTES,
    VERDICT_SUPPORTS,
    synthesize,
)

# v0.8.3-B: WEAK_SUPPORT verdict added to synthesis layer.
# Imported lazily so module-level collection succeeds even before T2 lands.
try:
    from synthesis import VERDICT_WEAK_SUPPORT  # type: ignore
except ImportError:  # RED phase — T2 will add it
    VERDICT_WEAK_SUPPORT = "WEAK_SUPPORT"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def two_sources():
    return [
        {"url": "http://a.com/x", "title": "A", "text": "snippet A"},
        {"url": "http://b.com/y", "title": "B", "text": "snippet B"},
    ]


# --- AC #3: answer_markdown structure --------------------------------------


class TestUserSections:
    """AC #3 — answer_markdown has the 6 user sections in order."""

    REQUIRED_SECTIONS = [
        "## Краткий ответ",
        "## Подтверждено источниками",
        "## Слабые или неподтверждённые сигналы",
        "## Противоречия / расхождения",
        "## Что не удалось проверить",
        "## Источники",
    ]

    def test_answer_markdown_has_user_sections(self, two_sources):
        """v0.8.3-B AC #3: с входом, который активирует все 4 секции routing,
        в answer_markdown присутствуют все 6 user-facing секций в требуемом порядке.
        """
        results = [
            # confirmed
            {"fact": "cited fact", "verdict": VERDICT_SUPPORTS, "reasoning": "ok",
             "source_urls": ["http://a.com/x"]},
            # weak (SUPPORTS без resolvable citation)
            {"fact": "uncited supports", "verdict": VERDICT_SUPPORTS,
             "reasoning": "ok", "source_urls": []},
            # contradiction
            {"fact": "refuted fact", "verdict": VERDICT_REFUTES,
             "reasoning": "neg",
             "refuting_sources": [("http://b.com/y", 0.9, "negation")]},
            # unverifiable
            {"fact": "open fact", "verdict": VERDICT_INSUFFICIENT,
             "reasoning": "no data"},
        ]
        s = synthesize(query="q", claims=["cited fact", "uncited supports",
                                          "refuted fact", "open fact"],
                       results=results, source_candidates=two_sources)
        md = s.answer_markdown
        for sec in self.REQUIRED_SECTIONS:
            assert sec in md, f"missing section {sec!r} in answer_markdown"
        # Sections appear in the required order
        positions = [md.find(sec) for sec in self.REQUIRED_SECTIONS]
        assert positions == sorted(positions), (
            f"sections out of order: {list(zip(self.REQUIRED_SECTIONS, positions, strict=True))}"
        )

    def test_audit_markdown_preserves_old_claim_breakdown(self, two_sources):
        """AC #6 — the old per-claim breakdown moves to audit_markdown."""
        results = [
            {"fact": "claim one", "verdict": VERDICT_SUPPORTS, "reasoning": "ok",
             "source_urls": ["http://a.com/x"]},
            {"fact": "claim two", "verdict": VERDICT_INSUFFICIENT, "reasoning": "x"},
        ]
        s = synthesize(query="q", claims=["claim one", "claim two"],
                       results=results, source_candidates=two_sources)
        # The new answer_markdown should NOT contain the old section header
        assert "## Детали по утверждениям" not in s.answer_markdown
        # The old breakdown still exists, in audit_markdown
        assert "## Детали по утверждениям" in s.audit_markdown
        # And it carries the per-claim text
        assert "claim one" in s.audit_markdown
        assert "claim two" in s.audit_markdown


# --- AC #4: verdict routing ------------------------------------------------


class TestVerdictRouting:
    """AC #4 — verdict → section routing."""

    def test_supports_with_citation_goes_to_confirmed(self, two_sources):
        results = [
            {"fact": "fact alpha", "verdict": VERDICT_SUPPORTS, "reasoning": "ok",
             "source_urls": ["http://a.com/x"]},
        ]
        s = synthesize(query="q", claims=["fact alpha"], results=results,
                       source_candidates=two_sources)
        # Locate the confirmed section
        confirmed, _, rest = s.answer_markdown.partition(
            "## Слабые или неподтверждённые сигналы"
        )
        confirmed_block = confirmed.split("## Подтверждено источниками", 1)[-1]
        assert "fact alpha" in confirmed_block
        # Citation marker must be present
        assert re.search(r"\[\d+\]", confirmed_block), (
            f"confirmed block has no citation marker: {confirmed_block!r}"
        )

    def test_supports_without_citation_not_confirmed(self, two_sources):
        """AC #4 — SUPPORTS without a real citation marker must NOT be confirmed."""
        results = [
            # SUPPORTS but source_urls=[] → no marker in answer
            {"fact": "uncited fact", "verdict": VERDICT_SUPPORTS, "reasoning": "ok",
             "source_urls": []},
        ]
        s = synthesize(query="q", claims=["uncited fact"], results=results,
                       source_candidates=two_sources)
        confirmed_block = s.answer_markdown.split(
            "## Подтверждено источниками", 1
        )[-1].split("## Слабые или неподтверждённые сигналы", 1)[0]
        # The uncited fact must not appear in the confirmed section
        assert "uncited fact" not in confirmed_block
        # It must land somewhere: weak or unverifiable
        assert "uncited fact" in s.answer_markdown

    def test_weak_support_goes_to_weak_section(self, two_sources):
        """AC #4 + #7 — WEAK_SUPPORT renders in weak section, not confirmed."""
        results = [
            {"fact": "weak fact", "verdict": VERDICT_WEAK_SUPPORT,
             "reasoning": "no accepted urls", "source_urls": []},
        ]
        s = synthesize(query="q", claims=["weak fact"], results=results,
                       source_candidates=two_sources)
        confirmed_block = s.answer_markdown.split(
            "## Подтверждено источниками", 1
        )[-1].split("## Слабые или неподтверждённые сигналы", 1)[0]
        weak_block = s.answer_markdown.split(
            "## Слабые или неподтверждённые сигналы", 1
        )[-1].split("## Противоречия / расхождения", 1)[0]
        # Not in confirmed
        assert "weak fact" not in confirmed_block
        # In weak
        assert "weak fact" in weak_block
        # WEAK_SUPPORT UX text — phrased as a non-confirmed signal
        # Must NOT contain "подтверждено" as a positive label
        weak_lower = weak_block.lower()
        assert "слабый сигнал" in weak_lower or "не подтверждено" in weak_lower

    def test_refutes_goes_to_contradictions(self, two_sources):
        results = [
            {"fact": "refuted fact", "verdict": VERDICT_REFUTES,
             "reasoning": "neg",
             "refuting_sources": [("http://b.com/y", 0.9, "negation")]},
        ]
        s = synthesize(query="q", claims=["refuted fact"], results=results,
                       source_candidates=two_sources)
        cont_block = s.answer_markdown.split(
            "## Противоречия / расхождения", 1
        )[-1].split("## Что не удалось проверить", 1)[0]
        assert "refuted fact" in cont_block

    def test_numeric_mismatch_goes_to_contradictions(self, two_sources):
        results = [
            {"fact": "mismatch fact", "verdict": VERDICT_NUMERIC_MISMATCH,
             "reasoning": "diff num",
             "numeric_mismatch_sources": [("http://a.com/x", 0.7, "num_mismatch")]},
        ]
        s = synthesize(query="q", claims=["mismatch fact"], results=results,
                       source_candidates=two_sources)
        cont_block = s.answer_markdown.split(
            "## Противоречия / расхождения", 1
        )[-1].split("## Что не удалось проверить", 1)[0]
        assert "mismatch fact" in cont_block

    def test_conflicting_goes_to_contradictions(self, two_sources):
        """AC #4 — CONFLICTING also routes to contradictions."""
        results = [
            {"fact": "conflict fact", "verdict": VERDICT_CONFLICTING,
             "reasoning": "both sides",
             "supporting_sources": [("http://a.com/x", 0.5, "stem")],
             "refuting_sources": [("http://b.com/y", 0.6, "negation")]},
        ]
        s = synthesize(query="q", claims=["conflict fact"], results=results,
                       source_candidates=two_sources)
        cont_block = s.answer_markdown.split(
            "## Противоречия / расхождения", 1
        )[-1].split("## Что не удалось проверить", 1)[0]
        assert "conflict fact" in cont_block

    def test_insufficient_goes_to_unverified(self, two_sources):
        """AC #4 — INSUFFICIENT → 'Что не удалось проверить'."""
        results = [
            {"fact": "open fact", "verdict": VERDICT_INSUFFICIENT,
             "reasoning": "no data"},
        ]
        s = synthesize(query="q", claims=["open fact"], results=results,
                       source_candidates=two_sources)
        unver_block = s.answer_markdown.split(
            "## Что не удалось проверить", 1
        )[-1].split("## Источники", 1)[0]
        assert "open fact" in unver_block


# --- AC #5: citation rule --------------------------------------------------


class TestCitationRule:
    """AC #5 — confirmed bullets must carry a citation marker."""

    def test_confirmed_bullets_require_citation_marker(self, two_sources):
        results = [
            # Cited
            {"fact": "cited claim", "verdict": VERDICT_SUPPORTS, "reasoning": "ok",
             "source_urls": ["http://a.com/x"]},
            # Cited (second source)
            {"fact": "another cited", "verdict": VERDICT_SUPPORTS, "reasoning": "ok",
             "source_urls": ["http://b.com/y"]},
        ]
        s = synthesize(query="q", claims=["cited claim", "another cited"],
                       results=results, source_candidates=two_sources)
        # Extract bullets from "## Подтверждено источниками" section
        confirmed_block = s.answer_markdown.split(
            "## Подтверждено источниками", 1
        )[-1].split("## Слабые или неподтверждённые сигналы", 1)[0]
        # Every non-empty bullet must have at least one [N] marker
        bullets = [line for line in confirmed_block.splitlines()
                   if line.lstrip().startswith("-")]
        assert bullets, "no bullets in confirmed section"
        for b in bullets:
            assert re.search(r"\[\d+\]", b), (
                f"confirmed bullet missing citation marker: {b!r}"
            )


# --- AC #2: backward compatibility -----------------------------------------


class TestBackwardCompat:
    """AC #2 — to_dict() includes audit_markdown; answer_markdown is still a str."""

    def test_to_dict_includes_audit_markdown(self, two_sources):
        s = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": VERDICT_SUPPORTS,
                      "source_urls": ["http://a.com/x"]}],
            source_candidates=two_sources,
        )
        d = s.to_dict()
        assert "audit_markdown" in d
        assert isinstance(d["audit_markdown"], str)
        assert d["audit_markdown"]  # non-empty when results/claims provided
        # Existing callers of answer_markdown still get a string
        assert isinstance(d["answer_markdown"], str)

    def test_old_per_claim_breakdown_removed_from_answer(self, two_sources):
        """AC #6 — answer_markdown is no longer '## Ответ\\n\\n{query}' + claim table."""
        s = synthesize(
            query="q", claims=["c1"],
            results=[{"fact": "c1", "verdict": VERDICT_SUPPORTS,
                      "source_urls": ["http://a.com/x"]}],
            source_candidates=two_sources,
        )
        # The old layout is gone
        assert "## Детали по утверждениям" not in s.answer_markdown
        # The new layout is present
        assert "## Краткий ответ" in s.answer_markdown


# --- AC #7: WEAK_SUPPORT UX -------------------------------------------------


class TestWeakSupportUX:
    def test_weak_support_does_not_increase_confidence(self, two_sources):
        """WEAK_SUPPORT must not behave like a successful support."""
        # Two scenarios: (a) one SUPPORTS, (b) one WEAK_SUPPORT
        a = synthesize(
            query="q", claims=["c"],
            results=[{"fact": "c", "verdict": VERDICT_SUPPORTS,
                      "source_urls": ["http://a.com/x"]}],
            source_candidates=two_sources,
        )
        b = synthesize(
            query="q", claims=["c"],
            results=[{"fact": "c", "verdict": VERDICT_WEAK_SUPPORT,
                      "source_urls": []}],
            source_candidates=two_sources,
        )
        # The WEAK_SUPPORT case must have lower or equal confidence
        assert b.confidence <= a.confidence
