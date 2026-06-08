# Issues Ledger — deep-research-project v0.8.x

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

### LOW (informational)
- **#007** — `WONTFIX` — `meta["engines"]` counting `""` as engine (pre-existing)
- **#008** — `WONTFIX` — ruff pre-existing style (S310, UP045, I001, UP041) в `src/hermes_deepresearch.py` (15 ошибок) и `src/llm_verifier.py` (16 ошибок) — не от наших фаз
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

### #008 — ruff pre-existing style [LOW | WONTFIX | pre-existing]

**Что:** 15 ruff errors в `src/hermes_deepresearch.py` (S310 urlopen без scheme check, UP045 `Optional[X]` вместо `X | None`, I001 import sort, W605 escape sequence в docstring), 16 в `src/llm_verifier.py` (те же + UP041 socket.timeout). Pre-existing до P1.

**Решение:** `--fix` (ruff auto-fixes 13 из них, остальные требуют ручной правки). **Won't fix** в P1-P4 (out of scope). Возможно в P8 docs pass.

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

## Verification commands (replay any time)

```bash
cd /opt/searxng
python3 -m pytest -q                          # 123 passed expected (post-Phase 0)
python3 -m ruff check tests/                  # clean
python3 -m ruff check src/                    # 16+15=31 pre-existing (WONTFIX #008)
docker compose -f config/docker-compose.yml config  # OK
ls -la /opt/searxng/.env_llm                  # mode 0o600

# Phase 0 portability gate (3 modes, all must pass)
python3 -m pytest -q                                                            # repo root
tmp=$(mktemp -d) && cp -a . "$tmp/project" && cd "$tmp/project" && \
  PYTHONPATH=src python3 -m pytest -q                                            # /tmp + PYTHONPATH
cd "$tmp/project" && env -i PATH="$PATH" HOME="$PWD/.tmp-home" PYTHONPATH=src \
  python3 -m pytest -q                                                            # /tmp + env -i
rm -rf "$tmp"
```
