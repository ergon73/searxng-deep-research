# Issues Ledger — searxng-deep-research v0.8.x

> **Single source of truth для issues / regressions / known gaps.**
> Обновляется по ходу каждой фазы и при каждом smoke-тесте.
> Формат: ID | Severity | Status | Date | Title | Plan → Fix

## Status legend

- **DONE** — закрыто (есть fix + tests зелёные)
- **OPEN** — открыто (есть план, не сделано)
- **DEFERRED** — отложено в конкретную будущую фазу
- **WONTFIX** — решено не чинить (out of scope, ROI low)
- **PARTIAL** — частично сделано, остаток tracked

---

## Index по severity

### CRITICAL (must fix)
*(none open)*

### HIGH (should fix in current/next phase)
- **#003** — `OPEN` — Date dedup regression в smoke (P3 partial)
- **#004** — `OPEN` — БПЛА ↔ беспилотник morphology gap (P3.1/P5)

### MEDIUM (worth fixing, low urgency)
- **#001** — `DEFERRED→P5` — top-1 selection использует content heuristic, не search_votes
- **#002** — `DEFERRED→P5` — `_looks_like_news()` не реализован (news routing)
- **#005** — `PARTIAL` — structure synced (Phase 0); docs part still open (deferred P8)
- **#006** — `DEFERRED→P6` — proxy not actually wired (P0.5 из DR)
- **#013** — `OPEN 2026-06-06` — `reformulate()` broken (returns None for 100% queries; 20/20 baseline + 13/13 long-query tests). EN-fallback для RU потерян.
- **#014** — `MITIGATED 2026-06-06` — long-query degradation. Было: median top-1 score 0.48 для 200-400w запросов vs 1.0 для short (50% drop), sub-aspect coverage 0% для multi-aspect, 1/10 long queries возвращал 0 sources. **Mitigated via query-adaptation skill (v1.0.0) + alt_queries параметр в deep_research()**: 3-query re-eval показал L1 +0.39, L8 +0.83 (unblocked 0→0.83), L4 -0.13 (extractor issue, см. #015). Net: 2/3 улучшились, 1/3 ухудшилась (edge case), 1/3 unblocked. **NOT FULLY CLOSED** — нужны v1.0.1 улучшения (narrative entity filtering, LLM fallback).
- **#015** — `OPEN 2026-06-06` — narrative entity filtering. Запрос L4 ("стартап из 5 человек, ... Gemma 4 12B, Phi-4 Mini, ...") — extractor поднял "5 человек" как entity (top score выше чем "Gemma 4 12B"), в результате main_query = "Gemma 4 5 человек" — бессмысленно. **Нужен**: фильтр чисел в narrative context (не коды моделей), PoS-tagging (light), или LLM-based entity filtering. Low priority (1/3 edge case), но ухудшает average.
- **#028** — `DEFERRED→D2 2026-06-14` — duplicate `[doc_<int>:<int>-<int>]` regex. `_SPAN_MARKER_RE` (in `src/synthesis.py`) and `_CITATION_RE` (in `src/citations.py`) are two regexes for the same shape. Consolidate to one in a single module. Cosmetic / drift risk, no runtime impact. Surfaced by v0.8.3-D0 audit. Tracked in `RELEASE_NOTES_v0.8.3.md` P2 section.
- **#029** — `DEFERRED→D2 2026-06-14` — centralize `[doc_N:start-end]` formatting. Three code paths emit the same `f"[doc_{N}:{start}-{end}]"` format string: `_build_inline_span_markers` / `_build_contradiction_markers` (in `src/research_runner.py`) and `format_cited_claim` (in `src/citations.py`). Extract a single `format_span_marker(doc_index, start, end)` helper. Drift risk if any one is updated and the others aren't. Surfaced by v0.8.3-D0 audit. Tracked in `RELEASE_NOTES_v0.8.3.md` P2 section.
- **#030** — `DEFERRED→D2 2026-06-14` — `Citation.source_index` decision. Field is defined and populated (= `id - 1`, 0-based index in dedup'd `source_candidates`) in `src/synthesis.py::Citation`, but **not** exported in `to_dict()`. Decision needed: remove (dead) or wire it (expose for `eval.py` / downstream consumers). Surfaced by v0.8.3-D0 audit. Tracked in `RELEASE_NOTES_v0.8.3.md` P2 section. **Plus** a cosmetic docstring typo: `_build_contradiction_markers` docstring has a `f"[doc_{i}:{start-end}]"` placeholder (double hyphen, not a real `f-string` template); no runtime impact, just stale documentation after the C3 batch.
- **#031** — `DEFERRED→D2 2026-06-14` — use `_SPAN_MARKER_RE` instead of raw `"[doc_"` detection. The D1 `has_span_markers` flag in `_render_user_markdown` uses `any("[doc_" in b for b in ...)`. A safer test would use the validated regex (`_SPAN_MARKER_RE.search`) to avoid false positives on prose that happens to contain `[doc_` (e.g. in a quote or a `[doc_` URL parameter). Tracked in `RELEASE_NOTES_v0.8.3.md` P2 section.

