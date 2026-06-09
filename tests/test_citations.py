"""
Tests for span-level citations (Phase 4, v0.8.0, #019).

What we verify:
- find_span(): direct match, whitespace-normalized fallback, fuzzy prefix,
  miss returns (-1, -1). Edge cases: empty claim, empty text, unicode.
- build_evidence_window(): produces a window with correct offsets, None on miss.
- format_cited_claim(): emits `[doc_N:start-end]` markers; passes through
  uncited claims unchanged.
- citation_stats(): counts cited/uncited/stub and reports coverage.
- assert_citations_complete(): enforces invariant (raises on uncited non-stub).

All tests are offline — no network, no LLM.
"""
import pytest
from citations import (
    _CITATION_RE,
    _normalize_with_map,
    _normalize_ws,
    assert_citations_complete,
    build_evidence_window,
    citation_stats,
    find_span,
    format_cited_claim,
)
from evidence import EvidenceWindow
from models import Claim

# ============================================================
# find_span
# ============================================================

class TestFindSpanDirect:
    """Case 1: direct substring search."""

    def test_direct_match_at_start(self):
        c = Claim(text="привет")
        s, e = find_span(c, "привет мир")
        assert (s, e) == (0, 6)

    def test_direct_match_in_middle(self):
        c = Claim(text="5 июня 2026")
        s, e = find_span(c, "Сегодня 5 июня 2026 года.")
        assert (s, e) == (8, 19)
        assert "Сегодня 5 июня 2026 года."[s:e] == "5 июня 2026"

    def test_direct_match_at_end(self):
        c = Claim(text="событие")
        s, e = find_span(c, "произошло событие")
        assert (s, e) == (10, 17)

    def test_returns_exclusive_end(self):
        """Python-style: text[start:end] == claim.text."""
        c = Claim(text="abc")
        s, e = find_span(c, "xabcx")
        assert (s, e) == (1, 4)


class TestFindSpanNormalized:
    """Case 2: whitespace-normalized fallback."""

    def test_collapses_multiple_spaces(self):
        c = Claim(text="5 июня 2026")
        original = "Сегодня  5   июня    2026  года."
        s, e = find_span(c, original)
        # v0.8.1.1: offsets are in ORIGINAL space, not normalized. So
        # evidence_text[s:e] is a slice of the original document that
        # covers the claim (with extra whitespace preserved as in source).
        assert s >= 0
        assert e > s
        # The original-space span must include the claim's characters
        # in order, even if the slice has extra whitespace.
        span_text = original[s:e]
        assert "5" in span_text and "июня" in span_text and "2026" in span_text
        # And the slice must be longer than the normalized claim because
        # of the extra spaces (2-4 spaces collapsed to 1 in normalized).
        assert e - s > len(_normalize_ws(c.text))

    def test_collapses_newlines(self):
        c = Claim(text="5 июня 2026")
        original = "Сегодня\n5\nиюня\n2026\nгода."
        s, e = find_span(c, original)
        assert s >= 0
        assert e > s
        # v0.8.1.1: offsets in original space; the slice contains
        # the embedded newlines from the source.
        span_text = original[s:e]
        assert "5" in span_text and "июня" in span_text and "2026" in span_text
        # Critical invariant: the slice is a substring of the ORIGINAL.
        # We verify by reconstructing the slice and checking the chars at
        # the start/end offsets match the claim boundaries.
        assert original[s] == "5"   # first char of the claim in original
        assert original[e - 1] == "6"  # last char of "2026" in original

    def test_collapses_tabs(self):
        c = Claim(text="foo bar")
        original = "before\tfoo\t\tbar\tend"
        s, e = find_span(c, original)
        assert s >= 0
        assert e > s
        # v0.8.1.1: original-space offsets — span is the slice
        # "foo\t\tbar" (with tabs preserved) or similar.
        span_text = original[s:e]
        assert "foo" in span_text and "bar" in span_text
        # The slice must be longer than "foo bar" because of the
        # embedded tabs (collapsed in normalized, but here preserved).
        assert e - s > len("foo bar")


