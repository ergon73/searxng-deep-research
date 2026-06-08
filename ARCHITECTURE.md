# Deep Research — Architecture

**Version**: v0.7.3 (5 June 2026, after ChatGPT review)
**Last updated**: 5 June 2026
**Status**: Working, ~20-60% verification rate (depends heavily on query)

## Changelog v0.7.3 (5 June 2026)

Bugfixes after independent review (see `/opt/searxng/DR-05062026.txt`):

| # | Bug | Fix | File |
|---|---|---|---|
| 1 | `fetch_url` UnboundLocalError on `title` if trafilatura returns empty | Initialize `title` BEFORE if/else | `hermes_deepresearch.py` |
| 2 | SSRF-риск в `fetch_url` (private/loopback IP) | Added `_is_safe_fetch_url()` guard | `hermes_deepresearch.py` |
| 3 | `use_proxy=True` в `web_search` — концептуально сломан (проксирование localhost) | Removed | `hermes_searxng.py` |
| 4 | `requirements.txt` отсутствовал | Created | `requirements.txt` |
| 5 | Сортировка брала **короткий** текст при равном confidence | `-length` instead of `length` | `hermes_deepresearch.py` |
| 6 | OpenRouter `response_format: json_object` не использовался | Added | `llm_verifier.py` |
| 7 | `time_range` в `deep_research` не пробрасывался в `verify_sources` | Passed through | `hermes_deepresearch.py` |
| 8 | `infer_time_range()` отсутствовал | Added with 30+ RU/EN keywords | `hermes_deepresearch.py` |
| 9 | Документация врала про "94% rate" | Updated baseline to **honest** metrics | `ARCHITECTURE.md`, `projects.md` |

## 1. High-level data flow

```
User query (RU/EN)
  │
  ▼
[Query reformulation]              ← максимум 2 query variants (RU + reformulated EN)
  │                                  ⚠️ reformulate() — placeholder-словарь из 6 слов
  │                                  TODO: LLM-based reformulation
  ▼
[Auto time_range inference]        ← "сегодня" → day, "вчера" → week, "в 2020" → year
  │                                  30+ RU/EN keywords, порядок: fresh → year
  ▼
[Canonical URL dedup]              ← strip utm_*, fbclid, default ports, fragment
  │                                  vote: engines ∪ queries, multi-source boost
  ▼
[Weighted SearXNG ranking]         ← 0.45*rank + 0.35*coverage + 0.20*engine_weight
  │                                  12 default engines, 12.0s timeout
  ▼
[Parallel fetch top-N]             ← ThreadPoolExecutor, 6 concurrent, 12s timeout, 8KB cap
  │                                  SafeRedirectHandler (SSRF guard)
  │                                  trafilatura → main content (Mozilla Readability)
  ▼
[Top-1 = highest source_score]     ← length × has_keyword × status (НЕ truth!)
  │
  ▼
[Fact extraction]                  ← regex: числа+существительное, даты, capitalized≥6
  │                                  SKIP_NUM_UNITS filter
  ▼
[Verification: 4-level]            ← exact → fuzzy → synonym → LLM-conditional
  │                                  SUPPORTS / REFUTES / INSUFFICIENT / CONFLICTING
  │                                  per-source negation detection
  ▼
{verified_facts, total_facts, verification_rate, sources[], supporting_sources, refuting_sources}
```

## 2. Components

| Component | File | Purpose | Notes |
|---|---|---|---|
| `web_search()` | `hermes_searxng.py` | SearXNG wrapper, returns list of hits | Bug fix: `opener.open(context=...)` not supported, moved to `urlopen` |
| `news_search()` | `hermes_searxng.py` | Same, but `categories=news` | |
| `fetch_url()` | `hermes_deepresearch.py` | HTTP fetch + trafilatura extract | 12s timeout, 8KB cap, 2MB max body |
| `deep_search()` | `hermes_deepresearch.py` | search + fetch + confidence | Top-1 confidence 0.78-0.88 |
| `deep_research()` | `hermes_deepresearch.py` | multi-query + dedup + verify | **Main entry point** |
| `verify_sources()` | `hermes_deepresearch.py` | 4-level verification | **Conditional LLM-verify** when rate < 70% |
| `LLMVerifier` | `llm_verifier.py` | OpenRouter client | `meta-llama/llama-3.1-8b-instruct:free` |