### LOW (informational)
- **#007** — `WONTFIX` — `meta["engines"]` counting `""` as engine (pre-existing)
- **#008** — `DONE 2026-06-09` — ruff pre-existing style (S310/UP045/I001/UP041/S202/E402/etc). Was WONTFIX (31 errors). Closed in v0.8.1.2 commit `72f8b16` (`ruff check --fix` + `--unsafe-fixes` + `ruff format src scripts` + per-file-ignores): 207→0 errors total. Final per-file-ignores (v0.8.1.3, current in `pyproject.toml`): `tests/*` = `["S101", "S105", "S106", "S108", "E402"]`; `scripts/e2e_*.py` = `["S108"]`; 3× `src/*.py` = `["S310"]`. Shrunk from blanket `["S", "B", "E402"]` to surgical per-rule per v0.8.1.3 hygiene review.
- **#011** — `DONE 2026-06-06` — test suite not portable (hardcoded `/opt/searxng/src` in conftest, DNS-dependent `test_url_safety`, broken `.env_llm.example` syntax). Fixed via Phase 0 + new skill `portable-test-engineering`.

---

## Detail

### #003 — Date dedup regression в smoke [HIGH | OPEN | 2026-06-06]

**Что:** P3 plan acceptance criterion "5 июня 2026 not duplicated as 5 июня" — **PASS в unit-test**, **FAIL в end-to-end smoke** 2026-06-06.

**Симптом:**
- Top-1 текст "Магнитная буря 5 июня 2026: красный уровень опасности" 
- `_extract_facts()` возвращает: `['5 июня', '5 июня 2026', ...]` — оба присутствуют
- Dedup в `_add()` отбрасывает **новую длинную** фразу в пользу уже сохранённой короткой:
  ```python
  # L367-369 hermes_deepresearch.py
  for existing in seen:
      if phrase in existing and len(existing) > len(phrase):
          return False  # уже есть более полная версия
  ```
  Тут `phrase="5 июня 2026"`, `existing="5 июня"`, "5 июня" in "5 июня 2026" = True, len(11) > len(6) = True → return False (drop).

**Причина:** number-context regex `r"\b(\d{1,4})\s+([а-яёa-z]{3,})\b"` срабатывает на "5 июня" первым (в тексте есть "5 июня 2026" и просто "5 июня"). `FACT_RE_DATE` потом пытается добавить "5 июня 2026" — но dedup ловит "5 июня" в "5 июня 2026" и отбрасывает новую. **Эффект: partial "5 июня" остаётся, full "5 июня 2026" теряется**.

**Reverse-substring branch** (мой patch в P3) удаляет старый короткий при появлении нового длинного, но он смотрит в другую сторону — `existing in phrase` (а не `phrase in existing`).

**Impact:** `verification_rate` падает, шум в `verification_details`. User-facing: top-1 имеет 10 facts (5 чисел-из-контекста + 1 длинный entity + 4 частично пересекающихся). 

**Решение (для P3.1 / перед P5):** в `_add()` для number-context и date — приоритет: date → number-context. То есть **поменять порядок экстракции** (dates first), а number-context не трогает то, что уже в seen как date.

Альтернативно: regex `fact_re_num_ctx` с negative lookahead для года: `r"\b(\d{1,4})\s+([а-яёa-z]{3,})(?!\s+\d{4})\b"` — не матчит "5 июня" если за ним сразу `\s+\d{4}`.

