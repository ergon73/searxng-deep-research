"""Offline metrics for source-native LLM Release Radar discovery."""

from __future__ import annotations

import urllib.parse
from typing import Any

from radar_sources import HuggingFaceCandidate

_FRESH_STATUSES = {"release", "limited_release"}
_HARD_NEGATIVE_STATUS = "not_release_in_window"
_HUB_AVAILABILITY = {
    "open_weights",
    "gated_open_weights",
    "adapter_weights",
}


def _canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.query,
            "",
        )
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _case_aliases(case: dict[str, Any]) -> set[str]:
    aliases = case.get("aliases")
    if not isinstance(aliases, list):
        return set()
    return {alias.casefold() for alias in aliases if isinstance(alias, str) and alias}


def _eligible_fresh_hub_case(case: dict[str, Any]) -> bool:
    expected = case.get("expected") or {}
    availability = expected.get("availability") or []
    return expected.get("status") in _FRESH_STATUSES and bool(_HUB_AVAILABILITY.intersection(availability))


def evaluate_huggingface_candidates(
    dataset: dict[str, Any],
    candidates: list[HuggingFaceCandidate],
) -> dict[str, Any]:
    """Measure candidate and primary-source recall with explicit denominators."""
    by_family = {candidate.family_id.casefold(): candidate for candidate in candidates}
    results: list[dict[str, Any]] = []

    for case in dataset["cases"]:
        matched = [by_family[alias] for alias in sorted(_case_aliases(case)) if alias in by_family]
        unique_matched = {candidate.family_id.casefold(): candidate for candidate in matched}
        matched = sorted(unique_matched.values(), key=lambda item: item.family_id.casefold())
        primary_urls = {
            _canonical_url(source["url"])
            for source in case.get("primary_sources", [])
            if isinstance(source, dict) and isinstance(source.get("url"), str)
        }
        candidate_primary_urls = {
            _canonical_url(url)
            for candidate in matched
            for url in (candidate.model_url, candidate.model_api_url)
        }
        eligible = _eligible_fresh_hub_case(case)
        results.append(
            {
                "case_id": case["id"],
                "expected_status": case["expected"]["status"],
                "eligible_fresh_hub_case": eligible,
                "candidate_recalled": bool(matched),
                "primary_source_recalled": bool(primary_urls.intersection(candidate_primary_urls)),
                "matched_families": [candidate.family_id for candidate in matched],
                "state_hints": sorted({candidate.state_hint for candidate in matched}),
            }
        )

    eligible_results = [result for result in results if result["eligible_fresh_hub_case"]]
    hard_negatives = [result for result in results if result["expected_status"] == _HARD_NEGATIVE_STATUS]
    eligible_recalled = sum(result["candidate_recalled"] for result in eligible_results)
    primary_recalled = sum(result["primary_source_recalled"] for result in eligible_results)
    hard_negative_signals = [result for result in hard_negatives if result["candidate_recalled"]]
    hard_negative_update_only = sum(
        result["state_hints"] == ["update_only"] for result in hard_negative_signals
    )

    return {
        "schema_version": 1,
        "vertical": dataset["vertical"],
        "source": "huggingface",
        "window": dataset["window"],
        "aggregate": {
            "eligible_fresh_cases": len(eligible_results),
            "eligible_fresh_cases_recalled": eligible_recalled,
            "eligible_fresh_case_recall": _ratio(
                eligible_recalled,
                len(eligible_results),
            ),
            "eligible_primary_source_recall": _ratio(
                primary_recalled,
                len(eligible_results),
            ),
            "hard_negative_cases": len(hard_negatives),
            "hard_negative_cases_with_hub_signal": len(hard_negative_signals),
            "hard_negative_update_only_signals": hard_negative_update_only,
        },
        "results": results,
    }
