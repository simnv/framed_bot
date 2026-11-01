from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from answers.store import _upsert_names, _upsert_title
from answers.tmdb import Credit, TitleData
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


@pytest.mark.asyncio
async def test_title_links_deduplicate_ids_and_person_roles() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = Mock(scalar_one=Mock(return_value=42))
    data = TitleData(
        1,
        'movie',
        'Film',
        'Film',
        None,
        None,
        [],
        None,
        [(35, 'Comedy'), (18, 'Drama'), (35, 'Comedy')],
        [(33, 'Studio'), (2, 'Other studio'), (33, 'Studio')],
        [Credit(7, 'Actor', 'cast', 0), Credit(7, 'Actor', 'cast', 1), Credit(7, 'Actor', 'director', 0)],
        {},
    )

    assert await _upsert_title(session, data) == 42

    rows = {call.args[0].table.name: call.args[1] for call in session.execute.await_args_list if len(call.args) == 2}
    assert rows['title_genre'] == [{'title_id': 42, 'genre_id': 18}, {'title_id': 42, 'genre_id': 35}]
    assert rows['title_company'] == [{'title_id': 42, 'company_id': 2}, {'title_id': 42, 'company_id': 33}]
    assert rows['title_person'] == [
        {'title_id': 42, 'person_id': 7, 'role': 'cast', 'order': 0},
        {'title_id': 42, 'person_id': 7, 'role': 'director', 'order': 0},
    ]
