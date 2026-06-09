"""
eval.py — Quality Score eval runner для searxng-deep-research.

Прогоняет golden query set через pipeline, считает QS per query + aggregate,
аппендит результат в data/eval_log.jsonl.

Quality Score (QS) ∈ [0, 1]:
  QS = 0.45 * coverage_score
     + 0.22 * (1 - contradiction_rate)
     + 0.22 * synthesis.confidence
     + 0.11 * routing_precision

  needs_confirmation is intentionally NOT in the QS formula. Confirmation
  is a safety gate — True means the system correctly identified a high-risk
  or ambiguous query and asked for human review, not a quality defect.
  Confirmation is still tracked in result.needs_confirmation and shown in
  qs_breakdown["no_confirmation"] for diagnostic visibility.

Аргументы:
  --set PATH     Path to eval_set.json (default: data/eval_set.json)
  --log PATH     Path to eval_log.jsonl (default: data/eval_log.jsonl)
  --no-network   Skip web_search/fetch (offline mode for fast baseline)
  --online       Real pipeline: web_search + fetch + LLM verify + synthesis
  --query ID     Только один query (для debugging)
  --dry-run      Не аппендить в log
"""

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Ensure PYTHONPATH — portable: derive src/ from this file's location, not /opt/searxng
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from critical_review import review  # noqa: E402  (portable path bootstrap above)
from query_adaptation import adapt_query  # noqa: E402
from routing import classify_intent  # noqa: E402
from synthesis import synthesize  # noqa: E402

# Quality Score weights (sum = 1.0)
# NOTE: `needs_confirmation` is intentionally NOT in the QS formula. Confirmation
# is a safety gate — `True` means the system correctly identified a high-risk
# or ambiguous query and asked for human review, not a quality defect.
# Confirmation is still tracked in result.needs_confirmation and reported separately.
W_COVERAGE = 0.45
W_NO_CONTRADICTIONS = 0.22
W_CONFIDENCE = 0.22
W_ROUTING_PRECISION = 0.11
# W_NO_CONFIRMATION removed in v0.8.0 (was penalising correct safety behaviour).

# Penalty constants (для future extensions)
PENALTY_LOW_CONFIDENCE = 0.0  # not used yet

# Online-mode tunables
ONLINE_MAX_RESULTS = 5
ONLINE_FETCH_TOP_N = 3
ONLINE_FETCH_TIMEOUT = 12.0
ONLINE_FETCH_MAX_CHARS = 4000
ONLINE_LLM_FALLBACK_DETERMINISTIC = True  # if LLM fails → continue deterministic


@dataclass
class QueryResult:
    """Результат прогона одного query через pipeline."""

    query_id: str
    query: str
    expected_route: str
    category: str

    # Pre-search metrics
    main_query: str
    needs_confirmation: bool
    dropped_terms: list[str]
    route_predicted: str
    route_match: bool  # True если route_predicted == expected_route

    # Post-search (offline mode: empty/None)
    coverage_score: float = 0.0
    confidence: float = 0.0
    contradiction_rate: float = 0.0
    synthesis_citations: int = 0
    risk_level: str = "low"

    # Online mode (extra fields)
    search_results_count: int = 0
    sources_fetched: int = 0
    facts_extracted: int = 0
    claims_verified: int = 0
    claims_supported: int = 0
    llm_calls: int = 0
    llm_model_used: str = ""
    stage: str = ""  # "search" | "fetch" | "verify" | "synthesis" | "review" | "done"

    # Online mode: silent-skip counters (v0.8.1.3 hygiene — observability for
    # degraded retrieval. If these grow, the pipeline may be silently dropping
    # sources, which would mask Quality Score regressions.)
    urls_total: int = 0
    urls_skipped_canonical: int = 0  # canonical_url() raised
    urls_skipped_deny_pattern: int = 0  # matched _URL_DENY_PATTERNS
    urls_skipped_duplicate: int = 0  # URL or canon already in seen
    fetch_errors: int = 0  # fetch_url() raised (no content, network, etc.)
    urls_empty_or_error: int = 0  # fetch returned no text or error field
    search_errors: int = 0  # web_search() raised
    search_no_results: int = 0  # web_search() returned []
    verify_errors: int = 0  # verify_sources() raised
    synthesis_errors: int = 0  # synthesize() raised
    review_errors: int = 0  # review() raised

    # Quality Score
    qs: float = 0.0
    qs_breakdown: dict = field(default_factory=dict)

    # Meta
    elapsed_sec: float = 0.0
    mode: str = "offline"  # "offline" | "online"
    error: str | None = None


