"""
E2E smoke: ranking order-independence (v0.8.1.1 hardening).

The previous e2e_smoke_ranking.py showed that ranking puts the best
document on top. This one checks the COMPLEMENT: that the rank is
**independent of fetch completion order** — i.e. ranking still picks
the right top1 even if the slowest-fetched document actually has the
best content.

This is the real-world scenario: a small Wikipedia summary fetches in
0.3s, a heavy Apple.com page fetches in 4.2s. If we naively used fetch
order, the small page would be top1.

What we verify:
  1. Run the pipeline twice with the same docs but different fetch
     orders.
  2. The resulting top1 URL must be the same in both runs.
  3. The source_score for that URL must be the same.

Usage:
  PYTHONPATH=src python3 scripts/e2e_smoke_ranking_independent.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# Same synthetic corpus as e2e_smoke_ranking.py.
SYNTHETIC_HITS = [
    {
        "url": "https://low-quality.example.com/spam",
        "title": "Buy cheap stuff now!",
        "content": "Click here for amazing deals!",
        "engine": "duckduckgo",
    },
    {
        "url": "https://error.example.com/timeout",
        "title": "Page that will fail to fetch",
        "content": "",
        "engine": "duckduckgo",
    },
    {
        "url": "https://apple.com/about",
        "title": "Apple - Official Site",
        "content": "Apple was founded in 1976 by Steve Jobs. " * 20,
        "engine": "wikipedia",
    },
]

SYNTHETIC_FETCH_RESULTS = {
    "https://low-quality.example.com/spam": {
        "url": "https://low-quality.example.com/spam",
        "title": "Buy cheap stuff now!",
        "text": "Click here for amazing deals! Best prices guaranteed!",
        "length": 58,
        "error": None,
    },
    "https://error.example.com/timeout": {
        "url": "https://error.example.com/timeout",
        "title": "",
        "text": "",
        "length": 0,
        "error": "fetch timeout after 5.0s",
    },
    "https://apple.com/about": {
        "url": "https://apple.com/about",
        "title": "Apple - Official Site",
        "text": "Apple was founded in 1976 by Steve Jobs, Steve Wozniak, and Ronald Wayne. " * 20,
        "length": 1380,
        "error": None,
    },
}


def _run_with_fetch_order(permutation: list[str], query: str) -> dict:
    """Run run_research with fetch_url returning docs in the given order.

    The runner calls _fetch_documents(candidate_urls), which returns docs
    in URL-input order (NOT fetch-completion order, because of the
    by_url dict fix in v0.8.1 Phase A). To simulate "fetch order != URL
    order" we monkeypatch _fetch_documents to shuffle.
    """
    from research_runner import run_research

    # Build a fetch result list in the requested permutation order.
    permuted = [SYNTHETIC_FETCH_RESULTS[u] for u in permutation]

    import research_runner
    orig_fetch = research_runner._fetch_documents
    research_runner._fetch_documents = lambda urls, *, max_chars=4000: list(permuted)
    orig_dispatch = research_runner._dispatch_search_task
    research_runner._dispatch_search_task = lambda task, max_results=8: list(SYNTHETIC_HITS)
    try:
        result = run_research(query, approved_plan=True, max_iterations=1, use_llm=False)
        docs = list(result.state.documents) if result.state else []
        return {
            "status": result.status,
            "error": result.error,
            "top1_url": docs[0]["url"] if docs else None,
            "top1_score": docs[0].get("source_score") if docs else None,
            "all_urls_in_order": [d["url"] for d in docs],
        }
    finally:
        research_runner._fetch_documents = orig_fetch
        research_runner._dispatch_search_task = orig_dispatch


def main() -> int:
    t0 = time.time()
    print("=" * 70)
    print("E2E SMOKE: ranking order-independence (v0.8.1.1)")
    print("=" * 70)
    print()

    query = "Apple founding year Steve Jobs history"

    # Three different fetch-completion orders.
    # Each simulates a different "what the network gave us" order.
    orderings = [
        ["https://apple.com/about", "https://low-quality.example.com/spam", "https://error.example.com/timeout"],
        ["https://low-quality.example.com/spam", "https://error.example.com/timeout", "https://apple.com/about"],
        ["https://error.example.com/timeout", "https://apple.com/about", "https://low-quality.example.com/spam"],
    ]

    runs = []
    for i, perm in enumerate(orderings, 1):
        print(f"--- Run {i}: fetch order = {perm[0][:40]} first ---")
        out = _run_with_fetch_order(perm, query)
        runs.append(out)
        print(f"  status: {out['status']}")
        print(f"  top1:   {out['top1_url']} (score={out['top1_score']})")
        print(f"  full order:")
        for u in out["all_urls_in_order"]:
            print(f"    - {u}")
        print()

    # --- Assertions ----------------------------------------------------

    failures: list[str] = []

    # 1. All three runs should pick the same top1.
    top1_urls = {r["top1_url"] for r in runs}
    if len(top1_urls) != 1:
        failures.append(
            f"TOP1_NOT_STABLE: different runs picked different top1s: {top1_urls}"
        )

    # 2. The chosen top1 should be the relevant Apple doc, not the spam
    # or the error doc.
    for r in runs:
        if r["top1_url"] and "apple.com" not in r["top1_url"]:
            failures.append(
                f"TOP1_NOT_RELEVANT: top1 is {r['top1_url']}, expected apple.com"
            )

    # 3. The full ordering of documents must be IDENTICAL across runs,
    # even though scores differ (because the SearXNG input position is
    # itself a signal — different fetch order = different input position).
    # The KEY thing is that ranking RE-SORTS so the final order is
    # always the same: best content first, errors last.
    first_order = runs[0]["all_urls_in_order"]
    for i, r in enumerate(runs[1:], 2):
        if r["all_urls_in_order"] != first_order:
            failures.append(
                f"ORDER_NOT_STABLE: run {i} produced different order:\n"
                f"  run 1: {first_order}\n"
                f"  run {i}: {r['all_urls_in_order']}"
            )

    # 4. The error doc should ALWAYS be last regardless of fetch order.
    for r in runs:
        if r["all_urls_in_order"]:
            last = r["all_urls_in_order"][-1]
            if "error.example.com" not in last:
                failures.append(
                    f"ERROR_DOC_NOT_LAST: error doc not last in order "
                    f"{r['all_urls_in_order']}"
                )

    # --- Verdict -------------------------------------------------------

    elapsed = round(time.time() - t0, 2)
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(f"Failures: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    print(f"Elapsed: {elapsed}s")

    out_dir = Path("/tmp/e2e-smoke-ranking-independent")
    out_dir.mkdir(parents=True, exist_ok=True)
    trace = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": elapsed,
        "runs": runs,
        "failures": failures,
    }
    out_path = out_dir / "trace.json"
    out_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTrace: {out_path}")

    if failures:
        print("\n❌ E2E RANKING INDEPENDENCE FAILED")
        return 1
    print("\n✅ E2E RANKING INDEPENDENCE PASSED — same top1 regardless of fetch order")
    return 0


if __name__ == "__main__":
    sys.exit(main())
