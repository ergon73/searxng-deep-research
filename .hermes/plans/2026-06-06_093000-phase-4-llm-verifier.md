# Phase 4 Implementation Plan — LLM verifier semantics

> **For Hermes:** TDD workflow. Tests first, patch second. Use `/senior-dev` mindset.
> Generated 2026-06-06 after P1+P2+P3 done.

**Goal:** Align `llm_verifier.py` with core verification semantics. Currently LLM uses `MATCH/NO MATCH` (binary), but core uses `SUPPORTS/REFUTES/INSUFFICIENT/CONFLICTING`. Per DR §10-14, fix:
1. Verdict enum: `SUPPORTS | REFUTES | INSUFFICIENT`
2. `response_format`: `json_object` → `json_schema` (strict)
3. LLM errors: returned as `llm_error` field, not silently swallowed
4. `verify_fact` (semantically broken per DR §14) → remove OR replace with `verify_claim_against_evidence`

**Architecture:** Surgical rewrite of `llm_verifier.py::LLMVerifier`:
1. **Constants:** `VERDICT_SUPPORTS = "SUPPORTS"`, `VERDICT_REFUTES = "REFUTES"`, `VERDICT_INSUFFICIENT = "INSUFFICIENT"`
2. **Schema:** strict json_schema with `enum: [SUPPORTS, REFUTES, INSUFFICIENT]` for verdict
3. **Prompt:** "decide whether the sources SUPPORT, REFUTE, or provide INSUFFICIENT evidence"
4. **Error handling:** add `socket.timeout` to caught exceptions; on any error, return `{"verdict": None, "llm_error": "..."}` instead of fake "NO MATCH"
5. **API cleanup:** delete `verify_fact` (semantically broken). Add new `verify_claim_against_evidence(claim, evidence_blocks)` per DR §14
6. **Wire-up:** `verify_sources()` in `hermes_deepresearch.py` adds `llm_error` to return dict

**Tech Stack:** Python 3.11 stdlib, urllib.request, json. No new deps.

**Out of scope:**
- Ranking / news routing (Phase 5)
- Proxy (Phase 6)
- Docs (Phase 8)
- Replacing the LLM model (DR §12 — note for Phase 5 or separate)

---

## Audit findings (read-only, already done)

The current `llm_verifier.py` has 4 structural problems vs DR §Phase 4:

### Problem A: Wrong verdict enum (DR §10)

```python
# Current prompt in verify_facts_batch (L195):
f'Reply JSON: {{"results": [{{"index": 1, "verdict": "MATCH"|"NO MATCH", ...}}]}}'

# Current mapping (L242):
"llm_verified": match.get("verdict") == "MATCH"
```

LLM is asked binary `MATCH/NO MATCH`. Core verifier uses 4-value `SUPPORTS/REFUTES/INSUFFICIENT/CONFLICTING` (CONFLICTING is computed locally, not from LLM). The LLM cannot return refutation.

**Fix:** Prompt + schema enum → `SUPPORTS | REFUTES | INSUFFICIENT`. Mapping: `llm_verified = verdict == "SUPPORTS"`, `llm_refuted = verdict == "REFUTES"`.

### Problem B: `json_object` instead of `json_schema` (DR §11)

```python
# Current (L207):
"response_format": {"type": "json_object"}
```

This is "any JSON object". For strict batch verification with array of `{index, verdict, reasoning, source_urls}`, `json_schema` with `strict: True` is required.

**Fix:** Add `json_schema` block per DR §11. Schema:
```python
{
    "type": "json_schema",
    "json_schema": {
        "name": "fact_verification_batch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "verdict": {"type": "string", "enum": ["SUPPORTS", "REFUTES", "INSUFFICIENT"]},
                            "reasoning": {"type": "string"},
                            "source_urls": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["index", "verdict", "reasoning", "source_urls"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["results"],
            "additionalProperties": False,
        },
    },
}
```

### Problem C: LLM errors silently swallowed (DR §13)

```python
# Current verify_facts_batch (L252):
except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
    last_err = e
    ...
    return [
        {"fact": f, "llm_verified": False, "reasoning": f"LLM error: {last_err}"}
        for f in facts
    ]
```

