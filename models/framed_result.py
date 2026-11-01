from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from answers import FRAMES

from .db import AsyncScopedSession, Base


class FramedResult(Base):
    __tablename__ = 'framed_result'
    __table_args__ = (
        UniqueConstraint('user_id', 'framed_round'),
        CheckConstraint(
            f'(won AND win_frame IS NOT NULL AND win_frame BETWEEN 1 AND {FRAMES["framed"]}) '
            'OR (NOT won AND win_frame IS NULL)',
            name='ck_framed_result_win_frame',
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('user.id'), index=True)
    framed_round: Mapped[int] = mapped_column(index=True)
    won: Mapped[bool] = mapped_column()
    win_frame: Mapped[int | None] = mapped_column()

    @staticmethod
    async def save_result(user_id: int, framed_round: int, won: bool, win_frame: int | None) -> bool:
        async with AsyncScopedSession() as session:
            result = await session.execute(
                insert(FramedResult)
                .values(user_id=user_id, framed_round=framed_round, won=won, win_frame=win_frame)
                .on_conflict_do_nothing(index_elements=[FramedResult.user_id, FramedResult.framed_round])
                .returning(FramedResult.id)
            )
            saved = result.scalar_one_or_none() is not None
            await session.commit()
            return saved