def _score_query_offline(result: QueryResult) -> None:
    """Offline scoring: pipeline runs, but no web_search/LLM → coverage=0.

    Routing + adaptation metrics still computed (deterministic).
    """
    result.coverage_score = 0.0
    result.confidence = 0.0
    result.contradiction_rate = 0.0
    result.synthesis_citations = 0
    result.risk_level = "low"


def _run_online_pipeline(result: QueryResult) -> None:
    """Online pipeline: web_search → fetch → LLM verify → synthesize → review.

    Updates result fields in-place. Catches exceptions per stage so we
    degrade gracefully (one stage failing shouldn't kill the whole run).
    """
    # Late imports to keep --no-network mode hermetic (no LLM deps loaded)
    from hermes_deepresearch import (
        _extract_facts,
        canonical_url,
        fetch_url,
        verify_sources,
    )
    from hermes_searxng import web_search

    qtext = result.query
    plan_main = result.main_query

    # 1. SEARCH
    result.stage = "search"
    try:
        intent = classify_intent(qtext)
        # NOTE 2026-06-07: we pass engines=None (SearXNG default) instead of
        # intent.engines. Why: the local SearXNG instance only has 3 engines
        # enabled (semanticscholar, duckduckgo, google) — wikipedia, arxiv,
        # github etc. return 0 results. intent.engines is a recommendation
        # for production routing, but at eval time we use whatever works
        # on this instance. Routing precision is still measured by intent.route,
        # not by which engines the search actually used.
        search_results = web_search(
            plan_main or qtext,
            lang="en",
            time_range=intent.time_range or "year",
            engines=None,
            max_results=ONLINE_MAX_RESULTS,
        )
    except Exception as e:
        result.error = f"search: {type(e).__name__}: {e}"
        result.stage = "search_failed"
        result.search_errors += 1
        return
    result.search_results_count = len(search_results)
    if not search_results:
        result.stage = "no_results"
        result.search_no_results += 1
        return

    # 2. FETCH top-N
    result.stage = "fetch"
    sources: list[dict] = []
    seen: set[str] = set()

    # v0.8.3: URL quality filter — skip navigation/portal pages, social media,
    # and Wikimedia category listings. These never contain claim-level facts.
    # Prefer Wikipedia article URLs, official docs, established news.
    _URL_DENY_PATTERNS = (
        "commons.wikimedia.org/wiki/Category",  # nav pages, not articles
        "/wiki/Category:",
        "vk.com/wall-",  # social media posts
        "vk.com/wall",
        "instagram.com/p/",  # social media
        "facebook.com/",
        "twitter.com/",
        "t.me/",  # telegram public posts (often low quality)
        "youtube.com/watch",  # video (no text facts)
        "?action=",  # MediaWiki actions
    )

    for r in search_results:
        url = r.get("url", "")
        result.urls_total += 1
        if not url or url in seen:
            result.urls_skipped_duplicate += 1
            continue
        # Apply URL quality filter
        if any(pat in url for pat in _URL_DENY_PATTERNS):
            result.urls_skipped_deny_pattern += 1
            continue
        seen.add(url)
        try:
            canon = canonical_url(url)
            if canon in seen:
                result.urls_skipped_duplicate += 1
                continue
            seen.add(canon)
        except Exception:  # noqa: S110  (unparseable URL → skip silently, intentional)
            result.urls_skipped_canonical += 1
            pass
        try:
            content = fetch_url(
                url,
                timeout=ONLINE_FETCH_TIMEOUT,
                max_chars=ONLINE_FETCH_MAX_CHARS,
            )
        except Exception:  # noqa: S112  (fetch error → skip URL, intentional noise filter)
            result.fetch_errors += 1
            continue
        if not content or content.get("error"):
            result.urls_empty_or_error += 1
            continue
        sources.append(
            {
                "url": url,
                "title": r.get("title", ""),
                "text": content.get("text", "")[:ONLINE_FETCH_MAX_CHARS],
            }
        )
        if len(sources) >= ONLINE_FETCH_TOP_N:
            break
    result.sources_fetched = len(sources)
    if not sources:
        result.stage = "fetch_failed"
        return

    # 3. EXTRACT FACTS из top-1
    result.stage = "extract"
    top1_text = sources[0]["text"]
    # v0.8.3: pass query to enable query-aware fact ranking (filters nav fragments)
    facts = _extract_facts(top1_text, max_facts=6, query=qtext)
    result.facts_extracted = len(facts)
    if not facts:
        result.stage = "no_facts"
        return

    # 4. VERIFY (use_llm=True → goes through LLMVerifier model chain)
    result.stage = "verify"
    main_fact = facts[0]
    main_source = sources[0]
    other = [{"url": s["url"], "text": s["text"]} for s in sources[1:]]
    try:
        verify_result = verify_sources(
            top1={"url": main_source["url"], "text": main_source["text"]},
            other_sources=other,
            query=main_fact,
            use_llm=True,  # ← LLM chain активен
        )
    except Exception as e:
        result.error = f"verify: {type(e).__name__}: {e}"
        result.stage = "verify_failed"
        result.verify_errors += 1
        return
    # Extract per-claim details — v0.8.3: use ALL details from verify_sources
    # (which now ranks by query and returns facts from top-1 source). The
    # old behaviour only used the first fact (stubbed the rest) which
    # suppressed the actual verification signal. Now synthesis sees the
    # real mix of SUPPORTS / INSUFFICIENT / etc.
    details = verify_result.get("details") or verify_result.get("verification_details") or []
    # Build claims_with_results in shape synthesis expects
    claims_with_results: list[dict] = []
    for d in details:
        claims_with_results.append(
            {
                "fact": d.get("fact", main_fact),
                "verdict": d.get("verdict", "INSUFFICIENT"),
                "reasoning": d.get("reasoning", ""),
                "supporting_sources": d.get("supporting_sources", []),
                "refuting_sources": d.get("refuting_sources", []),
                "numeric_mismatch_sources": d.get("numeric_mismatch_sources", []),
                "verified": d.get("verified", False),
            }
        )
    # NOTE: do NOT add stubs for facts[1:] here — those facts are already
    # represented in details from verify_sources (which calls _extract_facts
    # internally with the same query). Adding them again would inflate
    # total and suppress the SUPPORTS count.
    result.claims_verified = len(claims_with_results)
    result.claims_supported = sum(1 for c in claims_with_results if c["verified"])
    # Track LLM call count and model
    llm_meta = verify_result.get("llm_meta") or {}
    result.llm_calls = llm_meta.get(
        "calls",
        1
        if any(
            c.get("supporting_sources")
            and any("llm" in str(s) for s in c.get("supporting_sources", []))
            or c.get("refuting_sources")
            and any("llm" in str(s) for s in c.get("refuting_sources", []))
            for c in claims_with_results
        )
        else 0,
    )

    # 5. SYNTHESIS
    result.stage = "synthesis"
    try:
        synth = synthesize(
            query=qtext,
            claims=[c["fact"] for c in claims_with_results],
            results=claims_with_results,
            source_candidates=sources,
        )
    except Exception as e:
        result.error = f"synthesis: {type(e).__name__}: {e}"
        result.stage = "synthesis_failed"
        result.synthesis_errors += 1
        return
    result.coverage_score = synth.coverage.get("score", 0.0) if isinstance(synth.coverage, dict) else 0.0
    result.confidence = synth.confidence
    result.contradiction_rate = len(synth.contradictions) / max(len(claims_with_results), 1)
    result.synthesis_citations = len(synth.citations)

    # 6. CRITICAL REVIEW
    result.stage = "review"
    try:
        rv = review(
            synth,
            claims=[c["fact"] for c in claims_with_results],
            results=claims_with_results,
            source_candidates=sources,
        )
    except Exception:
        # Review failure is non-fatal; keep synthesis scores
        result.risk_level = "unknown"
        result.stage = "review_failed"
        result.review_errors += 1
    else:
        result.risk_level = rv.risk_level
        # Apply confidence adjustment (only downward per design)
        result.confidence = max(0.0, result.confidence + rv.confidence_adjustment)
    result.stage = "done"