Two issues:
1. `socket.timeout` is NOT in the except list — when OpenRouter hangs, the exception propagates uncaught (observed in test).
2. The error is in `reasoning` text, not a structured `llm_error` field. The user can't programmatically distinguish "LLM said NO MATCH" from "LLM call failed with timeout".

**Fix:**
- Add `socket.timeout` to except list.
- Return `{"verdict": None, "llm_verified": False, "llm_error": f"{type(e).__name__}: {e}", "reasoning": ""}` on error.
- Normal flow returns `{"verdict": "SUPPORTS", "llm_verified": True, "llm_refuted": False, "llm_error": None, "reasoning": "..."}`.

### Problem D: `verify_fact` is semantically broken (DR §14)

```python
# Current (L146-165):
def verify_fact(self, fact, top1_context, candidate_context):
    prompt = (
        f'Fact from primary source: "{fact_safe}"\n\n'
        f'Context A (primary): {top1_safe}\n'
        f'Context B (candidate): {cand_safe}\n\n'
        f'Is the fact from Context A confirmed by Context B? '
        f'Reply JSON: {{"verdict": "MATCH" or "NO MATCH", "reasoning": "<short>"}}'
    )
    return self._ask_llm(fact_safe, prompt)
```

This is asked as "is fact from A confirmed by B?", but `_ask_llm` actually asks "are A and B about the same fact?" (L91-95). The prompt and the function are inconsistent.

DR §14: "Если этот метод больше не используется — удалить. Если используется — переписать отдельно."

Check usage: `grep -r "verify_fact" /opt/searxng --include="*.py" --exclude-dir=tests`. Only `llm_verifier.py` defines it; no caller. Safe to delete.

**Fix:** Delete `verify_fact` and `_ask_llm`. Add new `verify_claim_against_evidence(claim, evidence_blocks)` per DR §14.

### Problem E: `verify_sources()` swallows LLM errors at the integration site

```python
# Current hermes_deepresearch.py (L807-809):
except Exception:
    # LLM-verify failed silently — keep fuzzy results
    pass
```

This catches everything from `LLMVerifier()` construction, API call, JSON parse. The user gets no signal.

**Fix:** Track `llm_error = None` at start. On any exception, set `llm_error = f"{type(e).__name__}: {e}"`. Return `llm_error` in the verification dict.

---

## Tests to write BEFORE patching (10 tests, all in `tests/test_llm_verifier.py`)

```python
class TestVerdictEnum:        # 4 tests
    # SUPPORTS → llm_verified=True
    # REFUTES → llm_verified=False, llm_refuted=True
    # INSUFFICIENT → llm_verified=False, llm_refuted=False
    # Legacy MATCH → not passed through (normalized to INSUFFICIENT)

class TestResponseFormat:     # 1 test
    # Capture request body, assert response_format.type == "json_schema" with strict=True
    # Assert verdict enum is exactly {SUPPORTS, REFUTES, INSUFFICIENT}

class TestLLMError:           # 3 tests
    # 401 HTTPError → results have llm_error
    # socket.timeout → results have llm_error (currently NOT caught!)
    # bad JSON → results have llm_error (currently in reasoning text)

class TestVerifyFactRemoval:  # 2 tests
    # LLMVerifier.verify_fact does NOT exist
    # LLMVerifier.verify_claim_against_evidence(claim, evidence_blocks) works
```

All tests use `monkeypatch.setattr("urllib.request.urlopen", ...)` — no real OpenRouter calls.

---

## Task 1: Create test_llm_verifier.py with 10 failing tests (DONE — see red run)

## Task 2: Patch llm_verifier.py

### Edit 1: Add constants

```python
# Verdict enum (DR §10)
VERDICT_SUPPORTS = "SUPPORTS"
VERDICT_REFUTES = "REFUTES"
VERDICT_INSUFFICIENT = "INSUFFICIENT"
VALID_VERDICTS = {VERDICT_SUPPORTS, VERDICT_REFUTES, VERDICT_INSUFFICIENT}

# Strict response schema (DR §11)
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "fact_verification_batch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "verdict": {"type": "string", "enum": [VERDICT_SUPPORTS, VERDICT_REFUTES, VERDICT_INSUFFICIENT]},
                            "reasoning": {"type": "string"},
                            "source_urls": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["index", "verdict", "reasoning", "source_urls"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["results"],
            "additionalProperties": False,
        },
    },
}
```

