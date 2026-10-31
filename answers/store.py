"""PostgreSQL storage of game answers and TMDB titles."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import AsyncScopedSession
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.game_answer import GameAnswer
from models.title import Company, Genre, Person, Title, TitleCompany, TitleGenre, TitlePerson

from . import Game, MatchStatus
from .tmdb import TitleData


@dataclass(frozen=True, slots=True)
class PendingAnswer:
    round: int
    source_title: str


async def _upsert_names(session: AsyncSession, model: type[Genre | Person | Company], rows: dict[int, str]) -> None:
    if not rows:
        return
    # Sorted ids give every transaction the same lock order on these shared rows.
    statement = insert(model).values([{'id': key, 'name': rows[key]} for key in sorted(rows)])
    await session.execute(
        statement.on_conflict_do_update(index_elements=[model.id], set_={'name': statement.excluded.name})
    )


async def _upsert_title(session: AsyncSession, data: TitleData) -> int:
    values: dict[str, Any] = {
        'tmdb_id': data.tmdb_id,
        'kind': data.kind,
        'name': data.name,
        'original_name': data.original_name,
        'year': data.year,
        'runtime': data.runtime,
        'countries': data.countries,
        'imdb_id': data.imdb_id,
        'raw': data.raw,
    }
    statement = insert(Title).values(**values, fetched_at=func.now())
    statement = statement.on_conflict_do_update(
        index_elements=[Title.kind, Title.tmdb_id],
        set_={**{key: statement.excluded[key] for key in values}, 'fetched_at': func.now()},
    ).returning(Title.id)
    title_id = (await session.execute(statement)).scalar_one()

    await _upsert_names(session, Genre, dict(data.genres))
    await _upsert_names(session, Company, dict(data.companies))
    await _upsert_names(session, Person, {credit.person_id: credit.name for credit in data.credits})
    # A new fetch replaces the links of the title.
    for link in (TitleGenre, TitleCompany, TitlePerson):
        await session.execute(delete(link).where(link.title_id == title_id))
    if data.genres:
        await session.execute(insert(TitleGenre), [{'title_id': title_id, 'genre_id': key} for key, _ in data.genres])
    if data.companies:
        await session.execute(
            insert(TitleCompany), [{'title_id': title_id, 'company_id': key} for key, _ in data.companies]
        )
    if data.credits:
        await session.execute(
            insert(TitlePerson),
            [
                {'title_id': title_id, 'person_id': credit.person_id, 'role': credit.role, 'order': credit.order}
                for credit in data.credits
            ],
        )
    return title_id


async def _lock_answer(session: AsyncSession, game: Game, round_number: int) -> str | None:
    """Lock one answer row. Return its match status ('' when NULL), or None when the row does not exist."""
    result = await session.execute(
        select(GameAnswer.match_status)
        .where(GameAnswer.game == game, GameAnswer.round == round_number)
        .with_for_update()
    )
    row = result.one_or_none()
    return None if row is None else row.match_status or ''


class DbAnswerStore:
    async def missing_rounds(self, game: Game, up_to_round: int) -> list[int]:
        async with AsyncScopedSession() as session:
            result = await session.execute(
                select(GameAnswer.round).where(GameAnswer.game == game, GameAnswer.round <= up_to_round)
            )
            stored = set(result.scalars())
        return [number for number in range(1, up_to_round + 1) if number not in stored]

    async def insert_answers(self, game: Game, answers: dict[int, str]) -> None:
        if not answers:
            return
        async with AsyncScopedSession() as session:
            await session.execute(
                insert(GameAnswer)
                .values([{'game': game, 'round': number, 'source_title': title} for number, title in answers.items()])
                .on_conflict_do_nothing(index_elements=[GameAnswer.game, GameAnswer.round])
            )
            await session.commit()

    async def pending(self, game: Game, up_to_round: int) -> list[PendingAnswer]:
        async with AsyncScopedSession() as session:
            result = await session.execute(
                select(GameAnswer.round, GameAnswer.source_title)
                .where(
                    GameAnswer.game == game,
                    GameAnswer.round <= up_to_round,
                    GameAnswer.title_id.is_(None),
                    or_(GameAnswer.match_status.is_(None), GameAnswer.match_status.not_in(('unmatched', 'manual'))),
                )
                .order_by(GameAnswer.round)
            )
            return [PendingAnswer(number, title) for number, title in result.all()]

    async def save_title(self, game: Game, round_number: int, data: TitleData, status: MatchStatus) -> None:
        """Link a round to a title found by the sync. A manual row stays unchanged."""
        async with AsyncScopedSession() as session, session.begin():
            # Lock the answer row before the title, in the same order as set_manual_title.
            current = await _lock_answer(session, game, round_number)
            if current is None or current == 'manual':
                return
            title_id = await _upsert_title(session, data)
            await session.execute(
                update(GameAnswer)
                .where(GameAnswer.game == game, GameAnswer.round == round_number)
                .values(title_id=title_id, match_status=status, updated_at=func.now())
            )

    async def mark_unmatched(self, game: Game, round_number: int) -> None:
        async with AsyncScopedSession() as session, session.begin():
            await session.execute(
                update(GameAnswer)
                .where(
                    GameAnswer.game == game,
                    GameAnswer.round == round_number,
                    GameAnswer.match_status.is_distinct_from('manual'),
                )
                .values(match_status='unmatched', updated_at=func.now())
            )

    async def set_manual_title(self, game: Game, round_number: int, data: TitleData) -> bool:
        """Link a round to a title chosen by the admin. Return False when the round has no answer row."""
        async with AsyncScopedSession() as session, session.begin():
            if await _lock_answer(session, game, round_number) is None:
                return False
            title_id = await _upsert_title(session, data)
            await session.execute(
                update(GameAnswer)
                .where(GameAnswer.game == game, GameAnswer.round == round_number)
                .values(title_id=title_id, match_status='manual', updated_at=func.now())
            )
            return True

    async def has_answer(self, game: Game, round_number: int) -> bool:
        async with AsyncScopedSession() as session:
            found = await session.scalar(
                select(GameAnswer.round).where(GameAnswer.game == game, GameAnswer.round == round_number)
            )
            return found is not None

    async def max_result_round(self, game: Game) -> int | None:
        model = FramedResult if game == 'framed' else EpisodeResult
        async with AsyncScopedSession() as session:
            return await session.scalar(select(func.max(model.framed_round)))
