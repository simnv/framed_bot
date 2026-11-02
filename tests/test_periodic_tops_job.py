"""Build, post and schedule periodic tops without database or Telegram calls."""

import asyncio
from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Bot
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes, JobQueue

import periodic_tops
from periodic_tops import Period, build_messages, post_due_tops, post_period, schedule
from top_type import TopType

CHAT, OTHER = -100, -200
OCTOBER = Period('monthly', 2026, 10)
NOVEMBER_START = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
YEAR_END = datetime(2026, 12, 31, 9, 0, tzinfo=UTC)


@pytest.fixture
def bot():
    return AsyncMock(spec=Bot)


@pytest.fixture
def queries(monkeypatch):
    data = SimpleNamespace(calls=[], populated={(CHAT, 'framed')})

    async def top_results(game, top_type, chat_id, *, rounds=None, min_rounds=10):
        data.calls.append((game, top_type, chat_id, rounds, min_rounds))
        if (chat_id, game) not in data.populated:
            return []
        return [SimpleNamespace(user_id=1, name='A <b>', score=5), SimpleNamespace(user_id=2, name='B', score=4)]

    async def enrich(bot, results, *, group_chat_id=None):
        assert group_chat_id in (CHAT, OTHER)
        return tuple(results)

    monkeypatch.setattr(periodic_tops, 'top_results', top_results)
    monkeypatch.setattr(periodic_tops, 'enrich_top_profiles', enrich)
    return data


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def send(bot, chat_id, message):
        await asyncio.sleep(0)
        messages.append((chat_id, message))

    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    return messages


@pytest.fixture
def groups(monkeypatch):
    values = [SimpleNamespace(id=CHAT), SimpleNamespace(id=OTHER)]
    monkeypatch.setattr(periodic_tops.Group, 'get_all', AsyncMock(return_value=values))
    return values


@pytest.mark.asyncio
async def test_build_messages_queries_all_metrics_with_chat_period_and_game_gate(bot, queries):
    messages = await build_messages(bot, CHAT, OCTOBER)

    assert len(messages) == 1
    assert '🏆 Framed — итоги октября 2026' in messages[0]['html']
    assert 'Раунды #1665–#1695, 1–31 октября' in messages[0]['html']
    assert 'A &lt;b&gt;' in messages[0]['html']
    assert messages[0]['html'].count('<table') == 4
    framed = [call for call in queries.calls if call[0] == 'framed']
    episode = [call for call in queries.calls if call[0] == 'episode']
    assert [call[1] for call in framed] == list(TopType)
    assert [call[1] for call in episode] == list(TopType)
    assert {call[2] for call in queries.calls} == {CHAT}
    assert {call[3] for call in framed} == {range(1665, 1696)}
    assert {call[4] for call in framed} == {10}
    # October has four Episode rounds; its average-frame gate is two.
    assert {call[4] for call in episode} == {2}
    assert await build_messages(bot, OTHER, OCTOBER) == []


@pytest.mark.asyncio
async def test_build_messages_keeps_framed_before_episode(bot, queries):
    queries.populated.add((CHAT, 'episode'))

    messages = await build_messages(bot, CHAT, OCTOBER)

    assert len(messages) == 2
    assert '🏆 Framed' in messages[0]['html']
    assert '🏆 Episode' in messages[1]['html']


@pytest.mark.asyncio
async def test_post_period_marks_sent_and_empty_games_separately(bot, queries, sent):
    bot_data: dict[str, str] = {}

    assert await post_period(bot, bot_data, CHAT, OCTOBER) == 'sent'
    assert [chat for chat, _ in sent] == [CHAT]
    assert bot_data == {
        'periodic_top:monthly:-100:framed': '2026-10',
        'periodic_top:monthly:-100:episode': '2026-10',
    }
    assert await post_period(bot, bot_data, OTHER, OCTOBER) == 'skipped'
    assert bot_data['periodic_top:monthly:-200:framed'] == '2026-10'
    assert bot_data['periodic_top:monthly:-200:episode'] == '2026-10'


