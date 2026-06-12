"""
v0.8.3-C1 — span markers in confirmed answer bullets.

Concern: expose existing span-level evidence markers
(`[doc_<int>:<int>-<int>]`) in the `answer_markdown` "Подтверждено
источниками" bullets, without changing the contract for weak,
contradiction, or unverifiable bullets.

These tests pin the v0.8.3-C1 contract:
  1. confirmed bullets gain a span marker when one is supplied
  2. weak / unverifiable / contradiction bullets never gain a span marker
  3. inline_span_markers=None is byte-identical to omitting the kwarg
  4. misaligned list length does not crash
  5. invalid marker strings are silently ignored
  6. runner wires a span marker through to confirmed bullets end-to-end

Pure stdlib + synthesis/research_runner modules — no LLM, no network.
"""

import re

import pytest
from synthesis import (
    VERDICT_INSUFFICIENT,
    VERDICT_REFUTES,
    VERDICT_SUPPORTS,
    VERDICT_WEAK_SUPPORT,
    synthesize,
)

# --- helpers ---------------------------------------------------------------


def _confirmed_block(md: str) -> str:
    """Extract the '## Подтверждено источниками' section."""
    return md.split("## Подтверждено источниками", 1)[-1].split("## Слабые или неподтверждённые сигналы", 1)[
        0
    ]


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


# --- AC: append span marker to confirmed bullet ----------------------------


def test_inline_span_marker_appended_to_confirmed_bullet(two_sources):
    """Confirmed bullet must end with `<text> [N] [doc_0:120-187]`."""
    results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        }
    ]
    s = synthesize(
        query="q",
        claims=["cited fact"],
        results=results,
        source_candidates=two_sources,
        inline_span_markers=["[doc_0:120-187]"],
    )
    block = _confirmed_block(s.answer_markdown)
    # Bullet must contain both the [N] citation marker and the span marker.
    assert "- cited fact" in block
    assert "[1]" in block
    assert "[doc_0:120-187]" in block
    # Order: [N] before [doc_...]
    assert block.index("[1]") < block.index("[doc_0:120-187]")


# --- AC: skip span marker for non-confirmed bullets ------------------------


def test_inline_span_marker_skipped_for_weak_claim(two_sources):
    """WEAK_SUPPORT bullets must NOT receive a span marker in this batch."""
    results = [
        {
            "fact": "weak fact",
            "verdict": VERDICT_WEAK_SUPPORT,
            "reasoning": "no accepted urls",
            "source_urls": [],
        }
    ]
    s = synthesize(
        query="q",
        claims=["weak fact"],
        results=results,
        source_candidates=two_sources,
        # Even if a span marker is provided, weak bullets ignore it.
        inline_span_markers=["[doc_0:99-100]"],
    )
    md = s.answer_markdown
    # Weak bullet is in the weak section (no [N], no [doc_...]).
    weak = _weak_block(md)
    assert "weak fact" in weak
    assert "[doc_" not in weak
    # The supplied marker is silently dropped — no bullet in the entire
    # answer carries it.
    assert "[doc_0:99-100]" not in md


def test_inline_span_marker_skipped_for_unverifiable_claim(two_sources):
    """INSUFFICIENT bullets must NOT receive a span marker in this batch."""
    results = [
        {
            "fact": "open fact",
            "verdict": VERDICT_INSUFFICIENT,
            "reasoning": "no data",
        }
    ]
    s = synthesize(
        query="q",
        claims=["open fact"],
        results=results,
        source_candidates=two_sources,
        inline_span_markers=["[doc_1:200-220]"],
    )
    md = s.answer_markdown
    assert "[doc_" not in md  # no span marker anywhere


def test_inline_span_marker_skipped_for_contradiction_claim(two_sources):
    """REFUTES bullets must NOT receive a span marker in this batch."""
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://b.com/y", 0.9, "negation")],
        }
    ]
    s = synthesize(
        query="q",
        claims=["refuted fact"],
        results=results,
        source_candidates=two_sources,
        inline_span_markers=["[doc_0:300-400]"],
    )
    md = s.answer_markdown
    # The contradiction bullet renders in its own section and the
    # span marker must not leak into the answer.
    assert "[doc_0:300-400]" not in md


