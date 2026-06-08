"""
Tests for src/models.py — typed state skeleton (Phase 1, v0.8.0).

No network. No LLM. Pure stdlib dataclass roundtrip + construction.
"""
from __future__ import annotations

import json

import pytest

from evidence import EvidenceWindow
from models import Claim, ResearchState, SearchTask


# ---------------------------------------------------------------- SearchTask


class TestSearchTask:
    def test_minimal_construction(self):
        t = SearchTask(query="Falcon 9")
        assert t.query == "Falcon 9"
        assert t.route == "general"               # default
        assert t.language == "auto"               # default
        assert t.engines is None
        assert t.categories is None
        assert t.time_range is None
        assert t.priority == 0
        assert t.rationale == ""

    def test_full_construction(self):
        t = SearchTask(
            query="БПЛА Москва сегодня",
            route="news",
            language="ru",
            engines="duckduckgo,bing",
            categories="news",
            time_range="day",
            priority=80,
            rationale="main adapted query",
        )
        assert t.query == "БПЛА Москва сегодня"
        assert t.route == "news"
        assert t.priority == 80

    def test_frozen_immutability(self):
        t = SearchTask(query="x")
        with pytest.raises((AttributeError, Exception)):
            t.priority = 99  # type: ignore[misc]

    def test_to_dict_roundtrip(self):
        t = SearchTask(
            query="q",
            route="technical",
            time_range="year",
            priority=42,
            rationale="r",
        )
        d = t.to_dict()
        # JSON-friendly: no nested objects, no callable defaults
        json.dumps(d)  # must not raise
        assert d == {
            "query": "q",
            "route": "technical",
            "language": "auto",
            "engines": None,
            "categories": None,
            "time_range": "year",
            "priority": 42,
            "rationale": "r",
        }


# ----------------------------------------------------------------------- Claim


class TestClaim:
    def test_minimal_construction(self):
        c = Claim(text="5 июня 2026")
        assert c.text == "5 июня 2026"
        assert c.subject is None
        assert c.predicate is None
        assert c.value is None
        assert c.unit is None
        assert c.date is None
        assert c.location is None
        assert c.polarity == "unknown"

    def test_numeric_claim(self):
        c = Claim(
            text="123 дрона сбито",
            subject="дроны",
            predicate="количество",
            value="123",
            unit="штук",
            polarity="positive",
        )
        assert c.value == "123"
        assert c.polarity == "positive"

    def test_frozen_immutability(self):
        c = Claim(text="x")
        with pytest.raises((AttributeError, Exception)):
            c.value = "9"  # type: ignore[misc]

    def test_to_dict(self):
        c = Claim(text="t", value="v", unit="u")
        d = c.to_dict()
        json.dumps(d)
        assert d["text"] == "t"
        assert d["value"] == "v"
        assert d["unit"] == "u"
        assert d["polarity"] == "unknown"  # default preserved


# ------------------------------------------------------------------ ResearchState


class TestResearchState:
    def test_minimal_construction(self):
        s = ResearchState(original_query="Falcon 9")
        assert s.original_query == "Falcon 9"
        assert s.adapted is None
        assert s.search_tasks == []
        assert s.search_hits == []
        assert s.documents == []
        assert s.claims == []
        assert s.evidence == []
        assert s.verdicts == []
        assert s.gaps == []
        assert s.iterations == 0

    def test_mutable_container(self):
        s = ResearchState(original_query="q")
        s.search_tasks.append(SearchTask(query="t1"))
        s.claims.append(Claim(text="c1"))
        s.iterations = 1
        assert len(s.search_tasks) == 1
        assert len(s.claims) == 1
        assert s.iterations == 1

    def test_independent_instances(self):
        """Default factories must not share state between instances."""
        s1 = ResearchState(original_query="a")
        s2 = ResearchState(original_query="b")
        s1.claims.append(Claim(text="c"))
        assert s2.claims == []  # not affected by s1 mutation

    def test_to_dict_json_serialisable(self):
        s = ResearchState(
            original_query="БПЛА Москва",
            adapted={"main_query": "БПЛА Москва", "needs_confirmation": False},
            search_tasks=[
                SearchTask(query="БПЛА Москва", route="news", time_range="day", priority=100),
                SearchTask(query="БПЛА опровержение", priority=40),
            ],
            claims=[
                Claim(text="22 БПЛА", value="22", unit="штук"),
            ],
            evidence=[
                EvidenceWindow(
                    text="...сбито 22 БПЛА за ночь...",
                    offset_start=120,
                    offset_end=160,
                    match_terms=["22", "БПЛА"],
                    match_score=0.85,
                ),
            ],
            gaps=["too_few_sources"],
            iterations=1,
        )
        d = s.to_dict()
        # Must be JSON-encodable (no dataclass objects leak)
        encoded = json.dumps(d, ensure_ascii=False)
        decoded = json.loads(encoded)
        # Spot-check
        assert decoded["original_query"] == "БПЛА Москва"
        assert len(decoded["search_tasks"]) == 2
        assert decoded["search_tasks"][0]["priority"] == 100
        assert decoded["claims"][0]["value"] == "22"
        assert decoded["evidence"][0]["offset_start"] == 120
        assert decoded["evidence"][0]["match_terms"] == ["22", "БПЛА"]
        assert decoded["gaps"] == ["too_few_sources"]
        assert decoded["iterations"] == 1

    def test_to_dict_with_empty_state(self):
        s = ResearchState(original_query="q")
        d = s.to_dict()
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded == {
            "original_query": "q",
            "adapted": None,
            "search_tasks": [],
            "search_hits": [],
            "documents": [],
            "claims": [],
            "evidence": [],
            "verdicts": [],
            "gaps": [],
            "iterations": 0,
        }

    def test_evidence_window_reuse(self):
        """We deliberately reuse EvidenceWindow from evidence.py (not redefine).

        Sanity check: appending an EvidenceWindow to state.evidence works,
        the value is preserved, and `to_dict()` round-trips correctly.
        """
        s = ResearchState(original_query="q")
        w = EvidenceWindow(
            text="x", offset_start=0, offset_end=1, match_terms=["a"], match_score=0.5
        )
        s.evidence.append(w)
        assert s.evidence[0] is w
        # Round-trip via to_dict (verifies EvidenceWindow.to_dict() compat)
        d = s.to_dict()
        assert d["evidence"][0]["text"] == "x"
        assert d["evidence"][0]["match_terms"] == ["a"]
        assert d["evidence"][0]["match_score"] == 0.5

    def test_search_task_via_state(self):
        """Realistic end-to-end shape that a runner would build (Phase 3+)."""
        s = ResearchState(original_query="БПЛА Москва сегодня")
        s.adapted = {
            "main_query": "БПЛА Москва",
            "needs_confirmation": False,
            "dropped_terms": ["сегодня"],   # time hint is encoded in task, not query
            "adaptation_confidence": 0.85,
        }
        s.search_tasks.append(
            SearchTask(
                query="БПЛА Москва",
                route="news",
                time_range="day",
                priority=100,
                rationale="main adapted query",
            )
        )
        s.search_tasks.append(
            SearchTask(
                query="БПЛА Москва опровержение",
                route="news",
                time_range="week",
                priority=40,
                rationale="falsification for news route",
            )
        )
        s.iterations = 1
        # Two tasks: main (high priority) + falsification (low)
        assert s.search_tasks[0].priority > s.search_tasks[1].priority
        assert s.search_tasks[1].rationale.startswith("falsification")
