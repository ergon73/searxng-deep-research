"""
SSRF guard tests — _is_safe_fetch_url и SafeRedirectHandler.

Critical: regression на 169.254.169.254, localhost, private IP, IPv4-mapped IPv6.
"""
import pytest

from hermes_deepresearch import _is_safe_fetch_url


class TestIsSafeFetchUrl:
    """Tests for SSRF allowlist (ip.is_global)."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1",
        "http://127.0.0.1:8080/admin",
        "http://localhost",
        "http://localhost.localdomain/admin",
        "http://10.0.0.1",
        "http://10.255.255.255",
        "http://192.168.0.1",
        "http://192.168.255.255",
        "http://172.16.0.1",
        "http://172.31.255.255",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "http://[fe80::1]",
        "http://0.0.0.0",
    ])
    def test_blocks_private_loopback(self, url):
        assert _is_safe_fetch_url(url) is False, f"Should block: {url}"

    @pytest.mark.parametrize("url,expected", [
        ("file:///etc/passwd", False),
        ("ftp://example.com/", False),
        ("gopher://example.com/", False),
        ("ldap://example.com/", False),
        ("javascript:alert(1)", False),
    ])
    def test_blocks_non_http_schemes(self, url, expected):
        assert _is_safe_fetch_url(url) is expected

    @pytest.mark.parametrize("url", [
        # RFC 5737 reserved ranges are safe to use as "looks public" without
        # touching DNS or the public internet. They are NOT routable on the
        # public internet, so we cannot claim "should allow" for them; the
        # point of these tests is that _is_safe_fetch_url does DNS resolution
        # and checks ip.is_global.
        #
        # We use real public IPs (no DNS required) for the "allow" cases:
        #  - 93.184.216.34 — example.com (IANA reserved for documentation)
        #  - 1.1.1.1 — Cloudflare DNS
        #  - 142.251.46.110 — Google
        #  - 140.82.112.3 — GitHub
        #  - 2606:4700:4700::1111 — Cloudflare DNS (IPv6)
        "https://93.184.216.34/",                # example.com
        "https://1.1.1.1/",                      # Cloudflare
        "https://142.251.46.110/",               # Google
        "https://140.82.112.3/user/repo",        # GitHub
        "https://[2606:4700:4700::1111]/",       # Cloudflare (IPv6)
    ])
    def test_allows_public_sites(self, url):
        assert _is_safe_fetch_url(url) is True, f"Should allow: {url}"

    def test_unresolvable_hostname(self):
        # .invalid — RFC 2606 reserved TLD, guaranteed not to resolve.
        # No real DNS lookup will ever hit a real host.
        assert _is_safe_fetch_url("http://this-host-does-not-exist-12345.invalid/") is False

    def test_empty_hostname(self):
        """URL with no hostname must be blocked before any DNS lookup."""
        assert _is_safe_fetch_url("http:///path") is False

    def test_dns_failure_blocks(self, monkeypatch):
        """When DNS resolution fails (gaierror), the URL is unsafe."""
        import socket as _socket
        def _raise_gaierror(host, *args, **kwargs):
            raise _socket.gaierror(-2, "Name or service not known")
        monkeypatch.setattr("socket.getaddrinfo", _raise_gaierror)
        assert _is_safe_fetch_url("https://example.com/") is False

    def test_dns_resolves_to_private_ip_blocks(self, monkeypatch):
        """DNS-rebinding defence: if a public hostname resolves to a private IP,
        _is_safe_fetch_url must still block."""
        import socket as _socket
        def _fake_resolve(host, *args, **kwargs):
            return [(2, 1, 6, "", ("10.0.0.1", 0))]
        monkeypatch.setattr("socket.getaddrinfo", _fake_resolve)
        assert _is_safe_fetch_url("https://attacker.example.com/") is False
