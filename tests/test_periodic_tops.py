"""Period math, post markers and group delivery of the periodic tops."""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

import periodic_tops
from answers import round_date
from periodic_tops import (
    Period,
    deliver,
    frame_gate,
    is_due,
    last_posted,
    latest_due,
    mark_posted,
    move_markers,
    parse_period,
)

CHAT = -1001390409915


def test_monthly_period_dates_rounds_and_text_match_the_spec_example():
    period = Period('monthly', 2026, 10)

    assert (period.id, period.start, period.end) == ('2026-10', date(2026, 10, 1), date(2026, 10, 31))
    assert period.due_at == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert period.rounds('framed') == range(1665, 1696)
    assert period.heading('framed') == '🏆 Framed — итоги октября 2026'
    assert period.rounds_line('framed') == 'Раунды #1665–#1695, 1–31 октября'
    episode = period.rounds('episode')
    assert round_date('episode', episode.start) >= date(2026, 10, 1) > round_date('episode', episode.start - 1)
    assert round_date('episode', episode.stop - 1) <= date(2026, 10, 31) < round_date('episode', episode.stop)
    assert period.heading('episode') == '🏆 Episode — итоги октября 2026'


def test_yearly_period_stops_at_december_30():
    period = Period('yearly', 2026)

    assert (period.id, period.start, period.end) == ('2026', date(2026, 1, 1), date(2026, 12, 30))
    assert period.due_at == datetime(2026, 12, 31, 9, 0, tzinfo=UTC)
    rounds = period.rounds('framed')
    assert round_date('framed', rounds.start) == date(2026, 1, 1)
    assert round_date('framed', rounds.stop - 1) == date(2026, 12, 30)
    assert period.heading('framed') == '🏆 Framed — итоги 2026 года'
    assert period.rounds_line('framed') == f'Раунды #{rounds.start}–#{rounds.stop - 1}, 1 января – 30 декабря'


def test_previous_and_parse():
    assert Period('monthly', 2026, 1).previous() == Period('monthly', 2025, 12)
    assert Period('yearly', 2026).previous() == Period('yearly', 2025)
    assert parse_period('2026-10') == Period('monthly', 2026, 10)
    assert parse_period('2026') == Period('yearly', 2026)
    assert [parse_period(text) for text in ('2026-13', '26-10', '2026-1', 'x', '')] == [None] * 5


@pytest.mark.parametrize(
    ('now', 'monthly', 'yearly'),
    [
        (datetime(2026, 11, 2, 12, 0, tzinfo=UTC), Period('monthly', 2026, 10), Period('yearly', 2025)),
        (datetime(2026, 11, 1, 9, 0, tzinfo=UTC), Period('monthly', 2026, 10), Period('yearly', 2025)),
        (datetime(2026, 11, 1, 8, 59, tzinfo=UTC), Period('monthly', 2026, 9), Period('yearly', 2025)),
        (datetime(2026, 12, 31, 9, 0, tzinfo=UTC), Period('monthly', 2026, 11), Period('yearly', 2026)),
        (datetime(2027, 1, 1, 9, 0, tzinfo=UTC), Period('monthly', 2026, 12), Period('yearly', 2026)),
    ],
)
def test_latest_due_is_the_newest_period_whose_post_time_has_passed(now, monthly, yearly):
    assert latest_due('monthly', now) == monthly
    assert latest_due('yearly', now) == yearly


@pytest.mark.parametrize(
    ('rounds', 'gate'), [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (19, 10), (20, 10), (31, 10), (52, 10)]
)
def test_frame_gate_is_half_the_rounds_rounded_up_at_most_ten(rounds, gate):
    assert frame_gate(rounds) == gate


