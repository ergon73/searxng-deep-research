"""
canonical_url() tests — strip utm_*, fbclid, default ports, fragment, lowercase.
"""
import pytest

from hermes_deepresearch import canonical_url


class TestCanonicalUrl:
    @pytest.mark.parametrize("inp,expected", [
        # Strip fragment
        ("https://site.ru/article#comments", "https://site.ru/article"),
        ("https://site.ru/article#section-2", "https://site.ru/article"),
        # Strip tracking params
        ("https://site.ru/article?utm_source=x", "https://site.ru/article"),
        ("https://site.ru/article?utm_source=x&utm_medium=email", "https://site.ru/article"),
        ("https://site.ru/article?fbclid=x&page=2", "https://site.ru/article?page=2"),
        ("https://site.ru/article?gclid=x", "https://site.ru/article"),
        ("https://site.ru/article?yclid=x", "https://site.ru/article"),
        # Lowercase host
        ("https://SITE.RU/article", "https://site.ru/article"),
        ("HTTP://Site.Ru/Article", "http://site.ru/Article"),
        # Strip default ports
        ("http://site.ru:80/article", "http://site.ru/article"),
        ("https://site.ru:443/article", "https://site.ru/article"),
        # Path: strip trailing /
        ("https://site.ru/article/", "https://site.ru/article"),
        # Preserve meaningful query params
        ("https://site.ru/search?q=python&page=2", "https://site.ru/search?q=python&page=2"),
        # Mixed
        ("HTTPS://SITE.RU:443/article?utm_source=x#frag", "https://site.ru/article"),
    ])
    def test_canonicalization(self, inp, expected):
        assert canonical_url(inp) == expected

    def test_root_path_preserved(self):
        """Корень всегда нормализуется к '/'."""
        # По RFC 3986 "https://site.ru" и "https://site.ru/" эквивалентны.
        # canonical_url нормализует к "https://site.ru/" как к канонической форме.
        assert canonical_url("https://site.ru/") == "https://site.ru/"
        assert canonical_url("https://site.ru") == "https://site.ru/"

    def test_preserves_meaningful_params(self):
        """Не стрипнуть реально нужные query params."""
        result = canonical_url("https://site.ru/article?id=123&lang=ru")
        assert "id=123" in result
        assert "lang=ru" in result
