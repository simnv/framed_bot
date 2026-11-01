"""Run the public insight queries on SQLite with fixed expected values."""

import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql, sqlite

import insight
from answers import round_date

TODAY = date(2026, 11, 1)


@pytest.fixture(params=[False, True], ids=['sqlite', 'postgres-averages'])
def database(monkeypatch, request):
    connection = sqlite3.connect(':memory:')
    connection.executescript(
        'CREATE TABLE user (id INTEGER PRIMARY KEY, full_name TEXT);'
        'CREATE TABLE framed_result (user_id INTEGER, framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
        'CREATE TABLE episode_result (user_id INTEGER, framed_round INTEGER, won BOOLEAN, win_frame INTEGER);'
        'CREATE TABLE game_answer (game TEXT, round INTEGER, title_id INTEGER, match_status TEXT);'
        'CREATE TABLE title (id INTEGER PRIMARY KEY, name TEXT, year INTEGER, kind TEXT);'
        'CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT);'
        'CREATE TABLE title_genre (title_id INTEGER, genre_id INTEGER);'
        'CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT);'
        'CREATE TABLE title_person (title_id INTEGER, person_id INTEGER, role TEXT, "order" INTEGER);'
    )

    async def execute(statement):
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
        sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
        cursor = connection.execute(sql)
        names = [column[0] for column in cursor.description]
        rows = [SimpleNamespace(**dict(zip(names, row, strict=True))) for row in cursor.fetchall()]
        if request.param:
            # PostgreSQL returns Decimal for AVG of integer columns.
            numeric = ('mean', 'points', 'win_rate') if 'players' in names else ('mean',)
            for row in rows:
                for name in numeric:
                    value = getattr(row, name, None)
                    if value is not None:
                        setattr(row, name, Decimal(str(value)))
        return SimpleNamespace(all=lambda: rows)

    session = AsyncMock()
    session.execute.side_effect = execute
    session.__aenter__.return_value = session
    monkeypatch.setattr(insight, 'AsyncScopedSession', lambda: session, raising=False)
    yield connection
    session.commit.assert_not_awaited()
    connection.close()


def add_title(database, key=1, *, genres=(1,), year=1999, kind='movie'):
    database.execute('INSERT INTO title VALUES (?, ?, ?, ?)', (key, f'Title {key}', year, kind))
    for genre in genres:
        database.execute('INSERT OR IGNORE INTO genre VALUES (?, ?)', (genre, f'Genre {genre}'))
        database.execute('INSERT INTO title_genre VALUES (?, ?)', (key, genre))


def add_rounds(database, user=1, *, start=1, count=1, title=1, frame=1, game='framed', status='auto'):
    database.execute('INSERT OR IGNORE INTO user VALUES (?, ?)', (user, f'User {user:02}'))
    for round_id in range(start, start + count):
        database.execute(
            f'INSERT INTO {game}_result VALUES (?, ?, ?, ?)',  # noqa: S608 -- game is a test literal
            (user, round_id, frame is not None, frame),
        )
        if not database.execute('SELECT 1 FROM game_answer WHERE game = ? AND round = ?', (game, round_id)).fetchone():
            database.execute('INSERT INTO game_answer VALUES (?, ?, ?, ?)', (game, round_id, title, status))


