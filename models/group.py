from __future__ import annotations

from sqlalchemy import Select, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger
from telegram import Chat as TgChat

from .db import AsyncScopedSession, Base


class Group(Base):
    __tablename__ = 'group'

    id: Mapped[int] = mapped_column('id', BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column()

    @staticmethod
    async def update_from_tg_chat(tg_chat: TgChat) -> None:
        async with AsyncScopedSession() as session:
            statement = insert(Group).values(id=tg_chat.id, title=tg_chat.title or '')
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Group.id], set_={'title': statement.excluded.title},
                )
            )
            await session.commit()

    @staticmethod
    async def get(group_id: int) -> Group | None:
        async with AsyncScopedSession() as session:
            result = await session.execute(Select(Group).filter(Group.id == group_id))
            return result.scalars().first()

    @staticmethod
    async def get_all() -> list[Group]:
        async with AsyncScopedSession() as session:
            result = await session.execute(Select(Group))
            return list(result.scalars().all())

    @staticmethod
    async def remove(group_id: int) -> None:
        async with AsyncScopedSession() as session:
            await session.execute(delete(Group).where(Group.id == group_id))
            await session.commit()
