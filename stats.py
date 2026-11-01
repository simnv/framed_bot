import dataclasses

from sqlalchemy import func, select

from answers import FRAMES
from models.db import AsyncScopedSession
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.user import User


@dataclasses.dataclass(frozen=True)
class Stats:
    rounds_count: int
    rounds_won_count: int
    average_frame: float | None
    points: int
    average_frame_all: float | None

    @classmethod
    def from_totals(cls, rounds: int, wins: int, win_frames: int, *, frames: int) -> Stats:
        """Score like /top: a win earns (frames + 1 - win_frame), a miss earns 1 and counts as frame frames + 1."""
        miss_frame = frames + 1
        losses = rounds - wins
        return cls(
            rounds_count=rounds,
            rounds_won_count=wins,
            average_frame=win_frames / wins if wins else None,
            points=wins * miss_frame - win_frames + losses,
            average_frame_all=(win_frames + losses * miss_frame) / rounds if rounds else None,
        )


class MissingWinFrameError(Exception):
    """A won result has no win frame, so the bot cannot give a correct average."""

    def __init__(self) -> None:
        super().__init__('winning result has no win_frame')


async def get_user_stats(user_id: int) -> tuple[Stats, Stats] | None:
    """Return fixed-size aggregates without hydrating either result history."""
    async with AsyncScopedSession() as session:
        if (await session.execute(select(User.id).where(User.id == user_id))).scalar_one_or_none() is None:
            return None
        game_stats: list[Stats] = []
        for model, frames in ((FramedResult, FRAMES['framed']), (EpisodeResult, FRAMES['episode'])):
            rounds, wins, win_frames, missing = (
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
                raise MissingWinFrameError
            game_stats.append(Stats.from_totals(rounds, wins, win_frames or 0, frames=frames))
        return game_stats[0], game_stats[1]