class TestFindSpanFuzzyPrefix:
    """Case 3: prefix search (first 30 chars) when whole string fails."""

    def test_partial_overlap_returns_prefix_span(self):
        c = Claim(text="Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor")
        text = "Different content. Then: Lorem ipsum dolor sit amet, consectetur adipiscing"
        s, e = find_span(c, text)
        assert s >= 0
        # e should be at least s + len(claim.text) (best-effort)
        assert e > s

    def test_short_claim_skips_fuzzy(self):
        """Claims shorter than 10 chars: no fuzzy prefix search."""
        c = Claim(text="abc")
        text = "no match here"
        # Direct fails, normalized fails, fuzzy skipped (len < 10)
        s, e = find_span(c, text)
        assert (s, e) == (-1, -1)


class TestFindSpanMiss:
    """Miss cases."""

    def test_no_match_returns_negatives(self):
        c = Claim(text="привет")
        s, e = find_span(c, "другой текст")
        assert (s, e) == (-1, -1)

    def test_empty_claim_returns_negatives(self):
        c = Claim(text="")
        s, e = find_span(c, "some text")
        assert (s, e) == (-1, -1)

    def test_empty_text_returns_negatives(self):
        c = Claim(text="foo")
        s, e = find_span(c, "")
        assert (s, e) == (-1, -1)

    def test_both_empty_returns_negatives(self):
        c = Claim(text="")
        s, e = find_span(c, "")
        assert (s, e) == (-1, -1)

    def test_unicode_claim_works(self):
        c = Claim(text="Магнитная буря")
        s, e = find_span(c, "Уровень: Магнитная буря достигла 5 Гц")
        assert "Уровень: Магнитная буря достигла 5 Гц"[s:e] == "Магнитная буря"


# ============================================================
# build_evidence_window
# ============================================================

class TestBuildEvidenceWindow:
    """Build an EvidenceWindow from a claim + document."""

    def test_returns_window_with_correct_offsets(self):
        c = Claim(text="5 июня 2026")
        doc = {
            "url": "https://example.com/news",
            "title": "Новости",
            "text": "Сегодня 5 июня 2026 года произошло событие.",
            "score": 0.95,
        }
        w = build_evidence_window(c, doc)
        assert w is not None
        assert w.offset_start == 8
        assert w.offset_end == 19
        assert w.source_url == "https://example.com/news"
        assert w.source_title == "Новости"
        assert w.score == 0.95

    def test_returns_none_on_miss(self):
        c = Claim(text="нет такого текста")
        doc = {
            "url": "https://example.com",
            "text": "другой контент",
            "title": "t",
            "score": 0.5,
        }
        assert build_evidence_window(c, doc) is None

    def test_returns_none_on_empty_text(self):
        c = Claim(text="foo")
        doc = {"url": "u", "text": "", "title": "t", "score": 0.0}
        assert build_evidence_window(c, doc) is None

    def test_handles_whitespace_normalized_fallback(self):
        c = Claim(text="foo bar")
        doc = {"url": "u", "text": "before  foo   bar  end", "title": "t", "score": 0.0}
        w = build_evidence_window(c, doc)
        assert w is not None
        # Offsets are against normalized text in fallback
        assert w.offset_start >= 0
        assert w.offset_end > w.offset_start

    def test_score_default_zero_if_missing(self):
        c = Claim(text="foo")
        doc = {"url": "u", "text": "foo bar", "title": "t"}  # no score
        w = build_evidence_window(c, doc)
        assert w is not None
        assert w.score == 0.0


# ============================================================
# format_cited_claim
# ============================================================

class TestFormatCitedClaim:
    """Render a claim with inline [doc_N:start-end] marker."""

    def test_formats_with_window(self):
        c = Claim(text="5 июня 2026")
        w = EvidenceWindow(
            text="5 июня 2026",
            offset_start=8,
            offset_end=19,
            source_url="https://example.com",
        )
        result = format_cited_claim(c, w, doc_index=0)
        assert result == "5 июня 2026 [doc_0:8-19]"

    def test_doc_index_appears_in_marker(self):
        c = Claim(text="x")
        w = EvidenceWindow(text="x", offset_start=10, offset_end=11)
        assert "[doc_3:" in format_cited_claim(c, w, doc_index=3)
        assert "[doc_42:" in format_cited_claim(c, w, doc_index=42)

    def test_passes_through_when_no_window(self):
        c = Claim(text="непроверенный факт")
        result = format_cited_claim(c, None, doc_index=0)
        assert result == "непроверенный факт"
        assert "[doc_" not in result

    def test_marker_regex_parses_output(self):
        c = Claim(text="foo bar")
        w = EvidenceWindow(text="foo bar", offset_start=100, offset_end=107)
        result = format_cited_claim(c, w, doc_index=5)
        m = _CITATION_RE.search(result)
        assert m is not None
        assert m.group(1) == "5"
        assert m.group(2) == "100"
        assert m.group(3) == "107"