**Plan:** будет исправлено в начале P5 (ranking/cleanup) или отдельной P3.1.

---

### #004 — БПЛА ↔ беспилотник morphology gap [HIGH | OPEN | 2026-06-06]

**Что:** `_MORPH_MAP` имеет:
```python
"дрона": "дрон", "дронов": "дрон", ...
"беспилотника": "беспилотник", "беспилотников": "беспилотник", ...
"бпла": "бпла",  # ← но не связан с "беспилотник"!
```

**Симптом (smoke 2026-06-06):**
```
'22 БПЛА' ~ '...уничтожено 22 беспилотника' → matched=False score=37
```

**Решение:** добавить в `_MORPH_MAP`:
```python
"бпла": "беспилотник",
"беспилотник": "беспилотник",
"беспилотника": "беспилотник",
...
```

Или сделать synonym dict: `{"БПЛА": ["беспилотник", "дрон", "БПЛА"]}` (уже частично в `SYNONYM_DICT`, но не покрывает "БПЛА"→"беспилотник" в num_morph контексте).

**Plan:** будет исправлено в P5 (как часть ranking cleanup / `categories=news`).

---

### #001 — top-1 selection ranking [MEDIUM | DEFERRED→P5 | 2026-06-06]

**Что:** `source_score` для top-1 считается через `_confidence()` (content length + keyword), а не через `_combined_source_score(search_score, content_score, search_votes)`.

**Симптом (smoke 2026-06-06):** запрос про антидрон → top-1 = "Магнитная буря 5 июня" с presearch (score 0.92, votes 2), а правильный ответ (myseldon про дронов с 22 БПЛА / 86 дронов) на 2-м месте (score 0.75).

**Root cause:** P5 deferred — DR §5 рекомендует `_combined_source_score = 0.45*search_score + 0.40*content_score + 0.15*vote_score`.

**Plan:** DR §Phase 5.

---

### #002 — news routing not implemented [MEDIUM | DEFERRED→P5 | 2026-06-06]

**Что:** `_looks_like_news()` отсутствует. `deep_research()` всегда вызывает `web_search()`, не `news_search()`. Для `time_range=day` нужен `categories=news`.

**Симптом:** прессеarch выдаёт общие результаты про "уровень опасности" (магнитная буря попала в top-1 потому что слово "опасность" в её заголовке).

**Plan:** DR §Phase 5.

---

### #005 — docs stale [PARTIAL | 2026-06-06]
- **STRUCTURE PART — CLOSED 2026-06-06** (Phase 0): repo now matches AGENTS.md — `config/docker-compose.yml` + `config/settings.yml` + `src/hermes_*.py` + `src/llm_verifier.py`. `tests/conftest.py` derives `sys.path` from `Path(__file__).parents[1]`, no hardcoded `/opt`. `pyproject.toml` has `pythonpath = ["src"]`.
- **DOCS PART — STILL OPEN** (deferred to P8): `ARCHITECTURE.md` still references v0.7.3 + "94% → ~96% rate" (real AVG ~33%); `INSTALL.md` still has `privaccheck`→`privac...eck` mangling + `secret_key: "СЮДА..."` placeholder; no `.gitignore` exists.

---

### #006 — proxy not wired [MEDIUM | DEFERRED→P6 | 2026-06-06]

**Что:** DR P0.5 — `.env_proxy` существует, но `settings.yml` нет `outgoing.proxies:` и compose env не проксирует.

**Plan:** DR §Phase 6 (proxy integration).

---

### #007 — `meta["engines"]` counting `""` as engine [LOW | WONTFIX | pre-existing]

**Что:** Если `web_search` возвращает результат без `engine` поля, код добавляет `""` (empty string) в `meta["engines"]`, что увеличивает `len(meta["engines"])` на 1.

**Решение:** фильтровать `""` в `meta["engines"].add(...)`. Но это P5 territory (ranking cleanup). **Won't fix** в P1-P4.

---

### #008 — ruff pre-existing style [LOW | DONE | 2026-06-09, ref: `72f8b16`]

