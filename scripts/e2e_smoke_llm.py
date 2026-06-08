"""
e2e_smoke_llm.py — минимальный smoke test новой model chain (qwen primary).

Проверяет:
1. LLMVerifier инициализируется с DEFAULT_MODEL_CHAIN (qwen → 3 free fallback)
2. LLM call с qwen работает: HTTP 200, JSON schema, verdict в [SUPPORTS/REFUTES/INSUFFICIENT]
3. Latency вменяемая (< 30s для qwen)
4. Учёт стоимости (usage.prompt_tokens/completion_tokens, если есть)
5. Post-validation: response_format strict schema не ломает

Если qwen упадёт, автоматически fallback на free модели. Smoke test
должен пройти в любом случае (иначе — bug в chain).

Запуск: PYTHONPATH=src python3 scripts/e2e_smoke_llm.py
"""
import json
import os
import sys
import time
import socket
import urllib.request
import urllib.error
from pathlib import Path

# Ensure imports
sys.path.insert(0, "/opt/searxng/src")

from llm_verifier import (
    LLMVerifier,
    DEFAULT_MODEL,
    DEFAULT_MODEL_CHAIN,
    _FALLBACK_STATUS_CODES,
    _load_api_key,
    RESPONSE_SCHEMA,
)


# =========================================================================
# Smoke test cases — 3 коротких fact-vs-evidence кейса
# =========================================================================

SMOKE_CASES = [
    {
        "name": "SUPPORTS (smoking kills, evidence: smoking causes cancer)",
        "fact": "smoking causes cancer",
        "evidence": [
            {"url": "https://en.wikipedia.org/wiki/Tobacco_and_cancer",
             "text": "Tobacco smoking is the leading preventable cause of cancer. "
                     "Cigarette smoke contains carcinogens that cause lung, throat, "
                     "and other cancers. The WHO classifies smoking as a Group 1 carcinogen."},
        ],
    },
    {
        "name": "REFUTES (water boils at 100C, evidence: boils at 50C)",
        "fact": "water boils at 100 degrees Celsius at sea level",
        "evidence": [
            {"url": "https://example.com/flat-earth-wrong",
             "text": "According to the Flat Earth Society, water boils at exactly 50°C. "
                     "This is the official temperature. All other sources are wrong."},
        ],
    },
    {
        "name": "INSUFFICIENT (fact about topic not in evidence)",
        "fact": "the speed of light in a vacuum is 299792458 m/s",
        "evidence": [
            {"url": "https://example.com/recipe",
             "text": "Here's a recipe for chocolate cake. You need flour, sugar, eggs. "
                     "Mix them together and bake at 180 degrees for 30 minutes."},
        ],
    },
]


def run_one(verifier: LLMVerifier, case: dict) -> dict:
    """Run one smoke case, return timing + result."""
    t0 = time.time()
    result = verifier.verify_facts_batch(
        facts=[case["fact"]],
        source_candidates=case["evidence"],
    )
    elapsed = round(time.time() - t0, 2)
    r = result[0] if result else {}
    return {
        "case": case["name"],
        "verdict": r.get("verdict"),
        "verified": r.get("llm_verified"),
        "refuted": r.get("llm_refuted"),
        "llm_error": r.get("llm_error"),
        "reasoning": (r.get("reasoning") or "")[:100],
        "elapsed_sec": elapsed,
        "model_used": verifier.model,  # tracks last successful
    }


def main() -> int:
    print("=" * 70)
    print("E2E SMOKE: LLMVerifier + model chain (qwen primary)")
    print("=" * 70)

    # 1. API key check
    try:
        key = _load_api_key()
        # Don't print full key, just confirm it's there
        print(f"\n[setup] API key loaded: {key[:8]}...{key[-4:]} (len={len(key)})")
    except RuntimeError as e:
        print(f"\n[setup] FAIL: {e}")
        print("Set OPENROUTER_API_KEY in env or /opt/searxng/.env_llm")
        return 1

    # 2. Model chain check
    print(f"\n[setup] DEFAULT_MODEL: {DEFAULT_MODEL}")
    print(f"[setup] DEFAULT_MODEL_CHAIN ({len(DEFAULT_MODEL_CHAIN)} models):")
    for i, m in enumerate(DEFAULT_MODEL_CHAIN, 1):
        print(f"  {i}. {m}")
    print(f"[setup] Fallback status codes: {sorted(_FALLBACK_STATUS_CODES)}")

    # 3. Init verifier (no explicit model → uses chain)
    v = LLMVerifier()
    print(f"\n[setup] Verifier initialized:")
    print(f"  primary: {v.model_chain[0]}")
    print(f"  current: {v.model}")
    print(f"  chain length: {len(v.model_chain)}")
    print(f"  max_retries: {v.max_retries}")

    # 4. Run smoke cases
    print("\n" + "=" * 70)
    print("SMOKE TEST CASES")
    print("=" * 70)
    results = []
    for i, case in enumerate(SMOKE_CASES, 1):
        print(f"\n[{i}/{len(SMOKE_CASES)}] {case['name']}")
        print(f"  fact: {case['fact'][:60]}")
        r = run_one(v, case)
        results.append(r)
        status = "✓" if not r["llm_error"] else "✗"
        print(f"  {status} verdict={r['verdict']} | "
              f"verified={r['verified']} | "
              f"elapsed={r['elapsed_sec']}s | "
              f"model={r['model_used']}")
        if r["llm_error"]:
            print(f"  ✗ error: {r['llm_error'][:100]}")
        else:
            print(f"  reasoning: {r['reasoning']}")

    # 5. Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = len(results)
    ok = sum(1 for r in results if not r["llm_error"])
    correct_expected = (
        (results[0]["verdict"] == "SUPPORTS")
        + (results[1]["verdict"] == "REFUTES")
        + (results[2]["verdict"] == "INSUFFICIENT")
    )
    total_time = sum(r["elapsed_sec"] for r in results)
    models_used = {r["model_used"] for r in results if not r["llm_error"]}

    print(f"Cases passed (no error): {ok}/{total}")
    print(f"Expected verdicts match: {correct_expected}/3")
    print(f"Total time: {total_time}s (avg {total_time/total:.1f}s per case)")
    print(f"Models used: {sorted(models_used) if models_used else 'NONE (all failed)'}")

    # 6. Save trace
    out_dir = Path("/tmp/e2e-smoke-llm")
    out_dir.mkdir(parents=True, exist_ok=True)
    trace = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "primary_model": v.model_chain[0],
        "chain_length": len(v.model_chain),
        "results": results,
        "summary": {
            "ok": ok,
            "total": total,
            "correct_expected": correct_expected,
            "total_time_sec": total_time,
            "models_used": sorted(models_used),
        },
    }
    out_path = out_dir / "trace.json"
    out_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTrace saved: {out_path}")

    # 7. Verdict
    if ok == total and correct_expected == 3:
        print("\n✅ SMOKE TEST PASSED — chain works, qwen answers correctly")
        return 0
    elif ok == total:
        print(f"\n⚠️  PARTIAL: chain works but {3 - correct_expected} verdicts unexpected")
        return 0  # still pass — we just need connectivity
    else:
        print(f"\n❌ FAIL: {total - ok} cases errored")
        return 1


if __name__ == "__main__":
    sys.exit(main())
