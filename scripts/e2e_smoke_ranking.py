"""
E2E smoke: source ranking (v0.8.1.1 hardening).

Simulates a full pipeline pass with monkeypatched search + fetch,
asserting that documents are ranked by source_score (not URL order).

Why this is "e2e" rather than a unit test:
  - Calls `run_research()` through the public API (the entrypoint users hit).
  - Monkeypatches the network layer (`_dispatch_search_task` + `fetch_url`),
    NOT the ranking layer. So if rank_documents() is bypassed, the
    assertions fail — proving the fix is actually wired in.
  - The "documents" are realistic: long-vs-short, on-topic-vs-off-topic,
    with-vs-without errors. The ranking must pick on-topic + long ones.

What we verify:
  1. top1 is the highest-scored document, not the first URL.
  2. The runner adds source_score to each document.
  3. The order survives into `state.documents`.
  4. Documents with fetch errors drop to the bottom.

Usage:
  PYTHONPATH=src python3 scripts/e2e_smoke_ranking.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Make src importable (portable: derive from this file's location, not /opt/searxng).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# --- Test fixtures ------------------------------------------------------

# Realistic-ish search results: 1 highly relevant, 1 marginal, 1 error.
# Order in the list is the WORST possible — a high-quality document LAST.
# If ranking works, top1 should be the relevant one, not the first URL.
SYNTHETIC_HITS = [
    {
        "url": "https://low-quality.example.com/spam",
        "title": "Buy cheap stuff now!",
        "content": "Click here for amazing deals! Best prices guaranteed!",
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
        "content": "Apple was founded in 1976 by Steve Jobs, Steve Wozniak, and Ronald Wayne. "
        "The company is headquartered in Cupertino, California. " * 20,
        "engine": "wikipedia",
    },
]

# What fetch_url should "return" for each URL. This simulates a real
# network: some pages are empty/error, some are full content.
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
        "text": "Apple was founded in 1976 by Steve Jobs, Steve Wozniak, and Ronald Wayne. "
        "The company is headquartered in Cupertino, California. " * 20,
        "length": 1380,
        "error": None,
    },
}


# --- E2E pipeline setup -------------------------------------------------


def _run_e2e(query: str = "Apple founding year") -> dict:
    """Run run_research() with monkeypatched network. Return state.documents."""
    from research_runner import _dispatch_search_task, _fetch_documents, run_research

    # Save original functions to restore after.
    orig_dispatch = _dispatch_search_task
    orig_fetch = _fetch_documents

    def fake_dispatch(task, max_results=8):
        return list(SYNTHETIC_HITS)  # always the same 3 hits

    def fake_fetch(urls, *, max_chars=4000):
        out = []
        for u in urls:
            out.append(
                SYNTHETIC_FETCH_RESULTS.get(
                    u,
                    {"url": u, "title": "", "text": "", "length": 0, "error": "not in fixtures"},
                )
            )
        return out

    # Patch.
    import research_runner

    research_runner._dispatch_search_task = fake_dispatch
    research_runner._fetch_documents = fake_fetch

    try:
        # approved_plan=True so we skip the confirmation gate.
        result = run_research(query, approved_plan=True, max_iterations=1, use_llm=False)
        docs = list(result.state.documents) if result.state else []
        return {
            "status": result.status,
            "error": result.error,
            "documents": docs,
            "n_documents": len(docs),
        }
    finally:
        # Restore.
        research_runner._dispatch_search_task = orig_dispatch
        research_runner._fetch_documents = orig_fetch


# --- Main ---------------------------------------------------------------


def main() -> int:
    t0 = time.time()
    print("=" * 70)
    print("E2E SMOKE: source ranking (v0.8.1.1)")
    print("=" * 70)
    print()
    print("Synthetic corpus (3 docs, deliberately bad URL order):")
    for i, h in enumerate(SYNTHETIC_HITS, 1):
        print(f"  {i}. {h['url'][:50]} | {h['title'][:40]}")
    print()

    # Run pipeline.
    out = _run_e2e("Apple founding year Steve Jobs history")
    print(f"Pipeline status: {out['status']}")
    if out["error"]:
        print(f"Pipeline error: {out['error']}")
    print(f"Documents returned: {out['n_documents']}")
    print()

    # Show what we got, in pipeline order.
    print("Documents in pipeline order:")
    for i, d in enumerate(out["documents"], 1):
        score = d.get("source_score", "MISSING")
        url = d.get("url", "?")
        text_len = len(d.get("text", "") or "")
        error = d.get("error")
        print(f"  {i}. score={score} | url={url[:50]} | text_len={text_len} | error={error}")
    print()

    # --- Assertions ----------------------------------------------------

    failures: list[str] = []

    # 1. At least one document came back.
    if out["n_documents"] == 0:
        failures.append("NO_DOCUMENTS: pipeline returned 0 documents")

    # 2. Every document has a source_score attached.
    for i, d in enumerate(out["documents"]):
        if "source_score" not in d:
            failures.append(f"NO_SOURCE_SCORE: doc {i} ({d.get('url', '?')}) lacks source_score")
        elif not isinstance(d["source_score"], (int, float)):
            failures.append(f"BAD_SOURCE_SCORE: doc {i} has non-numeric source_score")

    # 3. Documents are in descending source_score order.
    scores = [d.get("source_score", 0) for d in out["documents"]]
    if scores != sorted(scores, reverse=True):
        failures.append(f"NOT_SORTED_DESC: scores in pipeline order are {scores}, expected descending")

    # 4. The error document (timeout) is at the bottom (score = 0).
    if out["n_documents"] >= 2:
        last_doc = out["documents"][-1]
        if last_doc.get("error") and last_doc.get("source_score", 1) > 0:
            failures.append(
                f"ERROR_DOC_NOT_LAST: error doc {last_doc.get('url')} "
                f"has source_score={last_doc.get('source_score')}, should be 0 and last"
            )

    # 5. The relevant Apple doc (long, on-topic) is at the TOP.
    # It should beat the spam and the error doc.
    if out["n_documents"] >= 1:
        top1 = out["documents"][0]
        if "apple.com" not in top1.get("url", ""):
            failures.append(
                f"TOP1_NOT_RELEVANT: top1 is {top1.get('url')}, expected apple.com/about. "
                f"URL-order bias not fixed!"
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

    # Persist trace.
    out_dir = Path("/tmp/e2e-smoke-ranking")
    out_dir.mkdir(parents=True, exist_ok=True)
    trace = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": elapsed,
        "pipeline_status": out["status"],
        "pipeline_error": out["error"],
        "documents": [
            {
                "url": d.get("url"),
                "title": d.get("title"),
                "text_len": len(d.get("text", "") or ""),
                "source_score": d.get("source_score"),
                "error": d.get("error"),
            }
            for d in out["documents"]
        ],
        "failures": failures,
    }
    out_path = out_dir / "trace.json"
    out_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTrace: {out_path}")

    if failures:
        print("\n❌ E2E RANKING SMOKE FAILED")
        return 1
    print("\n✅ E2E RANKING SMOKE PASSED — top-1 is the best source, not URL order")
    return 0


if __name__ == "__main__":
    sys.exit(main())
