"""Personal insight and metadata leaderboards from TMDB title data.

See docs/superpowers/specs/2026-11-01-metadata-stats-design.md for every rule. Every number is per chat: a group sees
its own results, a private chat sees all chats merged (see `models.top.scoped_results`).
"""

from __future__ import annotations

import dataclasses
from datetime import date
from fractions import Fraction
from typing import Any, Final

from sqlalchemy import Integer, Select, and_, case, func, literal, or_, select
from sqlalchemy.sql.selectable import CTE

from answers import FRAMES, KIND_BY_GAME, Game, current_round
from insight_types import (
    CAST_MAX_ORDER,
    ELIGIBLE_STATUSES,
    LEAGUE_LIMIT,
    MIN_LEAGUE_PLAYERS,
    MIN_LEAGUE_ROUNDS,
    MIN_STATS_BUCKET,
    MIN_STATS_PERSON,
    MIN_TITLE_PLAYERS,
    PEOPLE_BEST,
    PEOPLE_WORST,
    SMOOTHING,
    STATS_BEST,
    TITLE_LIMIT,
    Bucket,
    Choice,
    Dimension,
    GameInsight,
    HardTitles,
    League,
    LeagueRow,
    Rank,
    RankBoard,
    Role,
    Section,
    TitleRow,
    decade_label,
)
from models.db import AsyncScopedSession
from models.game_answer import GameAnswer
from models.title import Genre, Person, Title, TitleGenre, TitlePerson
from models.top import scoped_results, top_statement
from models.user import User
from top_type import TopType

__all__ = [
    'Bucket',
    'CAST_MAX_ORDER',
    'Choice',
    'Dimension',
    'ELIGIBLE_STATUSES',
    'GameInsight',
    'HardTitles',
    'LEAGUE_LIMIT',
    'League',
    'LeagueRow',
    'MIN_LEAGUE_PLAYERS',
    'MIN_LEAGUE_ROUNDS',
    'MIN_STATS_BUCKET',
    'MIN_STATS_PERSON',
    'MIN_TITLE_PLAYERS',
    'Rank',
    'RankBoard',
    'Role',
    'SMOOTHING',
    'PEOPLE_BEST',
    'PEOPLE_WORST',
    'STATS_BEST',
    'Section',
    'TITLE_LIMIT',
    'TitleRow',
    'decade_label',
    'game_insight',
    'hard_titles',
    'league',
    'league_choices',
]


def _eligible(game: Game, today: date, chat_id: int | None) -> CTE:
    results = scoped_results(game, chat_id)
    won = results.c.won.is_(True)
    return (
        select(
            results.c.user_id.label('user_id'),
            User.full_name.label('name'),
            results.c.framed_round.label('round'),
            Title.id.label('title_id'),
            Title.name.label('title_name'),
            Title.year.label('year'),
            case((won, FRAMES[game] + 1 - results.c.win_frame), else_=1).label('points'),
            case((won, 1), else_=0).label('won'),
        )
        .select_from(results)
        .join(User, User.id == results.c.user_id)
        .join(GameAnswer, and_(GameAnswer.game == game, GameAnswer.round == results.c.framed_round))
        .join(Title, Title.id == GameAnswer.title_id)
        .where(
            ~and_(won, results.c.win_frame.is_(None)),
            results.c.framed_round < current_round(game, today),
            GameAnswer.match_status.in_(ELIGIBLE_STATUSES),
            Title.kind == KIND_BY_GAME[game],
        )
        .cte('eligible')
    )


def _mean_statement(eligible: CTE) -> Select:
    return select(func.sum(eligible.c.points).label('total'), func.count().label('count'))


async def _rows(statement: Select) -> list[Any]:
    async with AsyncScopedSession() as session:
        return list((await session.execute(statement)).all())


# Weights are integers scaled by this number, so SQL sums them exactly. It is the lowest common multiple of
# 1 to 16, so a title with up to 16 genres gives each genre an exact share.
WEIGHT_SCALE: Final = 720_720