**Что было:** 15 ruff errors в `src/hermes_deepresearch.py` (S310 urlopen без scheme check, UP045 `Optional[X]` вместо `X | None`, I001 import sort, W605 escape sequence в docstring), 16 в `src/llm_verifier.py` (те же + UP041 socket.timeout). Pre-existing до P1. **Plus 176 more в v0.8.1.2 sweep** (full repo audit выявил кумулятивный drift: B006, S110/S112, E402, S105, S202, и т.д.).

**Что сделано (v0.8.1.2 commit `72f8b16`):**
- `ruff check --fix --unsafe-fixes` — 184 auto-fixed (mechanical)
- `ruff format src scripts` — 22 reformatted (mechanical; tests оставлены per user choice)
- 3 surgical `# noqa` в `scripts/eval.py`, `src/query_adaptation.py`, `src/release_packaging.py`
- 1 rename: `_SECRET_KEY_NAMES` → `_SECRET_KEY_NAME_PATTERNS` в `src/redact.py` (имя вводило в заблуждение — там regex patterns, не credentials)
- 5 файлов скопированы в `/opt/searxng/` для prod smoke
- Per-file-ignores в `pyproject.toml` (current v0.8.1.3):
  - `tests/*` = `["S101", "S105", "S106", "S108", "E402"]` (shrunk from blanket `["S", "B", "E402"]`)
  - `scripts/e2e_*.py` = `["S108"]` (для `/tmp` smoke traces)
  - 3× `src/*.py` (`hermes_*.py`, `llm_verifier.py`) = `["S310"]` (S310 false-positive validated by `tests/test_url_safety.py`)

**Результат:** 207 → 0 errors. `ruff check src tests scripts` = clean. CI green на `72f8b16` (run `27198068324`).

**Acceptance:** reviewer (ChatGPT audit 2026-06-09) подтвердил, что per-file-ignores в `pyproject.toml` + ISSUES.md L37 синхронизированы.

### #011 — test suite not portable [DONE | 2026-06-06]

**Что было:** 2026-06-06 GPT-5.5 Pro ревью архива v0.8.2 показало, что test suite воспроизводится **только** на авторском VPS:
- `tests/conftest.py` hardcoded `sys.path.insert(0, "/opt/searxng/src")` — fails on clean machine with `ModuleNotFoundError: No module named 'hermes_deepresearch'`.
- `tests/test_url_safety.py::test_allows_public_sites` лезет в реальный DNS для `example.com` / `google.com` / `github.com` — fails на машине без DNS или в CI без сети.
- `.env_llm.example` имел невалидный env-file syntax (`LLM_API_KEY=*** File permissions: chmod 600` — 2 значения на одной строке), не парсился.
- `test_compose_config.py` ссылался на старые пути (`docker-compose.yml` в root, `searxng/settings.yml`) — drift из-за недоделанной структурной миграции.
- 4 из 8 acceptance criteria GPT-5.5 рекомендации про test portability были **невыполнимы** в текущем виде.

**Что сделано (Phase 0 + new skill):**
- Реорганизация repo: `config/docker-compose.yml` + `config/settings.yml` + `src/hermes_*.py` + `src/llm_verifier.py` (структура матчит `AGENTS.md`).
- `tests/conftest.py` → `Path(__file__).resolve().parents[1]` (portable, no hardcoded paths).
- `pyproject.toml` → `[tool.pytest.ini_options] pythonpath = ["src"]` (single source of truth).
- `tests/test_url_safety.py` → заменены DNS-dependent hostnames на public IP literals (`93.184.216.34`, `1.1.1.1`, `142.251.46.110`, `140.82.112.3`); добавлены 2 monkeypatch теста для DNS failure + DNS rebinding defence.
- `config/.env_llm.example` → переписан как валидный env-file с placeholder values.
- `tests/test_compose_config.py` → обновлены пути на `config/`, добавлено 4 новых теста на `.env_llm.example` (exists, valid syntax, contains LLM_API_KEY, no real-looking keys).
- Создан skill `portable-test-engineering` v1.0.0 (software-development/) с hard rules, anti-patterns, verification checklist, 3-mode pre-commit gate.

**Verification (3-mode gate, all green):**
```
[repo root]                    pytest -q                                          → 123 passed
[/tmp/$(mktemp -d) + PYTHONPATH=src]  pytest -q                                  → 123 passed
[/tmp/$(mktemp -d) + env -i PATH HOME PYTHONPATH=src]  pytest -q              → 123 passed
```

