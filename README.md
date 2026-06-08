# searxng-deep-research

Local SearXNG-based research fetcher with 4-level fact verification and optional LLM cross-check.

**Version:** v0.8 (5 June 2026)
**Status:** working prototype, see `ISSUES.md` and `SECURITY.md` for current state.

## What this is

- `src/hermes_deepresearch.py::deep_research()` — entry point
- `src/evidence.py`, `src/routing.py`, `src/synthesis.py`, `src/critical_review.py`, `src/llm_verifier.py` — pipeline stages
- `src/models.py` — typed state skeleton (Phase 1, v0.8.0: `SearchTask`, `Claim`, `EvidenceWindow`, `ResearchState`)
- `src/redact.py` — secret redaction (mandatory before archive/chat)
- `src/hermes_searxng.py` — SearXNG JSON helper
- `scripts/e2e_falcon9.py` — 8-stage end-to-end smoke (~2s)
- `scripts/eval.py` + `data/eval_set.json` — synthetic eval set
- `tests/` — 397 portable tests, run with `PYTHONPATH=src python3 -m pytest`

## Read these first

- `AGENTS.md` — project rules and security policy for any coding agent
- `SECURITY.md` — threat model, hard rules, secret redaction policy
- `ARCHITECTURE.md` — pipeline diagram and data flow
- `ISSUES.md` — known gaps, open/closed issues, verification commands
- `INSTALL.md` — how to run the SearXNG + Valkey stack locally

## Quick start (for review)

```bash
# Tests (offline, no LLM calls)
PYTHONPATH=src python3 -m pytest

# End-to-end smoke
PYTHONPATH=src python3 scripts/e2e_falcon9.py

# Eval run (offline)
PYTHONPATH=src python3 scripts/eval.py
```

## Privacy

This is a **public repository**. It contains a development snapshot of
the project. The `.gitignore` excludes all real secrets (`.env_llm`, `.env_proxy`),
local caches, and external review snapshots. Only example configs and synthetic
eval data are committed.
