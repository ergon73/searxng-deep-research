"""
Tests for redact module (skill 6.8: secret-redaction-release).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from redact import redact_secrets, redact_file, scan_paths


# Helper builders
def _secret(value):
    return "VAL_" + value


def _plain_secret():
    return "abcdef1234567890abcdef1234567890"


def _plain_secret2():
    return "bcdef1234567890abcdef1234567890z"


# === 1. Env-style KEY=VALUE ===

def test_redact_env_kv_form():
    s = "LLM_API_KEY" + "=" + _plain_secret()
    out = redact_secrets(s)
    assert _plain_secret() not in out
    assert out.startswith("LLM_API_KEY=")
    assert out.endswith("7890")


def test_redact_env_kv_form_with_quotes():
    s = "PROXY_PASS" + '="s3cretP@ssw0rd"'
    out = redact_secrets(s)
    assert "s3cretP@ssw0rd" not in out
    assert "w0rd" in out
    assert "PROXY_PASS" in out


def test_redact_env_kv_form_yaml_style():
    val = "longsecret" + "c" * 30 + "45"
    s = "SEARXNG_SECRET: '" + val + "'"
    out = redact_secrets(s)
    assert val not in out
    assert "SEARXNG_SECRET" in out


def test_redact_env_kv_form_does_not_touch_non_secret_key():
    s = "LOG_LEVEL=info"
    out = redact_secrets(s)
    assert out == s


# === 2. URL-embedded credentials ===

def test_redact_url_https_creds():
    s = "https://admin:hunter2@proxy.example.com:8080"
    out = redact_secrets(s)
    assert "hunter2" not in out
    assert "ter2" in out
    assert "admin" in out
    assert "proxy.example.com" in out


def test_redact_url_http_creds():
    s = "http://user:p@ssword@example.com/path"
    out = redact_secrets(s)
    assert "p@ssword" not in out
    assert "word" in out


def test_redact_url_ssh_creds():
    s = "ssh://git:token123@github.com/repo.git"
    out = redact_secrets(s)
    assert "token123" not in out
    assert "n123" in out


def test_redact_url_no_creds_passthrough():
    s = "see https://example.com/foo for docs"
    out = redact_secrets(s)
    assert out == s


# === 3. Telegram bot tokens ===

def test_redact_telegram_bot_token():
    s = "TELEGRAM_BOT_TOKEN" + "=" + "123456" + ":" + "AAEhBOweik6ad9JQBca8Fvk4rHt8xxxx"
    out = redact_secrets(s)
    assert "AAEhBOweik6ad9JQBca8Fvk4rHt8xxxx" not in out
    assert "TELEGRAM_BOT_TOKEN" in out


# === 4. Bearer tokens ===

def test_redact_bearer_authorization():
    s = "Authorization: Bearer " + "eyJhbG" + "abc" * 15 + "5c"
    out = redact_secrets(s)
    assert "eyJhbG" not in out
    assert "Bearer" in out


# === 5. Generic sk-/ghp_/etc. literals ===

def test_redact_openrouter_key():
    s = "OPENROUTER_KEY" + "=" + "sk-or-v1-" + _plain_secret()
    out = redact_secrets(s)
    assert _plain_secret() not in out
    assert "sk-or-v1-" in out


def test_redact_github_pat():
    s = "GH_TOKEN" + "=" + "ghp_AB" + "c" * 35 + "ghij"
    out = redact_secrets(s)
    assert "ghp_AB" not in out
    assert "ghij" in out


def test_redact_aws_key():
    s = "AWS_ACCESS_KEY_ID" + "=" + "AKIAIO" + "X" * 12 + "MPLE"
    out = redact_secrets(s)
    assert "AKIAIO" not in out


# === 6. Idempotency ===

def test_redact_idempotent():
    samples = [
        "LLM_API_KEY" + "=" + _plain_secret(),
        "https://user:secretpass@host.com",
        "ghp_" + "a" * 40,
    ]
    for s in samples:
        once = redact_secrets(s)
        twice = redact_secrets(once)
        assert once == twice


# === 7. Multiline ===

def test_redact_multiline_env_file():
    val1 = "a" * 32
    val2 = "b" * 16
    s = (
        "LLM_API_KEY=" + val1 + "\n"
        "PROXY_HOST=proxy.example.com\n"
        "PROXY_PASS=" + val2 + "\n"
        "LOG_LEVEL=info\n"
    )
    out = redact_secrets(s)
    assert val1 not in out
    assert val2 not in out
    assert "PROXY_HOST=proxy.example.com" in out
    assert "LOG_LEVEL=info" in out


# === 8. redact_file() ===

def test_redact_file_roundtrip(tmp_path):
    val = "x" * 32
    f = tmp_path / "test.env"
    f.write_text("LLM_API_KEY=" + val + "\nPROXY_HOST=example.com\n")
    out = redact_file(f)
    assert val not in out
    assert "PROXY_HOST=example.com" in out
    assert "LLM_API_KEY" in out


def test_redact_file_missing(tmp_path):
    f = tmp_path / "does_not_exist.env"
    out = redact_file(f)
    assert "cannot read" in out or "<redact_file" in out


# === 9. scan_paths() ===

def test_scan_paths_reports_redactions(tmp_path):
    val1 = "a" * 32
    val2 = "b" * 40
    f = tmp_path / "leaky.env"
    f.write_text(
        "LLM_API_KEY=" + val1 + "\n"
        "https://user:secretpass@host.com\n"
        "ghp_" + val2 + "\n"
        "LOG_LEVEL=info\n"
    )
    counts = scan_paths([str(f)])
    n = counts[str(f)]
    assert n >= 3, "Expected >=3 redactions, got " + str(n)


def test_scan_paths_handles_missing(tmp_path):
    target = tmp_path / "missing.env"
    counts = scan_paths([str(target)])
    assert counts[str(target)] == -1


# === 10. JSON structure preservation ===

def test_redact_json_value_preserves_keys():
    val = "abcdef" + "g" * 30
    s = '{"api_key": "sk-or-v1-' + val + '", "host": "example.com"}'
    out = redact_secrets(s)
    assert val not in out
    assert "sk-or-v1-" in out
    assert "host" in out
    assert "example.com" in out


# === 11. Negative cases ===

def test_redact_passthrough_safe_text():
    samples = [
        "This is a perfectly normal sentence.",
        "Расскажи подробно про Flutter и React Native.",
        "Visit https://example.com for more information.",
        "The user has 5 repos on GitHub.",
        "My favourite framework is Django.",
    ]
    for s in samples:
        out = redact_secrets(s)
        assert out == s, "Touched: " + repr(s) + " -> " + repr(out)


def test_redact_handles_empty_string():
    assert redact_secrets("") == ""


def test_redact_short_value_fully_hidden():
    s = "MY_TOKEN" + "=" + "abc"
    out = redact_secrets(s)
    assert "abc" not in out


# === 12. Already-redacted passthrough ===

def test_redact_passthrough_already_redacted():
    """Text that's already been redacted: the secret suffix doesn't grow.

    A real secret would be like 'LLM_API_KEY=mysecret1234'.
    After redact, it becomes 'LLM_API_KEY=...1234' (or shorter).
    Re-redacting should NOT change the visible suffix '...1234' to something
    else. We don't require byte-for-byte equality because the *replacement*
    pattern depends on the original length, but the last 4 chars of the
    visible secret portion must stay the same.
    """
    secret = "mysecret1234"
    already = "LLM_API_KEY" + "=" + "..." + secret
    out = redact_secrets(already)
    # The visible suffix (last 4 chars of the secret) must be preserved
    assert out.endswith(secret[-4:])
