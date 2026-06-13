"""
hermes_deepresearch.py — deep research поверх SearXNG.

Паттерны:
- web_search(): как раньше, голый список результатов
- deep_search(query): web_search + parallel fetch top-N + extract text
- deep_research(query): multi-query (RU/EN) + merge + dedup + top-K sources

Best practices:
- Trafilatura для HTML→text (Mozilla Readability под капотом)
- Concurrent fetches через ThreadPoolExecutor (5-10x быстрее sequential)
- Per-source token budget (TRUNCATE_CHARS) чтобы не раздувать контекст
- Failure isolation: один сломанный URL не ломает остальные
- Confidence score per source (length + has_keyword + status)
- "Honest don't know": пустой результат возвращается явно, без мусора
"""

import concurrent.futures
import json
import re
import ssl
import time
import urllib.parse
import urllib.request

import trafilatura

# Импортируем существующие helpers. Требует PYTHONPATH=/opt/searxng
# или запуска из /opt/searxng. Hardcoded sys.path удалён — это плохая практика
# (см. DR-05062026(2).txt P0 #hardcoded-syspath).
from hermes_searxng import web_search
from llm_verifier import LLMVerifier
from rapidfuzz import fuzz

# === Canonical URL (v0.8) ===

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def canonical_url(url: str) -> str:
    """
    Нормализует URL для dedup:
    - scheme lowercase
    - host lowercase + strip default ports (:80, :443)
    - strip tracking params (utm_*, fbclid, ...)
    - strip fragment
    - path: rstrip trailing / (кроме корня)
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    query = urlencode(
        [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    )
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


# === Search-result ranking (v0.8) ===

ENGINE_WEIGHT = {
    "wikipedia": 0.85,
    "wikidata": 0.80,
    "bing": 0.75,
    "bing news": 0.85,
    "duckduckgo": 0.70,
    "duckduckgo news": 0.85,
    "github": 0.85,
    "stackoverflow": 0.80,
    "semantic scholar": 0.90,
    "arxiv": 0.85,
    "mojeek": 0.65,
    "presearch": 0.55,
    "brave": 0.70,
    "brave news": 0.80,
    "google": 0.75,
    "google news": 0.85,
}


def _search_result_score(r: dict, query_terms: list[str]) -> float:
    """
    Weighted score для ranking search results:
    - 0.45 * rank_score (1/(rank+1), позиция в SearXNG)
    - 0.35 * coverage (доля query_terms в title+snippet)
    - 0.20 * engine_weight
    """
    rank = r.get("_rank", 0)
    rank_score = 1.0 / max(rank + 1, 1)

    haystack = f"{(r.get('title') or '').lower()} {(r.get('snippet') or '').lower()}"
    if query_terms:
        coverage = sum(1 for t in query_terms if t.lower() in haystack) / len(query_terms)
    else:
        coverage = 0.5

    engine_score = ENGINE_WEIGHT.get((r.get("engine") or "").lower(), 0.5)

    return 0.45 * rank_score + 0.35 * coverage + 0.20 * engine_score


UA_FETCH = "hermes-research/1.0 (+local SearXNG)"
TIMEOUT = 12.0
MAX_CONTENT_CHARS = 8000  # ~2к токенов на источник
MAX_CONCURRENT_FETCH = 6  # параллельных запросов
MAX_FETCH_BYTES = 2_000_000  # 2 МБ cap per response

# Verification tuning
FUZZY_THRESHOLD = 75  # % similarity для fuzzy match
LLM_VERIFY_THRESHOLD = 0.7  # если verification_rate < 70% → подключаем LLM
SYNONYM_DICT = {
    # Технические / частотные пары
    "append": {"add", "push", "insert"},
    "delete": {"remove", "pop", "discard"},
    "дрон": {"бпла", "беспилотник", "квадрокоптер", "uav", "drone"},
    "бпла": {"дрон", "беспилотник", "uav", "drone"},
    "беспилотник": {"бпла", "дрон", "uav", "drone"},
    "сбит": {"уничтожен", "перехвачен", "перехватили", "сбили", "shot down"},
    "уничтожен": {"сбит", "ликвидирован", "destroyed"},
    "аэропорт": {"воздушная гавань", "airport"},
    "python": {"питон", "python3"},
    "async": {"asynchronous", "асинхронный"},
    "function": {"функция", "method", "метод"},
}

# Укороченный список стоп-слов / шумовых паттернов для confidence
NOISE_PATTERNS = re.compile(
    r"(cookie\s+(policy|consent)|subscribe\s+to|paywall|"
    r"sign\s+up\s+for|enable\s+javascript|gdpr|"
    r"advertisement|sponsored)",
    re.I,
)


def _is_safe_fetch_url(url: str) -> bool:
    """
    SSRF-guard (allowlist): пропускаем только http/https + IP.is_global.
    ip.is_global автоматически исключает:
    - 127.0.0.0/8 (loopback)
    - 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 (private)
    - 169.254.0.0/16 (link-local, AWS metadata)
    - 100.64.0.0/10 (CGNAT)
    - 0.0.0.0, multicast, reserved
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        # IPv4-mapped IPv6 (::ffff:127.0.0.1 etc.)
        if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped is not None:
            if not ip.ipv4_mapped.is_global:
                return False
            continue
        if not ip.is_global:
            return False
    return True


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Перехватывает HTTP redirect'ы и блокирует те, что ведут на unsafe URL.
    Защита от SSRF через redirect-bypass: публичный URL → 169.254.169.254.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_fetch_url(newurl):
            raise urllib.error.URLError(f"blocked unsafe redirect: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_urlopen(req, *, timeout: float):
    """
    urlopen с защитой от SSRF через redirect. Использует наш SafeRedirectHandler.
    """
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _SafeRedirectHandler,
        urllib.request.HTTPSHandler(context=ctx),
    )
    return opener.open(req, timeout=timeout)


def fetch_url(url: str, *, timeout: float = TIMEOUT, max_chars: int = MAX_CONTENT_CHARS) -> dict | None:
    """
    Fetch URL and extract main content.
    Returns {url, title, text, length, fetch_dt, error} или None если совсем плохо.
    """
    # SSRF guard
    if not _is_safe_fetch_url(url):
        return {
            "url": url,
            "title": "",
            "text": "",
            "length": 0,
            "fetch_dt": 0.0,
            "error": "blocked unsafe URL (SSRF guard)",
        }

    t0 = time.time()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA_FETCH,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru,en;q=0.7",
            },
        )
        with _safe_urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("content-type", "")
            data = r.read(MAX_FETCH_BYTES + 1)
            if len(data) > MAX_FETCH_BYTES:
                data = data[:MAX_FETCH_BYTES]
            final_url = r.geturl()
            # После возможных redirect'ов снова проверить final URL
            if not _is_safe_fetch_url(final_url):
                return {
                    "url": final_url,
                    "title": "",
                    "text": "",
                    "length": 0,
                    "fetch_dt": round(time.time() - t0, 2),
                    "error": "blocked unsafe final URL (after redirect)",
                }
            if "html" not in ct and "xml" not in ct:
                return {
                    "url": final_url,
                    "title": "",
                    "text": "",
                    "length": 0,
                    "fetch_dt": round(time.time() - t0, 2),
                    "error": f"non-html: {ct}",
                }

        html_raw = data.decode("utf-8", errors="ignore")

        # Title из <title> (нужно в обоих ветках, не только fallback)
        from html import unescape

        m = re.search(r"<title[^>]*>([^<]+)</title>", html_raw)
        title = unescape(m.group(1)).strip() if m else ""

        # Extract через trafilatura (Mozilla Readability)
        text = trafilatura.extract(html_raw) or ""

        # Fallback: simple tag-strip (если trafilatura не справилась)
        if not text:
            html = re.sub(r"<script.*?</script>", "", html_raw, flags=re.DOTALL)
            html = re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)
            m_art = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
            if m_art:
                t = re.sub(r"<[^>]+>", " ", m_art.group(1))
                text = re.sub(r"\s+", " ", unescape(t)).strip()
            else:
                t = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", unescape(t)).strip()[:max_chars]

        # Truncate до max_chars
        if len(text) > max_chars:
            text = text[:max_chars] + "…[truncated]"

        return {
            "url": final_url,
            "title": title,
            "text": text.strip(),
            "length": len(text),
            "fetch_dt": round(time.time() - t0, 2),
            "error": None,
        }
    except Exception as e:
        return {
            "url": url,
            "title": "",
            "text": "",
            "length": 0,
            "fetch_dt": round(time.time() - t0, 2),
            "error": f"{type(e).__name__}: {e}",
        }