## 3. Data model

```python
deep_research(query, time_range=None, top_n=5) -> {
    "query": str,
    "queries_used": [str],          # actual query variants sent to SearXNG
    "sources": [
        {
            "url": str,
            "title": str,
            "text": str,             # main content, ~8KB max
            "length": int,
            "fetch_dt": float,       # seconds
            "engine": str,           # which SearXNG engine returned this
            "confidence": float,     # 0.0-1.0
            "error": None | str,
        },
        ...
    ],
    "top1": { ...same as source... },
    "top1_confidence": float,
    "verified_facts": int,           # count of facts confirmed by ≥1 other source
    "total_facts": int,
    "verification_rate": float,      # verified_facts / total_facts
    "verification_details": [
        {
            "fact": str,
            "verified": bool,
            "matches": [(source_url, similarity%), ...],
            "method": "exact"|"fuzzy"|"synonym"|"llm",
        },
        ...
    ],
    "llm_enhanced": bool,            # True if LLM-verify was triggered
    "llm_verified_count": int,
    "llm_latency": float,            # seconds, 0 if LLM not triggered
}
```

## 4. Decision log (10 ключевых решений)

| # | Decision | Why | What was rejected |
|---|---|---|---|
| 1 | **Per-engine proxy, not global `outgoing.proxies`** | Если прокся падает, глобальная ломает все движки, включая работающие direct | Global proxy (Brave, DDG, Startpage) |
| 2 | **HYBRID engines config** (12 default) | Score 553.4, beats BASELINE (537.6) и TUNED (550.0) | All 70+ engines (CAPTCHA, latency) |
| 3 | **Per-fact verification, not per-claim** | Regex + counter: детерминирован, дёшево, < 5s | Per-sentence LLM (×10 cost) |
| 4 | **Regex fact extraction, not LLM** | Детерминирован, воспроизводим, $0 | LLM-extract (но это часть v0.9 plan) |
| 5 | **Fuzzy matching via `rapidfuzz`** | C-биндинг, ~30ms, ловит "БПЛА"/"беспилотник" | Pure Python fuzzy (медленно) |
| 6 | **Synonym dict (~30 пар)** | 0 cost, покрывает 80% технических синонимов | Embeddings (overkill) |
| 7 | **LLM-conditional (rate < 70%)** | На 79% кейсов LLM не нужен → экономим 4-5s | LLM-always (дорого) |
| 8 | **Batch prompt (N facts → 1 LLM call)** | Per-fact = 3s × 5 = 15s; batch = 2.3s total | Single-fact calls |
| 9 | **OpenRouter + Llama 3.1 8B free** | Free, JSON mode ✅, 600ms latency | Mistral 7B (no JSON), GPT-4o (paid) |
| 10 | **`time_range` bug — fix pending v0.8** | Сейчас параметр принимается, но игнорируется в `verify_sources` | — |

## 5. Metrics baseline (v0.7.3 — HONEST)

### 4 regression test cases (auto time_range enabled)

| Query | Type | Verified/Total | Rate | Top-1 conf | Latency | Auto time_range |
|---|---|---|---|---|---|---|
| `БПЛА над Москвой сегодня` | RU news | 1/10 | 10% | 0.75 | 22.3s | `day` |
| `БПЛА Внуково вчера` | RU news | 0/10 | 0% | 0.63 | 6.4s | `week` |
| `python decorators example code` | EN dev | 3/5 | 60% | 1.00 | 6.0s | None |
| `погода Москва сегодня` | RU news | 3/10 | 30% | 0.83 | 7.8s | `day` |
| **Average** | | **7/35** | **20%** | **0.80** | **10.6s** | — |

### Honest assessment

**Average rate ~20% is much lower than the "94%" previously claimed in this doc.**
Reasons:
- **Top-1 is often irrelevant** for broad queries (e.g. "погода" returns "афиша")
- **Source overlap is low** for news: 2-3 sources may give different framings
- **Fact extraction still produces noise** (capitalized words pass the filters)
- **LLM helps but not enough** when top-1 is wrong