**Diff stats:** +6 файлов изменено, +4 теста, 0 regressions. `hermes prompt-size` delta: +218 B (1.1% system prompt).

**Related:**
- `~/.hermes/skills/software-development/portable-test-engineering/SKILL.md` — full policy.
- `/opt/searxng/.hermes/plans/2026-06-05_172617-phase-1-runtime-config.md` — original Phase 1 plan (P1).
- DR-06062026.txt §8 — GPT-5.5 Pro Phase 0 reproducibility gate (this issue is the resolution).

---

## Smoke test history

### 2026-06-06 (post-Phase 0): portable test gate

- ✅ 123/123 tests pass in repo root (`/opt/searxng`)
- ✅ 123/123 tests pass in `/tmp/$(mktemp -d)` copy + `PYTHONPATH=src`
- ✅ 123/123 tests pass in `/tmp/$(mktemp -d)` copy + `env -i PATH HOME PYTHONPATH=src`
- ✅ `hermes prompt-size` baseline: 20,090 B → 20,308 B (+218 B for `skill-autodiscovery-controlled` skill index entry)
- ✅ Bundle `skill-maintenance` created (5 skills)
- ✅ `hermes skills list` shows `skill-autodiscovery-controlled` enabled under `meta/` category

**Phase 0 reproducibility gate — CLOSED.**

### 2026-06-06: deep_research query "уровень антидроновой опасности в Москве 6 июня 2026"

- ✅ P1: SearXNG healthy, 5 sources from 3 engines (presearch, duckduckgo, google)
- ✅ P2: search_votes=2 per source, found_by_engines populated
- ✅ P3: numeric morphology (`method=num_morph`) ловит "4 июня" + "5 июня 2026"
- ✅ P3: multi-word entity extraction работает ("Метеозависимых людей ждёт тяжёлый")
- ✅ P4: llm_enhanced=True, llm_error=None (реальный OpenRouter вызов с .env_llm прошёл)
- ❌ #001: top-1 = магнитная буря (presearch), не дроны — **DEFERRED→P5**
- ❌ #002: news routing отсутствует — **DEFERRED→P5**
- ❌ #003: date dedup regression ("5 июня" остаётся, "5 июня 2026" теряется) — **OPEN**
- ❌ #004: "22 БПЛА" не матчит "22 беспилотника" (БПЛА↔беспилотник gap) — **OPEN**

**Команда:** `python3 /tmp/smoke_deepresearch.py` (полный output в этой сессии)

---

# Phase 0 release hygiene (v0.8.0) — DONE 2026-06-08

External review (`/tmp/hermes-recomendation-08062026.txt`, ChatGPT) highlighted repo
hygiene gaps. Closed in v0.8.0, no research logic change.

| Item | Status | Verification |
|---|---|---|
| README references non-existent `claim_modeling.py` | ✅ FIXED | `grep -r claim_modeling README.md` → 0 hits |
| README says "private repository" (now public) | ✅ FIXED | line 41 updated |
| `pyproject.toml` `Source` URL = `github.com/example/...` | ✅ FIXED | → `ergon73/searxng-deep-research` |
| `pyproject.toml` version = 0.8.1 (mismatch with README v0.8) | ✅ FIXED | → 0.8.0 |
| ARCHITECTURE.md says v0.7.3 / "94% rate" / "94%→96%" | ✅ FIXED | honest baseline only, v0.8.0 |
| ARCHITECTURE.md references `/opt/searxng/DR-...` absolute path | ✅ FIXED | relative + "not in repo" note |
| `scripts/{e2e_falcon9,e2e_smoke_llm,eval}.py` hardcoded `/opt/searxng/src` | ✅ FIXED | portable `Path(__file__).resolve().parents[1] / "src"` |
| `config/docker-compose.yml` default volume `./searxng/settings.yml` (wrong) | ✅ FIXED | → `./settings.yml` |
| `config/.env.example` missing | ✅ FIXED | new file with SEARXNG_SECRET + SEARXNG_SETTINGS_PATH |
| `scripts/eval.py` QS penalises `needs_confirmation=True` | ✅ FIXED | weights redistributed (0.45/0.22/0.22/0.11), no_confirmation diagnostic only |
| `config/.env_llm.example` references `/opt/searxng/.env_llm` absolute path | ✅ FIXED | relative + scoped to LLM only |
| `config/.env_proxy.example` references `llm_verifier.py` (wrong) | ✅ FIXED | scoped to docker-compose `env_file` only |
| `.gitignore` excludes `config/.env.example` (false positive on `.env.*` rule) | ✅ FIXED | added `!.env.example` |
| `.gitignore` did not exclude `.git-credentials-store`, `.ssh/`, `.gnupg/` | ✅ FIXED | added |
| `PYTHONPATH=src python3 -m pytest -q` | ✅ 404 passed in 25.35s |

