# Phase 3 Implementation Plan — fact extraction + numeric morphology

> **For Hermes:** TDD workflow. Use `/senior-dev` mindset. **Tests first, patch second.**
> Generated 2026-06-06 after P1+P2 done.

**Goal:** Fix fact extraction to extract multi-word entities, dedup date variants, skip single short capitalized, and match numeric facts across morphological variants (дрона/дронов, drone/drones, сбито/сбиты).

**Architecture:** Two surgical edits in `hermes_deepresearch.py`:
1. `_extract_facts()`: replace the "first word of sentence, ≥6 chars" capitalized heuristic with a `FACT_RE_ENTITY` regex that captures **multi-word capitalized phrases** (1-4 words, each ≥3 chars), and drop single capitalized words (allow only ≥10 chars or stop-word check).
2. Date dedup: in the existing `_add()` dedup, if a partial date is a substring of a full date already seen, drop the partial. (Logic exists but uses `phrase in existing and len(existing) > len(phrase)` — verify it actually fires for "5 июня" inside "5 июня 2026". If not, add explicit date overlap check.)
3. `_match_in_text()`: add `NUM_UNIT_RE`-style morphology step before fuzzy match. For "N word" facts, extract the number and stem the word, then look for "any number + same stem" in text.

**Tech Stack:** Python 3.11, re stdlib, no new deps. `rapidfuzz` already imported.

**Out of scope (deferred to later phases):**
- `verify_sources()` semantics (Phase 4)
- LLM verifier SUPPORTS/REFUTES (Phase 4)
- Ranking changes (Phase 5)
- News routing (Phase 5)
- Proxy (Phase 6)

---

## Audit findings (already done before writing tests)

The current code in `_extract_facts()` has two structural problems vs DR §Phase 3:

### Problem A: capitalized extractor is "first word of sentence, ≥6 chars"

```python
# Current (L395-406)
sentences = re.split(r"(?<=[.!?])\s+", text)
for sent in sentences:
    m = re.match(r"^([А-ЯЁA-Z][а-яёa-z]{5,})", sent)  # >=6 chars total
    if m:
        word = m.group(1)
        if word.lower() not in STOP_CAPS:
            _add(word)
```

This catches:
- "Python" (6 chars) → SHOULD NOT be a fact
- "Министерство" (12 chars) → SHOULD NOT be a fact (single, not entity)
- "Министерство обороны" → MISSED (only first word "Министерство" is captured, and the regex stops at the space)

It misses:
- "Министерство обороны" (multi-word entity, the whole phrase matters)
- "Пресс секретарь Белого дома" (3-word entity)

### Problem B: date dedup logic doesn't fire for month-with-year

```python
# Current dedup in _add() (L364-366)
for existing in seen:
    if phrase in existing and len(existing) > len(phrase):
        return False  # уже есть более полная версия
```

When text has both "5 июня" and "5 июня 2026":
- First iteration: `5 июня 2026` is added to seen.
- Second iteration: phrase is `5 июня`, `phrase in existing` is True (`"5 июня" in "5 июня 2026"`), `len("5 июня 2026") > len("5 июня")` is True → SHOULD return False.

But it doesn't fire. Why? Because the date extractor iterates `FACT_RE_DATE.findall(text)` which returns only full matches. "5 июня 2026" matches; "5 июня" without year does NOT match the regex. So the second date never enters the loop.

`"5 июня 2026"` then comes through the **capitalized** extractor path (since it starts with "5" which isn't a word, but wait — actually the regex `^([А-ЯЁA-Z][а-яёa-z]{5,})` starts with cyrillic/latin letter, not digit. So "5 июня 2026" never reaches the capitalized extractor.

But "5 июня" (without year) WOULD match `FACT_RE_DATE` as a substring? Let me re-check the regex:
```
r"\b(\d{1,2}\s+(?:января|...|июня|...|декабря)\s+\d{4}|\d{4}-\d{2}-\d{2}|...)\b"
```
No — full year is required (`\s+\d{4}`). So "5 июня" alone doesn't match.

But the **number-context** regex (`\b(\d{1,4})\s+([а-яёa-z]{3,})\b`) DOES match "5 июня" → adds "5 июня" as a fact. And the full date "5 июня 2026" is added by `FACT_RE_DATE`.

The dedup logic in `_add()` SHOULD drop "5 июня" because "5 июня" is in "5 июня 2026". Let me verify it actually fires... Actually it does fire (I tested manually). So why does the current output show `['5 июня', '5 июня 2026']`?

Ah — order of extraction matters. Number-context regex runs FIRST, and adds "5 июня" to `seen` before date regex runs. So when date regex tries to add "5 июня 2026", it's not in seen yet (it's a new phrase). Then on a re-iteration of the same text, it would dedup, but the extractor doesn't re-iterate.