@pytest.mark.asyncio
async def test_personal_buckets_split_genres_and_use_own_eligible_mean(database):
    add_title(database, genres=(1, 2))
    add_title(database, 2, genres=(), year=2001)
    add_rounds(database, count=10, status='low_confidence')
    add_rounds(database, start=11, count=5, title=2, frame=None)
    # Another player's low scores do not change the personal mean of 65 / 15.
    add_rounds(database, user=2, count=10, frame=None)

    result = await insight.game_insight(1, 'framed', TODAY)

    assert [(b.key, b.rounds, b.win_rate) for b in result.genres.best] == [(1, 5.0, 1.0), (2, 5.0, 1.0)]
    assert [b.points for b in result.genres.best] == pytest.approx([4.8888888889, 4.8888888889])
    assert [(b.key, b.label, b.rounds) for b in result.decades.best] == [(1990, '1990-е', 10.0), (2000, '2000-е', 5.0)]
    assert [b.points for b in result.decades.best] == pytest.approx([5.1666666667, 3.2222222222])
    assert [b.win_rate for b in result.decades.best] == [1.0, 0.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(('game', 'kind', 'score'), [('framed', 'movie', 5.0), ('episode', 'tv', 9.0)])
async def test_metadata_filters_closed_valid_matched_rounds_and_game_kind(database, game, kind, score):
    add_title(database, kind=kind)
    other_kind = 'tv' if kind == 'movie' else 'movie'
    add_title(database, 2, kind=other_kind)
    for round_id, status in enumerate(('site', 'auto', 'visual', 'low_confidence', 'manual'), 1):
        add_rounds(database, start=round_id, frame=2, game=game, status=status)
    add_rounds(database, start=6, count=5, game=game, status='unmatched')
    add_rounds(database, start=11, count=5, game=game, status=None)
    add_rounds(database, start=16, count=5, game=game, title=None)
    add_rounds(database, start=21, count=5, game=game, title=2)
    add_rounds(database, start=26, count=5, game=game)
    database.execute(f'UPDATE {game}_result SET win_frame = NULL WHERE framed_round >= 26')  # noqa: S608
    add_rounds(database, start=31, count=5, game=game)
    database.execute('DELETE FROM game_answer WHERE round >= 31')
    # At round 36, its results and all later results are still open.
    add_rounds(database, start=36, count=5, game=game)

    result = await insight.game_insight(1, game, round_date(game, 36))

    assert [(b.rounds, b.points, b.win_rate) for b in result.genres.best] == [(5.0, score, 1.0)]
    assert [(b.key, b.rounds, b.points) for b in result.decades.best] == [(1990, 5.0, score)]


@pytest.mark.asyncio
@pytest.mark.parametrize(('game', 'kind', 'role'), [('framed', 'movie', 'director'), ('episode', 'tv', 'creator')])
async def test_people_use_game_role_and_first_three_cast_with_three_round_gate(database, game, kind, role):
    add_title(database, kind=kind)
    add_title(database, 2, genres=(), year=None, kind=kind)
    for key in range(1, 8):
        database.execute('INSERT INTO person VALUES (?, ?)', (key, f'Person {key}'))
    database.executemany(
        'INSERT INTO title_person VALUES (?, ?, ?, ?)',
        [
            (1, 1, 'director', 0),
            (1, 2, 'creator', 0),
            (1, 3, 'cast', 0),
            (1, 4, 'cast', 2),
            (1, 5, 'cast', 3),
            (2, 6, role, 0),
            (2, 7, 'cast', 1),
        ],
    )
    add_rounds(database, count=3, game=game, frame=2)
    add_rounds(database, start=4, count=2, title=2, game=game, frame=None)

    result = await insight.game_insight(1, game, TODAY)

    assert [(b.key, b.role, b.rounds) for b in result.people.best] == [
        (1 if game == 'framed' else 2, role, 3.0),
        (3, 'cast', 3.0),
        (4, 'cast', 3.0),
    ]
    expected = 3.7692307692 if game == 'framed' else 6.5384615385
    assert [b.points for b in result.people.best] == pytest.approx([expected, expected, expected])
    assert result.people.worst == ()
    assert result.genres.best == result.decades.best == ()


@pytest.mark.asyncio
async def test_a_person_with_two_roles_is_one_row_and_counts_each_round_once(database):
    add_title(database, genres=(), year=None)
    add_title(database, 2, genres=(), year=None)
    database.execute('INSERT INTO person VALUES (1, ?)', ('Director Actor',))
    database.executemany(
        'INSERT INTO title_person VALUES (?, ?, ?, ?)',
        # Person 1 directs title 1 and also plays in it, and only plays in title 2.
        [(1, 1, 'director', 0), (1, 1, 'cast', 0), (2, 1, 'cast', 1)],
    )
    add_rounds(database, count=2, frame=2)
    add_rounds(database, start=3, title=2, frame=2)

    result = await insight.game_insight(1, 'framed', TODAY)

    # 2 director rounds and 1 cast round pass the gate of 3 together, and the directing role wins.
    assert [(b.key, b.role, b.rounds, b.win_rate) for b in result.people.best] == [(1, 'director', 3.0, 1.0)]
    assert result.people.best[0].points == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_equal_smoothed_points_tie_exactly_and_sort_by_label(database):
    # All rounds win on frame 4, so every round and the mean give 3 points, and every bucket has 3.0.
    add_title(database, genres=(2, 3, 4))
    add_title(database, 2, genres=(1,), year=2005)
    add_rounds(database, count=46, frame=4)
    add_rounds(database, start=47, count=20, title=2, frame=4)

    result = await insight.game_insight(1, 'framed', TODAY)

    assert [b.points for b in result.genres.best] == [3.0, 3.0, 3.0, 3.0]
    assert [b.key for b in result.genres.best] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_a_won_round_without_a_frame_breaks_the_streak(database):
    add_rounds(database, count=5, frame=1, title=None)
    database.execute('UPDATE framed_result SET win_frame = NULL WHERE framed_round = 3')
    result = await insight.game_insight(1, 'framed', round_date('framed', 6))
    assert (result.current_streak, result.best_streak) == (2, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('history', 'today_round', 'current', 'best'),
    [
        ([], 10, 0, 0),
        ([(1, True), (2, True), (4, True), (5, True), (6, True)], 7, 3, 3),
        ([(1, True), (2, True), (3, False), (4, True)], 10, 0, 2),
        ([(1, True), (2, True), (3, False)], 4, 0, 2),
        ([(8, True), (9, True), (10, True), (11, True)], 10, 4, 4),
        ([(7, True), (8, True), (10, True)], 11, 1, 2),
    ],
)
async def test_streaks_use_all_results_and_break_on_gaps_or_losses(database, history, today_round, current, best):
    for round_id, won in history:
        add_rounds(database, start=round_id, frame=1 if won else None, title=None)
    result = await insight.game_insight(1, 'framed', round_date('framed', today_round))
    assert (result.current_streak, result.best_streak) == (current, best)
    assert result.genres.best == result.decades.best == result.people.best == ()


@pytest.mark.asyncio
async def test_current_board_ranks_include_players_outside_top_ten_and_no_metadata(database):
    for user in range(1, 13):
        add_rounds(database, user=user, count=10, frame=6 if user == 12 else 1, title=None)
    result = await insight.game_insight(12, 'framed', TODAY)
    assert [(rank.board, rank.place, rank.total, rank.label) for rank in result.ranks] == [
        ('score', 12, 12, None),
        ('frame', 12, 12, None),
        ('win', 12, 12, None),
        ('rounds', 12, 12, None),
    ]


@pytest.mark.asyncio
async def test_frame_rank_needs_ten_valid_rounds_and_missing_player_has_no_ranks(database):
    add_rounds(database, count=9, title=None)
    add_rounds(database, start=10, title=None)
    database.execute('UPDATE framed_result SET win_frame = NULL WHERE framed_round = 10')
    result = await insight.game_insight(1, 'framed', TODAY)
    assert [(rank.board, rank.place, rank.total) for rank in result.ranks] == [
        ('score', 1, 1),
        ('win', 1, 1),
        ('rounds', 1, 1),
    ]
    assert (await insight.game_insight(2, 'framed', TODAY)).ranks == ()


@pytest.mark.asyncio
async def test_leagues_use_split_weights_global_game_mean_and_picker_order(database):
    add_title(database, genres=(1, 2))
    add_title(database, 2, genres=(), year=None)
    database.execute("UPDATE genre SET name = 'Zebra' WHERE id = 1")
    database.execute("UPDATE genre SET name = 'Action' WHERE id = 2")
    for user, frame in ((1, 1), (2, 3), (3, None)):
        add_rounds(database, user=user, count=30, frame=frame)
    add_rounds(database, user=4, start=31, count=10, title=2)
    add_title(database, 3, kind='tv')
    add_rounds(database, user=1, count=50, game='episode', title=3)

    choices = await insight.league_choices('framed', 'genre', TODAY)
    assert [(choice.key, choice.label) for choice in choices] == [(2, 'Action'), (1, 'Zebra')]
    assert await insight.league_choices('episode', 'genre', TODAY) == ()
    assert await insight.league('framed', 'genre', 999, TODAY) is None
    board = await insight.league('framed', 'genre', 1, TODAY)
    assert board is not None
    assert (board.dimension, board.key, board.label) == ('genre', 1, 'Zebra')
    assert [(row.user_id, row.name, row.rounds, row.win_rate) for row in board.rows] == [
        (1, 'User 01', 15.0, 1.0),
        (2, 'User 02', 15.0, 1.0),
        (3, 'User 03', 15.0, 0.0),
    ]
    assert [row.score for row in board.rows] == pytest.approx([5.16, 3.96, 2.16])
    decade = await insight.league('framed', 'decade', 1990, TODAY)
    assert decade is not None
    assert [row.score for row in decade.rows] == pytest.approx([5.475, 3.975, 1.725])
    assert [row.rounds for row in decade.rows] == [30.0, 30.0, 30.0]
    assert [(c.key, c.label) for c in await insight.league_choices('framed', 'decade', TODAY)] == [(1990, '1990-е')]


@pytest.mark.asyncio
async def test_league_needs_three_players_with_fifteen_weighted_rounds(database):
    add_title(database, genres=(1, 2))
    add_rounds(database, user=1, count=30)
    add_rounds(database, user=2, count=30)
    add_rounds(database, user=3, count=29)
    assert await insight.league('framed', 'genre', 1, TODAY) is None
    assert await insight.league_choices('framed', 'genre', TODAY) == ()
    add_rounds(database, user=3, start=30)
    board = await insight.league('framed', 'genre', 1, TODAY)
    assert board is not None
    assert [row.user_id for row in board.rows] == [1, 2, 3]


@pytest.mark.asyncio
async def test_league_ranks_use_full_board_and_league_ties_use_rounds_name_and_id(database):
    add_title(database)
    for user in range(1, 13):
        add_rounds(database, user=user, count=16 if user == 1 else 15)
    database.execute("UPDATE user SET full_name = 'A' WHERE id IN (2, 3)")
    board = await insight.league('framed', 'genre', 1, TODAY)
    assert board is not None
    assert [row.user_id for row in board.rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    result = await insight.game_insight(12, 'framed', TODAY)
    assert [(r.board, r.place, r.total, r.label) for r in result.ranks if r.board in ('genre', 'decade')] == [
        ('genre', 12, 12, 'Genre 1'),
        ('decade', 12, 12, '1990-е'),
    ]


@pytest.mark.asyncio
async def test_best_league_rank_uses_place_then_larger_board_then_label(database):
    groups = [(1, 2, 3, 4, 5), (3, 4, 5), (5, 6, 7), (5, 6, 7, 8), (5, 6, 7, 8)]
    for key, users in enumerate(groups, 1):
        add_title(database, key, genres=(key,), year=1950 + key * 10)
        for user in users:
            add_rounds(database, user=user, start=key * 20, count=15, title=key)
    database.execute("UPDATE genre SET name = 'A' WHERE id = 5")
    result = await insight.game_insight(5, 'framed', TODAY)
    assert [(r.board, r.place, r.total, r.label) for r in result.ranks if r.board in ('genre', 'decade')] == [
        ('genre', 1, 4, 'A'),
        ('decade', 1, 4, '1990-е'),
    ]
    # The 2000s board has the same place and size; the earlier decade label wins.
    assert [(c.key, c.label) for c in await insight.league_choices('framed', 'decade', TODAY)] == [
        (1960, '1960-е'),
        (1970, '1970-е'),
        (1980, '1980-е'),
        (1990, '1990-е'),
        (2000, '2000-е'),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('game', 'kind', 'high', 'mixed'), [('framed', 'movie', 6.0, 3.0), ('episode', 'tv', 10.0, 4.6)]
)
async def test_hard_titles_count_rounds_not_titles_and_sort_ties_by_players_then_newness(
    database, game, kind, high, mixed
):
    add_title(database, genres=(1, 2), kind=kind)
    for round_id, players in ((1, 5), (2, 6), (3, 6), (4, 5), (5, 5), (6, 6), (7, 6), (8, 4), (9, 5)):
        for user in range(1, players + 1):
            frame = 1 if round_id >= 5 or (round_id == 4 and user <= 2) else None
            add_rounds(database, user=user, start=round_id, frame=frame, game=game)
    database.execute(f'UPDATE {game}_result SET win_frame = NULL WHERE framed_round = 9 AND user_id = 1')  # noqa: S608
    for user in range(1, 7):
        add_rounds(database, user=user, start=10, game=game, status='unmatched')
        add_rounds(database, user=user, start=11, game=game)

    result = await insight.hard_titles(game, round_date(game, 11))

    assert [row.round for row in result.hardest] == [3, 2, 1, 4, 7]
    assert [row.round for row in result.easiest] == [7, 6, 5, 4, 3]
    assert [(row.name, row.year, row.players) for row in result.hardest] == [
        ('Title 1', 1999, 6),
        ('Title 1', 1999, 6),
        ('Title 1', 1999, 5),
        ('Title 1', 1999, 5),
        ('Title 1', 1999, 6),
    ]
    assert [row.points for row in result.hardest] == pytest.approx([1.0, 1.0, 1.0, mixed, high])
    assert [row.win_rate for row in result.hardest] == [0.0, 0.0, 0.0, 0.4, 1.0]


@pytest.mark.asyncio
async def test_hard_titles_empty_game_has_no_rows(database):
    result = await insight.hard_titles('episode', TODAY)
    assert result.hardest == result.easiest == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('count', 'best_keys', 'worst_keys', 'best_decades', 'worst_decades'),
    [
        (6, [1, 2, 3, 4, 5, 6], [], ['1910-е', '1920-е', '1930-е', '1940-е', '1950-е', '1960-е'], []),
        (7, [1, 7, 2], [4, 5, 6], ['1910-е', '1970-е', '1920-е'], ['1940-е', '1950-е', '1960-е']),
    ],
)
async def test_personal_sections_show_all_up_to_six_else_best_and_worst_three(
    database, count, best_keys, worst_keys, best_decades, worst_decades
):
    for key in range(1, count + 1):
        add_title(database, key, genres=(key,), year=1900 + key * 10)
        add_rounds(database, start=key * 10, count=5, title=key, frame=key if key <= 6 else 1)
    result = await insight.game_insight(1, 'framed', TODAY)
    assert [b.key for b in result.genres.best] == best_keys
    assert [b.key for b in result.genres.worst] == worst_keys
    assert [b.label for b in result.decades.best] == best_decades
    assert [b.label for b in result.decades.worst] == worst_decades


@pytest.mark.asyncio
async def test_fractional_genres_at_exact_gates_do_not_lose_players_to_float_error(database):
    add_title(database, genres=(1, 2, 3, 4, 5, 6))
    for user in (1, 2, 3):
        add_rounds(database, user=user, count=90)
    board = await insight.league('framed', 'genre', 1, TODAY)
    assert board is not None
    assert [row.rounds for row in board.rows] == pytest.approx([15.0, 15.0, 15.0])
    assert [row.score for row in board.rows] == pytest.approx([6.0, 6.0, 6.0])
    assert len(await insight.league_choices('framed', 'genre', TODAY)) == 6


@pytest.mark.asyncio
async def test_genre_win_rates_use_the_same_split_weights_as_points(database):
    add_title(database, genres=(1, 2))
    add_title(database, 2, genres=(1,))
    add_rounds(database, count=10)
    add_rounds(database, start=11, count=5, title=2, frame=None)
    result = await insight.game_insight(1, 'framed', TODAY)
    assert [(b.key, b.rounds, b.win_rate) for b in result.genres.best] == [(2, 5.0, 1.0), (1, 10.0, 0.5)]
    assert [b.points for b in result.genres.best] == pytest.approx([4.8888888889, 3.9166666667])


@pytest.mark.asyncio
async def test_people_show_only_the_best_three_by_smoothed_points(database):
    for key in range(1, 5):
        add_title(database, key, genres=())
        database.execute('INSERT INTO person VALUES (?, ?)', (key, f'Person {5 - key}'))
        database.execute('INSERT INTO title_person VALUES (?, ?, ?, ?)', (key, key, 'director', 0))
        add_rounds(database, start=key * 10, count=3, title=key, frame=key)
    result = await insight.game_insight(1, 'framed', TODAY)
    assert [b.key for b in result.people.best] == [1, 2, 3]
    assert [b.points for b in result.people.best] == pytest.approx([4.8461538462, 4.6153846154, 4.3846153846])
    assert result.people.worst == ()
