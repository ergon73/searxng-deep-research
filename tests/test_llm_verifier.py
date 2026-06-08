"""
LLM verifier semantics tests — DR §Phase 4 acceptance criteria.

Locks in:
  AC1. LLM verdict enum: SUPPORTS, REFUTES, INSUFFICIENT (NOT MATCH/NO MATCH)
  AC2. MATCH/NO MATCH fully removed from batch path
  AC3. response_format uses json_schema (with strict: True), NOT json_object
  AC4. LLM error returned as llm_error, NOT silently swallowed
  AC5. verify_fact (single-call) removed from public API (or rewritten as
       verify_claim_against_evidence per DR §14)
  AC6. tests without real OpenRouter — via monkeypatch urlopen
"""
import json
import urllib.error

from llm_verifier import LLMVerifier

# =============================================================================
# AC1, AC2 — verdict enum + remove MATCH/NO MATCH
# =============================================================================


class TestVerdictEnum:
    """AC1+AC2: SUPPORTS/REFUTES/INSUFFICIENT enum, no MATCH/NO MATCH."""

    def test_batch_returns_supports_verdict(self, monkeypatch):
        """LLM returns verdict SUPPORTS → verifier maps to SUPPORTS, llm_verified=True."""
        fake_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "results": [
                            {"index": 1, "verdict": "SUPPORTS", "reasoning": "matches", "source_urls": ["https://b.com"]}
                        ]
                    })
                }
            }]
        }
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout: _FakeResponse(json.dumps(fake_response).encode("utf-8")),
        )

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["123 дрона"],
            source_candidates=[{"url": "https://b.com", "text": "сбито 123 дрона"}],
        )
        assert len(results) == 1
        assert results[0]["verdict"] == "SUPPORTS", f"got: {results[0]}"
        assert results[0]["llm_verified"] is True
        assert "reasoning" in results[0]

    def test_batch_returns_refutes_verdict(self, monkeypatch):
        """LLM returns verdict REFUTES → verifier maps to REFUTES, llm_verified=False, llm_refuted=True."""
        fake_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "results": [
                            {"index": 1, "verdict": "REFUTES", "reasoning": "no evidence", "source_urls": []}
                        ]
                    })
                }
            }]
        }
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout: _FakeResponse(json.dumps(fake_response).encode("utf-8")),
        )

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["1000 дронов"],
            source_candidates=[{"url": "https://b.com", "text": "no drones mentioned"}],
        )
        assert len(results) == 1
        assert results[0]["verdict"] == "REFUTES", f"got: {results[0]}"
        assert results[0]["llm_verified"] is False
        assert results[0].get("llm_refuted") is True, f"llm_refuted missing: {results[0]}"

    def test_batch_returns_insufficient_verdict(self, monkeypatch):
        """LLM returns INSUFFICIENT → llm_verified=False, no llm_refuted."""
        fake_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "results": [
                            {"index": 1, "verdict": "INSUFFICIENT", "reasoning": "no info", "source_urls": []}
                        ]
                    })
                }
            }]
        }
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout: _FakeResponse(json.dumps(fake_response).encode("utf-8")),
        )

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["амброзия цвела"],
            source_candidates=[{"url": "https://b.com", "text": "..."}],
        )
        assert results[0]["verdict"] == "INSUFFICIENT"
        assert results[0]["llm_verified"] is False
        assert results[0].get("llm_refuted") is False

    def test_match_enum_is_removed(self, monkeypatch):
        """AC2: If LLM returns legacy 'MATCH' (old prompt), verifier must NOT pass it through as a verdict.
        The new schema enum forbids it, but defensively: unknown verdict → mapped to INSUFFICIENT."""
        fake_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "results": [
                            {"index": 1, "verdict": "MATCH", "reasoning": "legacy", "source_urls": []}
                        ]
                    })
                }
            }]
        }
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout: _FakeResponse(json.dumps(fake_response).encode("utf-8")),
        )

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["x"],
            source_candidates=[{"url": "https://b.com", "text": "x"}],
        )
        # "MATCH" не должен проходить как verdict — должен быть нормализован
        assert results[0]["verdict"] != "MATCH", f"legacy MATCH leaked: {results[0]}"
        # Либо INSUFFICIENT (защитное default), либо поднят как llm_error
        assert results[0]["verdict"] in ("SUPPORTS", "REFUTES", "INSUFFICIENT"), \
            f"unexpected verdict: {results[0]}"


# =============================================================================
# AC3 — json_schema response_format
# =============================================================================


