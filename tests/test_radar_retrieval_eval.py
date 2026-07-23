"""Focused tests for the LLM Release Radar retrieval baseline."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from eval_radar_retrieval import (  # noqa: E402
    SearchResponse,
    aggregate_results,
    evaluate_case,
)


def _case(
    case_id: str,
    *,
    status: str = "release",
    source_url: str = "https://example.com/releases/model",
) -> dict:
    return {
        "id": case_id,
        "family": "Example Model",
        "aliases": ["org/example-model"],
        "discovery_queries": ["Example Model release"],
        "expected": {
            "status": status,
            "freshness": "fresh" if status == "release" else "outside_window",
        },
        "primary_sources": [
            {
                "url": source_url,
                "kind": "official_announcement",
                "supports": "fixture",
            }
        ],
    }


def test_evaluate_case_recalls_canonical_primary_source_and_engines():
    def fake_search(_query: str, _top_k: int) -> SearchResponse:
        return SearchResponse(
            hits=[
                {
                    "url": "https://example.com/releases/model?utm_source=test",
                    "title": "Example Model",
                    "engine": "github",
                }
            ],
            unresponsive=["brave: 429"],
        )

    result = evaluate_case(_case("positive"), search=fake_search, top_k=10)

    assert result["primary_source_recalled"] is True
    assert result["matched_primary_sources"] == ["https://example.com/releases/model"]
    assert result["engines"] == ["github"]
    assert result["unresponsive"] == ["brave: 429"]
    assert result["zero_result_queries"] == 0


def test_evaluate_case_records_empty_results_and_search_errors():
    responses = iter(
        [
            SearchResponse(hits=[]),
            SearchResponse(hits=[], error="TimeoutError"),
        ]
    )
    case = _case("empty")
    case["discovery_queries"] = ["first", "second"]

    result = evaluate_case(
        case,
        search=lambda _query, _top_k: next(responses),
        top_k=5,
    )

    assert result["primary_source_recalled"] is False
    assert result["zero_result_queries"] == 2
    assert result["search_errors"] == ["TimeoutError"]


def test_aggregate_results_keeps_release_and_negative_denominators_explicit():
    results = [
        {
            "expected_status": "release",
            "primary_source_recalled": True,
            "queries": 1,
            "zero_result_queries": 0,
            "search_errors": [],
            "engines": ["github"],
            "unresponsive": [],
        },
        {
            "expected_status": "limited_release",
            "primary_source_recalled": False,
            "queries": 1,
            "zero_result_queries": 1,
            "search_errors": [],
            "engines": [],
            "unresponsive": ["brave: 429"],
        },
        {
            "expected_status": "not_release_in_window",
            "primary_source_recalled": True,
            "queries": 1,
            "zero_result_queries": 0,
            "search_errors": [],
            "engines": ["bing"],
            "unresponsive": [],
        },
    ]

    aggregate = aggregate_results(results)

    assert aggregate["fresh_release_cases"] == 2
    assert aggregate["fresh_release_primary_source_recall"] == 0.5
    assert aggregate["hard_negative_cases"] == 1
    assert aggregate["hard_negative_primary_source_recall"] == 1.0
    assert aggregate["zero_result_queries"] == 1
    assert aggregate["engines"] == ["bing", "github"]
    assert aggregate["unresponsive"] == ["brave: 429"]
