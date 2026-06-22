"""
Tests for src/ranking.py — source ranking (v0.9-C1).

ChatGPT P1 (v0.8.0 review): research_runner used `top1 = iter_documents[0]`,
which is "the first URL we asked SearXNG for" — not the highest-ranked
source. Long irrelevant documents could win against short, highly-relevant
ones simply because of fetch order.

Fix: rank_documents() sorts by combined source_score (provenance/position
0.35 + content 0.45 + length 0.20) BEFORE selecting top1 or top-N. v0.9-C1
adds `_search_provenance` as the primary prior, falling back to original
SearXNG ordinal position for legacy documents. This module tests the ranking
logic in isolation (no network, no runner).

All tests are offline — pure stdlib, no I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make src importable (test file may run from repo root or tests/).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from ranking import (  # noqa: E402
    _keyword_coverage,
    _length_score,
    _noise_penalty,
    _position_score,
    _provenance_query_vote_bonus,
    _provenance_rank_score,
    _provenance_search_score,
    _query_terms,
    compute_source_score,
    rank_documents,
    select_top_n,
)


def _doc(
    url: str,
    text: str = "x" * 1000,
    length: int | None = None,
    error: str | None = None,
    provenance: list[dict] | None = None,
) -> dict:
    """Build a doc dict the way _fetch_documents does."""
    if length is None:
        length = len(text)
    d = {"url": url, "text": text, "title": "t", "length": length, "error": error}
    if provenance is not None:
        d["_search_provenance"] = provenance
    return d


# ========================================================================
# Provenance search tests (v0.9-C1)
# ========================================================================


class TestProvenanceSearchScore:
    def test_no_provenance_falls_back_to_none(self):
        assert _provenance_search_score(_doc("https://x.com")) is None
        assert _provenance_search_score({}) is None

    def test_rank_score_uses_true_one_based_rank(self):
        d = _doc("https://x.com", provenance=[{"rank": 1, "task_priority": 0}])
        assert _provenance_rank_score(d) == 1.0

    def test_rank_score_for_second_result(self):
        d = _doc("https://x.com", provenance=[{"rank": 2, "task_priority": 100}])
        assert _provenance_rank_score(d) == 0.5

    def test_task_priority_does_not_affect_rank_score(self):
        high_priority = _doc("https://x.com", provenance=[{"rank": 1, "task_priority": 100}])
        low_priority = _doc("https://x.com", provenance=[{"rank": 1, "task_priority": 0}])
        assert _provenance_rank_score(high_priority) == _provenance_rank_score(low_priority)

    def test_best_rank_wins_across_multiple_entries(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 5, "task_query": "q1"},
                {"rank": 1, "task_query": "q2"},
            ],
        )
        assert _provenance_rank_score(d) == 1.0

    def test_exact_duplicate_provenance_entries_are_ignored(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 5, "task_query": "q1", "task_priority": 100},
                {"rank": 5, "task_query": "q1", "task_priority": 100},
            ],
        )
        assert _provenance_query_vote_bonus(d) == 0.0

    def test_provenance_without_rank_or_query_is_unusable(self):
        d = _doc("https://x.com", provenance=[{"task_query": "q"}])
        assert _provenance_rank_score(d) is None

    def test_partial_provenance_with_only_rank(self):
        d = _doc("https://x.com", provenance=[{"rank": 1}])
        assert _provenance_search_score(d) == 1.0

    def test_partial_provenance_with_only_query_vote_is_unusable(self):
        d = _doc("https://x.com", provenance=[{"task_query": "q1"}])
        assert _provenance_search_score(d) is None

    def test_query_vote_bonus_counts_extra_distinct_task_queries(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 5, "task_query": "q1"},
                {"rank": 4, "task_query": "q2"},
            ],
        )
        assert _provenance_query_vote_bonus(d) == 0.10

    def test_query_vote_bonus_saturates_at_three_distinct_queries(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 5, "task_query": "q1"},
                {"rank": 4, "task_query": "q2"},
                {"rank": 3, "task_query": "q3"},
                {"rank": 2, "task_query": "q4"},
            ],
        )
        assert _provenance_query_vote_bonus(d) == 0.20

    def test_rank_zero_ignored(self):
        d = _doc("https://x.com", provenance=[{"rank": 0, "task_query": "q1"}])
        assert _provenance_rank_score(d) is None

    def test_bool_rank_true_ignored_and_falls_back_to_position(self):
        bool_rank = _doc(
            "https://x.com",
            text="apple news today" * 100,
            length=1600,
            provenance=[{"rank": True, "task_query": "q1"}],
        )
        legacy = _doc("https://x.com", text="apple news today" * 100, length=1600)
        assert _provenance_rank_score(bool_rank) is None
        assert _provenance_search_score(bool_rank) is None
        assert compute_source_score(bool_rank, ["apple"], original_index=3) == compute_source_score(
            legacy, ["apple"], original_index=3
        )

    def test_bool_rank_false_ignored(self):
        d = _doc("https://x.com", provenance=[{"rank": False, "task_query": "q1"}])
        assert _provenance_rank_score(d) is None
        assert _provenance_search_score(d) is None

    def test_whitespace_only_task_query_gives_zero_vote_bonus(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 1, "task_query": "   "},
                {"rank": 1, "task_query": "q1"},
            ],
        )
        assert _provenance_query_vote_bonus(d) == 0.0

    def test_task_query_dedupes_after_strip(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 2, "task_query": " q1 "},
                {"rank": 2, "task_query": "q1"},
            ],
        )
        assert _provenance_query_vote_bonus(d) == 0.0

    def test_search_score_rank_one_single_query_stays_one(self):
        d = _doc("https://x.com", provenance=[{"rank": 1, "task_query": "q1"}])
        assert _provenance_search_score(d) == 1.0

    def test_search_score_adds_small_capped_query_vote_bonus(self):
        d = _doc(
            "https://x.com",
            provenance=[
                {"rank": 2, "task_query": "q1"},
                {"rank": 2, "task_query": "q2"},
                {"rank": 2, "task_query": "q3"},
                {"rank": 2, "task_query": "q4"},
            ],
        )
        assert _provenance_search_score(d) == 0.7

    def test_task_priority_is_ignored_by_search_score(self):
        high_priority = _doc(
            "https://x.com", provenance=[{"rank": 2, "task_query": "q1", "task_priority": 100}]
        )
        low_priority = _doc("https://x.com", provenance=[{"rank": 2, "task_query": "q1", "task_priority": 0}])
        assert _provenance_search_score(high_priority) == _provenance_search_score(low_priority)


# ========================================================================
# Helper-function unit tests
# ========================================================================


class TestPositionScore:
    def test_first_position_is_one(self):
        assert _position_score(0) == 1.0

    def test_second_position_is_half(self):
        assert _position_score(1) == 0.5

    def test_ten_is_small_but_nonzero(self):
        assert 0.0 < _position_score(10) < 0.2

    def test_negative_index_is_zero(self):
        assert _position_score(-1) == 0.0


class TestLengthScore:
    def test_empty_text_is_zero(self):
        assert _length_score(0) == 0.0

    def test_long_text_saturates(self):
        assert _length_score(5000) == 1.0
        assert _length_score(100000) == 1.0

    def test_medium_text_is_partial(self):
        # 2000/4000 = 0.5
        assert _length_score(2000) == 0.5


class TestKeywordCoverage:
    def test_no_query_terms_is_zero(self):
        assert _keyword_coverage("hello world", []) == 0.0

    def test_all_terms_present(self):
        assert _keyword_coverage("apple founded 1976 steve jobs", ["apple", "1976"]) == 1.0

    def test_partial_match(self):
        # 1 of 2 terms present → 0.5
        assert _keyword_coverage("apple founded", ["apple", "1976"]) == 0.5

    def test_no_terms_present(self):
        assert _keyword_coverage("random text here", ["apple", "1976"]) == 0.0


class TestNoisePenalty:
    def test_clean_text(self):
        assert _noise_penalty("Just a normal article about Apple.") == 0.0

    def test_cookie_policy(self):
        assert _noise_penalty("We use cookies. Cookie Policy applies.") >= 0.5

    def test_empty_text(self):
        assert _noise_penalty("") == 1.0


class TestQueryTerms:
    def test_basic_extraction(self):
        terms = _query_terms("Apple was founded in 1976")
        assert "apple" in terms
        # Tokens shorter than 3 chars are filtered out (noise like "a", "to", "in")
        # "was" is exactly 3 chars so it passes; "in" (2 chars) is filtered.
        assert "in" not in terms
        assert "founded" in terms
        assert "1976" in terms

    def test_dedup(self):
        terms = _query_terms("apple apple apple founded founded")
        assert terms.count("apple") == 1
        assert terms.count("founded") == 1

    def test_empty_query(self):
        assert _query_terms("") == []
        assert _query_terms("a b c") == []  # all <3 chars


# ========================================================================
# compute_source_score — main public function
# ========================================================================


class TestComputeSourceScore:
    def test_empty_doc_is_zero(self):
        assert compute_source_score({}) == 0.0
        assert compute_source_score(None) == 0.0

    def test_doc_with_error_is_zero(self):
        d = _doc("https://x.com", text="some content", error="timeout")
        assert compute_source_score(d) == 0.0

    def test_empty_text_is_zero(self):
        d = _doc("https://x.com", text="")
        assert compute_source_score(d) == 0.0

    def test_score_in_zero_one_range(self):
        d = _doc("https://apple.com", text="x" * 2000, length=2000)
        s = compute_source_score(d, ["apple"], original_index=0)
        assert 0.0 <= s <= 1.0

    def test_higher_relevance_higher_score(self):
        """A document matching all query terms beats one matching none."""
        # 60 chars, missing length; will get length set below.
        relevant = _doc(
            "https://apple.com",
            text="apple founded 1976 steve jobs wozniak garage cupertino",
        )
        irrelevant = _doc("https://random.com", text="z" * 1000)  # 1000 chars of z
        # Make lengths comparable
        relevant["length"] = len(relevant["text"])
        terms = ["apple", "founded", "1976"]
        s_rel = compute_source_score(relevant, terms, original_index=5)
        s_irr = compute_source_score(irrelevant, terms, original_index=5)
        # Even though irrelevant is longer, it has 0 keyword coverage.
        # Relevant has high coverage. Relevant should win clearly.
        assert s_rel > s_irr, f"relevant={s_rel} should beat irrelevant={s_irr} on coverage"

    def test_position_matters(self):
        """Same content, different position → different score."""
        d = _doc("https://x.com", text="apple news today" * 100, length=1600)
        s0 = compute_source_score(d, ["apple"], original_index=0)
        s5 = compute_source_score(d, ["apple"], original_index=5)
        assert s0 > s5, f"position 0 ({s0}) should beat position 5 ({s5})"

    def test_no_query_terms_still_ranks(self):
        """Without query terms, position + length still give signal."""
        d1 = _doc("https://a.com", text="x" * 2000, length=2000)
        d2 = _doc("https://b.com", text="x" * 500, length=500)
        s1 = compute_source_score(d1, [], original_index=0)
        s2 = compute_source_score(d2, [], original_index=3)
        # d1 is longer AND earlier — should win.
        assert s1 > s2

    def test_provenance_beats_position(self):
        """A doc with weak provenance loses to one with strong provenance."""
        strong = _doc(
            "https://strong.com",
            text="apple news today" * 100,
            length=1600,
            provenance=[{"rank": 1, "task_priority": 100}],
        )
        weak = _doc(
            "https://weak.com",
            text="apple news today" * 100,
            length=1600,
            provenance=[{"rank": 10, "task_priority": 40}],
        )
        ranked = rank_documents([weak, strong], "apple")
        assert ranked[0]["url"] == "https://strong.com"

    def test_provenance_beats_content_length_position(self):
        """Strong provenance can outrank a longer/earlier but weak-provenance doc."""
        strong = _doc(
            "https://strong.com",
            text="apple news today" * 10,  # short
            length=160,
            provenance=[{"rank": 1, "task_priority": 100}],
        )
        weak = _doc(
            "https://weak.com",
            text="apple news today" * 100,  # long
            length=1600,
            provenance=[{"rank": 5, "task_priority": 40}],
        )
        ranked = rank_documents([weak, strong], "apple")
        assert ranked[0]["url"] == "https://strong.com"

    def test_fallback_to_position_when_no_provenance(self):
        """Legacy docs without provenance still use original_index as prior."""
        d1 = _doc("https://a.com", text="apple news today" * 100, length=1500)
        d2 = _doc("https://b.com", text="apple news today" * 100, length=1500)
        s0 = compute_source_score(d1, ["apple"], original_index=0)
        s1 = compute_source_score(d2, ["apple"], original_index=1)
        assert s0 > s1


# ========================================================================
# rank_documents — main entry point
# ========================================================================


class TestRankDocuments:
    def test_empty_input(self):
        assert rank_documents([]) == []

    def test_relevant_doc_beats_irrelevant(self):
        relevant = _doc(
            "https://apple.com",
            text="apple steve jobs founded 1976 garage cupertino california" * 10,
        )
        irrelevant = _doc("https://random.com", text="random unrelated text content" * 50)
        relevant["length"] = len(relevant["text"])
        irrelevant["length"] = len(irrelevant["text"])
        ranked = rank_documents([irrelevant, relevant], "apple founded 1976")
        assert ranked[0]["url"] == "https://apple.com"

    def test_source_score_attached(self):
        """Each ranked doc gets a source_score field added."""
        d = _doc("https://x.com", text="apple news" * 100, length=1000)
        ranked = rank_documents([d], "apple")
        assert "source_score" in ranked[0]
        assert isinstance(ranked[0]["source_score"], float)

    def test_provenance_attached_preserved(self):
        """rank_documents must preserve existing `_search_provenance`."""
        d = _doc(
            "https://x.com",
            text="apple news" * 100,
            length=1000,
            provenance=[{"rank": 1, "task_priority": 100}],
        )
        ranked = rank_documents([d], "apple")
        assert ranked[0].get("_search_provenance") == [{"rank": 1, "task_priority": 100}]

    def test_does_not_mutate_input_order(self):
        """Input list order should be irrelevant — output is sorted."""
        d1 = _doc("https://low.com", text="x" * 100, length=100)
        d2 = _doc("https://high.com", text="apple news " * 200, length=2400)
        d3 = _doc("https://mid.com", text="apple " * 50, length=300)
        # Input order: low, high, mid
        ranked = rank_documents([d1, d2, d3], "apple")
        # Output: high, mid, low (by score)
        assert ranked[0]["url"] == "https://high.com"
        assert ranked[1]["url"] == "https://mid.com"
        assert ranked[2]["url"] == "https://low.com"

    def test_ties_broken_by_original_index(self):
        """Two docs with identical content: earlier index wins."""
        d1 = _doc("https://a.com", text="apple news today" * 100, length=1500)
        d2 = _doc("https://b.com", text="apple news today" * 100, length=1500)
        ranked = rank_documents([d1, d2], "apple")
        # Identical scores → a.com (index 0) should come first.
        assert ranked[0]["url"] == "https://a.com"

    def test_error_doc_drops_to_bottom(self):
        """A doc with fetch error should be ranked last (score 0)."""
        good = _doc("https://good.com", text="apple news " * 100, length=1000)
        bad = _doc("https://bad.com", text="", error="timeout")
        ranked = rank_documents([bad, good], "apple")
        assert ranked[0]["url"] == "https://good.com"
        assert ranked[0]["source_score"] > 0
        assert ranked[1]["url"] == "https://bad.com"
        assert ranked[1]["source_score"] == 0.0

    def test_returns_new_list(self):
        """rank_documents should return a new list, not mutate input order."""
        d1 = _doc("https://a.com", text="x" * 100, length=100)
        d2 = _doc("https://b.com", text="x" * 100, length=100)
        input_list = [d1, d2]
        rank_documents(input_list, "")
        # The input list object should be unchanged.
        assert input_list == [d1, d2]


class TestSelectTopN:
    def test_returns_top_n(self):
        docs = [
            _doc(f"https://{c}.com", text="apple " * (i + 1) * 50, length=(i + 1) * 100)
            for i, c in enumerate("abcdef")
        ]
        out = select_top_n(docs, "apple", n=3)
        assert len(out) == 3

    def test_n_larger_than_input(self):
        d1 = _doc("https://a.com", text="apple " * 100, length=600)
        d2 = _doc("https://b.com", text="apple " * 50, length=300)
        out = select_top_n([d1, d2], "apple", n=10)
        assert len(out) == 2  # all available docs

    def test_n_zero_returns_empty(self):
        docs = [_doc("https://a.com", text="x" * 1000, length=1000)]
        assert select_top_n(docs, "x", n=0) == []

    def test_n_negative_returns_empty(self):
        docs = [_doc("https://a.com", text="x" * 1000, length=1000)]
        assert select_top_n(docs, "x", n=-5) == []

    def test_empty_input(self):
        assert select_top_n([], "x", n=4) == []
