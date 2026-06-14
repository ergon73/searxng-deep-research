"""
v0.8.3-D1: provenance note in the "## Источники" section of answer_markdown.

Concern:
The dual marker format in answer_markdown — `[N]` (1-based citation
table id) and `[doc_M:start-end]` (0-based document index with char
offsets) — is well-defined but not obvious to a user reading the
output. The D0 audit (accepted) chose Option A: do not change the
marker format; instead, append a short textual note to the
"## Источники" section that explains the dual numbering, but only
when at least one span marker is actually present in the answer.

Acceptance criteria pinned here:
  1. The note appears in "## Источники" iff answer_markdown contains
     at least one `[doc_M:start-end]` span marker.
  2. The note does not alter citation table ids, the citation table
     itself, or the marker format. It is purely additive.
  3. When the runner does not pass any inline span markers (legacy
     v0.8.3-B1 contract), the answer is byte-identical to v0.8.3-B1.

These four tests cover the four sub-cases of the v0.8.3-D1 AC list:
  - present-when-marker (AC #1, AC #2 partial)
  - absent-when-no-marker (AC #1 inverse, AC #3)
  - confirmed-bullet output unchanged except the note (AC #2)
  - contradiction-bullet output unchanged except the note (AC #2)
"""
from __future__ import annotations

from synthesis import (
    VERDICT_NUMERIC_MISMATCH,
    VERDICT_REFUTES,
    VERDICT_SUPPORTS,
    synthesize,
)

# Exact wording from the D0 audit / D1 AC #1. Kept as a constant so
# the four tests share one source of truth — a future wording tweak
# (e.g. D-later for translation) updates one place.
PROVENANCE_NOTE = (
    "_Примечание: [N] — номер источника в списке ниже; "
    "[doc_M:start-end] — технический указатель на фрагмент "
    "в документе M с символьными offset-ами._"
)


# --- fixtures --------------------------------------------------------------


def _two_sources():
    return [
        {"url": "http://a.com/x", "title": "A", "text": "snippet A"},
        {"url": "http://b.com/y", "title": "B", "text": "snippet B"},
    ]


def _sources_block(md: str) -> str:
    """Return the '## Источники' section of answer_markdown, with the
    trailing empty line stripped. Used by every test in this file to
    localize assertions to the section that the D1 batch actually
    changes."""
    return md.split("## Источники", 1)[-1].rstrip("\n")


# --- AC #1: note is present when a span marker is rendered ----------------


def test_sources_section_explains_span_markers_when_present():
    """A confirmed bullet that carries a `[doc_0:120-187]` span marker
    must trigger the provenance note in the "## Источники" section.
    The note must contain the dual-numbering explanation
    (verbatim, per the D0-accepted wording)."""
    two_sources = _two_sources()
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
    md = s.answer_markdown
    # Sanity: the span marker really was rendered into a confirmed
    # bullet — otherwise this test could pass on an empty branch.
    assert "[doc_0:120-187]" in md
    sources = _sources_block(md)
    assert PROVENANCE_NOTE in sources, (
        f"provenance note missing in ## Источники when span marker "
        f"is present, got section:\n{sources!r}"
    )
    # The note must be in the same section as the citation table
    # (i.e. below "## Источники", not somewhere else).
    assert md.index("## Источники") < md.index(PROVENANCE_NOTE)


# --- AC #2: note is absent when no span marker is present -----------------


def test_sources_section_omits_span_marker_note_when_no_markers():
    """When the runner does not pass `inline_span_markers` (or any
    `inline_contradiction_markers`), no `[doc_M:start-end]` marker
    reaches the markdown and the provenance note must NOT be
    appended. The answer_markdown is byte-identical to v0.8.3-B1
    for this case (BC for legacy callers)."""
    two_sources = _two_sources()
    results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        }
    ]
    # Two call shapes must both produce no note: omitted kwargs
    # (legacy) and explicit None (BC-defensive).
    legacy = synthesize(
        query="q",
        claims=["cited fact"],
        results=results,
        source_candidates=two_sources,
    )
    explicit_none = synthesize(
        query="q",
        claims=["cited fact"],
        results=results,
        source_candidates=two_sources,
        inline_span_markers=None,
        inline_contradiction_markers=None,
    )
    for label, md in (("legacy", legacy.answer_markdown), ("explicit_none", explicit_none.answer_markdown)):
        assert "[doc_" not in md, f"{label}: span marker leaked where none expected"
        assert PROVENANCE_NOTE not in md, (
            f"{label}: provenance note appended even though no span "
            f"marker was rendered:\n{md!r}"
        )
    # And legacy / explicit_none must be byte-identical (BC pin from
    # the v0.8.3-C1 test suite, repeated here against the D1 change).
    assert legacy.answer_markdown == explicit_none.answer_markdown


