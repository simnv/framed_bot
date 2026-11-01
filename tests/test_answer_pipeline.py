from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, cast

import numpy as np
import pytest

from answers import Game, Kind, round_date
from answers.embedder import EmbedderError
from answers.images import ImageError
from answers.matching import Candidate
from answers.pipeline import Resolution, Resolver, sibling_years
from answers.ratelimit import Throttled
from answers.tmdb import TmdbRateLimited

# Image bytes name a direction; the fake embedder turns them into unit vectors.
DIRECTIONS = {b'a': (1.0, 0.0), b'b': (0.0, 1.0), b'ab': (1.0, 1.0)}


class FakeEmbedder:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[list[bytes]] = []
        self.error = error
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def embed(self, images: list[bytes]) -> np.ndarray:
        self.calls.append(images)
        if self.error is not None:
            raise self.error
        rows = [np.array(DIRECTIONS[image]) / np.linalg.norm(DIRECTIONS[image]) for image in images]
        return np.stack(rows) if rows else np.zeros((0, 2))


class FakeVisual:
    def __init__(self, frames: list[bytes], images: dict[int, list[bytes]], error: BaseException | None = None) -> None:
        self._frames, self._images, self._error = frames, images, error
        self.frame_requests: list[tuple[Game, int]] = []
        self.image_requests: list[int] = []

    async def frames(self, game: Game, image_id: int) -> list[bytes]:
        self.frame_requests.append((game, image_id))
        return self._frames

    async def candidate_images(self, kind: Kind, tmdb_id: int) -> list[bytes]:
        if self._error is not None:
            raise self._error
        self.image_requests.append(tmdb_id)
        return self._images.get(tmdb_id, [])


def film(tmdb_id: int, name: str, release: str = '2000-01-01', votes: int = 1000) -> Candidate:
    return Candidate(tmdb_id, frozenset({name}), release, votes)


def searching(*candidates: Candidate):
    async def search(kind: Kind, query: str, year: int | None) -> list[Candidate]:
        return list(candidates)

    return search


def on(day: date) -> Callable[[], date]:
    return lambda: day


# Framed round 1000 is dated 2024-12-06: the day after is "recent", and the clock of most tests stands there.
ROUND_DAY = round_date('framed', 1000)
NEAR = on(ROUND_DAY + timedelta(days=1))
OLD = on(ROUND_DAY + timedelta(days=31))
EDGE = on(ROUND_DAY + timedelta(days=30))

REMAKES = (film(1, 'Dune', '1984-12-14', 3000), film(2, 'Dune', '2021-09-15', 9000))


@pytest.mark.asyncio
async def test_a_clear_text_match_is_auto_without_images() -> None:
    visual = FakeVisual([b'a'], {})
    resolver = Resolver(searching(film(5, 'Alien')), visual, FakeEmbedder())

    assert await resolver.resolve('framed', 100, 'Alien', {}, 100) == Resolution(5, 'auto')
    assert visual.frame_requests == []


