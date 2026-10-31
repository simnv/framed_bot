from __future__ import annotations

from sqlalchemy import BigInteger, Select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column
from telegram import User as TgUser

from .db import AsyncScopedSession, Base


class User(Base):
    __tablename__ = 'user'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column()
    username: Mapped[str] = mapped_column()

    @staticmethod
    async def update_from_tg_user(tg_user: TgUser) -> None:
        async with AsyncScopedSession() as session:
            statement = insert(User).values(
                id=tg_user.id, full_name=tg_user.full_name, username=tg_user.username or ''
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[User.id],
                    set_={'full_name': statement.excluded.full_name, 'username': statement.excluded.username},
                )
            )
            await session.commit()

    @staticmethod
    async def get(user_id: int) -> User | None:
        async with AsyncScopedSession() as session:
            result = await session.execute(Select(User).filter(User.id == user_id))
            return result.scalars().first()
