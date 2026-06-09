"""
Secret redaction utilities (skill 6.8: secret-redaction-release).

Provides `redact_secrets(text)` and `redact_file(path)` to scrub
secrets from any string or file before sending to chat, archiving,
or committing.

Spec: ~/.hermes/skills/security-review-python/secret-redaction-release/SKILL.md
Source audit: /tmp/hermes-recomendation-07062026.txt, sections 1.3 + 6.8

Design principles:
- Pure stdlib, no external deps (works inside archived project).
- Redaction is conservative: when in doubt, redact. False positives
  on common words are worse than leaking a real key.
- Output format: prefix + "..." + last 4 chars, e.g.
    sk-or-v1-abc123def456ghi789jkl012mno345pqr678stu → sk-or-v1-...pqr678stu
  (we keep the last 4 chars of the *secret portion*, not of the prefix).
- Always idempotent: redact(redact(x)) == redact(x).
- Never raises on unknown input — returns a best-effort redacted copy.

Hard rules (from audit):
- Never print raw .env.
- Never include real tokens in assertion output.
- Redact all keys before chat output.
- Before archive: scan for sk-, token, secret, password, proxy credentials.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

# ====================================================================
# Recognized secret prefixes
# ====================================================================
# Each entry: (prefix_str, is_prefix_in_key).
# `is_prefix_in_key=True` means the prefix appears as the START of the
# secret value (e.g. "sk-or-v1-abc..."). False means the prefix is a
# marker elsewhere in the value (e.g. a URL like "https://x-access-token:...").
#
# We redact the entire value but show the first chars of the prefix and
# the last 4 chars of the value, so the user can recognize which key
# was redacted without exposing it.

_PREFIX_PATTERNS: list[tuple[str, bool]] = [
    # OpenRouter
    ("sk-or-v1-", True),
    ("sk-or-", True),
    # OpenAI
    ("sk-", True),  # broad: sk-proj-, sk-svcacct-, sk-...
    # Anthropic
    ("sk-ant-", True),
    # GitHub
    ("ghp_", True),  # personal access token
    ("gho_", True),  # OAuth
    ("ghu_", True),  # user-to-server
    ("ghs_", True),  # server-to-server
    ("ghr_", True),  # refresh
    # Google API keys (broad)
    ("AIza", True),
    # Telegram bot token (always starts with digits, contains ':' separator)
    # We'll handle via dedicated regex below (variable prefix).
    # AWS access key
    ("AKIA", True),
    # Stripe
    ("sk_live_", True),
    ("sk_test_", True),
    ("pk_live_", True),
    ("pk_test_", True),
    ("rk_live_", True),
    # Slack
    ("xoxb-", True),
    ("xoxp-", True),
    ("xoxa-", True),
]


# ====================================================================
# Patterns
# ====================================================================

# Env-style "KEY=VALUE" where KEY suggests a secret and VALUE has content.
# Anchored at line start; allows leading whitespace; covers both:
#   LLM_API_KEY=***
#   "PROXY_PASS" = "s3cret123"
#   SEARXNG_SECRET: 'abc123'   (YAML-like — also matches)
#
# We match the key first, then the value (until end of line / quote / space).
_SECRET_KEY_NAME_PATTERNS = (
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|"  # noqa: S105  (regex patterns, not credentials)
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY|CLIENT[_-]?SECRET|"
    r"MTPROTO[_-]?SECRET|MTSecret|SECRET[_-]?KEY)"
)
# We accept: KEY=anything (no whitespace), KEY="...", KEY='...', KEY: ...
# Note: envname may have trailing _SUFFIX (e.g. AWS_ACCESS_KEY_ID),
# so we use a non-capturing optional suffix group after the secret marker.
_ENV_SECRET_RE = re.compile(
    r"(?P<key>(?P<envname>[A-Z_][A-Z0-9_]*)"
    r"(?:_|\.)?" + _SECRET_KEY_NAME_PATTERNS + r"(?:_[A-Z0-9_]+)?)"
    r"\s*[:=]\s*"
    r"(?:"  # optional quote
    r"\"(?P<dqval>[^\"\n]*)\""
    r"|'(?P<sqval>[^'\n]*)'"
    r"|(?P<bareval>[^\s\"'\n#]+)"
    r")",
    re.MULTILINE,
)

# URL with embedded credentials: scheme://user:password@host
_URL_CREDS_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+\-.]*://)"
    r"(?P<user>[^:@\s/]+)"
    r":(?P<password>[^@\s/]+)"
    r"@(?P<rest>[^\s\"'<>)]*)",
    re.IGNORECASE,
)

# Telegram bot token: <digits>:<base64-ish>
# Bot tokens are like 1234567890:AAEhBOweik6ad9JQB... (35+ chars after colon)
_TELEGRAM_BOT_RE = re.compile(
    r"(?P<prefix>\b\d{6,12}):(?P<token>[A-Za-z0-9_\-]{30,})",
)

# Bearer / Authorization header
_BEARER_RE = re.compile(
    r"(?P<scheme>[Bb]earer\s+)(?P<token>[A-Za-z0-9._\-+/=]{20,})",
)

# Generic sk-/gho_/etc. literal in text (not part of env-line)
# Must be word-bounded and reasonably long.
_LITERAL_PREFIX_RE = re.compile(
    r"\b(?P<prefix>sk-or-v1-|sk-ant-|sk-|ghp_|gho_|ghu_|ghs_|ghr_|"
    r"AIza[0-9A-Za-z_\-]{20,}|AKIA[0-9A-Z]{8,}|"
    r"sk_live_|sk_test_|pk_live_|pk_test_|rk_live_|"
    r"xoxb-|xoxp-|xoxa-)"
    r"(?P<secret>[A-Za-z0-9_\-]{8,})",
)


# ====================================================================
# Helpers
# ====================================================================


def _redact_value(value: str) -> str:
    """Return a redacted representation of a value, preserving prefix + last 4.

    For very short values (<= 4 chars), return '***' entirely.
    For empty values, return '***'.
    For values that are obviously the placeholder '***' or '****' already, return as-is.
    """
    if not value:
        return "***"
    if value.strip() in {"***", "****", "<redacted>", "REDACTED"}:
        return value
    if len(value) <= 4:
        return "***"
    return f"...{value[-4:]}"


def _redact_with_prefix(prefix: str, value: str) -> str:
    """Redact a value but show its prefix (e.g. 'sk-or-v1-') and last 4 chars.

    Example: ('sk-or-v1-', 'abcdef1234567890') -> 'sk-or-v1-...7890'
    """
    if not value:
        return f"{prefix}***"
    if len(value) <= 4:
        return f"{prefix}***"
    return f"{prefix}...{value[-4:]}"


def _has_secret_key_name(name: str) -> bool:
    """True if an env-var-like name looks like it stores a secret."""
    n = name.upper().replace(".", "_")
    # Split by underscore and look for any secret-y token
    parts = set(n.split("_"))
    secret_markers = {
        "KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "PASSWD",
        "PASS",
        "PRIVATE",
        "CREDENTIAL",
        "AUTH",
    }
    return bool(parts & secret_markers)


def _looks_like_url_creds(scheme: str) -> bool:
    """Whitelist of URL schemes where embedded credentials are a real risk.

    The `scheme` capture group in _URL_CREDS_RE includes the trailing '://',
    so we strip it before comparing. e.g. 'https://' -> 'https'.

    Strip order matters: '/', then ':', because 'https://' has both at
    the end and rstrip(':') will not touch the trailing '//'.
    """
    s = scheme.lower().rstrip("/").rstrip(":")
    return s in {"http", "https", "ftp", "ftps", "ssh", "git"}


# ====================================================================
# Public API
# ====================================================================


def redact_secrets(text: str) -> str:
    """Redact all recognized secrets in a text string.

    Applies (in order):
    1. URL-embedded credentials (https://user:pass@host).
    2. Env-style KEY=VALUE / "KEY": "VALUE" with secret-y key name.
    3. Telegram bot tokens (digits:base64).
    4. Bearer authorization tokens.
    5. Generic sk-/ghp_/etc. literals.

    Idempotent: redact_secrets(redact_secrets(x)) == redact_secrets(x).
    Never raises.
    """
    if not text:
        return text

    out = text

    # 1. URL-embedded credentials
    def _url_sub(m: re.Match) -> str:
        scheme = m.group("scheme")
        if not _looks_like_url_creds(scheme):
            return m.group(0)
        user = m.group("user")
        rest = m.group("rest")
        return f"{scheme}{user}:{_redact_value(m.group('password'))}@{rest}"

    out = _URL_CREDS_RE.sub(_url_sub, out)

    # 2. Env-style KEY=VALUE
    def _env_sub(m: re.Match) -> str:
        key = m.group("key")
        # Extract the value (whichever capture group is non-None)
        value = m.group("dqval") or m.group("sqval") or m.group("bareval") or ""
        quote = ""
        if m.group("dqval") is not None:
            quote = '"'
        elif m.group("sqval") is not None:
            quote = "'"
        return f"{key}={quote}{_redact_value(value)}{quote}"

    out = _ENV_SECRET_RE.sub(_env_sub, out)

    # 3. Telegram bot tokens
    def _tg_sub(m: re.Match) -> str:
        return f"{m.group('prefix')}:{_redact_value(m.group('token'))}"

    out = _TELEGRAM_BOT_RE.sub(_tg_sub, out)

    # 4. Bearer tokens
    def _bearer_sub(m: re.Match) -> str:
        return f"{m.group('scheme')}{_redact_value(m.group('token'))}"

    out = _BEARER_RE.sub(_bearer_sub, out)

    # 5. Generic prefixes (sk-, ghp_, etc.)
    def _literal_sub(m: re.Match) -> str:
        return _redact_with_prefix(m.group("prefix"), m.group("secret"))

    out = _LITERAL_PREFIX_RE.sub(_literal_sub, out)

    return out


def redact_file(path: str | Path) -> str:
    """Read a file and return its redacted contents.

    Useful before printing a config to chat or before including in
    an archive manifest.

    Handles binary safety: if the file looks binary (null bytes in
    first 1KB), return a placeholder '<binary file redacted>'.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except (OSError, FileNotFoundError) as e:
        return f"<redact_file: cannot read {p}: {e}>"
    if b"\x00" in raw[:1024]:
        return "<binary file redacted>"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return redact_secrets(text)


def scan_paths(paths: Iterable[str | Path]) -> dict[str, int]:
    """Scan a list of paths/files and report how many redacts were applied.

    Returns a dict {path: n_redactions}. n_redactions is the count of
    redaction patterns matched (rough metric).

    Used by pre-archive hooks to detect "this file has secrets, abort!".
    """
    results: dict[str, int] = {}
    for p_str in paths:
        p = Path(p_str)
        if p.is_dir():
            for child in p.rglob("*"):
                if child.is_file():
                    results[str(child)] = _count_redactions(child)
        elif p.is_file():
            results[str(p)] = _count_redactions(p)
        else:
            results[str(p)] = -1
    return results


def _count_redactions(path: Path) -> int:
    """Count redaction patterns that would match in this file."""
    try:
        raw = path.read_bytes()
    except (OSError, FileNotFoundError):
        return -1
    if b"\x00" in raw[:1024]:
        return 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return 0
    n = 0
    n += len(_URL_CREDS_RE.findall(text))
    n += len(_ENV_SECRET_RE.findall(text))
    n += len(_TELEGRAM_BOT_RE.findall(text))
    n += len(_BEARER_RE.findall(text))
    n += len(_LITERAL_PREFIX_RE.findall(text))
    return n


# ====================================================================
# CLI
# ====================================================================


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="redact",
        description="Redact secrets from a file or stdin. Output goes to stdout.",
    )
    p.add_argument("file", nargs="?", help="File to redact (default: stdin)")
    p.add_argument("--json", action="store_true", help="Parse input as JSON, redact values, output JSON")
    args = p.parse_args(argv)

    if args.file:
        out = redact_file(args.file)
    else:
        out = redact_secrets(sys.stdin.read())

    if args.json:
        try:
            json.loads(out)
            redacted = json.loads(redact_secrets(out))
            print(json.dumps(redacted, ensure_ascii=False, indent=2))
        except json.JSONDecodeError as e:
            print(f"<not valid JSON: {e}>", file=sys.stderr)
            return 1
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
