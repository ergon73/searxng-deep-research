"""Stable CLI contract tests (no network, no LLM)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import cli

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_installs_versioned_cli_and_modules():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["version"] == "0.9.0b1"
    assert config["project"]["scripts"]["searxng-research"] == "cli:main"
    modules = config["tool"]["setuptools"]["py-modules"]
    assert {"cli", "research_runner", "radar_sources", "radar_source_eval"} <= set(modules)


def test_parser_exposes_research_and_radar_discover():
    parser = cli.build_parser()

    research = parser.parse_args(["research", "new", "model", "--approved-plan"])
    assert research.command == "research"
    assert research.query == ["new", "model"]
    assert research.approved_plan is True

    radar = parser.parse_args(
        [
            "radar",
            "discover",
            "--since-hours",
            "48",
            "--top",
            "25",
        ]
    )
    assert radar.command == "radar"
    assert radar.radar_command == "discover"
    assert radar.since_hours == 48
    assert radar.top == 25


def test_json_envelope_is_versioned_and_machine_readable():
    payload = cli.json_envelope(
        command="radar.discover",
        status="ok",
        data={"candidate_count": 2},
    )

    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded == {
        "schema_version": 1,
        "tool": "searxng-research",
        "tool_version": "0.9.0b1",
        "command": "radar.discover",
        "status": "ok",
        "data": {"candidate_count": 2},
        "errors": [],
    }
