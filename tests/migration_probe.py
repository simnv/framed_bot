"""Seeded migration 0004 and answer-lock checks on a disposable PostgreSQL. Runs inside the bot image.

tests/test_deployment.py starts it with synthetic settings only. It prints one JSON summary line.
"""

import asyncio
import json
from typing import Any

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from answers import Game
from answers.store import DbAnswerStore
from answers.tmdb import Credit, TitleData
from models.db import engine
from models.migrate import alembic_config

# Production-shaped statuses before 0004 (the 2026-11-01 snapshot had 1880 auto, 31 low_confidence, 1 unmatched),
# plus manual rows and rows that the sync never matched.
SEED = {'auto': 1880, 'low_confidence': 31, 'unmatched': 1, 'manual': 3, None: 5}
NON_MANUAL = sum(count for status, count in SEED.items() if status != 'manual')


async def migrate(target: str, down: bool = False) -> None:
    async with engine.begin() as connection:
        step = command.downgrade if down else command.upgrade
        await connection.run_sync(lambda sync: step(alembic_config(sync), target))


async def value(sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar()


async def rejected(sql: str) -> bool:
    try:
        async with engine.begin() as connection:
            await connection.execute(text(sql))
    except IntegrityError:
        return True
    return False


def title(tmdb_id: int, genres: list[tuple[int, str]]) -> TitleData:
    credits = [Credit(9000 + tmdb_id % 7, f'Person {tmdb_id % 7}', 'cast', 0)]
    return TitleData(tmdb_id, 'movie', f'T{tmdb_id}', f'T{tmdb_id}', 2000, 90, ['US'], None, genres, [], credits, {})


async def seed() -> dict[int, int]:
    """Insert answers and linked titles; return round -> title id of the manual rows."""
    manual: dict[int, int] = {}
    async with engine.begin() as connection:
        number = 0
        for status, count in SEED.items():
            for _ in range(count):
                number += 1
                title_id = None
                if status in ('auto', 'low_confidence', 'manual'):
                    title_id = (
                        await connection.execute(
                            text(
                                'INSERT INTO title (tmdb_id, kind, name, original_name, countries, raw, fetched_at) '
                                "VALUES (:tmdb, 'movie', 'T', 'T', '{}', '{}', now()) RETURNING id"
                            ),
                            {'tmdb': 100_000 + number},
                        )
                    ).scalar_one()
                game = 'framed' if number % 3 else 'episode'
                await connection.execute(
                    text(
                        'INSERT INTO game_answer (game, round, source_title, title_id, match_status) '
                        'VALUES (:game, :round, :source, :title, :status)'
                    ),
                    {'game': game, 'round': number, 'source': f'Film {number}', 'title': title_id, 'status': status},
                )
                if status == 'manual' and title_id is not None:
                    manual[number] = title_id
    return manual


async def manual_beats_a_waiting_sync(store: DbAnswerStore, round_number: int, game: Game) -> str:
    """Hold the answer row like an admin write, start a sync write, then commit the manual title first."""
    manual_id = await value('SELECT id FROM title WHERE tmdb_id = 100001')
    async with engine.connect() as admin:
        transaction = await admin.begin()
        await admin.execute(
            text('SELECT match_status FROM game_answer WHERE game = :game AND round = :round FOR UPDATE'),
            {'game': game, 'round': round_number},
        )
        sync_write = asyncio.create_task(store.save_title(game, round_number, title(555, []), 'auto'))
        for _ in range(100):
            if await value("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError('the sync write did not wait for the row lock')
        await admin.execute(
            text(
                "UPDATE game_answer SET title_id = :title, match_status = 'manual' "
                'WHERE game = :game AND round = :round'
            ),
            {'title': manual_id, 'game': game, 'round': round_number},
        )
        await transaction.commit()
    await sync_write
    return await value(
        'SELECT match_status || :sep || title_id FROM game_answer WHERE game = :game AND round = :round',
        sep='/',
        game=game,
        round=round_number,
    )


async def main() -> None:
    summary: dict[str, Any] = {}
    await migrate('0003')
    manual = await seed()
    titles_before = await value('SELECT count(*) FROM title')
    await migrate('0004')
    summary['reset'] = await value('SELECT count(*) FROM game_answer WHERE title_id IS NULL AND match_status IS NULL')
    summary['manual_kept'] = sum(
        [
            await value(
                'SELECT count(*) FROM game_answer '
                "WHERE round = :round AND match_status = 'manual' AND title_id = :title",
                round=number,
                title=title_id,
            )
            for number, title_id in manual.items()
        ]
    )
    summary['titles_kept'] = await value('SELECT count(*) FROM title') == titles_before
    summary['dangling'] = await value(
        'SELECT count(*) FROM game_answer g LEFT JOIN title t ON t.id = g.title_id '
        'WHERE g.title_id IS NOT NULL AND t.id IS NULL'
    )
    store = DbAnswerStore()
    await store.save_title('framed', 1, title(700, [(18, 'Drama')]), 'site')
    await store.save_title('framed', 2, title(701, [(18, 'Drama')]), 'visual')
    await store.save_title('framed', 1, title(702, []), 'auto')
    summary['site_kept'] = await value("SELECT match_status FROM game_answer WHERE game = 'framed' AND round = 1")
    summary['bogus_status_rejected'] = await rejected("UPDATE game_answer SET match_status = 'bogus' WHERE round = 4")
    summary['bogus_mode_rejected'] = await rejected("INSERT INTO framed_asset (mode, day) VALUES ('bogus', 1)")
    summary['manual_race'] = await manual_beats_a_waiting_sync(store, 4, 'framed')
    # Framed and Episode syncs upsert shared genres and people in parallel; sorted ids must prevent deadlocks.
    genres = [(n, f'Genre {n}') for n in range(1, 30)]
    writes = []
    for number in range(5, 45):
        game = 'framed' if number % 3 else 'episode'
        order = genres if number % 2 else list(reversed(genres))
        writes.append(store.save_title(game, number, title(800 + number % 4, order), 'auto'))
    await asyncio.gather(*writes)
    summary['parallel_upserts'] = 'ok'
    await migrate('0003', down=True)
    summary['after_downgrade_site_visual'] = await value(
        "SELECT count(*) FROM game_answer WHERE game = 'framed' AND round IN (1, 2) AND match_status IS NULL"
    )
    summary['after_downgrade_manual'] = await value("SELECT count(*) FROM game_answer WHERE match_status = 'manual'")
    summary['asset_table_dropped'] = await value("SELECT to_regclass('framed_asset') IS NULL")
    await migrate('0006')
    # 0007 replaces HTML entities in stored names. '&amp;lt;' must become '&lt;', not '<'.
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO genre (id, name) VALUES (99001, 'Sci-Fi &amp; Fantasy'), (99002, 'A &amp;lt; B'), "
                "(99003, 'Drama')"
            )
        )
        await connection.execute(text("INSERT INTO person (id, name) VALUES (99001, 'O&#39;Neil')"))
    await migrate('head')
    summary['head'] = await value('SELECT version_num FROM alembic_version')
    summary['unescaped_names'] = await value(
        "SELECT string_agg(name, '|' ORDER BY id) FROM (SELECT id, name FROM genre WHERE id > 99000 "
        'UNION ALL SELECT id + 10, name FROM person WHERE id = 99001) names'
    )
    await engine.dispose()
    print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    asyncio.run(main())