### Edit 2: Rewrite `verify_facts_batch`

New prompt:
```python
prompt_system = (
    "You are a fact verification assistant. For each fact, decide whether "
    "the sources SUPPORT, REFUTE, or provide INSUFFICIENT evidence. "
    "Return JSON only, no extra text."
)
prompt_user = (
    f"Verify each fact against the supporting sources below.\n\n"
    f"Facts:\n{facts_block}\n\n"
    f"Supporting sources:\n{sources_block}\n\n"
    f'Reply JSON: {{"results": [{{"index": 1, "verdict": "SUPPORTS"|"REFUTES"|"INSUFFICIENT", "reasoning": "<short>", "source_urls": [...]}}]}}'
)
```

New error handling:
```python
import socket
...
except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, json.JSONDecodeError, KeyError) as e:
    llm_error = f"{type(e).__name__}: {e}"
    return [
        {"fact": f, "verdict": None, "llm_verified": False, "llm_refuted": False, "llm_error": llm_error, "reasoning": ""}
        for f in facts
    ]
```

New mapping:
```python
for i, fact in enumerate(facts):
    match = next((r for r in results_raw if r.get("index") == i + 1), None)
    if not match:
        out.append({"fact": fact, "verdict": None, "llm_verified": False, "llm_refuted": False, "llm_error": "no LLM result for this fact", "reasoning": ""})
        continue
    raw_verdict = match.get("verdict", "").strip().upper()
    # Defensive: normalize legacy MATCH/NO MATCH to INSUFFICIENT
    if raw_verdict == "MATCH":
        verdict = VERDICT_SUPPORTS
    elif raw_verdict == "NO MATCH":
        verdict = VERDICT_INSUFFICIENT
    elif raw_verdict in VALID_VERDICTS:
        verdict = raw_verdict
    else:
        verdict = VERDICT_INSUFFICIENT  # unknown → conservative
    out.append({
        "fact": fact,
        "verdict": verdict,
        "llm_verified": verdict == VERDICT_SUPPORTS,
        "llm_refuted": verdict == VERDICT_REFUTES,
        "llm_error": None,
        "reasoning": match.get("reasoning", "")[:200],
    })
```

### Edit 3: Delete `verify_fact` and `_ask_llm`

DR §14: `verify_fact` is semantically broken and unused.

### Edit 4: Add `verify_claim_against_evidence(claim, evidence_blocks)`

```python
def verify_claim_against_evidence(self, claim: str, evidence_blocks: list[dict]) -> dict:
    """
    Verify a single claim against a list of evidence blocks.
    Returns: {"verdict": "SUPPORTS"|"REFUTES"|"INSUFFICIENT", "llm_verified": bool,
              "llm_refuted": bool, "llm_error": str|None, "reasoning": str}
    """
    if not claim or not evidence_blocks:
        return {"verdict": VERDICT_INSUFFICIENT, "llm_verified": False, "llm_refuted": False,
                "llm_error": "no claim or evidence", "reasoning": ""}
    results = self.verify_facts_batch(
        facts=[claim],
        source_candidates=[{"url": e.get("url", "?"), "text": e.get("text", "")} for e in evidence_blocks],
    )
    if not results:
        return {"verdict": VERDICT_INSUFFICIENT, "llm_verified": False, "llm_refuted": False,
                "llm_error": "empty batch result", "reasoning": ""}
    return results[0]
```

## Task 3: Patch `verify_sources()` in hermes_deepresearch.py

### Edit 5: Add `llm_error` to return dict + integration

