import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from answers import KIND_BY_GAME, Game, Kind, MatchStatus
from answers.assets import Asset, AssetError, Mode
from answers.pipeline import Resolution, Resolver
from answers.ratelimit import Throttled
from answers.scraper import BundleEntry, ScrapeError
from answers.store import PendingAnswer
from answers.sync import AnswerSync
from answers.tmdb import TitleData, TmdbClient, TmdbError, TmdbRateLimited

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
    assets: dict[tuple[Mode, int], Asset] = field(default_factory=dict)

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
        if row.match_status != 'manual' and (row.match_status != 'site' or status == 'site'):
            row.title_id, row.match_status = data.tmdb_id, status

    async def mark_unmatched(self, game: Game, round_number: int) -> None:
        row = self.rows[(game, round_number)]
        if row.match_status not in ('manual', 'site'):
            row.match_status = 'unmatched'

    async def source_titles(self, game: Game) -> list[str]:
        return [row.source_title for (row_game, _), row in sorted(self.rows.items()) if row_game == game]

    async def missing_asset_days(self, mode: Mode, up_to_day: int) -> list[int]:
        return [day for day in range(1, up_to_day + 1) if (mode, day) not in self.assets]

    async def save_asset(self, asset: Asset) -> None:
        self.assets.setdefault((asset.mode, asset.day), asset)

    async def site_updates(self, up_to_round: int) -> list[tuple[int, int]]:
        site = {a.classic_day: a.tmdb_id for a in self.assets.values() if a.classic_day and a.tmdb_id}
        return [
            (number, site[number])
            for (game, number), row in sorted(self.rows.items())
            if game == 'framed'
            and number <= up_to_round
            and number in site
            and row.match_status != 'manual'
            and (row.match_status != 'site' or row.title_id != site[number])
        ]


@dataclass
class FakeScraper:
    titles: list[str] = field(default_factory=lambda: list(TITLES))
    error: Exception | None = None
    calls: list[Game] = field(default_factory=list)
    gate: asyncio.Event | None = None

    async def __call__(self, game: Game) -> list[BundleEntry]:
        self.calls.append(game)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return [BundleEntry(title, 100 + number) for number, title in enumerate(self.titles, start=1)]


def title_data(tmdb_id: int, kind: Kind = 'movie') -> TitleData:
    return TitleData(tmdb_id, kind, f'Title {tmdb_id}', f'Title {tmdb_id}', 2000, 100, ['US'], None, [], [], [], {})


@dataclass
class FakeTmdb:
    """Both the resolver and the title source of the sync."""

    # source_title -> outcome of resolve: a Resolution, None, or an exception.
    outcomes: dict[str, Resolution | None | Exception] = field(default_factory=dict)
    finds: list[tuple[Kind, str]] = field(default_factory=list)
    image_ids: list[int | None] = field(default_factory=list)
    siblings: list[dict[str, frozenset[int]]] = field(default_factory=list)
    detail_errors: dict[int, Exception] = field(default_factory=dict)
    closed: bool = False

    async def resolve(
        self,
        game: Game,
        round_number: int,
        source_title: str,
        siblings: dict[str, frozenset[int]],
        image_id: int | None,
    ) -> Resolution | None:
        self.finds.append((KIND_BY_GAME[game], source_title))
        self.image_ids.append(image_id)
        self.siblings.append(siblings)
        if source_title not in self.outcomes:
            return Resolution(int(source_title.split()[-1]), 'auto')
        outcome = self.outcomes[source_title]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.closed = True

    async def details(self, kind: Kind, tmdb_id: int) -> TitleData:
        if tmdb_id in self.detail_errors:
            raise self.detail_errors[tmdb_id]
        return title_data(tmdb_id, kind)


