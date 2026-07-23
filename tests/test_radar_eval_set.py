"""Schema and temporal-consistency checks for the LLM Release Radar set."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

DATASET = Path(__file__).resolve().parent.parent / "data" / "radar_eval_set.json"

RELEASE_STATUSES = {"release", "limited_release"}
OTHER_STATUSES = {"preview", "not_release_in_window", "insufficient_evidence"}
SOURCE_KINDS = {
    "official_announcement",
    "official_model_page",
    "official_model_api",
    "official_repository_api",
    "paper",
}


def _load() -> dict:
    with DATASET.open(encoding="utf-8") as stream:
        return json.load(stream)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)


def test_radar_eval_set_exists_and_has_versioned_shape():
    assert DATASET.exists()
    data = _load()
    assert data["schema_version"] == 1
    assert data["vertical"] == "llm_release_radar"
    assert data["cases"]


def test_radar_case_ids_and_aliases_are_unique():
    cases = _load()["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))

    aliases = [alias.casefold() for case in cases for alias in case.get("aliases", [])]
    assert len(aliases) == len(set(aliases))


def test_radar_expected_statuses_and_freshness_are_temporally_consistent():
    data = _load()
    as_of = _dt(data["window"]["as_of"])
    fresh_start = as_of - timedelta(hours=data["window"]["fresh_hours"])
    allowed_statuses = RELEASE_STATUSES | OTHER_STATUSES

    for case in data["cases"]:
        expected = case["expected"]
        assert expected["status"] in allowed_statuses
        assert expected["freshness"] in {"fresh", "delayed", "outside_window"}

        release_date = expected.get("release_date")
        if expected["status"] in RELEASE_STATUSES:
            assert release_date, case["id"]
            day = datetime.fromisoformat(release_date).date()
            assert day <= as_of.date()
            if expected["freshness"] == "fresh":
                # Official posts often expose only a calendar date. Treat a
                # date touching the 48-hour boundary as fresh rather than
                # inventing an unsupported publication time.
                assert fresh_start.date() <= day
        elif release_date:
            assert datetime.fromisoformat(release_date).date() <= as_of.date()


def test_radar_has_both_positive_and_hard_negative_cases():
    cases = _load()["cases"]
    positives = [case for case in cases if case["expected"]["status"] in RELEASE_STATUSES]
    negatives = [case for case in cases if case["expected"]["status"] == "not_release_in_window"]
    assert len(positives) >= 4
    assert len(negatives) >= 4


def test_radar_primary_sources_are_https_and_typed():
    for case in _load()["cases"]:
        sources = case["primary_sources"]
        assert sources, case["id"]
        for source in sources:
            parsed = urlparse(source["url"])
            assert parsed.scheme == "https"
            assert parsed.hostname
            assert source["kind"] in SOURCE_KINDS
            assert source["supports"]
