"""
tests/test_compose_config.py — static checks for docker-compose.yml and settings.yml.

These tests run without Docker or network. They are the regression net for Phase 1
of DR-05062026(3): runtime/config blockers.

Lock-in tests, written BEFORE the patch. Initial state: most tests should FAIL
against the current code. The diff that turns them green lives in T2/T3 of the
Phase 1 plan (/opt/searxng/.hermes/plans/2026-06-05_172617-phase-1-runtime-config.md).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "config" / "docker-compose.yml"
SETTINGS = REPO_ROOT / "config" / "settings.yml"
ENV_LLM = REPO_ROOT / ".env_llm"
ENV_LLM_EXAMPLE = REPO_ROOT / "config" / ".env_llm.example"


# --- Docker Compose ---


class TestDockerCompose:
    @pytest.fixture
    def compose(self):
        assert COMPOSE.exists(), f"missing {COMPOSE}"
        return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    def test_searxng_service_defined(self, compose):
        assert "searxng" in compose["services"]

    def test_environment_is_list_of_strings(self, compose):
        env = compose["services"]["searxng"].get("environment", [])
        assert isinstance(env, list), f"environment must be a list, got {type(env)}"
        for item in env:
            assert isinstance(item, str), f"each env entry must be a string, got {type(item)}: {item!r}"

    def test_environment_no_joined_lines(self, compose):
        """Each env entry must be a single 'KEY=value' string. Catches the
        SEARXNG_SECRET=***      - SEARXNG_BIND_ADDRESS=0.0.0.0 splicing bug
        (DR-05062026(3) §P0.2)."""
        env = compose["services"]["searxng"].get("environment", [])
        bad = [e for e in env if " - " in e]
        assert not bad, "environment contains entries that look like two joined lines:\n" + "\n".join(
            repr(e) for e in bad
        )

    def test_environment_has_bind_address(self, compose):
        env = compose["services"]["searxng"].get("environment", [])
        keys = [e.split("=", 1)[0] for e in env if "=" in e]
        assert "SEARXNG_BIND_ADDRESS" in keys, (
            f"SEARXNG_BIND_ADDRESS missing from environment; got keys: {keys}"
        )

    def test_environment_has_searxng_secret(self, compose):
        """SEARXNG_SECRET must be set, either as an env entry that references
        an env_file var, or as a direct value. The placeholder value itself
        is enforced to not be the literal '***ME_GENERATE_…' by
        test_settings_yml_secret_key_not_placeholder."""
        env = compose["services"]["searxng"].get("environment", [])
        keys = [e.split("=", 1)[0] for e in env if "=" in e]
        assert "SEARXNG_SECRET" in keys, "SEARXNG_SECRET must be set (env or env_file)"

    def test_settings_volume_uses_env_path_or_absolute(self, compose):
        """The host-side settings.yml path must not silently depend on cwd.
        Acceptable forms:
          - absolute host path (starts with /)
          - environment variable expansion (${VAR} or $VAR)
        """
        volumes = compose["services"]["searxng"].get("volumes", [])
        settings_mounts = [v for v in volumes if "settings.yml" in v]
        assert settings_mounts, "no settings.yml volume mounted"
        mount = settings_mounts[0]
        host_part = mount.split(":")[0]
        is_absolute = host_part.startswith("/")
        is_env_var = host_part.startswith("${") or host_part.startswith("$")
        assert is_absolute or is_env_var, (
            f"settings.yml host path must be absolute or env-var expanded, got {host_part!r}. "
            "Relative path silently depends on cwd of `docker compose up` "
            "(DR-05062026(3) §P0.1)."
        )


# --- settings.yml ---


class TestSettingsYml:
    @pytest.fixture
    def settings(self):
        assert SETTINGS.exists(), f"missing {SETTINGS}"
        return yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))

    def test_secret_key_not_placeholder(self, settings):
        """server.secret_key must not be the literal '***ME_GENERATE_…'
        placeholder (DR-05062026(3) §P0.3). Empty string is allowed because
        T3 will wire SEARXNG_SECRET through env, and SearXNG falls back to
        the env override when this is empty."""
        key = settings.get("server", {}).get("secret_key", "")
        assert key is not None, "server.secret_key is None"
        assert "GENERATE_WITH" not in key, (
            f"server.secret_key is still the placeholder: {key!r}. "
            "Phase 1 Task 3 must wire SEARXNG_SECRET through env."
        )

    def test_valkey_url_present(self, settings):
        url = settings.get("valkey", {}).get("url", "")
        assert url.startswith("valkey://"), f"valkey.url must start with valkey://, got {url!r}"

    def test_search_formats_includes_json(self, settings):
        formats = settings.get("search", {}).get("formats", [])
        assert "json" in formats, f"search.formats must include 'json' for deep_research, got: {formats}"

    def test_brand_uses_current_searxng_schema(self, settings):
        """Legacy branding keys make current SearXNG reject settings.yml."""
        brand = settings.get("brand", {})
        removed_keys = {"new_name", "private_instance"} & set(brand)
        assert not removed_keys, (
            "settings.yml contains branding keys rejected by current SearXNG: "
            f"{sorted(removed_keys)}"
        )

    def test_engines_reuse_current_searxng_definitions(self, settings):
        """Curated engines should inherit module names and shortcuts upstream."""
        defaults = settings.get("use_default_settings")
        assert isinstance(defaults, dict), (
            "use_default_settings must use the mapping form with engines.keep_only"
        )
        keep_only = defaults.get("engines", {}).get("keep_only", [])
        assert keep_only, "use_default_settings.engines.keep_only must not be empty"

        overrides = settings.get("engines", [])
        assert {item["name"] for item in overrides} <= set(keep_only)
        forbidden_override_keys = {"engine", "shortcut"}
        offenders = [
            item["name"]
            for item in overrides
            if forbidden_override_keys & set(item)
        ]
        assert not offenders, (
            "engine module names and shortcuts must come from current SearXNG "
            f"defaults; stale overrides found for: {offenders}"
        )


# --- .env_llm ---
#
# v0.8.2-C1: the original TestEnvLlm hard-required a real `.env_llm` at
# REPO_ROOT with mode 0600 and a SEARXNG_SECRET. That made `pytest -q`
# fail in clean checkouts and forced CI to fabricate a dummy file. We
# now test the validation logic against a tmp_path fixture so the
# contract is "if you ship a .env_llm, it must look like this" — the
# file itself is no longer a test prerequisite.

VALID_ENV_LLM_BODY = (
    "LLM_API_KEY=dummy_for_test\n"
    "LLM_MODEL=meta-llama/llama-3.1-8b-instruct:free\n"
    "SEARXNG_SECRET=dummy_for_test_secret_value\n"
)


def _write_env_llm(path: Path, body: str = VALID_ENV_LLM_BODY, mode: int = 0o600) -> None:
    """Helper: write a .env_llm at `path` with the requested mode."""
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)


class TestEnvLlm:
    """Validation contract for .env_llm.

    These tests do NOT require a real .env_llm in the repo root. They
    create a synthetic one in tmp_path, then verify the validation
    helpers (mode 0600, SEARXNG_SECRET present) work correctly. The
    legacy TestEnvLlmLegacyCompatibility below optionally checks a
    real REPO_ROOT/.env_llm if one happens to exist (skipped otherwise).
    """

    def test_synthetic_env_llm_is_mode_600(self, tmp_path):
        """A correctly created .env_llm must be mode 0600."""
        p = tmp_path / ".env_llm"
        _write_env_llm(p)
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == 0o600, f".env_llm must be mode 0600, got {oct(mode)}"

    def test_synthetic_env_llm_has_searxng_secret(self, tmp_path):
        """A correctly created .env_llm must contain SEARXNG_SECRET=<non-empty>."""
        p = tmp_path / ".env_llm"
        _write_env_llm(p)
        keys = _parse_env_file(p)
        assert "SEARXNG_SECRET" in keys, f".env_llm must contain SEARXNG_SECRET=*** found keys: {list(keys)}"
        assert keys["SEARXNG_SECRET"], "SEARXNG_SECRET value is empty"
        assert "GENERATE_WITH" not in keys["SEARXNG_SECRET"], (
            f"SEARXNG_SECRET is still the placeholder: {keys['SEARXNG_SECRET']!r}"
        )

    def test_non_0600_mode_is_rejected(self, tmp_path):
        """Validation contract: files that are not 0600 must be detected."""
        p = tmp_path / ".env_llm"
        _write_env_llm(p, mode=0o644)  # world-readable
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode != 0o600, f"expected validation to reject mode {oct(mode)}"

    def test_missing_searxng_secret_is_rejected(self, tmp_path):
        """Validation contract: files without SEARXNG_SECRET must be detected."""
        p = tmp_path / ".env_llm"
        _write_env_llm(p, body="LLM_API_KEY=foo\n")
        keys = _parse_env_file(p)
        assert "SEARXNG_SECRET" not in keys, (
            f"expected validation to reject missing SEARXNG_SECRET, but parser found: {list(keys)}"
        )


class TestEnvLlmLegacyCompatibility:
    """If a legacy REPO_ROOT/.env_llm happens to exist (dev machine with
    real secrets), it should still be validated. Skipped when the file
    is absent — clean checkouts and CI without a fabricated file are
    fully supported.
    """

    @pytest.mark.skipif(
        not ENV_LLM.exists(),
        reason="REPO_ROOT/.env_llm not present (clean checkout / CI) — skip legacy check",
    )
    def test_legacy_env_llm_is_mode_600(self):
        mode = stat.S_IMODE(os.stat(ENV_LLM).st_mode)
        assert mode == 0o600, f"legacy .env_llm must be mode 0600, got {oct(mode)}"

    @pytest.mark.skipif(
        not ENV_LLM.exists(),
        reason="REPO_ROOT/.env_llm not present (clean checkout / CI) — skip legacy check",
    )
    def test_legacy_env_llm_has_searxng_secret(self):
        keys = _parse_env_file(ENV_LLM)
        assert "SEARXNG_SECRET" in keys, f"legacy .env_llm must contain SEARXNG_SECRET, found: {list(keys)}"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env-style file into a dict.

    Lines starting with '#' and empty lines are ignored. Each remaining
    line is split on the first '=' into (key, value). Values are stripped
    of surrounding whitespace and matching quote characters.
    """
    keys: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip().strip('"').strip("'")
    return keys


