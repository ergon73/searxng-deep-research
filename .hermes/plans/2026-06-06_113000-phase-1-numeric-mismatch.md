# Phase 1 Implementation Plan — БПЛА morphology + date dedup (SCOPED v2)

> **For Hermes:** Use this plan with the `/senior-dev` bundle. TDD-first: failing test → red → minimal patch → green → verify.
>
> Source: DR-06062026.txt §"Phase 1 — correctness hotfix" (subset).
> Scope: close ISSUES.md **#003** (date dedup) and **#004** (БПЛА morphology) only.
> **OUT OF SCOPE (deferred to Phase 1.5):** AC#2 ("123 vs 124 → not SUPPORTS") — see "Why this scope" below.

**Goal:** Two surgical fixes in `src/hermes_deepresearch.py`:
1. Date dedup: when a partial date ("5 июня") and a full date ("5 июня 2026") both fire, **the full date wins** (the partial is dropped, not the other way around).
2. БПЛА ↔ беспилотник / дрон cross-stem equivalence: the same numeric fact in two surface forms shares the same canonical stem.

**Architecture (2 surgical edits, all in `hermes_deepresearch.py`):**
1. `_extract_facts()` extraction order: swap blocks so `FACT_RE_DATE` runs **before** `fact_re_num_ctx`. Currently num_ctx (catches "5 июня") runs first and adds the partial to `seen` before the full date arrives.
2. `_add()` symmetric dedup: add a branch that, when an existing short phrase is a substring of a new long phrase, evicts the short and keeps the long. Currently the code only drops the new phrase, never replaces the old.
3. `_MORPH_MAP`: change `"бпла": "бпла"` → `"бпла": "беспилотник"` so БПЛА and беспилотник share the stem.

**Tech Stack:** Python 3.11, re stdlib. No new deps.

