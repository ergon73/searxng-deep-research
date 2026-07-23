"""Retrieval baseline for the LLM Release Radar vertical.

The evaluator asks precise verification queries from the frozen Radar dataset
and measures whether SearXNG retrieves a recorded primary source. It does not
claim to classify releases yet; the explicit release and hard-negative
denominators show how much source evidence the future classifier receives.

Examples:

    PYTHONPATH=src python scripts/eval_radar_retrieval.py --no-network
    PYTHONPATH=src python scripts/eval_radar_retrieval.py --online
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_SET = REPO_ROOT / "data" / "radar_eval_set.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8888"
RELEASE_STATUSES = {"release", "limited_release"}
HARD_NEGATIVE_STATUS = "not_release_in_window"
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


@dataclass
class SearchResponse:
    """One SearXNG response without fetched page content."""

    hits: list[dict[str, Any]] = field(default_factory=list)
    unresponsive: list[str] = field(default_factory=list)
    error: str | None = None


SearchFn = Callable[[str, int], SearchResponse]


def _canonical_url(url: str) -> str:
    """Small dependency-free URL normalizer for exact source matching."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    query = urllib.parse.urlencode(
        [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key.lower() not in TRACKING_PARAMS
        ]
    )
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _validate_base_url(base_url: str) -> str:
    """Accept only the private loopback SearXNG endpoint."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Radar eval base URL must be an HTTP loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Radar eval base URL must not contain credentials or query data")
    return base_url.rstrip("/")


def _unresponsive_labels(raw: Any) -> list[str]:
    labels: list[str] = []
    if not isinstance(raw, list):
        return labels
    for item in raw:
        if isinstance(item, (list, tuple)) and item:
            engine = str(item[0])
            reason = str(item[1]) if len(item) > 1 else "unresponsive"
            labels.append(f"{engine}: {reason}")
        elif isinstance(item, str):
            labels.append(item)
    return sorted(set(labels))


def make_searxng_search(
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 20.0,
) -> SearchFn:
    """Build a bounded loopback-only JSON search function."""
    validated_base = _validate_base_url(base_url)

    def search(query: str, top_k: int) -> SearchResponse:
        params = urllib.parse.urlencode(
            {
                "q": query,
                "format": "json",
                "language": "all",
            }
        )
        request = urllib.request.Request(  # noqa: S310 - loopback URL validated above
            f"{validated_base}/search?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": "searxng-deep-research-radar-eval/0.9",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback URL validated above
                request,
                timeout=timeout,
            ) as response:
                payload = json.load(response)
        except Exception as exc:  # the error class is enough for baseline telemetry
            return SearchResponse(error=type(exc).__name__)

        hits = payload.get("results")
        return SearchResponse(
            hits=hits[:top_k] if isinstance(hits, list) else [],
            unresponsive=_unresponsive_labels(payload.get("unresponsive_engines")),
        )

    return search


def _engines_from_hit(hit: dict[str, Any]) -> set[str]:
    raw_engines = hit.get("engines")
    if isinstance(raw_engines, list):
        return {str(engine) for engine in raw_engines if isinstance(engine, str) and engine}
    engine = hit.get("engine")
    return {engine} if isinstance(engine, str) and engine else set()


def evaluate_case(case: dict[str, Any], *, search: SearchFn, top_k: int) -> dict[str, Any]:
    """Measure primary-source retrieval for one frozen Radar case."""
    source_by_canonical = {_canonical_url(source["url"]): source["url"] for source in case["primary_sources"]}
    matched_sources: set[str] = set()
    engines: set[str] = set()
    unresponsive: set[str] = set()
    errors: list[str] = []
    zero_result_queries = 0
    queries = list(case.get("discovery_queries") or [])

    for query in queries:
        response = search(query, top_k)
        if response.error:
            errors.append(response.error)
        if not response.hits:
            zero_result_queries += 1
        unresponsive.update(response.unresponsive)

        for hit in response.hits:
            if not isinstance(hit, dict):
                continue
            engines.update(_engines_from_hit(hit))
            raw_url = hit.get("url")
            if not isinstance(raw_url, str) or not raw_url:
                continue
            source_url = source_by_canonical.get(_canonical_url(raw_url))
            if source_url:
                matched_sources.add(source_url)

    return {
        "case_id": case["id"],
        "family": case["family"],
        "expected_status": case["expected"]["status"],
        "expected_freshness": case["expected"]["freshness"],
        "queries": len(queries),
        "primary_source_recalled": bool(matched_sources),
        "matched_primary_sources": sorted(matched_sources),
        "engines": sorted(engines),
        "unresponsive": sorted(unresponsive),
        "zero_result_queries": zero_result_queries,
        "search_errors": errors,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics with explicit positive and negative denominators."""
    release_results = [result for result in results if result["expected_status"] in RELEASE_STATUSES]
    negative_results = [result for result in results if result["expected_status"] == HARD_NEGATIVE_STATUS]
    release_recalled = sum(bool(result["primary_source_recalled"]) for result in release_results)
    negative_recalled = sum(bool(result["primary_source_recalled"]) for result in negative_results)

    return {
        "cases": len(results),
        "fresh_release_cases": len(release_results),
        "fresh_release_primary_source_recall": _ratio(
            release_recalled,
            len(release_results),
        ),
        "hard_negative_cases": len(negative_results),
        "hard_negative_primary_source_recall": _ratio(
            negative_recalled,
            len(negative_results),
        ),
        "queries": sum(int(result["queries"]) for result in results),
        "zero_result_queries": sum(int(result["zero_result_queries"]) for result in results),
        "search_errors": sum(len(result["search_errors"]) for result in results),
        "engines": sorted({engine for result in results for engine in result["engines"]}),
        "unresponsive": sorted({label for result in results for label in result["unresponsive"]}),
    }


def load_dataset(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def evaluate_dataset(
    dataset: dict[str, Any],
    *,
    search: SearchFn,
    top_k: int,
) -> dict[str, Any]:
    results = [evaluate_case(case, search=search, top_k=top_k) for case in dataset["cases"]]
    return {
        "schema_version": 1,
        "vertical": dataset["vertical"],
        "window": dataset["window"],
        "aggregate": aggregate_results(results),
        "results": results,
    }


def _offline_summary(dataset: dict[str, Any]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for case in dataset["cases"]:
        status = case["expected"]["status"]
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "schema_version": 1,
        "mode": "offline",
        "vertical": dataset["vertical"],
        "window": dataset["window"],
        "cases": len(dataset["cases"]),
        "statuses": dict(sorted(statuses.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=DEFAULT_SET)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--no-network", action="store_true")
    mode.add_argument("--online", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    if args.top_k < 1 or args.top_k > 50:
        parser.error("--top-k must be between 1 and 50")
    if args.timeout <= 0 or args.timeout > 60:
        parser.error("--timeout must be greater than 0 and at most 60")

    dataset = load_dataset(args.set)
    if args.no_network:
        report = _offline_summary(dataset)
    else:
        report = evaluate_dataset(
            dataset,
            search=make_searxng_search(
                base_url=args.base_url,
                timeout=args.timeout,
            ),
            top_k=args.top_k,
        )
        report["mode"] = "online"

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
