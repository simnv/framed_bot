from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, Select, UniqueConstraint, case, cast, desc, func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import AsyncScopedSession, Base
from .user import User


class FramedResult(Base):
    __tablename__ = 'framed_result'
    # Legacy imports need deduplication and a constraint migration; create_all does not migrate tables.
    __table_args__ = (UniqueConstraint('user_id', 'framed_round'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('user.id'), index=True)
    framed_round: Mapped[int] = mapped_column(index=True)
    won: Mapped[bool] = mapped_column()
    win_frame: Mapped[int | None] = mapped_column()

    user: Mapped[Base] = relationship('User', back_populates='framed_results')

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

    @staticmethod
    async def top_score():
        async with AsyncScopedSession() as session:
            result = await session.execute(
                Select(
                    FramedResult.user_id.label('user_id'),
                    User.full_name.label('name'),
                    func.sum(case((FramedResult.won.is_(True), 7 - FramedResult.win_frame), else_=1)).label('score'),
                )
                .select_from(FramedResult)
                .join(User, User.id == FramedResult.user_id)
                .group_by(FramedResult.user_id, User.full_name)
                .order_by(desc(text('score')))
                .limit(10)
            )
            return result.all()

    @staticmethod
    async def top_average_frame():
        async with AsyncScopedSession() as session:
            result = await session.execute(
                Select(
                    FramedResult.user_id.label('user_id'),
                    User.full_name.label('name'),
                    func.avg(cast(case((FramedResult.won.is_(True), FramedResult.win_frame), else_=7), Float)).label(
                        'score'
                    ),
                )
                .select_from(FramedResult)
                .join(User, User.id == FramedResult.user_id)
                .group_by(FramedResult.user_id, User.full_name)
                .order_by(text('score'))
                .limit(10)
            )
            return result.all()

    @staticmethod
    async def top_rounds():
        async with AsyncScopedSession() as session:
            result = await session.execute(
                Select(FramedResult.user_id.label('user_id'), User.full_name.label('name'), func.count().label('score'))
                .select_from(FramedResult)
                .join(User, User.id == FramedResult.user_id)
                .group_by(FramedResult.user_id, User.full_name)
                .order_by(desc(text('score')))
                .limit(10)
            )
            return result.all()

    @staticmethod
    async def top_won():
        async with AsyncScopedSession() as session:
            result = await session.execute(
                Select(
                    FramedResult.user_id.label('user_id'),
                    User.full_name.label('name'),
                    func.sum(cast(FramedResult.won, Integer)).label('score'),
                )
                .select_from(FramedResult)
                .join(User, User.id == FramedResult.user_id)
                .group_by(FramedResult.user_id, User.full_name)
                .order_by(desc(text('score')))
                .limit(10)
            )
            return result.all()
