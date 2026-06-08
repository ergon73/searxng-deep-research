# Инструкция: локальный SearXNG-поиск для Hermes Agent

Поднимает приватный metasearch рядом с Hermes, доступный по `127.0.0.1:8888`, с JSON API. Residential-прокси опциональна — подключается только для CAPTCHA-движков.

## Что получится

- 12 поисковых движков в default (Bing, Google, Yandex, Mojeek, Presearch, Wikipedia, Wikidata, Arxiv, Brave через прокси, DuckDuckGo через прокси, Bing News, DuckDuckGo News)
- Помощник `web_search()` для Hermes — вызываешь из своего кода и получаешь нормализованные результаты
- Без внешних API-ключей, без лимитов на запросы, без логов
- **~250 МБ RAM**, 1.5 ГБ диска

## Что понадобится

- Linux VPS или домашний сервер с Docker
- Ubuntu 22.04+ (или любой с Docker)
- 3+ ГБ свободной RAM
- (Опционально) residential прокся — `pool.proxy.market` или аналог

## Шаг 1. Установка Docker

```bash
apt update
DEBIAN_FRONTEND=noninteractive apt install -y docker.io docker-compose-v2 curl jq openssl
systemctl enable --now docker
docker --version
docker compose version
```

## Шаг 2. Каталог

```bash
mkdir -p /opt/searxng/searxng
cd /opt/searxng
```

## Шаг 3. Прокси-креды (опционально, но рекомендую)

```bash
cat > /opt/searxng/.env_proxy <<'EOF'
PROXY_HOST=pool.proxy.market
PROXY_PORT=10000
PROXY_USER=your_proxy_user
PROXY_PASS=your_proxy_pass
EOF
chmod 600 /opt/searxng/.env_proxy
```

Если прокси нет — оставь файл пустым, движки будут идти напрямую (часть из них упрётся в CAPTCHA, но Bing/Google/Mojeek/Presearch всё равно работают).

## Шаг 3a. .env_llm (API ключ + SEARXNG_SECRET + LLM_MODEL)

```bash
# Скопируй пример и заполни реальные значения
cp /opt/searxng/.env_llm.example /opt/searxng/.env_llm
nano /opt/searxng/.env_llm
chmod 600 /opt/searxng/.env_llm
```

В `.env_llm` должно быть минимум три ключа:

```bash
LLM_API_KEY=sk-or-v1-...   # OpenRouter API key (https://openrouter.ai/keys)
LLM_MODEL=meta-llama/llama-3.1-8b-instruct:free  # или платная модель для critical use
SEARXNG_SECRET=...         # 64 hex chars; сгенерируй командой ниже
```

Сгенерируй свой `SEARXNG_SECRET` (это будет 64-символьный hex):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

⚠️ **Не** коммить `.env_llm` в git. **Не** вставляй чужой секрет — сгенерируй свой.

## Шаг 4. docker-compose.yml

```yaml
# /opt/searxng/docker-compose.yml
# Это reference-снимок; реальный файл собран с учётом .env_llm.
# Полный текущий compose — в /opt/searxng/docker-compose.yml, см. git status.
services:
  valkey:
    image: valkey/valkey:9-alpine
    container_name: searxng-valkey
    restart: unless-stopped
    command: ["valkey-server", "--save", "", "--appendonly", "no"]
    networks:
      - searxng-internal
    healthcheck:
      test: ["CMD", "valkey-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 3

  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    restart: unless-stopped
    depends_on:
      valkey:
        condition: service_healthy
    ports:
      - "127.0.0.1:8888:8080"
    volumes:
      # Host path overridable через SEARXNG_SETTINGS_PATH в .env_llm.
      - ${SEARXNG_SETTINGS_PATH:-./searxng/settings.yml}:/etc/searxng/settings.yml:ro
    environment:
      # Fail-fast если SEARXNG_SECRET не задан в .env_llm.
      - SEARXNG_SECRET=${SEARXNG_SECRET:?set SEARXNG_SECRET in .env_llm}
      - SEARXNG_BIND_ADDRESS=0.0.0.0
      - SEARXNG_PORT=8080
    env_file:
      - ./.env_llm
      - ./.env_proxy
    networks:
      - searxng-internal
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8080/healthz"]
      interval: 30s
      timeout: 10s
      retries: 3

networks:
  searxng-internal:
    driver: bridge
```

## Шаг 5. settings.yml