# --- AC #3: confirmed-bullet marker behaviour preserved -------------------


def test_existing_confirmed_span_marker_output_unchanged_except_note():
    """The v0.8.3-C1 contract — confirmed bullets carry `[N] [doc_M:start-end]`
    in that order — is preserved verbatim. The only thing the D1
    batch adds is the provenance note in the "## Источники" section;
    no bullet, no citation id, no marker shape changes."""
    two_sources = _two_sources()
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
    md = s.answer_markdown
    # Confirmed block (everything between "## Подтверждено источниками"
    # and "## Слабые или неподтверждённые сигналы") must be the
    # pre-D1 C1 output byte-for-byte.
    confirmed = md.split("## Подтверждено источниками", 1)[-1].split(
        "## Слабые или неподтверждённые сигналы", 1
    )[0]
    assert "- cited fact" in confirmed
    assert "[1]" in confirmed
    assert "[doc_0:120-187]" in confirmed
    # Order: [N] before [doc_...] (same as the C1 contract).
    assert confirmed.index("[1]") < confirmed.index("[doc_0:120-187]")
    # The only D1-induced delta is the provenance note, which lives
    # in the "## Источники" section, NOT in the confirmed block.
    assert PROVENANCE_NOTE not in confirmed
    # The note lives in the sources section, after the citation
    # table list.
    sources = _sources_block(md)
    assert PROVENANCE_NOTE in sources


# --- AC #4: contradiction-bullet marker behaviour preserved ---------------


def test_existing_contradiction_span_marker_output_unchanged_except_note():
    """The v0.8.3-C3 contract — contradiction bullets carry
    `[M] [doc_N:start-end]` in that order, where `[M]` is the
    1-based citation table id for the refuting source — is
    preserved verbatim. The D1 batch adds only the provenance note
    in "## Источники"; no contradiction bullet, citation id, or
    marker shape changes."""
    two_sources = _two_sources()
    results = [
        {
            "fact": "refuted fact",
            "verdict": VERDICT_REFUTES,
            "reasoning": "neg",
            "refuting_sources": [("http://a.com/x", 0.9, "neg")],
        }
    ]
    s = synthesize(
        query="q",
        claims=["refuted fact"],
        results=results,
        source_candidates=two_sources,
        inline_contradiction_markers=["[doc_0:8-19]"],
    )
    md = s.answer_markdown
    # Contradiction block: span marker + citation marker preserved.
    contradiction = md.split("## Противоречия / расхождения", 1)[-1].split(
        "## Что не удалось проверить", 1
    )[0]
    assert "refuted fact" in contradiction
    assert "[1]" in contradiction  # citation table marker for a.com
    assert "[doc_0:8-19]" in contradiction  # 0-based span marker
    # Order: [N] before [doc_...] (C3 contract).
    assert contradiction.index("[1]") < contradiction.index("[doc_0:8-19]")
    # Note is NOT in the contradiction block; it lives in "## Источники".
    assert PROVENANCE_NOTE not in contradiction
    # And it IS in the sources section.
    sources = _sources_block(md)
    assert PROVENANCE_NOTE in sources

    # Also check the NUMERIC_MISMATCH route, which uses the same
    # contradiction bullet rendering but a different verdict label.
    nm_results = [
        {
            "fact": "5 дронов сбиты",
            "verdict": VERDICT_NUMERIC_MISMATCH,
            "reasoning": "diff",
            "numeric_mismatch_sources": [
                ("http://b.com/y", 0.85, "num_mismatch"),
            ],
        }
    ]
    s_nm = synthesize(
        query="q",
        claims=["5 дронов сбиты"],
        results=nm_results,
        source_candidates=two_sources,
        inline_contradiction_markers=["[doc_1:42-58]"],
    )
    md_nm = s_nm.answer_markdown
    assert "[doc_1:42-58]" in md_nm
    assert "[2]" in md_nm  # b.com is the second source
    assert PROVENANCE_NOTE in _sources_block(md_nm)