def _confidence(result: dict, query_terms: list[str]) -> float:
    """
    Эвристическая оценка: 0..1.
    Учитывает: длина текста, наличие ключевых слов из запроса,
    отсутствие шумовых паттернов.
    """
    if not result or result.get("error") or not result.get("text"):
        return 0.0
    text = result["text"].lower()
    # длина: лог-кривая от 0 до 2000 chars
    length_score = min(1.0, result["length"] / 2000)
    # попадание ключевых слов: доля терминов, которые встретились
    if query_terms:
        hits = sum(1 for t in query_terms if t.lower() in text)
        keyword_score = hits / len(query_terms)
    else:
        keyword_score = 0.5
    # штраф за noise
    noise_hits = len(NOISE_PATTERNS.findall(text[:2000]))
    noise_penalty = min(0.5, noise_hits * 0.1)
    return max(0.0, min(1.0, 0.5 * length_score + 0.5 * keyword_score - noise_penalty))


def _extract_query_terms(query: str) -> list[str]:
    """Выделяет значимые слова (>3 букв, не стоп-слова)."""
    STOP = {
        "и",
        "в",
        "на",
        "с",
        "по",
        "о",
        "у",
        "для",
        "the",
        "a",
        "an",
        "in",
        "on",
        "of",
        "to",
        "is",
        "are",
        "was",
        "were",
    }
    words = re.findall(r"\w{3,}", query.lower())
    return [w for w in words if w not in STOP]


# === Fact extraction & 4-level verification (v0.7) ===

# Паттерны: числа, даты, имена собственные (capitalized words), ключевые слова
FACT_RE_NUM = re.compile(r"\b\d[\d\s.,]{0,15}\b")  # 123, 1 500, 12.5
FACT_RE_DATE = re.compile(
    r"\b("
    r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
    r")\b",
    re.IGNORECASE,
)
FACT_RE_CAPS = re.compile(
    r"\b[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})*\b|\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b"
)
FACT_RE_NEG = re.compile(r"^(не|нет|без)\s+", re.IGNORECASE)

# v0.8.2 (Phase 3): Multi-word entities.
# Первое слово capitalized, остальные 1-3 — любые рус/eng слова >=3 chars.
# Catches: "Министерство обороны", "Пресс секретарь Белого дома", "Ministry of Defense".
# NB: разрешаем non-capitalized продолжение, потому что в середине предложения
# "обороны" после "Министерство" пишется со строчной. Стоимость: больше шума,
# но stop-words + max_facts ограничивают.
FACT_RE_ENTITY = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]{2,}(?:\s+(?:[А-ЯЁA-Z]?[а-яёa-z]{2,})){1,3}\b")
# Solo capitalized: v0.8.2 (Phase 3) — выключен по AC2 ("Министерство" одно не факт).
# Оставлен как escape hatch на будущее, но не используется в _extract_facts.
# Фильтрует "Python" (6), "Методы" (6), "Сегодня" (7), "Министерство" (12).
FACT_RE_ENTITY_SOLO = re.compile(r"(?!)")  # never matches


def _extract_facts(text: str, max_facts: int = 8, query: str = "") -> list[str]:
    r"""
    Извлекает ключевые факты из текста top-1 источника.

    Улучшенная версия (v0.7.2):
    - Числа: только в контексте существительного (\d+\s+\w{3,})
    - Capitalized words: только в начале предложения, длина >=6 (отсекает "Всего", "Системы")
    - Дедуп дат: "5 июня 2026" не дублируется с "5 июня" + "2026"
    - Фильтр стоп-слов ("Что", "Как", "Это", "Методы", ...)

    v0.8.3 (Phase C — query-aware scoring):
    - Если передан query, fact'ы скоринг по:
      * +1.0 за каждое overlap word с query
      * +0.5 за длину (longer = more specific, до 4 words)
      * -2.0 за generic/short фрагменты (< 2 words, len < 8)
      * -1.0 за meta/nav words (Category, Upload, File, Version, Block, ...)
    - Возвращаем top-N by score, не первые-N в порядке появления.
    - Backwards-compat: query="" → original behavior (first-N, dedup).

    Цель: убрать шум (числа из таблиц, capitalized-обрывки, nav-фрагменты).
    """
    if not text:
        return []

    # Стоп-слова для capitalized-фильтра (рус + англ обрывки)
    STOP_CAPS = {
        "это",
        "что",
        "как",
        "или",
        "для",
        "при",
        "его",
        "её",
        "их",
        "этот",
        "эта",
        "эти",
        "тот",
        "та",
        "те",
        "все",
        "весь",
        "однако",
        "также",
        "котор",
        "методы",
        "учимся",
        "сегодня",
        "вчера",
        "сейчас",
        "теперь",
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "what",
        "how",
    }

    # Skip units: "123 item" — не факт, "1 год" — не самостоятельный факт
    # (включая plural и падежные формы)
    SKIP_NUM_UNITS = {
        "год",
        "года",
        "году",
        "годом",
        "годы",
        "лет",
        "item",
        "items",
        "line",
        "lines",
        "example",
        "examples",
        "код",
        "кода",
        "коды",
        "пример",
        "примеры",
        "примеров",
        "раз",
        "раза",
        "размер",
        "размера",
        "size",
        "sizes",
        "pixel",
        "pixels",
        "px",
        "msk",
        "мин",
        "минут",
        "сек",
        "секунд",
        "час",
        "часа",
        "часов",
        "hours",
        "minutes",
        "seconds",
    }

    facts = []
    seen = set()

    def _add(phrase: str) -> bool:
        """Add fact if not duplicate and not in stop words. Returns True если добавлен."""
        if not phrase or phrase in seen:
            return False
        if any(w in STOP_CAPS for w in phrase.lower().split()):
            return False
        # Дедуп дат и entities: если уже есть более ПОЛНАЯ версия — пропускаем,
        # и наоборот: если новая фраза содержит уже сохранённую — пропускаем старую.
        # Пример: "5 июня" уже в seen, пытаемся добавить "5 июня 2026" — keep full, drop partial.
        # Пример: "Министерство" уже в seen, пытаемся добавить "Министерство обороны" — keep entity.
        to_remove = []
        for existing in seen:
            if phrase in existing and len(existing) > len(phrase):
                return False  # уже есть более полная версия
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
        return True

    # 1. Числа ТОЛЬКО в контексте существительного: "123 дрона", "5 июня 2026"
    #    num = 1-4 цифры (без вложенных пробелов, чтобы не ловить "2020 5 примеров")
    fact_re_num_ctx = re.compile(r"\b(\d{1,4})\s+([а-яёa-z]{3,})\b", re.IGNORECASE)
    for m in fact_re_num_ctx.finditer(text):
        num = m.group(1).strip().rstrip(".,")
        word = m.group(2).lower()
        if word in STOP_CAPS or word in SKIP_NUM_UNITS:
            continue
        if len(word) < 3:
            continue
        phrase = f"{num} {m.group(2)}"
        if len(phrase) <= 30:
            _add(phrase)
            if len(facts) >= max_facts * 3:  # collect more, will rank later
                break

    # 2. Даты (точные паттерны)
    for m in FACT_RE_DATE.findall(text):
        _add(m)
        if len(facts) >= max_facts * 3:
            break

    # 3. Capitalized entities (v0.8.2 — Phase 3)
    #    Алгоритм: ищем capitalized anchors, экстендим entity вправо, останавливаясь
    #    на стоп-слове / глаголе / предлоге / знаке препинания. Пропускаем anchors,
    #    которые сами по себе являются стоп-словами ("Сегодня", "Методы", ...).
    for m in re.finditer(r"\b[А-ЯЁA-Z][а-яёa-z]{2,}\b", text):
        anchor = m.group(0)

        # Skip anchors that are stop words
        if anchor.lower() in STOP_CAPS:
            continue

        # Try to extend entity: collect following words (any case) until stop word
        # or punctuation or end. Cap at 4 words.
        entity_words = [anchor]
        end_pos = m.end()
        for _ in range(3):  # up to 3 more words
            tail = text[end_pos:]
            tm = re.match(r"\s+([А-ЯЁA-Za-zЁёА-Яа-я]{2,})", tail)
            if not tm:
                break
            next_word = tm.group(1)
            # Stop on prepositions/pronouns/stop words (don't extend past them)
            if next_word.lower() in STOP_CAPS:
                break
            # Stop on short words that look like endings
            if len(next_word) < 3:
                break
            entity_words.append(next_word)
            end_pos += tm.end()

        entity = " ".join(entity_words)

        # Single-word entities are filtered by AC2 (skip — see FACT_RE_ENTITY_SOLO)
        if len(entity_words) == 1:
            continue

        if len(entity) <= 80:
            _add(entity)
            if len(facts) >= max_facts * 3:
                break

    # v0.8.3 (Phase C): score & rank by query overlap (only if query given)
    if query and len(facts) > max_facts:
        facts = _score_and_rank_facts(facts, query, max_facts)
    return facts[:max_facts]


