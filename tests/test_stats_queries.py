"""Run get_user_stats on SQLite: a group counts its own results, a private chat merges all chats."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql, sqlite

import stats

CHAT, OTHER_CHAT = -100, -200


@pytest.fixture
def database(monkeypatch):
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(
            'CREATE TABLE user (id INTEGER PRIMARY KEY, full_name TEXT, username TEXT);'
            'CREATE TABLE framed_result (id INTEGER PRIMARY KEY, chat_id INTEGER, user_id INTEGER, '
            'framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
            'CREATE TABLE episode_result (id INTEGER PRIMARY KEY, chat_id INTEGER, user_id INTEGER, '
            'framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
            "INSERT INTO user VALUES (1, 'One', '');"
        )

        async def execute(statement):
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
            sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
            rows = connection.execute(sql).fetchall()
            return SimpleNamespace(
                one=lambda: rows[0], scalar_one_or_none=lambda: rows[0][0] if rows else None, all=lambda: rows
            )

        session = AsyncMock()
        session.execute.side_effect = execute
        session.__aenter__.return_value = session
        monkeypatch.setattr(stats, 'AsyncScopedSession', lambda: session)
        yield connection
        session.commit.assert_not_awaited()


def add(database, chat, rounds, *, game='framed', user=1):
    columns = '(chat_id, user_id, framed_round, won, win_frame)'
    database.executemany(
        f'INSERT INTO {game}_result {columns} VALUES (?, ?, ?, ?, ?)',  # noqa: S608 - game is a test literal
        [(chat, user, round_id, frame is not None, frame) for round_id, frame in rounds],
    )


@pytest.mark.asyncio
async def test_group_stats_count_only_that_chat(database):
    add(database, CHAT, [(1, 1), (2, None)])
    add(database, OTHER_CHAT, [(3, 2), (4, 2)])
    add(database, OTHER_CHAT, [(1, 3)], game='episode')

    result = await stats.get_user_stats(1, CHAT)
    other = await stats.get_user_stats(1, OTHER_CHAT)

    assert result is not None and other is not None
    framed, episode = result
    assert (framed.rounds_count, framed.rounds_won_count, framed.points) == (2, 1, 7)
    assert episode.rounds_count == 0
    assert other[0].rounds_count == 2


@pytest.mark.asyncio
async def test_private_stats_merge_chats_with_the_first_saved_row(database):
    add(database, CHAT, [(1, 1)])
    add(database, OTHER_CHAT, [(1, None), (2, 2)])

    result = await stats.get_user_stats(1, None)

    assert result is not None
    framed, _episode = result
    assert (framed.rounds_count, framed.rounds_won_count, framed.points) == (2, 2, 11)
    assert await stats.get_user_stats(2, None) is None
