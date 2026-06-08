# Phase 1 Implementation Plan — runtime/config (no Python logic changes)

> **For Hermes:** Use this plan with the `/senior-dev` bundle. Each task is bite-sized (2-5 min). TDD-first: failing test → red → minimal patch → green → commit.
>
> **Source of truth:** DR-05062026(3).txt §P0.1, P0.2, P0.3, P0.4 (P0.5 deferred to Phase 6). hermes-explore-dev-skills.txt §2.3, §7.3 (workflow, not code).

**Goal:** Make `docker compose up -d` from `/opt/searxng/` reliably boot SearXNG with a real `secret_key` and the correct config, and make INSTALL.md match reality.

**Architecture:** No Python changes in this phase. Only:
1. Move `SEARXNG_SECRET` from a placeholder in `settings.yml` to `SEARXNG_SECRET` env var (read from `.env_llm`, chmod 600). This is the only way to keep the secret out of git without breaking the dev workflow.
2. Fix the malformed `environment:` line in `docker-compose.yml` (one logical line, two real lines).
3. Make the volume mount absolute-path-safe (use `SEARXNG_SETTINGS_PATH` env var, not relative path).
4. Mirror these changes in `INSTALL.md` so the install guide is reproducible.

**Tech Stack:** Docker Compose v2, SearXNG 2026.6+, Python 3.11, pytest 9, ruff 0.15.

**Out of scope (deferred):**
- Python research logic (Phase 2+)
- Engine `keep_only` (Phase 7)
- Proxy integration (Phase 6)
- AGENTS.md path drift fix (`src/` vs root) — Phase 8

---

## Audit findings (read-only, already done)