# v0.8.3 (Phase C): query-aware fact scoring.
# Heuristics:
#   +1.0  per query word overlap (case-insensitive, substring)
#   +0.5  per fact word above 1, capped at +1.5 (longer = more specific)
#   -2.0  if too short (< 2 words or len < 8) — fragment, not claim
#   -1.0  if contains meta/nav words (Category, File, Upload, Block) — nav text
#   -0.5  if starts with single digit word like "9" — likely from a list
_NAV_WORDS = frozenset(
    {
        "category",
        "file",
        "upload",
        "version",
        "block",
        "subcategory",
        "subcategories",
        "appearance",
        "flight",
        "current",
        "media",
        "wiki",
    }
)


def _score_and_rank_facts(facts: list[str], query: str, max_facts: int) -> list[str]:
    """Score facts against query, return top max_facts by score (desc)."""
    if not query:
        return facts

    q_words = {w.lower().strip(".,!?") for w in query.split() if len(w) >= 2}

    scored: list[tuple[float, str, int]] = []
    for idx, fact in enumerate(facts):
        score = 0.0
        fact_lower = fact.lower()
        fact_words = fact_lower.split()

        # +1.0 per query word overlap
        for qw in q_words:
            if qw in fact_lower or any(qw in fw for fw in fact_words):
                score += 1.0

        # +0.5 per fact word above 1, capped
        score += min(1.5, max(0, len(fact_words) - 1) * 0.5)

        # -2.0 short fragments
        if len(fact_words) < 2 or len(fact) < 8:
            score -= 2.0

        # -1.0 nav words
        if any(nw in fact_words for nw in _NAV_WORDS):
            score -= 1.0

        # -0.5 leading single-digit list markers
        if len(fact_words) >= 1 and fact_words[0].isdigit():
            score -= 0.5

        scored.append((score, fact, idx))

    # Sort by score desc, then by original index (stable)
    scored.sort(key=lambda x: (-x[0], x[2]))
    return [f for _, f, _ in scored[:max_facts]]


# === Auto time_range inference (v0.8) ===

_TIME_KEYWORDS = {
    "day": [
        "сегодня",
        "сейчас",
        "только что",
        "за сутки",
        "сегодняшний",
        "today",
        "now",
        "latest",
        "breaking",
        "just now",
        "this hour",
    ],
    "week": [
        "вчера",
        "на этой неделе",
        "за неделю",
        "этой неделе",
        "yesterday",
        "this week",
        "past week",
        "last week",
    ],
    "month": [
        "в этом месяце",
        "за месяц",
        "этом месяце",
        "текущий месяц",
        "this month",
        "past month",
        "last month",
    ],
    "year": [
        "в этом году",
        "в прошлом году",
        "за год",
        "этом году",
        "прошлый год",
        "this year",
        "last year",
        "past year",
    ],
}


def infer_time_range(query: str) -> str | None:
    """
    Эвристический выбор time_range по ключевым словам в запросе.
    Returns: "day" | "week" | "month" | "year" | None

    ВАЖНО: проверка fresh-сигналов ("сегодня"/"вчера") идёт ПЕРЕД годом,
    иначе запросы типа "БПЛА 5 июня 2026 сегодня" получат "year" вместо "day".
    """
    q = query.lower()

    # 1. Fresh signals (ДО проверки года)
    for kw in _TIME_KEYWORDS["day"]:
        if kw in q:
            return "day"
    for kw in _TIME_KEYWORDS["week"]:
        if kw in q:
            return "week"
    for kw in _TIME_KEYWORDS["month"]:
        if kw in q:
            return "month"

    # 2. Год — только если есть явный preposition ("в 2020", "за 2020", "in 2020")
    if re.search(r"\b(?:в|за|in|during)\s+(19\d{2}|20\d{2})\b", q):
        return "year"
    if re.search(r"\b(19\d{2}|20\d{2})\s*(?:год|году|year)\b", q):
        return "year"

    return None


# v0.8.2 (Phase 3): Numeric morphology — "N дрона" должен матчить "N дронов".
# Извлекаем пару (число, слово), стеммим слово до канонической формы.
NUM_UNIT_RE = re.compile(r"\b(\d+)\s+([а-яёa-z]{3,})\b", re.IGNORECASE)