# ============================================================
# citation_stats
# ============================================================

class TestCitationStats:
    """Coverage statistics."""

    def test_empty_list(self):
        stats = citation_stats([])
        assert stats["total"] == 0
        assert stats["cited"] == 0
        assert stats["uncited"] == 0
        assert stats["stub"] == 0
        assert stats["coverage"] == 0.0
        assert stats["non_stub_coverage"] == 0.0

    def test_all_cited(self):
        w = EvidenceWindow(text="x", offset_start=0, offset_end=1)
        claims = [Claim(text="c1", evidence_window=w), Claim(text="c2", evidence_window=w)]
        stats = citation_stats(claims)
        assert stats["total"] == 2
        assert stats["cited"] == 2
        assert stats["uncited"] == 0
        assert stats["stub"] == 0
        assert stats["coverage"] == 1.0
        assert stats["non_stub_coverage"] == 1.0

    def test_all_uncited(self):
        claims = [Claim(text="c1"), Claim(text="c2")]
        stats = citation_stats(claims)
        assert stats["total"] == 2
        assert stats["cited"] == 0
        assert stats["uncited"] == 2
        assert stats["coverage"] == 0.0

    def test_mixed_with_stubs(self):
        w = EvidenceWindow(text="x", offset_start=0, offset_end=1)
        claims = [
            Claim(text="c1", evidence_window=w),       # cited
            Claim(text="c2"),                            # uncited, non-stub
            Claim(text="c3", is_stub=True),              # stub, no window
        ]
        stats = citation_stats(claims)
        assert stats["total"] == 3
        assert stats["cited"] == 1
        assert stats["uncited"] == 2
        assert stats["stub"] == 1
        assert stats["coverage"] == 0.3333  # 1/3 rounded to 4 decimals
        # Non-stub coverage: 1 cited / 2 non-stub = 0.5
        assert stats["non_stub_coverage"] == 0.5

    def test_only_stubs_zero_non_stub_coverage(self):
        claims = [Claim(text="c1", is_stub=True), Claim(text="c2", is_stub=True)]
        stats = citation_stats(claims)
        assert stats["stub"] == 2
        assert stats["non_stub_coverage"] == 0.0  # no non-stub claims

    def test_rounding_to_4_decimals(self):
        w = EvidenceWindow(text="x", offset_start=0, offset_end=1)
        claims = [Claim(text="c1", evidence_window=w)] + [Claim(text=f"c{i}") for i in range(6)]
        # 1 cited / 7 total = 0.142857...
        stats = citation_stats(claims)
        assert stats["coverage"] == 0.1429  # rounded


# ============================================================
# assert_citations_complete
# ============================================================

class TestAssertCitationsComplete:
    """Invariant: every non-stub claim must have evidence_window."""

    def test_all_cited_passes(self):
        w = EvidenceWindow(text="x", offset_start=0, offset_end=1)
        claims = [Claim(text="c1", evidence_window=w), Claim(text="c2", evidence_window=w)]
        cited, uncited = assert_citations_complete(claims)
        assert cited == 2
        assert uncited == 0

    def test_stubs_skipped_by_default(self):
        w = EvidenceWindow(text="x", offset_start=0, offset_end=1)
        claims = [Claim(text="c1", evidence_window=w), Claim(text="stub", is_stub=True)]
        cited, uncited = assert_citations_complete(claims)
        assert cited == 1
        assert uncited == 0  # stub skipped

    def test_strict_mode_checks_stubs(self):
        w = EvidenceWindow(text="x", offset_start=0, offset_end=1)
        claims = [Claim(text="c1", evidence_window=w), Claim(text="stub", is_stub=True)]
        with pytest.raises(AssertionError, match="lack evidence_window"):
            assert_citations_complete(claims, allow_stub=False)

    def test_raises_on_uncited_non_stub(self):
        claims = [Claim(text="orphan claim")]
        with pytest.raises(AssertionError, match="orphan claim"):
            assert_citations_complete(claims)

    def test_error_message_lists_offending_claims(self):
        claims = [Claim(text=f"claim {i} text") for i in range(3)]
        with pytest.raises(AssertionError) as exc:
            assert_citations_complete(claims)
        msg = str(exc.value)
        assert "3" in msg  # count
        assert "claim 0 text" in msg or "claim 1 text" in msg

    def test_truncation_at_5_claims(self):
        claims = [Claim(text=f"c{i}") for i in range(10)]
        with pytest.raises(AssertionError) as exc:
            assert_citations_complete(claims)
        msg = str(exc.value)
        assert "5 more" in msg  # "(and 5 more)" suffix

    def test_no_raise_mode_returns_counts(self):
        claims = [Claim(text="c1"), Claim(text="c2")]
        cited, uncited = assert_citations_complete(claims, raise_on_missing=False)
        assert cited == 0
        assert uncited == 2


