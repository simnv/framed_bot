"""Resolve one round to a TMDB title: text candidates first, then a visual tie-break when the text is not sure."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import numpy as np

from . import KIND_BY_GAME, Game, Kind, round_date
from .embedder import Embedder, EmbedderError
from .images import ImageError, ImageFetcher, has_frames
from .matching import Candidate, Search, TextChoice, text_match
from .ratelimit import Throttled
from .tmdb import TmdbClient, TmdbError, split_year
from .visual import pick

logger = logging.getLogger(__name__)

LOW_CONFIDENCE_POOL: Final = 8
MAX_ROUND: Final = 1_000_000
MAX_IMAGE_ID: Final = 10_000_000  # as the scraper bound

type ResolvedStatus = Literal['auto', 'visual', 'low_confidence']


@dataclass(frozen=True, slots=True)
class Resolution:
    tmdb_id: int
    status: ResolvedStatus


class VisualSource(Protocol):
    async def frames(self, game: Game, image_id: int) -> list[bytes]: ...

    async def candidate_images(self, kind: Kind, tmdb_id: int) -> list[bytes]: ...


class TmdbVisualSource:
    """Frames from the game site and TMDB backdrops (and TV stills) of a candidate."""

    def __init__(self, fetcher: ImageFetcher, tmdb: TmdbClient) -> None:
        self._fetcher = fetcher
        self._tmdb = tmdb

    async def frames(self, game: Game, image_id: int) -> list[bytes]:
        return await self._fetcher.frames(game, image_id)

    async def candidate_images(self, kind: Kind, tmdb_id: int) -> list[bytes]:
        return await self._fetcher.tmdb_images(await self._tmdb.image_paths(kind, tmdb_id))


def sibling_years(source_titles: Iterable[str]) -> dict[str, frozenset[int]]:
    """Map a title to the years of its year-tagged rounds: 'Halloween (2018)' gives {'Halloween': {2018}}."""
    years: dict[str, set[int]] = {}
    for source_title in source_titles:
        name, year = split_year(source_title)
        if year is not None:
            years.setdefault(name, set()).add(year)
    return {name: frozenset(found) for name, found in years.items()}


def visual_group(text: TextChoice) -> list[Candidate]:
    """The tie-break group: the tie, or for a low-confidence choice the choice and 7 other pool entries."""
    if text.ties:
        return text.ties
    if text.confident:
        return []
    choice = text.choice.candidate
    others = [candidate for candidate in text.pool if candidate.tmdb_id != choice.tmdb_id]
    return [choice, *others[: LOW_CONFIDENCE_POOL - 1]]


class Resolver:
    def __init__(self, search: Search, visual: VisualSource | None = None, embedder: Embedder | None = None) -> None:
        self._search = search
        self._visual = visual
        self._embedder = embedder
        self._visual_disabled = False  # set after an EmbedderError; the backend stays off until restart

    async def close(self) -> None:
        """Stop the CLIP child process, if there is one."""
        if self._embedder is not None:
            await self._embedder.close()

    async def resolve(
        self,
        game: Game,
        round_number: int,
        source_title: str,
        siblings: dict[str, frozenset[int]],
        image_id: int | None,
    ) -> Resolution | None:
        """None means no candidate (unmatched). TmdbError from the text search and Throttled propagate."""
        if type(round_number) is not int or not 1 <= round_number <= MAX_ROUND:
            raise ValueError('Invalid round number')
        kind = KIND_BY_GAME[game]
        query, year = split_year(source_title)
        text = await text_match(
            self._search, kind, query, year, round_date(game, round_number), siblings.get(query, frozenset())
        )
        if text is None:
            return None
        chosen = text.choice.candidate.tmdb_id
        group = visual_group(text)
        if not group:
            return Resolution(chosen, 'auto')
        if type(image_id) is int and 1 <= image_id < MAX_IMAGE_ID and has_frames(game, image_id):
            winner = await self._tie_break(game, kind, image_id, group)
            if winner is not None:
                return Resolution(winner, 'visual')
        return Resolution(chosen, 'low_confidence')

    async def _tie_break(self, game: Game, kind: Kind, image_id: int, group: list[Candidate]) -> int | None:
        if self._visual is None or self._embedder is None or self._visual_disabled:
            return None
        try:
            frames = await self._embedder.embed(await self._visual.frames(game, image_id))
            if len(frames) == 0:
                return None
            images: dict[int, np.ndarray] = {}
            for candidate in group:
                images[candidate.tmdb_id] = await self._embedder.embed(
                    await self._visual.candidate_images(kind, candidate.tmdb_id)
                )
        except Throttled:
            # HTTP 429 from TMDB or an image host: the sync run stops and tries this round again later.
            raise
        except EmbedderError as error:
            self._visual_disabled = True
            logger.warning('Visual step disabled for %s image %d: %s', game, image_id, type(error).__name__)
            return None
        except (ImageError, TmdbError) as error:
            logger.warning('Visual tie-break failed for %s image %d: %s', game, image_id, type(error).__name__)
            return None
        winner = pick(game, frames, images)
        return None if winner is None else winner.tmdb_id