@pytest.mark.asyncio
async def test_a_tie_is_broken_by_the_frames() -> None:
    # Text prefers the 2021 film (more votes); the frames look like the 1984 film.
    visual = FakeVisual([b'a', b'a'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder())

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(1, 'visual')
    assert visual.frame_requests == [('framed', 1000)]
    assert sorted(visual.image_requests) == [1, 2]


@pytest.mark.asyncio
async def test_a_small_gap_keeps_the_text_choice_with_low_confidence() -> None:
    visual = FakeVisual([b'ab'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=NEAR)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(2, 'low_confidence')


@pytest.mark.parametrize('image_id', [None, 12])
@pytest.mark.asyncio
async def test_a_tie_without_frames_is_low_confidence(image_id: int | None) -> None:
    visual = FakeVisual([b'a'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=NEAR)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, image_id) == Resolution(2, 'low_confidence')
    assert visual.frame_requests == []


@pytest.mark.asyncio
async def test_no_frames_means_no_candidate_downloads() -> None:
    visual = FakeVisual([], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=on(round_date('episode', 10)))

    assert await resolver.resolve('episode', 10, 'Dune', {}, 10) == Resolution(2, 'low_confidence')
    assert visual.image_requests == []


@pytest.mark.asyncio
async def test_a_fuzzy_match_compares_the_top_of_the_pool() -> None:
    candidates = [film(10, 'Moonlight', votes=50)] + [film(20 + n, f'Other {n}') for n in range(10)]
    visual = FakeVisual([b'a'], {10: [b'b'], 21: [b'a']})
    embedder = FakeEmbedder()
    resolver = Resolver(searching(*candidates), visual, embedder)

    assert await resolver.resolve('framed', 1000, 'Moonlightt', {}, 1000) == Resolution(21, 'visual')
    assert visual.image_requests == [10, 20, 21, 22, 23, 24, 25, 26]


@pytest.mark.asyncio
async def test_no_candidate_is_unmatched() -> None:
    assert await Resolver(searching()).resolve('framed', 1, 'Nothing', {}, 1) is None


@pytest.mark.asyncio
async def test_without_an_embedder_ties_are_low_confidence() -> None:
    assert await Resolver(searching(*REMAKES), today=NEAR).resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(
        2, 'low_confidence'
    )


@pytest.mark.asyncio
async def test_image_errors_keep_the_text_choice(caplog: pytest.LogCaptureFixture) -> None:
    visual = FakeVisual([b'a'], {}, error=ImageError('secret detail'))
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=NEAR)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(2, 'low_confidence')
    assert 'ImageError' in caplog.text
    assert 'secret detail' not in caplog.text


@pytest.mark.asyncio
async def test_a_tmdb_rate_limit_stops_the_tie_break() -> None:
    visual = FakeVisual([b'a'], {}, error=TmdbRateLimited('429'))
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder())

    with pytest.raises(TmdbRateLimited):
        await resolver.resolve('framed', 1000, 'Dune', {}, 1000)


@pytest.mark.asyncio
async def test_sibling_years_reach_the_filter() -> None:
    halloween = (film(1, 'Halloween', '1978-10-24', 4000), film(2, 'Halloween', '2018-10-18', 9000))
    resolver = Resolver(searching(*halloween))
    siblings = sibling_years(['Halloween (2018)', 'Alien', 'Halloween (2007)'])

    assert siblings == {'Halloween': frozenset({2007, 2018})}
    assert await resolver.resolve('framed', 1000, 'Halloween', siblings, 1000) == Resolution(1, 'auto')


@pytest.mark.asyncio
async def test_embedder_errors_keep_the_text_choice_and_later_rounds_still_resolve() -> None:
    visual = FakeVisual([b'a'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(EmbedderError('Fail')), today=NEAR)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(2, 'low_confidence')
    assert visual.frame_requests == [('framed', 1000)]
    assert await resolver.resolve('framed', 1001, 'Dune', {}, 1001) == Resolution(2, 'low_confidence')
    assert visual.frame_requests == [('framed', 1000)]
    assert visual.image_requests == []


@pytest.mark.parametrize('error', [Throttled('429'), TmdbRateLimited('429')], ids=['image-host', 'tmdb'])
@pytest.mark.asyncio
async def test_throttling_in_the_tie_break_propagates(error: Exception) -> None:
    resolver = Resolver(searching(*REMAKES), FakeVisual([b'a'], {}, error=error), FakeEmbedder())

    with pytest.raises(Throttled):
        await resolver.resolve('framed', 1000, 'Dune', {}, 1000)


@pytest.mark.asyncio
async def test_close_closes_the_embedder() -> None:
    embedder = FakeEmbedder()

    await Resolver(searching(), FakeVisual([], {}), embedder).close()

    assert embedder.closed


@pytest.mark.parametrize('image_id', [True, 10**30, 0, -5, 1.0, '1000'])
@pytest.mark.asyncio
async def test_an_invalid_image_id_means_no_frames(image_id: object) -> None:
    visual = FakeVisual([b'a'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=NEAR)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, cast(Any, image_id)) == Resolution(2, 'low_confidence')
    assert visual.frame_requests == []


@pytest.mark.parametrize('round_number', [True, 0, 10**30, 1.0, '5'])
@pytest.mark.asyncio
async def test_an_invalid_round_number_is_rejected(round_number: object) -> None:
    with pytest.raises(ValueError):
        await Resolver(searching(film(5, 'Alien'))).resolve('framed', cast(Any, round_number), 'Alien', {}, None)


@pytest.mark.asyncio
async def test_cancellation_propagates_from_the_tie_break() -> None:
    import asyncio

    visual = FakeVisual([b'a'], {}, error=asyncio.CancelledError())
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder())

    with pytest.raises(asyncio.CancelledError):
        await resolver.resolve('framed', 1000, 'Dune', {}, 1000)