# ============================================================
# Integration: full pipeline (no runner, just the parts)
# ============================================================

class TestCitationIntegration:
    """End-to-end on a small document set."""

    def test_multiple_claims_from_one_doc(self):
        text = (
            "5 июня 2026 года произошла магнитная буря уровня G5.\n"
            "Скорость ветра достигла 123 км/ч.\n"
            "Давление упало до 740 мм рт.ст."
        )
        doc = {
            "url": "https://example.com/report",
            "title": "Метеоотчёт",
            "text": text,
            "score": 0.9,
        }
        claims = [
            Claim(text="5 июня 2026"),
            Claim(text="магнитная буря уровня G5"),
            Claim(text="123 км/ч"),
            Claim(text="740 мм рт.ст."),
        ]
        windows = [build_evidence_window(c, doc) for c in claims]
        # All four should resolve
        assert all(w is not None for w in windows)
        # All offsets should be valid
        for w in windows:
            assert 0 <= w.offset_start < w.offset_end <= len(text)

        # Format citations
        formatted = [format_cited_claim(c, w, doc_index=0) for c, w in zip(claims, windows, strict=False)]
        for f in formatted:
            assert "[doc_0:" in f

        # Augment claims via dataclasses.replace
        from dataclasses import replace
        augmented = [replace(c, evidence_window=w) for c, w in zip(claims, windows, strict=False)]

        stats = citation_stats(augmented)
        assert stats["total"] == 4
        assert stats["cited"] == 4
        assert stats["coverage"] == 1.0
        assert_citations_complete(augmented)  # no raise

    def test_partial_match_one_uncited(self):
        text = "5 июня 2026 произошла буря."
        doc = {"url": "u", "text": text, "title": "t", "score": 0.0}
        claims = [
            Claim(text="5 июня 2026"),       # present
            Claim(text="не упоминается"),    # absent
        ]
        windows = [build_evidence_window(c, doc) for c in claims]
        assert windows[0] is not None
        assert windows[1] is None

        formatted = [format_cited_claim(c, w, doc_index=0) for c, w in zip(claims, windows, strict=False)]
        assert "[doc_0:" in formatted[0]
        assert "[doc_0:" not in formatted[1]

        from dataclasses import replace
        augmented = [replace(c, evidence_window=w) for c, w in zip(claims, windows, strict=False)]
        with pytest.raises(AssertionError, match="не упоминается"):
            assert_citations_complete(augmented)


# ========================================================================
# v0.8.1.1 hardening: tests for the offset-preserving normalizer.
# ChatGPT P1 (v0.8.0 review): find_span() returned offsets against
# whitespace-normalized text, not the original `evidence_text`. A citation
# marker like [doc_0:120-187] was therefore not a reproducible pointer
# into the actual document.
#
# Fix: _normalize_with_map() builds (normalized_text, index_map) so
# find_span() can convert any normalized-space hit back to original-space
# offsets. These tests pin the new contract.
# ========================================================================


