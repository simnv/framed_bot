import dataclasses
from collections.abc import Sequence
from typing import override

from sqlalchemy import func, select

from models.db import AsyncScopedSession, ResultForStats
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.user import User


@dataclasses.dataclass(frozen=True)
class Stats:
    rounds_count: int
    rounds_won_count: int
    average_frame: float | None


@dataclasses.dataclass(frozen=True, slots=True)
class MissingWinFrameError(Exception):
    result_index: int | None

    @override
    def __str__(self) -> str:
        if self.result_index is None:
            return 'winning result has no win_frame'
        return f'winning result at index {self.result_index} has no win_frame'


async def count_stats(results: Sequence[ResultForStats]) -> Stats:
    rounds_count = len(results)
    rounds_won = [result for result in results if result.won]
    rounds_won_count = len(rounds_won)
    total_frames = 0
    for result_index, result in enumerate(rounds_won):
        if result.win_frame is None:
            raise MissingWinFrameError(result_index=result_index)
        total_frames += result.win_frame
    average_frame = (total_frames / rounds_won_count) if rounds_won_count else None

    return Stats(rounds_count=rounds_count, rounds_won_count=rounds_won_count, average_frame=average_frame)


async def get_user_stats(user_id: int) -> tuple[Stats, Stats] | None:
    """Return fixed-size aggregates without hydrating either result history."""
    async with AsyncScopedSession() as session:
        if (await session.execute(select(User.id).where(User.id == user_id))).scalar_one_or_none() is None:
            return None
        game_stats: list[Stats] = []
        for model in (FramedResult, EpisodeResult):
            rounds, wins, frames, missing = (
                await session.execute(
                    select(
                        func.count(model.id),
                        func.count(model.id).filter(model.won.is_(True)),
                        func.sum(model.win_frame).filter(model.won.is_(True)),
                        func.count(model.id).filter(model.won.is_(True), model.win_frame.is_(None)),
                    ).where(model.user_id == user_id)
                )
            ).one()
            if missing:
                raise MissingWinFrameError(result_index=None)
            game_stats.append(Stats(rounds, wins, (frames or 0) / wins if wins else None))
        return game_stats[0], game_stats[1]
