"""Installed command-line interface for searxng-deep-research."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

TOOL_NAME = "searxng-research"
TOOL_VERSION = "0.9.0b1"
SCHEMA_VERSION = 1


def json_envelope(
    *,
    command: str,
    status: str,
    data: Any,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the stable top-level machine contract shared by all commands."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "command": command,
        "status": status,
        "data": data,
        "errors": list(errors or []),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    research = commands.add_parser("research", help="Run the evidence-first research pipeline.")
    research.add_argument("query", nargs="+")
    research.add_argument("--approved-plan", action="store_true")
    research.add_argument("--top-n", type=int, default=5)
    research.add_argument("--max-iterations", type=int, default=1)
    research.add_argument("--use-llm", action="store_true")

    radar = commands.add_parser("radar", help="LLM Release Radar commands.")
    radar_commands = radar.add_subparsers(dest="radar_command", required=True)
    discover = radar_commands.add_parser(
        "discover",
        help="Discover unconfirmed candidates from source-native channels.",
    )
    discover.add_argument("--since-hours", type=int, default=72)
    discover.add_argument("--limit-per-channel", type=int, default=100)
    discover.add_argument("--max-pages-per-channel", type=int, default=20)
    discover.add_argument("--timeout", type=float, default=15.0)
    discover.add_argument("--top", type=int, default=50)
    discover.add_argument("--include-signals", action="store_true")
    return parser


def _validate_research_args(args: argparse.Namespace) -> None:
    if not 1 <= args.top_n <= 20:
        raise ValueError("--top-n must be between 1 and 20")
    if not 1 <= args.max_iterations <= 5:
        raise ValueError("--max-iterations must be between 1 and 5")


def _run_research(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    _validate_research_args(args)
    from research_runner import run_research

    result = run_research(
        " ".join(args.query),
        approved_plan=args.approved_plan,
        top_n=args.top_n,
        max_iterations=args.max_iterations,
        use_llm=args.use_llm,
    )
    exit_code = 0 if result.status == "done" else 3 if result.status == "needs_confirmation" else 2
    errors = [{"kind": "pipeline", "message": result.error}] if result.error else []
    return (
        json_envelope(
            command="research",
            status=result.status,
            data=result.to_dict(),
            errors=errors,
        ),
        exit_code,
    )


def _validate_radar_args(args: argparse.Namespace) -> None:
    if not 1 <= args.since_hours <= 168:
        raise ValueError("--since-hours must be between 1 and 168")
    if not 1 <= args.limit_per_channel <= 500:
        raise ValueError("--limit-per-channel must be between 1 and 500")
    if not 1 <= args.max_pages_per_channel <= 50:
        raise ValueError("--max-pages-per-channel must be between 1 and 50")
    if not 0 < args.timeout <= 60:
        raise ValueError("--timeout must be greater than 0 and at most 60")
    if not 1 <= args.top <= 500:
        raise ValueError("--top must be between 1 and 500")


def _run_radar_discover(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    _validate_radar_args(args)
    from radar_sources import discover_huggingface, format_discovery_report

    checked_at = datetime.now(UTC)
    report = discover_huggingface(
        since=checked_at - timedelta(hours=args.since_hours),
        checked_at=checked_at,
        limit_per_channel=args.limit_per_channel,
        max_pages_per_channel=args.max_pages_per_channel,
        timeout=args.timeout,
    )
    errors = [{"kind": "source_channel", **error} for error in report.errors]
    all_failed = bool(errors) and all(channel["records"] == 0 for channel in report.channels)
    return (
        json_envelope(
            command="radar.discover",
            status="error" if all_failed else "degraded" if errors else "ok",
            data=format_discovery_report(
                report,
                top=args.top,
                include_signals=args.include_signals,
            ),
            errors=errors,
        ),
        2 if all_failed else 0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "research":
            payload, exit_code = _run_research(args)
        elif args.command == "radar" and args.radar_command == "discover":
            payload, exit_code = _run_radar_discover(args)
        else:
            parser.error("unsupported command")
    except Exception as exc:
        payload = json_envelope(
            command=(f"radar.{args.radar_command}" if args.command == "radar" else str(args.command)),
            status="error",
            data=None,
            errors=[
                {
                    "kind": "client",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        )
        exit_code = 2

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
