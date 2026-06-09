"""
infer_time_range() tests — fresh-сигналы побеждают год.
"""
import pytest
from hermes_deepresearch import infer_time_range


class TestInferTimeRange:
    @pytest.mark.parametrize("query,expected,description", [
        # Fresh wins over year (regression на баг)
        ("БПЛА Москва 5 июня 2026 сегодня", "day", "fresh day wins over year"),
        ("БПЛА 5 июня 2026 сейчас", "day", "now → day"),
        ("breaking news today", "day", "EN breaking → day"),
        # Year with preposition
        ("в 2020 году события", "year", "RU preposition"),
        ("in 2020 events", "year", "EN preposition"),
        ("за 2019 год", "year", "RU за"),
        ("during 2021", "year", "EN during"),
        # Week
        ("вчера news", "week", "RU yesterday → week"),
        ("yesterday events", "week", "EN yesterday → week"),
        ("this week summary", "week", "EN this week"),
        # Month
        ("в этом месяце", "month", "RU month"),
        ("this month recap", "month", "EN month"),
        # None
        ("обычный запрос", None, "no signal"),
        ("python asyncio", None, "no temporal kw"),
        # Edge: голая дата без preposition/fresh
        ("5 июня 2026", None, "голый день-месяц-год"),
    ])
    def test_inference(self, query, expected, description):
        result = infer_time_range(query)
        assert result == expected, f"{description}: expected {expected}, got {result} for '{query}'"