**Commits / PRs:** single commit `release-hygiene: v0.8.0 phase 0` on `ergon73/searxng-deep-research`.

---

# Backlog from 8 June 2026 external review (DEFERRED, no implementation timeline)

ChatGPT review `/tmp/hermes-recomendation-08062026.txt` suggested several
v0.9+ items. Tracked here as `DEFERRED`, not open — we do not have a commitment
to implement them and they are not blocking v0.8.x.

| Topic | Status | Notes |
|---|---|---|
| **#016** — `PARTIAL→DONE 2026-06-08` Typed `ResearchState` | ✅ PARTIAL DONE | Implemented: `SearchTask`, `Claim`, `ResearchState` (mutable container), reuse `EvidenceWindow` from `evidence.py`. **NOT done**: `SearchHit`, `Document`, `ClaimVerdict`, `ResearchPlan`, `ResearchReport` (5 deferred — no consumer yet, dicts are fine at this scale). Tests: `tests/test_models.py` (15 cases, all pass). |
| **#017** — `PARTIAL→DONE 2026-06-08` `planner.py::build_research_plan()` | ✅ DONE | Composes `adapt_query()` (main+alts) + `classify_intent()` (variants) into typed `SearchTask` list (priorities 100/80/70/40). Falsification tasks (priority 40) added only for `news`/`security`/`product`/`technical` routes. Confirmation gate: True if EITHER `adapted.needs_confirmation` OR `intent.routing_warning`. Pure function — no network, no LLM, no fetch. Tests: `tests/test_planner.py` (27 cases, all pass). |
| **#018** — `PARTIAL→DONE 2026-06-08` `research_runner.py::deep_research_v2()` | ✅ DONE | Strangler refactor: new `run_research()` / `deep_research_v2()` composes `planner` → `web_search` → `fetch_url` → `verify_sources` → `synthesize` → `review`. Confirmation gate honoured: returns `status="needs_confirmation"` if `plan.needs_confirmation` and not `approved_plan`. Legacy `deep_research()` NOT modified (test verifies signature unchanged). Typed `ResearchResult` with `to_dict()` JSON serialisable. Tests: `tests/test_research_runner.py` (31 cases, all pass, monkeypatched network). Live smoke verified: 5 scenarios incl. confirmation gate, error handling, alias. |
| **#019** — `DONE 2026-06-08` Span-level citations (Claim→EvidenceWindow→[N]) | ✅ DONE | Phase 4 of external plan. New `src/citations.py` (~9KB): `find_span()` (direct / whitespace-normalized / fuzzy-prefix substring search, stdlib-only), `build_evidence_window()` (attaches `EvidenceWindow` with `offset_start`/`offset_end` + `source_url`/`source_title`/`score` to a `Claim`), `format_cited_claim()` (emits `[doc_N:start-end]` markers, parseable via `_CITATION_RE`), `citation_stats()` (coverage / non-stub coverage), `assert_citations_complete()` (invariant enforcer). `Claim` and `EvidenceWindow` extended (backward-compat: new fields with defaults). `EvidenceWindow` gains `source_url`/`source_title`/`score`; `Claim` gains `is_stub` (placeholder flag, exempt from invariant) and `evidence_window` (frozen-dataclass; set via `dataclasses.replace`). Runner integration: `_extract_typed_claims_with_citations()` runs alongside legacy string extraction (synthesis still gets strings for backward compat). After synthesis, runner decorates `synth.coverage` with `citation_stats` / `inline_citations` / `unverified_claims`. 38 new unit tests in `test_citations.py` + 8 integration tests in `test_research_runner.py::TestRunnerSpanCitations`. Live smoke (offline): 4/4 claims cited at 100% coverage with correct `[doc_0:N-M]` markers. |
| **#020** — `PARTIAL→DONE 2026-06-08` `gap_analysis.py` + iterative deepening | ✅ DONE | New `src/gap_analysis.py`: pure stdlib `analyze_gaps(state)` detects 6 gap kinds (too_few_sources, no_search_results, low_source_diversity, too_many_unsupported_claims, contradictions_unresolved, low_confidence). `gaps_to_search_tasks()` maps to priority-50 retry tasks. Runner loop (Phase 3) now runs gap analysis after each pass; if gaps exist and `max_iterations` not reached, adds gap-fill tasks and continues. Early-exit when no gaps (don't waste iterations). Thresholds: `MIN_DOCUMENTS=3`, `MIN_UNIQUE_DOMAINS=2`, `MIN_TOP1_CONFIDENCE=0.5`, `MAX_UNSUPPORTED_CLAIM_RATIO=0.4` — all in one place for tuning. Tests: `tests/test_gap_analysis.py` (40 cases incl. 4 runner-integration tests verifying iteration count, gap-fill queries, max_iterations cap). |
| **#021** — `DEFERRED` Falsification tasks (`<query> criticism`, `debunked`, `опровержение`) | backlog | Phase 2 — but LLM-conditional dependent, low ROI on rule-based |
| **#022** — `WONTFIX` Tavily/Exa/Firecrawl provider interface | rejected | Premature: 1 provider (SearXNG), no need to abstract |
| **#023** — `WONTFIX` Qdrant/Neo4j | rejected | Premature: no claim/evidence model yet |
| **#024** — `DEFERRED` LangGraph adapter | backlog | Only if checkpointing / human-in-the-loop / fault tolerance needed |
| **#025** — `WONTFIX` Split eval into 5 separate evals (A/B/C/D/E) | rejected | Over-engineering for 1 metric, low ROI |
| **#026** — `WONTFIX` `use_default_settings.engines.keep_only` rewrite | rejected | Current explicit engine list works, no reproducibility issue observed |
| **#027** — `WONTFIX` Multi-agent roles in separate processes | rejected | Functions in one process are sufficient at this scale |