def test_markers_track_the_latest_posted_period_per_chat_kind_and_game():
    bot_data: dict[str, str] = {}
    october, november = Period('monthly', 2026, 10), Period('monthly', 2026, 11)

    assert is_due(bot_data, 'monthly', CHAT, 'framed', october)
    mark_posted(bot_data, 'monthly', CHAT, 'framed', october)
    assert bot_data == {f'periodic_top:monthly:{CHAT}:framed': '2026-10'}
    assert not is_due(bot_data, 'monthly', CHAT, 'framed', october)
    assert is_due(bot_data, 'monthly', CHAT, 'framed', november)
    # A partial send: Framed is done, Episode is still due.
    assert is_due(bot_data, 'monthly', CHAT, 'episode', october)
    assert is_due(bot_data, 'yearly', CHAT, 'framed', Period('yearly', 2026))
    assert is_due(bot_data, 'monthly', -5, 'framed', october)
    # A forced post of an older period never lowers the marker.
    mark_posted(bot_data, 'monthly', CHAT, 'framed', november)
    mark_posted(bot_data, 'monthly', CHAT, 'framed', october)
    assert bot_data[f'periodic_top:monthly:{CHAT}:framed'] == '2026-11'
    assert last_posted(bot_data, 'monthly', CHAT, 'episode') is None


@pytest.mark.parametrize('game', ['framed', 'episode'])
@pytest.mark.parametrize('year', [2024, 2025])
def test_yearly_periods_before_the_first_year_are_never_due(game, year):
    assert not is_due({}, 'yearly', CHAT, game, Period('yearly', year))


@pytest.mark.parametrize('game', ['framed', 'episode'])
def test_first_yearly_top_becomes_due_at_the_2026_december_31_post_time(game):
    before = latest_due('yearly', datetime(2026, 12, 31, 8, 59, tzinfo=UTC))
    at_post_time = latest_due('yearly', datetime(2026, 12, 31, 9, 0, tzinfo=UTC))

    assert not is_due({}, 'yearly', CHAT, game, before)
    assert at_post_time == Period('yearly', 2026)
    assert is_due({}, 'yearly', CHAT, game, at_post_time)


def test_move_markers_renames_the_keys_of_one_chat():
    bot_data = {
        f'periodic_top:monthly:{CHAT}:framed': '2026-10',
        f'periodic_top:monthly:{CHAT}:episode': '2026-09',
        f'periodic_top:yearly:{CHAT}:framed': '2025',
        'other': 'x',
    }

    move_markers(bot_data, CHAT, -7)
    move_markers(bot_data, CHAT, -7)

    assert bot_data == {
        'periodic_top:monthly:-7:framed': '2026-10',
        'periodic_top:monthly:-7:episode': '2026-09',
        'periodic_top:yearly:-7:framed': '2025',
        'other': 'x',
    }


def test_move_markers_keeps_the_later_period_when_the_new_chat_has_a_marker():
    bot_data = {f'periodic_top:monthly:{CHAT}:framed': '2026-10', 'periodic_top:monthly:-7:framed': '2026-11'}

    move_markers(bot_data, CHAT, -7)

    assert bot_data == {'periodic_top:monthly:-7:framed': '2026-11'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('errors', 'outcome', 'removed', 'calls'),
    [
        ([], 'sent', False, 1),
        ([Forbidden('kicked')], 'removed', True, 1),
        ([BadRequest('Chat not found')], 'removed', True, 1),
        ([BadRequest('Group chat was deactivated')], 'removed', True, 1),
        ([BadRequest('other')], 'failed', False, 1),
        ([RetryAfter(1)], 'sent', False, 2),
        ([RetryAfter(6)], 'failed', False, 1),
        ([NetworkError('x'), NetworkError('x')], 'sent', False, 3),
        ([NetworkError('x'), NetworkError('x'), NetworkError('x')], 'failed', False, 3),
        ([TelegramError('x')], 'failed', False, 1),
    ],
)
async def test_deliver_follows_the_announce_rules(monkeypatch, errors, outcome, removed, calls):
    remove = AsyncMock()
    monkeypatch.setattr(periodic_tops.Group, 'remove', remove)
    monkeypatch.setattr(periodic_tops.asyncio, 'sleep', AsyncMock())
    attempts = []

    async def send():
        attempts.append(None)
        if len(attempts) <= len(errors):
            raise errors[len(attempts) - 1]

    assert await deliver(CHAT, send) == outcome
    assert len(attempts) == calls
    assert remove.await_count == (1 if removed else 0)
    if removed:
        remove.assert_awaited_once_with(CHAT)
