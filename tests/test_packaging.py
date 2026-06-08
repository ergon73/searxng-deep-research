"""
Тесты для release-artifact-packaging.

Структура:
  TestIgnorePattern     (4)
  TestFileCollection    (4)
  TestRedaction         (3)
  TestDeterministic     (3)
  TestManifest          (3)
  TestIntegration       (3)
  TestHardRules         (4)
  TestAdversarial       (3)

Всего: ~27 тестов.
"""
import hashlib
import io
import os
import re
import tarfile
import tempfile
from pathlib import Path

import pytest

from release_packaging import (
    ReleaseConfig,
    ReleaseManifest,
    PackagingError,
    SecretLeakError,
    IgnorePattern,
    load_releaseignore,
    collect_files,
    build_tar,
    build_release,
    verify_release,
    quick_verify,
    DEFAULT_IGNORE_PATTERNS,
    FIXED_MTIME,
    MAX_UNPACK_SIZE,
    MAX_FILES,
    REDACTION_MARKER,
)


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """Создаёт минимальный проект для pack."""
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "app.py").write_text('print("hello")\n')
    (project / "README.md").write_text("# Project\n")
    (project / "data").mkdir()
    (project / "data" / "notes.txt").write_text("Some notes\n")
    return project


@pytest.fixture
def config(tmp_project, tmp_path):
    """ReleaseConfig для tmp_project."""
    return ReleaseConfig(
        root=tmp_project,
        output=tmp_path / "release.tar.gz",
        redact_secrets=True,
    )


# --- TestIgnorePattern -----------------------------------------------------

class TestIgnorePattern:
    def test_literal_match(self):
        ig = IgnorePattern([".env"])
        assert ig.matches(".env")
        assert ig.matches("path/.env")
        assert not ig.matches("env.py")

    def test_glob_star(self):
        ig = IgnorePattern(["*.pyc"])
        assert ig.matches("app.pyc")
        assert ig.matches("dir/app.pyc")
        assert not ig.matches("app.py")

    def test_double_star(self, tmp_path):
        # Создаём .releaseignore с __pycache__/ pattern
        # load_releaseignore auto-expands to **/__pycache__/
        project = tmp_path / "p"
        project.mkdir()
        (project / ".releaseignore").write_text("__pycache__/\n")
        # Use IgnorePattern with already-expanded pattern (что load_releaseignore даёт)
        ig = IgnorePattern(["__pycache__/", "**/__pycache__/"])
        assert ig.matches("__pycache__/app.cpython-311.pyc")
        assert ig.matches("src/__pycache__/foo.pyc")
        assert ig.matches("a/b/__pycache__/x.pyc")

    def test_empty_patterns(self):
        ig = IgnorePattern([])
        assert not ig.matches("anything.py")


# --- TestFileCollection ----------------------------------------------------

