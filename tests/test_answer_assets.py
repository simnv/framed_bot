from datetime import date
from typing import Any

import httpx
import pytest

from answers.assets import Asset, AssetError, current_mode_day, fetch_asset, parse_asset
from answers.ratelimit import RateLimiter, Throttled


def body(day: int = 7, **asset: Any) -> dict[str, Any]:
    values = {'external_id': 391, 'imdb_id': 'tt0058461', 'type': 'MOVIE', 'classic_day': 36, **asset}
    return {'id': 1, 'day': day, 'asset_id': 36, 'asset': values}


def client_for(response: httpx.Response, requests: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ('mode', 'today', 'day'),
    [
        ('titleshot', date(2025, 6, 4), 0),
        ('titleshot', date(2025, 6, 5), 1),
        ('titleshot', date(2026, 11, 1), 515),
        ('poster', date(2025, 7, 24), 0),
        ('poster', date(2025, 7, 25), 1),
        ('poster', date(2026, 11, 1), 465),
    ],
)
def test_current_mode_day_counts_from_the_bundle_first_day(mode: Any, today: date, day: int) -> None:
    assert current_mode_day(mode, today) == day


def test_parse_asset_reads_ids() -> None:
    assert parse_asset('titleshot', 7, body()) == Asset('titleshot', 7, 36, 391, 'tt0058461')


def test_parse_asset_keeps_null_ids() -> None:
    parsed = parse_asset('poster', 7, body(external_id=None, imdb_id=None, classic_day=None))

    assert parsed == Asset('poster', 7, None, None, None)


def test_parse_asset_drops_the_tmdb_id_of_a_tv_asset() -> None:
    assert parse_asset('poster', 7, body(type='TV')).tmdb_id is None


@pytest.mark.parametrize(
    'data',
    [
        None,
        [],
        {'day': 7},
        {'day': 8, 'asset': {}},
        body(external_id='391'),
        body(external_id=True),
        body(external_id=0),
        body(classic_day=1.5),
        body(imdb_id='nm123'),
        body(imdb_id=7),
        body(type=None),
    ],
)
def test_parse_asset_rejects_unexpected_shapes(data: object) -> None:
    with pytest.raises(AssetError):
        parse_asset('titleshot', 7, data)


@pytest.mark.asyncio
async def test_fetch_asset_requests_the_day_with_the_bot_user_agent() -> None:
    requests: list[httpx.Request] = []
    client = client_for(httpx.Response(200, json=body()), requests)

    asset = await fetch_asset(client, RateLimiter(1000), 'titleshot', 7)

    assert asset.tmdb_id == 391
    assert str(requests[0].url) == 'https://core.framed.wtf/titleshot/challenges/day/7'
    assert requests[0].headers['User-Agent'] == 'framed-bot/1.0'


@pytest.mark.asyncio
async def test_fetch_asset_stores_a_404_as_null_ids() -> None:
    client = client_for(httpx.Response(404, json={'error': 'No challenge found for the specified day'}), [])

    assert await fetch_asset(client, RateLimiter(1000), 'poster', 343) == Asset('poster', 343, None, None, None)


@pytest.mark.parametrize(
    'response',
    [httpx.Response(500), httpx.Response(302, headers={'Location': 'https://example.test/'}), httpx.Response(200)],
    ids=['server-error', 'redirect', 'invalid-json'],
)
@pytest.mark.asyncio
async def test_fetch_asset_raises_on_other_responses(response: httpx.Response) -> None:
    with pytest.raises(AssetError):
        await fetch_asset(client_for(response, []), RateLimiter(1000), 'poster', 1)


@pytest.mark.asyncio
async def test_fetch_asset_hides_transport_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('secret detail')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(AssetError) as raised:
        await fetch_asset(client, RateLimiter(1000), 'poster', 1)

    assert 'secret detail' not in str(raised.value)


@pytest.mark.asyncio
async def test_fetch_asset_raises_throttled_and_pauses_the_site_group() -> None:
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    limiter = RateLimiter(1000, clock=lambda: clock[0], sleep=sleep)
    client = client_for(httpx.Response(429, headers={'Retry-After': '20'}), [])

    with pytest.raises(Throttled):
        await fetch_asset(client, limiter, 'titleshot', 1)

    await limiter.wait()
    assert clock[0] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_fetch_asset_does_not_follow_redirects_of_a_redirect_enabled_client() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={'Location': 'https://core.framed.wtf/titleshot/challenges/day/1'})
        return httpx.Response(200, json=body(day=1))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with pytest.raises(AssetError):
        await fetch_asset(client, RateLimiter(1000), 'titleshot', 1)

    assert len(calls) == 1


@pytest.mark.parametrize('day', [True, 1.0])
def test_parse_asset_rejects_a_day_that_is_not_an_int(day: Any) -> None:
    with pytest.raises(AssetError):
        parse_asset('titleshot', 1, body(day=day))


@pytest.mark.parametrize('imdb_id', ['tt１２３', 'tt١٢٣'])
def test_parse_asset_rejects_non_ascii_imdb_digits(imdb_id: str) -> None:
    with pytest.raises(AssetError):
        parse_asset('titleshot', 7, body(imdb_id=imdb_id))
