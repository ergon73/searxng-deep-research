"""
E2E smoke: full run_research() pipeline (v0.8.1.1 regression check).

This is the simplest possible e2e: it confirms that
`run_research(query, approved_plan=True, max_iterations=1, use_llm=False)`
goes all the way through the pipeline (plan → search → fetch → rank →
extract → verify → synthesize → review) and returns a ResearchResult
with status="done".

We monkeypatch the network layer (SearXNG + URL fetch) so this test
is hermetic — no Docker, no internet, no LLM. The point is to catch
runtime errors introduced by the ranking changes (bad imports, wrong
signatures, key errors, etc.).

What we verify:
  1. run_research() returns successfully.
  2. status == "done" (not "needs_confirmation", not "error").
  3. state.documents is non-empty.
  4. Each document has source_score attached (ranking ran).
  5. synthesis is not None and answer_markdown is non-empty.

Usage:
  PYTHONPATH=src python3 scripts/e2e_smoke_pipeline.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# Minimal corpus — 2 docs, no errors, both on-topic.
SYNTHETIC_HITS = [
    {
        "url": "https://en.wikipedia.org/wiki/Apple_Inc.",
        "title": "Apple Inc. - Wikipedia",
        "content": "Apple Inc. is an American multinational technology company.",
        "engine": "wikipedia",
    },
    {
        "url": "https://www.apple.com/about",
        "title": "Apple - Official Site",
        "content": "Apple was founded in 1976.",
        "engine": "duckduckgo",
    },
]

SYNTHETIC_FETCH_RESULTS = {
    "https://en.wikipedia.org/wiki/Apple_Inc.": {
        "url": "https://en.wikipedia.org/wiki/Apple_Inc.",
        "title": "Apple Inc. - Wikipedia",
        "text": "Apple Inc. is an American multinational technology company. "
                "It was founded by Steve Jobs, Steve Wozniak, and Ronald Wayne "
                "on April 1, 1976. The company is headquartered in Cupertino, California. "
                "Apple designs and sells consumer electronics, software, and online services. "
                "Its best-known products include the iPhone, iPad, Mac, and Apple Watch. " * 5,
        "length": 800,
        "error": None,
    },
    "https://www.apple.com/about": {
        "url": "https://www.apple.com/about",
        "title": "Apple - Official Site",
        "text": "Apple was founded in 1976. The company creates iPhone, iPad, Mac, "
                "and other products. Innovation is at the core of everything we do. " * 3,
        "length": 400,
        "error": None,
    },
}


def main() -> int:
    t0 = time.time()
    print("=" * 70)
    print("E2E SMOKE: full run_research() pipeline (v0.8.1.1)")
    print("=" * 70)
    print()

    query = "When was Apple founded and by whom?"
    print(f"Query: {query}")
    print()

    from research_runner import run_research
    import research_runner

    # Monkeypatch network layer.
    orig_dispatch = research_runner._dispatch_search_task
    orig_fetch = research_runner._fetch_documents
    research_runner._dispatch_search_task = lambda task, max_results=8: list(SYNTHETIC_HITS)
    research_runner._fetch_documents = lambda urls, *, max_chars=4000: [
        SYNTHETIC_FETCH_RESULTS[u] for u in urls
    ]
    try:
        result = run_research(query, approved_plan=True, max_iterations=1, use_llm=False)
    finally:
        research_runner._dispatch_search_task = orig_dispatch
        research_runner._fetch_documents = orig_fetch

    elapsed = round(time.time() - t0, 2)
    print(f"Pipeline status: {result.status}")
    if result.error:
        print(f"Pipeline error: {result.error}")
    print(f"Elapsed: {elapsed}s")
    print()

    # --- Show what we got --------------------------------------------

    docs = list(result.state.documents) if result.state else []
    print(f"Documents in state ({len(docs)}):")
    for i, d in enumerate(docs, 1):
        score = d.get("source_score", "MISSING")
        url = d.get("url", "?")
        text_len = len(d.get("text", "") or "")
        print(f"  {i}. score={score} | url={url[:50]} | text_len={text_len}")
    print()

    if result.synthesis:
        print(f"Synthesis:")
        print(f"  answer_markdown len: {len(result.synthesis.answer_markdown)}")
        print(f"  confidence: {result.synthesis.confidence}")
        print(f"  citations: {len(result.synthesis.citations)}")
        print()

    if result.review:
        print(f"Review:")
        print(f"  risk_level: {result.review.risk_level}")
        print(f"  risk_score: {result.review.risk_score}")
        print()

    # --- Assertions ----------------------------------------------------

    failures: list[str] = []

    if result.status != "done":
        failures.append(f"NOT_DONE: status is {result.status!r}, expected 'done'")

    if not docs:
        failures.append("NO_DOCUMENTS: state.documents is empty")

    for i, d in enumerate(docs):
        if "source_score" not in d:
            failures.append(
                f"NO_SOURCE_SCORE: doc {i} ({d.get('url', '?')}) lacks source_score"
            )

    if result.synthesis is None:
        failures.append("NO_SYNTHESIS: result.synthesis is None")
    elif not result.synthesis.answer_markdown:
        failures.append("EMPTY_ANSWER: synthesis.answer_markdown is empty")

    # --- Verdict -------------------------------------------------------

    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    print(f"Failures: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    print(f"Elapsed: {elapsed}s")

    out_dir = Path("/tmp/e2e-smoke-pipeline")
    out_dir.mkdir(parents=True, exist_ok=True)
    trace = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": elapsed,
        "query": query,
        "status": result.status,
        "error": result.error,
        "documents": [
            {"url": d.get("url"), "source_score": d.get("source_score")}
            for d in docs
        ],
        "synthesis_present": result.synthesis is not None,
        "review_present": result.review is not None,
        "failures": failures,
    }
    out_path = out_dir / "trace.json"
    out_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTrace: {out_path}")

    if failures:
        print("\n❌ E2E PIPELINE FAILED")
        return 1
    print("\n✅ E2E PIPELINE PASSED — run_research() end-to-end works after ranking change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