@dataclass
class FakeAssets:
    # (mode, day) -> asset; other days return 404 rows. An exception stops the mode.
    found: dict[tuple[Mode, int], Asset | Exception] = field(default_factory=dict)
    requests: list[tuple[Mode, int]] = field(default_factory=list)

    async def fetch(self, mode: Mode, day: int) -> Asset:
        self.requests.append((mode, day))
        outcome = self.found.get((mode, day), Asset(mode, day, None, None, None))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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
    assets: FakeAssets | None = None,
) -> tuple[AnswerSync, FakeStore, FakeScraper, FakeTmdb, Alerts]:
    store, scraper, tmdb, alerts = store or FakeStore(), scraper or FakeScraper(), tmdb or FakeTmdb(), Alerts()
    sync = AnswerSync(store, scraper, tmdb, alerts, today=lambda: today, resolver=tmdb, assets=assets)
    return sync, store, scraper, tmdb, alerts


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
    sync, store, _, _, _ = make_sync(tmdb=FakeTmdb({'Film 1': Resolution(77, 'low_confidence')}))

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
            # Variants such as 'Film III' find nothing.
            return httpx.Response(200, json=searches.get(request.url.params['query'], {'results': []}))
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
    sync = AnswerSync(store, FakeScraper(), tmdb, Alerts(), today=lambda: TODAY, resolver=Resolver(tmdb.search))

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


def site_asset(mode: Mode, day: int, classic_day: int, tmdb_id: int) -> Asset:
    return Asset(mode, day, classic_day, tmdb_id, None)


@pytest.mark.asyncio
async def test_framed_sync_fetches_mode_days_up_to_today_only() -> None:
    # 2025-06-07 is titleshot day 3; poster day 1 is 2025-07-25.
    assets = FakeAssets()
    sync, store, _, _, _ = make_sync(today=date(2025, 6, 7), assets=assets)

    await sync.sync('framed', 2)
    await sync.sync('framed', 2)

    # All three days are recent 404s: both syncs ask again, and neither goes past day 3.
    assert assets.requests == [('titleshot', day) for day in (1, 2, 3, 1, 2, 3)]
    assert store.assets == {}


@pytest.mark.asyncio
async def test_episode_sync_fetches_no_assets() -> None:
    assets = FakeAssets()
    sync, _, _, _, _ = make_sync(today=date(2025, 6, 7), assets=assets)

    await sync.sync('episode', 2)

    assert assets.requests == []


@pytest.mark.asyncio
async def test_site_ids_win_over_text_and_skip_the_resolver() -> None:
    assets = FakeAssets({('titleshot', 1): site_asset('titleshot', 1, 2, 900)})
    sync, store, _, tmdb, _ = make_sync(today=date(2025, 6, 5), assets=assets)

    await sync.sync('framed', 3)

    assert store.rows[('framed', 2)] == Row('Film 2', 900, 'site')
    assert tmdb.finds == [('movie', 'Film 1'), ('movie', 'Film 3')]


@pytest.mark.asyncio
async def test_a_new_site_id_replaces_a_text_match_but_text_never_replaces_site() -> None:
    store = FakeStore(
        {
            ('framed', 1): Row('Film 1', 1, 'auto'),
            ('framed', 2): Row('Film 2', 800, 'site'),
            ('framed', 3): Row('Film 3', 3, 'manual'),
        }
    )
    assets = FakeAssets(
        {
            ('titleshot', 1): site_asset('titleshot', 1, 1, 700),
            ('titleshot', 2): site_asset('titleshot', 2, 3, 600),
        }
    )
    sync, _, _, _, _ = make_sync(store, today=date(2025, 6, 6), assets=assets)

    await sync.sync('framed', 3)
    await store.save_title('framed', 2, title_data(5), 'auto')

    assert store.rows[('framed', 1)] == Row('Film 1', 700, 'site')
    assert store.rows[('framed', 2)] == Row('Film 2', 800, 'site')
    assert store.rows[('framed', 3)] == Row('Film 3', 3, 'manual')


@pytest.mark.asyncio
async def test_an_asset_error_stops_that_mode_and_matching_continues(caplog: pytest.LogCaptureFixture) -> None:
    assets = FakeAssets({('titleshot', 2): AssetError('titleshot day 2 returned HTTP 500')})
    sync, store, _, _, _ = make_sync(today=date(2025, 7, 25), assets=assets)

    await sync.sync('framed', 1)

    assert ('titleshot', 3) not in assets.requests
    assert ('poster', 1) in assets.requests
    assert store.rows[('framed', 1)] == Row('Film 1', 1, 'auto')
    assert 'HTTP 500' in caplog.text


