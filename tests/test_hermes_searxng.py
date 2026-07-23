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

import json


def test_module_imports_cleanly():
    """hermes_searxng should import without touching .env_proxy."""
    import importlib
    mod = importlib.import_module("hermes_searxng")
    assert mod is not None


def test_public_api_still_callable():
    """web_search and news_search must remain callable after refactor."""
    from hermes_searxng import news_search, web_search
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


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def test_metadata_response_preserves_engine_health(monkeypatch):
    """Rich mode must not discard SearXNG engine and failure metadata."""
    import hermes_searxng as mod

    payload = {
        "results": [
            {
                "engine": "bing",
                "engines": ["bing", "mojeek"],
                "title": "Primary source",
                "url": "https://example.com/release",
                "content": "Released today",
                "score": 4.2,
                "category": "general",
                "publishedDate": "2026-07-23T00:00:00Z",
            }
        ],
        "unresponsive_engines": [
            ["duckduckgo", "CAPTCHA"],
            ["brave", "too many requests"],
        ],
        "suggestions": ["example model"],
    }
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(payload),
    )

    response = mod.web_search(
        "example release",
        max_results=5,
        retries=0,
        include_metadata=True,
    )

    assert isinstance(response, mod.SearchResponse)
    assert response.error is None
    assert response.responding_engines == ("bing", "mojeek")
    assert response.unresponsive_engines == (
        "brave: too many requests",
        "duckduckgo: CAPTCHA",
    )
    assert response.suggestions == ("example model",)
    assert response.degraded is True
    assert response.hits[0]["engines"] == ["bing", "mojeek"]
    assert response.hits[0]["score"] == 4.2
    assert response.hits[0]["published_date"] == "2026-07-23T00:00:00Z"


def test_legacy_web_search_still_returns_plain_list(monkeypatch):
    """Existing Hermes callers keep their list-of-hits contract."""
    import hermes_searxng as mod

    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(
            {
                "results": [
                    {
                        "engine": "bing",
                        "title": "Result",
                        "url": "https://example.com",
                        "content": "Snippet",
                    }
                ]
            }
        ),
    )

    hits = mod.web_search("example", retries=0)

    assert isinstance(hits, list)
    assert hits[0]["engine"] == "bing"


def test_metadata_response_records_transport_error(monkeypatch):
    """A network failure must be observable instead of becoming a silent []."""
    import hermes_searxng as mod

    def fail(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fail)

    response = mod.web_search(
        "example",
        retries=0,
        include_metadata=True,
    )

    assert isinstance(response, mod.SearchResponse)
    assert response.hits == []
    assert response.error == "TimeoutError: timed out"
    assert response.degraded is True
