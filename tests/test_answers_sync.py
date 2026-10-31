import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from answers import Game, Kind, MatchStatus
from answers.scraper import ScrapeError
from answers.store import PendingAnswer
from answers.sync import AnswerSync
from answers.tmdb import Match, TitleData, TmdbClient, TmdbError, TmdbRateLimited

TITLES = [f'Film {number}' for number in range(1, 201)]
# Framed round 1695 and Episode round 217; both limits are far above the small rounds in these tests.
TODAY = date(2026, 10, 31)


@dataclass
class Row:
    source_title: str
    title_id: int | None = None
    match_status: MatchStatus | None = None


@dataclass
class FakeStore:
    rows: dict[tuple[Game, int], Row] = field(default_factory=dict)
    limits: list[int] = field(default_factory=list)

    async def missing_rounds(self, game: Game, up_to_round: int) -> list[int]:
        self.limits.append(up_to_round)
        # A forged round must never reach the store as a huge range.
        assert up_to_round <= 10_000
        return [number for number in range(1, up_to_round + 1) if (game, number) not in self.rows]

    async def insert_answers(self, game: Game, answers: dict[int, str]) -> None:
        for number, title in answers.items():
            self.rows.setdefault((game, number), Row(title))

    async def pending(self, game: Game, up_to_round: int) -> list[PendingAnswer]:
        return [
            PendingAnswer(number, row.source_title)
            for (row_game, number), row in sorted(self.rows.items())
            if row_game == game
            and number <= up_to_round
            and row.title_id is None
            and row.match_status not in ('unmatched', 'manual')
        ]

    async def save_title(self, game: Game, round_number: int, data: TitleData, status: MatchStatus) -> None:
        row = self.rows[(game, round_number)]
        if row.match_status != 'manual':
            row.title_id, row.match_status = data.tmdb_id, status

    async def mark_unmatched(self, game: Game, round_number: int) -> None:
        row = self.rows[(game, round_number)]
        if row.match_status != 'manual':
            row.match_status = 'unmatched'


@dataclass
class FakeScraper:
    titles: list[str] = field(default_factory=lambda: list(TITLES))
    error: Exception | None = None
    calls: list[Game] = field(default_factory=list)
    gate: asyncio.Event | None = None

    async def __call__(self, game: Game) -> list[str]:
        self.calls.append(game)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.titles


def title_data(tmdb_id: int, kind: Kind = 'movie') -> TitleData:
    return TitleData(tmdb_id, kind, f'Title {tmdb_id}', f'Title {tmdb_id}', 2000, 100, ['US'], None, [], [], [], {})


@dataclass
class FakeTmdb:
    # source_title -> outcome of find: a Match, None, or an exception.
    outcomes: dict[str, Match | None | Exception] = field(default_factory=dict)
    finds: list[tuple[Kind, str]] = field(default_factory=list)

    async def find(self, kind: Kind, source_title: str) -> Match | None:
        self.finds.append((kind, source_title))
        outcome = self.outcomes.get(source_title, Match(int(source_title.split()[-1]), 'auto'))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def details(self, kind: Kind, tmdb_id: int) -> TitleData:
        return title_data(tmdb_id, kind)


@dataclass
class Alerts:
    messages: list[str] = field(default_factory=list)

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def make_sync(
    store: FakeStore | None = None,
    scraper: FakeScraper | None = None,
    tmdb: FakeTmdb | None = None,
    today: date = TODAY,
) -> tuple[AnswerSync, FakeStore, FakeScraper, FakeTmdb, Alerts]:
    store, scraper, tmdb, alerts = store or FakeStore(), scraper or FakeScraper(), tmdb or FakeTmdb(), Alerts()
    return AnswerSync(store, scraper, tmdb, alerts, today=lambda: today), store, scraper, tmdb, alerts


async def wait_until(condition: Any) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError('condition not reached')


@pytest.mark.asyncio
async def test_sync_stores_rounds_up_to_the_played_round_only() -> None:
    sync, store, scraper, _, _ = make_sync()

    await sync.sync('framed', 3)

    assert scraper.calls == ['framed']
    assert store.rows == {
        ('framed', 1): Row('Film 1', 1, 'auto'),
        ('framed', 2): Row('Film 2', 2, 'auto'),
        ('framed', 3): Row('Film 3', 3, 'auto'),
    }


@pytest.mark.asyncio
async def test_sync_does_not_scrape_when_no_round_is_missing() -> None:
    store = FakeStore({('framed', 1): Row('Film 1', 1, 'auto')})
    sync, _, scraper, tmdb, _ = make_sync(store)

    await sync.sync('framed', 1)

    assert scraper.calls == []
    assert tmdb.finds == []


@pytest.mark.asyncio
async def test_sync_uses_tv_lookup_for_episode() -> None:
    sync, store, _, tmdb, _ = make_sync()

    await sync.sync('episode', 1)

    assert tmdb.finds == [('tv', 'Film 1')]
    assert store.rows[('episode', 1)] == Row('Film 1', 1, 'auto')


