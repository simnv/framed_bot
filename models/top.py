"""Leaderboards for each game, built from one query shape for every result table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import Float, Row, Select, and_, case, cast, func, select

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


def top_statement(game: Game, top_type: TopType) -> Select[int, str, Any]:
    """Return the ranked rows (user_id, name, score) of one leaderboard."""
    model = MODELS[game]
    miss = FRAMES[game] + 1
    won = model.won.is_(True)
    rounds = func.count()
    wins = func.sum(case((won, 1), else_=0))
    score = {
        TopType.TOP_SCORE: func.sum(case((won, miss - model.win_frame), else_=1)),
        TopType.TOP_FRAME: func.avg(cast(case((won, model.win_frame), else_=miss), Float)),
        TopType.TOP_WIN: wins,
        TopType.TOP_ROUNDS: rounds,
    }[top_type].label('score')
    statement = (
        select(model.user_id.label('user_id'), User.full_name.label('name'), score)
        .select_from(model)
        .join(User, User.id == model.user_id)
        # A won row without a frame is broken. It must not change a sum or an average.
        .where(~and_(won, model.win_frame.is_(None)))
        .group_by(model.user_id, User.full_name)
    )
    if top_type == TopType.TOP_FRAME:
        statement = statement.having(rounds >= MIN_ROUNDS_FOR_AVERAGE)
    # A lower average frame is better; for the other metrics a higher number is better.
    primary = score.asc() if top_type == TopType.TOP_FRAME else score.desc()
    secondary = wins if top_type == TopType.TOP_ROUNDS else rounds
    return statement.order_by(primary, secondary.desc(), User.full_name.asc(), model.user_id.asc()).limit(TOP_LIMIT)


async def top_results(game: Game, top_type: TopType) -> Sequence[Row[int, str, Any]]:
    async with AsyncScopedSession() as session:
        result = await session.execute(top_statement(game, top_type))
        return result.all()
