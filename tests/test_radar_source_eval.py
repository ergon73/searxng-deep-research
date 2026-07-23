"""Evaluate source-native Radar discovery against the frozen release set."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from radar_source_eval import evaluate_huggingface_candidates
from radar_sources import build_huggingface_candidates, parse_huggingface_model

ROOT = Path(__file__).resolve().parents[1]
SINCE = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
UNTIL = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _signal(
    repo_id: str,
    *,
    created: str,
    modified: str,
    base_model: str | None = None,
):
    tags = ["text-generation"]
    card_data = None
    if base_model:
        tags.append(f"base_model:quantized:{base_model}")
        card_data = {
            "base_model": base_model,
            "base_model_relation": "quantized",
        }
    return parse_huggingface_model(
        {
            "id": repo_id,
            "createdAt": created,
            "lastModified": modified,
            "pipeline_tag": "text-generation",
            "library_name": "transformers",
            "tags": tags,
            "cardData": card_data,
            "downloads": 0,
            "likes": 0,
            "gated": False,
        }
    )


def test_huggingface_candidate_recall_has_explicit_denominators():
    dataset = json.loads((ROOT / "data" / "radar_eval_set.json").read_text(encoding="utf-8"))
    raw_signals = [
        _signal(
            "mlx-community/Laguna-S-2.1-MLX",
            created="2026-07-22T12:00:00Z",
            modified="2026-07-22T12:00:00Z",
            base_model="poolside/Laguna-S-2.1",
        ),
        _signal(
            "bartowski/Laguna-S-2.1-GGUF",
            created="2026-07-22T13:00:00Z",
            modified="2026-07-22T13:00:00Z",
            base_model="poolside/Laguna-S-2.1",
        ),
        _signal(
            "fdtn-ai/antares-350m",
            created="2026-07-21T14:00:00Z",
            modified="2026-07-21T14:00:00Z",
        ),
        _signal(
            "interfaze-ai/diffusion-gemma-asr-small",
            created="2026-06-26T10:00:00Z",
            modified="2026-07-22T12:00:00Z",
        ),
    ]
    candidates = build_huggingface_candidates(
        [signal for signal in raw_signals if signal],
        since=SINCE,
        until=UNTIL,
    )

    report = evaluate_huggingface_candidates(dataset, candidates)
    aggregate = report["aggregate"]

    assert aggregate["eligible_fresh_cases"] == 2
    assert aggregate["eligible_fresh_cases_recalled"] == 2
    assert aggregate["eligible_fresh_case_recall"] == 1.0
    assert aggregate["eligible_primary_source_recall"] == 1.0
    assert aggregate["hard_negative_cases"] == 6
    assert aggregate["hard_negative_cases_with_hub_signal"] == 1
    assert aggregate["hard_negative_update_only_signals"] == 1

    by_case = {result["case_id"]: result for result in report["results"]}
    assert by_case["laguna-s-2-1"]["matched_families"] == ["poolside/Laguna-S-2.1"]
    assert by_case["antares-security-slm"]["candidate_recalled"] is True
    assert by_case["diffusion-gemma-asr-update"]["state_hints"] == ["update_only"]
