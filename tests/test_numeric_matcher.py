"""Tests for v0.8.2-A numeric matcher scan-all-occurrences fix.

Regression test for the early-return bug in _match_in_text() numeric
branch: previously the matcher returned (True, "num_mismatch", 85) on
the first same-stem-different-number occurrence, never reaching a
later correct same-number occurrence in the same text.

For fact "22 БПЛА" vs "23 беспилотника ... 22 беспилотника" the matcher
would return num_mismatch even though a perfect match exists later.

Bug reported by external review 2026-06-09 (recommendation file
`hermes-recomendation-09062026(7).txt`, section 4).
"""

import sys
from pathlib import Path

import pytest

# Ensure src/ is on sys.path (conftest already does this; be explicit for
# direct test execution: `pytest tests/test_numeric_matcher.py`).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ----------------------------------------------------------- unit tests


def test_numeric_same_number_same_stem_supports():
    """22 дрона vs "Сбито 22 дронов" → num_morph (same number, same stem)."""
    from hermes_deepresearch import _match_in_text

    result = _match_in_text("22 дрона", "Сбито 22 дронов")
    assert result[:2] == (True, "num_morph"), (
        f"expected (True, 'num_morph'), got {result[:2]}"
    )


def test_numeric_same_number_synonym_supports_bpla_bespilotnik():
    """22 БПЛА vs "Сбито 22 беспилотника" → num_morph (synonym stem)."""
    from hermes_deepresearch import _match_in_text

    result = _match_in_text("22 БПЛА", "Сбито 22 беспилотника")
    assert result[:2] == (True, "num_morph"), (
        f"expected (True, 'num_morph'), got {result[:2]}"
    )


def test_numeric_different_number_same_stem_mismatch():
    """22 дрона vs "Сбито 23 дрона" → num_mismatch (different number, no later match)."""
    from hermes_deepresearch import _match_in_text

    result = _match_in_text("22 дрона", "Сбито 23 дрона")
    assert result[:2] == (True, "num_mismatch"), (
        f"expected (True, 'num_mismatch'), got {result[:2]}"
    )


def test_numeric_later_same_number_beats_earlier_mismatch():
    """PRIMARY REGRESSION TEST.

    22 БПЛА vs "Утром 23 беспилотника. Позже 22 беспилотника." → num_morph.
    Before fix: returned num_mismatch on first 23.
    After fix: scans all, finds 22 later, returns num_morph.
    """
    from hermes_deepresearch import _match_in_text

    matched, method, score = _match_in_text(
        "22 БПЛА",
        "Утром обнаружили 23 беспилотника. Позже сбили 22 беспилотника.",
    )
    assert matched is True
    assert method == "num_morph", (
        f"expected 'num_morph' (later same-number wins), got '{method}'"
    )


def test_numeric_same_number_before_later_mismatch_still_supports():
    """22 БПЛА vs "Сбито 22 беспилотника. Всего обнаружили 23 беспилотника." → num_morph.

    The earlier same-number match returns immediately; the later
    different-number is not reached. Should be num_morph.
    """
    from hermes_deepresearch import _match_in_text

    matched, method, score = _match_in_text(
        "22 БПЛА",
        "Сбито 22 беспилотника. Всего за сутки обнаружили 23 беспилотника.",
    )
    assert matched is True
    assert method == "num_morph"


# ------------------------------------------------------- integration test


def test_verify_sources_numeric_later_support_beats_earlier_mismatch():
    """Integration: verify_sources should classify fact as SUPPORTS when
    one of the supporting sources contains the same number (even if
    another source had a different number first)."""
    from hermes_deepresearch import verify_sources

    top1 = {
        "url": "https://top.example",
        "title": "top",
        "text": "Сообщается, что сбито 22 БПЛА.",
        "error": None,
    }
    other = [{
        "url": "https://other.example",
        "title": "other",
        "text": "Утром обнаружили 23 беспилотника. Позже сбили 22 беспилотника.",
        "error": None,
    }]

    out = verify_sources(top1, other, "22 БПЛА", use_llm=False, max_facts=5)

    # The '22 БПЛА' fact from top1 should find a SUPPORTS verdict via the
    # 'other' source (which has both 23 and 22 occurrences; the later 22
    # match beats the earlier 23 mismatch after the v0.8.2-A fix).
    details = out.get("verification_details") or out.get("details") or []
    has_22_supports = any(
        "22" in d.get("fact", "")
        and d.get("verdict") == "SUPPORTS"
        for d in details
    )
    assert has_22_supports, (
        f"expected SUPPORTS verdict for '22 БПЛА' fact via later same-number "
        f"match in 'other' source, but verification_details={details}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