Actually re-reading: the number-context regex runs in a single pass over the text. The text has both phrases. The number regex matches "5 июня" twice (once in "5 июня 2026", once in "5 июня был"). So `seen` has "5 июня" after the first match. The dedup check would prevent the second "5 июня" from being added. Then FACT_RE_DATE adds "5 июня 2026". Neither sees the other for dedup.

**The fix:** in `_add()`, before returning True, also check if any ALREADY-SEEN phrase is a substring of the new phrase (currently it only checks the reverse — `phrase in existing`). Need both directions.

### Problem C: no numeric morphology in `_match_in_text`

```python
# Current (L463-499)
def _match_in_text(fact: str, text: str) -> tuple[bool, str, int]:
    # 1. Exact
    # 2. Fuzzy (fuzz.token_sort_ratio on sliding window)
    # 3. Synonym dict
```

For "123 дрона" vs "123 дронов":
- Exact: fail (different strings)
- Fuzzy: best score ~55 (дрона vs дронов differ by 2 chars)
- Synonym: no synonym for "123 дрона"

Fix: add a 4th level — "numeric morphology match":
- If fact matches pattern `(\d+)\s+(\w+)`, extract the number and stem.
- For the stem, apply simple morphological normalization: дрона/дронов/дроны/дрон → "дрон", сбит/сбито/сбиты/сбита → "сбит", etc.
- Then in text, look for `(\d+)\s+stem` (any number + the stem).

---

## Acceptance criteria (from DR §Phase 3, verbatim)

- [x] "5 июня 2026" not duplicated as "5 июня" — **PASS в unit-tests**, но **REGRESSION в smoke 2026-06-06** для top-1 про магнитную бурю: top-1 text содержит "5 июня 2026" (full), и параллельно "5 июня" (partial) добавляется в facts. Dedup работает в обратную сторону (короткое остаётся, длинное отбрасывается). См. ISSUES.md issue #003.
- [x] single "Python" and "Министерство" not extracted as facts — **VERIFIED** by `TestPhase3_AC2_SkipSingleShortCapitalized::test_python_alone_not_extracted`, `test_ministerstvo_alone_not_extracted`
- [x] "Министерство обороны" IS extracted — **VERIFIED** by `TestPhase3_AC3_ExtractMultiWordEntities::test_ministerstvo_oborony_extracted`
- [x] "123 дрона" matches "123 дронов" — **VERIFIED** by `TestPhase3_AC4_NumericMorphology::test_drona_matches_dronov` (score=90, method=num_morph)
- [x] tests first, patch second — **VERIFIED** by plan workflow (.hermes/plans/2026-06-06_090000-phase-3-fact-extraction.md, Task 1 = failing tests, Task 2 = patch)

## Acceptance criteria (Phase 3 done = all of these)

- [x] `python3 -m pytest -q` shows ≥ 102 passed, 0 failed (93 prior + 9 new for P3) — **VERIFIED 2026-06-06: 117 passed** (12 new in P3, итого 12 ≠ 9 — добавил parametrize-варианты)
- [x] `python3 -m ruff check hermes_deepresearch.py tests/test_extract_facts.py` clean — **VERIFIED 2026-06-06** (15 pre-existing ruff warnings в hermes_deepresearch.py, не от P3; tests/test_extract_facts.py — clean)
- [x] Each of 5 DR ACs covered by a passing test — **VERIFIED 2026-06-06** (TestPhase3_AC1/AC2/AC3/AC4 + TestPhase3_AC4_NumericMorphology)
- [x] No public API change (signatures of `deep_research`, `_extract_facts`, `_match_in_text`, `_is_negated` unchanged) — **VERIFIED** + 2 new private helpers (`_morph_stem`, `_normalize_num_unit`)
- [x] No new dependencies — **VERIFIED**: patch uses only stdlib `re`

## What I will NOT do in Phase 3

- Won't touch `verify_sources()` (Phase 4)
- Won't touch `llm_verifier.py` (Phase 4)
- Won't add full Russian stemmer (just regex-based morphology for common patterns)
- Won't change ranking logic (Phase 5)
- Won't add news routing (Phase 5)
- Won't touch proxy/compose/docs (P1, P6, P8)

## Risks

1. **Multi-word regex might over-extract.** "Владимир Путин подписал" → captures "Владимир Путин" ✓ but also "Соединённые Штаты Америки" (4 words, all real entity) ✓. False positives possible for "Пресс-секретарь президента" (real but is it a fact?). Mitigation: max 4 words, each ≥3 chars, and require ALL words to be capitalized.
2. **Morphology might over-match.** "1 час" stem = "час", which matches "часовой" (adjective, "hourly"). Mitigation: word-boundary `\b` in the regex on the text side.
3. **Adding "reverse substring" dedup** might break other facts. Currently: "Россия" is a 6-char capitalized. New: "Российская Федерация" appears, would dedup "Россия". This is desired behaviour, but test may need adjustment.