```yaml
# /opt/searxng/searxng/settings.yml
use_default_settings: true

general:
  instance_name: "hermes-local"
  privaccheck: 0
  contact_url: ""
  enable_metrics: false

server:
  bind_address: "0.0.0.0"
  port: 8080
  secret_key: "СЮДА_ВСТАВЬ_СВОЙ_СЕКРЕТ"
  base_url: false
  image_proxy: false
  default_locale: "ru"
  limiter: false
  public_instance: false

botlimitation:
  ip_limit: 0
  link_token: false

search:
  safe_search: 0
  autocomplete: ""
  default_lang: "ru"
  formats:
    - html
    - json

engines:
  # === primary web (без прокси) ===
  - name: bing
    engine: bing
    disabled: false
    shortcut: bi
    weight: 1.0
  - name: google
    engine: google
    disabled: false
    shortcut: go
    weight: 1.0
  - name: yandex
    engine: yandex
    disabled: false
    shortcut: yn
    weight: 1.0
  - name: presearch
    engine: presearch
    disabled: false
    shortcut: pre
    search_type: search
    categories: [general, web]
    weight: 1.2
    timeout: 6.0
  - name: presearch news
    engine: presearch
    disabled: false
    shortcut: pren
    search_type: news
    categories: [news]
    weight: 1.0
    timeout: 6.0
  - name: mojeek
    engine: mojeek
    disabled: false
    shortcut: mj
    weight: 1.2
    timeout: 6.0

  # === справочные ===
  - name: wikipedia
    engine: wikipedia
    disabled: false
    shortcut: wp
    weight: 1.2
    timeout: 5.0
  - name: wikidata
    engine: wikidata
    disabled: false
    shortcut: wd
    weight: 0.8
    timeout: 5.0
  - name: duckduckgo definitions
    engine: duckduckgo_definitions
    disabled: false
    shortcut: ddgd
    weight: 0.6

  # === научный ===
  - name: arxiv
    engine: arxiv
    disabled: false
    shortcut: arx
    weight: 1.5
    timeout: 6.0

  # === news ===
  - name: bing news
    engine: bing_news
    disabled: false
    shortcut: bin
    weight: 1.0

  # === CAPTCHA-движки через residential proxy ===
  - name: duckduckgo
    engine: duckduckgo
    disabled: false
    shortcut: ddg
    weight: 1.0
    timeout: 8.0
    proxies:
      http: http://USER:PASS@HOST:PORT
      https: http://USER:PASS@HOST:PORT
  - name: duckduckgo news
    engine: duckduckgo_news
    disabled: false
    shortcut: ddgn
    weight: 0.8
    timeout: 8.0
    proxies:
      http: http://USER:PASS@HOST:PORT
      https: http://USER:PASS@HOST:PORT
  - name: brave
    engine: brave
    disabled: false
    shortcut: br
    weight: 1.5
    timeout: 8.0
    proxies:
      http: http://USER:PASS@HOST:PORT
      https: http://USER:PASS@HOST:PORT

  # === мусор (отключены) ===
  - name: qwant
    engine: qwant
    disabled: true
    shortcut: qw
  - name: startpage
    engine: startpage
    disabled: true
    shortcut: stpg
  - name: startpage news
    engine: startpage_news
    disabled: true
    shortcut: stpn
  - name: startpage images
    engine: startpage_images
    disabled: true
    shortcut: stpi
  - name: yahoo
    engine: yahoo
    disabled: true
    shortcut: yh
  - name: yahoo news
    engine: yahoo_news
    disabled: true
    shortcut: yhn
  - name: wolframalpha
    engine: wolframalpha
    disabled: true
    shortcut: wa

  # === dev-движки, отключены по умолчанию ===
  - name: stackexchange
    engine: stackexchange
    disabled: true
    shortcut: stex
    weight: 1.5
  - name: github
    engine: github
    disabled: true
    shortcut: gh
    weight: 1.5
  - name: mdn
    engine: mdn
    disabled: true
    shortcut: mdn
    weight: 1.0

# Redis cache (modern SearXNG uses valkey)
valkey:
  url: "valkey://redis:6379/0"
  result_ttl: 240h

outgoing:
  pool_connections: 50
  pool_maxsize: 20
  request_timeout: 8.0
  enable_http2: false
  useragent_suffix: "hermes-local"

ui:
  static_path: ""
  templates_path: ""
  default_theme: simple
  theme_args:
    simple_style: auto
```

**Замени `USER:PASS@HOST:PORT`** в трёх движках (duckduckgo, duckduckgo news, brave) на свои прокси-креды, **или удали весь блок `proxies:`** у каждого, если прокси нет.

## Шаг 6. Запуск

```bash
cd /opt/searxng
docker compose up -d

# Проверить
sleep 3
docker compose ps
ss -ltn | grep 8888
# Ожидаемо: 127.0.0.1:8888

# Healthcheck
curl -s http://127.0.0.1:8888/healthz
# Должно вернуть "OK"
```

## Шаг 7. Тест JSON API

```bash
curl -fsS -A 'hermes-agent/1.0' \
  'http://127.0.0.1:8888/search?q=searxng&format=json' \
  | jq '{query, count: (.results | length), engines: ([.results[].engine] | unique), unresponsive_engines}'
```

Ожидаемый результат: `count` > 10, `unresponsive_engines: []`.

