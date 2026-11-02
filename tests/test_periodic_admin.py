"""The /post_top admin command, the supergroup move and the job start."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
from telegram.warnings import PTBUserWarning

import main
import periodic_tops
from periodic_tops import POST_TIME, Period, marker_key, post_due_tops, post_due_tops_job

ADMIN = 1


def command(application, name):
    return next(
        handler
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler) and name in handler.commands
    )


@pytest.mark.parametrize(
    ('chat_type', 'user_id', 'allowed'), [('private', 1, True), ('private', 2, False), ('group', 1, False)]
)
def test_post_top_route_requires_private_administrator(chat_type, user_id, allowed):
    handler = command(main.build_application(), 'post_top')
    message = Message(
        1, datetime.now(UTC), Chat(10, chat_type), from_user=User(user_id, 'S', False), text='/post_top -1 2026-10'
    )
    assert bool(handler.filters.check_update(Update(1, message=message))) is allowed


def admin_context(args, bot_data=None):
    bot = SimpleNamespace(send_message=AsyncMock())
    application = SimpleNamespace(update_persistence=AsyncMock())
    return SimpleNamespace(
        args=args, bot=bot, bot_data=bot_data if bot_data is not None else {}, application=application
    )


def admin_update():
    return SimpleNamespace(effective_chat=Chat(ADMIN, 'private'), effective_user=User(ADMIN, 'A', False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'args',
    [
        [],
        ['-1'],
        ['x', '2026-10'],
        ['-1', '2026-13'],
        ['-1', '2026-10', 'now'],
        ['-1', '0000'],
        ['9223372036854775808', '2026'],
        ['-1', '2025', 'post'],
    ],
)
async def test_post_top_explains_usage_on_bad_arguments(monkeypatch, args):
    context = admin_context(args)
    await main.post_top(admin_update(), context)
    context.bot.send_message.assert_awaited_once_with(chat_id=ADMIN, text=main.POST_TOP_USAGE)


@pytest.mark.asyncio
async def test_post_top_preview_sends_the_messages_to_the_admin(monkeypatch):
    build = AsyncMock(return_value=[{'html': 'a', 'skip_entity_detection': True}])
    send = AsyncMock()
    monkeypatch.setattr(main, 'build_messages', build)
    monkeypatch.setattr(main, 'send_rich_message', send)
    context = admin_context(['-100', '2026-10'])

    await main.post_top(admin_update(), context)

    build.assert_awaited_once_with(context.bot, -100, Period('monthly', 2026, 10))
    send.assert_awaited_once_with(context.bot, ADMIN, {'html': 'a', 'skip_entity_detection': True})
    assert context.bot_data == {}


@pytest.mark.asyncio
async def test_post_top_preview_reports_an_empty_period(monkeypatch):
    monkeypatch.setattr(main, 'build_messages', AsyncMock(return_value=[]))
    context = admin_context(['-100', '2026'])
    await main.post_top(admin_update(), context)
    context.bot.send_message.assert_awaited_once_with(chat_id=ADMIN, text='Нет результатов за период')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['sent', 'skipped', 'removed', 'failed'])
async def test_post_top_force_posts_to_the_group_and_persists(monkeypatch, outcome):
    post = AsyncMock(return_value=outcome)
    monkeypatch.setattr(main, 'post_period', post)
    context = admin_context(['-100', '2026-10', 'post'])

    await main.post_top(admin_update(), context)

    post.assert_awaited_once_with(context.bot, context.bot_data, -100, Period('monthly', 2026, 10))
    context.application.update_persistence.assert_awaited_once()
    context.bot.send_message.assert_awaited_once_with(chat_id=ADMIN, text=main.POST_TOP_OUTCOMES[outcome])


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['to', 'from'])
async def test_migrate_chat_moves_results_group_and_markers(monkeypatch, direction):
    move = AsyncMock()
    monkeypatch.setattr(main, 'move_chat', move)
    old, new = -100, -1001000000100
    chat = Chat(old, 'group') if direction == 'to' else Chat(new, 'supergroup')
    message = Message(
        1,
        datetime.now(UTC),
        chat,
        migrate_to_chat_id=new if direction == 'to' else None,
        migrate_from_chat_id=old if direction == 'from' else None,
    )
    update = Update(1, message=message)
    application = main.build_application()
    persisted = AsyncMock()
    monkeypatch.setattr(type(application), 'update_persistence', persisted)
    context = ContextTypes.DEFAULT_TYPE(application)
    context.bot_data[marker_key('monthly', old, 'framed')] = '2026-10'

    await main.migrate_chat(update, context)

    move.assert_awaited_once_with(old, new)
    assert context.bot_data == {marker_key('monthly', new, 'framed'): '2026-10'}
    persisted.assert_awaited_once()


@pytest.mark.asyncio
async def test_migration_waits_for_an_in_flight_periodic_send(monkeypatch):
    old, new = -100, -1001000000100
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    sends = []
    send_started, release_send = asyncio.Event(), asyncio.Event()

    async def send(bot, chat_id, message):
        sends.append(chat_id)
        send_started.set()
        await release_send.wait()

    async def top_results(game, top_type, chat_id, *, rounds=None, min_rounds=10):
        return [SimpleNamespace(user_id=1, name='A', score=5)] if game == 'framed' else []

    async def enrich(bot, results, *, group_chat_id=None):
        return tuple(results)

    groups = AsyncMock(return_value=[SimpleNamespace(id=old)])
    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    monkeypatch.setattr(periodic_tops, 'top_results', top_results)
    monkeypatch.setattr(periodic_tops, 'enrich_top_profiles', enrich)
    monkeypatch.setattr(periodic_tops.Group, 'get_all', groups)
    monkeypatch.setattr(main, 'move_chat', AsyncMock())
    bot_data: dict[str, str] = {}
    # A lock that was contended in another test's event loop cannot be reused here.
    lock = asyncio.Lock()
    monkeypatch.setattr(periodic_tops, 'run_lock', lock)
    monkeypatch.setattr(main, 'run_lock', lock)
    context = SimpleNamespace(
        bot=AsyncMock(), bot_data=bot_data, application=SimpleNamespace(update_persistence=AsyncMock())
    )
    update = Update(1, message=Message(1, now, Chat(old, 'group'), migrate_to_chat_id=new))

    job = asyncio.create_task(post_due_tops(context.bot, bot_data, now))
    await send_started.wait()
    migration = asyncio.create_task(main.migrate_chat(update, cast(ContextTypes.DEFAULT_TYPE, context)))
    for _ in range(5):
        await asyncio.sleep(0)
    release_send.set()
    await asyncio.gather(job, migration)

    assert sends == [old]
    assert all(f':{old}:' not in key for key in bot_data)
    groups.return_value = [SimpleNamespace(id=new)]
    await post_due_tops(context.bot, bot_data, now)
    assert sends == [old]


def test_application_registers_the_migrate_handler():
    application = main.build_application()
    handlers = [handler for handlers in application.handlers.values() for handler in handlers]
    assert any(getattr(handler, 'callback', None) is main.migrate_chat for handler in handlers)


def test_start_periodic_tops_schedules_the_job(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        JobQueue,
        'run_daily',
        lambda self, callback, when, **kwargs: scheduled.append(('daily', callback, when, kwargs)),
    )
    monkeypatch.setattr(
        JobQueue, 'run_once', lambda self, callback, when, **kwargs: scheduled.append(('once', callback, when, kwargs))
    )
    application = main.build_application()

    main.start_periodic_tops(application)

    assert scheduled == [
        ('daily', post_due_tops_job, POST_TIME, {'name': 'periodic-tops'}),
        ('once', post_due_tops_job, 0, {'name': 'periodic-tops', 'job_kwargs': {'misfire_grace_time': None}}),
    ]
    assert POST_TIME.hour == 9 and POST_TIME.utcoffset() == UTC.utcoffset(None)


def test_chat_scope_is_the_chat_id_only_in_groups():
    assert main.chat_scope(Chat(-5, 'group')) == -5
    assert main.chat_scope(Chat(-1005, 'supergroup')) == -1005
    assert main.chat_scope(Chat(7, 'private')) is None
    assert main.chat_scope(Chat(-9, 'channel')) is None


@pytest.mark.asyncio
async def test_initialize_database_starts_periodic_tops_after_database_and_answers(monkeypatch):
    application = main.build_application()
    started = []
    monkeypatch.setattr(main, 'upgrade_database', AsyncMock(side_effect=lambda: started.append('database')))
    monkeypatch.setattr(main.ReactionDelivery, 'start', lambda self: started.append('reactions'))
    monkeypatch.setattr(main, 'start_answers', lambda app: started.append(('answers', app)))
    monkeypatch.setattr(main, 'start_periodic_tops', lambda app: started.append(('tops', app)))

    await main.initialize_database(application)

    assert started == ['database', 'reactions', ('answers', application), ('tops', application)]


def test_start_periodic_tops_without_a_job_queue():
    application = (
        ApplicationBuilder().application_class(main.ReactionApplication).token('123:ABC').job_queue(None).build()
    )
    assert isinstance(application, main.ReactionApplication)
    with pytest.warns(PTBUserWarning, match='No `JobQueue`'):
        main.start_periodic_tops(application)


@pytest.mark.asyncio
async def test_force_post_marks_each_game_even_when_period_was_already_posted(monkeypatch):
    message = {'html': '<p>Топ</p>', 'skip_entity_detection': True}
    build = AsyncMock(return_value=message)
    send = AsyncMock()
    monkeypatch.setattr(periodic_tops, '_build_game_message', build)
    monkeypatch.setattr(periodic_tops, 'send_rich_message', send)
    markers = {
        marker_key('monthly', -100, 'framed'): '2026-09',
        marker_key('monthly', -100, 'episode'): '2026-10',
    }
    context = admin_context(['-100', '2026-10', 'post'], markers)

    await main.post_top(admin_update(), context)

    assert [call.args[-1] for call in build.await_args_list] == ['framed', 'episode']
    assert [call.args for call in send.await_args_list] == [(context.bot, -100, message)] * 2
    assert context.bot_data == {
        marker_key('monthly', -100, 'framed'): '2026-10',
        marker_key('monthly', -100, 'episode'): '2026-10',
    }
    context.application.update_persistence.assert_awaited_once()
    context.bot.send_message.assert_awaited_once_with(chat_id=ADMIN, text='Отправлено')
