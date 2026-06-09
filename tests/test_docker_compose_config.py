"""
Tests for config/docker-compose.yml — env_file hardening (v0.8.1.1).

ChatGPT P1 (v0.8.0 review): .env_proxy was required in env_file, so
clean installs without per-engine proxies would fail. Fix: mark it
`required: false` (Compose >= 2.24). We run v5.1.4.

These tests parse the compose file as YAML and assert:
1. The file is valid YAML.
2. The searxng service has env_file with `path: ./.env_proxy`.
3. The searxng service env_file entry has `required: false`.
4. .env_llm is NOT in env_file (Phase C invariant: don't leak LLM key).
5. SEARXNG_SECRET is still in environment with ${VAR:?msg} (fail-fast).

We don't shell out to `docker compose config` (requires Docker daemon).
Pure stdlib YAML parsing — fast, hermetic, no network.
"""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

COMPOSE_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "docker-compose.yml"


def _load_compose() -> dict[str, Any]:
    with open(COMPOSE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_compose_file_is_valid_yaml():
    """docker-compose.yml must parse as YAML without errors."""
    data = _load_compose()
    assert "services" in data
    assert "searxng" in data["services"]


def test_searxng_env_file_uses_object_form():
    """env_file must be a list of dicts (object form), not bare strings.

    The object form `{path: ..., required: ...}` is required for the
    `required: false` syntax. Bare strings (`- ./.env_proxy`) always
    behave as required.
    """
    data = _load_compose()
    env_file = data["services"]["searxng"].get("env_file", [])
    assert isinstance(env_file, list), f"env_file must be a list, got {type(env_file)}"
    assert len(env_file) >= 1, "env_file must have at least one entry"
    for entry in env_file:
        assert isinstance(entry, dict), (
            f"env_file entries must be dicts (object form), got {type(entry)}: {entry!r}"
        )


def test_env_proxy_entry_is_optional():
    """The ./.env_proxy entry must have `required: false`.

    Without this, clean installs without per-engine proxies fail with:
    `env file ... not found: ...` and the stack won't start.
    """
    data = _load_compose()
    env_file = data["services"]["searxng"]["env_file"]
    proxy_entries = [e for e in env_file if e.get("path", "").endswith(".env_proxy")]
    assert len(proxy_entries) == 1, (
        f"expected exactly one .env_proxy entry, got {len(proxy_entries)}: {env_file!r}"
    )
    assert proxy_entries[0].get("required") is False, (
        f".env_proxy must be required: false (optional), got: {proxy_entries[0]!r}"
    )


def test_env_llm_not_in_env_file():
    """Phase C invariant: .env_llm must NOT be passed to SearXNG container.

    OpenRouter LLM_API_KEY is consumed by src/llm_verifier.py at Python
    runtime, not by the SearXNG image. Injecting it into the container
    leaks the secret to a process that doesn't need it.
    """
    data = _load_compose()
    env_file = data["services"]["searxng"].get("env_file", [])
    llm_entries = [e for e in env_file if ".env_llm" in str(e)]
    assert not llm_entries, (
        f".env_llm must not be in searxng env_file (leaks LLM key): {llm_entries!r}"
    )


def test_searxng_secret_environment_still_required():
    """SEARXNG_SECRET must remain ${VAR:?msg} (fail-fast on missing).

    This is the Phase C invariant: the container must never boot with
    a placeholder or empty secret. Compose interpolation should error
    if SEARXNG_SECRET is not set in config/.env.
    """
    data = _load_compose()
    env = data["services"]["searxng"].get("environment", [])
    secret_entries = [e for e in env if "SEARXNG_SECRET" in str(e)]
    assert len(secret_entries) == 1, (
        f"expected exactly one SEARXNG_SECRET entry, got {len(secret_entries)}"
    )
    secret_value = str(secret_entries[0])
    assert "${SEARXNG_SECRET:?msg" in secret_value or "${SEARXNG_SECRET:?" in secret_value, (
        f"SEARXNG_SECRET must use fail-fast :?msg interpolation, got: {secret_value!r}"
    )