@pytest.mark.asyncio
async def test_a_rate_limit_in_the_site_step_stops_the_run() -> None:
    assets = FakeAssets({('titleshot', 1): site_asset('titleshot', 1, 1, 900)})
    tmdb = FakeTmdb(detail_errors={900: TmdbRateLimited('429')})
    sync, store, _, _, _ = make_sync(tmdb=tmdb, today=date(2025, 6, 5), assets=assets)

    await sync.sync('framed', 2)

    assert store.rows[('framed', 1)] == Row('Film 1')
    assert tmdb.finds == []


@pytest.mark.asyncio
async def test_the_resolver_gets_bundle_image_ids_and_sibling_years() -> None:
    titles = ['Halloween (2018)', 'Halloween'] + TITLES[2:]
    sync, store, _, tmdb, _ = make_sync(
        scraper=FakeScraper(titles=titles),
        tmdb=FakeTmdb({'Halloween (2018)': Resolution(5, 'auto'), 'Halloween': Resolution(6, 'visual')}),
    )

    await sync.sync('framed', 2)

    assert tmdb.image_ids == [101, 102]
    assert tmdb.siblings[1] == {'Halloween': frozenset({2018})}
    assert store.rows[('framed', 2)] == Row('Halloween', 6, 'visual')


@pytest.mark.asyncio
async def test_pending_rows_scrape_the_bundle_once_for_image_ids() -> None:
    store = FakeStore({('framed', 1): Row('Film 1'), ('framed', 2): Row('Film 2')})
    sync, _, scraper, tmdb, _ = make_sync(store)

    await sync.sync('framed', 2)
    store.rows[('framed', 1)] = Row('Film 1')
    await sync.sync('framed', 2)

    assert scraper.calls == ['framed']
    assert tmdb.image_ids == [101, 102, 101]


@pytest.mark.asyncio
async def test_a_failed_bundle_scrape_still_matches_without_image_ids() -> None:
    store = FakeStore({('framed', 1): Row('Film 1')})
    sync, _, _, tmdb, alerts = make_sync(store, scraper=FakeScraper(error=ScrapeError('no array')))

    await sync.sync('framed', 1)

    assert tmdb.image_ids == [None]
    assert store.rows[('framed', 1)] == Row('Film 1', 1, 'auto')
    assert len(alerts.messages) == 1


@pytest.mark.asyncio
async def test_a_throttled_asset_request_stops_both_modes() -> None:
    assets = FakeAssets({('titleshot', 2): Throttled('titleshot day 2 returned HTTP 429')})
    sync, store, _, tmdb, _ = make_sync(today=date(2025, 7, 26), assets=assets)

    await sync.sync('framed', 1)

    assert assets.requests == [('titleshot', 1), ('titleshot', 2)]
    # The run ends: no site ids, no matching; the round stays pending.
    assert store.rows[('framed', 1)] == Row('Film 1')
    assert tmdb.finds == []


@pytest.mark.asyncio
async def test_a_throttled_tie_break_stops_the_run_and_keeps_the_round_pending() -> None:
    tmdb = FakeTmdb({'Film 2': Throttled('Image host returned HTTP 429')})
    sync, store, _, _, _ = make_sync(tmdb=tmdb)

    await sync.sync('framed', 3)

    assert store.rows[('framed', 2)] == Row('Film 2')
    assert store.rows[('framed', 3)] == Row('Film 3')
    assert tmdb.finds == [('movie', 'Film 1'), ('movie', 'Film 2')]


@pytest.mark.asyncio
async def test_a_throttled_scrape_does_not_alert_the_admin() -> None:
    sync, store, _, _, alerts = make_sync(scraper=FakeScraper(error=Throttled('429')))

    await sync.sync('framed', 1)

    assert store.rows == {}
    assert alerts.messages == []


@pytest.mark.asyncio
async def test_shutdown_closes_the_resolver() -> None:
    sync, _, _, tmdb, _ = make_sync()

    await sync.shutdown()

    assert tmdb.closed