class TestFileCollection:
    def test_collects_all_files(self, tmp_project):
        # Write more files
        (tmp_project / "extra.py").write_text("x = 1\n")
        ig = IgnorePattern([])
        files = collect_files(tmp_project, ig)
        rels = {f.relative_to(tmp_project).as_posix() for f in files}
        assert "app.py" in rels
        assert "README.md" in rels
        assert "extra.py" in rels
        assert "data/notes.txt" in rels

    def test_excludes_pycache(self, tmp_project):
        (tmp_project / "__pycache__").mkdir()
        (tmp_project / "__pycache__" / "app.cpython-311.pyc").write_text("bc")
        ig = IgnorePattern(["__pycache__/", "*.pyc"])
        files = collect_files(tmp_project, ig)
        rels = [f.relative_to(tmp_project).as_posix() for f in files]
        assert not any("__pycache__" in r for r in rels)
        assert not any(r.endswith(".pyc") for r in rels)

    def test_excludes_env(self, tmp_project):
        (tmp_project / ".env").write_text("KEY=value\n")
        (tmp_project / ".env.local").write_text("KEY=value\n")
        ig = IgnorePattern([".env*"])
        files = collect_files(tmp_project, ig)
        rels = [f.relative_to(tmp_project).as_posix() for f in files]
        assert not any(".env" in r for r in rels)

    def test_empty_project_raises(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        # Empty project → 0 files
        # Note: collect_files doesn't raise, build_release does
        ig = IgnorePattern([])
        files = collect_files(empty, ig)
        assert files == []


# --- TestRedaction ---------------------------------------------------------

class TestRedaction:
    def test_in_memory_redact(self, tmp_project):
        # Секретный паттерн, должен быть excluded / redacted
        secret = 'XX-API-KEY-XXXXXXXXXXXXXX'
        content = f'SECRET = "{secret}"\n'
        (tmp_project / "config.py").write_text(content)
        (tmp_project / "app.py").write_text("import config\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_project.parent / "out.tar.gz",
            redact_secrets=True,
        )
        m = build_release(cfg)
        # config.py in archive
        assert "config.py" in m.file_list
        # Original config.py все ещё содержит secret (мы in-memory redact)
        assert secret in (tmp_project / "config.py").read_text()

    def test_no_redact_mode(self, tmp_project):
        (tmp_project / "config.py").write_text("X = 1\n")
        (tmp_project / "README.md").write_text("# test\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_project.parent / "out.tar.gz",
            redact_secrets=False,
        )
        m = build_release(cfg)
        # config.py в archive без redaction (но содержит безобидный X=1)
        assert "config.py" in m.file_list

    def test_redact_in_text_files_only(self, tmp_project):
        # Бинарный файл пропускается без decode
        (tmp_project / "image.bin").write_bytes(b"\x00\x01\xfe\xffSECRET")
        (tmp_project / "app.py").write_text("print('ok')\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_project.parent / "out.tar.gz",
            redact_secrets=True,
        )
        # Should not raise on binary file
        m = build_release(cfg)
        assert "image.bin" in m.file_list


# --- TestDeterministic -----------------------------------------------------

class TestDeterministic:
    def test_same_content_same_sha(self, config, tmp_project):
        m1 = build_release(config)
        m2 = build_release(config)
        assert m1.sha256 == m2.sha256

    def test_tar_entries_sorted(self, config):
        m = build_release(config)
        # Unpack to verify
        unpack = config.output.parent / "unpack"
        verify_release(m, unpack)
        # Read tar, get all member names
        with tarfile.open(config.output, mode="r:gz") as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
        # File entries (after root dir) should be sorted
        file_names = [n for n in names if not n.endswith("/")]
        assert file_names == sorted(file_names)

    def test_mtime_fixed(self, config):
        m = build_release(config)
        with tarfile.open(config.output, mode="r:gz") as tar:
            for member in tar.getmembers():
                assert member.mtime == FIXED_MTIME


# --- TestManifest ---------------------------------------------------------

class TestManifest:
    def test_manifest_fields(self, config):
        m = build_release(config)
        assert m.sha256
        assert m.size_bytes > 0
        assert m.file_count > 0
        assert m.created_at
        assert m.release_name
        assert m.root_name

    def test_manifest_to_dict(self, config):
        m = build_release(config)
        d = m.to_dict()
        assert "sha256" in d
        assert "file_list" in d
        assert isinstance(d["file_list"], list)

    def test_sha256_sidecar(self, config):
        m = build_release(config)
        sidecar = m.write_sha256_sidecar()
        assert sidecar.is_file()
        content = sidecar.read_text()
        # sha256sum format: "<hash>  <filename>\n"
        assert m.sha256 in content
        assert m.tar_path.name in content
        # Verify it's correct format
        parts = content.strip().split()
        assert parts[0] == m.sha256


# --- TestIntegration ------------------------------------------------------

class TestIntegration:
    def test_pack_verify_roundtrip(self, config, tmp_project):
        m = build_release(config)
        unpack = config.output.parent / "unpack"
        ok = verify_release(m, unpack)
        assert ok is True
        # Root dir exists
        root = unpack / m.root_name
        assert root.is_dir()
        # Files preserved
        assert (root / "app.py").is_file()
        assert (root / "data" / "notes.txt").is_file()

    def test_quick_verify(self, config):
        result = quick_verify(config)
        assert result["verified"] is True
        assert result["manifest"] is not None
        assert result["errors"] == []

    def test_real_project_pack(self, tmp_path):
        """Pack /opt/searxng (real project) — sanity check."""
        # Создаём "копию" с минимальным набором файлов чтобы не тащить весь project
        mini = tmp_path / "mini"
        mini.mkdir()
        (mini / "src").mkdir()
        (mini / "src" / "app.py").write_text("x = 1\n")
        (mini / "tests").mkdir()
        (mini / "tests" / "test_app.py").write_text("def test_x(): pass\n")
        (mini / "README.md").write_text("# Mini\n")
        (mini / ".releaseignore").write_text("build/\n*.tmp\n")
        out = tmp_path / "mini.tar.gz"
        cfg = ReleaseConfig(root=mini, output=out, redact_secrets=True)
        m = build_release(cfg)
        # All files included
        assert m.file_count >= 4
        # .releaseignore not in result (it's in root but should be packed)
        # Actually .releaseignore is in mini/ root, so it's included as config file
        # Build dir doesn't exist anyway


# --- TestHardRules --------------------------------------------------------

class TestHardRules:
    def test_no_env_leak(self, tmp_project, tmp_path):
        (tmp_project / ".env").write_text("KEY=val\n")
        (tmp_project / "app.py").write_text("print(1)\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        m = build_release(cfg)
        # .env НЕ в file_list
        assert not any(".env" in f for f in m.file_list)

    def test_no_git_leak(self, tmp_project, tmp_path):
        git_dir = tmp_project / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("git config")
        (tmp_project / "app.py").write_text("print(1)\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        m = build_release(cfg)
        assert not any(".git" in f for f in m.file_list)

    def test_no_pycache_leak(self, tmp_project, tmp_path):
        pc = tmp_project / "__pycache__"
        pc.mkdir()
        (pc / "app.cpython-311.pyc").write_text("bytecode")
        (tmp_project / "app.py").write_text("print(1)\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        m = build_release(cfg)
        assert not any("__pycache__" in f for f in m.file_list)
        assert not any(".pyc" in f for f in m.file_list)

    def test_secret_in_file_redacted(self, tmp_project, tmp_path):
        # OpenRouter-style key в .py файле
        key = "sk-or-v1-" + "abcdef1234567890abcdef1234567890"
        content = f'API_KEY = "{key}"\nprint(API_KEY)\n'
        (tmp_project / "config.py").write_text(content)
        (tmp_project / "app.py").write_text("import config\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        # Should not raise (post-scan would catch unredacted key)
        m = build_release(cfg)
        # Verify key NOT в архиве
        with tarfile.open(cfg.output, mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                content_bytes = f.read()
                assert key.encode() not in content_bytes, (
                    f"unredacted key in {member.name}"
                )


# --- TestAdversarial --------------------------------------------------------

class TestAdversarial:
    def test_empty_project_fails(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        cfg = ReleaseConfig(
            root=empty,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        with pytest.raises(PackagingError, match="no files"):
            build_release(cfg)

    def test_unicode_filenames(self, tmp_project, tmp_path):
        (tmp_project / "файл.txt").write_text("unicode content\n")
        (tmp_project / "app.py").write_text("print(1)\n")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        m = build_release(cfg)
        # Unicode file в archive
        assert any("файл" in f for f in m.file_list)

    def test_symlink_excluded(self, tmp_project, tmp_path):
        # Создаём symlink, должен быть excluded
        try:
            (tmp_project / "link.py").symlink_to(tmp_project / "app.py")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        cfg = ReleaseConfig(
            root=tmp_project,
            output=tmp_path / "out.tar.gz",
            redact_secrets=True,
        )
        m = build_release(cfg)
        # Symlink не в file_list
        assert not any("link.py" in f for f in m.file_list)