## Шаг 8. Helper для Hermes

```python
# /opt/searxng/hermes_searxng.py
import urllib.request, urllib.parse, json, ssl, time
from typing import Optional
from pathlib import Path

BASE = "http://127.0.0.1:8888"
UA = "hermes-bot/1.0"
_PROXY_ENV = Path("/opt/searxng/.env_proxy")


def _load_proxy() -> Optional[str]:
    if not _PROXY_ENV.exists():
        return None
    env = {}
    for line in _PROXY_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    if all(env.get(k) for k in ("PROXY_HOST", "PROXY_PORT", "PROXY_USER", "PROXY_PASS")):
        return f"http://{env['PROXY_USER']}:{env['PROXY_PASS']}@{env['PROXY_HOST']}:{env['PROXY_PORT']}"
    return None


PROXY_URL = _load_proxy()


def web_search(
    query: str,
    *,
    lang: str = "ru",
    time_range: Optional[str] = None,
    engines: Optional[str] = None,
    categories: Optional[str] = None,
    max_results: int = 10,
    timeout: float = 15.0,
    retries: int = 1,
) -> list[dict]:
    """Возвращает [{engine, title, url, snippet}, ...]."""
    qs = {"q": query, "format": "json", "language": lang}
    if time_range: qs["time_range"] = time_range
    if engines: qs["engines"] = engines
    if categories: qs["categories"] = categories

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
        time.sleep(1.5)

    return [{
        "engine": r.get("engine"),
        "title": r.get("title", ""),
        "url": r.get("url", ""),
        "snippet": r.get("content", ""),
    } for r in last]


def news_search(query: str, time_range: str = "day", max_results: int = 10, **kw) -> list[dict]:
    return web_search(query, time_range=time_range, categories="news", max_results=max_results, **kw)
```

## Шаг 9. Использование в агенте

```python
import sys
sys.path.insert(0, "/opt/searxng")
from hermes_searxng import web_search, news_search

# Web-поиск
hits = web_search("БПЛА Москва 5 июня 2026", time_range="day")
for h in hits:
    print(f"[{h['engine']}] {h['title']}\n  {h['url']}")

# Новости
news = news_search("Собянин ПВО")
```

## Обслуживание

```bash
# Логи
cd /opt/searxng && docker compose logs --no-color --tail=120 searxng

# Рестарт после правки конфига
cd /opt/searxng && docker compose restart searxng

# Обновление
cd /opt/searxng && docker compose pull && docker compose up -d

# Остановить
cd /opt/searxng && docker compose down
```

## Смена прокси-кредов

```bash
# 1. Поменять в /opt/searxng/.env_proxy
nano /opt/searxng/.env_proxy
# 2. Поменять в /opt/searxng/searxng/settings.yml (в трёх блоках proxies: у duckduckgo, duckduckgo news, brave)
# 3. Рестарт
cd /opt/searxng && docker compose restart searxng
```

## Troubleshooting

**Контейнер упал в loop-restart:**
```bash
cd /opt/searxng && docker compose logs --no-color --tail=30 searxng
```
- `Port could not be cast to integer value as '...'` → в `valkey:` URL неправильный порт, должно быть `valkey://redis:6379/0`
- `ModuleNotFoundError: yandex_news` → в новой версии SearXNG этот движок переименован, удали его из `engines:`
- `ambiguous shortcut` → у какого-то движка shortcut совпадает со встроенным (например `se`), замени на уникальный (`stex`, `stpg`)

**Все движки CAPTCHA-ят:** прокся не работает. Проверь:
```bash
curl -x http://USER:PASS@HOST:PORT https://api.ipify.org
```
Должен вернуть residential IP, не VPS-IP.

**SearXNG ничего не находит:** подожди 10-15 секунд после рестарта, идёт прогрев. Первый запрос — самый медленный.

## Что **не** делать

- Не открывай порт 8888 в мир (`127.0.0.1:8888` → `0.0.0.0:8888`) — без reverse proxy + TLS твой SearXNG отдаст логи всех запросов всему интернету
- Не пихай креды в `settings.yml` напрямую без `chmod 600` — они будут видны в `docker inspect`
- Не включай все 70+ движков одновременно — больше движков = больше параллельных запросов = больше шанс CAPTCHA от каждого
- Не считай SearXNG финальным авторитетом — он агрегатор, а не судья; первоисточники всегда проверяй руками

## Контрольный список после установки

- [ ] `curl http://127.0.0.1:8888/healthz` возвращает `OK`
- [ ] `ss -ltn | grep 8888` показывает только `127.0.0.1:8888`
- [ ] `python3 hermes_searxng.py` отдаёт 5+ результатов
- [ ] `docker stats searxng` показывает <300 МБ RAM
- [ ] `.env_proxy` имеет права `600`

Готово. Если что-то не работает — пришли `docker compose logs --tail=50 searxng` и описание, что увидел.
