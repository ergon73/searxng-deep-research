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
import urllib.request
import urllib.parse
import json
import ssl
import time
import re
import concurrent.futures
import trafilatura
from typing import Optional
from rapidfuzz import fuzz

# Импортируем существующие helpers. Требует PYTHONPATH=/opt/searxng
# или запуска из /opt/searxng. Hardcoded sys.path удалён — это плохая практика
# (см. DR-05062026(2).txt P0 #hardcoded-syspath).
from hermes_searxng import web_search
from llm_verifier import LLMVerifier

# === Canonical URL (v0.8) ===

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "ref", "ref_src",
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
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    query = urlencode([
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ])
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


# === Search-result ranking (v0.8) ===

ENGINE_WEIGHT = {
    "wikipedia": 0.85, "wikidata": 0.80,
    "bing": 0.75, "bing news": 0.85,
    "duckduckgo": 0.70, "duckduckgo news": 0.85,
    "github": 0.85, "stackoverflow": 0.80,
    "semantic scholar": 0.90,
    "arxiv": 0.85, "mojeek": 0.65, "presearch": 0.55,
    "brave": 0.70, "brave news": 0.80,
    "google": 0.75, "google news": 0.85,
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
MAX_CONTENT_CHARS = 8000      # ~2к токенов на источник
MAX_CONCURRENT_FETCH = 6      # параллельных запросов
MAX_FETCH_BYTES = 2_000_000   # 2 МБ cap per response

# Verification tuning
FUZZY_THRESHOLD = 75          # % similarity для fuzzy match
LLM_VERIFY_THRESHOLD = 0.7    # если verification_rate < 70% → подключаем LLM
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
    from urllib.parse import urlparse
    import ipaddress
    import socket
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


def fetch_url(url: str, *, timeout: float = TIMEOUT, max_chars: int = MAX_CONTENT_CHARS) -> Optional[dict]:
    """
    Fetch URL and extract main content.
    Returns {url, title, text, length, fetch_dt, error} или None если совсем плохо.
    """
    # SSRF guard
    if not _is_safe_fetch_url(url):
        return {
            "url": url, "title": "", "text": "", "length": 0,
            "fetch_dt": 0.0, "error": "blocked unsafe URL (SSRF guard)",
        }

    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_FETCH,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.7",
        })
        with _safe_urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("content-type", "")
            data = r.read(MAX_FETCH_BYTES + 1)
            if len(data) > MAX_FETCH_BYTES:
                data = data[:MAX_FETCH_BYTES]
            final_url = r.geturl()
            # После возможных redirect'ов снова проверить final URL
            if not _is_safe_fetch_url(final_url):
                return {"url": final_url, "title": "", "text": "", "length": 0,
                        "fetch_dt": round(time.time() - t0, 2),
                        "error": "blocked unsafe final URL (after redirect)"}
            if "html" not in ct and "xml" not in ct:
                return {"url": final_url, "title": "", "text": "", "length": 0,
                        "fetch_dt": round(time.time() - t0, 2), "error": f"non-html: {ct}"}

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
        return {"url": url, "title": "", "text": "", "length": 0,
                "fetch_dt": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {e}"}


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
    STOP = {"и", "в", "на", "с", "по", "о", "у", "для", "the", "a", "an", "in", "on", "of", "to", "is", "are", "was", "were"}
    words = re.findall(r"\w{3,}", query.lower())
    return [w for w in words if w not in STOP]


# === Fact extraction & 4-level verification (v0.7) ===

# Паттерны: числа, даты, имена собственные (capitalized words), ключевые слова
FACT_RE_NUM = re.compile(r"\b\d[\d\s.,]{0,15}\b")            # 123, 1 500, 12.5
FACT_RE_DATE = re.compile(
    r"\b("
    r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
    r")\b", re.IGNORECASE
)
FACT_RE_CAPS = re.compile(r"\b[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})*\b|\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b")
FACT_RE_NEG = re.compile(r"^(не|нет|без)\s+", re.IGNORECASE)

# v0.8.2 (Phase 3): Multi-word entities.
# Первое слово capitalized, остальные 1-3 — любые рус/eng слова >=3 chars.
# Catches: "Министерство обороны", "Пресс секретарь Белого дома", "Ministry of Defense".
# NB: разрешаем non-capitalized продолжение, потому что в середине предложения
# "обороны" после "Министерство" пишется со строчной. Стоимость: больше шума,
# но stop-words + max_facts ограничивают.
FACT_RE_ENTITY = re.compile(
    r"\b[А-ЯЁA-Z][а-яёa-z]{2,}(?:\s+(?:[А-ЯЁA-Z]?[а-яёa-z]{2,})){1,3}\b"
)
# Solo capitalized: v0.8.2 (Phase 3) — выключен по AC2 ("Министерство" одно не факт).
# Оставлен как escape hatch на будущее, но не используется в _extract_facts.
# Фильтрует "Python" (6), "Методы" (6), "Сегодня" (7), "Министерство" (12).
FACT_RE_ENTITY_SOLO = re.compile(r"(?!)")  # never matches


def _extract_facts(text: str, max_facts: int = 8, query: str = "") -> list[str]:
    """
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
        "это", "что", "как", "или", "для", "при", "его", "её", "их", "этот", "эта", "эти",
        "тот", "та", "те", "все", "весь", "однако", "также", "котор", "методы", "учимся",
        "сегодня", "вчера", "сейчас", "теперь", "также",
        "the", "and", "for", "with", "this", "that", "from", "into", "what", "how",
    }

    # Skip units: "123 item" — не факт, "1 год" — не самостоятельный факт
    # (включая plural и падежные формы)
    SKIP_NUM_UNITS = {
        "год", "года", "году", "годом", "годы", "лет",
        "item", "items", "line", "lines", "example", "examples",
        "код", "кода", "коды", "пример", "примеры", "примеров",
        "раз", "раза", "размер", "размера", "size", "sizes",
        "pixel", "pixels", "px",
        "msk", "мин", "минут", "сек", "секунд", "час", "часа", "часов",
        "hours", "minutes", "seconds",
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
    fact_re_num_ctx = re.compile(
        r"\b(\d{1,4})\s+([а-яёa-z]{3,})\b", re.IGNORECASE
    )
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
_NAV_WORDS = frozenset({
    "category", "file", "upload", "version", "block", "subcategory",
    "subcategories", "appearance", "flight", "current", "media", "wiki",
})


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
        "сегодня", "сейчас", "только что", "за сутки", "сегодняшний",
        "today", "now", "latest", "breaking", "just now", "this hour",
    ],
    "week": [
        "вчера", "на этой неделе", "за неделю", "этой неделе",
        "yesterday", "this week", "past week", "last week",
    ],
    "month": [
        "в этом месяце", "за месяц", "этом месяце", "текущий месяц",
        "this month", "past month", "last month",
    ],
    "year": [
        "в этом году", "в прошлом году", "за год", "этом году", "прошлый год",
        "this year", "last year", "past year",
    ],
}


def infer_time_range(query: str) -> Optional[str]:
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
    "дрона": "дрон", "дронов": "дрон", "дроны": "дрон", "дроне": "дрон", "дрону": "дрон", "дроном": "дрон",
    "беспилотника": "беспилотник", "беспилотников": "беспилотник", "беспилотники": "беспилотник",
    "бпла": "бпла",
    "сбито": "сбит", "сбиты": "сбит", "сбита": "сбит", "сбит": "сбит",
    "обнаружено": "обнаруж", "обнаружены": "обнаруж", "обнаружена": "обнаруж", "обнаружен": "обнаруж",
    "перехвачено": "перехвач", "перехвачены": "перехвач", "перехвачена": "перехвач", "перехвачен": "перехвач",
    "уничтожено": "уничтож", "уничтожены": "уничтож", "уничтожена": "уничтож", "уничтожен": "уничтож",
    "часа": "час", "часов": "час", "часы": "час", "часу": "час", "часом": "час",
    "минуты": "мин", "минут": "мин", "минуту": "мин", "минутой": "мин",
    # EN nouns (singular → plural-agnostic stem)
    "drone": "drone", "drones": "drone",
    "vehicle": "vehicle", "vehicles": "vehicle",
    "missile": "missile", "missiles": "missile",
    "attack": "attack", "attacks": "attack",
    "shot": "shot", "shots": "shot",
    "detected": "detect", "detects": "detect",
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

    # 2. Numeric morphology (v0.8.2 — Phase 3)
    #    "123 дрона" должен матчить "123 дронов" (same number, same stem) → num_morph.
    #    "123 дрона" vs "124 дрона" → num_mismatch (same stem, different number).
    #    This is a CRITICAL distinction for verification: a count mismatch
    #    is not "supports" — it is a contradiction that needs human review.
    #    See audit 2026-06-07, section 5, P0: numeric mismatch.
    norm = _normalize_num_unit(fact)
    if norm:
        fact_num, fact_stem = norm
        for m in NUM_UNIT_RE.finditer(text_lower):
            t_num, t_stem = m.group(1), _morph_stem(m.group(2))
            if t_stem == fact_stem:
                if t_num == fact_num:
                    # Same number + same stem → confident support.
                    return (True, "num_morph", 90)
                # Different number + same stem → CONFLICT, not support.
                # Return True for backwards-compatible signature, but
                # method='num_mismatch' signals verify_sources() to
                # classify this as NUMERIC_MISMATCH, not SUPPORTS.
                return (True, "num_mismatch", 85)
            # If fact_stem and t_stem are synonyms (e.g. "бпла" vs
            # "беспилотник"), still numeric-match with cross-stem check.
            t_norm = t_stem
            f_norm = fact_stem
            t_syns = SYNONYM_DICT.get(t_norm, set())
            f_syns = SYNONYM_DICT.get(f_norm, set())
            if t_norm in f_syns or f_norm in t_syns or (t_syns & f_syns):
                # Synonym stems + same number → num_morph (high confidence).
                if t_num == fact_num:
                    return (True, "num_morph", 90)
                # Synonym stems + different number → still a count conflict.
                return (True, "num_mismatch", 85)

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
        r"(?:^|[^а-яёa-z0-9])(не|нет|без|не\s+был[аи]?|ни|н[еи]\s+одного|ни\s+одного|ни\s+одной)\s+(?:\w+\s+){0,4}" + fact_esc + r"(?:\b|[^а-яёa-z0-9])",
        re.IGNORECASE,
    )
    if after.search(text_l):
        return True

    # 2. fact до "не/нет" (в одном предложении, до 3 слов между)
    before = re.compile(
        fact_esc + r"\w*\s+(?:\w+\s+){0,3}(не|нет|не\s+был[аи]?|ни\s+сбит|не\s+сбит|не\s+обнаружен|ни\s+одного)",
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


def verify_sources(
    top1: dict,
    other_sources: list[dict],
    query: str,
    *,
    use_llm: bool = True,
    max_facts: int = 10,
    time_range: Optional[str] = None,
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
            "llm_latency": 0.0,
            "llm_error": None,
        }

    details = []
    verified_count = 0

    for fact in facts:
        # Negation detection (на top-1 + other_sources)
        negated_in_top1 = _is_negated(fact, top1_text)
        refuting_sources = []  # sources where fact appears with negation

        # Match against each other source
        supporting_sources = []
        numeric_mismatch_sources = []  # skill 6.5: P0 numeric mismatch
        for src in other_sources:
            if src.get("error") or not src.get("text"):
                continue
            ok, method, score = _match_in_text(fact, src["text"])
            if ok:
                # Проверить refutation: fact в text с отрицанием?
                if _is_negated(fact, src["text"]):
                    refuting_sources.append(src.get("url", "?"))
                elif method == "num_mismatch":
                    # Same stem, different number → count conflict.
                    # Do NOT count as support. Surface separately so the
                    # user / synthesis stage can see the contradiction.
                    # See audit 2026-06-07, section 5, P0.
                    numeric_mismatch_sources.append(
                        (src.get("url", "?"), score, method)
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

        details.append({
            "fact": fact,
            "verdict": verdict,
            "verified": verified,  # legacy: True только для SUPPORTS
            "negated": negated_in_top1,
            "supporting_sources": supporting_sources,
            "refuting_sources": refuting_sources,
            "numeric_mismatch_sources": numeric_mismatch_sources,
            "method": supporting_sources[0][2] if supporting_sources else None,
        })

    total = len(facts)
    rate = verified_count / total if total else 0.0

    # Conditional LLM-enhancement: если rate < 70% и есть unverified
    llm_enhanced = False
    llm_verified_count = 0
    llm_latency = 0.0
    llm_error = None  # v0.8.2 (Phase 4): exposed in return dict, not swallowed

    if use_llm and rate < LLM_VERIFY_THRESHOLD:
        unverified = [d for d in details if not d["verified"]]
        if unverified:
            try:
                verifier = LLMVerifier()
                t0 = time.time()
                llm_results = verifier.verify_facts_batch(
                    facts=[d["fact"] for d in unverified],
                    source_candidates=[
                        {"url": s.get("url", "?"), "text": s.get("text", "")[:2000]}
                        for s in other_sources if not s.get("error")
                    ][:3],
                )
                llm_latency = round(time.time() - t0, 2)

                # Map back — use new verdict enum (DR §10)
                for d, lr in zip(unverified, llm_results):
                    verdict = lr.get("verdict")
                    if verdict == "SUPPORTS":
                        d["verified"] = True
                        d["verdict"] = "SUPPORTS"
                        d["method"] = "llm"
                        d["supporting_sources"].append(("llm_batch", 0, "llm"))
                        llm_verified_count += 1
                    elif verdict == "REFUTES":
                        d["verdict"] = "REFUTES"
                        d["method"] = "llm"
                        d["refuting_sources"].append("llm_batch")
                    # INSUFFICIENT or None → no change to d
                    # Propagate per-fact llm_error if set
                    if lr.get("llm_error"):
                        d["llm_error"] = lr["llm_error"]

                # Recompute rate
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
        "llm_latency": llm_latency,
        "llm_error": llm_error,  # v0.8.2 (Phase 4)
    }


def deep_search(
    query: str,
    *,
    lang: str = "ru",
    time_range: Optional[str] = None,
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
        return {"query": query, "search_results": [], "sources": [],
                "stats": {"fetched_ok": 0, "fetched_err": 0, "total_dt": 0.0}}

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


def reformulate(query: str, source_lang: str = "ru", target_lang: str = "en") -> Optional[str]:
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
    time_range: Optional[str] = None,
    top_n: int = 4,
    max_chars: int = MAX_CONTENT_CHARS,
    alt_queries: Optional[list] = None,
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
            meta = sources_meta.setdefault(u, {
                "engines": set(),
                "queries": set(),
                "ranks": [],
                "snippets": [],
                "titles": [],
                "raw_urls": set(),
            })
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
    verification = verify_sources(top1, other_sources, query, time_range=effective_time_range) if top1 else {
        "verified_facts": 0, "total_facts": 0, "verification_rate": 0.0,
        "verification_details": [], "llm_enhanced": False,
        "llm_verified_count": 0, "llm_latency": 0.0,
        "llm_error": None,  # v0.8.2 (Phase 4)
    }

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
    print(json.dumps({k: v if k != "sources" else f"[{len(v)} sources]" for k, v in out.items()}, ensure_ascii=False, indent=2))
    for s in out["sources"][:3]:
        print(f"\n--- {s['url']}")
        print(f"  confidence={s.get('confidence', 0):.2f}, length={s.get('length', 0)}, dt={s.get('fetch_dt', 0)}s")
        print(f"  title: {s.get('title', '')[:100]}")
        if s.get("text"):
            print(f"  preview: {s['text'][:200]}")
        if s.get("error"):
            print(f"  ERROR: {s['error']}")