@pytest.mark.asyncio
async def test_sync_stores_nothing_for_rounds_beyond_the_bundle() -> None:
    sync, store, _, _, _ = make_sync(scraper=FakeScraper(titles=TITLES[:2]))

    await sync.sync('framed', 3)

    assert sorted(store.rows) == [('framed', 1), ('framed', 2)]


@pytest.mark.asyncio
async def test_sync_never_changes_manual_rows() -> None:
    store = FakeStore({('framed', 1): Row('Film 1', 999, 'manual')})
    sync, _, _, tmdb, _ = make_sync(store)

    await sync.sync('framed', 2)

    assert store.rows[('framed', 1)] == Row('Film 1', 999, 'manual')
    assert tmdb.finds == [('movie', 'Film 2')]


@pytest.mark.asyncio
async def test_sync_marks_unmatched_and_does_not_retry_them() -> None:
    sync, store, _, tmdb, _ = make_sync(tmdb=FakeTmdb({'Film 1': None}))

    await sync.sync('framed', 1)
    await sync.sync('framed', 1)

    assert store.rows[('framed', 1)] == Row('Film 1', None, 'unmatched')
    assert tmdb.finds == [('movie', 'Film 1')]


@pytest.mark.asyncio
async def test_sync_keeps_low_confidence_status() -> None:
    sync, store, _, _, _ = make_sync(tmdb=FakeTmdb({'Film 1': Match(77, 'low_confidence')}))

    await sync.sync('framed', 1)

    assert store.rows[('framed', 1)] == Row('Film 1', 77, 'low_confidence')


@pytest.mark.asyncio
async def test_tmdb_error_keeps_title_empty_and_next_sync_retries() -> None:
    tmdb = FakeTmdb({'Film 1': TmdbError('timeout')})
    sync, store, _, _, _ = make_sync(tmdb=tmdb)

    await sync.sync('framed', 2)

    assert store.rows[('framed', 1)] == Row('Film 1')
    assert store.rows[('framed', 2)] == Row('Film 2', 2, 'auto')
    del tmdb.outcomes['Film 1']
    await sync.sync('framed', 2)
    assert store.rows[('framed', 1)] == Row('Film 1', 1, 'auto')


@pytest.mark.parametrize('unmatched', [False, True], ids=['save-title', 'mark-unmatched'])
@pytest.mark.asyncio
async def test_store_error_keeps_round_pending_and_later_rounds_continue(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, unmatched: bool
) -> None:
    sync, store, _, _, _ = make_sync(tmdb=FakeTmdb({'Film 1': None}) if unmatched else None)
    method = 'mark_unmatched' if unmatched else 'save_title'
    original = getattr(store, method)

    async def fail_first(game: Game, round_number: int, *args: Any) -> None:
        if round_number == 1:
            raise SQLAlchemyError('sensitive database parameters')
        await original(game, round_number, *args)

    monkeypatch.setattr(store, method, fail_first)

    await sync.sync('framed', 2)

    assert store.rows[('framed', 1)] == Row('Film 1')
    assert store.rows[('framed', 2)] == Row('Film 2', 2, 'auto')
    assert 'framed round 1: SQLAlchemyError' in caplog.text
    assert 'sensitive database parameters' not in caplog.text
    monkeypatch.setattr(store, method, original)
    await sync.sync('framed', 2)
    assert store.rows[('framed', 1)].match_status == ('unmatched' if unmatched else 'auto')


@pytest.mark.asyncio
async def test_rate_limit_stops_the_run() -> None:
    sync, store, _, tmdb, _ = make_sync(tmdb=FakeTmdb({'Film 2': TmdbRateLimited('429')}))

    await sync.sync('framed', 3)

    assert tmdb.finds == [('movie', 'Film 1'), ('movie', 'Film 2')]
    assert store.rows[('framed', 3)] == Row('Film 3')


@pytest.mark.asyncio
async def test_missing_tmdb_key_stores_answers_only() -> None:
    store, alerts = FakeStore(), Alerts()
    sync = AnswerSync(store, FakeScraper(), None, alerts, today=lambda: TODAY)

    await sync.sync('framed', 2)

    assert store.rows == {('framed', 1): Row('Film 1'), ('framed', 2): Row('Film 2')}


@pytest.mark.asyncio
async def test_lock_prevents_parallel_syncs_of_one_game() -> None:
    gate = asyncio.Event()
    sync, store, scraper, _, _ = make_sync(scraper=FakeScraper(gate=gate))

    first = asyncio.create_task(sync.sync('framed', 2))
    second = asyncio.create_task(sync.sync('framed', 2))
    other_game = asyncio.create_task(sync.sync('episode', 1))
    # The second framed sync waits for the lock; the episode sync runs in parallel.
    await wait_until(lambda: len(scraper.calls) == 2)
    for _ in range(10):
        await asyncio.sleep(0)
    assert scraper.calls == ['framed', 'episode']
    gate.set()
    await asyncio.gather(first, second, other_game)

    assert scraper.calls == ['framed', 'episode']
    assert store.rows[('framed', 2)] == Row('Film 2', 2, 'auto')