def score_query(
    query_data: dict,
    *,
    no_network: bool = True,
) -> QueryResult:
    """Прогоняет один query через pipeline, возвращает QueryResult.

    Args:
        query_data: dict из eval_set.json
        no_network: True → skip web_search/fetch (offline baseline)
    """
    t0 = time.time()
    qid = query_data["id"]
    qtext = query_data["query"]
    expected_route = query_data.get("expected_route", "general")

    result = QueryResult(
        query_id=qid,
        query=qtext,
        expected_route=expected_route,
        category=query_data.get("category", "unknown"),
        main_query="",
        needs_confirmation=False,
        dropped_terms=[],
        route_predicted="",
        route_match=False,
    )

    try:
        # 1. ROUTING (6.3) — always, both modes
        intent = classify_intent(qtext)
        result.route_predicted = intent.route
        result.route_match = intent.route == expected_route

        # 2. QUERY ADAPTATION (6.1) — always, both modes
        plan = adapt_query(qtext)
        result.main_query = plan.get("main_query", "")
        result.needs_confirmation = plan.get("needs_confirmation", False)
        result.dropped_terms = plan.get("dropped_terms", [])

        # 3. PIPELINE — offline or online
        if no_network:
            _score_query_offline(result)
        else:
            _run_online_pipeline(result)

        # 4. COMPUTE QS (works in both modes)
        # `no_confirmation` kept in breakdown for diagnostic visibility, but NOT
        # used in the QS formula (see weight comments above).
        result.qs_breakdown = {
            "coverage": result.coverage_score,
            "no_contradictions": 1.0 - result.contradiction_rate,
            "confidence": result.confidence,
            "routing_precision": 1.0 if result.route_match else 0.0,
            "no_confirmation": 0.0 if result.needs_confirmation else 1.0,  # diagnostic only
        }
        result.qs = round(
            W_COVERAGE * result.qs_breakdown["coverage"]
            + W_NO_CONTRADICTIONS * result.qs_breakdown["no_contradictions"]
            + W_CONFIDENCE * result.qs_breakdown["confidence"]
            + W_ROUTING_PRECISION * result.qs_breakdown["routing_precision"],
            4,
        )

    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.stage = result.stage or "exception"

    result.elapsed_sec = round(time.time() - t0, 3)
    result.mode = "offline" if no_network else "online"
    return result


