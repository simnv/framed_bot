from datetime import date

import pytest

from answers import current_round, parse_game, round_limit


@pytest.mark.parametrize(
    ('game', 'today', 'expected'),
    [
        ('framed', date(2022, 3, 12), 1),
        ('framed', date(2022, 3, 13), 2),
        # Halloween (2018) is element 1694 of the real Framed bundle.
        ('framed', date(2026, 10, 31), 1695),
        ('episode', date(2022, 9, 5), 1),
        ('episode', date(2022, 9, 11), 1),
        ('episode', date(2022, 9, 12), 2),
        ('episode', date(2026, 10, 31), 217),
        ('framed', date(2022, 3, 11), 0),
    ],
)
def test_current_round_counts_from_round_one_date(game, today, expected) -> None:
    assert current_round(game, today) == expected


def test_round_limit_allows_one_round_of_slack() -> None:
    assert round_limit('framed', date(2026, 10, 31)) == 1696
    assert round_limit('episode', date(2026, 10, 31)) == 218


def test_parse_game() -> None:
    assert parse_game('framed') == 'framed'
    assert parse_game('episode') == 'episode'
    assert parse_game('Framed') is None
