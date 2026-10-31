"""Store the answer of each played round and link it to TMDB title data."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Final, Protocol

import httpx

from . import GAMES, KIND_BY_GAME, Game, Kind, MatchStatus, round_limit, utc_today
from .scraper import ScrapeError
from .store import PendingAnswer
from .tmdb import Match, TitleData, TmdbError, TmdbRateLimited

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT: Final = 5.0

type Scrape = Callable[[Game], Awaitable[list[str]]]
type Alert = Callable[[str], Awaitable[None]]


class AnswerStore(Protocol):
    async def missing_rounds(self, game: Game, up_to_round: int) -> list[int]: ...

    async def insert_answers(self, game: Game, answers: dict[int, str]) -> None: ...

    async def pending(self, game: Game, up_to_round: int) -> list[PendingAnswer]: ...

    async def save_title(self, game: Game, round_number: int, data: TitleData, status: MatchStatus) -> None: ...

    async def mark_unmatched(self, game: Game, round_number: int) -> None: ...


class TitleSource(Protocol):
    async def find(self, kind: Kind, source_title: str) -> Match | None: ...

    async def details(self, kind: Kind, tmdb_id: int) -> TitleData: ...


class AnswerSync:
    def __init__(
        self,
        store: AnswerStore,
        scrape: Scrape,
        tmdb: TitleSource | None,
        alert: Alert,
        today: Callable[[], date] = utc_today,
    ) -> None:
        self._store = store
        self._scrape = scrape
        self._tmdb = tmdb
        self._alert = alert
        self._today = today
        self._locks: dict[Game, asyncio.Lock] = {game: asyncio.Lock() for game in GAMES}
        # Games whose scrape failure the admin already knows about.
        self._alerted: set[Game] = set()
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
        """Stop new syncs, cancel the active ones and wait for them (bounded)."""
        self._closed = True
        active = list(self._active)
        for work in active:
            work.cancel()
        if active:
            _, still_running = await asyncio.wait(active, timeout=SHUTDOWN_TIMEOUT)
            if still_running:
                logger.warning('%d answer syncs did not stop in time', len(still_running))

    async def _run_newest(self, game: Game, newest_round: Callable[[], Awaitable[int | None]]) -> None:
        newest = await newest_round()
        # The stored maximum comes from player messages, so it is untrusted too.
        limit = self.limit(game, newest) if newest else 0
        if limit >= 1:
            await self._run(game, limit)

    async def _run(self, game: Game, limit: int) -> None:
        async with self._locks[game]:
            missing = await self._store.missing_rounds(game, limit)
            if missing:
                await self._store_answers(game, missing)
            if self._tmdb is not None:
                await self._match_titles(game, limit, self._tmdb)

    async def _store_answers(self, game: Game, missing: list[int]) -> None:
        try:
            titles = await self._scrape(game)
        except (ScrapeError, httpx.HTTPError) as error:
            logger.warning('Cannot scrape %s answers: %s', game, error)
            if game not in self._alerted:
                self._alerted.add(game)
                await self._alert(f'Не удалось получить ответы {game}: {type(error).__name__}. Подробности в логах.')
            return
        self._alerted.discard(game)
        answers = {number: titles[number - 1] for number in missing if number <= len(titles)}
        if len(answers) < len(missing):
            logger.warning('The %s bundle has %d rounds; round %d is not in it', game, len(titles), max(missing))
        await self._store.insert_answers(game, answers)

    async def _match_titles(self, game: Game, limit: int, tmdb: TitleSource) -> None:
        kind = KIND_BY_GAME[game]
        for answer in await self._store.pending(game, limit):
            try:
                match = await tmdb.find(kind, answer.source_title)
                if match is None:
                    await self._store.mark_unmatched(game, answer.round)
                    continue
                data = await tmdb.details(kind, match.tmdb_id)
            except TmdbRateLimited:
                logger.warning('TMDB rate limit reached; the next %s sync continues', game)
                return
            except TmdbError as error:
                # title_id stays NULL, so the next sync tries this round again.
                logger.warning('TMDB lookup failed for %s round %d: %s', game, answer.round, error)
                continue
            await self._store.save_title(game, answer.round, data, match.status)