def aggregate_results(results: list[QueryResult]) -> dict:
    """Считает aggregate metrics по списку результатов."""
    n = len(results)
    if n == 0:
        return {"count": 0}

    qs_values = [r.qs for r in results if r.qs is not None]
    confirmation_count = sum(1 for r in results if r.needs_confirmation)
    route_match_count = sum(1 for r in results if r.route_match)
    error_count = sum(1 for r in results if r.error)
    online_count = sum(1 for r in results if r.mode == "online")
    avg_coverage = sum(r.coverage_score for r in results) / n
    avg_confidence = sum(r.confidence for r in results) / n
    total_llm_calls = sum(r.llm_calls for r in results)
    avg_stage = sum(1 for r in results if r.stage == "done") / n

    # v0.8.1.3: aggregate silent-skip counters across all results.
    # If any of these grow, retrieval is degrading silently and the QS
    # numbers above may be misleadingly stable.
    skip_counters = {
        "urls_total": sum(r.urls_total for r in results),
        "urls_skipped_duplicate": sum(r.urls_skipped_duplicate for r in results),
        "urls_skipped_deny_pattern": sum(r.urls_skipped_deny_pattern for r in results),
        "urls_skipped_canonical": sum(r.urls_skipped_canonical for r in results),
        "fetch_errors": sum(r.fetch_errors for r in results),
        "urls_empty_or_error": sum(r.urls_empty_or_error for r in results),
        "search_errors": sum(r.search_errors for r in results),
        "search_no_results": sum(r.search_no_results for r in results),
        "verify_errors": sum(r.verify_errors for r in results),
        "synthesis_errors": sum(r.synthesis_errors for r in results),
        "review_errors": sum(r.review_errors for r in results),
    }

    return {
        "count": n,
        "qs_mean": round(sum(qs_values) / n, 4) if qs_values else 0.0,
        "qs_min": round(min(qs_values), 4) if qs_values else 0.0,
        "qs_max": round(max(qs_values), 4) if qs_values else 0.0,
        "confirmation_rate": round(confirmation_count / n, 4),
        "routing_accuracy": round(route_match_count / n, 4),
        "error_count": error_count,
        "online_count": online_count,
        "avg_coverage": round(avg_coverage, 4),
        "avg_confidence": round(avg_confidence, 4),
        "total_llm_calls": total_llm_calls,
        "pipeline_completion_rate": round(avg_stage, 4),
        "skip_counters": skip_counters,
    }


