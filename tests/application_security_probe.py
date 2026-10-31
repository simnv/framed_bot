"""Isolated real database probes using synthetic data, without Telegram networking."""
import asyncio
import json
import resource
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, inspect, insert, select, text
from sqlalchemy.exc import IntegrityError
from telegram import Chat, Message, Update, User as TelegramUser
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes, ApplicationBuilder, ExtBot

import main
import stats as stats_module
from models.db import Base, engine
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.group import Group
from models.migrate import BASELINE_TABLES, PartialSchemaError, alembic_config, upgrade_database
from models.user import User

USER_ID = 5_000_000_123


async def drop_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text('DROP TABLE IF EXISTS alembic_version'))


async def reset():
    await drop_schema()
    await upgrade_database()


async def seed_history():
    await User.update_from_tg_user(TelegramUser(USER_ID, 'Synthetic', False))
    async with engine.begin() as conn:
        for model in (FramedResult, EpisodeResult):
            await conn.execute(insert(model), [
                dict(user_id=USER_ID, framed_round=i, won=i % 2 == 0, win_frame=2 if i % 2 == 0 else None)
                for i in range(600)
            ])


async def profile():
    await seed_history()
    statements = []
    rows = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        rows.append(_cursor.rowcount)

    event.listen(engine.sync_engine, 'after_cursor_execute', observe)
    try:
        await User.update_from_tg_user(TelegramUser(USER_ID, 'Renamed', False, username='new_name'))
    finally:
        event.remove(engine.sync_engine, 'after_cursor_execute', observe)
    assert not any('framed_result' in sql or 'episode_result' in sql for sql in statements), \
        f'Profile update read game histories: returned rows={rows}'
    async with engine.connect() as conn:
        assert (await conn.execute(select(User.full_name, User.username).where(User.id == USER_ID))).one() == ('Renamed', 'new_name')
    profile_rows = list(rows)
    profile_statements = len(statements)
    rows.clear()
    statements.clear()
    event.listen(engine.sync_engine, 'after_cursor_execute', observe)
    try:
        user = await User.get(USER_ID)
        assert user is not None
    finally:
        event.remove(engine.sync_engine, 'after_cursor_execute', observe)
    assert sum(max(0, count) for count in rows) == 1, f'User lookup read game histories: {rows}'
    # Re-registration must be safe even when handlers overlap for a new account/group.
    await asyncio.gather(*(User.update_from_tg_user(TelegramUser(USER_ID + 1, 'Concurrent', False)) for _ in range(12)))
    await asyncio.gather(*(Group.update_from_tg_chat(Chat(-99, 'group', title='Concurrent')) for _ in range(12)))
    print(json.dumps({'profile_statements': profile_statements, 'profile_rows': profile_rows,
                      'history_read_rows': rows,
                      'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


async def duplicates():
    await User.update_from_tg_user(TelegramUser(USER_ID, 'Synthetic', False))
    await User.update_from_tg_user(TelegramUser(USER_ID + 1, 'Second', False))
    for model in (FramedResult, EpisodeResult):
        results = await asyncio.gather(*(model.save_result(USER_ID, 77, True, 2) for _ in range(12)))
        async with engine.connect() as conn:
            count = (await conn.execute(text(f'SELECT count(*) FROM {model.__tablename__} WHERE user_id=:id AND framed_round=77'), {'id': USER_ID})).scalar_one()
        assert sum(results) == count == 1, (model.__tablename__, sum(results), count)
        # Enforce the invariant even for future callers that bypass save_result.
        try:
            async with engine.begin() as conn:
                await conn.execute(insert(model).values(user_id=USER_ID, framed_round=77, won=False, win_frame=None))
        except IntegrityError:
            pass
        else:
            raise AssertionError(f'{model.__tablename__} has no database uniqueness boundary')
        assert await model.save_result(USER_ID, 78, False, None)
        assert await model.save_result(USER_ID, 79, True, 1)
        assert await model.save_result(USER_ID + 1, 77, True, 3)
        print(f'{model.__tablename__}: 12 concurrent saves, 1 success, 1 stored row; direct duplicate rejected')


async def aggregate_stats():
    await seed_history()
    statements, fetched = [], []

    def observe(_conn, cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        fetched.append(cursor.rowcount)

    event.listen(engine.sync_engine, 'after_cursor_execute', observe)
    try:
        # Exercise the real /stats handler, not only the aggregate helper.
        sent = []

        async def do_api_request(_bot, endpoint, api_kwargs, return_type=None):
            assert endpoint == 'sendRichMessage'
            sent.append(api_kwargs)
            return SimpleNamespace(id=19)

        message = Message(17, datetime.now(UTC), Chat(123, 'group'), from_user=TelegramUser(USER_ID, 'Synthetic', False), text='/stats')
        application = ApplicationBuilder().token('123:synthetic-only').job_queue(None).build()
        context = ContextTypes.DEFAULT_TYPE(application)
        with patch.object(ExtBot, 'do_api_request', do_api_request):
            await main.stats(Update(1, message=message), context)
    finally:
        event.remove(engine.sync_engine, 'after_cursor_execute', observe)
    assert len(sent) == 1
    rich = sent[0]['rich_message']
    assert rich['skip_entity_detection'] is True
    for game in ('framed.wtf', 'episode.wtf'):
        assert f'<td><a href="https://{game}/">{game}</a></td><td>600</td><td>300</td><td>2.00</td>' in rich['html']
    assert sum(max(0, count) for count in fetched) <= 3, f'/stats materialized history rows: {fetched}'
    assert len(statements) <= 3
    assert await stats_module.get_user_stats(USER_ID + 1) is None
    await User.update_from_tg_user(TelegramUser(USER_ID + 1, 'Empty', False))
    assert await stats_module.get_user_stats(USER_ID + 1) == (stats_module.Stats(0, 0, None), stats_module.Stats(0, 0, None))
    async with engine.begin() as conn:
        await conn.execute(insert(FramedResult).values(user_id=USER_ID + 1, framed_round=1, won=False, win_frame=None))
        await conn.execute(insert(EpisodeResult).values(user_id=USER_ID + 1, framed_round=1, won=True, win_frame=3))
    assert await stats_module.get_user_stats(USER_ID + 1) == (stats_module.Stats(1, 0, None), stats_module.Stats(1, 1, 3.0))
    async with engine.begin() as conn:
        await conn.execute(insert(FramedResult).values(user_id=USER_ID + 1, framed_round=2, won=True, win_frame=None))
    try:
        await stats_module.get_user_stats(USER_ID + 1)
    except stats_module.MissingWinFrameError:
        pass
    else:
        raise AssertionError('Invalid winning row silently changed the average')
    print(json.dumps({'stats_statements': len(statements), 'stats_rows': fetched,
                      'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


async def announce():
    for chat_id in range(-9, 0):
        await Group.update_from_tg_chat(Chat(chat_id, 'group', title='Synthetic'))
    attempts, delivered, sleeps = [], [], []

    async def send_message(_bot, *, chat_id, text):
        assert text == 'Synthetic notice'
        attempts.append(chat_id)
        if chat_id == -9:
            raise TelegramError('unknown recipient failure')
        if chat_id == -8:
            raise RetryAfter(timedelta(seconds=1))
        if chat_id == -7:
            raise Forbidden('bot was kicked')
        if chat_id == -6:
            raise BadRequest('Chat not found')
        if chat_id == -5:
            raise NetworkError('temporary network failure')
        if chat_id == -4 and attempts.count(chat_id) == 1:
            raise RetryAfter(timedelta(seconds=1))
        if chat_id == -3:
            raise RetryAfter(timedelta(hours=1))
        if chat_id == -2:
            raise BadRequest('Message is too long')
        delivered.append(chat_id)

    async def sleep(delay):
        sleeps.append(delay)

    message = Message(17, datetime.now(UTC), Chat(1, 'private'), from_user=TelegramUser(1, 'Admin', False), text='/announce Synthetic notice')
    application = ApplicationBuilder().token('123:synthetic-only').build()
    context = ContextTypes.DEFAULT_TYPE(application)
    with patch('asyncio.sleep', sleep), patch.object(ExtBot, 'send_message', send_message):
        await main.announce(Update(1, message=message), context)
    assert sorted(delivered) == [-4, -1]
    assert attempts.count(-7) == attempts.count(-6) == attempts.count(-3) == attempts.count(-2) == 1
    assert attempts.count(-8) == attempts.count(-5) == 3 and attempts.count(-4) == 2
    assert attempts.count(-9) == 1
    assert max(sleeps) <= 5 and sum(sleeps) <= 8
    assert {group.id for group in await Group.get_all()} == {-9, -8, -5, -4, -3, -2, -1}
    # A later announcement excludes permanently unavailable recipients.
    async def later_send(_bot, *, chat_id, text):
        assert chat_id not in (-7, -6)
    with patch.object(ExtBot, 'send_message', later_send):
        await main.announce(Update(2, message=message), context)
    # Fresh activity can register a previously unavailable recipient again.
    await Group.update_from_tg_chat(Chat(-7, 'group', title='Returned'))
    assert await Group.get(-7) is not None
    print(json.dumps({'broadcast_attempts': attempts, 'delivered': delivered, 'retry_sleeps': sleeps}))


async def schema_state():
    def read(sync_conn):
        tables = set(inspect(sync_conn).get_table_names())
        version = None
        if 'alembic_version' in tables:
            version = sync_conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
        return tables, version, compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)

    async with engine.connect() as conn:
        return await conn.run_sync(read)


async def migrations():
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    tables, version, diff = await schema_state()
    assert version == head and diff == [], (version, diff)
    await upgrade_database()
    assert await schema_state() == (tables, head, [])
    # A schema that create_all made before Alembic keeps its rows and is adopted at the baseline.
    await drop_schema()
    async with engine.begin() as conn:
        # create_all before Alembic made only the baseline tables.
        await conn.run_sync(Base.metadata.create_all, tables=[Base.metadata.tables[name] for name in BASELINE_TABLES])
    await User.update_from_tg_user(TelegramUser(USER_ID, 'Legacy', False))
    assert await FramedResult.save_result(USER_ID, 1, True, 2)
    await upgrade_database()
    assert await schema_state() == (tables, head, [])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(FramedResult.user_id, FramedResult.won, FramedResult.win_frame))).all()
    assert rows == [(USER_ID, True, 2)]
    # A partial pre-Alembic schema stops startup and is left unchanged.
    await drop_schema()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.tables['user'].create)
    try:
        await upgrade_database()
    except PartialSchemaError:
        pass
    else:
        raise AssertionError('Partial pre-Alembic schema was migrated')
    assert (await schema_state())[:2] == ({'user'}, None)
    print(json.dumps({'head': head, 'tables': sorted(tables)}))


async def answers():
    from answers.store import DbAnswerStore, PendingAnswer
    from answers.tmdb import Credit, TitleData
    from models.game_answer import GameAnswer
    from models.title import Person, Title, TitlePerson

    store = DbAnswerStore()

    def show(tmdb_id, name, credits):
        raw = {'id': tmdb_id, 'networks': [{'id': 49, 'name': 'HBO'}]}
        return TitleData(tmdb_id, 'tv', name, name, 2018, None, ['US'], None, [(18, 'Drama')], [(3268, 'HBO')],
                         credits, raw)

    simon = [Credit(1, 'David Simon', 'creator', 0), Credit(1, 'David Simon', 'cast', 0)]
    wire = show(1438, 'The Wire', simon)
    wire.genres.extend(wire.genres)
    wire.companies.extend(wire.companies)
    wire.credits.extend(wire.credits)
    succession = show(76331, 'Succession', [Credit(2, 'Jesse Armstrong', 'creator', 0)])
    await User.update_from_tg_user(TelegramUser(USER_ID, 'Synthetic', False))
    assert await store.max_result_round('episode') is None
    assert await EpisodeResult.save_result(USER_ID, 3, True, 2)
    assert await store.max_result_round('episode') == 3
    assert await store.missing_rounds('episode', 3) == [1, 2, 3]
    await store.insert_answers('episode', {1: 'The Wire', 2: 'Succession', 3: 'The Wire'})
    # An existing answer row never changes.
    await store.insert_answers('episode', {1: 'Changed'})
    assert await store.missing_rounds('episode', 3) == []
    assert await store.has_answer('episode', 2) and not await store.has_answer('framed', 2)
    assert await store.pending('episode', 2) == [PendingAnswer(1, 'The Wire'), PendingAnswer(2, 'Succession')]
    # Two rounds share one title; one person can be creator and cast of a title.
    await store.save_title('episode', 1, wire, 'auto')
    await store.save_title('episode', 3, wire, 'low_confidence')
    await store.mark_unmatched('episode', 2)
    assert await store.pending('episode', 3) == []
    assert not await store.set_manual_title('episode', 9, wire)
    assert await store.set_manual_title('episode', 2, succession)
    # A sync result never replaces a manual choice.
    await store.save_title('episode', 2, wire, 'auto')
    await store.mark_unmatched('episode', 2)
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(GameAnswer.round, GameAnswer.source_title, Title.name, GameAnswer.match_status)
            .join(Title, Title.id == GameAnswer.title_id).order_by(GameAnswer.round)
        )).all()
        assert rows == [(1, 'The Wire', 'The Wire', 'auto'), (2, 'Succession', 'Succession', 'manual'),
                        (3, 'The Wire', 'The Wire', 'low_confidence')], rows
        assert (await conn.execute(select(func.count()).select_from(Title))).scalar_one() == 2
        roles = (await conn.execute(
            select(Person.name, TitlePerson.role).join(Person).order_by(TitlePerson.role, Person.name)
        )).all()
        assert roles == [('David Simon', 'cast'), ('David Simon', 'creator'), ('Jesse Armstrong', 'creator')], roles
        network = Title.raw['networks'][0]['name'].astext
        assert (await conn.execute(select(network).where(Title.tmdb_id == 1438))).scalar_one() == 'HBO'
    # A new fetch replaces the links of the title.
    await store.save_title('episode', 1, show(1438, 'The Wire', []), 'auto')
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(TitlePerson))).scalar_one() == 1
    print(json.dumps({'answers': len(rows)}))
    await concurrent_answers(store, wire)


