"""
v0.8.3-C3: render contradiction/refutation span markers in answer_markdown.

Concern:
The C2-data batch added `refuting_evidence_windows` and
`numeric_mismatch_evidence_windows` to each `verification_details[i]`
dict, but did NOT render anything in `answer_markdown`. C3 surfaces
those windows as `[doc_N:start-end]` markers in the
"Противоречия / расхождения" section of `answer_markdown`.

The marker format mirrors v0.8.3-C1 confirmed-bullet markers, with
the same C1b / C1c no-fabrication guarantee: a marker is only
appended when the runner resolved `window.source_url` to a real
document index via `_doc_index_for_window`. No `[doc_0:*]` fallback.

Acceptance criteria pinned here:
  1. REFUTES with a resolvable refuting_evidence_windows → span marker.
  2. NUMERIC_MISMATCH with a resolvable numeric_mismatch_evidence_windows
     → span marker.
  3. No span marker when no window exists.
  4. No span marker when the window's source_url cannot be resolved.
  5. WEAK_SUPPORT / INSUFFICIENT never get a contradiction span marker.
  6. Existing C1 confirmed-bullet marker behavior is unchanged.
"""
from __future__ import annotations

import pytest
from research_runner import _build_contradiction_markers
from synthesis import (
    VERDICT_CONFLICTING,
    VERDICT_NUMERIC_MISMATCH,
    VERDICT_REFUTES,
    VERDICT_SUPPORTS,
    VERDICT_WEAK_SUPPORT,
    synthesize,
)

# --- helpers ---------------------------------------------------------------


def _contradiction_block(md: str) -> str:
    """Extract the '## Противоречия / расхождения' section of answer_markdown."""
    return md.split("## Противоречия / расхождения", 1)[-1].split(
        "## Что не удалось проверить", 1
    )[0]


def _confirmed_block(md: str) -> str:
    return md.split("## Подтверждено источниками", 1)[-1].split(
        "## Слабые или неподтверждённые сигналы", 1
    )[0]


def _weak_block(md: str) -> str:
    return md.split("## Слабые или неподтверждённые сигналы", 1)[-1].split(
        "## Противоречия / расхождения", 1
    )[0]


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def two_sources():
    return [
        {"url": "http://a.com/x", "title": "A", "text": "snippet A"},
        {"url": "http://b.com/y", "title": "B", "text": "snippet B"},
    ]


# --- AC #1: REFUTES with refuting_evidence_windows -> span marker ---------


def test_refutes_with_evidence_window_renders_span_marker(two_sources):
    """REFUTES + refuting_evidence_windows[0] with a real source_url →
    the contradiction bullet ends with `[doc_0:8-19]` (matching
    documents[0]) plus the citation marker [1] for the same URL."""
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "negation",
            "refuting_sources": [("http://a.com/x", 0.9, "negation")],
            "refuting_evidence_windows": [
                {
                    "source_url": "http://a.com/x",
                    "quote": "не было сбито",
                    "offset_start": 8,
                    "offset_end": 19,
                    "method": "negation_after",
                }
            ],
        }
    ]
    s = synthesize(
        query="q",
        claims=["refuted fact"],
        results=results,
        source_candidates=two_sources,
        inline_contradiction_markers=["[doc_0:8-19]"],
    )
    block = _contradiction_block(s.answer_markdown)
    # Both the citation marker [1] and the span marker [doc_0:8-19]
    # are present, in that order.
    assert "refuted fact" in block
    assert "[1]" in block
    assert "[doc_0:8-19]" in block
    assert block.index("[1]") < block.index("[doc_0:8-19]")


# --- AC #2: NUMERIC_MISMATCH with numeric_mismatch_evidence_windows ------


def test_numeric_mismatch_with_evidence_window_renders_span_marker(
    two_sources,
):
    """NUMERIC_MISMATCH + numeric_mismatch_evidence_windows[0] resolvable
    → the contradiction bullet ends with the citation marker for
    http://b.com/y and the span marker [doc_1:42-58] (doc[1] is
    http://b.com/y in the fixture)."""
    results = [
        {
            "fact": "5 дронов сбиты",
            "verdict": VERDICT_NUMERIC_MISMATCH,
            "reasoning": "diff number",
            "numeric_mismatch_sources": [
                ("http://b.com/y", 0.85, "num_mismatch"),
            ],
            "numeric_mismatch_evidence_windows": [
                {
                    "source_url": "http://b.com/y",
                    "quote": "7 дронов",
                    "offset_start": 42,
                    "offset_end": 58,
                    "method": "num_mismatch",
                }
            ],
        }
    ]
    s = synthesize(
        query="q",
        claims=["5 дронов сбиты"],
        results=results,
        source_candidates=two_sources,
        inline_contradiction_markers=["[doc_1:42-58]"],
    )
    block = _contradiction_block(s.answer_markdown)
    assert "5 дронов сбиты" in block
    # http://b.com/y is the second source in the fixture (id=2 in the
    # citation table), so its citation marker is [2].
    assert "[2]" in block
    assert "[doc_1:42-58]" in block
    assert block.index("[2]") < block.index("[doc_1:42-58]")


