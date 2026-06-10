"""
llm_verifier.py — LLM-based fact verification через OpenRouter.

Используется в hermes_deepresearch.verify_sources() для conditional
LLM-усиления verification (когда fuzzy + synonym даёт < 70% rate).

API (v0.8.2 — Phase 4):
    verifier = LLMVerifier()  # читает OPENROUTER_API_KEY из env или /opt/searxng/.env_llm
    result = verifier.verify_facts_batch(
        facts=["123 дрона", "5 сбито"],
        source_candidates=[{"url": "...", "text": "..."}, ...],
    )
    # [{"fact", "verdict": "SUPPORTS"|"REFUTES"|"INSUFFICIENT",
    #   "llm_verified": bool, "llm_refuted": bool, "llm_error": str|None, "reasoning": str}]

    result = verifier.verify_claim_against_evidence(claim, evidence_blocks)
    # same shape, single claim

Security:
- Sanitize inputs (replace null bytes, strip control chars)
- Cap fact/context до 500/2000 chars (защита от prompt injection)
- max_tokens=200 (verdict + reasoning + source_urls)
- 2 retries на transient errors (429, 5xx, timeout)
- OpenRouter API key берётся из:
  1. os.environ['OPENROUTER_API_KEY']
  2. /opt/searxng/.env_llm (если существует)
  3. RuntimeError если ни то, ни другое
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

# Skill 6.4: evidence window extraction. Pure-function, no LLM/network.
try:
    from evidence import EvidenceWindow, extract_windows, windows_to_blob

    _HAS_EVIDENCE = True
except ImportError:
    _HAS_EVIDENCE = False


DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

# Model chain 2026-06-07: qwen3-235b-a22b-2507 (Instruct) primary, free fallbacks.
# Order matters: tried top-to-bottom. We switch on transient errors
# (429/5xx/timeout) but NOT on schema errors (those are our bug, not the model's).
# Override with env LLM_MODEL_CHAIN=comma,separated,ids for ad-hoc experiments.
DEFAULT_MODEL_CHAIN = (
    "qwen/qwen3-235b-a22b-2507",  # primary: Instruct, $0.09/$0.10, 262K ctx
    "mistralai/mistral-small-3.2-24b-instruct:free",  # free fallback #1
    "google/gemini-2.0-flash-exp:free",  # free fallback #2 (structured)
    "meta-llama/llama-3.1-8b-instruct:free",  # last resort (legacy default)
)
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ENV_FILE = Path("/opt/searxng/.env_llm")

# HTTP status codes that should trigger fallback (transient errors).
# 4xx client errors (except 429) are usually our bug (bad schema, bad prompt)
# and switching model won't help — surface them immediately.
_FALLBACK_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Verdict enum (DR §10) — single source of truth
VERDICT_SUPPORTS = "SUPPORTS"
VERDICT_REFUTES = "REFUTES"
VERDICT_INSUFFICIENT = "INSUFFICIENT"
# v0.8.2-B1: WEAK_SUPPORT — SUPPORTS от LLM без валидных source_urls.
# НЕ возвращается LLM напрямую — это caller-computed downgrade (см. hermes_deepresearch.verify_sources).
# LLM schema enum остаётся без WEAK_SUPPORT (strict json_schema).
VERDICT_WEAK_SUPPORT = "WEAK_SUPPORT"
VALID_VERDICTS = {VERDICT_SUPPORTS, VERDICT_REFUTES, VERDICT_INSUFFICIENT}

# Strict response schema (DR §11) — гарантирует структуру, не "any JSON"
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
                            "verdict": {
                                "type": "string",
                                "enum": [VERDICT_SUPPORTS, VERDICT_REFUTES, VERDICT_INSUFFICIENT],
                            },
                            "reasoning": {"type": "string"},
                            "source_urls": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
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


def _load_api_key() -> str:
    """Читает API key: сначала os.environ, потом /opt/searxng/.env_llm."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "LLM_API_KEY":
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v

    placeholder = "sk-or-v1-..."
    raise RuntimeError(
        "OPENROUTER_API_KEY not set. Set os.environ['OPENROUTER_API_KEY'] or create "
        "/opt/searxng/.env_llm with LLM_API_KEY=" + placeholder
    )