async def concurrent_answers(store, wire):
    from answers.tmdb import Credit, TitleData
    from models.game_answer import GameAnswer

    # A sync and a manual fix of the same answer and title overlap; the answer row lock comes first in both.
    rounds = range(10, 40)
    await store.insert_answers('episode', {number: 'The Wire' for number in rounds})
    for number in rounds:
        await asyncio.gather(store.save_title('episode', number, wire, 'auto'),
                             store.set_manual_title('episode', number, wire))
    # Framed and Episode share person, genre and company rows in reverse order; sorted upserts keep one lock order.
    people = [Credit(10_000 + number, f'Person {number}', 'cast', number) for number in range(60)]
    film = TitleData(5000, 'movie', 'Film', 'Film', 2000, 90, ['US'], None, [(18, 'Drama'), (35, 'Comedy')],
                     [(3268, 'HBO'), (33, 'Universal Pictures')], people, {})
    show = TitleData(6000, 'tv', 'Show', 'Show', 2001, None, ['US'], None, [(35, 'Comedy'), (18, 'Drama')],
                     [(33, 'Universal Pictures'), (3268, 'HBO')], people[::-1], {})
    await store.insert_answers('framed', {number: 'Film' for number in rounds})
    await store.insert_answers('episode', {number + 100: 'Show' for number in rounds})
    for number in rounds:
        await asyncio.gather(store.save_title('framed', number, film, 'auto'),
                             store.save_title('episode', number + 100, show, 'auto'))
    async with engine.connect() as conn:
        statuses = (await conn.execute(
            select(GameAnswer.match_status, func.count()).where(GameAnswer.title_id.is_not(None))
            .group_by(GameAnswer.match_status).order_by(GameAnswer.match_status)
        )).all()
    # Rounds 1-3 from the scenario above, 30 manual fixes, 60 concurrent title saves.
    assert statuses == [('auto', 61), ('low_confidence', 1), ('manual', 31)], statuses


async def run():
    try:
        await reset()
        scenarios = {'profile': profile, 'duplicates': duplicates, 'stats': aggregate_stats, 'announce': announce,
                     'migrations': migrations, 'answers': answers}
        await scenarios[sys.argv[1]]()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(run())
