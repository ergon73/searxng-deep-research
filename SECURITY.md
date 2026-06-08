# Security Policy

## Scope

This document describes security policy for the **deep-research-project** — a local SearXNG-based research fetcher with 4-level fact verification and optional LLM cross-check.

## Reporting vulnerabilities

If you discover a security issue, contact the project maintainer directly. Do not open a public GitHub issue for security-sensitive problems.

## Threat model

This is a **single-user, local tool** running on a personal VPS. Threat model:

| Threat | In scope? | Why |
|---|---|---|
| Network attacker (DDOS, scan) | No | This is a private service, not exposed |
| Malicious search results | **Yes** | User can be tricked into following a malicious URL via crafted search query |
| Prompt injection via fetched web pages | **Yes** | Fetched content may contain LLM-targeted instructions |
| SSRF to internal services | **Yes** | VPS has private services (Valkey, SearXNG, possibly others) |
| Leaked API keys | **Yes** | OpenRouter/proxy keys in `.env_*` files |
| Privilege escalation via Hermes agent | **Yes** | Agent has host-level access in local backend |

## Hard rules (non-negotiable)

### Network / SSRF

- ✅ **Allow only http/https** schemes in `_is_safe_fetch_url()`
- ✅ **Allow only `ip.is_global`** (no loopback, private, link-local, multicast, reserved)
- ✅ **Validate redirects** via `SafeRedirectHandler` — public URL must not redirect to internal
- ✅ **Timeout every request** (default 12s, max 20s)
- ❌ Never fetch URLs from untrusted sources without SSRF check
- ❌ Never follow redirects blindly (default `urllib` does — we override)

### Secrets

- ✅ API keys only in `/opt/searxng/.env_*` files with `chmod 600`
- ✅ `.env_proxy` and `.env_llm` are NOT committed to git (in `.gitignore`)
- ❌ Never log secrets, never echo them
- ❌ Never put secrets in code, even in comments

### Shell

- ❌ Never use `shell=True` in `subprocess`
- ✅ Use list args, shell-quote dynamic strings
- ❌ Never construct shell commands from user input without validation

### Dependencies

- ✅ Pinned sane ranges in `requirements.txt`
- ❌ Never `curl | bash` from untrusted sources
- ❌ Never install from random URLs

### LLM / tool safety

- ✅ Tool outputs (web pages, LLM responses, file contents) are **untrusted**
- ✅ Web pages may contain prompt injection — never follow page instructions as system instructions
- ✅ Keep citations/source separation (don't let LLM fabricate URLs)
- ✅ LLM-conditional verification is **off by default** if `OPENROUTER_API_KEY` not set

## Security checks in this project

### Automated

```bash
# SSRF guard
cd /opt/searxng && python3 -m pytest tests/test_url_safety.py -v

# Lint (catches many simple bugs)
cd /opt/searxng && ruff check hermes_deepresearch.py hermes_searxng.py llm_verifier.py

# All unit tests
cd /opt/searxng && python3 -m pytest tests/ -v
```

### Manual review

When changing `fetch_url` or `_safe_urlopen`:

1. Can an attacker reach a private IP via a public URL? — must be blocked
2. Can an attacker reach internal services via redirect? — must be blocked
3. Can a malicious page send instructions to the LLM? — fetched text goes to LLM, but only as evidence, not as system prompt
4. Are we timing out? — every fetch has timeout

When changing `verify_sources` or LLM code:

1. Can a malicious source CONFIRM a false fact? — yes, if many compromised sources, but LLM-conditional mitigates
2. Can a fact be injected via negation games? — `_is_negated` checks for "не/нет/no/not" near fact
3. Does the LLM have system-prompt access to user's `.env`? — no, LLM is called via OpenRouter API only, no local prompt injection possible

## Security-relevant files

- `hermes_deepresearch.py` — `fetch_url`, `_is_safe_fetch_url`, `SafeRedirectHandler`
- `hermes_searxng.py` — `web_search` (talks to local SearXNG)
- `llm_verifier.py` — `LLMVerifier` (talks to OpenRouter, no local secrets)
- `tests/test_url_safety.py` — SSRF regression tests

## Recent security history

- v0.7.3 (5 June 2026): SSRF guard added (blacklist of private IPs)
- v0.8 (5 June 2026): SSRF guard rewritten to `ip.is_global` allowlist; `SafeRedirectHandler` added; canonical URL dedup; SUPPORTS/REFUTES/INSUFFICIENT verdicts
- v0.8.1 (this version): negation detection widened; `not/no` EN support; plural awareness

## Out of scope (for now)

- TLS certificate validation: standard Python `ssl.create_default_context()` does this
- Rate limiting: SearXNG `limiter: false` (single-user)
- Auth: no auth on SearXNG (localhost only)
- Database: no persistent DB (Redis is cache only)

## Acknowledgments

This security policy was informed by:
- ChatGPT review of v0.7-v0.8 (see `DR-05062026.txt`, `DR-05062026(2).txt`)
- OWASP SSRF prevention cheatsheet
- Hermes Agent security docs
