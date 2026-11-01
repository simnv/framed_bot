"""Store the answer of each played round and link it to TMDB title data."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Final, Protocol

import httpx
from sqlalchemy.exc import SQLAlchemyError

from . import GAMES, KIND_BY_GAME, Game, Kind, MatchStatus, round_limit, utc_today
from .assets import MODES, Asset, AssetError, Mode, current_mode_day
from .pipeline import Resolution, sibling_years
from .ratelimit import Throttled
from .scraper import BundleEntry, ScrapeError
from .store import PendingAnswer
from .tmdb import TitleData, TmdbError

logger = logging.getLogger(__name__)

# Shutdown ends within SHUTDOWN_TIMEOUT: the active syncs get ACTIVE_TIMEOUT, the CLIP child the rest.
SHUTDOWN_TIMEOUT: Final = 5.0
ACTIVE_TIMEOUT: Final = 3.0
# A 404 within this many mode days is not stored, so the next sync asks again.
RECENT_DAYS: Final = 14

type Scrape = Callable[[Game], Awaitable[list[BundleEntry]]]
type Alert = Callable[[str], Awaitable[None]]


class AnswerStore(Protocol):
    async def missing_rounds(self, game: Game, up_to_round: int) -> list[int]: ...

    async def insert_answers(self, game: Game, answers: dict[int, str]) -> None: ...

    async def pending(self, game: Game, up_to_round: int) -> list[PendingAnswer]: ...

    async def save_title(self, game: Game, round_number: int, data: TitleData, status: MatchStatus) -> None: ...

    async def mark_unmatched(self, game: Game, round_number: int) -> None: ...

    async def source_titles(self, game: Game) -> list[str]: ...

    async def missing_asset_days(self, mode: Mode, up_to_day: int) -> list[int]: ...

    async def save_asset(self, asset: Asset) -> None: ...

    async def site_updates(self, up_to_round: int) -> list[tuple[int, int]]: ...


class TitleSource(Protocol):
    async def details(self, kind: Kind, tmdb_id: int) -> TitleData: ...


class RoundResolver(Protocol):
    async def resolve(
        self,
        game: Game,
        round_number: int,
        source_title: str,
        siblings: dict[str, frozenset[int]],
        image_id: int | None,
    ) -> Resolution | None: ...

    async def close(self) -> None: ...


class AssetSource(Protocol):
    async def fetch(self, mode: Mode, day: int) -> Asset: ...


class AnswerSync:
    def __init__(
        self,
        store: AnswerStore,
        scrape: Scrape,
        tmdb: TitleSource | None,
        alert: Alert,
        today: Callable[[], date] = utc_today,
        resolver: RoundResolver | None = None,
        assets: AssetSource | None = None,
    ) -> None:
        self._store = store
        self._scrape = scrape
        self._tmdb = tmdb
        self._alert = alert
        self._today = today
        self._resolver = resolver
        self._assets = assets
        self._locks: dict[Game, asyncio.Lock] = {game: asyncio.Lock() for game in GAMES}
        # Games whose scrape failure the admin already knows about.
        self._alerted: set[Game] = set()
        # The last scraped bundle of each game; its image ids feed the visual tie-break.
        self._bundles: dict[Game, list[BundleEntry]] = {}
        self._active: set[asyncio.Task[None]] = set()
        self._closed = False

    def limit(self, game: Game, posted_round: int) -> int:
        """A posted round is untrusted: clamp it to the round that the date allows."""
        return min(posted_round, round_limit(game, self._today()))

    async def sync(self, game: Game, up_to_round: int) -> None:
        """Store answers of rounds 1..limit, then look up titles. Never stores rounds after the date limit."""
        limit = self.limit(game, up_to_round)
        if self._closed or limit < 1:
            return
        await self._tracked(self._run(game, limit))

    async def sync_newest(self, game: Game, newest_round: Callable[[], Awaitable[int | None]]) -> None:
        """Sync up to the newest round that newest_round() returns; the lookup is cancelled by shutdown() too."""
        if self._closed:
            return
        await self._tracked(self._run_newest(game, newest_round))

    async def _tracked(self, coroutine: Awaitable[None]) -> None:
        work = asyncio.ensure_future(coroutine)
        self._active.add(work)
        try:
            await work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            # shutdown() cancelled the work; a cancel of the caller itself still propagates.
            if not self._closed or (current is not None and current.cancelling()):
                raise
        finally:
            self._active.discard(work)

    async def shutdown(self) -> None:
        """Stop new syncs, cancel the active ones, wait for them, then stop the CLIP child (bounded)."""
        self._closed = True
        active = list(self._active)
        for work in active:
            work.cancel()
        if active:
            _, still_running = await asyncio.wait(active, timeout=ACTIVE_TIMEOUT)
            if still_running:
                logger.warning('%d answer syncs did not stop in time', len(still_running))
        if self._resolver is not None:
            await self._close_resolver(self._resolver)

    async def _close_resolver(self, resolver: RoundResolver) -> None:
        closing = asyncio.ensure_future(resolver.close())
        # asyncio.wait never waits for the cancellation of a close() that ignores it.
        await asyncio.wait([closing], timeout=SHUTDOWN_TIMEOUT - ACTIVE_TIMEOUT)
        if not closing.done():
            closing.cancel()
            logger.warning('The CLIP worker did not stop in time')
        elif not closing.cancelled() and (error := closing.exception()) is not None:
            logger.warning('The CLIP worker failed to stop: %s', type(error).__name__)

    async def _run_newest(self, game: Game, newest_round: Callable[[], Awaitable[int | None]]) -> None:
        newest = await newest_round()
        # The stored maximum comes from player messages, so it is untrusted too.
        limit = self.limit(game, newest) if newest else 0
        if limit >= 1:
            await self._run(game, limit)

    async def _run(self, game: Game, limit: int) -> None:
        async with self._locks[game]:
            try:
                await self._steps(game, limit)
            except Throttled:
                # HTTP 429 from a game site, TMDB or an image host: rounds stay pending for the next sync.
                logger.warning('Requests are throttled; the next %s sync continues', game)

    async def _steps(self, game: Game, limit: int) -> None:
        missing = await self._store.missing_rounds(game, limit)
        if missing:
            await self._store_answers(game, missing)
        if game == 'framed' and self._assets is not None:
            await self._sync_assets(self._assets)
        if self._tmdb is None:
            return
        if game == 'framed':
            await self._apply_site_ids(limit, self._tmdb)
        if self._resolver is not None:
            await self._match_titles(game, limit, self._tmdb, self._resolver)

    async def _bundle(self, game: Game) -> list[BundleEntry] | None:
        try:
            entries = await self._scrape(game)
        except Throttled:
            raise
        except (ScrapeError, httpx.HTTPError) as error:
            # ScrapeError text is ours; transport exception text is not logged.
            logger.warning(
                'Cannot scrape %s answers: %s', game, error if isinstance(error, ScrapeError) else type(error).__name__
            )
            if game not in self._alerted:
                self._alerted.add(game)
                await self._alert(f'Не удалось получить ответы {game}: {type(error).__name__}. Подробности в логах.')
            return None
        self._alerted.discard(game)
        self._bundles[game] = entries
        return entries

    async def _store_answers(self, game: Game, missing: list[int]) -> None:
        entries = await self._bundle(game)
        if entries is None:
            return
        answers = {number: entries[number - 1].title for number in missing if number <= len(entries)}
        if len(answers) < len(missing):
            logger.warning('The %s bundle has %d rounds; round %d is not in it', game, len(entries), max(missing))
        await self._store.insert_answers(game, answers)

    async def _sync_assets(self, assets: AssetSource) -> None:
        """Fetch the mode days that the asset table lacks, from day 1 up to today's day. Never a future day."""
        today = self._today()
        for mode in MODES:
            newest = current_mode_day(mode, today)
            try:
                for day in await self._store.missing_asset_days(mode, newest):
                    asset = await assets.fetch(mode, day)
                    # A 404 on a recent day may mean a late publication: keep it missing to fetch it again.
                    if asset.tmdb_id is None and asset.classic_day is None and day > newest - RECENT_DAYS:
                        continue
                    await self._store.save_asset(asset)
            except Throttled:
                raise
            except AssetError as error:
                logger.warning('Cannot read Framed %s assets: %s', mode, error)
            except SQLAlchemyError as error:
                logger.warning('Cannot store Framed %s assets: %s', mode, type(error).__name__)

    async def _save(self, game: Game, round_number: int, tmdb: TitleSource, tmdb_id: int, status: MatchStatus) -> None:
        data = await tmdb.details(KIND_BY_GAME[game], tmdb_id)
        await self._store.save_title(game, round_number, data, status)

    async def _apply_site_ids(self, limit: int, tmdb: TitleSource) -> None:
        """Link Framed rounds to the ids of the asset table. Throttled propagates and ends the run."""
        try:
            updates = await self._store.site_updates(limit)
        except SQLAlchemyError as error:
            logger.warning('Cannot read Framed site ids: %s', type(error).__name__)
            return
        for round_number, tmdb_id in updates:
            try:
                await self._save('framed', round_number, tmdb, tmdb_id, 'site')
            except Throttled:
                raise
            except TmdbError as error:
                logger.warning('TMDB lookup failed for framed round %d: %s', round_number, error)
            except SQLAlchemyError as error:
                logger.warning('Cannot store title for framed round %d: %s', round_number, type(error).__name__)

    async def _match_titles(self, game: Game, limit: int, tmdb: TitleSource, resolver: RoundResolver) -> None:
        pending = await self._store.pending(game, limit)
        if not pending:
            return
        try:
            siblings = sibling_years(await self._store.source_titles(game))
        except SQLAlchemyError as error:
            logger.warning('Cannot read %s source titles: %s', game, type(error).__name__)
            return
        entries = self._bundles.get(game)
        if entries is None or len(entries) < pending[-1].round:
            entries = await self._bundle(game) or []
        for answer in pending:
            image_id = entries[answer.round - 1].image_id if answer.round <= len(entries) else None
            try:
                found = await resolver.resolve(game, answer.round, answer.source_title, siblings, image_id)
                if found is None:
                    await self._store.mark_unmatched(game, answer.round)
                    continue
                await self._save(game, answer.round, tmdb, found.tmdb_id, found.status)
            except Throttled:
                # HTTP 429 from TMDB or an image host; the round stays pending and the run ends.
                raise
            except TmdbError as error:
                # title_id stays NULL, so the next sync tries this round again.
                logger.warning('TMDB lookup failed for %s round %d: %s', game, answer.round, error)
                continue
            except SQLAlchemyError as error:
                logger.warning('Cannot store title for %s round %d: %s', game, answer.round, type(error).__name__)
                continue