def format_report(results: list[QueryResult], aggregate: dict) -> str:
    """Format human-readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"EVAL REPORT ({aggregate['count']} queries, {aggregate.get('online_count', 0)} online)")
    lines.append("=" * 70)
    lines.append(
        f"QS mean: {aggregate['qs_mean']:.4f}  "
        f"(min {aggregate['qs_min']:.4f} / max {aggregate['qs_max']:.4f})"
    )
    if aggregate.get("online_count", 0) > 0:
        lines.append(
            f"  coverage avg: {aggregate.get('avg_coverage', 0):.4f} | "
            f"confidence avg: {aggregate.get('avg_confidence', 0):.4f} | "
            f"LLM calls: {aggregate.get('total_llm_calls', 0)} | "
            f"pipeline done: {aggregate.get('pipeline_completion_rate', 0):.1%}"
        )
        # v0.8.1.3: silent-skip counters (observability for degraded retrieval)
        sc = aggregate.get("skip_counters", {})
        if sc:
            lines.append(
                f"  URLs: total={sc.get('urls_total', 0)} | "
                f"skipped(dup)={sc.get('urls_skipped_duplicate', 0)} | "
                f"skipped(deny)={sc.get('urls_skipped_deny_pattern', 0)} | "
                f"skipped(canonical)={sc.get('urls_skipped_canonical', 0)} | "
                f"fetch_errors={sc.get('fetch_errors', 0)} | "
                f"empty_or_error={sc.get('urls_empty_or_error', 0)}"
            )
            lines.append(
                f"  Pipeline errors: search={sc.get('search_errors', 0)} | "
                f"search_no_results={sc.get('search_no_results', 0)} | "
                f"verify={sc.get('verify_errors', 0)} | "
                f"synthesis={sc.get('synthesis_errors', 0)} | "
                f"review={sc.get('review_errors', 0)}"
            )
    lines.append(f"Confirmation rate: {aggregate['confirmation_rate']:.1%}  (target: 0%)")
    lines.append(f"Routing accuracy: {aggregate['routing_accuracy']:.1%}")
    lines.append(f"Errors: {aggregate['error_count']}")
    lines.append("")
    lines.append("Per-query breakdown:")
    lines.append("-" * 70)
    for r in results:
        flag = "❌" if r.error else "  "
        match = "✓" if r.route_match else "✗"
        conf = "!" if r.needs_confirmation else " "
        stage = r.stage or "-"
        cov = f"{r.coverage_score:.2f}" if r.mode == "online" else "  -"
        cnf = f"{r.confidence:.2f}" if r.mode == "online" else "  -"
        lines.append(
            f"{flag} {r.query_id:20s} | QS {r.qs:.3f} | "
            f"route {match} ({r.route_predicted:8s}/{r.expected_route:8s}) | "
            f"conf{conf} | cov={cov} cnf={cnf} | stage={stage:14s} | "
            f"dropped: {r.dropped_terms}"
        )
    lines.append("-" * 70)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Quality Score eval runner")
    parser.add_argument(
        "--set",
        type=Path,
        default=REPO_ROOT / "data" / "eval_set.json",
        help="Path to eval_set.json (default: <repo>/data/eval_set.json)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=REPO_ROOT / "data" / "eval_log.jsonl",
        help="Path to eval_log.jsonl (append; default: <repo>/data/eval_log.jsonl)",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        default=True,
        help="Skip web_search/fetch (offline baseline mode, DEFAULT)",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        default=False,
        help="Real pipeline: web_search + fetch + LLM verify + synthesis",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Run only this query_id (для debugging)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't append to log",
    )
    args = parser.parse_args()

    # Resolve mode
    no_network = not args.online  # --online wins over --no-network

    # Load eval set
    if not args.set.exists():
        print(f"ERROR: eval set not found: {args.set}", file=sys.stderr)
        return 1

    eval_set = json.loads(args.set.read_text(encoding="utf-8"))
    queries = eval_set["queries"]
    if args.query:
        queries = [q for q in queries if q["id"] == args.query]
        if not queries:
            print(f"ERROR: query {args.query!r} not found in {args.set}", file=sys.stderr)
            return 1

    mode_label = "online" if args.online else "offline"
    print(f"Running {len(queries)} queries (mode: {mode_label})...")

    # Run
    results = [score_query(q, no_network=no_network) for q in queries]
    aggregate = aggregate_results(results)

    # Report
    print()
    print(format_report(results, aggregate))

    # Persist
    if not args.dry_run:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        run_record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "mode": mode_label,
            "set_version": eval_set.get("version"),
            "aggregate": aggregate,
            "queries": [asdict(r) for r in results],
        }
        with args.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(run_record, ensure_ascii=False) + "\n")
        print(f"\nAppended to: {args.log}")

    # Exit code based on errors
    return 0 if aggregate["error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
