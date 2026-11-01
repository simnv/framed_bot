"""Check leaderboard SQL contracts and execute the leaderboard queries in memory."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql, sqlite

import models.top as top_module
from models import EpisodeResult, FramedResult
from models.top import top_results, top_statement
from top_type import TopType

FIRST_USER_ID = 5_000_000_123
LOSS = (False, None)
# A broken row: the player won, but the bot has no frame for the win.
BROKEN = (True, None)


def test_framed_and_episode_have_separate_frame_counts():
    assert top_module.FRAMES == {'framed': 6, 'episode': 10}
    assert top_module.MODELS['framed'] is FramedResult
    assert top_module.MODELS['episode'] is EpisodeResult


@pytest.mark.parametrize('game', ['framed', 'episode'])
@pytest.mark.parametrize(
    ('top_type', 'score_fragment', 'order', 'having'),
    [
        (
            TopType.TOP_SCORE,
            'sum(CASE WHEN ({table}.won IS true) THEN {miss} - {table}.win_frame ELSE 1 END)',
            'ORDER BY score DESC, count(*) DESC, "user".full_name ASC, {table}.user_id ASC',
            False,
        ),
        (
            TopType.TOP_FRAME,
            'avg(CAST(CASE WHEN ({table}.won IS true) THEN {table}.win_frame ELSE {miss} END AS FLOAT))',
            'ORDER BY score ASC, count(*) DESC, "user".full_name ASC, {table}.user_id ASC',
            True,
        ),
        (
            TopType.TOP_WIN,
            'sum(CASE WHEN ({table}.won IS true) THEN 1 ELSE 0 END)',
            'ORDER BY score DESC, count(*) DESC, "user".full_name ASC, {table}.user_id ASC',
            False,
        ),
        (
            TopType.TOP_ROUNDS,
            'count(*) AS score',
            'ORDER BY score DESC, sum(CASE WHEN ({table}.won IS true) THEN 1 ELSE 0 END) DESC, '
            '"user".full_name ASC, {table}.user_id ASC',
            False,
        ),
    ],
)
def test_top_statement_keeps_row_shape_sorting_and_minimum(game, top_type, score_fragment, order, having):
    table = f'{game}_result'
    miss = {'framed': 7, 'episode': 11}[game]
    statement = top_statement(game, top_type)
    assert list(statement.selected_columns.keys()) == ['user_id', 'name', 'score']
    sql = ' '.join(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})).split())
    assert f'{table}.user_id AS user_id' in sql
    assert f'JOIN "user" ON "user".id = {table}.user_id' in sql
    assert f'GROUP BY {table}.user_id, "user".full_name' in sql
    assert score_fragment.format(table=table, miss=miss) in sql
    assert sql.endswith(order.format(table=table) + ' LIMIT 10')
    assert ('HAVING count(*) >= 10' in sql) is having
    other = 'episode_result' if game == 'framed' else 'framed_result'
    assert other not in sql


@pytest.mark.asyncio
async def test_top_results_reads_without_commit(monkeypatch):
    rows = [SimpleNamespace(user_id=FIRST_USER_ID, name='A & B', score=3)]
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(all=lambda: rows)
    session.__aenter__.return_value = session
    monkeypatch.setattr(top_module, 'AsyncScopedSession', lambda: session)

    assert await top_results('episode', TopType.TOP_ROUNDS) is rows
    session.execute.assert_awaited_once()
    session.commit.assert_not_awaited()


async def run_top(monkeypatch, game, top_type, histories, names=None):
    """Store one history per user in memory and run the real leaderboard statement on it."""
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(
            'CREATE TABLE user (id INTEGER PRIMARY KEY, full_name TEXT);'
            'CREATE TABLE framed_result (user_id INTEGER, framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
            'CREATE TABLE episode_result (user_id INTEGER, framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
        )
        for offset, history in enumerate(histories):
            user_id = FIRST_USER_ID + offset
            name = names[offset] if names else f'User {offset}'
            connection.execute('INSERT INTO user VALUES (?, ?)', (user_id, name))
            connection.executemany(
                f'INSERT INTO {game}_result VALUES (?, ?, ?, ?)',  # noqa: S608 -- game is a test literal
                [(user_id, round_id, won, frame) for round_id, (won, frame) in enumerate(history, 1)],
            )
        other = 'episode' if game == 'framed' else 'framed'
        # A row of the other game must never change this game's leaderboard.
        connection.execute(f'INSERT INTO {other}_result VALUES (?, 1, 1, 1)', (FIRST_USER_ID,))  # noqa: S608

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
        monkeypatch.setattr(top_module, 'AsyncScopedSession', lambda: session)
        rows = await top_results(game, top_type)
        session.commit.assert_not_awaited()
        return [(row.user_id - FIRST_USER_ID, row.score) for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('game', 'histories', 'expected'),
    [
        (
            'framed',
            [[(True, 1)] + [LOSS] * 10, [(True, 3)] * 10],
            [(1, 3.0), (0, 71 / 11)],
        ),
        ('framed', [[LOSS] * 10], [(0, 7.0)]),
        ('framed', [[LOSS] * 10, [(True, 6)] * 10], [(1, 6.0), (0, 7.0)]),
        ('framed', [[(True, 1), (True, 6), LOSS] * 4, [(True, 5)] * 10], [(0, 14 / 3), (1, 5.0)]),
        ('episode', [[LOSS] * 10, [(True, 10)] * 10], [(1, 10.0), (0, 11.0)]),
        ('episode', [[(True, 1)] + [LOSS] * 9], [(0, 10.0)]),
        ('framed', [[(True, 1)] * 9, [(True, 2)] * 10], [(1, 2.0)]),
        ('framed', [[], [(True, 3)] * 10], [(1, 3.0)]),
        ('framed', [], []),
    ],
    ids=[
        'losses-change-ranking',
        'all-losses',
        'last-frame-beats-loss',
        'mixed',
        'episode-loss-is-eleven',
        'episode-mixed',
        'fewer-than-ten-rounds-hidden',
        'unsubmitted',
        'empty',
    ],
)
async def test_average_frame_counts_each_loss_as_miss_value(monkeypatch, game, histories, expected):
    rows = await run_top(monkeypatch, game, TopType.TOP_FRAME, histories)
    assert [offset for offset, _score in rows] == [offset for offset, _score in expected]
    assert [score for _offset, score in rows] == pytest.approx([score for _offset, score in expected])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('game', 'histories', 'expected'),
    [
        ('framed', [[(True, 1), (True, 6), LOSS], [(True, 2)]], [(0, 6 + 1 + 1), (1, 5)]),
        ('episode', [[(True, 1), (True, 10), LOSS], [(True, 2)]], [(0, 10 + 1 + 1), (1, 9)]),
    ],
)
async def test_points_use_game_miss_value(monkeypatch, game, histories, expected):
    assert await run_top(monkeypatch, game, TopType.TOP_SCORE, histories) == expected


@pytest.mark.asyncio
async def test_guessed_and_rounds_count_wins_and_rounds(monkeypatch):
    histories = [[(True, 3), LOSS, LOSS], [(True, 1), (True, 2)]]
    assert await run_top(monkeypatch, 'episode', TopType.TOP_WIN, histories) == [(1, 2), (0, 1)]
    assert await run_top(monkeypatch, 'episode', TopType.TOP_ROUNDS, histories) == [(0, 3), (1, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('top_type', 'same', 'more_rounds'),
    [
        # 10 points in 2 rounds, or 10 points in 10 lost rounds.
        (TopType.TOP_SCORE, [(True, 2)] * 2, [LOSS] * 10),
        (TopType.TOP_FRAME, [(True, 4)] * 10, [(True, 4)] * 12),
        (TopType.TOP_WIN, [(True, 1)] * 10, [(True, 1)] * 10 + [LOSS]),
    ],
)
async def test_ties_rank_more_rounds_then_name_then_user_id(monkeypatch, top_type, same, more_rounds):
    names = ['Bob', 'Alice', 'Alice', 'Zed']
    rows = await run_top(monkeypatch, 'framed', top_type, [same, same, same, more_rounds], names)
    assert [offset for offset, _score in rows] == [3, 1, 2, 0]
    assert len({round(score, 6) for _offset, score in rows}) == 1


@pytest.mark.asyncio
async def test_rounds_ties_rank_more_wins_then_name_then_user_id(monkeypatch):
    histories = [[LOSS] * 3, [LOSS] * 3, [LOSS] * 3, [(True, 1), LOSS, LOSS]]
    names = ['Bob', 'Alice', 'Alice', 'Zed']
    rows = await run_top(monkeypatch, 'framed', TopType.TOP_ROUNDS, histories, names)
    assert rows == [(3, 3), (1, 3), (2, 3), (0, 3)]


@pytest.mark.asyncio
async def test_leaderboard_stops_at_ten_players(monkeypatch):
    histories = [[(True, 1)] * (offset + 1) for offset in range(12)]
    rows = await run_top(monkeypatch, 'framed', TopType.TOP_ROUNDS, histories)
    assert [offset for offset, _score in rows] == list(range(11, 1, -1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('top_type', 'expected'),
    [
        (TopType.TOP_SCORE, [(1, 60), (0, 60)]),
        (TopType.TOP_FRAME, [(1, 1.0), (0, 1.0)]),
        (TopType.TOP_WIN, [(1, 10), (0, 10)]),
        (TopType.TOP_ROUNDS, [(1, 10), (0, 10)]),
    ],
)
async def test_won_rows_without_frame_are_ignored_by_every_metric(monkeypatch, top_type, expected):
    # Player 0 has 10 valid frame-1 wins and 5 broken rows; player 1 has 10 valid frame-1 wins only.
    histories = [[(True, 1)] * 10 + [BROKEN] * 5, [(True, 1)] * 10]
    names = ['Bob', 'Alice']
    rows = await run_top(monkeypatch, 'framed', top_type, histories, names)
    assert [offset for offset, _score in rows] == [offset for offset, _score in expected]
    assert [score for _offset, score in rows] == pytest.approx([score for _offset, score in expected])


@pytest.mark.asyncio
async def test_player_with_only_broken_rows_is_absent(monkeypatch):
    rows = await run_top(monkeypatch, 'episode', TopType.TOP_ROUNDS, [[BROKEN] * 12, [LOSS]])
    assert rows == [(1, 1)]


@pytest.mark.parametrize('top_type', list(TopType))
def test_top_statement_filters_won_rows_without_frame(top_type):
    sql = ' '.join(
        str(
            top_statement('framed', top_type).compile(
                dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}
            )
        ).split()
    )
    assert 'WHERE NOT (framed_result.won IS true AND framed_result.win_frame IS NULL)' in sql