class TestNormalizeWithMap:
    """Unit tests for the offset-preserving whitespace normalizer."""

    def test_empty_string(self):
        norm, idx_map = _normalize_with_map("")
        assert norm == ""
        assert idx_map == []

    def test_pure_whitespace(self):
        norm, idx_map = _normalize_with_map("   \t\n  ")
        assert norm == ""
        assert idx_map == []

    def test_already_normalized(self):
        """No whitespace runs: map should be identity (0, 1, 2, ...)."""
        norm, idx_map = _normalize_with_map("hello world")
        assert norm == "hello world"
        assert idx_map == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # Sanity: the i-th normalized char is the i-th original char.
        for i, ch in enumerate(norm):
            assert ch == "hello world"[idx_map[i]]

    def test_collapses_whitespace_runs(self):
        """Multiple spaces / tabs / newlines collapse to a single ' '."""
        text = "  hello   \t\n  world  "
        norm, idx_map = _normalize_with_map(text)
        assert norm == "hello world"
        # Map length must equal normalized length.
        assert len(idx_map) == len(norm)
        # Each index in the map must point to a char in the original.
        for i, orig_i in enumerate(idx_map):  # noqa: B007  (i is part of enumerate iteration; test reads orig_i)
            assert 0 <= orig_i < len(text)
        # Round-trip: the chars at the mapped positions reconstruct norm.
        for i, ch in enumerate(norm):
            assert ch == text[idx_map[i]]

    def test_map_offsets_preserve_original_positions(self):
        """Map must point to the FIRST char of each collapsed WS run."""
        text = "ab   cd"  # 3 spaces between ab and cd
        norm, idx_map = _normalize_with_map(text)
        assert norm == "ab cd"
        # Position 0 ('a') -> original 0
        # Position 1 ('b') -> original 1
        # Position 2 (' ') -> original 2 (FIRST space, not 3 or 4)
        # Position 3 ('c') -> original 5
        # Position 4 ('d') -> original 6
        assert idx_map == [0, 1, 2, 5, 6]

    def test_stripped_leading_whitespace_shifts_map(self):
        """Leading WS is dropped from normalized, so map starts past it."""
        text = "   hello"
        norm, idx_map = _normalize_with_map(text)
        assert norm == "hello"
        # Map starts at original offset 3 (first 'h').
        assert idx_map == [3, 4, 5, 6, 7]


class TestFindSpanReturnsOriginalOffsets:
    """End-to-end check: find_span offsets are in ORIGINAL text space.

    The slice evidence_text[s:e] must be a substring of the ORIGINAL
    text, not a substring of the whitespace-normalized text. This is
    the contract ChatGPT flagged as broken in the v0.8.0 review.
    """

    def test_direct_hit_uses_original_offsets(self):
        c = Claim(text="5 июня 2026")
        original = "Сегодня 5 июня 2026 года."
        s, e = find_span(c, original)
        assert s == 8  # position of '5' in original
        assert original[s:e] == "5 июня 2026"

    def test_normalized_match_returns_original_offsets(self):
        """When claim is found only after WS normalization, offsets
        must still point into the original text (not the normalized)."""
        c = Claim(text="5 июня 2026")
        original = "Сегодня  5   июня    2026  года."  # extra spaces
        s, e = find_span(c, original)
        # original[s:e] must be a slice of the original, containing
        # the claim with its extra spaces preserved.
        slice_text = original[s:e]
        assert slice_text.startswith("5")
        assert slice_text.endswith("2026")
        assert "5" in slice_text and "июня" in slice_text and "2026" in slice_text
        # And the slice must be a real substring of the original.
        assert slice_text in original

    def test_newline_normalization_returns_original_offsets(self):
        c = Claim(text="5 июня 2026")
        original = "Сегодня\n5\nиюня\n2026\nгода."
        s, e = find_span(c, original)
        slice_text = original[s:e]
        # Slice must include the newlines from the source.
        assert "\n" in slice_text
        assert slice_text in original
        # Start should be the '5' (not the 'С' of Сегодня).
        assert original[s] == "5"

    def test_citation_marker_points_into_original(self):
        """End-to-end: a downstream consumer can use [s:e] to slice
        the original text and get a meaningful span (not garbage)."""
        c = Claim(text="5 июня 2026")
        original = "Преамбула. Сегодня  5   июня    2026  года. Постскриптум."
        s, e = find_span(c, original)
        marker = f"[doc_0:{s}-{e}]"
        # Reconstruct what a reader would see: take the slice from original.
        # The slice must contain the date components in order.
        slice_text = original[s:e]
        assert "5" in slice_text
        assert "июня" in slice_text
        assert "2026" in slice_text
        # And the marker should be parseable by _CITATION_RE.
        import re as _re
        m = _re.search(r"\[doc_(\d+):(\d+)-(\d+)\]", marker)
        assert m is not None
        doc_id, start_s, end_s = m.groups()
        assert int(start_s) == s
        assert int(end_s) == e
