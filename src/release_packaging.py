"""
packaging.py — release artifact packer (audit section 5 P1).

См. audit 2026-06-07, раздел 5 (Release hygiene), 8.6 (мой, не из ChatGPT),
и раздел 10 (шкала прогресса).

Что делает:
  Собирает source + tests + skills + audit docs в воспроизводимый .tar.gz:
    - Excludes .env*, __pycache__/, .git/, .pytest_cache/, secrets/
    - In-memory redaction через redact.redact_secrets() (defense in depth)
    - Deterministic tar: mtime=2020-01-01, uid=0, gid=0, sorted entries
    - Writes .sha256 manifest рядом с архивом
    - Self-verifiable: tar -xzf + pytest → green

Hard rules (release hygiene + 6.8 secret defense):
  1. .env* файлы НИКОГДА не включаются (даже в no-redact mode)
  2. Deterministic tar (mtime, uid, gid, sort, mode)
  3. No network access
  4. Idempotency: один контент → один SHA256
  5. Redaction reuses redact.py (single source of truth)
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from redact import redact_secrets


# --- public constants -------------------------------------------------------

# Deterministic mtime for reproducible tar (2020-01-01 UTC)
FIXED_MTIME = 1577836800  # 2020-01-01 00:00:00 UTC

# Default fixed mode (read-only, owner)
DEFAULT_FILE_MODE = 0o644

# Default redaction: re-use redact.py's REDACTED marker
REDACTION_MARKER = "[REDACTED]"

# Default .releaseignore patterns (always applied, даже если .releaseignore не найден)
DEFAULT_IGNORE_PATTERNS: list[str] = [
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
    "*.pyc",
    "*.pyo",
    ".env*",
    "secrets/",
    "build/",
    "dist/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".mypy_cache/",
    ".ruff_cache/",
    "*.egg-info/",
    ".tox/",
]

# Max unpacked size (defensive: 500 MB)
MAX_UNPACK_SIZE = 500 * 1024 * 1024

# Max files in archive (defensive: 10k files)
MAX_FILES = 10_000

# Max path length (defensive)
MAX_PATH_LEN = 512


# --- exceptions -------------------------------------------------------------

class PackagingError(Exception):
    """Базовая ошибка packaging."""


class SecretLeakError(PackagingError):
    """Обнаружен .env файл или secret в архиве после pack."""


# --- dataclasses ------------------------------------------------------------

@dataclass
class ReleaseConfig:
    """Конфигурация release artifact."""
    root: Path
    output: Path
    redact_secrets: bool = True
    include_tests: bool = True
    include_skills: bool = True
    include_audit: bool = True
    fixed_mtime: int = FIXED_MTIME
    release_name: str = ""  # e.g. "hermes-deepresearch-2026-06-07"

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.output = Path(self.output).resolve()
        if not self.release_name:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.release_name = f"release-{ts}"


@dataclass
class ReleaseManifest:
    """Метаданные release архива."""
    tar_path: Path
    sha256: str
    size_bytes: int
    file_count: int
    file_list: list[str]  # sorted
    created_at: str       # ISO 8601 UTC
    redacted: bool
    release_name: str
    root_name: str        # top-level dir name в архиве

    def to_dict(self) -> dict:
        return {
            "tar_path": str(self.tar_path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "file_list": self.file_list,
            "created_at": self.created_at,
            "redacted": self.redacted,
            "release_name": self.release_name,
            "root_name": self.root_name,
        }

    def write_sha256_sidecar(self) -> Path:
        """Write <tar_path>.sha256 sidecar file (sha256sum-compatible format)."""
        sidecar = self.tar_path.with_suffix(self.tar_path.suffix + ".sha256")
        content = f"{self.sha256}  {self.tar_path.name}\n"
        sidecar.write_text(content, encoding="utf-8")
        return sidecar


# --- gitignore-style ignore -------------------------------------------------

class IgnorePattern:
    """Минимальный gitignore-style matcher.

    Поддерживает:
      - dir patterns: ".git/" — match directory или любой файл внутри
      - glob patterns: "*.pyc" — match basename
      - literal: ".env" — match exact basename
      - "**/foo" — match anywhere (treated as recursive)

    НЕ поддерживает: negation ("!"), full gitignore semantics.
    Достаточно для release packaging.
    """

    def __init__(self, patterns: Iterable[str]):
        self._patterns = [self._compile(p) for p in patterns]

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        # Strip trailing slash (directory marker)
        p = pattern.rstrip("/")
        # Escape regex specials, then replace globs
        # Order: **, *, ?
        # ** → match any (including /)
        # * → match any non-/
        # ? → match any single non-/
        regex_parts = []
        i = 0
        while i < len(p):
            if p[i:i+3] == "**/":
                regex_parts.append(".*")
                i += 3
            elif p[i:i+3] == "**":
                regex_parts.append(".*")
                i += 2
            elif p[i] == "*":
                regex_parts.append("[^/]*")
                i += 1
            elif p[i] == "?":
                regex_parts.append("[^/]")
                i += 1
            elif p[i] == ".":
                regex_parts.append(r"\.")
                i += 1
            else:
                regex_parts.append(re.escape(p[i]))
                i += 1
        regex = "^" + "".join(regex_parts) + "($|/)"
        return re.compile(regex)

    def matches(self, relative_path: str, is_dir: bool = False) -> bool:
        """Возвращает True если path matches any pattern.

        relative_path: POSIX-style relative path (e.g. "src/foo.py")
        is_dir: True если это директория
        """
        for pat in self._patterns:
            if pat.match(relative_path):
                return True
            # Также проверяем basename для file patterns
            basename = relative_path.rsplit("/", 1)[-1]
            if pat.match(basename):
                return True
        return False


def load_releaseignore(root: Path) -> list[str]:
    """Загружает .releaseignore из root, fallback на defaults.

    Auto-adds **/ prefix для dir patterns без явного prefix'а,
    чтобы `__pycache__/` match'ил как top-level, так и nested dirs.
    """
    ignore_file = root / ".releaseignore"
    if ignore_file.is_file():
        patterns: list[str] = []
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    else:
        patterns = list(DEFAULT_IGNORE_PATTERNS)
    return _expand_dir_patterns(patterns)


def _expand_dir_patterns(patterns: list[str]) -> list[str]:
    """Добавляет **/ variant для dir patterns без префикса.

    Gitignore semantics: pattern без '/' матчит на любой глубине.
    Pattern с '/' (например `__pycache__/`) матчит только top-level.
    Мы хотим глобальный exclude → добавляем **/ вариант.
    """
    expanded: list[str] = []
    for p in patterns:
        expanded.append(p)
        # Если pattern выглядит как dir pattern (ends with /) и не имеет ** prefix
        if p.endswith("/") and not p.startswith("**/"):
            # Добавляем recursive variant
            stripped = p.rstrip("/")
            expanded.append(f"**/{stripped}/")
    return expanded


# --- file collection --------------------------------------------------------

def collect_files(
    root: Path,
    ignore: IgnorePattern,
) -> list[Path]:
    """Собирает все files под root, исключая ignore patterns.

    Возвращает: list[Path] — отсортированный по relative path.
    """
    if not root.is_dir():
        raise PackagingError(f"root is not a directory: {root}")

    files: list[Path] = []
    root_resolved = root.resolve()

    for path in sorted(root.rglob("*")):
        if not path.is_file() and not path.is_dir():
            continue
        # Skip symlinks (defense)
        if path.is_symlink():
            continue
        rel = path.relative_to(root_resolved).as_posix()
        is_dir = path.is_dir()
        if ignore.matches(rel, is_dir=is_dir):
            continue
        if path.is_file():
            files.append(path)

    if len(files) > MAX_FILES:
        raise PackagingError(
            f"too many files: {len(files)} > MAX_FILES={MAX_FILES}"
        )

    return files


# --- tar building -----------------------------------------------------------

def _build_tarinfo(
    name: str,
    size: int,
    fixed_mtime: int,
) -> tarfile.TarInfo:
    """Создаёт deterministic TarInfo."""
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = fixed_mtime
    info.mode = DEFAULT_FILE_MODE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def build_tar(
    files: list[Path],
    root_name: str,
    config: ReleaseConfig,
) -> bytes:
    """Собирает deterministic tar.gz в bytes.

    Args:
        files: отсортированный list of absolute Path
        root_name: top-level dir name в архиве
        config: ReleaseConfig (redact_secrets, fixed_mtime, etc.)

    Returns:
        bytes: содержимое tar.gz
    """
    if not files:
        raise PackagingError("no files to pack")

    # Sort by relative path (relative to file's own root)
    # files уже отсортированы по collect_files, но double-check
    files = sorted(files, key=lambda p: p.as_posix())

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for path in files:
            # Read file content
            try:
                content = path.read_bytes()
            except OSError as e:
                raise PackagingError(
                    f"failed to read {path}: {e}"
                ) from e

            # Apply redaction if enabled
            if config.redact_secrets:
                try:
                    text = content.decode("utf-8")
                    redacted = redact_secrets(text)
                    content = redacted.encode("utf-8")
                except UnicodeDecodeError:
                    # Бинарный файл — пропускаем redaction
                    pass

            # Compute relative path inside archive
            rel = path.relative_to(config.root).as_posix()
            archive_name = f"{root_name}/{rel}"
            if len(archive_name) > MAX_PATH_LEN:
                raise PackagingError(
                    f"path too long: {archive_name}"
                )

            # Add to tar
            info = _build_tarinfo(
                name=archive_name,
                size=len(content),
                fixed_mtime=config.fixed_mtime,
            )
            tar.addfile(info, io.BytesIO(content))

    return buf.getvalue()


# --- main API: build_release() ---------------------------------------------

def build_release(config: ReleaseConfig) -> ReleaseManifest:
    """Главная entry point: собирает release archive + manifest.

    Returns:
        ReleaseManifest с путями, SHA256, file list, metadata.

    Raises:
        PackagingError на любых failures.
        SecretLeakError если после pack обнаружены .env файлы.
    """
    config.output.parent.mkdir(parents=True, exist_ok=True)

    # 1. Load ignore patterns
    patterns = load_releaseignore(config.root)
    # Always add defense-in-depth: .env* NEVER included
    patterns = list(patterns) + [".env*", "**/.env*"]
    ignore = IgnorePattern(patterns)

    # 2. Collect files
    files = collect_files(config.root, ignore)

    if not files:
        raise PackagingError("no files to pack after filtering")

    # 3. Hard rule #1: ensure no .env files slipped through
    for f in files:
        rel = f.relative_to(config.root).as_posix()
        if f.name.startswith(".env") or "/.env" in rel or rel.endswith(".env"):
            raise SecretLeakError(f".env file in pack list: {rel}")

    # 4. Build tar
    root_name = config.root.name
    tar_bytes = build_tar(files, root_name, config)

    # 5. Write tar
    config.output.write_bytes(tar_bytes)

    # 6. Compute SHA256
    sha256 = hashlib.sha256(tar_bytes).hexdigest()

    # 7. Post-pack safety scan: check archive contents
    file_list = [f.relative_to(config.root).as_posix() for f in files]
    _post_pack_safety_scan(tar_bytes, file_list, root_name)

    # 8. Build manifest
    manifest = ReleaseManifest(
        tar_path=config.output,
        sha256=sha256,
        size_bytes=len(tar_bytes),
        file_count=len(files),
        file_list=sorted(file_list),
        created_at=datetime.now(timezone.utc).isoformat(),
        redacted=config.redact_secrets,
        release_name=config.release_name,
        root_name=root_name,
    )

    # 9. Write sha256 sidecar
    manifest.write_sha256_sidecar()

    return manifest


# --- safety scan ------------------------------------------------------------

def _post_pack_safety_scan(
    tar_bytes: bytes,
    file_list: list[str],
    root_name: str,
) -> None:
    """Проверяет архив на утечку секретов после pack.

    Raises:
        SecretLeakError если найдены .env файлы или unredacted secrets.
    """
    # 1. .env files in archive
    for rel in file_list:
        basename = rel.rsplit("/", 1)[-1]
        if basename.startswith(".env") or basename == "secrets":
            raise SecretLeakError(
                f"archive contains suspicious file: {rel}"
            )

    # 2. Scan tar contents for common secret patterns
    buf = io.BytesIO(tar_bytes)
    secret_patterns = [
        re.compile(rb"sk-or-v1-[A-Za-z0-9]{20,}"),  # OpenRouter
        re.compile(rb"sk-[A-Za-z0-9]{20,}"),        # OpenAI
        re.compile(rb"AKIA[0-9A-Z]{16}"),           # AWS access key
        re.compile(rb"ghp_[A-Za-z0-9]{30,}"),       # GitHub PAT
        re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    ]

    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            try:
                content = f.read()
            except Exception:
                continue
            for pat in secret_patterns:
                if pat.search(content):
                    raise SecretLeakError(
                        f"unredacted secret pattern in {member.name}"
                    )


# --- verify -----------------------------------------------------------------

def verify_release(
    manifest: ReleaseManifest,
    unpack_dir: Path,
) -> bool:
    """Верифицирует release: unpack + re-compute SHA256 + structural check.

    Args:
        manifest: ReleaseManifest (от build_release)
        unpack_dir: директория для распаковки

    Returns:
        True если verification passed.

    Raises:
        PackagingError на любой failure.
    """
    unpack_dir = Path(unpack_dir).resolve()
    unpack_dir.mkdir(parents=True, exist_ok=True)

    # 1. Re-compute SHA256 of tar
    actual_sha = hashlib.sha256(manifest.tar_path.read_bytes()).hexdigest()
    if actual_sha != manifest.sha256:
        raise PackagingError(
            f"SHA256 mismatch: expected {manifest.sha256}, got {actual_sha}"
        )

    # 2. Unpack
    with tarfile.open(manifest.tar_path, mode="r:gz") as tar:
        # Defense: check total uncompressed size
        total = sum(m.size for m in tar.getmembers())
        if total > MAX_UNPACK_SIZE:
            raise PackagingError(
                f"unpack size {total} > MAX_UNPACK_SIZE={MAX_UNPACK_SIZE}"
            )
        # Defense: check for path traversal
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in m.name:
                raise PackagingError(f"unsafe path in archive: {m.name}")
        tar.extractall(unpack_dir)

    # 3. Check root dir exists
    root_dir = unpack_dir / manifest.root_name
    if not root_dir.is_dir():
        raise PackagingError(
            f"unpacked root not found: {root_dir}"
        )

    return True


# --- self-test helpers ------------------------------------------------------

def quick_verify(
    config: ReleaseConfig,
    unpack_dir: Path | None = None,
) -> dict:
    """Pack + verify roundtrip. Удобно для smoke-testing.

    Returns:
        dict с keys: manifest, verified, errors
    """
    try:
        manifest = build_release(config)
        if unpack_dir is None:
            unpack_dir = config.output.parent / f"{config.release_name}-unpack"
        verify_release(manifest, unpack_dir)
        return {
            "manifest": manifest.to_dict(),
            "verified": True,
            "errors": [],
        }
    except Exception as e:
        return {
            "manifest": None,
            "verified": False,
            "errors": [f"{type(e).__name__}: {e}"],
        }


# --- public API re-exports --------------------------------------------------

__all__ = [
    "ReleaseConfig",
    "ReleaseManifest",
    "PackagingError",
    "SecretLeakError",
    "IgnorePattern",
    "load_releaseignore",
    "collect_files",
    "build_tar",
    "build_release",
    "verify_release",
    "quick_verify",
    "DEFAULT_IGNORE_PATTERNS",
    "FIXED_MTIME",
    "REDACTION_MARKER",
    "MAX_UNPACK_SIZE",
    "MAX_FILES",
]