# --- AC #3 / #4: no window / unmatched URL → no span marker --------------


def test_refutes_without_window_has_no_span_marker(two_sources):
    """REFUTES without any refuting_evidence_windows → no `[doc_*:*]`
    appears in the contradiction block."""
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "negation",
            "refuting_sources": [("http://a.com/x", 0.9, "negation")],
            # intentionally: no refuting_evidence_windows
        }
    ]
    s = synthesize(
        query="q",
        claims=["refuted fact"],
        results=results,
        source_candidates=two_sources,
        # No inline_contradiction_markers at all → behavior identical
        # to legacy (URL-only [1] marker).
    )
    block = _contradiction_block(s.answer_markdown)
    assert "refuted fact" in block
    assert "[1]" in block  # citation table marker preserved
    assert "[doc_" not in block, (
        f"no span marker expected when window is absent, got block:\n{block!r}"
    )


def test_unmatched_refuting_window_url_has_no_span_marker(two_sources):
    """REFUTES + refuting_evidence_windows with an URL NOT in
    source_candidates → runner-side `_doc_index_for_window` returns
    None, so the marker is None. Synthesis must not fabricate
    `[doc_0:*]`."""
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://orphan.example/z", 0.9, "neg")],
            "refuting_evidence_windows": [
                {
                    "source_url": "http://orphan.example/z",
                    "quote": "не было сбито",
                    "offset_start": 0,
                    "offset_end": 12,
                    "method": "negation_after",
                }
            ],
        }
    ]
    # Simulate the runner contract: when the window's source_url is
    # unmatched, _build_contradiction_markers returns None at index 0.
    s = synthesize(
        query="q",
        claims=["refuted fact"],
        results=results,
        source_candidates=two_sources,
        inline_contradiction_markers=[None],  # runner-supplied None
    )
    block = _contradiction_block(s.answer_markdown)
    assert "refuted fact" in block
    # No span marker in this batch (citation [1] may or may not appear
    # because the URL is also not in the citation table).
    assert "[doc_" not in block, (
        f"unmatched source_url must NOT produce a span marker, got: {block!r}"
    )


# --- AC #5: WEAK_SUPPORT does NOT get a refuting span marker --------------


def test_weak_support_does_not_get_refuting_span_marker(two_sources):
    """WEAK_SUPPORT bullets are routed to the WEAK section (not the
    contradiction section). Even if a malicious caller passes
    inline_contradiction_markers[i] for a WEAK_SUPPORT fact, the
    marker must NOT be appended to the weak bullet and must NOT
    appear in the contradiction block either."""
    results = [
        {
            "fact": "weak fact",
            "verdict": VERDICT_WEAK_SUPPORT,
            "reasoning": "uncited",
            "source_urls": [],
        }
    ]
    s = synthesize(
        query="q",
        claims=["weak fact"],
        results=results,
        source_candidates=two_sources,
        # Even if we accidentally pass a marker, it must be ignored
        # for WEAK_SUPPORT (the contradiction-section rendering only
        # fires when route == "contradiction").
        inline_contradiction_markers=["[doc_0:99-100]"],
    )
    weak = _weak_block(s.answer_markdown)
    contradiction = _contradiction_block(s.answer_markdown)
    # WEAK_SUPPORT bullet is in the weak section with no span marker.
    assert "weak fact" in weak
    assert "[doc_" not in weak
    # The supplied marker is silently dropped (contradiction bullet
    # is the empty placeholder, not the marker).
    assert "[doc_0:99-100]" not in s.answer_markdown, (
        f"WEAK_SUPPORT must not surface contradiction markers, got:\n"
        f"{s.answer_markdown!r}"
    )
    # The contradiction block shows the `_нет_` placeholder.
    assert "_нет_" in contradiction


# --- AC #6: existing C1 confirmed-bullet marker behavior unchanged -------


