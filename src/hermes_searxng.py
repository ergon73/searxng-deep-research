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

BASE = "http://127.0.0.1:8888"
UA = "hermes-bot/1.0 (+local searxng)"


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
) -> list[dict]:
    """
    Возвращает список dict'ов {title, url, content, engine}.

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

    last = []
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                data = json.loads(r.read())
            last = data.get("results", [])[:max_results]
            if last or attempt == retries:
                break
        except Exception:
            if attempt == retries:
                break
        time.sleep(retry_sleep)

    out = []
    for r in last:
        out.append(
            {
                "engine": r.get("engine"),
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
        )
    return out


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
