"""Alembic environment: migrate with a caller connection, or with the application engine from the CLI."""
import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from models import Base
from models.db import engine

target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_with_engine() -> None:
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=engine.url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif (connection := context.config.attributes.get('connection')) is not None:
    run_migrations(connection)
else:
    asyncio.run(run_with_engine())
