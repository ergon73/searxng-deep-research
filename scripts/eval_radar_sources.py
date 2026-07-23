#!/usr/bin/env python3
"""Evaluate source-native Radar candidate recall on the frozen dataset."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from radar_source_eval import evaluate_huggingface_candidates
from radar_sources import discover_huggingface

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SET = ROOT / "data" / "radar_eval_set.json"


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=DEFAULT_SET)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--no-network", action="store_true")
    mode.add_argument("--online", action="store_true")
    parser.add_argument("--limit-per-channel", type=int, default=100)
    parser.add_argument("--max-pages-per-channel", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    dataset = json.loads(args.set.read_text(encoding="utf-8"))
    if args.no_network:
        report = evaluate_huggingface_candidates(dataset, [])
        report["mode"] = "offline_contract"
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    as_of = _timestamp(dataset["window"]["as_of"])
    since = as_of - timedelta(hours=dataset["window"]["discovery_buffer_hours"])
    discovery = discover_huggingface(
        since=since,
        checked_at=as_of,
        limit_per_channel=args.limit_per_channel,
        max_pages_per_channel=args.max_pages_per_channel,
        timeout=args.timeout,
    )
    evaluation = evaluate_huggingface_candidates(dataset, list(discovery.candidates))
    discovery_payload = discovery.to_dict()
    evaluation["mode"] = "online_current_api_historical_cutoff"
    evaluation["discovery"] = {
        "checked_at": discovery_payload["checked_at"],
        "since": discovery_payload["since"],
        "fetched_records": discovery_payload["fetched_records"],
        "unique_signals": discovery_payload["unique_signals"],
        "candidate_count": len(discovery.candidates),
        "channels": discovery_payload["channels"],
        "errors": discovery_payload["errors"],
        "creation_window_complete": discovery_payload["creation_window_complete"],
        "modification_window_complete": discovery_payload["modification_window_complete"],
        "historical_replay_caveat": (
            "The live Hub API exposes current lastModified values, not an immutable "
            "historical snapshot; frozen correctness remains fixture-backed."
        ),
    }
    print(json.dumps(evaluation, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