# Простая словарная морфология (RU + EN). Цель — нормализовать дрона/дронов/дроны
# к одной форме, чтобы fuzzy / exact match сработали. НЕ полноценный стеммер.
_MORPH_MAP = {
    # RU nouns (singular → stem)
    "дрона": "дрон",
    "дронов": "дрон",
    "дроны": "дрон",
    "дроне": "дрон",
    "дрону": "дрон",
    "дроном": "дрон",
    "беспилотника": "беспилотник",
    "беспилотников": "беспилотник",
    "беспилотники": "беспилотник",
    "бпла": "бпла",
    "сбито": "сбит",
    "сбиты": "сбит",
    "сбита": "сбит",
    "сбит": "сбит",
    "обнаружено": "обнаруж",
    "обнаружены": "обнаруж",
    "обнаружена": "обнаруж",
    "обнаружен": "обнаруж",
    "перехвачено": "перехвач",
    "перехвачены": "перехвач",
    "перехвачена": "перехвач",
    "перехвачен": "перехвач",
    "уничтожено": "уничтож",
    "уничтожены": "уничтож",
    "уничтожена": "уничтож",
    "уничтожен": "уничтож",
    "часа": "час",
    "часов": "час",
    "часы": "час",
    "часу": "час",
    "часом": "час",
    "минуты": "мин",
    "минут": "мин",
    "минуту": "мин",
    "минутой": "мин",
    # EN nouns (singular → plural-agnostic stem)
    "drone": "drone",
    "drones": "drone",
    "vehicle": "vehicle",
    "vehicles": "vehicle",
    "missile": "missile",
    "missiles": "missile",
    "attack": "attack",
    "attacks": "attack",
    "shot": "shot",
    "shots": "shot",
    "detected": "detect",
    "detects": "detect",
}


def _morph_stem(word: str) -> str:
    """Вернуть каноническую форму слова (lower). Известные формы из _MORPH_MAP,
    иначе naive -s/-es/-ed для EN и -ов/-ы/-а для RU."""
    w = word.lower()
    if w in _MORPH_MAP:
        return _MORPH_MAP[w]
    # Naive EN: drones → drone
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("es") and len(w) > 3:
        return w[:-2]
    if w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
        return w[:-1]
    # Naive RU: дронов → дрон (genitive plural)
    if w.endswith("ов") and len(w) > 3:
        return w[:-2]
    if w.endswith("ев") and len(w) > 3:
        return w[:-2]
    if w.endswith("ы") and len(w) > 3:
        return w[:-1]
    if w.endswith("а") and len(w) > 3:
        return w[:-1]
    if w.endswith("у") and len(w) > 3:
        return w[:-1]
    if w.endswith("ом") and len(w) > 3:
        return w[:-2]
    if w.endswith("ей") and len(w) > 3:
        return w[:-2]
    if w.endswith("ть") and len(w) > 3:  # инфинитив
        return w[:-2]
    return w


def _normalize_num_unit(phrase: str) -> tuple[str, str] | None:
    """Если phrase выглядит как 'N word', вернуть (number_or_None, stem).
    None если не матчится паттерну."""
    m = NUM_UNIT_RE.search(phrase)
    if not m:
        return None
    return (m.group(1), _morph_stem(m.group(2)))


def _match_numeric_unit(fact: str, text: str) -> tuple[bool, str | None, int]:
    """v0.8.2-A: scan ALL numeric occurrences before deciding.

    Bug fixed: previously the matcher returned (True, "num_mismatch", 85)
    on the first same-stem-different-number occurrence, never reaching a
    later correct same-number occurrence in the same text. For fact
    "22 БПЛА" vs "23 беспилотника ... 22 беспилотника" the matcher would
    return num_mismatch even though a perfect match exists later.

    New algorithm: scan all matches, return (True, "num_morph", 90) on
    the FIRST same-number same-or-synonym-stem occurrence, and only
    fall back to (True, "num_mismatch", 85) if the entire scan has no
    same-number match but at least one same/synonym-stem mismatch.
    """
    norm = _normalize_num_unit(fact)
    if not norm:
        return (False, None, 0)
    fact_num, fact_stem = norm
    text_lower = text.lower()
    saw_mismatch = False

    for m in NUM_UNIT_RE.finditer(text_lower):
        t_num = m.group(1)
        t_stem = _morph_stem(m.group(2))

        same_stem = t_stem == fact_stem
        t_syns = SYNONYM_DICT.get(t_stem, set())
        f_syns = SYNONYM_DICT.get(fact_stem, set())
        synonym_stem = t_stem in f_syns or fact_stem in t_syns or bool(t_syns & f_syns)

        if not (same_stem or synonym_stem):
            continue

        if t_num == fact_num:
            # Same number + same/synonym stem → confident support.
            return (True, "num_morph", 90)

        # Different number + same/synonym stem → potential conflict, but
        # keep scanning in case a same-number match exists later.
        saw_mismatch = True

    if saw_mismatch:
        return (True, "num_mismatch", 85)

    return (False, None, 0)