def test_sibling_years_do_not_depend_on_order() -> None:
    assert sibling_years(['A (2001)', 'A (2002)']) == sibling_years(['A (2002)', 'A (2001)'])


@pytest.mark.asyncio
async def test_a_fuzzy_choice_after_the_eighth_entry_keeps_the_group_at_eight() -> None:
    candidates = [film(20 + n, f'Other {n}') for n in range(8)] + [film(10, 'Moonlight', votes=50)]
    visual = FakeVisual([b'a'], {10: [b'b'], 20: [b'a']})
    resolver = Resolver(searching(*candidates), visual, FakeEmbedder())

    await resolver.resolve('framed', 1000, 'Moonlightt', {}, 1000)

    assert visual.image_requests == [10, 20, 21, 22, 23, 24, 25, 26]


@pytest.mark.asyncio
async def test_an_old_tie_with_an_undecided_visual_step_goes_to_the_most_voted() -> None:
    visual = FakeVisual([b'ab'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=OLD)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(2, 'auto')


@pytest.mark.parametrize('clock', [EDGE, NEAR], ids=['exactly 30 days', 'recent'])
@pytest.mark.asyncio
async def test_a_tie_not_older_than_30_days_stays_low_confidence(clock: Callable[[], date]) -> None:
    visual = FakeVisual([b'ab'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=clock)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(2, 'low_confidence')


@pytest.mark.asyncio
async def test_an_old_fuzzy_choice_stays_low_confidence() -> None:
    candidates = [film(10, 'Moonlight', votes=50)] + [film(20 + n, f'Other {n}') for n in range(3)]
    visual = FakeVisual([b'ab'], {})
    resolver = Resolver(searching(*candidates), visual, FakeEmbedder(), today=OLD)

    assert await resolver.resolve('framed', 1000, 'Moonlightt', {}, 1000) == Resolution(10, 'low_confidence')


@pytest.mark.asyncio
async def test_a_decisive_visual_result_wins_for_an_old_tie() -> None:
    visual = FakeVisual([b'a', b'a'], {1: [b'a'], 2: [b'b']})
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=OLD)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(1, 'visual')


@pytest.mark.asyncio
async def test_an_old_tie_without_the_visual_step_is_auto() -> None:
    resolver = Resolver(searching(*REMAKES), today=OLD)

    assert await resolver.resolve('framed', 1000, 'Dune', {}, 1000) == Resolution(2, 'auto')


@pytest.mark.asyncio
async def test_throttling_still_propagates_for_an_old_tie() -> None:
    visual = FakeVisual([b'a'], {}, error=TmdbRateLimited('429'))
    resolver = Resolver(searching(*REMAKES), visual, FakeEmbedder(), today=OLD)

    with pytest.raises(TmdbRateLimited):
        await resolver.resolve('framed', 1000, 'Dune', {}, 1000)


@pytest.mark.asyncio
async def test_an_old_tier_3_tie_goes_to_the_most_voted_even_when_its_match_is_shorter() -> None:
    candidates = (film(1, 'Alpha', votes=1000), film(2, 'Beta', votes=9000))
    resolver = Resolver(searching(*candidates), today=OLD)

    assert await resolver.resolve('framed', 1000, 'Alpha: Beta', {}, 1000) == Resolution(2, 'auto')


@pytest.mark.asyncio
async def test_an_old_tie_with_equal_votes_keeps_the_text_order() -> None:
    candidates = (film(1, 'Dune', '1984-12-14', 5000), film(2, 'Dune', '2021-09-15', 5000))
    resolver = Resolver(searching(*candidates), today=OLD)

    old = await resolver.resolve('framed', 1000, 'Dune', {}, 1000)
    recent = await Resolver(searching(*candidates), today=NEAR).resolve('framed', 1000, 'Dune', {}, 1000)

    assert old is not None and recent is not None
    assert (old.tmdb_id, old.status) == (recent.tmdb_id, 'auto')