def _sanitize(text: str, max_chars: int) -> str:
    """Strip control chars, cap length."""
    if not text:
        return ""
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    return text[:max_chars].strip()


def _normalize_verdict(raw: str) -> str:
    """Defensive normalization of LLM verdict.

    - Strip whitespace, uppercase.
    - Legacy MATCH/NO MATCH (old prompt) → SUPPORTS/INSUFFICIENT.
    - Unknown → INSUFFICIENT (conservative default).
    """
    if not raw:
        return VERDICT_INSUFFICIENT
    v = raw.strip().upper()
    if v == "MATCH":
        return VERDICT_SUPPORTS
    if v == "NO MATCH" or v == "NO_MATCH":
        return VERDICT_INSUFFICIENT
    if v in VALID_VERDICTS:
        return v
    return VERDICT_INSUFFICIENT


class LLMVerifier:
    """Conditional LLM-based fact verification."""

    def __init__(
        self,
        model: str | None = None,
        max_retries: int = 2,
        model_chain: list | None = None,
    ):
        self.api_key = _load_api_key()
        # If explicit model given, use it (single-model mode, backward-compat).
        # Otherwise build chain from env override or default.
        if model:
            self.model = model
            self.model_chain = [model]
        else:
            env_chain = os.environ.get("LLM_MODEL_CHAIN", "").strip()
            if env_chain:
                self.model_chain = [m.strip() for m in env_chain.split(",") if m.strip()]
            elif model_chain:
                self.model_chain = list(model_chain)
            else:
                self.model_chain = list(DEFAULT_MODEL_CHAIN)
            self.model = self.model_chain[0]
        self.max_retries = max_retries
        self.endpoint = ENDPOINT

    def _is_fallback_status(self, exc: Exception) -> bool:
        """Decide whether an error should trigger model switch.

        Transient (5xx/429/timeout/connection) → switch model.
        Persistent (4xx other than 429) → likely our bug, surface immediately.
        """
        if isinstance(exc, (socket.timeout, urllib.error.URLError, ConnectionError)):
            return True
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in _FALLBACK_STATUS_CODES
        return False

    def _call_with_fallback(self, body: dict, timeout: float = 20.0) -> tuple[str, dict]:
        """POST to OpenRouter, trying each model in the chain on transient errors.

        Returns: (model_used, response_body)

        Raises:
            The last error if all models fail on transient errors, OR
            immediately on the first non-transient (4xx) error.
        """
        last_err: Exception | None = None
        for model in self.model_chain:
            body_for_model = {**body, "model": model}
            # We retry within a single model for transient errors (per max_retries),
            # but switch model if retries exhausted OR if a 4xx fires.
            for attempt in range(self.max_retries + 1):
                try:
                    data = json.dumps(body_for_model).encode("utf-8")
                    req = urllib.request.Request(
                        self.endpoint,
                        data=data,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://github.com/hermes-agent",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        return (model, json.loads(r.read().decode("utf-8")))
                except Exception as e:
                    last_err = e
                    if not self._is_fallback_status(e):
                        # Persistent error (bad schema, bad prompt) — our bug.
                        # Don't try other models, surface immediately.
                        raise
                    if attempt < self.max_retries:
                        time.sleep(2**attempt)
                        continue
                    # Exhausted retries on this model → try next.
                    break
        # All models exhausted.
        assert last_err is not None
        raise last_err

    def _post(self, body: dict, timeout: float = 20.0) -> dict:
        """POST to OpenRouter. Returns parsed JSON body. Raises on error."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/hermes-agent",
            },
        )
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise
        # Should not reach here
        raise RuntimeError(f"unreachable: {last_err}")

    # ------------------------------------------------------------------
    # Skill 6.4: evidence window extraction for LLM prompt
    # ------------------------------------------------------------------

    def _render_sources_block(
        self,
        facts: list[str],
        sources: list[dict],
        *,
        per_source_window_size: int = 300,
        per_source_max_total: int = 1500,
    ) -> str:
        """Render the 'Evidence sources:' block for the LLM prompt.

        For each source, we extract evidence windows around ANY of the
        facts (so a single source can serve multiple facts). Windows
        are concatenated, deduped by offset, and capped to fit in the
        prompt.

        Backward-compat: if evidence module is unavailable, falls back
        to the old behaviour (first 500 chars of each source).
        """
        if not sources:
            return ""

        if not _HAS_EVIDENCE:
            # Fallback: old behaviour
            return "\n".join(
                f"- {_sanitize(s.get('url', '?'), 200)}: {_sanitize(s.get('text', ''), 500)}" for s in sources
            )

        rendered = []
        for s in sources:
            url = _sanitize(s.get("url", "?"), 200)
            text = s.get("text", "")
            if not text:
                rendered.append(f"- {url}: (empty source)")
                continue

            # Collect windows from all facts for this source.
            all_windows: list[EvidenceWindow] = []
            seen_offsets: set[tuple[int, int]] = set()
            for fact in facts:
                wins = extract_windows(
                    text,
                    fact,
                    window_size=per_source_window_size,
                    max_windows=2,
                )
                for w in wins:
                    key = (w.offset_start, w.offset_end)
                    if key in seen_offsets:
                        continue
                    seen_offsets.add(key)
                    all_windows.append(w)

            # Sort by match_score desc, then by offset_start (stable).
            all_windows.sort(key=lambda w: (-w.match_score, w.offset_start))

            blob = windows_to_blob(all_windows, max_total_chars=per_source_max_total)
            blob = _sanitize(blob, per_source_max_total)
            rendered.append(f"- {url}: {blob}")

        return "\n".join(rendered)

    def verify_facts_batch(
        self,
        facts: list[str],
        source_candidates: list[dict],
    ) -> list[dict]:
        """
        Batch verify multiple facts in one LLM call.

        facts: list of fact strings (e.g. extracted from top-1)
        source_candidates: [{"url": "...", "text": "..."}, ...] — evidence sources

        Returns: list of dicts, one per fact:
            {
                "fact": str,
                "verdict": "SUPPORTS" | "REFUTES" | "INSUFFICIENT" | None,
                "llm_verified": bool,
                "llm_refuted": bool,
                "llm_error": str | None,  # populated only on error
                "reasoning": str,
            }
        """
        if not facts or not source_candidates:
            return [
                {
                    "fact": f,
                    "verdict": VERDICT_INSUFFICIENT,
                    "llm_verified": False,
                    "llm_refuted": False,
                    "llm_error": "empty facts or source_candidates" if f else "no fact",
                    "reasoning": "",
                }
                for f in facts
            ]

        facts_block = "\n".join(f'{i + 1}. "{_sanitize(f, 200)}"' for i, f in enumerate(facts))
        # Skill 6.4: extract evidence windows per source instead of feeding
        # the LLM the first 500 chars. The first 500 chars often don't
        # contain the claim, leading to false INSUFFICIENT verdicts.
        sources_block = self._render_sources_block(facts, source_candidates[:3])

        prompt_system = (
            "You are a fact verification assistant. For each fact, decide whether "
            "the sources SUPPORT, REFUTE, or provide INSUFFICIENT evidence. "
            "Return JSON only, no extra text."
        )
        prompt_user = (
            f"Verify each fact against the evidence sources below.\n\n"
            f"Facts:\n{facts_block}\n\n"
            f"Evidence sources:\n{sources_block}\n\n"
            f'Reply JSON: {{"results": [{{"index": 1, "verdict": "SUPPORTS"|"REFUTES"|"INSUFFICIENT", "reasoning": "<short>", "source_urls": [...]}}]}}'
        )

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user},
            ],
            "max_tokens": 200 + 80 * len(facts),
            "temperature": 0.0,
            # Strict schema per DR §11
            "response_format": RESPONSE_SCHEMA,
        }

        # Single LLM call with model fallback (qwen primary → free fallbacks)
        try:
            model_used, resp = self._call_with_fallback(body, timeout=20.0)
            self.model = model_used  # track which model succeeded (for debugging)
            content = resp["choices"][0]["message"]["content"].strip()
        except (
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            json.JSONDecodeError,
            KeyError,
        ) as e:
            llm_error = f"{type(e).__name__}: {e}"
            return [
                {
                    "fact": f,
                    "verdict": None,
                    "llm_verified": False,
                    "llm_refuted": False,
                    "llm_error": llm_error,
                    "reasoning": "",
                }
                for f in facts
            ]

        # Parse response. With strict json_schema, the result is guaranteed to
        # match the schema — but defensively try to extract JSON.
        try:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if not m:
                raise json.JSONDecodeError("no JSON object in response", content, 0)
            parsed = json.loads(m.group(0))
            results_raw = parsed.get("results", [])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            llm_error = f"JSONParseError: {type(e).__name__}: {e}"
            return [
                {
                    "fact": f,
                    "verdict": None,
                    "llm_verified": False,
                    "llm_refuted": False,
                    "llm_error": llm_error,
                    "reasoning": "",
                }
                for f in facts
            ]

        # Map back to original facts
        out = []
        for i, fact in enumerate(facts):
            match = next((r for r in results_raw if r.get("index") == i + 1), None)
            if not match:
                out.append(
                    {
                        "fact": fact,
                        "verdict": VERDICT_INSUFFICIENT,
                        "llm_verified": False,
                        "llm_refuted": False,
                        "llm_error": "no LLM result for this fact",
                        "reasoning": "",
                        "source_urls": [],
                    }
                )
                continue
            verdict = _normalize_verdict(match.get("verdict", ""))
            # v0.8.2-B1: extract raw source_urls from LLM response.
            # Caller (verify_sources) applies whitelist against source_candidates.
            raw_urls = match.get("source_urls") or []
            if not isinstance(raw_urls, list):
                raw_urls = []
            # Defensive: keep only non-empty strings, trim, cap per-fact.
            cleaned_urls: list[str] = []
            for u in raw_urls:
                if isinstance(u, str) and u.strip():
                    cleaned_urls.append(u.strip()[:500])
                if len(cleaned_urls) >= 10:
                    break
            out.append(
                {
                    "fact": fact,
                    "verdict": verdict,
                    "llm_verified": verdict == VERDICT_SUPPORTS,
                    "llm_refuted": verdict == VERDICT_REFUTES,
                    "llm_error": None,
                    "reasoning": (match.get("reasoning", "") or "")[:200],
                    "source_urls": cleaned_urls,
                }
            )

        return out

    def verify_claim_against_evidence(
        self,
        claim: str,
        evidence_blocks: list[dict],
    ) -> dict:
        """
        Verify a single claim against a list of evidence blocks (DR §14).

        Returns: {
            "fact": claim,
            "verdict": "SUPPORTS"|"REFUTES"|"INSUFFICIENT"|None,
            "llm_verified": bool,
            "llm_refuted": bool,
            "llm_error": str|None,
            "reasoning": str,
        }
        """
        if not claim:
            return {
                "fact": claim,
                "verdict": VERDICT_INSUFFICIENT,
                "llm_verified": False,
                "llm_refuted": False,
                "llm_error": "no claim",
                "reasoning": "",
                "source_urls": [],
            }
        if not evidence_blocks:
            return {
                "fact": claim,
                "verdict": VERDICT_INSUFFICIENT,
                "llm_verified": False,
                "llm_refuted": False,
                "llm_error": "no evidence blocks",
                "reasoning": "",
                "source_urls": [],
            }

        results = self.verify_facts_batch(
            facts=[claim],
            source_candidates=[
                {"url": e.get("url", "?"), "text": e.get("text", "")} for e in evidence_blocks
            ],
        )
        first = results[0] if results else None
        if not first:
            return {
                "fact": claim,
                "verdict": VERDICT_INSUFFICIENT,
                "llm_verified": False,
                "llm_refuted": False,
                "llm_error": "empty batch result",
                "reasoning": "",
                "source_urls": [],
            }
        # v0.8.2-B1: propagate source_urls from batch.
        first.setdefault("source_urls", [])
        return first


# Self-test
if __name__ == "__main__":
    print("=== LLMVerifier self-test (v0.8.2 — Phase 4) ===")
    v = LLMVerifier()

    test_cases = [
        ("Functionally equivalent", "Remove item", "Delete the element"),
        ("Adversarial (same word, different sense)", "Apple is a company", "An apple is a fruit"),
        ("Negation", "The file was not deleted", "The file was removed"),
    ]

    for name, claim, evidence in test_cases:
        result = v.verify_claim_against_evidence(claim, [{"url": "test", "text": evidence}])
        print(
            f"  {name}: verdict={result['verdict']}, verified={result['llm_verified']}, error={result.get('llm_error')}"
        )
        time.sleep(1)  # rate limit courtesy
