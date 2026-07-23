"""Offline tests for source-native LLM Release Radar discovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from radar_sources import (
    build_huggingface_candidates,
    discover_huggingface,
    format_discovery_report,
    parse_huggingface_model,
)

SINCE = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


def _model(
    repo_id: str,
    *,
    created: str,
    modified: str,
    tags: list[str] | None = None,
    card_data: dict | None = None,
    downloads: int = 0,
    likes: int = 0,
) -> dict:
    return {
        "id": repo_id,
        "createdAt": created,
        "lastModified": modified,
        "pipeline_tag": "text-generation",
        "library_name": "transformers",
        "tags": tags or ["text-generation"],
        "cardData": card_data,
        "downloads": downloads,
        "likes": likes,
        "gated": False,
    }


def test_parse_derivative_resolves_base_model():
    signal = parse_huggingface_model(
        _model(
            "mlx-community/Laguna-S-2.1-oQ3e-fast",
            created="2026-07-23T21:19:41.000Z",
            modified="2026-07-23T21:48:11.000Z",
            tags=[
                "text-generation",
                "base_model:poolside/Laguna-S-2.1",
                "base_model:quantized:poolside/Laguna-S-2.1",
            ],
            card_data={
                "base_model": "poolside/Laguna-S-2.1",
                "base_model_relation": "quantized",
            },
        )
    )

    assert signal is not None
    assert signal.base_model == "poolside/Laguna-S-2.1"
    assert signal.relation == "quantized"
    assert signal.is_derivative is True
    assert signal.family_id == "poolside/Laguna-S-2.1"


def test_build_candidates_separates_release_signals_from_update_only():
    payloads = [
        _model(
            "fdtn-ai/antares-350m",
            created="2026-07-21T14:00:00.000Z",
            modified="2026-07-22T10:00:00.000Z",
            downloads=50,
            likes=12,
        ),
        _model(
            "mlx-community/Laguna-S-2.1-oQ3e-fast",
            created="2026-07-23T21:19:41.000Z",
            modified="2026-07-23T21:48:11.000Z",
            tags=["text-generation", "base_model:poolside/Laguna-S-2.1"],
            card_data={
                "base_model": "poolside/Laguna-S-2.1",
                "base_model_relation": "quantized",
            },
        ),
        _model(
            "bartowski/Laguna-S-2.1-GGUF",
            created="2026-07-22T12:00:00.000Z",
            modified="2026-07-22T13:00:00.000Z",
            tags=["text-generation", "base_model:quantized:poolside/Laguna-S-2.1"],
        ),
        _model(
            "interfaze-ai/diffusion-gemma-asr-small",
            created="2026-06-26T10:00:00.000Z",
            modified="2026-07-22T12:00:00.000Z",
        ),
    ]
    signals = [signal for payload in payloads if (signal := parse_huggingface_model(payload))]

    candidates = build_huggingface_candidates(signals, since=SINCE)
    by_family = {candidate.family_id: candidate for candidate in candidates}

    antares = by_family["fdtn-ai/antares-350m"]
    assert antares.state_hint == "new_root_repository"
    assert "root_repo_created_in_window" in antares.reasons

    laguna = by_family["poolside/Laguna-S-2.1"]
    assert laguna.state_hint == "derivative_burst"
    assert laguna.derivative_repositories == 2
    assert "multiple_derivatives_created_in_window" in laguna.reasons

    diffusion = by_family["interfaze-ai/diffusion-gemma-asr-small"]
    assert diffusion.state_hint == "update_only"
    assert diffusion.new_repositories == 0
    assert diffusion.updated_repositories == 1
    assert "modified_without_new_root" in diffusion.reasons


def test_candidate_output_never_claims_confirmed_release():
    signal = parse_huggingface_model(
        _model(
            "unknown-author/NewModel-7B",
            created="2026-07-23T08:00:00.000Z",
            modified="2026-07-23T08:00:00.000Z",
        )
    )
    assert signal is not None

    candidate = build_huggingface_candidates([signal], since=SINCE)[0]
    payload = candidate.to_dict()

    assert payload["state_hint"] == "new_root_repository"
    assert "confirmed" not in payload["state_hint"]
    assert payload["requires_primary_verification"] is True


def test_invalid_or_non_model_payload_is_ignored():
    assert parse_huggingface_model({}) is None
    assert (
        parse_huggingface_model(
            {
                "id": "example/dataset-like",
                "createdAt": "not-a-date",
                "lastModified": "2026-07-23T08:00:00Z",
            }
        )
        is None
    )


class _FakeApiResponse:
    def __init__(self, payload: list[dict], *, next_link: str | None = None):
        self.payload = json.dumps(payload).encode()
        self.headers = {
            "Link": f'<{next_link}>; rel="next"'
        } if next_link else {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_discovery_merges_channels_and_reports_update_only():
    antares = _model(
        "fdtn-ai/antares-350m",
        created="2026-07-21T14:00:00.000Z",
        modified="2026-07-22T10:00:00.000Z",
    )
    diffusion = _model(
        "interfaze-ai/diffusion-gemma-asr-small",
        created="2026-06-26T10:00:00.000Z",
        modified="2026-07-22T12:00:00.000Z",
    )
    requested_urls: list[str] = []

    def opener(request, **kwargs):
        requested_urls.append(request.full_url)
        if "sort=createdAt" in request.full_url:
            return _FakeApiResponse([antares])
        return _FakeApiResponse([diffusion, antares])

    report = discover_huggingface(
        since=SINCE,
        checked_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
        pipeline_tags=("text-generation",),
        sorts=("createdAt", "lastModified"),
        limit_per_channel=10,
        opener=opener,
    )
    payload = report.to_dict()

    assert payload["fetched_records"] == 3
    assert payload["unique_signals"] == 2
    assert payload["errors"] == []
    assert payload["creation_window_complete"] is True
    assert payload["modification_window_complete"] is True
    assert len(payload["channels"]) == 2
    assert all(url.startswith("https://huggingface.co/api/models?") for url in requested_urls)
    by_family = {item["family_id"]: item for item in payload["candidates"]}
    assert by_family["fdtn-ai/antares-350m"]["state_hint"] == "new_root_repository"
    assert (
        by_family["interfaze-ai/diffusion-gemma-asr-small"]["state_hint"]
        == "update_only"
    )

    bounded = format_discovery_report(report, top=1)
    assert bounded["candidate_count"] == 2
    assert len(bounded["candidates"]) == 1
    assert bounded["output_truncated"] is True


def test_discovery_isolates_failed_channel():
    def opener(request, **kwargs):
        if "sort=createdAt" in request.full_url:
            raise TimeoutError("timed out")
        return _FakeApiResponse([])

    report = discover_huggingface(
        since=SINCE,
        checked_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
        pipeline_tags=("text-generation",),
        sorts=("createdAt", "lastModified"),
        limit_per_channel=10,
        opener=opener,
    )

    assert len(report.errors) == 1
    assert report.errors[0]["channel"] == "text-generation:createdAt"
    assert report.errors[0]["error"] == "TimeoutError: timed out"
    assert report.unique_signals == 0


def test_discovery_paginates_until_window_boundary():
    recent = _model(
        "example/recent",
        created="2026-07-22T10:00:00.000Z",
        modified="2026-07-22T10:00:00.000Z",
    )
    old = _model(
        "example/old",
        created="2026-07-20T10:00:00.000Z",
        modified="2026-07-20T10:00:00.000Z",
    )
    calls = 0

    def opener(request, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FakeApiResponse(
                [recent],
                next_link=(
                    "https://huggingface.co/api/models?"
                    "pipeline_tag=text-generation&sort=createdAt&cursor=opaque"
                ),
            )
        return _FakeApiResponse([old])

    report = discover_huggingface(
        since=SINCE,
        checked_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
        pipeline_tags=("text-generation",),
        sorts=("createdAt",),
        limit_per_channel=1,
        max_pages_per_channel=5,
        opener=opener,
    )

    assert calls == 2
    assert report.channels[0]["pages"] == 2
    assert report.channels[0]["truncated"] is False
    assert report.unique_signals == 1
    assert report.to_dict()["creation_window_complete"] is True


def test_discovery_rejects_cross_host_pagination():
    recent = _model(
        "example/recent",
        created="2026-07-22T10:00:00.000Z",
        modified="2026-07-22T10:00:00.000Z",
    )

    report = discover_huggingface(
        since=SINCE,
        checked_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
        pipeline_tags=("text-generation",),
        sorts=("createdAt",),
        limit_per_channel=1,
        opener=lambda *args, **kwargs: _FakeApiResponse(
            [recent],
            next_link="https://attacker.example/steal",
        ),
    )

    assert report.channels[0]["pages"] == 1
    assert report.channels[0]["truncated"] is True
    assert report.errors[0]["error"] == "unsafe pagination URL"
    assert report.to_dict()["creation_window_complete"] is False