@pytest.mark.asyncio
@pytest.mark.parametrize('first_game_empty', [False, True])
async def test_each_game_is_marked_before_the_next_game_is_sent(bot, queries, monkeypatch, first_game_empty):
    queries.populated.add((CHAT, 'episode'))
    if first_game_empty:
        queries.populated.remove((CHAT, 'framed'))
    bot_data: dict[str, str] = {}

    async def send(bot, chat_id, message):
        if '🏆 Episode' in message['html']:
            assert bot_data['periodic_top:monthly:-100:framed'] == '2026-10'

    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)

    assert await post_period(bot, bot_data, CHAT, OCTOBER) == 'sent'


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_game', ['Framed', 'Episode'])
async def test_failed_game_stays_unmarked_and_does_not_stop_the_other_game(bot, queries, monkeypatch, failed_game):
    queries.populated.add((CHAT, 'episode'))
    sent = []

    async def send(bot, chat_id, message):
        if f'🏆 {failed_game}' in message['html']:
            raise BadRequest('invalid message')
        sent.append(message)

    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    bot_data: dict[str, str] = {}

    assert await post_period(bot, bot_data, CHAT, OCTOBER) == 'failed'
    other_game = 'episode' if failed_game == 'Framed' else 'framed'
    assert bot_data == {f'periodic_top:monthly:{CHAT}:{other_game}': '2026-10'}
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_removed_chat_stops_delivery_without_marking_unsent_games(bot, queries, monkeypatch):
    queries.populated.add((CHAT, 'episode'))
    send = AsyncMock(side_effect=Forbidden('kicked'))
    remove = AsyncMock()
    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    monkeypatch.setattr(periodic_tops.Group, 'remove', remove)
    bot_data: dict[str, str] = {}

    assert await post_period(bot, bot_data, CHAT, OCTOBER) == 'removed'
    assert bot_data == {}
    send.assert_awaited_once()
    remove.assert_awaited_once_with(CHAT)


@pytest.mark.asyncio
async def test_direct_post_forces_both_games_and_keeps_newer_markers(bot, queries, sent):
    queries.populated.add((CHAT, 'episode'))
    bot_data = {
        'periodic_top:monthly:-100:framed': '2026-11',
        'periodic_top:monthly:-100:episode': '2026-11',
    }

    assert await post_period(bot, bot_data, CHAT, OCTOBER) == 'sent'
    assert len(sent) == 2
    assert set(bot_data.values()) == {'2026-11'}


@pytest.mark.asyncio
async def test_job_posts_october_at_first_start_but_never_the_2025_yearly_top(bot, queries, sent, groups):
    bot_data: dict[str, str] = {}

    await post_due_tops(bot, bot_data, NOVEMBER_START)
    calls = list(queries.calls)
    await post_due_tops(bot, bot_data, NOVEMBER_START)

    assert len(sent) == 1
    assert 'итоги октября 2026' in sent[0][1]['html']
    assert bot_data == {
        'periodic_top:monthly:-100:framed': '2026-10',
        'periodic_top:monthly:-100:episode': '2026-10',
        'periodic_top:monthly:-200:framed': '2026-10',
        'periodic_top:monthly:-200:episode': '2026-10',
    }
    assert queries.calls == calls


@pytest.mark.asyncio
async def test_job_posts_the_first_yearly_top_on_december_31(bot, queries, sent, groups):
    bot_data: dict[str, str] = {}

    await post_due_tops(bot, bot_data, datetime(2026, 12, 31, 8, 59, tzinfo=UTC))
    assert all(':yearly:' not in key for key in bot_data)
    await post_due_tops(bot, bot_data, YEAR_END)

    assert len(sent) == 2
    assert 'итоги ноября 2026' in sent[0][1]['html']
    assert 'итоги 2026 года' in sent[1][1]['html']
    assert bot_data['periodic_top:yearly:-100:framed'] == '2026'
    assert bot_data['periodic_top:yearly:-100:episode'] == '2026'


