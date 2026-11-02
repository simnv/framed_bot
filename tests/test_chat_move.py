"""Run the supergroup move on SQLite: results and the group row follow the new chat id."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql, sqlite

import models.chat_move as chat_move

OLD, NEW = -100, -1001000000100


@pytest.fixture
def database(monkeypatch):
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(
            'CREATE TABLE "group" (id INTEGER PRIMARY KEY, title TEXT);'
            'CREATE TABLE framed_result (id INTEGER PRIMARY KEY, chat_id INTEGER, user_id INTEGER, '
            'framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
            'CREATE TABLE episode_result (id INTEGER PRIMARY KEY, chat_id INTEGER, user_id INTEGER, '
            'framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
        )

        def run(statement):
            # Every statement must also compile for PostgreSQL.
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
            sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
            return connection.execute(sql)

        async def execute(statement):
            cursor = run(statement)
            rows = cursor.fetchall()
            return SimpleNamespace(all=lambda: rows, scalar_one_or_none=lambda: rows[0][0] if rows else None)

        async def scalar(statement):
            row = run(statement).fetchone()
            return row[0] if row else None

        session = AsyncMock()
        session.execute.side_effect = execute
        session.scalar.side_effect = scalar
        session.__aenter__.return_value = session
        monkeypatch.setattr(chat_move, 'AsyncScopedSession', lambda: session)
        yield connection, session


def rows(connection, table):
    query = f'SELECT id, chat_id, user_id, framed_round FROM {table} ORDER BY id'  # noqa: S608 - test table names
    return connection.execute(query).fetchall()


@pytest.mark.asyncio
async def test_move_chat_moves_results_and_keeps_the_first_row_of_a_clash(database):
    connection, session = database
    connection.executemany(
        'INSERT INTO framed_result VALUES (?, ?, ?, ?, ?, ?)',
        [(1, OLD, 7, 10, 1, 1), (2, NEW, 7, 10, 0, None), (3, OLD, 7, 11, 1, 2), (4, -300, 7, 10, 1, 3)],
    )
    connection.execute('INSERT INTO episode_result VALUES (1, ?, 7, 1, 1, 1)', (OLD,))
    connection.execute('INSERT INTO "group" VALUES (?, ?)', (OLD, 'Old'))

    await chat_move.move_chat(OLD, NEW)

    assert rows(connection, 'framed_result') == [(1, NEW, 7, 10), (3, NEW, 7, 11), (4, -300, 7, 10)]
    assert rows(connection, 'episode_result') == [(1, NEW, 7, 1)]
    assert connection.execute('SELECT id, title FROM "group"').fetchall() == [(NEW, 'Old')]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_move_chat_keeps_an_existing_new_group_row_and_is_idempotent(database):
    connection, _session = database
    connection.executemany('INSERT INTO "group" VALUES (?, ?)', [(OLD, 'Old'), (NEW, 'New')])

    await chat_move.move_chat(OLD, NEW)
    await chat_move.move_chat(OLD, NEW)

    assert connection.execute('SELECT id, title FROM "group"').fetchall() == [(NEW, 'New')]
