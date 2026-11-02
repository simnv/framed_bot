"""Leaderboards for each game, built from one query shape for every result table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import Float, Row, Select, Subquery, and_, case, cast, func, select

from answers import FRAMES, Game
from top_type import TopType

from .db import AsyncScopedSession
from .episode_result import EpisodeResult
from .framed_result import FramedResult
from .user import User

MODELS: Final[dict[Game, type[FramedResult] | type[EpisodeResult]]] = {
    'framed': FramedResult,
    'episode': EpisodeResult,
}
TOP_LIMIT: Final = 10
MIN_ROUNDS_FOR_AVERAGE: Final = 10


def scoped_results(game: Game, chat_id: int | None) -> Subquery:
    """Result rows of one chat, or of all chats with one row per user and round (the lowest id).

    A private chat sees every chat. A user who posted one round in two groups counts once there: the row that
    the bot saved first.
    """
    model = MODELS[game]
    statement = select(
        model.id.label('id'),
        model.chat_id.label('chat_id'),
        model.user_id.label('user_id'),
        model.framed_round.label('framed_round'),
        model.won.label('won'),
        model.win_frame.label('win_frame'),
    )
    if chat_id is None:
        first = select(func.min(model.id)).group_by(model.user_id, model.framed_round)
        statement = statement.where(model.id.in_(first))
    else:
        statement = statement.where(model.chat_id == chat_id)
    return statement.subquery('results')


def top_statement(
    game: Game,
    top_type: TopType,
    chat_id: int | None,
    limit: int | None = TOP_LIMIT,
    *,
    rounds: range | None = None,
    min_rounds: int = MIN_ROUNDS_FOR_AVERAGE,
) -> Select[int, str, Any]:
    """Return the ranked rows (user_id, name, score) of one leaderboard.

    rounds limits the board to a round range (a period). min_rounds is the gate of the average-frame board.
    """
    results = scoped_results(game, chat_id)
    miss = FRAMES[game] + 1
    won = results.c.won.is_(True)
    played = func.count()
    wins = func.sum(case((won, 1), else_=0))
    score = {
        TopType.TOP_SCORE: func.sum(case((won, miss - results.c.win_frame), else_=1)),
        TopType.TOP_FRAME: func.avg(cast(case((won, results.c.win_frame), else_=miss), Float)),
        TopType.TOP_WIN: wins,
        TopType.TOP_ROUNDS: played,
    }[top_type].label('score')
    statement = (
        select(results.c.user_id.label('user_id'), User.full_name.label('name'), score)
        .select_from(results)
        .join(User, User.id == results.c.user_id)
        # A won row without a frame is broken. It must not change a sum or an average.
        .where(~and_(won, results.c.win_frame.is_(None)))
        .group_by(results.c.user_id, User.full_name)
    )
    if rounds is not None:
        statement = statement.where(results.c.framed_round >= rounds.start, results.c.framed_round < rounds.stop)
    if top_type == TopType.TOP_FRAME:
        statement = statement.having(played >= min_rounds)
    # A lower average frame is better; for the other metrics a higher number is better.
    primary = score.asc() if top_type == TopType.TOP_FRAME else score.desc()
    secondary = wins if top_type == TopType.TOP_ROUNDS else played
    return statement.order_by(primary, secondary.desc(), User.full_name.asc(), results.c.user_id.asc()).limit(limit)


async def top_results(
    game: Game,
    top_type: TopType,
    chat_id: int | None,
    *,
    rounds: range | None = None,
    min_rounds: int = MIN_ROUNDS_FOR_AVERAGE,
) -> Sequence[Row[int, str, Any]]:
    async with AsyncScopedSession() as session:
        result = await session.execute(top_statement(game, top_type, chat_id, rounds=rounds, min_rounds=min_rounds))
        return result.all()
