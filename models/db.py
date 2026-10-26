from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DB_CONNECTION_STRING

engine = create_async_engine(
    DB_CONNECTION_STRING,
    pool_pre_ping=True,
)


# async_sessionmaker: a factory for new AsyncSession objects.
# expire_on_commit - don't expire objects after transaction commit
async_session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
)
AsyncScopedSession = async_session_factory


class Base(DeclarativeBase):
    pass


class ResultForStats(Protocol):
    won: bool
    win_frame: int | None
