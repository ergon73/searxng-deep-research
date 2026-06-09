# Release artifact — verification guide

Этот архив — reproducible release artifact проекта searxng-deep-research,
собранный через `release_packaging.build_release()`.

## Что внутри

- `src/` — production code
- `tests/` — pytest test suite
- `~/.hermes/skills/research/` — research skills (если включены)
- `~/.hermes/skills/security-review-python/` — security skills
- `AGENTS.md`, `pyproject.toml`, `README.md` — project metadata
- `.releaseignore` — exclusion list

## Что **НЕ** внутри

- `.env`, `.env.*` — секреты (NEVER included, hard rule)
- `__pycache__/`, `*.pyc` — bytecode
- `.git/` — VCS metadata
- `secrets/` — secret directory
- `build/`, `dist/` — build artifacts

## Verification (3 шага)

### 1. Проверить SHA256

```bash
# Из sidecar файла
sha256sum -c release-<date>.tar.gz.sha256

# Или вручную
sha256sum release-<date>.tar.gz
# Сравнить с содержимым .sha256 файла
```

### 2. Распаковать

```bash
mkdir verify && cd verify
tar -xzf ../release-<date>.tar.gz
cd <root_name>  # имя из manifest
```

### 3. Прогнать тесты

```bash
# Из clean temp dir (per portable-test-engineering)
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
PYTHONPATH=/path/to/unpacked/src python3 -m pytest /path/to/unpacked/tests/
# Ожидаем: 648/648 passed
```

## Reproducibility

Архив **deterministic**:
- mtime фиксирован на 2020-01-01 UTC
- uid/gid = 0/0
- entries sorted
- mode = 0o644

Два прогона на одном контенте → **identical SHA256**.

## Security guarantees

1. **No .env files** — enforced at collect time И post-pack scan
2. **In-memory redaction** — секреты в исходных файлах заменяются на
   `[REDACTED]` через `redact.redact_secrets()` ПЕРЕД записью в tar
3. **Post-pack safety scan** — ищем unredacted secret patterns
   (sk-or-v1-, sk-, AKIA, ghp_, xox[baprs]-) в tar contents
4. **No network access** — packer работает offline
5. **SHA256 sidecar** — внешняя верификация через `sha256sum -c`

## Файт манифеста

```json
{
  "tar_path": "/path/to/release.tar.gz",
  "sha256": "abc123...",
  "size_bytes": 12345,
  "file_count": 42,
  "file_list": ["src/foo.py", "tests/test_foo.py", ...],
  "created_at": "2026-06-07T08:24:00+00:00",
  "redacted": true,
  "release_name": "release-2026-06-07",
  "root_name": "searxng"
}
```

## Что делать если verification fail

1. SHA256 mismatch → архив повреждён, скачать заново
2. Тесты fail → среда отличается (зависимости, Python version)
3. Post-pack scan fail → `SecretLeakError` (НЕ должно случиться,
   если release собран через `release_packaging`)
