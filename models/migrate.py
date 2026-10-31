"""Bring the database schema to the newest Alembic revision."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from .db import engine

BASELINE = '0001'
BASELINE_TABLES = frozenset({'user', 'group', 'framed_result', 'episode_result'})


class PartialSchemaError(RuntimeError):
    pass


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).with_name('migrations')))
    config.attributes['connection'] = connection
    return config


def upgrade(connection: Connection) -> None:
    config = alembic_config(connection)
    tables = set(inspect(connection).get_table_names())
    if 'alembic_version' not in tables:
        present = tables & BASELINE_TABLES
        if present == BASELINE_TABLES:
            # create_all made this schema before Alembic; it matches the baseline revision.
            command.stamp(config, BASELINE)
        elif present:
            raise PartialSchemaError(f'Pre-Alembic schema has only {sorted(present)}; repair it manually')
    command.upgrade(config, 'head')


async def upgrade_database() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(upgrade)
