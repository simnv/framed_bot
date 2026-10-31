"""Check leaderboard SQL contracts and execute average-frame queries in memory."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql, sqlite

import models.episode_result  # noqa: F401 -- register User's other relationship before compiling ORM queries
import models.framed_result as framed_module
from models.framed_result import FramedResult


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('query_name', 'score_fragment', 'sort_suffix'),
    [
        (
            'top_score',
            'sum(CASE WHEN (framed_result.won IS true) THEN 7 - framed_result.win_frame ELSE 1 END)',
            ' DESC',
        ),
        (
            'top_average_frame',
            'avg(CAST(CASE WHEN (framed_result.won IS true) THEN framed_result.win_frame ELSE 7 END AS FLOAT))',
            '',
        ),
        ('top_rounds', 'count(*)', ' DESC'),
        ('top_won', 'sum(CAST(framed_result.won AS INTEGER))', ' DESC'),
    ],
)
async def test_top_queries_keep_profile_ids_and_expected_sorting(
    monkeypatch,
    query_name,
    score_fragment,
    sort_suffix,
):
    rows = [SimpleNamespace(user_id=5_000_000_123, name='A & B', score=3)]
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(all=lambda: rows)
    session.__aenter__.return_value = session
    monkeypatch.setattr(framed_module, 'AsyncScopedSession', lambda: session)

    returned = await getattr(FramedResult, query_name)()

    assert returned is rows
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    assert list(statement.selected_columns.keys()) == ['user_id', 'name', 'score']
    sql = ' '.join(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})).split())
    assert 'framed_result.user_id AS user_id' in sql
    assert 'JOIN "user" ON "user".id = framed_result.user_id' in sql
    assert 'GROUP BY framed_result.user_id, "user".full_name' in sql
    assert score_fragment in sql
    assert sql.endswith(f'ORDER BY score{sort_suffix} LIMIT 10')
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('histories', 'expected'),
    [
        (
            [[(True, 1)] + [(False, None)] * 10, [(True, 3)] * 3],
            [(1, 3.0), (0, 71 / 11)],
        ),
        ([[(False, None)] * 2], [(0, 7.0)]),
        ([[(False, None)], [(True, 6)]], [(1, 6.0), (0, 7.0)]),
        ([[(True, 1), (True, 2), (True, 6)]], [(0, 3.0)]),
        ([[(True, 1), (True, 6), (False, None)], [(True, 5)]], [(0, 14 / 3), (1, 5.0)]),
        ([[], [(True, 3)]], [(1, 3.0)]),
        ([], []),
    ],
    ids=['losses-change-ranking', 'all-losses', 'last-frame-beats-loss', 'all-wins', 'mixed', 'unsubmitted', 'empty'],
)
async def test_average_frame_counts_each_submitted_loss_as_seven(monkeypatch, histories, expected):
    first_user_id = 5_000_000_123
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(
            'CREATE TABLE user (id INTEGER PRIMARY KEY, full_name TEXT);'
            'CREATE TABLE framed_result (user_id INTEGER, framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
        )
        for offset, history in enumerate(histories):
            user_id = first_user_id + offset
            connection.execute('INSERT INTO user VALUES (?, ?)', (user_id, f'User {offset}'))
            connection.executemany(
                'INSERT INTO framed_result VALUES (?, ?, ?, ?)',
                [(user_id, round_id, won, frame) for round_id, (won, frame) in enumerate(history, 1)],
            )

        async def execute(statement):
            assert statement.is_select
            sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
            rows = [
                SimpleNamespace(user_id=user_id, name=name, score=score)
                for user_id, name, score in connection.execute(sql).fetchall()
            ]
            return SimpleNamespace(all=lambda: rows)

        session = AsyncMock()
        session.execute.side_effect = execute
        session.__aenter__.return_value = session
        monkeypatch.setattr(framed_module, 'AsyncScopedSession', lambda: session)

        rows = await FramedResult.top_average_frame()

        assert [(row.user_id, row.name) for row in rows] == [
            (first_user_id + offset, f'User {offset}') for offset, _score in expected
        ]
        assert [row.score for row in rows] == pytest.approx([score for _offset, score in expected])
        session.commit.assert_not_awaited()
