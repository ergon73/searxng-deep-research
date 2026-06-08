"""
e2e_falcon9.py — end-to-end smoke test для всех 8+1 скиллов.

Запрос: "Сколько ступеней у ракеты Falcon 9 и когда первый запуск"
(англ. variant для routing)

Pipeline:
  1. routing.suggest_route()      (6.3)
  2. query_adaptation.adapt_query() (6.1) — confirmation gate
  3. web_search() + fetch_url()    (SearXNG)
  4. extract_facts() из top-1     (existing)
  5. evidence.extract_windows()    (6.4)
  6. verify_sources() per fact     (existing + 6.5 NUMERIC_MISMATCH)
  7. synthesis.synthesize()         (6.6)
  8. critical_review.review()       (6.7)
  9. release_packaging не триггерим тут (отдельный skill)

Оффлайн-режим: use_llm=False → LLM verifier выключен, только deterministic
checks. Это покажет 6.1, 6.3, 6.4, 6.5 (без LLM), 6.6, 6.7.

Outputs:
  - stdout: summary
  - /tmp/e2e-falcon9-*.json: full trace
"""
import json
import sys
import time
from pathlib import Path

# Ensure imports
sys.path.insert(0, "/opt/searxng/src")

from query_adaptation import adapt_query, build_search_plan_preview
from routing import classify_intent
from evidence import extract_windows, windows_to_blob
from synthesis import synthesize, enrich_with_llm
from critical_review import review
from hermes_searxng import web_search
from hermes_deepresearch import (
    _extract_facts,
    fetch_url,
    verify_sources,
    canonical_url,
)

QUERY = "Сколько ступеней у ракеты Falcon 9 и в каком году первый запуск"
ENG_QUERY = "Falcon 9 rocket stages first launch year"

