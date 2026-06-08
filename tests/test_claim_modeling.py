"""
Tests for skill 6.5: claim-modeling / numeric mismatch fix.

Acceptance criteria from audit 2026-06-07, section 8 Phase B.2:

1. "123 дрона" vs "123 дронов" => SUPPORTS
2. "123 дрона" vs "124 дрона" => NUMERIC_MISMATCH, not SUPPORTS
3. "22 БПЛА" vs "22 беспилотника" => SUPPORTS (synonym stems + same number)
4. "22 БПЛА" vs "23 беспилотника" => NUMERIC_MISMATCH
5. verify_sources() does NOT raise verification_rate on numeric mismatch

NOTE: These tests exercise the **deterministic** verification path
(use_llm=False) so they are hermetic and don't depend on OPENROUTER_API_KEY
or model availability. The LLM-enhanced path is covered by integration
tests / online eval, where LLM verdicts are post-processed through
the deterministic aggregation logic.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from hermes_deepresearch import _match_in_text, verify_sources


# ====================================================================
# Skill 6.5 AC1-AC4: _match_in_text() with numeric counts
# ====================================================================


def test_match_same_number_same_stem_supports():
    """AC1: '123 дрона' vs '123 дронов' → num_morph, score >= 80."""
    matched, method, score = _match_in_text(
        "123 дрона", "сбито 123 дронов за ночь"
    )
    assert matched is True
    assert method == "num_morph", f"expected num_morph, got {method}"
    assert score >= 80


def test_match_different_number_same_stem_mismatch():
    """AC2: '123 дрона' vs '124 дрона' → num_mismatch, NOT num_morph."""
    matched, method, score = _match_in_text(
        "123 дрона", "сбито 124 дрона за ночь"
    )
    assert matched is True, "signature backward-compat: matched=True"
    assert method == "num_mismatch", f"expected num_mismatch, got {method}"
    # This is the CRITICAL bug fix: must NOT be num_morph (which would
    # be treated as support by verify_sources).


def test_match_same_number_synonym_stems_supports():
    """AC3: '22 БПЛА' vs '22 беспилотника' → num_morph (synonym stems).

    SYNONYM_DICT has 'бпла' <-> 'беспилотник'. The cross-stem check
    recognises these as equivalent, and with the same number we get
    a confident num_morph match.
    """
    matched, method, score = _match_in_text(
        "22 БПЛА", "перехвачено 22 беспилотника"
    )
    assert matched is True
    assert method == "num_morph", f"expected num_morph, got {method}"
    assert score >= 80


def test_match_different_number_synonym_stems_mismatch():
    """AC4: '22 БПЛА' vs '23 беспилотника' → num_mismatch."""
    matched, method, score = _match_in_text(
        "22 БПЛА", "перехвачено 23 беспилотника"
    )
    assert matched is True, "signature backward-compat"
    assert method == "num_mismatch", f"expected num_mismatch, got {method}"


# ====================================================================
# Skill 6.5 AC5: verify_sources() does NOT count num_mismatch as support
# ====================================================================


def _make_top1(text: str, url: str = "https://top1.example.com") -> dict:
    return {"url": url, "text": text, "title": "Top1"}


def _make_other(text: str, url: str = "https://other.example.com") -> dict:
    return {"url": url, "text": text, "title": "Other"}


def test_verify_does_not_inflate_rate_on_mismatch():
    """AC5: verify_sources() does NOT raise verification_rate when
    other_sources contain only numeric-mismatched numbers.

    Concretely: top1 says '123 дрона', other source says '124 дрона'.
    The fact should be reported as NUMERIC_MISMATCH, verified=False,
    and verification_rate should be 0.0 (not 1.0).

    Uses use_llm=False to keep the test hermetic (deterministic path).
    """
    top1 = _make_top1("Сбито 123 дрона противника за ночь.")
    others = [
        _make_other("По уточнённым данным, сбито 124 дрона.")
    ]
    result = verify_sources(top1, others, "сколько дронов сбито", use_llm=False)
    assert result["total_facts"] >= 1
    assert result["verified_facts"] == 0, (
        f"numeric mismatch must NOT count as verified, got {result['verified_facts']}"
    )
    assert result["verification_rate"] == 0.0, (
        f"verification_rate must stay at 0, got {result['verification_rate']}"
    )
    # Find the fact about "123 дрона" in details
    found = False
    for d in result["verification_details"]:
        if "123" in d["fact"] and "дрон" in d["fact"].lower():
            assert d["verdict"] in ("NUMERIC_MISMATCH", "CONFLICTING"), (
                f"expected NUMERIC_MISMATCH/CONFLICTING, got {d['verdict']}"
            )
            assert d["verified"] is False
            assert d["numeric_mismatch_sources"], (
                f"numeric_mismatch_sources must be populated: {d}"
            )
            # CRITICAL: the mismatched source must NOT be in supporting_sources
            for src in d["supporting_sources"]:
                assert "other.example.com" not in src[0], (
                    f"num_mismatch source leaked into supporting: {src}"
                )
            found = True
    assert found, f"No fact with '123' + 'дрон' in details: {result['verification_details']}"


def test_verify_supports_on_same_number():
    """Counter-test: '123 дрона' vs '123 дронов' → SUPPORTS, verified=True."""
    top1 = _make_top1("Сбито 123 дрона противника за ночь.")
    others = [
        _make_top1("По другим данным, перехвачено 123 дронов.")
    ]
    others[0]["url"] = "https://other.example.com"
    result = verify_sources(top1, others, "сколько дронов сбито")
    found = False
    for d in result["verification_details"]:
        if "123" in d["fact"] and "дрон" in d["fact"].lower():
            assert d["verdict"] == "SUPPORTS", (
                f"expected SUPPORTS, got {d['verdict']}: {d}"
            )
            assert d["verified"] is True
            found = True
    assert found, f"No matching fact: {result['verification_details']}"


def test_verify_mixed_support_and_mismatch_conflicting():
    """When some sources support and others report a different count,
    verdict is CONFLICTING (not SUPPORTS, not NUMERIC_MISMATCH alone).

    Uses use_llm=False (deterministic path) for hermetic testing.
    """
    top1 = _make_top1("Сбито 123 дрона.")
    others = [
        _make_top1("Подтверждено: сбито 123 дронов."),
        _make_top1("Уточнение: 124 дрона."),
    ]
    others[0]["url"] = "https://supportive.example.com"
    others[1]["url"] = "https://mismatch.example.com"
    result = verify_sources(top1, others, "сколько дронов сбито", use_llm=False)
    for d in result["verification_details"]:
        if "123" in d["fact"] and "дрон" in d["fact"].lower():
            assert d["verdict"] in ("CONFLICTING",), (
                f"expected CONFLICTING, got {d['verdict']}: {d}"
            )
            assert d["verified"] is False
            # Both source lists must be populated
            assert d["supporting_sources"], f"no support: {d}"
            assert d["numeric_mismatch_sources"], f"no mismatch: {d}"


def test_verify_synonym_same_number_supports():
    """AC3 verification path: '22 БПЛА' vs '22 беспилотника' → SUPPORTS."""
    top1 = _make_top1("Сбито 22 БПЛА.")
    others = [
        _make_top1("По нашим данным, уничтожено 22 беспилотника.")
    ]
    result = verify_sources(top1, others, "сколько БПЛА сбито")
    found = False
    for d in result["verification_details"]:
        if "22" in d["fact"] and ("бпла" in d["fact"].lower() or "беспилотник" in d["fact"].lower()):
            assert d["verdict"] == "SUPPORTS", f"got {d['verdict']}: {d}"
            assert d["verified"] is True
            found = True
    assert found, f"No matching fact: {result['verification_details']}"


def test_verify_synonym_different_number_mismatch():
    """AC4 verification path: '22 БПЛА' vs '23 беспилотника' → NUMERIC_MISMATCH.

    Uses use_llm=False (deterministic path) for hermetic testing.
    """
    top1 = _make_top1("Сбито 22 БПЛА.")
    others = [
        _make_top1("Уточнение: уничтожено 23 беспилотника.")
    ]
    result = verify_sources(top1, others, "сколько БПЛА сбито", use_llm=False)
    assert result["verified_facts"] == 0, (
        f"synonym + diff number must not be verified: {result}"
    )
    for d in result["verification_details"]:
        if "22" in d["fact"] and "бпла" in d["fact"].lower():
            assert d["verdict"] in ("NUMERIC_MISMATCH", "CONFLICTING"), (
                f"got {d['verdict']}: {d}"
            )
            assert d["numeric_mismatch_sources"]


# ====================================================================
# Regression: backward-compatible signature
# ====================================================================


def test_match_signature_unchanged():
    """The (matched, method, score) tuple shape is preserved.

    Other call sites rely on this 3-tuple. Skill 6.5 must not break them.
    """
    result = _match_in_text("123 дрона", "124 дрона")
    assert isinstance(result, tuple)
    assert len(result) == 3
    matched, method, score = result
    assert isinstance(matched, bool)
    assert method in ("exact", "fuzzy", "synonym", "num_morph", "num_mismatch", None)
    assert isinstance(score, int)


def test_legacy_exact_match_still_works():
    """Non-numeric fact still matches via exact method."""
    matched, method, score = _match_in_text(
        "Москва — столица России", "Москва — столица России с 1917 года."
    )
    assert matched is True
    assert method == "exact"
    assert score == 100
