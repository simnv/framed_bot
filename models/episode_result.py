from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from answers import FRAMES

from .db import AsyncScopedSession, Base


class EpisodeResult(Base):
    __tablename__ = 'episode_result'
    __table_args__ = (
        # One result per chat, user and round. A user who posts one round in two groups has a row in each.
        UniqueConstraint('chat_id', 'user_id', 'framed_round', name='episode_result_chat_id_user_id_framed_round_key'),
        CheckConstraint(
            f'(won AND win_frame IS NOT NULL AND win_frame BETWEEN 1 AND {FRAMES["episode"]}) '
            'OR (NOT won AND win_frame IS NULL)',
            name='ck_episode_result_win_frame',
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The group that received the result. No foreign key: a group row is removed when the bot leaves.
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('user.id'), index=True)
    framed_round: Mapped[int] = mapped_column(index=True)
    won: Mapped[bool] = mapped_column()
    win_frame: Mapped[int | None] = mapped_column()

    @staticmethod
    async def save_result(user_id: int, framed_round: int, won: bool, win_frame: int | None, *, chat_id: int) -> bool:
        async with AsyncScopedSession() as session:
            result = await session.execute(
                insert(EpisodeResult)
                .values(chat_id=chat_id, user_id=user_id, framed_round=framed_round, won=won, win_frame=win_frame)
                .on_conflict_do_nothing(
                    index_elements=[EpisodeResult.chat_id, EpisodeResult.user_id, EpisodeResult.framed_round]
                )
                .returning(EpisodeResult.id)
            )
            saved = result.scalar_one_or_none() is not None
            await session.commit()
            return saved
