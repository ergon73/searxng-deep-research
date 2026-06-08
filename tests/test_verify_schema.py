"""
verify_sources() schema tests — SUPPORTS/REFUTES/INSUFFICIENT/CONFLICTING.
"""
from hermes_deepresearch import verify_sources


def make_source(url: str, text: str) -> dict:
    return {"url": url, "title": "Test", "text": text, "length": len(text), "error": None}


class TestVerifySources:
    """Tests for 4-level verification + SUPPORTS/REFUTES/INSUFFICIENT/CONFLICTING verdicts."""

    def test_empty_top1_returns_empty(self):
        result = verify_sources(None, [], "query")
        assert result["verified_facts"] == 0
        assert result["total_facts"] == 0
        assert result["verification_rate"] == 0.0
        assert result["verification_details"] == []

    def test_sufficient_match_suuports(self):
        top1 = make_source("https://a.com", "5 июня 2026 сбито 123 дрона")
        other = [make_source("https://b.com", "Подтверждаем: 5 июня 2026 сбито 123 дрона")]
        result = verify_sources(top1, other, "БПЛА", use_llm=False)
        # Хотя бы один fact должен быть SUPPORTS
        assert any(d["verdict"] == "SUPPORTS" for d in result["verification_details"])

    def test_refutation_in_other_source(self):
        top1 = make_source("https://a.com", "5 июня сбито 123 дрона")
        # Другой источник говорит "не сбито" с тем же фактом
        other = [make_source("https://b.com", "5 июня НЕ сбито ни одного дрона из 123")]
        result = verify_sources(top1, other, "БПЛА", use_llm=False)
        # Должен быть хотя бы один CONFLICTING или REFUTES fact
        verdicts = [d["verdict"] for d in result["verification_details"]]
        # Хотя бы один fact должен показывать refutation
        assert any(v in ("REFUTES", "CONFLICTING") for v in verdicts), \
            f"Expected REFUTES or CONFLICTING, got: {verdicts}"

    def test_no_support_in_other_sources(self):
        top1 = make_source("https://a.com", "Уникальный факт 9876 произошел вчера")
        other = [make_source("https://b.com", "Ничего про 9876 не слышали")]
        result = verify_sources(top1, other, "запрос", use_llm=False)
        # 9876 — должно быть INSUFFICIENT
        details_9876 = [d for d in result["verification_details"] if "9876" in d["fact"]]
        if details_9876:
            assert details_9876[0]["verdict"] == "INSUFFICIENT"

    def test_no_llm_when_disabled(self):
        top1 = make_source("https://a.com", "5 июня 2026 сбито 123 дрона")
        other = [make_source("https://b.com", "5 июня сбито 123 дрона")]
        result = verify_sources(top1, other, "БПЛА", use_llm=False)
        assert result["llm_enhanced"] is False
        assert result["llm_latency"] == 0.0
        assert result["llm_verified_count"] == 0
        assert result["llm_error"] is None  # v0.8.2 (Phase 4) — always present

    def test_llm_error_field_present(self):
        """v0.8.2 (Phase 4): llm_error field always in return dict (None on success)."""
        result = verify_sources(None, [], "q")
        assert "llm_error" in result
        assert result["llm_error"] is None

    def test_llm_error_propagates_from_verifier(self, monkeypatch):
        """If LLMVerifier raises, llm_error must be populated, not None."""
        from hermes_deepresearch import verify_sources

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated OpenRouter 401")

        # LLM используется только если rate < threshold и есть unverified
        top1 = make_source("https://a.com", "5 июня 2026 сбито 123 дрона")
        # Other source не поддерживает fact → INSUFFICIENT → rate < threshold
        other = [make_source("https://b.com", "Какая-то другая новость")]

        monkeypatch.setattr("hermes_deepresearch.LLMVerifier", _raise)
        result = verify_sources(top1, other, "БПЛА", use_llm=True)
        # Если LLM кинул exception, llm_error должен содержать trace.
        # llm_enhanced при этом остаётся False — это сигнал, что усиление не сработало.
        assert result.get("llm_error") is not None, f"llm_error not propagated: {result}"
        assert "simulated" in result["llm_error"], f"unexpected error: {result['llm_error']}"