# --- .env_llm.example (portable, no real secrets) ---


class TestEnvLlmExample:
    """Tests for config/.env_llm.example — the portable template shipped
    in the archive (no real keys, valid env file syntax)."""

    def test_env_llm_example_exists(self):
        assert ENV_LLM_EXAMPLE.exists(), (
            f"missing {ENV_LLM_EXAMPLE} — archives and clean checkouts need this template"
        )

    def test_env_llm_example_is_valid_env_file(self):
        """Valid env file: each non-comment line is KEY=VALUE with at most one
        '=' separator. No multi-value lines like 'KEY=*** File permissions: ...'."""
        content = ENV_LLM_EXAMPLE.read_text(encoding="utf-8")
        bad_lines = []
        for n, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.count("=") == 0:
                bad_lines.append((n, "no '=' separator", line))
            elif stripped.count("=") > 1:
                bad_lines.append((n, "multiple '=' in one line", line))
        assert not bad_lines, ".env_llm.example has invalid env-file syntax:\n" + "\n".join(
            f"  line {n}: {reason}: {line!r}" for n, reason, line in bad_lines
        )

    def test_env_llm_example_contains_llm_api_key(self):
        content = ENV_LLM_EXAMPLE.read_text(encoding="utf-8")
        keys = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                keys[k.strip()] = v.strip()
        assert "LLM_API_KEY" in keys, (
            f".env_llm.example must declare LLM_API_KEY=..., found keys: {list(keys)}"
        )

    def test_env_llm_example_values_are_placeholders(self):
        """No real-looking OpenRouter keys (which start with 'sk-or-v1-' and
        have 60+ hex/alphanumeric chars after)."""
        content = ENV_LLM_EXAMPLE.read_text(encoding="utf-8")
        for n, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            v = v.strip().strip('"').strip("'")
            # Real OpenRouter keys look like: sk-or-v1-<60+ chars>
            if v.startswith("sk-or-v1-") and len(v) > 30:
                # Allow only if the suffix looks like a placeholder
                tail = v[len("sk-or-v1-") :]
                if not all(c in ".*-_" or c.isalnum() and (c.isalpha() or c.isdigit()) for c in tail):
                    continue  # has special chars, likely placeholder
                # Real key: 60+ alphanumeric without placeholders
                if len(tail) > 50 and tail.replace("0", "").replace("1", "").isalnum():
                    pytest.fail(
                        f"line {n} in .env_llm.example looks like a real key: {line!r}. "
                        "Replace with placeholder like 'sk-or-...n'."
                    )