def test_confirmed_support_span_marker_still_works(two_sources):
    """The v0.8.3-C1 contract — confirmed bullets get inline_span_markers
    and contradiction bullets must NOT get them when only
    inline_span_markers is passed — is preserved. This is a regression
    test, not a new C3 behaviour."""
    results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        },
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://a.com/x", 0.9, "neg")],
        },
    ]
    s = synthesize(
        query="q",
        claims=["cited fact", "refuted fact"],
        results=results,
        source_candidates=two_sources,
        # Only inline_span_markers passed — no contradiction markers.
        inline_span_markers=["[doc_0:120-187]", None],
    )
    confirmed = _confirmed_block(s.answer_markdown)
    contradiction = _contradiction_block(s.answer_markdown)
    # C1 contract: confirmed bullet has the span marker.
    assert "[doc_0:120-187]" in confirmed
    # C1 contract: contradiction bullet has NO span marker (C1 test
    # was about NOT surfacing them; C3 introduces a new opt-in path).
    assert "[doc_" not in contradiction


# --- Byte-identical contract: no markers kwarg = no change ----------------


def test_no_contradiction_markers_kwarg_is_unchanged(two_sources):
    """When inline_contradiction_markers is not passed (default None),
    the contradiction block is byte-identical to v0.8.3-B1 — no span
    marker is added even if the runner-supplied window exists in
    `results[i]["refuting_evidence_windows"]`."""
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://a.com/x", 0.9, "neg")],
            "refuting_evidence_windows": [
                {
                    "source_url": "http://a.com/x",
                    "quote": "не было сбито",
                    "offset_start": 8,
                    "offset_end": 19,
                    "method": "negation_after",
                }
            ],
        }
    ]
    s = synthesize(
        query="q",
        claims=["refuted fact"],
        results=results,
        source_candidates=two_sources,
        # inline_contradiction_markers NOT passed.
    )
    block = _contradiction_block(s.answer_markdown)
    assert "refuted fact" in block
    assert "[1]" in block
    assert "[doc_" not in block


# --- Defensive: misaligned list length is tolerated ----------------------


def test_contradiction_markers_misaligned_length_does_not_crash(two_sources):
    """Mirrors the C1 defensive test: a list shorter than `results`
    must not raise, and out-of-range entries must be silently dropped."""
    results = [
        {
            "fact": "r1",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://a.com/x", 0.9, "neg")],
        },
        {
            "fact": "r2",
            "verdict": VERDICT_NUMERIC_MISMATCH,
            "reasoning": "diff",
            "numeric_mismatch_sources": [("http://b.com/y", 0.85, "num_mismatch")],
        },
    ]
    # Shorter than results — entry for the second result is out of
    # range and must be silently dropped.
    s = synthesize(
        query="q",
        claims=["r1", "r2"],
        results=results,
        source_candidates=two_sources,
        inline_contradiction_markers=["[doc_0:8-19]"],
    )
    block = _contradiction_block(s.answer_markdown)
    # First fact's marker is rendered; the second is not.
    assert "[doc_0:8-19]" in block
    # No fabricated [doc_1:...] for the second fact.
    assert "[doc_1:" not in block


# ===========================================================================
# v0.8.3-C3b: verdict-specific window selection
# ===========================================================================
#
# The C3 helper initially pooled both `refuting_evidence_windows` and
# `numeric_mismatch_evidence_windows` into a single candidate list per
# fact, so a REFUTES fact could pick a numeric-mismatch window and vice
# versa. C3b splits the candidate list by verdict:
#
#   * REFUTES  → refuting_evidence_windows only.
#   * NUMERIC_MISMATCH → numeric_mismatch_evidence_windows only.
#   * CONFLICTING → refuting_evidence_windows first, then
#     numeric_mismatch_evidence_windows as fallback.
#
# These four tests pin the contract at the helper level (no synthesis
# indirection) so a regression in the selection logic cannot hide
# behind a runner-supplied None.


def _refuting_window(url: str, start: int, end: int) -> dict:
    return {
        "source_url": url,
        "quote": "не было сбито",
        "offset_start": start,
        "offset_end": end,
        "method": "negation_after",
    }


def _mismatch_window(url: str, start: int, end: int) -> dict:
    return {
        "source_url": url,
        "quote": "7 дронов",
        "offset_start": start,
        "offset_end": end,
        "method": "num_mismatch",
    }


# --- AC #1: REFUTES ignores numeric_mismatch_evidence_windows ---------------