@pytest.mark.asyncio
async def test_job_retries_only_the_failed_game_next_run(bot, queries, sent, groups, monkeypatch):
    queries.populated.add((CHAT, 'episode'))
    fail = True
    attempts = []

    async def send(bot, chat_id, message):
        game = 'framed' if '🏆 Framed' in message['html'] else 'episode'
        attempts.append(game)
        if fail and game == 'framed':
            raise BadRequest('invalid message')

    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    bot_data: dict[str, str] = {}
    await post_due_tops(bot, bot_data, NOVEMBER_START)
    assert 'periodic_top:monthly:-100:framed' not in bot_data
    assert bot_data['periodic_top:monthly:-100:episode'] == '2026-10'

    queries.calls.clear()
    fail = False
    await post_due_tops(bot, bot_data, NOVEMBER_START)

    assert attempts == ['framed', 'episode', 'framed']
    assert {call[0] for call in queries.calls} == {'framed'}
    assert bot_data['periodic_top:monthly:-100:framed'] == '2026-10'


@pytest.mark.asyncio
async def test_job_stops_a_removed_group_but_posts_other_groups(bot, queries, sent, groups, monkeypatch):
    queries.populated.add((OTHER, 'framed'))
    remove = AsyncMock()
    monkeypatch.setattr(periodic_tops.Group, 'remove', remove)

    async def send(bot, chat_id, message):
        if chat_id == CHAT:
            raise Forbidden('kicked')
        sent.append((chat_id, message))

    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    bot_data: dict[str, str] = {}
    await post_due_tops(bot, bot_data, YEAR_END)

    assert [chat for chat, _ in sent] == [OTHER, OTHER]
    assert all(f':{CHAT}:' not in key for key in bot_data)
    remove.assert_awaited_once_with(CHAT)


@pytest.mark.asyncio
async def test_overlapping_job_runs_send_each_game_once(bot, queries, sent, groups):
    bot_data: dict[str, str] = {}
    await asyncio.gather(post_due_tops(bot, bot_data, NOVEMBER_START), post_due_tops(bot, bot_data, NOVEMBER_START))

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_job_flushes_markers_to_persistence(bot, queries, sent, groups, monkeypatch):
    persisted = []
    bot_data: dict[str, str] = {}

    async def persist():
        persisted.append(dict(bot_data))

    async def post(bot, data):
        await post_due_tops(bot, data, NOVEMBER_START)

    monkeypatch.setattr(periodic_tops, 'post_due_tops', post)
    context = Mock(
        spec=ContextTypes.DEFAULT_TYPE,
        bot=bot,
        bot_data=bot_data,
        application=SimpleNamespace(update_persistence=persist),
    )

    await periodic_tops.post_due_tops_job(context)

    assert len(persisted) == 1
    assert persisted[0] == bot_data
    assert persisted[0]['periodic_top:monthly:-100:framed'] == '2026-10'


def test_schedule_runs_daily_at_nine_utc_and_once_at_start():
    calls = []
    job_queue = Mock(
        spec=JobQueue,
        run_daily=lambda callback, when, **kwargs: calls.append(('daily', callback, when, kwargs)),
        run_once=lambda callback, when, **kwargs: calls.append(('once', callback, when, kwargs)),
    )

    schedule(job_queue)

    assert calls == [
        ('daily', periodic_tops.post_due_tops_job, time(9, 0, tzinfo=UTC), {'name': 'periodic-tops'}),
        (
            'once',
            periodic_tops.post_due_tops_job,
            0,
            {'name': 'periodic-tops', 'job_kwargs': {'misfire_grace_time': None}},
        ),
    ]