@pytest.mark.parametrize('error', [ScrapeError('no array'), httpx.ConnectError('down')], ids=['scrape', 'http'])
@pytest.mark.asyncio
async def test_scrape_failure_alerts_admin_once_until_a_scrape_succeeds(error: Exception) -> None:
    scraper = FakeScraper(error=error)
    sync, store, _, tmdb, alerts = make_sync(scraper=scraper)

    await sync.sync('framed', 1)
    await sync.sync('framed', 1)

    assert store.rows == {}
    assert tmdb.finds == []
    assert len(alerts.messages) == 1
    scraper.error = None
    await sync.sync('framed', 1)
    scraper.error = error
    await sync.sync('framed', 2)
    assert len(alerts.messages) == 2


@pytest.mark.asyncio
async def test_forged_future_round_is_clamped_to_the_date() -> None:
    # Framed round 3 is current on 2022-03-14; one round of slack allows round 4.
    sync, store, _, _, _ = make_sync(today=date(2022, 3, 14))

    await sync.sync('framed', 2_000_000_000)

    assert store.limits == [4]
    assert sorted(store.rows) == [('framed', number) for number in range(1, 5)]


@pytest.mark.asyncio
async def test_huge_persisted_round_does_not_build_a_huge_range() -> None:
    sync, store, _, _, _ = make_sync(today=date(2022, 9, 5) + timedelta(weeks=9))

    await sync.sync('episode', 10**12)

    assert store.limits == [11]


@pytest.mark.asyncio
async def test_sync_before_round_one_does_nothing() -> None:
    sync, store, scraper, _, _ = make_sync(today=date(2022, 3, 10))

    await sync.sync('framed', 5)

    assert store.limits == []
    assert scraper.calls == []


def tmdb_with_bodies(searches: dict[str, Any], details: dict[int, Any]) -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/3/search/movie':
            return httpx.Response(200, json=searches[request.url.params['query']])
        return httpx.Response(200, json=details[int(request.url.path.rsplit('/', 1)[1])])

    return TmdbClient('synthetic-tmdb-key', httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_malformed_tmdb_responses_keep_rows_pending_and_later_rows_continue() -> None:
    valid = {'id': 4, 'title': 'Film 4', 'release_date': '2001-01-01', 'credits': {'cast': [], 'crew': []}}
    tmdb = tmdb_with_bodies(
        {
            'Film 1': {'results': None},
            'Film 2': {'results': [{}]},
            'Film 3': {'results': [{'id': 3, 'title': 'Film 3'}]},
            'Film 4': {'results': [{'id': 4, 'title': 'Film 4'}]},
        },
        {3: {'id': 3, 'title': None}, 4: valid},
    )
    store = FakeStore()
    sync = AnswerSync(store, FakeScraper(), tmdb, Alerts(), today=lambda: TODAY)

    await sync.sync('framed', 4)

    assert [store.rows[('framed', number)] for number in (1, 2, 3)] == [Row('Film 1'), Row('Film 2'), Row('Film 3')]
    assert store.rows[('framed', 4)] == Row('Film 4', 4, 'auto')


@pytest.mark.asyncio
async def test_shutdown_cancels_active_and_queued_syncs() -> None:
    gate = asyncio.Event()
    sync, store, scraper, _, _ = make_sync(scraper=FakeScraper(gate=gate))
    active = asyncio.create_task(sync.sync('framed', 2))
    queued = asyncio.create_task(sync.sync('framed', 2))
    await wait_until(lambda: scraper.calls == ['framed'])

    await asyncio.wait_for(sync.shutdown(), 1)

    # Both callers end without an error; the queued sync never scraped.
    assert await asyncio.gather(active, queued) == [None, None]
    assert scraper.calls == ['framed']
    assert store.rows == {}
    await sync.sync('framed', 2)
    assert scraper.calls == ['framed']


@pytest.mark.asyncio
async def test_cancel_of_the_caller_still_propagates() -> None:
    gate = asyncio.Event()
    sync, _, scraper, _, _ = make_sync(scraper=FakeScraper(gate=gate))
    caller = asyncio.create_task(sync.sync('framed', 2))
    await wait_until(lambda: scraper.calls == ['framed'])

    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.parametrize(('newest', 'limits'), [(7, [7]), (None, []), (10**12, [1696])], ids=['newest', 'none', 'huge'])
@pytest.mark.asyncio
async def test_sync_newest_clamps_the_looked_up_round(newest: int | None, limits: list[int]) -> None:
    sync, store, _, _, _ = make_sync()

    async def newest_round() -> int | None:
        return newest

    await sync.sync_newest('framed', newest_round)

    assert store.limits == limits


@pytest.mark.asyncio
async def test_shutdown_cancels_a_stalled_newest_round_lookup() -> None:
    sync, store, _, _, _ = make_sync()
    started = asyncio.Event()

    async def stalled() -> int | None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(sync.sync_newest('framed', stalled))
    await asyncio.wait_for(started.wait(), 1)

    await asyncio.wait_for(sync.shutdown(), 1)

    assert await caller is None
    assert store.limits == []
    await sync.sync_newest('framed', stalled)