def test_refutes_ignores_numeric_mismatch_window(two_sources):
    """REFUTES fact whose `refuting_evidence_windows` is empty /
    unresolved MUST NOT pick up a `numeric_mismatch_evidence_windows`
    entry. The helper returns None, not a [doc_N:start-end] marker.

    Pre-C3b the helper pooled both lists, so a present-but-orphan
    numeric_mismatch_evidence_windows could leak into a REFUTES
    bullet. C3b forbids that leak.
    """
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://a.com/x", 0.9, "neg")],
            # Refuting list present but empty / non-resolving.
            "refuting_evidence_windows": [],
            # Numeric-mismatch list IS resolvable — the trap the C3b
            # contract closes. REFUTES must NOT touch it.
            "numeric_mismatch_evidence_windows": [
                _mismatch_window("http://a.com/x", 42, 58),
            ],
        }
    ]
    out = _build_contradiction_markers(results, two_sources)
    assert out == [None], (
        f"REFUTES must ignore numeric_mismatch_evidence_windows; got {out!r}"
    )


# --- AC #2: NUMERIC_MISMATCH ignores refuting_evidence_windows ---------------


def test_numeric_mismatch_ignores_refuting_window(two_sources):
    """NUMERIC_MISMATCH fact whose `numeric_mismatch_evidence_windows`
    is empty / unresolved MUST NOT pick up a
    `refuting_evidence_windows` entry. The helper returns None."""
    results = [
        {
            "fact": "5 дронов сбиты",
            "verdict": VERDICT_NUMERIC_MISMATCH,
            "reasoning": "diff",
            "numeric_mismatch_sources": [
                ("http://b.com/y", 0.85, "num_mismatch"),
            ],
            # Mismatch list present but empty / non-resolving.
            "numeric_mismatch_evidence_windows": [],
            # Refuting list IS resolvable — the symmetric trap. NUMERIC_MISMATCH
            # must NOT touch it.
            "refuting_evidence_windows": [
                _refuting_window("http://b.com/y", 8, 19),
            ],
        }
    ]
    out = _build_contradiction_markers(results, two_sources)
    assert out == [None], (
        f"NUMERIC_MISMATCH must ignore refuting_evidence_windows; got {out!r}"
    )


# --- AC #3: CONFLICTING can use refuting_evidence_windows -------------------


def test_conflicting_can_use_refuting_window(two_sources):
    """CONFLICTING fact resolves via `refuting_evidence_windows`
    when the first refuting window is resolvable. The helper emits
    `[doc_0:8-19]` for the first resolvable refuting window — even
    if a numeric_mismatch_evidence_windows entry would also be
    resolvable, the refuting side wins (CONFLICTING prefers refuting)."""
    results = [
        {
            "fact": "both-sides fact",
            "verdict": VERDICT_CONFLICTING,
            "reasoning": "mixed",
            "refuting_sources": [("http://a.com/x", 0.9, "neg")],
            "numeric_mismatch_sources": [
                ("http://b.com/y", 0.85, "num_mismatch"),
            ],
            "refuting_evidence_windows": [
                _refuting_window("http://a.com/x", 8, 19),
            ],
            "numeric_mismatch_evidence_windows": [
                _mismatch_window("http://b.com/y", 42, 58),
            ],
        }
    ]
    out = _build_contradiction_markers(results, two_sources)
    assert out == ["[doc_0:8-19]"], (
        f"CONFLICTING must prefer refuting side when it resolves; got {out!r}"
    )


# --- AC #4: CONFLICTING falls back to numeric_mismatch when refuting missing -


def test_conflicting_falls_back_to_numeric_mismatch_window_when_refuting_missing(
    two_sources,
):
    """CONFLICTING fact with empty / missing
    `refuting_evidence_windows` falls back to
    `numeric_mismatch_evidence_windows`. The mismatch side is the
    only available evidence window and the helper emits its marker."""
    results = [
        {
            "fact": "mismatch-only conflicting fact",
            "verdict": VERDICT_CONFLICTING,
            "reasoning": "mixed",
            "numeric_mismatch_sources": [
                ("http://b.com/y", 0.85, "num_mismatch"),
            ],
            # Refuting list is empty → CONFLICTING must fall through
            # to the mismatch list and pick the first resolvable
            # entry.
            "refuting_evidence_windows": [],
            "numeric_mismatch_evidence_windows": [
                _mismatch_window("http://b.com/y", 42, 58),
            ],
        }
    ]
    out = _build_contradiction_markers(results, two_sources)
    assert out == ["[doc_1:42-58]"], (
        f"CONFLICTING with empty refuting list must fall back to "
        f"numeric_mismatch_evidence_windows; got {out!r}"
    )