```python
# Before LLM-verify block (L774):
llm_enhanced = False
llm_verified_count = 0
llm_latency = 0.0
llm_error = None  # NEW

if use_llm and rate < LLM_VERIFY_THRESHOLD:
    unverified = [d for d in details if not d["verified"]]
    if unverified:
        try:
            verifier = LLMVerifier()
            t0 = time.time()
            llm_results = verifier.verify_facts_batch(
                facts=[d["fact"] for d in unverified],
                source_candidates=[
                    {"url": s.get("url", "?"), "text": s.get("text", "")[:2000]}
                    for s in other_sources if not s.get("error")
                ][:3],
            )
            llm_latency = round(time.time() - t0, 2)

            # Map back — use new verdict enum
            for d, lr in zip(unverified, llm_results):
                verdict = lr.get("verdict")
                if verdict == "SUPPORTS":
                    d["verified"] = True
                    d["verdict"] = "SUPPORTS"
                    d["method"] = "llm"
                    d["supporting_sources"].append(("llm_batch", 0, "llm"))
                    llm_verified_count += 1
                elif verdict == "REFUTES":
                    d["verdict"] = "REFUTES"
                    d["method"] = "llm"
                    d["refuting_sources"].append("llm_batch")
                # INSUFFICIENT → no change to d
                # None (error) → no change
                # Propagate llm_error if set
                if lr.get("llm_error"):
                    d["llm_error"] = lr["llm_error"]

            # Recompute rate
            verified_count = sum(1 for d in details if d["verified"])
            rate = verified_count / total if total else 0.0
            llm_enhanced = True
        except Exception as e:
            # Track error, do NOT swallow
            llm_error = f"{type(e).__name__}: {e}"

# Return:
return {
    "verified_facts": verified_count,
    "total_facts": total,
    "verification_rate": round(rate, 3),
    "verification_details": details,
    "llm_enhanced": llm_enhanced,
    "llm_verified_count": llm_verified_count,
    "llm_latency": llm_latency,
    "llm_error": llm_error,  # NEW
}
```

---

## Acceptance criteria (Phase 4 done = all of these)

- [x] `python3 -m pytest -q` shows 115 passed, 0 failed (105 prior + 10 new for P4) — **VERIFIED 2026-06-06: 117 passed** (10 new in test_llm_verifier.py + 2 new в test_verify_schema.py)
- [x] `python3 -m ruff check tests/test_llm_verifier.py` clean — **VERIFIED 2026-06-06** (1 warning I001 in initial commit, убран)
- [x] `python3 -m ruff check llm_verifier.py hermes_deepresearch.py` shows pre-existing errors only (no new ones from P4) — **VERIFIED 2026-06-06** (16 pre-existing style errors: S310, UP045, I001, UP041 — все не от P4)
- [x] `LLMVerifier.verify_fact` attribute does NOT exist — **VERIFIED** by `TestVerifyFactRemoval::test_verify_fact_method_removed`
- [x] `LLMVerifier.verify_claim_against_evidence(claim, evidence_blocks)` works — **VERIFIED** by `TestVerifyFactRemoval::test_new_verify_claim_against_evidence_exists`
- [x] `verify_facts_batch` returns `verdict` ∈ {SUPPORTS, REFUTES, INSUFFICIENT} — **VERIFIED** by `TestVerdictEnum` (3 tests + legacy MATCH mapping)
- [x] `verify_facts_batch` returns `llm_error` on HTTP/timeout/JSON errors — **VERIFIED** by `TestLLMError` (3 tests)
- [x] `verify_facts_batch` request body uses `response_format.type == "json_schema"` with `strict: True` — **VERIFIED** by `TestResponseFormat::test_request_uses_json_schema`
- [x] `verify_sources()` returns `llm_error` field (None on success, str on failure) — **VERIFIED** by `tests/test_verify_schema.py::TestVerifySources::test_llm_error_propagates_from_verifier`
- [x] No public API breakage for `verify_sources()` callers (all 5 existing tests still pass) — **VERIFIED** by `tests/test_verify_schema.py` (7 tests pass: 5 pre-existing + 2 new)

## What I will NOT do in Phase 4

- Won't change the default LLM model (DR §12 — separate concern; user can set `LLM_MODEL` env var)
- Won't touch `web_search()` or `hermes_searxng.py`
- Won't change `_extract_facts()` or `_match_in_text()`
- Won't add new ranking strategies
- Won't change proxy/compose/docs
