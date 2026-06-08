"""
Tests for hermes_searxng.py — clean proxy code removal (v0.8.1.1).

Verifies that the dead proxy code (_load_proxy, PROXY_URL, _has_proxy,
_PROXY_ENV_PATH) is gone, and the public API (web_search, news_search)
is still importable and callable.

v0.8.1 hardening: ChatGPT P1 (hermes_searxng.py reads .env_proxy at
import — secret surface + VPS coupling). Fix: remove dead code. The
SearXNG instance handles its own per-engine proxying via settings.yml.
"""
from __future__ import annotations


def test_module_imports_cleanly():
    """hermes_searxng should import without touching .env_proxy."""
    import importlib
    mod = importlib.import_module("hermes_searxng")
    assert mod is not None


def test_public_api_still_callable():
    """web_search and news_search must remain callable after refactor."""
    from hermes_searxng import web_search, news_search
    assert callable(web_search)
    assert callable(news_search)


def test_proxy_dead_code_removed():
    """_load_proxy, PROXY_URL, _has_proxy, _PROXY_ENV_PATH should be gone."""
    import hermes_searxng as mod
    for name in ("_load_proxy", "PROXY_URL", "_has_proxy", "_PROXY_ENV_PATH"):
        assert not hasattr(mod, name), (
            f"hermes_searxng still exports {name!r} — should be removed"
        )


def test_pathlib_unused_import_removed():
    """pathlib.Path was only used by _PROXY_ENV_PATH; should no longer be imported."""
    import hermes_searxng as mod
    # We don't assert it's absent from sys.modules (other modules use it),
    # only that hermes_searxng doesn't reference Path itself.
    src = open(mod.__file__, encoding="utf-8").read()
    assert "from pathlib import Path" not in src, (
        "hermes_searxng still imports pathlib.Path — should be removed"
    )


def test_docstring_no_longer_references_legacy_env_proxy_path():
    """Module docstring should not hardcode /opt/searxng/.env_proxy."""
    import hermes_searxng as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "/opt/searxng/.env_proxy" not in src, (
        "hermes_searxng still references hardcoded VPS path /opt/searxng/.env_proxy"
    )