---

## v0.8.3 release-prep P2 detail (audit-surfaced 2026-06-14)

These four P2 items were surfaced by the v0.8.3-D0 audit and
tracked here per project convention. None of them were started in
v0.8.3; all are deferred to a future `D2` doc-cleanup batch.
Cross-referenced in `RELEASE_NOTES_v0.8.3.md`.

### #028 — duplicate `[doc_<int>:<int>-<int>]` regex [MEDIUM | DEFERRED→D2 | 2026-06-14]

**Что:** Two regexes for the same shape:

- `src/synthesis.py` defines `_SPAN_MARKER_RE` for
  `[doc_<int>:<int>-<int>]` (validates inline span markers
  appended to confirmed / contradiction bullets).
- `src/citations.py` defines `_CITATION_RE` for the same
  pattern (used by `format_cited_claim` and downstream
  consumers of `coverage["inline_citations"]`).

**Зачем:** Drift risk. If the format ever needs to change
(e.g. add a `method` field, or extend the offsets to UTF-8
codepoints instead of chars), only one of the two regexes
might get updated, and the other path would silently fail to
match.

**Fix sketch:** Consolidate into a single
`SPAN_MARKER_RE = re.compile(r"\[doc_(\d+):(\d+)-(\d+)\]")`
in `src/citations.py` (the older module), and have
`src/synthesis.py` import it. The synthesis module
specifically avoids importing `citations.py` per a pinned
C1 contract; the consolidation may need a one-line
"forwarder" in `synthesis.py` that re-exports the regex
under the existing name `_SPAN_MARKER_RE`.

### #029 — centralize `[doc_N:start-end]` formatting [MEDIUM | DEFERRED→D2 | 2026-06-14]

**Что:** Three code paths emit the same format string
`f"[doc_{N}:{start}-{end}]"`:

1. `_build_inline_span_markers` in
   `src/research_runner.py` (C1 path).
2. `_build_contradiction_markers` in
   `src/research_runner.py` (C3 path).