def _match_in_text(fact: str, text: str) -> tuple[bool, str, int]:
    """
    Match fact в text на 4 уровнях.
    Returns (matched, method, best_score)
    method: "exact" | "fuzzy" | "synonym" | "num_morph" | None
    """
    if not text or not fact:
        return (False, None, 0)
    text_lower = text.lower()
    fact_lower = fact.lower()

    # 1. Exact
    if fact_lower in text_lower:
        return (True, "exact", 100)

    # 2. Numeric morphology (v0.8.2-A).
    #    Same number + same/synonym stem → num_morph (confident support).
    #    Different number + same/synonym stem, no later same-number
    #    occurrence → num_mismatch (count conflict, not support).
    #    All occurrences scanned (see _match_numeric_unit).
    matched, method, score = _match_numeric_unit(fact, text)
    if matched:
        # method is "num_morph" or "num_mismatch" from helper; convert
        # None to None for downstream compatibility (signature says str).
        return (matched, method if method is not None else "num_morph", score)

    # 3. Fuzzy (token_sort_ratio — ловит перестановки слов)
    # Сравниваем fact с каждым окном ±50% длины fact в text
    flen = len(fact_lower)
    window = max(20, int(flen * 1.5))
    best = 0
    for i in range(0, max(1, len(text_lower) - window), max(1, window // 2)):
        chunk = text_lower[i : i + window]
        score = fuzz.token_sort_ratio(fact_lower, chunk)
        if score > best:
            best = score
            if best >= 95:
                break
    if best >= FUZZY_THRESHOLD:
        return (True, "fuzzy", best)

    # 4. Synonym dict
    syns = SYNONYM_DICT.get(fact_lower, set())
    for syn in syns:
        if syn.lower() in text_lower:
            return (True, "synonym", 90)

    return (False, None, best)


def _is_negated(fact: str, text: str) -> bool:
    """
    Negation detection — ищем fact в контексте отрицания.
    Ловит:
    - "не был сбит дрон" (fact до "не")
    - "дрон не сбит" (fact после "не", в одном предложении)
    - "сведений о дроне нет"
    - "no drone was shot down"
    - "ни одного дрона не сбито"
    """
    if not text or not fact:
        return False
    text_l = text.lower()
    fact_l = fact.lower()
    fact_esc = re.escape(fact_l)

    # 1. fact после "не/нет/без" (в одном предложении, до 4 слов)
    #    NB: \b плохо работает с кириллицей в Python, используем lookarounds
    after = re.compile(
        r"(?:^|[^а-яёa-z0-9])(не|нет|без|не\s+был[аи]?|ни|н[еи]\s+одного|ни\s+одного|ни\s+одной)\s+(?:\w+\s+){0,4}"
        + fact_esc
        + r"(?:\b|[^а-яёa-z0-9])",
        re.IGNORECASE,
    )
    if after.search(text_l):
        return True

    # 2. fact до "не/нет" (в одном предложении, до 3 слов между)
    before = re.compile(
        fact_esc
        + r"\w*\s+(?:\w+\s+){0,3}(не|нет|не\s+был[аи]?|ни\s+сбит|не\s+сбит|не\s+обнаружен|ни\s+одного)",
        re.IGNORECASE,
    )
    if before.search(text_l):
        return True

    # 3. "no/not <noun>" для EN (с учётом plural: drone → drones)
    no_ = re.compile(
        r"(?:^|[^а-яёa-z0-9])(no|not)\s+(?:\w+\s+){0,3}" + fact_esc + r"\w*(?:\b|[^а-яёa-z0-9])",
        re.IGNORECASE,
    )
    if no_.search(text_l):
        return True

    return False


# v0.8.3-C2-data: span-localising helpers.
#
# `_is_negated` returns a bool — enough for verdict routing but it discards
# the offset of the negation match. For the future refuting-span markers we
# need the actual (start, end, method) triple pointing at the negation phrase
# in the source text, so it can be surfaced as `[refute_doc_N:start-end]`.
#
# These helpers never invent offsets: if the regex cannot localise the
# refuting / mismatching phrase in `text`, they return None and the caller
# MUST keep only the URL-level provenance (the legacy `refuting_sources` /
# `numeric_mismatch_sources` fields).
def _find_negation_span(fact: str, text: str) -> tuple[int, int, str] | None:
    """Locate the negation match for `fact` inside `text`.

    Returns (offset_start, offset_end, method) where the offsets are into
    the original `text` (not lower-cased) and cover the matched phrase
    ("не <...> <fact>" or similar). `method` is one of
    "negation_after" / "negation_before" / "negation_en". Returns None
    if no negation pattern is found.

    Defensive: returns None for empty inputs, mismatched encodings, or
    any regex error — the caller must treat None as "no span available"
    and keep only the URL-level refuting_sources entry.
    """
    if not text or not fact:
        return None
    try:
        text_l = text.lower()
        fact_l = fact.lower()
        fact_esc = re.escape(fact_l)

        # Same 3 patterns as `_is_negated` (line 962-988), but each
        # captures the matched span via `re.search(...).span()` instead
        # of discarding it.
        after_re = re.compile(
            r"(?:^|[^а-яёa-z0-9])(не|нет|без|не\s+был[аи]?|ни|н[еи]\s+одного|ни\s+одного|ни\s+одной)\s+(?:\w+\s+){0,4}"
            + fact_esc
            + r"(?:\b|[^а-яёa-z0-9])",
            re.IGNORECASE,
        )
        m = after_re.search(text_l)
        if m is not None:
            return (m.start(), m.end(), "negation_after")

        before_re = re.compile(
            fact_esc
            + r"\w*\s+(?:\w+\s+){0,3}(не|нет|не\s+был[аи]?|ни\s+сбит|не\s+сбит|не\s+обнаружен|ни\s+одного)",
            re.IGNORECASE,
        )
        m = before_re.search(text_l)
        if m is not None:
            return (m.start(), m.end(), "negation_before")

        no_re = re.compile(
            r"(?:^|[^а-яёa-z0-9])(no|not)\s+(?:\w+\s+){0,3}" + fact_esc + r"\w*(?:\b|[^а-яёa-z0-9])",
            re.IGNORECASE,
        )
        m = no_re.search(text_l)
        if m is not None:
            return (m.start(), m.end(), "negation_en")

        return None
    except re.error:
        return None


def _find_num_mismatch_span(fact: str, text: str) -> tuple[int, int, str] | None:
    """Locate the first numeric mismatch between `fact` and `text`.

    Scans every `NUM_UNIT_RE` occurrence in `text` and returns the span
    of the FIRST occurrence whose stem matches (or is a synonym of) the
    fact's stem but whose number disagrees. Returns None if no such
    occurrence exists, or if the fact has no normalised numeric unit
    (handled by `_normalize_num_unit`).

    This is the same first-mismatch position that `_match_numeric_unit`
    uses internally; we only extract the offset for the *first* mismatch
    seen, which is enough to surface a span marker pointing at the
    specific number that conflicts with the claim.

    Defensive: returns None on any unexpected error — the caller MUST
    keep only the URL-level `numeric_mismatch_sources` entry.
    """
    if not text or not fact:
        return None
    try:
        norm = _normalize_num_unit(fact)
        if not norm:
            return None
        _fact_num, fact_stem = norm
        text_lower = text.lower()
        f_syns = SYNONYM_DICT.get(fact_stem, set())

        for m in NUM_UNIT_RE.finditer(text_lower):
            t_num = m.group(1)
            t_stem = _morph_stem(m.group(2))
            t_syns = SYNONYM_DICT.get(t_stem, set())
            same_stem = t_stem == fact_stem
            synonym_stem = t_stem in f_syns or fact_stem in t_syns or bool(t_syns & f_syns)
            if not (same_stem or synonym_stem):
                continue
            if t_num == _fact_num:
                # Same number — not a mismatch, keep scanning.
                continue
            # First stem-matching, number-mismatching occurrence → emit.
            return (m.start(), m.end(), "num_mismatch")
        return None
    except (re.error, AttributeError, TypeError):
        return None


# v0.8.2-B1 (reviewer-9): whitelist helper.
# Принимает только URL, которые canonicalize к одному из source_candidates.
# v0.8.2-B2 (reviewer-9): возвращает ОРИГИНАЛЬНЫЕ URL кандидатов (НЕ LLM raw).
# v0.8.2-B1 ранее возвращал LLM raw URL после canonical-match — это позволяло LLM
# контролировать финальную строку цитаты (utm, fragment, case). B2 фиксит: после
# whitelist matching citation URL всегда равен candidate.original (как хранится в
# source_candidates), чтобы LLM не мог «дописать» tracking/мусор в финальную ссылку.
def _filter_source_urls_to_candidates(
    raw_urls: list[str],
    source_candidates: list[dict],
) -> list[str]:
    """
    Returns subset of candidate URLs whose canonical form matches at least
    one LLM-emitted raw URL.

    Each accepted URL is the CANDIDATE'S ORIGINAL URL (the one actually
    stored in source_candidates, preserving its real query/fragment/case),
    NOT the LLM-emitted raw URL. This is critical for citation integrity:
    the LLM should not be able to inject tracking params, fragments, or
    case-variants into the final citation string. The LLM's URL is only
    used to authorize the match; the candidate's URL is what gets stored.

    Defensive rules:
      - Skip empty / non-string entries (in both raw_urls and candidates)
      - Skip URLs that don't parse (urlsplit fails)
      - Skip URLs whose scheme is not http/https
      - Skip URLs whose canonical form is empty
      - Preserve order of FIRST occurrence of each canonical in raw_urls;
        dedup within the result by canonical
    """
    if not raw_urls or not source_candidates:
        return []

    # Pre-compute canonical → candidate-original map (deduped; first wins).
    cand_canon_to_original: dict[str, str] = {}
    for c in source_candidates:
        c_url = c.get("url", "") if isinstance(c, dict) else ""
        if not c_url:
            continue
        try:
            cn = canonical_url(c_url)
        except Exception:  # noqa: S112 — defensive: skip malformed URL
            continue
        if not cn:
            continue
        # First candidate with this canonical wins (preserves the URL that
        # was actually fetched and stored in the search/fetch pipeline).
        cand_canon_to_original.setdefault(cn, c_url)

    if not cand_canon_to_original:
        return []

    accepted: list[str] = []
    seen: set[str] = set()
    for raw in raw_urls:
        if not isinstance(raw, str):
            continue
        u = raw.strip()
        if not u:
            continue
        # Parse to validate scheme
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(u)
        except Exception:  # noqa: S112 — defensive: skip unparsable
            continue
        if parts.scheme not in ("http", "https"):
            continue
        if not parts.netloc:
            continue
        try:
            cn = canonical_url(u)
        except Exception:  # noqa: S112 — defensive: skip unparsable
            continue
        if not cn or cn not in cand_canon_to_original:
            continue
        if cn in seen:
            continue
        seen.add(cn)
        # v0.8.2-B2: store the CANDIDATE'S ORIGINAL URL, not the LLM raw URL.
        accepted.append(cand_canon_to_original[cn])
        if len(accepted) >= 5:
            break
    return accepted


def verify_sources(
    top1: dict,
    other_sources: list[dict],
    query: str,
    *,
    use_llm: bool = True,
    max_facts: int = 10,
    time_range: str | None = None,
) -> dict:
    """
    4-level verification of top-1 source against other_sources.
    Returns verification dict для встраивания в out.
    time_range: используется как hint (пока только в meta для LLM, не в matching).
    """
    if not top1 or top1.get("error") or not top1.get("text"):
        return {
            "verified_facts": 0,
            "total_facts": 0,
            "verification_rate": 0.0,
            "verification_details": [],
            "llm_enhanced": False,
            "llm_verified_count": 0,
            "llm_weak_count": 0,  # v0.8.2-B1
            "llm_unlinked_refute_count": 0,  # v0.8.2-B1
            "llm_latency": 0.0,
            "llm_error": None,
        }

    top1_text = top1.get("text", "")
    # v0.8.3: pass query to enable query-aware fact ranking
    facts = _extract_facts(top1_text, max_facts=max_facts, query=query)
    if not facts:
        return {
            "verified_facts": 0,
            "total_facts": 0,
            "verification_rate": 0.0,
            "verification_details": [],
            "llm_enhanced": False,
            "llm_verified_count": 0,
            "llm_weak_count": 0,  # v0.8.2-B1
            "llm_unlinked_refute_count": 0,  # v0.8.2-B1
            "llm_latency": 0.0,
            "llm_error": None,
        }

    details = []
    verified_count = 0

    for fact in facts:
        # Negation detection (на top-1 + other_sources).
        # v0.8.3-C2-data: also capture the negation span in top-1 so we
        # can later render refuting markers. If the top-1 text does not
        # localise the negation, the helper returns None and we keep only
        # the negated=True bool (legacy behaviour).
        top1_neg_span = _find_negation_span(fact, top1_text)
        negated_in_top1 = top1_neg_span is not None
        refuting_sources = []  # sources where fact appears with negation
        # v0.8.3-C2-data: span-level refuting evidence. Parallel to
        # `refuting_sources` (URL-level), but carries offset+quote for
        # future refuting-span markers in answer_markdown. Empty when
        # no span can be localised — we never invent offsets.
        refuting_evidence_windows: list[dict] = []
        if top1_neg_span is not None:
            off_s, off_e, method = top1_neg_span
            refuting_evidence_windows.append(
                {
                    "source_url": top1.get("url", ""),
                    "quote": top1_text[off_s:off_e],
                    "offset_start": off_s,
                    "offset_end": off_e,
                    "method": method,
                }
            )

        # Match against each other source
        supporting_sources = []
        numeric_mismatch_sources = []  # skill 6.5: P0 numeric mismatch
        # v0.8.3-C2-data: span-level numeric-mismatch evidence. Same
        # contract as `refuting_evidence_windows`: parallel to the URL
        # list, populated only when a span can be localised honestly.
        numeric_mismatch_evidence_windows: list[dict] = []
        for src in other_sources:
            if src.get("error") or not src.get("text"):
                continue
            ok, method, score = _match_in_text(fact, src["text"])
            if ok:
                # Проверить refutation: fact в text с отрицанием?
                if _is_negated(fact, src["text"]):
                    refuting_sources.append(src.get("url", "?"))
                    # v0.8.3-C2-data: try to localise the negation span
                    # in this source. If found, append a window; if not,
                    # the URL-only entry is still kept (no fabrication).
                    neg_span = _find_negation_span(fact, src["text"])
                    if neg_span is not None:
                        off_s, off_e, neg_method = neg_span
                        refuting_evidence_windows.append(
                            {
                                "source_url": src.get("url", "?"),
                                "quote": src["text"][off_s:off_e],
                                "offset_start": off_s,
                                "offset_end": off_e,
                                "method": neg_method,
                            }
                        )
                elif method == "num_mismatch":
                    # Same stem, different number → count conflict.
                    # Do NOT count as support. Surface separately so the
                    # user / synthesis stage can see the contradiction.
                    # See audit 2026-06-07, section 5, P0.
                    numeric_mismatch_sources.append((src.get("url", "?"), score, method))
                    # v0.8.3-C2-data: try to localise the mismatching
                    # number/unit. Emit a window only if the helper can
                    # honestly point at the mismatching span.
                    num_span = _find_num_mismatch_span(fact, src["text"])
                    if num_span is not None:
                        off_s, off_e, num_method = num_span
                        numeric_mismatch_evidence_windows.append(
                            {
                                "source_url": src.get("url", "?"),
                                "quote": src["text"][off_s:off_e],
                                "offset_start": off_s,
                                "offset_end": off_e,
                                "method": num_method,
                            }
                        )
                else:
                    supporting_sources.append((src.get("url", "?"), score, method))

        # Verdict: SUPPORTS / REFUTES / CONFLICTING / INSUFFICIENT / NUMERIC_MISMATCH
        has_support = bool(supporting_sources)
        has_refutation = bool(refuting_sources) or negated_in_top1
        has_num_mismatch = bool(numeric_mismatch_sources)
        if has_support and not has_refutation and not has_num_mismatch:
            verdict = "SUPPORTS"
            verified = True
        elif has_refutation and not has_support:
            verdict = "REFUTES"
            verified = False
        elif has_support and has_refutation:
            verdict = "CONFLICTING"  # supporting + refuting sources
            verified = False
        elif has_num_mismatch and not has_support and not has_refutation:
            # Numeric mismatch alone: source disagrees on the count, but
            # does not negate the fact. Needs human review.
            verdict = "NUMERIC_MISMATCH"
            verified = False
        elif has_num_mismatch and (has_support or has_refutation):
            # Mixed: some sources support, others report different count.
            verdict = "CONFLICTING"
            verified = False
        else:
            verdict = "INSUFFICIENT"
            verified = False

        if verified:
            verified_count += 1

        details.append(
            {
                "fact": fact,
                "verdict": verdict,
                "verified": verified,  # legacy: True только для SUPPORTS
                "negated": negated_in_top1,
                "supporting_sources": supporting_sources,
                "refuting_sources": refuting_sources,
                "numeric_mismatch_sources": numeric_mismatch_sources,
                # v0.8.3-C2-data: span-level evidence for refuting and
                # numeric-mismatch sources. Always present (even when
                # empty) for downstream consumers — backward-compat is
                # preserved because `supporting_evidence_windows` is
                # also included (empty in this batch; out of scope).
                "supporting_evidence_windows": [],
                "refuting_evidence_windows": refuting_evidence_windows,
                "numeric_mismatch_evidence_windows": numeric_mismatch_evidence_windows,
                "method": supporting_sources[0][2] if supporting_sources else None,
            }
        )

    total = len(facts)
    rate = verified_count / total if total else 0.0

    # Conditional LLM-enhancement: если rate < 70% и есть unverified
    llm_enhanced = False
    llm_verified_count = 0
    llm_weak_count = 0  # v0.8.2-B1: SUPPORTS без valid source_urls → WEAK_SUPPORT (not counted as verified)
    llm_unlinked_refute_count = 0  # REFUTES без valid source_urls (recorded but not cited)
    llm_latency = 0.0
    llm_error = None  # v0.8.2 (Phase 4): exposed in return dict, not swallowed

    if use_llm and rate < LLM_VERIFY_THRESHOLD:
        unverified = [d for d in details if not d["verified"]]
        if unverified:
            # v0.8.2-B1 (reviewer-9): source_candidates — реально прочитанные sources.
            # LLM будет сравнивать факты ТОЛЬКО с ними, а его source_urls
            # пройдут whitelist через _filter_source_urls_to_candidates.
            llm_source_candidates = [
                {"url": s.get("url", "?"), "text": s.get("text", "")[:2000]}
                for s in other_sources
                if not s.get("error")
            ][:3]
            try:
                verifier = LLMVerifier()
                t0 = time.time()
                llm_results = verifier.verify_facts_batch(
                    facts=[d["fact"] for d in unverified],
                    source_candidates=llm_source_candidates,
                )
                llm_latency = round(time.time() - t0, 2)

                # Map back — apply v0.8.2-B1 source_urls whitelist.
                # SUPPORTS only counts as verified if LLM cited at least one
                # URL that canonicalizes to a real source_candidate.
                # Otherwise → WEAK_SUPPORT (verified=False, does NOT increment
                # verified_facts, does NOT increase verification_rate).
                for d, lr in zip(unverified, llm_results, strict=False):
                    verdict = lr.get("verdict")
                    raw_urls = lr.get("source_urls") or []
                    accepted_urls = _filter_source_urls_to_candidates(raw_urls, llm_source_candidates)
                    if verdict == "SUPPORTS":
                        if accepted_urls:
                            # Valid: LLM cited a real source. Upgrade to verified.
                            d["verified"] = True
                            d["verdict"] = "SUPPORTS"
                            d["method"] = "llm"
                            d["source_urls"] = accepted_urls
                            # supporting_sources заполняется ORIGINAL URLs (не canonical)
                            for u in accepted_urls:
                                d["supporting_sources"].append((u, 0.8, "llm+url"))
                            llm_verified_count += 1
                        else:
                            # v0.8.2-B1: SUPPORTS без valid source_urls → WEAK_SUPPORT.
                            # НЕ verified, НЕ llm_verified_count += 1.
                            d["verified"] = False
                            d["verdict"] = "WEAK_SUPPORT"
                            d["method"] = "llm"
                            d["source_urls"] = []
                            d["llm_error"] = "SUPPORTS без валидных source_urls"
                            llm_weak_count += 1
                    elif verdict == "REFUTES":
                        if accepted_urls:
                            # Cited refutation: записываем URL как refuting source
                            d["verdict"] = "REFUTES"
                            d["method"] = "llm"
                            d["source_urls"] = accepted_urls
                            for u in accepted_urls:
                                d["refuting_sources"].append(u)
                        else:
                            # v0.8.2-B1: REFUTES без valid source_urls — НЕ cited refutation.
                            # verdict остаётся REFUTES (LLM видел), но URL не записывается.
                            d["verdict"] = "REFUTES"
                            d["method"] = "llm"
                            d["source_urls"] = []
                            d["llm_error"] = "REFUTES без валидных source_urls"
                            llm_unlinked_refute_count += 1
                    # INSUFFICIENT or None → no change to d
                    # Propagate per-fact llm_error if set
                    if lr.get("llm_error") and not d.get("llm_error"):
                        d["llm_error"] = lr["llm_error"]

                # Recompute rate — WEAK_SUPPORT не увеличивает verified_count,
                # поэтому rate может не подняться. Это by design.
                verified_count = sum(1 for d in details if d["verified"])
                rate = verified_count / total if total else 0.0
                llm_enhanced = True
            except Exception as e:
                # v0.8.2 (Phase 4): track error instead of swallowing it (DR §13).
                # Caller will see this in llm_error field of the return dict.
                llm_error = f"{type(e).__name__}: {e}"

    return {
        "verified_facts": verified_count,
        "total_facts": total,
        "verification_rate": round(rate, 3),
        "verification_details": details,
        "llm_enhanced": llm_enhanced,
        "llm_verified_count": llm_verified_count,
        "llm_weak_count": llm_weak_count,  # v0.8.2-B1
        "llm_unlinked_refute_count": llm_unlinked_refute_count,  # v0.8.2-B1
        "llm_latency": llm_latency,
        "llm_error": llm_error,  # v0.8.2 (Phase 4)
    }


def deep_search(
    query: str,
    *,
    lang: str = "ru",
    time_range: str | None = None,
    top_n: int = 5,
    max_chars: int = MAX_CONTENT_CHARS,
) -> dict:
    """
    1. web_search() через SearXNG
    2. Берёт top_n URL (по порядку из SearXNG)
    3. Параллельно fetches
    4. Возвращает структуру с sources и confidence

    Returns:
        {
          "query": "...",
          "search_results": [...],
          "sources": [{url, title, text, length, fetch_dt, confidence}, ...],
          "stats": {fetched_ok, fetched_err, total_dt}
        }
    """
    t0 = time.time()
    res = web_search(query, lang=lang, time_range=time_range, max_results=top_n * 2)
    if not res:
        return {
            "query": query,
            "search_results": [],
            "sources": [],
            "stats": {"fetched_ok": 0, "fetched_err": 0, "total_dt": 0.0},
        }

    # Берём top_n URL — уникальных
    seen = set()
    urls_to_fetch = []
    for r in res:
        u = canonical_url(r.get("url", ""))
        if u and u not in seen and not u.startswith("javascript:"):
            seen.add(u)
            urls_to_fetch.append((u, r))
            if len(urls_to_fetch) >= top_n:
                break

    query_terms = _extract_query_terms(query)
    sources = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCH) as ex:
        futures = {ex.submit(fetch_url, u, max_chars=max_chars): (u, sr) for u, sr in urls_to_fetch}
        for fut in concurrent.futures.as_completed(futures):
            u, sr = futures[fut]
            fr = fut.result() or {"url": u, "error": "fetch returned None"}
            fr["snippet"] = sr.get("snippet", "")
            fr["engine"] = sr.get("engine", "")
            fr["source_score"] = _confidence(fr, query_terms)
            sources.append(fr)

    # Сортируем по source_score desc, потом по length desc (более длинный = более полный)
    sources.sort(key=lambda s: (-s.get("source_score", 0), -s.get("length", 0)))
    ok = sum(1 for s in sources if not s.get("error"))
    err = len(sources) - ok
    return {
        "query": query,
        "search_results": res,
        "sources": sources,
        "stats": {
            "fetched_ok": ok,
            "fetched_err": err,
            "total_dt": round(time.time() - t0, 2),
        },
    }


# === query reformulation (best practice: multi-lingual + reformulation) ===

REFORMULATORS = {
    "ru_to_en": {
        # Простые замены для теста. В проде — LLM-call для качества.
        "погода": "weather",
        "новости": "news",
        "атака": "attack",
        "мэр": "mayor",
        "уничтожен": "destroyed",
        "сбит": "shot down",
    }
}


def reformulate(query: str, source_lang: str = "ru", target_lang: str = "en") -> str | None:
    """
    Простая reformulation: машинный перевод через словарь + реконструкция.
    Это PLACEHOLDER — для production нужен LLM-call. Но для теста хватит.
    """
    if source_lang == target_lang:
        return None
    d = REFORMULATORS.get(f"{source_lang}_to_{target_lang}", {})
    out = query.lower()
    for k, v in d.items():
        out = out.replace(k, v)
    return out if out != query.lower() else None


def deep_research(
    query: str,
    *,
    lang: str = "ru",
    time_range: str | None = None,
    top_n: int = 4,
    max_chars: int = MAX_CONTENT_CHARS,
    alt_queries: list | None = None,
) -> dict:
    """
    Multi-query research:
    1. Original query (RU)
    2. Reformulated EN (если есть) — **DEPRECATED**, see `adapt_query` skill
    3. **NEW:** caller-provided alt_queries (from `query_adaptation.adapt_query`)
    4. Merge + dedup по URL
    5. Fetch top-K параллельно

    Args:
        query: Primary search query (≤10 words recommended; use adapt_query for long)
        lang: 'ru' or 'en'
        time_range: 'day' | 'week' | 'month' | 'year' | None
        top_n: number of top sources to return
        max_chars: max chars per source
        alt_queries: Optional list of 1-3 additional short queries (each 3-10 words)
            from `query_adaptation.adapt_query()['alt_queries']`. Each is run
            as separate search and results are merged by canonical URL.

    Returns структуру как deep_search, но с дополнительным
    "queries_used" и "all_search_results".
    """
    queries = [query]
    if alt_queries:
        # Each alt is a short query; limit to first 3 to avoid blowing up
        for alt in alt_queries[:3]:
            if isinstance(alt, str) and 1 <= len(alt.split()) <= 15 and alt != query:
                queries.append(alt)

    # Note: reformulate() is intentionally kept for backward compatibility
    # but is broken in current state (see ISSUES.md #013). Use adapt_query()
    # upstream and pass alt_queries instead.
    reformulated = reformulate(query, source_lang=lang, target_lang="en" if lang == "ru" else "ru")
    if reformulated:
        queries.append(reformulated)

    # Auto-infer time_range из keywords в запросе (если не задан явно)
    effective_time_range = time_range or infer_time_range(query)

    t0 = time.time()
    all_search = []
    seen_urls = set()
    sources_meta = {}  # url -> {engine, snippet, query}

    for q in queries:
        qlang = "en" if q == reformulated else lang
        res = web_search(q, lang=qlang, time_range=effective_time_range, max_results=top_n * 2)
        for rank, r in enumerate(res):
            all_search.append({**r, "_query": q, "_lang": qlang, "_rank": rank})
            raw_u = r.get("url", "")
            u = canonical_url(raw_u)
            if not u:
                continue
            # Always merge meta for this canonical URL, even on second hit
            # (DR-05062026(3) §4 — multi-query votes must aggregate).
            meta = sources_meta.setdefault(
                u,
                {
                    "engines": set(),
                    "queries": set(),
                    "ranks": [],
                    "snippets": [],
                    "titles": [],
                    "raw_urls": set(),
                },
            )
            meta["engines"].add(r.get("engine", ""))
            meta["queries"].add(q)
            meta["ranks"].append(rank)
            meta["snippets"].append(r.get("snippet", ""))
            meta["titles"].append(r.get("title", ""))
            meta["raw_urls"].add(raw_u)
            # Track first-seen for FIFO ordering of top_urls
            if u not in seen_urls:
                seen_urls.add(u)

    # Weighted sort: rank + coverage + engine_weight (вместо длины snippet)
    query_terms_for_rank = _extract_query_terms(query)
    all_search.sort(key=lambda r: -_search_result_score(r, query_terms_for_rank))

    # Берём top_n URL (canonical, чтобы матчить sources_meta keys).
    # DR-05062026(3) §4 — raw vs canonical inconsistency lost meta on lookup.
    top_urls = []
    seen = set()
    for r in all_search:
        u = canonical_url(r.get("url", ""))
        if u and u not in seen:
            seen.add(u)
            top_urls.append(u)
            if len(top_urls) >= top_n:
                break

    query_terms = _extract_query_terms(query)
    sources = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCH) as ex:
        futures = {ex.submit(fetch_url, u, max_chars=max_chars): u for u in top_urls}
        for fut in concurrent.futures.as_completed(futures):
            u = futures[fut]
            fr = fut.result() or {"url": u, "error": "fetch returned None"}
            meta = sources_meta.get(u, {})
            fr["found_by_engines"] = sorted(meta.get("engines", set()))
            fr["found_by_queries"] = sorted(meta.get("queries", set()))
            fr["search_votes"] = len(meta.get("engines", set())) + len(meta.get("queries", set()))
            fr["engine"] = (list(meta.get("engines", set())) or [""])[0]
            fr["search_snippet"] = (meta.get("snippets", [""]) or [""])[0]
            fr["title"] = fr.get("title") or (meta.get("titles", [""]) or [""])[0]
            # Provenance: original URL(s) before canonical_url() normalisation
            fr["raw_urls"] = sorted(meta.get("raw_urls", set()))
            # source_score (НЕ confidence — это не truth, это relevance)
            fr["source_score"] = _confidence(fr, query_terms)
            sources.append(fr)

    # Сортируем по source_score desc, потом по length desc (более длинный = более полный)
    sources.sort(key=lambda s: (-s.get("source_score", 0), -s.get("length", 0)))
    ok = sum(1 for s in sources if not s.get("error"))
    err = len(sources) - ok

    # Top-1 (highest source_score)
    top1 = sources[0] if sources else None
    top1_confidence = top1.get("source_score", 0.0) if top1 else 0.0

    # Verification (4-level + conditional LLM)
    other_sources = sources[1:] if top1 else []
    verification = (
        verify_sources(top1, other_sources, query, time_range=effective_time_range)
        if top1
        else {
            "verified_facts": 0,
            "total_facts": 0,
            "verification_rate": 0.0,
            "verification_details": [],
            "llm_enhanced": False,
            "llm_verified_count": 0,
            "llm_latency": 0.0,
            "llm_error": None,  # v0.8.2 (Phase 4)
        }
    )

    return {
        "query": query,
        "queries_used": queries,
        "all_search_results": all_search[:30],  # cap чтобы не раздувать
        "sources": sources,
        "top1": top1,
        "top1_confidence": top1_confidence,
        **verification,
        "stats": {
            "fetched_ok": ok,
            "fetched_err": err,
            "total_dt": round(time.time() - t0, 2),
            "unique_sources": len(sources_meta),
        },
    }


if __name__ == "__main__":
    import json

    print("=== deep_search smoke test ===")
    out = deep_search("БПЛА Москва 5 июня 2026", time_range="day", top_n=3)
    print(
        json.dumps(
            {k: v if k != "sources" else f"[{len(v)} sources]" for k, v in out.items()},
            ensure_ascii=False,
            indent=2,
        )
    )
    for s in out["sources"][:3]:
        print(f"\n--- {s['url']}")
        print(
            f"  confidence={s.get('confidence', 0):.2f}, length={s.get('length', 0)}, dt={s.get('fetch_dt', 0)}s"
        )
        print(f"  title: {s.get('title', '')[:100]}")
        if s.get("text"):
            print(f"  preview: {s['text'][:200]}")
        if s.get("error"):
            print(f"  ERROR: {s['error']}")