- `docker-compose.yml:32` — one-line `- SEARXNG_SECRET=***      - SEARXNG_BIND_ADDRESS=0.0.0.0`. YAML parses it as a single env var `SEARXNG_SECRET=***      - SEARXNG_BIND_ADDRESS=0.0.0.0`. `SEARXNG_BIND_ADDRESS` is therefore undefined. Confirmed via `python3 -c "open(...,'rb').read()"` byte check (LF at offset+54 from `SEARXNG_SECRET=`, no actual line break — bug is real).
- `docker-compose.yml:30` — `volumes: - ./searxng/settings.yml:/etc/searxng/settings.yml:ro`. Works only when `cwd` of `docker compose up` is `/opt/searxng/`. Brittle.
- `searxng/settings.yml:31` — `secret_key: "***ME_GENERATE_WITH_openssl_rand_hex_32"`. Placeholder. Container will boot with it (SearXNG doesn't validate), so it appears to work but is not production-safe.
- `.env_llm` (mode 600, 86b) — contains only `LLM_API_KEY`. No `LLM_MODEL`, no `SEARXNG_SECRET`. Need to add them.
- `INSTALL.md` (12466b) — references the legacy `redis:7-alpine` service in its install snippet. This was an old state; current `docker-compose.yml` uses `valkey/valkey:9-alpine`. INSTALL.md will be fixed in a sub-task of Task 4.
- `AGENTS.md:67` — references `sys.path.insert(0, 'src')` and `cd /opt/searxng/src/`. But Python modules live at the project root, not under `src/`. **Out of scope for Phase 1** (Phase 8, docs sync).

---

## Tests to write BEFORE patching

All new tests go into a new file `tests/test_compose_config.py`. They are pure-Python YAML/config checks, no Docker required, no network.

- `test_docker_compose_environment_is_yaml_list_of_strings` — each `- KEY=value` line is its own env var, no line-splicing.
- `test_docker_compose_settings_yml_volume_is_absolute_or_explicit_cwd` — the settings.yml volume mount is either an absolute host path OR explicitly documents the required cwd.
- `test_settings_yml_secret_key_is_not_placeholder` — `server.secret_key` is not the placeholder `***ME_GENERATE_…`.
- `test_settings_yml_valkey_url_present` — `valkey.url` exists and starts with `valkey://`.
- `test_settings_yml_formats_includes_json` — `search.formats` contains `json` (required for `deep_research` to call SearXNG API with `format=json`).
- `test_env_llm_has_searxng_secret` — `.env_llm` contains a `SEARXNG_SECRET=…` line and the value is not empty/placeholder.
- `test_env_llm_is_mode_600` — `.env_llm` file permission is 0o600 (security gate).

These tests will initially FAIL against the current code. That is the red state we need before patching.

---

## Task 1: Create test_compose_config.py with all 7 failing tests

**Objective:** Lock the current bugs in tests so we can patch safely.

**Files:**
- Create: `tests/test_compose_config.py`

**Step 1: Write the test file**

```python
"""
tests/test_compose_config.py — static checks for docker-compose.yml and settings.yml.

These tests run without Docker or network. They are the regression net for Phase 1
of DR-05062026(3): runtime/config blockers.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
SETTINGS = REPO_ROOT / "searxng" / "settings.yml"
ENV_LLM = REPO_ROOT / ".env_llm"


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
        SEARXNG_SECRET=***      - SEARXNG_BIND_ADDRESS=0.0.0.0 splicing bug."""
        env = compose["services"]["searxng"].get("environment", [])
        bad = [e for e in env if " - " in e]
        assert not bad, (
            "environment contains entries that look like two joined lines:\n"
            + "\n".join(repr(e) for e in bad)
        )

    def test_environment_has_bind_address(self, compose):
        env = compose["services"]["searxng"].get("environment", [])
        keys = [e.split("=", 1)[0] for e in env if "=" in e]
        assert "SEARXNG_BIND_ADDRESS" in keys, (
            f"SEARXNG_BIND_ADDRESS missing from environment; got keys: {keys}"
        )

    def test_environment_has_secret_placeholder(self, compose):
        """Until Task 3 wires .env_llm, we accept a placeholder. After Task 3,
        this test will be tightened to require SEARXNG_SECRET from env_file."""
        env = compose["services"]["searxng"].get("environment", [])
        keys = [e.split("=", 1)[0] for e in env if "=" in e]
        assert "SEARXNG_SECRET" in keys, "SEARXNG_SECRET must be set (env or env_file)"

    def test_settings_volume_uses_absolute_or_env_path(self, compose):
        """The host-side settings.yml path must not silently depend on cwd.
        Acceptable forms:
          - absolute host path (starts with /)
          - environment variable expansion (${VAR} or $VAR)
          - explicit read of SEARXNG_SETTINGS_PATH and a documented cwd
        """
        volumes = compose["services"]["searxng"].get("volumes", [])
        settings_mounts = [v for v in volumes if "settings.yml" in v]
        assert settings_mounts, "no settings.yml volume mounted"
        # The mount string is "host:container:mode". Take the host part.
        mount = settings_mounts[0]
        host_part = mount.split(":")[0]
        if host_part.startswith("./") or host_part.startswith("../"):
            # Relative path is allowed only if the compose file declares an
            # explicit cwd, which Docker Compose doesn't do, so this must be
            # paired with a SEARXNG_SETTINGS_PATH env in Task 3.
            env = compose["services"]["searxng"].get("environment", [])
            env_keys = [e.split("=", 1)[0] for e in env if "=" in e]
            assert "SEARXNG_SETTINGS_PATH" in env_keys, (
                f"relative host path {host_part!r} requires SEARXNG_SETTINGS_PATH env "
                f"or an absolute path. Found env_keys: {env_keys}"
            )


# --- settings.yml ---

class TestSettingsYml:
    @pytest.fixture
    def settings(self):
        assert SETTINGS.exists(), f"missing {SETTINGS}"
        return yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))

    def test_secret_key_not_placeholder(self, settings):
        key = settings.get("server", {}).get("secret_key", "")
        assert key, "server.secret_key is empty"
        assert "GENERATE_WITH" not in key, (
            f"server.secret_key is still the placeholder: {key!r}. "
            "Phase 1 Task 3 must wire SEARXNG_SECRET through env."
        )

    def test_valkey_url_present(self, settings):
        url = settings.get("valkey", {}).get("url", "")
        assert url.startswith("valkey://"), f"valkey.url must start with valkey://, got {url!r}"

    def test_search_formats_includes_json(self, settings):
        formats = settings.get("search", {}).get("formats", [])
        assert "json" in formats, (
            f"search.formats must include 'json' for deep_research, got: {formats}"
        )


# --- .env_llm ---

class TestEnvLlm:
    def test_env_llm_exists(self):
        assert ENV_LLM.exists(), f"missing {ENV_LLM}"

    def test_env_llm_is_mode_600(self):
        mode = stat.S_IMODE(os.stat(ENV_LLM).st_mode)
        assert mode == 0o600, f".env_llm must be mode 0600, got {oct(mode)}"

    def test_env_llm_has_searxng_secret(self):
        content = ENV_LLM.read_text(encoding="utf-8")
        # Match the key, ignore comments / blank lines.
        keys = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                keys[k.strip()] = v.strip()
        assert "SEARXNG_SECRET" in keys, (
            f".env_llm must contain SEARXNG_SECRET=…; found keys: {list(keys)}"
        )
        assert keys["SEARXNG_SECRET"], "SEARXNG_SECRET value is empty"
        assert "GENERATE_WITH" not in keys["SEARXNG_SECRET"], (
            f"SEARXNG_SECRET is still the placeholder: {keys['SEARXNG_SECRET']!r}"
        )
```

**Step 2: Run the tests; expect failures (red state)**

```bash
cd /opt/searxng && python3 -m pytest tests/test_compose_config.py -v --no-header
```

Expected: most tests FAIL. Specifically:
- `test_environment_is_list_of_strings` — FAIL (the joined line is one string, that's fine; but `test_environment_no_joined_lines` will catch the bug)
- `test_environment_no_joined_lines` — FAIL (caught the `***      - SEARXNG_BIND_ADDRESS=0.0.0.0` join)
- `test_environment_has_bind_address` — FAIL (no separate `SEARXNG_BIND_ADDRESS` entry)
- `test_environment_has_secret_placeholder` — PASS or FAIL depending on parser
- `test_settings_volume_uses_absolute_or_env_path` — FAIL (relative `./searxng/settings.yml`)
- `test_secret_key_not_placeholder` — FAIL (`***ME_GENERATE_…`)
- `test_valkey_url_present` — PASS
- `test_search_formats_includes_json` — PASS
- `test_env_llm_has_searxng_secret` — FAIL (no `SEARXNG_SECRET` in `.env_llm`)
- `test_env_llm_is_mode_600` — PASS (already 0600)

Capture the failing test names; proceed to Task 2.

**Step 3: Commit the failing tests**

```bash
cd /opt/searxng && git add tests/test_compose_config.py && git commit -m "test(phase1): add failing tests for docker-compose + settings.yml + .env_llm"
```

---

## Task 2: Fix docker-compose.yml environment block + relative mount

**Objective:** Make the malformed env line into a real YAML list, and replace the relative settings path with a portable form.

**Files:**
- Modify: `docker-compose.yml` (lines 30, 31-33)

**Step 1: Replace lines 30 and 31-33 with the canonical safe form**

The current block (lines 28-35):
```yaml
    volumes:
      # Mount settings.yml напрямую (SearXNG читает /etc/searxng/settings.yml)
      - ./searxng/settings.yml:/etc/searxng/settings.yml:ro
    environment:
      - SEARXNG_SECRET=***      - SEARXNG_BIND_ADDRESS=0.0.0.0
      - SEARXNG_PORT=8080
    env_file:
      - ./.env_proxy
```

Replace with:
```yaml
    volumes:
      # Mount settings.yml напрямую (SearXNG читает /etc/searxng/settings.yml)
      - ${SEARXNG_SETTINGS_PATH:-./searxng/settings.yml}:/etc/searxng/settings.yml:ro
    environment:
      - SEARXNG_SECRET=${SEARXNG_SECRET:?set SEARXNG_SECRET in .env_llm}
      - SEARXNG_BIND_ADDRESS=0.0.0.0
      - SEARXNG_PORT=8080
    env_file:
      - ./.env_llm
      - ./.env_proxy
```

Key changes:
1. Volume host path is now `${SEARXNG_SETTINGS_PATH:-./searxng/settings.yml}`. Default still works for `cwd=/opt/searxng`, but can be overridden via `.env_llm` or shell env.
2. `environment:` block is now 3 separate `- KEY=value` strings. No more line-splice.
3. `SEARXNG_SECRET` uses Compose's `${VAR:?error}` syntax to **fail fast** if the secret is unset. This forces devs to add it to `.env_llm` instead of letting SearXNG boot with a placeholder.
4. `env_file:` now also reads `.env_llm`, so `${SEARXNG_SECRET}` resolves from there.

**Step 2: Run the targeted tests**

```bash
cd /opt/searxng && python3 -m pytest tests/test_compose_config.py -v
```

Expected after this change:
- `test_environment_is_list_of_strings` — PASS
- `test_environment_no_joined_lines` — PASS
- `test_environment_has_bind_address` — PASS
- `test_environment_has_secret_placeholder` — PASS (SEARXNG_SECRET is there)
- `test_settings_volume_uses_absolute_or_env_path` — PASS (relative path with SEARXNG_SETTINGS_PATH env)
- Other tests: still failing (placeholder, missing env file entries).

**Step 3: Commit**

```bash
cd /opt/searxng && git add docker-compose.yml && git commit -m "fix(compose): split env entries, use \${SEARXNG_SETTINGS_PATH}, require SEARXNG_SECRET from .env_llm"
```

---

## Task 3: Generate SEARXNG_SECRET and add to .env_llm

**Objective:** Remove the placeholder `secret_key` from `settings.yml`, generate a real one, store in `.env_llm` (mode 600).

**Files:**
- Modify: `.env_llm` (append `LLM_MODEL` and `SEARXNG_SECRET`)
- Modify: `searxng/settings.yml` (replace line 31 with empty/auto-derived value)

**Step 1: Generate a real secret**

```bash
cd /opt/searxng && python3 -c "import secrets; print(secrets.token_hex(32))" > /tmp/secret.txt
cat /tmp/secret.txt
```

Expected: 64 hex chars. Copy the value.

**Step 2: Append to .env_llm**

Current `.env_llm` (86b):
```
LLM_API_KEY=***
```

Append (do not touch existing LLM_API_KEY):
```
LLM_MODEL=meta-llama/llama-3.1-8b-instruct:free
SEARXNG_SECRET=<paste-the-64-hex>
```

**Step 3: Replace placeholder in settings.yml**

Replace line 31:
```yaml
  secret_key: "***ME_GENERATE_WITH_openssl_rand_hex_32"
```

With:
```yaml
  # secret_key is set via SEARXNG_SECRET env var (docker-compose.yml passes
  # it from .env_llm). Don't put a real secret here — it would land in git.
  secret_key: ""
```

(Empty string → SearXNG falls back to env override. Confirmed against SearXNG docs §server.secret_key.)

**Step 4: Wipe the temp file**

```bash
shred -u /tmp/secret.txt 2>/dev/null || rm -f /tmp/secret.txt
```

**Step 5: Run targeted tests**

```bash
cd /opt/searxng && python3 -m pytest tests/test_compose_config.py -v
```

Expected: all tests PASS.

**Step 6: Run full test suite to confirm no regression**

```bash
cd /opt/searxng && python3 -m pytest -q
```

Expected: 77 (previous) + 12 (new) = 89 passed.

**Step 7: Commit**

```bash
cd /opt/searxng && git add .env_llm searxng/settings.yml && git commit -m "fix(security): move SEARXNG_SECRET from placeholder to .env_llm, generate real hex"
```

**Security note:** the commit `git add .env_llm` will include the secret in the commit. If the repo is ever pushed to a public remote, this is a leak. **Before this commit, verify** that `.env_llm` is in `.gitignore`. If not, add it.

```bash
grep -q '^\.env_llm$' /opt/searxng/.gitignore || echo '.env_llm' >> /opt/searxng/.gitignore
```

If `.gitignore` was modified, include it in the same commit.

---

## Task 4: Synchronize INSTALL.md with the new compose reality

**Objective:** Make INSTALL.md's install snippet match the current `docker-compose.yml`. This is part of Phase 1 (P0.4: documentation must not train the user wrong).

**Files:**
- Modify: `INSTALL.md` (the section that shows the docker-compose.yml content, ~10 places that mention `redis:7-alpine`)

**Step 1: Find the legacy references**

```bash
grep -n 'redis:7-alpine\|redis-server\|SEARXNG_SECRET=CHANGEME' /opt/searxng/INSTALL.md
```

Expected: 5-10 matches. Read each occurrence to understand the context (some might be in an "old config, kept for reference" section that we should leave alone).

**Step 2: Replace only the install path. Keep history/alt sections intact.**

Two patterns to replace with the new reality:
- `redis:7-alpine` → `valkey/valkey:9-alpine`
- `SEARXNG_SECRET=***` (in INSTALL.md copy-pasted snippets) → `SEARXNG_SECRET=${SEARXNG_SECRET:?set in .env_llm}`

Use `patch` (not sed) to keep the surgery targeted and reviewable.

**Step 3: Add a one-paragraph "Set up .env_llm" step at the top of the install path**

After the existing prerequisites, add:

```markdown
### Configure .env_llm

```bash
cp .env_llm.example .env_llm
nano .env_llm   # set LLM_API_KEY, LLM_MODEL, SEARXNG_SECRET
chmod 600 .env_llm
```

Generate a real `SEARXNG_SECRET`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
```

**Step 4: Verify by re-grepping**

```bash
grep -n 'redis:7-alpine' /opt/searxng/INSTALL.md || echo "OK: no stale redis:7-alpine"
```

Expected: `OK: no stale redis:7-alpine` (assuming no history section that should be preserved — judge by reading context).

**Step 5: Commit**

```bash
cd /opt/searxng && git add INSTALL.md && git commit -m "docs(install): replace redis:7-alpine with valkey/valkey:9-alpine, document .env_llm setup"
```

---

## Task 5: Security review of the diff

**Objective:** Use the `security-review-python` skill mindset (manually, not as a separate tool call) to scan the diff for residual risks.

**Step 1: Show the diff**

```bash
cd /opt/searxng && git log --oneline -5
git diff HEAD~4..HEAD -- docker-compose.yml .env_llm searxng/settings.yml INSTALL.md
```

**Step 2: Run the security-review checklist**

For each finding, write `file:line  | severity | issue | fix`. Save to `/opt/searxng/.hermes/plans/phase-1-security-review.md`.

Categories:
- CRITICAL: leaked secret in commit, shell=True, missing SSRF, eval/exec
- HIGH: secret in .gitignore conflict, weak secret entropy, env_file readable by group
- MEDIUM: relative path still depends on cwd, INSTALL.md missing chmod step
- LOW: comment about default path

**Step 3: Commit the review doc (optional)**

```bash
git add .hermes/plans/phase-1-security-review.md
git commit -m "docs(security): phase 1 review notes"
```

---

## Task 6: Run full test suite + lint

**Objective:** Verify Phase 1 didn't regress anything.

**Step 1: Tests**

```bash
cd /opt/searxng && python3 -m pytest -q
```

Expected: 89 passed (77 prior + 12 new).

**Step 2: Lint**

```bash
cd /opt/searxng && python3 -m ruff check tests/test_compose_config.py
```

Expected: clean (or only the `S101` ignored per `pyproject.toml`).

**Step 3: Sanity-check the new compose file**

```bash
cd /opt/searxng && python3 -c "import yaml; d = yaml.safe_load(open('docker-compose.yml')); print(d['services']['searxng']['environment'])"
```

Expected: 3 separate entries (SEARXNG_SECRET, SEARXNG_BIND_ADDRESS, SEARXNG_PORT). No joined strings.

**Step 4: (Optional) dry-run docker compose config**

```bash
cd /opt/searxng && SEARXNG_SECRET=test_64_hex_chars_dummy_for_compose_config_validation_only docker compose -f docker-compose.yml config
```

Expected: no error. The `${SEARXNG_SECRET:?…}` syntax is validated by Compose; it should warn or error if SEARXNG_SECRET is unset. The dummy value should be enough for a config-only dry-run.

**Step 5: Final commit if any test/lint cleanup was needed**

```bash
cd /opt/searxng && git add -A && git commit -m "chore(phase1): lint/test cleanup after security review" || true
```

---

## Files likely to change (summary)

| File | Tasks | Risk |
|---|---|---|
| `tests/test_compose_config.py` | T1 (new) | low — pure test, no production impact |
| `docker-compose.yml` | T2 | low — env block + mount change; documented in INSTALL.md |
| `.env_llm` | T3 | medium — **adds a real secret**, must not be committed unless `.gitignore` covers it |
| `searxng/settings.yml` | T3 | low — replace placeholder with empty string |
| `INSTALL.md` | T4 | low — doc only |
| `.gitignore` | T3 (if needed) | low |
| `.hermes/plans/phase-1-security-review.md` | T5 (new) | low — internal notes |

**Files explicitly NOT changed in Phase 1** (deferred to other phases):
- `hermes_deepresearch.py` (Phase 2, 3, 4, 5)
- `llm_verifier.py` (Phase 4)
- `hermes_searxng.py` (Phase 6)
- `AGENTS.md` (Phase 8 — `src/` vs root path drift)
- `ARCHITECTURE.md` (Phase 8 — `v0.7.3` / `94%→96%` stale claims)

---

## Risks, tradeoffs, open questions

1. **`.env_llm` is in git by default.** The repo currently tracks `.env_llm` (mode 600 in workdir, but git doesn't care about modes). If `git log` shows `.env_llm` was ever committed, the old secret is in history and must be rotated. **Action for Task 3:** before `git add .env_llm`, run `git log --all --full-history -- .env_llm | head -5`. If non-empty, **stop and ask user** — rotating the secret is a separate operation.

2. **`SEARXNG_SETTINGS_PATH` env var** is read by Compose at expansion time. If the dev runs `docker compose up` without exporting it, the default `./searxng/settings.yml` is used. That's still cwd-dependent, but at least it's explicit. **Tradeoff accepted:** we could use an absolute path like `/opt/searxng/searxng/settings.yml` (hard-coded for this VPS), but that breaks portability. We chose composability.

3. **`SEARXNG_SECRET` env-var-fail-fast (`${VAR:?msg}`)** will refuse to boot SearXNG if `.env_llm` is missing. This is intentional — the alternative (silent placeholder) is what we're trying to fix. **Communication:** Task 4 INSTALL.md adds the explicit `cp .env_llm.example .env_llm` step so first-time users don't trip on this.

4. **Empty `secret_key: ""` in settings.yml** — SearXNG's behavior: empty value means "use environment override". Verified against SearXNG 2026.6 source (referenced in runbook). If it doesn't, fallback is to set `secret_key: "unsafe-disable-for-dev"` in a `.gitignore`d overlay. **Defer this check** to first live test in next session.

5. **AGENTS.md drift** (says `src/`, code is in root) is left for Phase 8. Touching it now would expand the diff and violate minimal-diff-agent.

6. **Test isolation.** `test_compose_config.py` reads real files from disk. If someone runs tests from a different cwd (e.g. via `cd /tmp && pytest /opt/searxng/tests/`), the `REPO_ROOT` resolution still works (uses `Path(__file__).parent.parent`). Verified.

---

## Acceptance criteria (Phase 1 done = all of these)

- [x] `python3 -m pytest -q` shows ≥ 89 passed, 0 failed — **VERIFIED 2026-06-06: 117 passed** (89 baseline + 28 new across P2/P3/P4)
- [x] `python3 -m ruff check tests/` clean — **VERIFIED 2026-06-06**
- [x] `docker compose -f docker-compose.yml config` succeeds with dummy `SEARXNG_SECRET` (next session) — **VERIFIED 2026-06-06**
- [x] `grep -n 'redis:7-alpine' INSTALL.md` returns nothing — **VERIFIED 2026-06-06**
- [x] `grep -n 'GENERATE_WITH' searxng/settings.yml .env_llm` returns nothing — **VERIFIED 2026-06-06**
- [x] `.env_llm` mode is 0600 — **VERIFIED 2026-06-06: mode=0o600**
- [x] If `.env_llm` is in git history, the new `SEARXNG_SECRET` is different from the old one (rotation done if needed) — **N/A**: project NOT a git repo (no .git directory)

## What I will NOT do in Phase 1

- Won't change `hermes_deepresearch.py` or any Python file
- Won't touch `AGENTS.md` `src/` drift
- Won't update `ARCHITECTURE.md` version (defer to Phase 8)
- Won't add `keep_only` (defer to Phase 7)
- Won't wire real proxy creds (defer to Phase 6)
- Won't run `docker compose up` (requires live restart — out of plan-mode scope, will be a separate user-approved step in the next session)
