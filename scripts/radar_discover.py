#!/usr/bin/env python3
"""Discover LLM release candidates from bounded source-native channels."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from radar_sources import discover_huggingface, format_discovery_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-hours",
        type=int,
        default=72,
        help="Discovery buffer in hours; confirmation applies the exact report window later.",
    )
    parser.add_argument("--limit-per-channel", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()

    if not 1 <= args.since_hours <= 168:
        parser.error("--since-hours must be between 1 and 168")
    if not 1 <= args.limit_per_channel <= 500:
        parser.error("--limit-per-channel must be between 1 and 500")
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be greater than 0 and at most 60")
    if not 1 <= args.top <= 500:
        parser.error("--top must be between 1 and 500")

    checked_at = datetime.now(UTC)
    report = discover_huggingface(
        since=checked_at - timedelta(hours=args.since_hours),
        checked_at=checked_at,
        limit_per_channel=args.limit_per_channel,
        timeout=args.timeout,
    )
    print(
        json.dumps(
            format_discovery_report(report, top=args.top),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 2 if report.errors and all(channel["records"] == 0 for channel in report.channels) else 0


if __name__ == "__main__":
    raise SystemExit(main())