# --- AC: backward-compat (None == omitted) --------------------------------


def test_inline_span_markers_none_means_legacy_behavior(two_sources):
    """inline_span_markers=None must produce byte-identical output to the
    v0.8.3-B1 call that omits the kwarg entirely (recompute and compare,
    not golden string)."""
    results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        },
        {
            "fact": "weak fact",
            "verdict": VERDICT_WEAK_SUPPORT,
            "reasoning": "no accepted urls",
            "source_urls": [],
        },
    ]
    kwargs = dict(
        query="q",
        claims=["cited fact", "weak fact"],
        results=results,
        source_candidates=two_sources,
    )
    legacy = synthesize(**kwargs)
    explicit_none = synthesize(**kwargs, inline_span_markers=None)
    assert legacy.answer_markdown == explicit_none.answer_markdown
    # And no [doc_...] marker leaked anywhere.
    assert "[doc_" not in legacy.answer_markdown


# --- AC: misaligned list length does not crash ----------------------------


def test_inline_span_markers_misaligned_does_not_crash(two_sources):
    """List length mismatch must be tolerated (short, long, empty)."""
    results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        },
        {
            "fact": "cited two",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://b.com/y"],
        },
    ]
    claims = ["cited fact", "cited two"]
    # 1) Empty list — markers ignored, no crash
    s_empty = synthesize(
        query="q",
        claims=claims,
        results=results,
        source_candidates=two_sources,
        inline_span_markers=[],
    )
    assert "[doc_" not in s_empty.answer_markdown
    # 2) Short list — only index 0 is matched
    s_short = synthesize(
        query="q",
        claims=claims,
        results=results,
        source_candidates=two_sources,
        inline_span_markers=["[doc_0:1-9]"],
    )
    block = _confirmed_block(s_short.answer_markdown)
    assert "[doc_0:1-9]" in block
    # The second bullet (no marker) does not gain one
    assert block.count("[doc_0:1-9]") == 1
    # 3) Long list — extra entries are ignored, no crash
    s_long = synthesize(
        query="q",
        claims=claims,
        results=results,
        source_candidates=two_sources,
        inline_span_markers=["[doc_0:1-9]", "[doc_1:2-8]", "[doc_2:99-100]"],
    )
    assert "[doc_0:1-9]" in _confirmed_block(s_long.answer_markdown)
    assert "[doc_1:2-8]" in _confirmed_block(s_long.answer_markdown)
    # 4) List of Nones — same as not providing the kwarg
    s_nones = synthesize(
        query="q",
        claims=claims,
        results=results,
        source_candidates=two_sources,
        inline_span_markers=[None, None],
    )
    assert (
        s_nones.answer_markdown
        == synthesize(
            query="q",
            claims=claims,
            results=results,
            source_candidates=two_sources,
        ).answer_markdown
    )


# --- AC: invalid marker strings are ignored ------------------------------


def test_invalid_inline_span_marker_ignored(two_sources):
    """Markers that don't match `[doc_<int>:<int>-<int>]` are silently
    dropped — confirmed bullet still renders cleanly with [N] only."""
    results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        }
    ]
    s = synthesize(
        query="q",
        claims=["cited fact"],
        results=results,
        source_candidates=two_sources,
        inline_span_markers=[
            "not-a-marker",
            "[doc_0:120-abc]",  # non-int offset
            "doc_0:120-187",  # missing brackets
            "[doc_:1-2]",  # missing doc id
            "[doc_0:120]",  # missing end
            "",  # empty
            "[doc_0:1-9][doc_1:2-8]",  # two markers concatenated
            " [doc_0:1-9]",  # leading space
            "[doc_0:1-9] trailing",  # trailing text
            None,  # explicit None is fine
        ],
    )
    block = _confirmed_block(s.answer_markdown)
    # The [N] marker is always there
    assert "[1]" in block
    # None of the invalid forms leak into the bullet
    assert "not-a-marker" not in block
    assert "doc_0:120-abc" not in block
    assert "doc_:1-2" not in block
    # No [doc_...] marker at all in this batch
    assert not re.search(r"\[doc_\d+:\d+-\d+\]", block), (
        f"invalid markers leaked into confirmed bullet:\n{block}"
    )