3. `format_cited_claim` in `src/citations.py`
   (provenance helper used by `coverage["inline_citations"]`).

**Зачем:** Same drift risk as #028. Plus, the current
naming of the parameter is inconsistent:
`_build_*` helpers call it `doc_index`,
`format_cited_claim` calls it `doc_index` too — lucky
consistency, but easy to break.

**Fix sketch:** Extract a single helper, e.g.
`format_span_marker(doc_index: int, start: int, end: int) -> str`
in `src/citations.py` (where the format is canonically
defined), and have all three callers use it. Tests assert
the literal `[doc_<int>:<int>-<int>]` shape; the helper
keeps that contract.

### #030 — `Citation.source_index` decision + docstring typo [MEDIUM | DEFERRED→D2 | 2026-06-14]

**Что (1):** `src/synthesis.py::Citation` defines
`source_index: int` and populates it as `i - 1` (0-based
index in dedup'd `source_candidates`) inside
`_build_citation_table`. The field is **not** exported in
`Citation.to_dict()` (which only ships `id, url, title,
quote`). The field is also never read by any code in
`src/` or `tests/`.

**Зачем:** Either it's dead code (the dataclass carries an
unused field) or it's reserved for a future consumer
(`eval.py`, an LLM-prompt assembler, an audit export). The
audit cannot tell which without a code-author signal.

**Decision needed:** remove (dead) or wire (expose in
`to_dict()` and document a consumer).

**Что (2) — bonus:** `_build_contradiction_markers`
docstring (in `src/research_runner.py`) has a
`f"[doc_{i}:{start-end}]"` placeholder — the `{start-end}`
is a **double hyphen**, not a real `f-string` template.
Cosmetic, no runtime impact. The v0.8.3 series tracked
this as a P2 (do not create a C3c). Will be cleaned up in
the same D2 batch as a one-line docstring fix.

### #031 — use `_SPAN_MARKER_RE` instead of raw `"[doc_"` detection [MEDIUM | DEFERRED→D2 | 2026-06-14]

**Что:** The D1 `has_span_markers` flag in
`_render_user_markdown` (in `src/synthesis.py`) uses
`any("[doc_" in b for b in (confirmed + contradiction_bullets))`.
That's a substring check, not a regex match.

**Зачем:** If a bullet ever contains the literal `[doc_` in
prose (e.g. a quote that includes a URL parameter, or a
URL fragment with the literal text `[doc_`), the
substring check would falsely report a span marker and
trigger the provenance note. The validated regex
(`_SPAN_MARKER_RE.search(b) is not None`) is strictly
safer and matches the validation logic used in
`_render_user_markdown` itself for inline span markers
(line ~730 and line ~760).

**Fix sketch:** Replace the substring check with a
regex-based check, e.g.
`has_span_markers = any(_SPAN_MARKER_RE.search(b) for b in confirmed) or any(_SPAN_MARKER_RE.search(b) for b in contradiction_bullets)`.
The regex is already imported in `synthesis.py`; no new
imports. Test: add a unit test that puts `[doc_` in a
quote (not a marker) and verifies the note is still
suppressed — and another that puts a real
`[doc_<int>:<int>-<int>]` in a quote and verifies the note
**is** appended (because the regex matches the marker, not
the quote prose). This last case is the gap the current
substring check has.

---

## Verification commands (replay any time)

```bash
cd /opt/searxng
python3 -m pytest -q                          # 648 passed (post-v0.8.1.2)
python3 -m ruff check tests/                  # clean
python3 -m ruff check src/                    # clean (post-v0.8.1.2 ruff cleanup; per-file-ignores documented in pyproject.toml)
docker compose -f config/docker-compose.yml config  # OK
ls -la /opt/searxng/.env_llm                  # mode 0o600

# Phase 0 portability gate (3 modes, all must pass)
python3 -m pytest -q                                                            # repo root
tmp=$(mktemp -d) && cp -a . "$tmp/project" && cd "$tmp/project" && \
  PYTHONPATH=src python3 -m pytest -q                                            # /tmp + PYTHONPATH
cd "$tmp/project" && env -i PATH="$PATH" HOME="$$PWD/.tmp-home" PYTHONPATH=src \
  python3 -m pytest -q                                                            # /tmp + env -i
rm -rf "$tmp"
```