OUT_DIR = Path("/tmp/e2e-falcon9")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    t0 = time.time()
    print("=" * 60)
    print(f"E2E: {QUERY}")
    print("=" * 60)

    # 1. ROUTING (6.3)
    print("\n[1/8] routing.classify_intent()")
    intent = classify_intent(QUERY)
    print(f"  route: {intent.route} | confidence: {intent.confidence:.2f}")
    print(f"  engines: {intent.engines} | categories: {intent.categories}")
    print(f"  time_range: {intent.time_range}")
    print(f"  warning: {intent.routing_warning}")
    if intent.query_variants:
        print(f"  variants: {intent.query_variants[:2]}")
    if intent.all_routes:
        print(f"  all_routes: {intent.all_routes[:3]}")

    # 2. QUERY ADAPTATION (6.1)
    print("\n[2/8] query_adaptation.adapt_query()")
    plan = adapt_query(QUERY)
    print(f"  main_query: {plan['main_query']}")
    print(f"  needs_confirmation: {plan.get('needs_confirmation', False)}")
    if plan.get("needs_confirmation"):
        print(f"  reason: {plan.get('confirmation_reason')}")
    print(f"  adaptation_confidence: {plan.get('adaptation_confidence', 0):.2f}")
    print(f"  dropped_terms: {plan.get('dropped_terms', [])}")
    print(f"  route: {plan.get('route')}")
    print(f"  adaptation_method: {plan.get('adaptation_method')}")

    # 3. WEB_SEARCH + FETCH (используем EN variant для лучшего покрытия)
    print("\n[3/8] web_search() + fetch_url()")
    search_q = ENG_QUERY
    results = web_search(
        search_q,
        lang="en",
        time_range=intent.time_range or "year",
        engines=intent.engines,
        max_results=5,
    )
    print(f"  Got {len(results)} results")
    for r in results[:5]:
        print(f"    - {r.get('title', '?')[:50]} | {r.get('url', '?')[:50]}")

    # 4. FETCH top-3 unique URLs
    print("\n[4/8] fetch top-3 sources")
    seen_urls: set[str] = set()
    sources: list[dict] = []
    for r in results:
        url = r.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        canonical = canonical_url(url)
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        try:
            content = fetch_url(url, timeout=15.0, max_chars=4000)
        except Exception as e:
            print(f"    SKIP {url[:50]}: {e}")
            continue
        if not content or content.get("error"):
            print(f"    SKIP {url[:50]}: {content.get('error') if content else 'no content'}")
            continue
        sources.append({
            "url": url,
            "title": r.get("title", ""),
            "text": content.get("text", "")[:4000],
        })
        if len(sources) >= 3:
            break
    print(f"  Fetched {len(sources)} sources")
    for s in sources:
        print(f"    - {s['title'][:50]} ({len(s['text'])} chars)")

    if not sources:
        print("FAIL: no sources fetched")
        return 1

    # 5. EXTRACT FACTS из top-1
    print("\n[5/8] extract facts from top-1")
    top1_text = sources[0]["text"]
    facts = _extract_facts(top1_text, max_facts=6)
    print(f"  Extracted {len(facts)} facts:")
    for i, f in enumerate(facts, 1):
        print(f"    {i}. {f[:80]}")

    # 6. EVIDENCE WINDOWS (6.4) — для каждого fact в каждом source
    print("\n[6/8] evidence.extract_windows() per fact per source")
    evidence_blocks: list[dict] = []
    for fact in facts:
        for src in sources:
            windows = extract_windows(src["text"], fact, window_size=300, max_windows=2)
            if windows:
                blob = windows_to_blob(windows)
                evidence_blocks.append({
                    "fact": fact,
                    "url": src["url"],
                    "text": blob,
                    "windows_count": len(windows),
                })
    print(f"  Generated {len(evidence_blocks)} evidence blocks")

    # 7. VERIFY SOURCES (6.5) — deterministic mode (use_llm=False)
    print("\n[7/8] verify_sources() deterministic")
    claims_with_results: list[dict] = []
    # Берём top-1 fact и verify его против top-1 source + other sources
    if facts:
        # Сгруппировать evidence blocks по fact
        # Берём первый fact
        main_fact = facts[0]
        # Find which source contained this fact
        main_source = None
        other = []
        for src in sources:
            windows = extract_windows(src["text"], main_fact, window_size=300, max_windows=2)
            if windows and main_source is None:
                main_source = src
            else:
                other.append({"url": src["url"], "text": src["text"]})

        if main_source is None:
            # Fallback: use first source as main
            main_source = sources[0]
            other = [{"url": s["url"], "text": s["text"]} for s in sources[1:]]

        result = verify_sources(
            top1={"url": main_source["url"], "text": main_source["text"]},
            other_sources=other,
            query=main_fact,
            use_llm=False,  # OFFLINE MODE
        )
        d = result.get("details", [{}])[0] if result.get("details") else {}
        claims_with_results.append({
            "fact": main_fact,
            "verdict": d.get("verdict", "INSUFFICIENT"),
            "reasoning": d.get("reasoning", ""),
            "supporting_sources": d.get("supporting_sources", []),
            "refuting_sources": d.get("refuting_sources", []),
            "numeric_mismatch_sources": d.get("numeric_mismatch_sources", []),
            "verified": d.get("verified", False),
        })

    # For remaining facts, mark as INSUFFICIENT (would need separate LLM call in production)
    for fact in facts[1:4]:
        claims_with_results.append({
            "fact": fact,
            "verdict": "INSUFFICIENT",
            "reasoning": "(offline mode: only first fact verified)",
            "supporting_sources": [],
            "refuting_sources": [],
            "numeric_mismatch_sources": [],
            "verified": False,
        })

    print(f"  Verified {len(claims_with_results)} claims (1 fully, rest stub)")
    for c in claims_with_results[:6]:
        print(f"    - {c['verdict']}: {c['fact'][:60]}")

    # 8. SYNTHESIS (6.6) — deterministic
    print("\n[8/8] synthesis.synthesize() + critical_review.review()")
    synth = synthesize(
        query=QUERY,
        claims=[c["fact"] for c in claims_with_results],
        results=claims_with_results,
        source_candidates=sources,
    )
    print(f"  citations: {len(synth.citations)}")
    print(f"  coverage: {synth.coverage}")
    print(f"  contradictions: {len(synth.contradictions)}")
    print(f"  confidence: {synth.confidence:.3f}")
    print(f"  open_questions: {len(synth.open_questions)}")
    print(f"  enriched_by_llm: {synth.enriched_by_llm}")

    # 9. CRITICAL REVIEW (6.7)
    rv = review(
        synth,
        claims=[c["fact"] for c in claims_with_results],
        results=claims_with_results,
        source_candidates=sources,
    )
    print(f"  risk_level: {rv.risk_level}")
    print(f"  risk_score: {rv.risk_score:.3f}")
    print(f"  flags: {len(rv.flags)}")
    for f in rv.flags[:5]:
        print(f"    [{f.severity}] {f.category}: {f.message[:80]}")
    print(f"  recommendations: {rv.recommendations}")
    print(f"  confidence_adjustment: {rv.confidence_adjustment:.3f}")

    # FINAL CONFIDENCE
    final_conf = max(0.0, synth.confidence + rv.confidence_adjustment)
    print(f"\n  FINAL CONFIDENCE: {final_conf:.3f}")

    # Persist
    elapsed = round(time.time() - t0, 2)
    output = {
        "query": QUERY,
        "elapsed_sec": elapsed,
        "routing": {
            "route": intent.route,
            "confidence": intent.confidence,
            "engines": intent.engines,
            "categories": intent.categories,
            "time_range": intent.time_range,
            "query_variants": intent.query_variants[:2],
            "routing_warning": intent.routing_warning,
        },
        "query_adaptation": {
            "main_query": plan["main_query"],
            "needs_confirmation": plan.get("needs_confirmation", False),
            "confirmation_reason": plan.get("confirmation_reason"),
            "adaptation_confidence": plan.get("adaptation_confidence", 0),
            "dropped_terms": plan.get("dropped_terms", []),
            "adaptation_method": plan.get("adaptation_method"),
        },
        "search_results_count": len(results),
        "sources_fetched": [
            {"url": s["url"], "title": s["title"], "len": len(s["text"])}
            for s in sources
        ],
        "facts_extracted": facts,
        "claims_verified": [
            {
                "fact": c["fact"],
                "verdict": c["verdict"],
                "supporting_n": len(c["supporting_sources"]),
                "refuting_n": len(c["refuting_sources"]),
                "mismatch_n": len(c["numeric_mismatch_sources"]),
            }
            for c in claims_with_results
        ],
        "synthesis": {
            "citations_count": len(synth.citations),
            "coverage": synth.coverage,
            "contradictions_count": len(synth.contradictions),
            "confidence": synth.confidence,
            "open_questions_count": len(synth.open_questions),
            "enriched_by_llm": synth.enriched_by_llm,
            "answer_markdown_len": len(synth.answer_markdown),
        },
        "review": {
            "risk_level": rv.risk_level,
            "risk_score": rv.risk_score,
            "flags_count": len(rv.flags),
            "flags_by_category": {
                cat: len([f for f in rv.flags if f.category == cat])
                for cat in {f.category for f in rv.flags}
            },
            "recommendations": rv.recommendations,
            "confidence_adjustment": rv.confidence_adjustment,
        },
        "final_confidence": final_conf,
    }
    out_path = OUT_DIR / "trace.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTrace saved: {out_path}")

    # Save markdown answer
    md_path = OUT_DIR / "answer.md"
    md_path.write_text(synth.answer_markdown, encoding="utf-8")
    print(f"Answer saved: {md_path}")

    print(f"\nElapsed: {elapsed}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