@pytest.mark.asyncio
async def test_shutdown_stays_bounded_when_the_resolver_does_not_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import answers.sync

    monkeypatch.setattr(answers.sync, 'ACTIVE_TIMEOUT', 0.1)
    monkeypatch.setattr(answers.sync, 'SHUTDOWN_TIMEOUT', 0.3)
    sync, _, _, tmdb, _ = make_sync()

    async def hang() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(tmdb, 'close', hang)

    await asyncio.wait_for(sync.shutdown(), 1)

    assert 'did not stop in time' in caplog.text


@pytest.mark.asyncio
async def test_a_throttled_scrape_ends_the_run_before_assets_and_matching() -> None:
    store = FakeStore({('framed', 1): Row('Film 1')})
    assets = FakeAssets()
    sync, _, _, tmdb, alerts = make_sync(
        store, FakeScraper(error=Throttled('429')), today=date(2025, 6, 7), assets=assets
    )

    await sync.sync('framed', 2)

    assert assets.requests == []
    assert tmdb.finds == []
    assert alerts.messages == []


@pytest.mark.asyncio
async def test_a_throttled_bundle_scrape_for_image_ids_ends_the_run() -> None:
    store = FakeStore({('framed', 1): Row('Film 1')})
    sync, _, _, tmdb, alerts = make_sync(store, FakeScraper(error=Throttled('429')))

    await sync.sync('framed', 1)

    assert tmdb.finds == []
    assert store.rows[('framed', 1)] == Row('Film 1')
    assert alerts.messages == []


@pytest.mark.asyncio
async def test_shutdown_stays_bounded_when_the_resolver_ignores_cancellation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import answers.sync

    monkeypatch.setattr(answers.sync, 'ACTIVE_TIMEOUT', 0.1)
    monkeypatch.setattr(answers.sync, 'SHUTDOWN_TIMEOUT', 0.3)
    sync, _, _, tmdb, _ = make_sync()
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(tmdb, 'close', stubborn)

    await asyncio.wait_for(sync.shutdown(), 1)

    release.set()
    assert 'did not stop in time' in caplog.text


@pytest.mark.asyncio
async def test_shutdown_logs_a_failing_resolver_close_by_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sync, _, _, tmdb, _ = make_sync()

    async def broken() -> None:
        raise OSError('sensitive path')

    monkeypatch.setattr(tmdb, 'close', broken)

    await sync.shutdown()

    assert 'OSError' in caplog.text
    assert 'sensitive path' not in caplog.text


@pytest.mark.asyncio
async def test_a_recent_404_day_is_fetched_again_and_an_old_one_is_not() -> None:
    # 2025-06-20 is titleshot day 16: days 3..16 are recent, days 1 and 2 are old.
    assets = FakeAssets()
    sync, store, _, _, _ = make_sync(today=date(2025, 6, 20), assets=assets)

    await sync.sync('framed', 1)
    first = len(assets.requests)
    await sync.sync('framed', 1)

    assert sorted(day for mode, day in store.assets if mode == 'titleshot') == [1, 2]
    again = assets.requests[first:]
    assert ('titleshot', 1) not in again
    assert ('titleshot', 2) not in again
    assert [day for mode, day in again if mode == 'titleshot'] == list(range(3, 17))


@pytest.mark.asyncio
async def test_a_recent_day_with_ids_is_stored() -> None:
    assets = FakeAssets({('titleshot', 3): site_asset('titleshot', 3, 5, 900)})
    sync, store, _, _, _ = make_sync(today=date(2025, 6, 7), assets=assets)

    await sync.sync('framed', 1)

    assert ('titleshot', 3) in store.assets


@pytest.mark.asyncio
async def test_a_scrape_failure_does_not_log_the_transport_text(caplog: pytest.LogCaptureFixture) -> None:
    sync, _, _, _, _ = make_sync(scraper=FakeScraper(error=httpx.ConnectError('synthetic transport secret')))

    await sync.sync('framed', 1)

    assert 'ConnectError' in caplog.text
    assert 'synthetic transport secret' not in caplog.text
