"""Move the results and the group row of a chat to a new chat id after a supergroup migration."""

from __future__ import annotations

from sqlalchemy import delete, func, select, update

from .db import AsyncScopedSession
from .episode_result import EpisodeResult
from .framed_result import FramedResult
from .group import Group


async def move_chat(old_id: int, new_id: int) -> None:
    """Results of old_id get chat_id new_id. When both chats have a row for one user and round, keep the row
    saved first (the lowest id). The group row follows, unless new_id already has one. Safe to call twice."""
    async with AsyncScopedSession() as session:
        for model in (FramedResult, EpisodeResult):
            both = model.chat_id.in_((old_id, new_id))
            first = select(func.min(model.id)).where(both).group_by(model.user_id, model.framed_round)
            await session.execute(delete(model).where(both, model.id.not_in(first)))
            await session.execute(update(model).where(model.chat_id == old_id).values(chat_id=new_id))
        if await session.scalar(select(Group.id).where(Group.id == new_id)) is None:
            await session.execute(update(Group).where(Group.id == old_id).values(id=new_id))
        else:
            await session.execute(delete(Group).where(Group.id == old_id))
        await session.commit()
