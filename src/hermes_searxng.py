"""
hermes_searxng.py — локальный web_search для Hermes-агента.

Подключается к SearXNG-инстансу на 127.0.0.1:8888, отдаёт список результатов.
SearXNG сам ходит во внешние движки через per-engine proxy
(см. config/settings.yml — engines с proxy.disabled=false).

Использование в execute_code:
    from hermes_searxng import web_search, news_search
    hits = web_search("БПЛА Москва", time_range="day")
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

BASE = "http://127.0.0.1:8888"
UA = "hermes-bot/1.0 (+local searxng)"


@dataclass
class SearchResponse:
    """A search result plus the health metadata returned by SearXNG."""

    query: str
    hits: list[dict[str, Any]] = field(default_factory=list)
    responding_engines: tuple[str, ...] = ()
    unresponsive_engines: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    error: str | None = None
    elapsed_sec: float = 0.0

    @property
    def degraded(self) -> bool:
        """Whether this request lost search capacity or returned no evidence."""
        return bool(self.error or self.unresponsive_engines or not self.hits)


def _normalise_unresponsive(raw: Any) -> tuple[str, ...]:
    labels: set[str] = set()
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if isinstance(item, (list, tuple)) and item:
            engine = str(item[0])
            reason = str(item[1]) if len(item) > 1 and item[1] else ""
            labels.add(f"{engine}: {reason}" if reason else engine)
        elif isinstance(item, str) and item:
            labels.add(item)
    return tuple(sorted(labels))


def _normalise_hits(results: Any, max_results: int) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        return []
    out: list[dict[str, Any]] = []
    for result in results[:max_results]:
        if not isinstance(result, dict):
            continue
        raw_engines = result.get("engines")
        engines = (
            [str(engine) for engine in raw_engines if isinstance(engine, str) and engine]
            if isinstance(raw_engines, list)
            else []
        )
        engine = result.get("engine")
        if not engines and isinstance(engine, str) and engine:
            engines = [engine]
        if not isinstance(engine, str) or not engine:
            engine = engines[0] if engines else None
        out.append(
            {
                "engine": engine,
                "engines": engines,
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("content", ""),
                "score": result.get("score"),
                "category": result.get("category"),
                "published_date": result.get("publishedDate"),
            }
        )
    return out


# --- основной API ------------------------------------------------------


def web_search(
    query: str,
    *,
    lang: str = "ru",
    time_range: str | None = None,  # day | week | month | year
    engines: str | None = None,  # "google,bing,yandex"
    categories: str | None = None,  # "general,news,science"
    max_results: int = 10,
    timeout: float = 15.0,
    retries: int = 1,
    retry_sleep: float = 1.5,
    include_metadata: bool = False,
) -> list[dict] | SearchResponse:
    """
    По умолчанию возвращает совместимый список результатов. При
    `include_metadata=True` возвращает SearchResponse с фактическими
    движками, отказами и ошибкой транспорта.

    При пустом ответе делает до `retries` повторов.
    Примечание: клиент НЕ проксирует — SearXNG сам ходит во внешние
    движки через свой proxy (см. .env_proxy + per-engine proxies в settings.yml).
    """
    qs = {"q": query, "format": "json", "language": lang}
    if time_range:
        qs["time_range"] = time_range
    if engines:
        qs["engines"] = engines
    if categories:
        qs["categories"] = categories

    url = f"{BASE}/search?{urllib.parse.urlencode(qs)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})

    started = time.monotonic()
    last: dict[str, Any] = {}
    last_error: str | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                data = json.loads(r.read())
            last = data if isinstance(data, dict) else {}
            last_error = None
            if last.get("results") or attempt == retries:
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == retries:
                break
        time.sleep(retry_sleep)

    hits = _normalise_hits(last.get("results"), max_results)
    responding_engines = tuple(
        sorted(
            {
                engine
                for hit in hits
                for engine in hit.get("engines", [])
                if isinstance(engine, str) and engine
            }
        )
    )
    unresponsive = _normalise_unresponsive(last.get("unresponsive_engines"))
    raw_suggestions = last.get("suggestions")
    suggestions = (
        tuple(str(item) for item in raw_suggestions if isinstance(item, str) and item)
        if isinstance(raw_suggestions, list)
        else ()
    )
    response = SearchResponse(
        query=query,
        hits=hits,
        responding_engines=responding_engines,
        unresponsive_engines=unresponsive,
        suggestions=suggestions,
        error=last_error,
        elapsed_sec=round(time.monotonic() - started, 4),
    )
    return response if include_metadata else response.hits


def news_search(query: str, time_range: str = "day", max_results: int = 10, **kwargs) -> list[dict]:
    """Короткая обёртка: только новостные категории."""
    return web_search(
        query,
        time_range=time_range,
        categories="news",
        max_results=max_results,
        **kwargs,
    )


if __name__ == "__main__":
    for r in web_search("SearXNG docker", time_range="month", max_results=3):
        print(f"  [{r['engine']}] {r['title']}")