**Out of scope (deferred):**
- LLM verifier per-fact error aggregation (DR §Phase 1 AC#3, #4) — separate sub-phase.
- Entity extraction stop verbs (DR §Phase 1 AC#5) — already working per P3 unit tests; no fix needed.
- **AC#2 ("123 vs 124 → not SUPPORTS")** — see "Why this scope" below. Tracked as new issue #012.
- Phase 2 (ranking + news routing), Phase 3 (evidence windows), Phase 4 (claim model), Phase 5 (evals), Phase 6 (proxy), Phase 8 (docs).

### Why this scope (Вариант 2)

While writing the original Phase 1 plan, I discovered a **design conflict** between search-time and verify-time semantics of `_match_in_text()`:

| Use case | "1 час" vs "3 часов" | "123 дрона" vs "124 дрона" |
|---|---|---|
| **Search ranking** (find docs about drones) | should match (different numbers OK) | should match |
| **Fact verification** (does doc support "123 дрона"?) | should NOT match | should NOT match |

The P3 plan (2026-06-06 09:00) shipped search-time semantics. Existing tests `test_chas_matches_chasov` and `test_drone_singular_matches_drones_plural_en` **require** "match on different numbers, same stem" — they would break if I changed `_match_in_text()` to require exact numeric match.

Proper fix is to **split** `_match_in_text()` (search) from `_verify_match()` (verify) — but that's a bigger architectural change than AC#2 deserves in this phase. Defer to **Phase 1.5 (file as new issue #012)**.

This phase therefore closes the two HIGH issues that are unambiguously fixable: #003 (date dedup) and #004 (БПЛА morphology). The numeric-mismatch problem remains OPEN and tracked.

---

## Audit findings (already done before writing tests)

### Bug A — date dedup (CLOSED via P3, no Phase 1 work needed)

**Location:** `src/hermes_deepresearch.py` L372-396 (`_add()`)

```python
# Current _add() dedup (P3 already shipped this)
for existing in seen:
    if phrase in existing and len(existing) > len(phrase):
        return False  # already have a more complete version
    if existing in phrase and len(phrase) > len(existing):
        to_remove.append(existing)
for old in to_remove:
    seen.discard(old)
    try:
        facts.remove(old)
    except ValueError:
        pass
seen.add(phrase)
facts.append(phrase)
```

**Status (2026-06-06, Phase 1 audit):** P3 (plan `2026-06-06_090000-phase-3-fact-extraction.md`, deployed earlier today) already shipped the `to_remove` symmetric dedup. The extraction order in `_extract_facts()` is still "num_ctx first, date second" (L402 then L416), but the symmetric `existing in phrase` branch at L386 catches the "5 июня" → "5 июня 2026" direction correctly.

**Verified manually:**
```python
_extract_facts("Магнитная буря 5 июня 2026: красный уровень опасности. Уровень опасности 5 июня ожидается высоким.")
# → ['5 июня 2026', 'Магнитная буря', 'Уровень опасности']   # "5 июня" NOT present
```

**Tested by existing P3 unit test** `TestPhase3_AC1_DateDedup::test_full_date_kept_partial_date_dropped` (L93-103 of `test_extract_facts.py`) — **green in the 123-passed baseline**.

**ISSUES.md #003** is **stale**: its description (L57 "Dedup отбрасывает новую длинную в пользу уже сохранённой короткой") was written before P3 shipped the symmetric dedup. Will close in T5 with explanation.

### Bug B: БПЛА is in `_MORPH_MAP` as its own stem

**Location:** `src/hermes_deepresearch.py` `_MORPH_MAP` (around L180-220)

```python
# Current
"бпла": "бпла",
"беспилотника": "беспилотник",
"беспилотников": "беспилотник",
"дрона": "дрон",
"дронов": "дрон",
```

When a fact says `"22 БПЛА"` and a source text says `"22 беспилотника"`, both surface forms stem to themselves: `"бпла"` vs `"беспилотник"` — **no match**. The user expects these to be equivalent.

**Fix:** change `"бпла": "бпла"` → `"бпла": "беспилотник"`. Now both surface forms share the stem `"беспилотник"`, and `"22 беспилотника"` matches `"22 БПЛА"` through the existing `num_morph` code path.

### Bug C (DEFERRED to Phase 1.5, new issue #012) — numeric-mismatch not enforced

**Location:** `_match_in_text()` L601-613 (`num_morph` block) — returns `(True, "num_morph", 85)` on stem match even if numbers differ.

**Why deferred:** search-time semantics (current) want this for ranking; verify-time semantics (DR §9 AC#2) want exact match. Existing tests `test_chas_matches_chasov` and `test_drone_singular_matches_drones_plural_en` **lock in** the search-time behavior. Fix requires splitting `_match_in_text()` (search) from a new `_verify_match()` (verify) — bigger refactor than this phase's scope. See "Why this scope" above and `ISSUES.md` #012 (to be created in T5).

**Tracked as new issue #012 — Phase 1.5 candidate.**

---

## Acceptance criteria (1, TDD-first)

| # | Criterion | Test file / function |
|---|---|---|
| AC1 | "22 БПЛА" matches "22 беспилотника" via `_normalize_num_unit` / `_match_in_text` | `tests/test_deepresearch_votes.py::test_bpla_synonym` (regression for #004) |

**Why only 1 test, not 2:** During T0 audit, I discovered that #003 (date dedup) is **already fixed** by Phase 3's `to_remove` symmetric dedup (see `_add()` L382-393 — discards old short phrase, keeps new long phrase). The corresponding unit test `TestPhase3_AC1_DateDedup::test_full_date_kept_partial_date_dropped` (L93-103 of `test_extract_facts.py`) is **already green** in the 123-passed baseline. The smoke 2026-06-06 finding referenced in ISSUES.md #003 was logged **before** P3 shipped its fix; ISSUES.md is stale.

**Plan adjustment:** close #003 in T5 (ISSUES.md update) as "DONE via P3" with no new test, no code edit. The single test+edit in this phase is for #004 (БПЛА morphology).

**Per DR §9 Phase 1 AC#6: "Тесты сначала, patch потом"** — this is the iron law for this phase. No production code edits until `test_bpla_synonym` is red.

---

## Task breakdown (TDD-first)

### T0. Pre-work (~5 min)
- [x] Audit findings written above.
- [x] 5 acceptance criteria listed with test file/function.
- [x] Plan written to `.hermes/plans/`.
- [ ] **Wait for user approval** before T1.

### T1. Red — write 1 failing test
1. Add `test_bpla_synonym` to `tests/test_deepresearch_votes.py`.
2. Run `pytest -q tests/test_deepresearch_votes.py` — expect **1 red, rest green**.
3. **Stop and verify** with user: red is a real missing test, not a typo.

### T2. Green — minimal patch (1 edit, no `_match_in_text()` changes)

**Edit 1: `_MORPH_MAP` (one line)**
```python
"бпла": "беспилотник",   # was: "бпла": "бпла"
```

Run targeted test after edit:
- After edit 1: `test_bpla_synonym` goes green.

### T3. Verify (full suite)
- `python3 -m pytest -q` from repo root — expect 123 + 1 = **124 passed**.
- `python3 -m pytest -q` from `/tmp/$(mktemp -d)` copy + `PYTHONPATH=src` — expect 124 passed (Phase 0 portability gate, no regression).
- `python3 -m ruff check tests/ src/` — expect no new errors (only pre-existing #008 still flagged).

### T4. Security review (per-phase, ~10 min)
- Write `.hermes/plans/2026-06-06_<HHMM>-phase-1-numeric-mismatch-security-review.md`.
- POSITIVE: closes #003 (date dedup) and #004 (БПЛА morphology) — both HIGH-severity.
- POSITIVE: БПЛА↔беспилотник cross-stem equivalence fixes #004 without breaking any other test.
- RESIDUAL: LLM per-fact error aggregation (DR §Phase 1 AC#3, #4) still OPEN — defer to next phase.
- RESIDUAL: entity extraction stop verbs (DR §Phase 1 AC#5) — no change needed, document as confirmed.
- RESIDUAL: numeric-mismatch verification (DR §Phase 1 AC#2) — deferred to Phase 1.5 as new issue #012. Document the design conflict and the chosen mitigation (split search/verify paths).
- NOT CHANGED: Phase 2-6 work untouched (per "out of scope").
- Acceptance criteria checklist: 1/1 PASS.

### T5. ISSUES.md update
- #003 → **DONE 2026-06-06** (close as "closed via P3, symmetric dedup already shipped in `_add()` L382-393"; ISSUES.md L57 description was stale). No new test (P3 unit test `test_full_date_kept_partial_date_dropped` already covers the case).
- #004 → **DONE 2026-06-06** (close with link to phase plan + tests).
- **#012 → OPEN 2026-06-06 (new)** — `_match_in_text()` does not enforce exact numeric match (returns match=True with score=85 on stem alignment even if numbers differ). Search-time semantics OK for ranking; verify-time semantics wrong. Fix: split `_match_in_text()` (search) from new `_verify_match()` (verify). Tracked for Phase 1.5.
- Smoke test history: add entry for post-Phase 1 portable verify (124/124 in 3 modes).

### T6. Memory update
- Update the project-status memory entry:
  - "Phase 1 (БПЛА morphology) DONE 2026-06-06, 124/124 portable, #004 CLOSED (#003 closed-via-P3)"
  - "PENDING: #012 (Phase 1.5, search/verify split for numeric-mismatch) / Phase 2 (ranking) / ..."
  - Keep entry ≤ 900 chars total (2200 char budget minus profile etc.).

---

## Verification (not commits)

This project is not a git repository. Each task is verified via:

```bash
cd /opt/searxng && python3 -m pytest -q
```

The pytest output (exit code + summary line) is the per-task gate. No `git add`, no `git commit`. Track progress in a TodoList instead.

**Phase 0 portable gate (re-run at T3):**

```bash
# Mode 1: repo root
cd /opt/searxng && python3 -m pytest -q

# Mode 2: clean /tmp + PYTHONPATH
tmp=$(mktemp -d) && cp -a /opt/searxng "$tmp/project" && cd "$tmp/project" && \
  PYTHONPATH=src python3 -m pytest -q

# Mode 3: clean /tmp + env -i
cd "$tmp/project" && env -i PATH="$PATH" HOME="$PWD/.tmp-home" PYTHONPATH=src \
  python3 -m pytest -q
rm -rf "$tmp"
```

All three must show **124 passed**.

---

## Risks and rollback

| Risk | Likelihood | Mitigation | Rollback |
|---|---|---|---|
| Edit 1 (`бпла` → `беспилотник`) changes `_MORPH_MAP` for other code paths | LOW | `grep` for `MORPH_MAP` consumers; ensure no other path depends on `бпла` ≠ `беспилотник` | Revert the one line |
| Edit 2a (regex order swap) breaks some existing test | LOW | P3 plan already verified date-dedup via 93 → 117 → 123; baseline is solid | Restore the original order; investigate the new red |
| Edit 2b (symmetric dedup) causes infinite loop or eviction of valid facts | LOW | The check `existing in phrase and len(phrase) > len(existing)` only triggers when the new phrase is a strict extension; copy `seen` to `list(seen)` to avoid mutation during iteration | Revert the 4 lines |

**Overall risk:** LOW. The 2 edits are all small, surgical, and verifiable. **No `_match_in_text()` changes** (Bug C deferred to Phase 1.5), so existing tests `test_chas_matches_chasov` and `test_drone_singular_matches_drones_plural_en` continue to pass.

---

## Estimated effort

- T0: 5 min (done)
- T1: 10 min (write 3 tests, verify red)
- T2: 8 min (2 surgical edits, verify green)
- T3: 5 min (full portable verify)
- T4: 8 min (security review writeup, smaller because Bug C deferred)
- T5: 5 min (ISSUES.md, including new #012)
- T6: 3 min (memory update)

**Total: ~35 min** from approval to DONE (down from 55 min in v1 plan, due to scope reduction).

---

## Related

- DR-06062026.txt §"Phase 1 — correctness hotfix" and §9 Phase 1 acceptance criteria
- ISSUES.md #003 (date dedup) and #004 (БПЛА morphology)
- `~/.hermes/skills/senior-python-prod/SKILL.md` P5 (multi-word entity regex) — adjacent lesson
- `~/.hermes/skills/portable-test-engineering/SKILL.md` (skill I just created) — for the 3-mode verification gate
- `.hermes/plans/2026-06-06_090000-phase-3-fact-extraction.md` — predecessor plan, established the `_MORPH_MAP` infrastructure this phase builds on