class TestResponseFormat:
    """AC3: response_format is json_schema (strict), NOT json_object."""

    def test_request_uses_json_schema(self, monkeypatch):
        """Capture the request body sent to urlopen and verify response_format.type == 'json_schema'."""
        captured = {}

        def _capture(req, timeout):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(json.dumps({
                "choices": [{"message": {"content": json.dumps({"results": [
                    {"index": 1, "verdict": "SUPPORTS", "reasoning": "ok", "source_urls": []}
                ]})}}]
            }).encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", _capture)

        v = LLMVerifier()
        v.verify_facts_batch(facts=["f1"], source_candidates=[{"url": "u", "text": "t"}])

        body = captured["body"]
        rf = body.get("response_format", {})
        assert rf.get("type") == "json_schema", f"expected json_schema, got: {rf}"
        schema = rf.get("json_schema", {})
        assert schema.get("strict") is True, f"expected strict=True, got: {schema}"
        # Schema must have a results array with verdict enum
        item_schema = (
            schema.get("schema", {})
            .get("properties", {})
            .get("results", {})
            .get("items", {})
            .get("properties", {})
        )
        verdict_prop = item_schema.get("verdict", {})
        enum = verdict_prop.get("enum", [])
        assert set(enum) == {"SUPPORTS", "REFUTES", "INSUFFICIENT"}, f"enum mismatch: {enum}"
        # No 'MATCH' in enum
        assert "MATCH" not in enum and "NO MATCH" not in enum


# =============================================================================
# AC4 — LLM error returned, not silently swallowed
# =============================================================================


class TestLLMError:
    """AC4: LLM errors (401, 500, timeout, JSON parse) are returned as llm_error, not swallowed."""

    def test_http_error_returns_llm_error(self, monkeypatch):
        """401/500 from OpenRouter → results have llm_error populated, llm_verified=False."""
        def _raise(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr("urllib.request.urlopen", _raise)

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["f1", "f2"],
            source_candidates=[{"url": "u", "text": "t"}],
        )
        assert len(results) == 2
        for r in results:
            assert r["llm_verified"] is False
            assert r.get("llm_error"), f"missing llm_error: {r}"
            assert "401" in r["llm_error"] or "Unauthorized" in r["llm_error"]

    def test_timeout_returns_llm_error(self, monkeypatch):
        """TimeoutError → llm_error populated."""
        def _raise(req, timeout):
            raise TimeoutError("read timed out")

        monkeypatch.setattr("urllib.request.urlopen", _raise)

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["f1"],
            source_candidates=[{"url": "u", "text": "t"}],
        )
        assert results[0].get("llm_error"), f"missing llm_error on timeout: {results[0]}"
        assert "timeout" in results[0]["llm_error"].lower() or "Timeout" in results[0]["llm_error"]

    def test_json_parse_error_returns_llm_error(self, monkeypatch):
        """LLM returns non-JSON → llm_error, no crash."""
        def _bad_json(req, timeout):
            return _FakeResponse(b"not json at all, sorry")

        monkeypatch.setattr("urllib.request.urlopen", _bad_json)

        v = LLMVerifier()
        results = v.verify_facts_batch(
            facts=["f1"],
            source_candidates=[{"url": "u", "text": "t"}],
        )
        assert results[0].get("llm_error"), f"missing llm_error on bad JSON: {results[0]}"


# =============================================================================
# AC5 — verify_fact removed from public API (or rewritten)
# =============================================================================


class TestVerifyFactRemoval:
    """AC5: verify_fact (single-call, semantically broken per DR §14) is removed."""

    def test_verify_fact_method_removed(self):
        """LLMVerifier should NOT have a verify_fact method (DR §14: 'удалить')."""
        assert not hasattr(LLMVerifier, "verify_fact"), \
            "verify_fact should be removed from LLMVerifier (DR §14)"

    def test_new_verify_claim_against_evidence_exists(self, monkeypatch):
        """DR §14 alternative: new method verify_claim_against_evidence(claim, evidence_blocks)."""
        fake_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "results": [
                            {"index": 1, "verdict": "SUPPORTS", "reasoning": "matches", "source_urls": ["https://b.com"]}
                        ]
                    })
                }
            }]
        }
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout: _FakeResponse(json.dumps(fake_response).encode("utf-8")),
        )

        v = LLMVerifier()
        # new API: claim + list of evidence blocks
        result = v.verify_claim_against_evidence(
            claim="123 дрона",
            evidence_blocks=[{"url": "https://b.com", "text": "сбито 123 дрона"}],
        )
        assert result["verdict"] == "SUPPORTS"
        assert result["llm_verified"] is True


# =============================================================================
# Helpers
# =============================================================================


class _FakeResponse:
    """Minimal context-manager response for urlopen mocking."""
    def __init__(self, body: bytes):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
