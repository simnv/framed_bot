from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from answers.store import DbAnswerStore, _upsert_names, _upsert_title
from answers.tmdb import Credit, TitleData
from models.framed_asset import FramedAsset
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


@pytest.mark.asyncio
async def test_reject_site_id_keeps_fetched_days_and_only_removes_the_failed_round_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Execute the store SQL on SQLite; only the async session boundary is adapted.
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        cast(Table, FramedAsset.__table__).create(connection)
        connection.exec_driver_sql(
            'CREATE TABLE game_answer (game TEXT, round INTEGER, title_id INTEGER, match_status TEXT)'
        )
        connection.exec_driver_sql('CREATE TABLE title (id INTEGER, tmdb_id INTEGER)')
        connection.exec_driver_sql(
            "INSERT INTO game_answer VALUES ('framed', 1, NULL, NULL), ('framed', 2, NULL, NULL), "
            "('framed', 3, 33, 'manual'), ('framed', 4, NULL, NULL)"
        )
        connection.exec_driver_sql('INSERT INTO title VALUES (33, 333)')
        connection.exec_driver_sql(
            'INSERT INTO framed_asset (mode, day, classic_day, tmdb_id, imdb_id) VALUES '
            "('titleshot', 1, 1, 900, 'tt900'), ('poster', 1, 1, 900, 'tt900'), "
            "('titleshot', 2, 2, 901, 'tt901'), ('poster', 2, 3, 900, 'tt900'), "
            "('titleshot', 3, 4, 902, 'tt902')"
        )
        session = AsyncMock(spec=AsyncSession)
        session.__aenter__.return_value = session
        session.execute.side_effect = connection.execute
        monkeypatch.setattr('answers.store.AsyncScopedSession', lambda: session)
        store = DbAnswerStore()
        assert await store.site_updates(4) == [(1, 900), (2, 901), (4, 902)]

        await store.reject_site_id(1, 900)
        await store.reject_site_id(2, 900)  # A different id already stored for the round stays intact.

        assert await store.site_updates(4) == [(2, 901), (4, 902)]
        assert await store.missing_asset_days('titleshot', 3) == []
        assert await store.missing_asset_days('poster', 2) == []
        assert connection.execute(
            select(FramedAsset.mode, FramedAsset.tmdb_id, FramedAsset.imdb_id)
            .where(FramedAsset.classic_day == 1)
            .order_by(FramedAsset.mode)
        ).all() == [('poster', None, 'tt900'), ('titleshot', None, 'tt900')]
        assert connection.exec_driver_sql('SELECT tmdb_id FROM framed_asset WHERE classic_day = 3').scalar() == 900
        assert connection.exec_driver_sql('SELECT title_id, match_status FROM game_answer WHERE round = 3').one() == (
            33,
            'manual',
        )
    engine.dispose()
