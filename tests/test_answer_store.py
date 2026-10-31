from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from answers.store import _upsert_names
from models.title import Genre, Person


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any) -> None:
        self.statements.append(statement)


@pytest.mark.parametrize('model', [Genre, Person])
@pytest.mark.asyncio
async def test_dimension_upsert_locks_rows_in_id_order(model: type[Genre | Person]) -> None:
    # Framed and Episode syncs run in parallel and share these rows; one id order prevents deadlocks.
    session = RecordingSession()

    await _upsert_names(cast(AsyncSession, session), model, {35: 'Comedy', 18: 'Drama', 27: 'Horror'})

    [statement] = session.statements
    params = statement.compile(dialect=postgresql.dialect()).params
    assert [params[f'id_m{index}'] for index in range(3)] == [18, 27, 35]


@pytest.mark.asyncio
async def test_dimension_upsert_skips_empty_rows() -> None:
    session = RecordingSession()

    await _upsert_names(cast(AsyncSession, session), Genre, {})

    assert session.statements == []