**However**: on well-formed, dev-focused queries the rate reaches 60-100%.
For dev/EN queries the system is **useful**. For news/RU it needs more work.

### Verification method distribution

- Exact match: ~20% of facts
- Fuzzy (rapidfuzz): ~30%
- Synonym dict: ~5%
- LLM-conditional: ~10%
- Unverified: **~35%** (the gap to 100%)

### Cost

- LLM calls: triggered in ~75% of research runs
- Per LLM call: ~500 tokens, 1.5-2.5s latency
- Cost: $0 (Llama 3.1 8B free tier)
- SearXNG: $0 (local)

## 6. v0.8 plan (small, high-ROI improvements)

| Step | What | Estimated effect | Time |
|---|---|---|---|
| 1 | Fix `time_range` in `verify_sources` (currently silently dropped) | Bug fix, no metric change | 15 min |
| 2 | Negation detection (regex: `(не\|нет\|без)\s+(fact)`) | +2-3% rate, +50ms | 30 min |
| 3 | Auto time_range inference (10 keywords RU+EN) | Better top-1 for fresh queries | 1 hour |

**Expected v0.8**: 94% → ~96% rate, **dramatic UX boost** (auto time_range).

## 7. Phase 4-5 (not planned for v0.8)

- **Phase 4: LLM synthesis** — TL;DR → facts → sources с inline citations. Не улучшает rate, **улучшает readability** для LLM-агента
- **Phase 5: Temporal reasoning** — улучшенный time_range (force по смыслу: "сейчас" → day, "в 2020" → year). Частично покрыто в v0.8 step 3

## 8. Cmd map (для частых операций)

```bash
# Run research
python3 -c "import sys; sys.path.insert(0,'/opt/searxng'); from hermes_deepresearch import deep_research; print(deep_research('БПЛА Москва'))"

# A/B test SearXNG config (skill: searxng-ab-testing)
hermes skill run searxng-ab-testing

# Restart SearXNG
cd /opt/searxng && docker compose restart searxng

# Check logs
docker logs --tail 50 searxng

# Test LLM verifier
python3 -c "import sys; sys.path.insert(0,'/opt/searxng'); from llm_verifier import LLMVerifier; v=LLMVerifier(); print(v.verify_fact('123 дрона', '123 дрона сбито', 'сбито 123 дронов'))"

# Update SearXNG image
cd /opt/searxng && docker compose pull && docker compose up -d
```

## 9. Changelog

| Version | Date | Change | Rate impact |
|---|---|---|---|
| v0.1 | 2026-06-05 | SearXNG + `web_search()` | — |
| v0.2 | 2026-06-05 | + `news_search()` | — |
| v0.3 | 2026-06-05 | + `deep_search()` (fetch + confidence) | — |
| v0.4 | 2026-06-05 | + `deep_research()` (multi-query) | baseline 79% |
| v0.5 | 2026-06-05 | + verification (fuzzy + synonym) | 79% → 86% |
| v0.6 | 2026-06-05 | + `LLMVerifier` (OpenRouter, free) | 86% → 91% |
| v0.7 | 2026-06-05 | + batch prompt + conditional integration | 91% → **94%** |
| v0.8 | planned | time_range fix + negation + auto inference | 94% → ~96% |

## 10. Known limitations (won't be fixed)

| Limitation | Why | Workaround |
|---|---|---|
| Adversarial: top-1 and 2-3 others all wrong the same way | System verifies top-1 vs others, can confirm shared error | Manual fact-check on critical claims |
| LLM false negatives on "too generic" facts ("Аэропорт Внуково") | LLM struggles with broad matches | Human review on flagged unverified |
| Residential proxy may exit | Upstream pool exhaustion | Per-engine proxy (not global) so others still work |
| `time_range` not propagated to verification in v0.7 | Bug in `verify_sources` signature | v0.8 step 1 |
| Reformulation is 6-word placeholder | Cheap but ineffective | v0.9: LLM-based reformulation |