# --- v0.8.4-A1: false-positive guard for non-marker [doc_ prose ------------


def test_provenance_note_not_triggered_by_non_marker_doc_like_text():
    """A bullet whose text contains a `[doc_...`-looking substring
    that is **not** a well-formed `[doc_<int>:<int>-<int>]` marker
    must NOT trigger the provenance note in `## Источники`.

    The v0.8.3-D1 implementation used a raw `"[doc_" in b` substring
    check to decide whether the note is needed. That check fires on
    **any** bullet that contains the literal `[doc_` — including a
    URL like `http://a.com/[doc_fake]/x` whose `[doc_fake]` fragment
    survives the `_md_escape` pass (URLs are not escaped in the
    citation table). The v0.8.4-A1 batch replaced the substring check
    with a validated regex search (`_SPAN_MARKER_RE.search(b)`), which
    only matches a well-formed `[doc_<int>:<int>-<int>]`. This test
    pins the false-positive fix: prose-only bullets (no real marker
    rendered) must NOT produce a note.

    Companion positive case (AC #3): when a real marker IS rendered,
    the note must still appear. Both shapes are exercised in the same
    test so a regression in either direction shows up here.
    """
    two_sources = _two_sources()

    # --- negative: URL contains [doc_fake] but no real marker ---
    # When a source_candidate URL contains the literal `[doc_fake]`,
    # the citation table renders the URL as `[A](http://a.com/[doc_fake]/x)`
    # — note that `_md_escape` is NOT applied to URLs (it only escapes
    # the title, see `src/synthesis.py:830`). So the substring `[doc_`
    # survives in the rendered markdown, and the OLD substring check
    # would falsely fire on it. The NEW regex check rejects the
    # literal (no digit run + no `:digit-digit` pattern after `[doc_`)
    # and the note is suppressed.
    two_sources_prose = [
        {"url": "http://a.com/[doc_fake]/x", "title": "A", "text": "snippet A"},
        {"url": "http://b.com/y", "title": "B", "text": "snippet B"},
    ]
    prose_results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/[doc_fake]/x"],
        }
    ]
    s_prose = synthesize(
        query="q",
        claims=["cited fact"],
        results=prose_results,
        source_candidates=two_sources_prose,
        # No inline_span_markers. The URL's `[doc_fake]` survives
        # in the rendered citation table, putting the substring
        # `[doc_` into the markdown without a digit run following it.
    )
    md_prose = s_prose.answer_markdown
    # Sanity: the URL with `[doc_fake]` is in the markdown, and the
    # substring is there (so the test would have caught a real
    # false positive in the old substring-check code).
    assert "http://a.com/[doc_fake]/x" in md_prose, (
        f"prose URL with [doc_fake] missing from answer_markdown:"
        f"\n{md_prose!r}"
    )
    assert "[doc_" in md_prose, (
        f"substring '[doc_' is absent — the false-positive scenario "
        f"isn't actually being exercised, got:\n{md_prose!r}"
    )
    # The actual fix: the note must NOT be in the sources section.
    assert PROVENANCE_NOTE not in _sources_block(md_prose), (
        f"provenance note was triggered by non-marker '[doc_' URL "
        f"fragment (regression of v0.8.3-D1 substring check). "
        f"answer_markdown:\n{md_prose!r}"
    )
    # The whole markdown must also be free of the note.
    assert PROVENANCE_NOTE not in md_prose

    # --- positive: a real [doc_<int>:<int>-<int>] marker renders ---
    real_results = [
        {
            "fact": "cited fact",
            "verdict": VERDICT_SUPPORTS,
            "reasoning": "ok",
            "source_urls": ["http://a.com/x"],
        }
    ]
    s_real = synthesize(
        query="q",
        claims=["cited fact"],
        results=real_results,
        source_candidates=two_sources,
        inline_span_markers=["[doc_0:120-187]"],
    )
    md_real = s_real.answer_markdown
    # Sanity: the real marker is rendered.
    assert "[doc_0:120-187]" in md_real
    # The note MUST appear in the sources section — proves the regex
    # check is not over-strict (would otherwise be a false negative).
    assert PROVENANCE_NOTE in _sources_block(md_real), (
        f"real span marker did NOT trigger the provenance note — "
        f"regression of v0.8.4-A1 regex-strict detection. "
        f"answer_markdown:\n{md_real!r}"
    )