def _bucket_statement(eligible: CTE, dimension: Dimension) -> Select:
    source = eligible
    weight: Any = literal(WEIGHT_SCALE, Integer)
    if dimension == 'genre':
        counts = select(TitleGenre.title_id, func.count().label('n')).group_by(TitleGenre.title_id).subquery()
        source = (
            eligible.join(TitleGenre, TitleGenre.title_id == eligible.c.title_id)
            .join(Genre, Genre.id == TitleGenre.genre_id)
            .join(counts, counts.c.title_id == eligible.c.title_id)
        )
        key = Genre.id
        label = Genre.name
        # Integer division: the share of one genre of a title with n genres, times WEIGHT_SCALE.
        weight = weight // counts.c.n
    else:
        key = (eligible.c.year // 10) * 10
        label = key
    statement = (
        select(
            eligible.c.user_id,
            eligible.c.name,
            key.label('key'),
            label.label('label'),
            func.sum(weight).label('rounds'),
            func.sum(weight * eligible.c.points).label('points'),
            func.sum(weight * eligible.c.won).label('wins'),
        )
        .select_from(source)
        .group_by(eligible.c.user_id, eligible.c.name, key, label)
    )
    return statement.where(eligible.c.year.is_not(None)) if dimension == 'decade' else statement


def _exact(total: Any, scale: int = WEIGHT_SCALE) -> Fraction:
    """A scaled integer sum from SQL as an exact fraction. PostgreSQL returns sums as Decimal."""
    return Fraction(int(total), scale)


async def _mean(statement: Select) -> Fraction:
    """The exact mean points of a select of (total, count)."""
    (row,) = await _rows(statement)
    return Fraction(int(row.total), int(row.count)) if row.count else Fraction(0)


def _bucket(row: Any, mean: Fraction, dimension: Dimension, scale: int = WEIGHT_SCALE) -> Bucket:
    """Smoothed points with exact fractions, so equal buckets tie exactly and the tie rules decide."""
    rounds = _exact(row.rounds, scale)
    smoothed = (_exact(row.points, scale) + SMOOTHING * mean) / (rounds + SMOOTHING)
    return Bucket(
        key=row.key,
        label=decade_label(row.key) if dimension == 'decade' else row.label,
        rounds=float(rounds),
        points=float(smoothed),
        win_rate=float(_exact(row.wins, scale) / rounds),
        strength=float(smoothed - mean),
    )


def _by_strength(bucket: Bucket) -> tuple[float, str, int]:
    return -bucket.strength, bucket.label, bucket.key


def _section(buckets: list[Bucket], dimension: Dimension) -> Section:
    """Genres come most played first. Decades come strongest first."""
    if dimension == 'genre':
        buckets.sort(key=lambda bucket: (-bucket.rounds, bucket.label, bucket.key))
    else:
        buckets.sort(key=_by_strength)
    return Section(tuple(buckets))


async def _people(eligible: CTE, user_id: int, game: Game, mean: Fraction) -> Section:
    """One row per person. A person who directs (or creates) one title and plays in another counts both rounds."""
    role = 'director' if game == 'framed' else 'creator'
    # One credit per title and person, so a person with two roles in one title counts that round once.
    credits = (
        select(
            TitlePerson.title_id,
            TitlePerson.person_id,
            func.max(case((TitlePerson.role == role, 1), else_=0)).label('leads'),
        )
        .where(or_(TitlePerson.role == role, and_(TitlePerson.role == 'cast', TitlePerson.order <= CAST_MAX_ORDER)))
        .group_by(TitlePerson.title_id, TitlePerson.person_id)
        .subquery()
    )
    rows = await _rows(
        select(
            Person.id.label('key'),
            Person.name.label('label'),
            func.max(credits.c.leads).label('leads'),
            func.count().label('rounds'),
            func.sum(eligible.c.points).label('points'),
            func.sum(eligible.c.won).label('wins'),
        )
        .select_from(eligible)
        .join(credits, credits.c.title_id == eligible.c.title_id)
        .join(Person, Person.id == credits.c.person_id)
        .where(eligible.c.user_id == user_id)
        .group_by(Person.id, Person.name)
        .having(func.count() >= MIN_STATS_PERSON)
    )
    # The main role of the game wins over cast.
    buckets = [
        dataclasses.replace(_bucket(row, mean, 'genre', scale=1), role=role if row.leads else 'cast') for row in rows
    ]
    buckets.sort(key=_by_strength)
    if len(buckets) <= PEOPLE_BEST + PEOPLE_WORST:
        return Section(tuple(buckets))
    return Section(tuple(buckets[:PEOPLE_BEST]), tuple(buckets[-PEOPLE_WORST:]))


async def _streaks(user_id: int, game: Game, today: date, chat_id: int | None) -> tuple[int, int]:
    """Win runs over consecutive round numbers, found in SQL so the bot does not load the history."""
    results = scoped_results(game, chat_id)
    won = results.c.won.is_(True)
    valid = (
        select(results.c.framed_round.label('round'), won.label('won'))
        .where(results.c.user_id == user_id, ~and_(won, results.c.win_frame.is_(None)))
        .subquery()
    )
    wins = (
        select(
            valid.c.round,
            # Consecutive won rounds share one value; a gap or a loss changes it.
            (valid.c.round - func.row_number().over(order_by=valid.c.round)).label('island'),
        )
        .where(valid.c.won.is_(True))
        .subquery()
    )
    islands = (
        select(func.count().label('length'), func.max(wins.c.round).label('last')).group_by(wins.c.island).subquery()
    )
    latest = select(func.max(valid.c.round)).scalar_subquery()
    (row,) = await _rows(
        select(
            func.max(islands.c.length).label('best'),
            func.max(case((islands.c.last == latest, islands.c.length), else_=0)).label('run'),
            latest.label('latest'),
        )
    )
    if row.latest is None or row.best is None:
        return 0, 0
    current = row.run if row.latest >= current_round(game, today) - 1 else 0
    return int(current), int(row.best)


async def _board_ranks(user_id: int, game: Game, chat_id: int | None) -> list[Rank]:
    ranks = []
    boards: tuple[tuple[TopType, RankBoard], ...] = (
        (TopType.TOP_SCORE, 'score'),
        (TopType.TOP_FRAME, 'frame'),
        (TopType.TOP_WIN, 'win'),
        (TopType.TOP_ROUNDS, 'rounds'),
    )
    for metric, board in boards:
        rows = await _rows(top_statement(game, metric, chat_id, limit=None))
        for place, row in enumerate(rows, 1):
            if row.user_id == user_id:
                ranks.append(Rank(board, place, len(rows)))
                break
    return ranks


async def game_insight(user_id: int, game: Game, today: date, chat_id: int | None) -> GameInsight:
    eligible = _eligible(game, today, chat_id)
    mean = await _mean(_mean_statement(eligible).where(eligible.c.user_id == user_id))
    sections = []
    for dimension in ('genre', 'decade'):
        rows = await _rows(_bucket_statement(eligible, dimension).where(eligible.c.user_id == user_id))
        sections.append(
            _section(
                [_bucket(row, mean, dimension) for row in rows if _exact(row.rounds) >= MIN_STATS_BUCKET], dimension
            )
        )
    current, best = await _streaks(user_id, game, today, chat_id)
    ranks = await _board_ranks(user_id, game, chat_id)
    dimensions: tuple[Dimension, ...] = ('genre', 'decade')
    for dimension in dimensions:
        candidates = []
        for board in await _league_boards(eligible, game, dimension):
            for place, row in enumerate(board.rows, 1):
                if row.user_id == user_id:
                    candidates.append(Rank(dimension, place, len(board.rows), board.label))
                    break
        if candidates:
            ranks.append(min(candidates, key=lambda rank: (rank.place, -rank.total, rank.label or '')))
    return GameInsight(
        sections[0], sections[1], await _people(eligible, user_id, game, mean), current, best, tuple(ranks)
    )


async def _league_boards(eligible: CTE, game: Game, dimension: Dimension) -> list[League]:
    mean = await _mean(_mean_statement(eligible))
    # The own mean of each player gives the personal strength, the same number as in /stats.
    own = {
        row.user_id: Fraction(int(row.total), int(row.count))
        for row in await _rows(_mean_statement(eligible).add_columns(eligible.c.user_id).group_by(eligible.c.user_id))
    }
    rows = await _rows(_bucket_statement(eligible, dimension))
    grouped: dict[int, list[LeagueRow]] = {}
    labels: dict[int, str] = {}
    for row in rows:
        if _exact(row.rounds) < MIN_LEAGUE_ROUNDS[game]:
            continue
        bucket = _bucket(row, mean, dimension)
        labels[row.key] = bucket.label
        grouped.setdefault(row.key, []).append(
            LeagueRow(
                row.user_id,
                row.name,
                bucket.points,
                bucket.rounds,
                bucket.win_rate,
                bucket.strength,
                _bucket(row, own[row.user_id], dimension).strength,
            )
        )
    boards = []
    for key, players in grouped.items():
        if len(players) < MIN_LEAGUE_PLAYERS:
            continue
        players.sort(key=lambda player: (-player.score, -player.rounds, player.name or '', player.user_id))
        boards.append(League(dimension, key, labels[key], tuple(players)))
    return boards


async def league_choices(game: Game, dimension: Dimension, today: date, chat_id: int | None) -> tuple[Choice, ...]:
    boards = await _league_boards(_eligible(game, today, chat_id), game, dimension)
    boards.sort(key=lambda board: board.key if dimension == 'decade' else board.label)
    return tuple(Choice(board.key, board.label) for board in boards)


async def league(game: Game, dimension: Dimension, key: int, today: date, chat_id: int | None) -> League | None:
    """None when the board does not exist (unknown key or fewer than MIN_LEAGUE_PLAYERS players)."""
    boards = await _league_boards(_eligible(game, today, chat_id), game, dimension)
    for board in boards:
        if board.key == key:
            return dataclasses.replace(board, rows=board.rows[:LEAGUE_LIMIT])
    return None


async def hard_titles(game: Game, today: date, chat_id: int | None) -> HardTitles:
    eligible = _eligible(game, today, chat_id)
    rows = await _rows(
        select(
            eligible.c.round,
            eligible.c.title_name.label('name'),
            eligible.c.year,
            func.count().label('players'),
            func.avg(eligible.c.won).label('win_rate'),
            func.avg(eligible.c.points).label('points'),
        )
        .group_by(eligible.c.round, eligible.c.title_name, eligible.c.year)
        .having(func.count() >= MIN_TITLE_PLAYERS)
    )
    titles = [
        TitleRow(row.round, row.name, row.year, row.players, float(row.win_rate), float(row.points)) for row in rows
    ]
    hardest = sorted(titles, key=lambda title: (title.points, -title.players, -title.round))
    easiest = sorted(titles, key=lambda title: (-title.points, -title.players, -title.round))
    return HardTitles(tuple(hardest[:TITLE_LIMIT]), tuple(easiest[:TITLE_LIMIT]))
