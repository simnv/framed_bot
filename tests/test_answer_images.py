from typing import Any

import httpx
import pytest

from answers.images import ImageError, ImageFetcher, frame_urls, has_frames
from answers.ratelimit import RateLimiter, Throttled
from answers.tmdb import TmdbClient, TmdbError

JPEG = {'Content-Type': 'image/jpeg'}


def fetcher_for(routes: dict[str, httpx.Response], requests: list[httpx.Request]) -> ImageFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return routes.get(str(request.url), httpx.Response(404, headers={'Content-Type': 'text/html'}))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ImageFetcher(client, RateLimiter(1000), RateLimiter(1000))


def test_frame_urls_follow_the_site_layout() -> None:
    assert frame_urls('framed', 1000) == [f'https://framed.wtf/images/1000/00{n}.jpeg' for n in range(1, 7)]
    assert frame_urls('episode', 5)[-1] == 'https://episode.wtf/images/5/010.jpeg'


def test_framed_images_one_to_thirty_two_have_no_frames() -> None:
    assert not has_frames('framed', 32)
    assert has_frames('framed', 33)
    assert has_frames('episode', 1)


@pytest.mark.asyncio
async def test_frames_skip_missing_files_and_send_the_user_agent() -> None:
    requests: list[httpx.Request] = []
    fetcher = fetcher_for(
        {'https://framed.wtf/images/40/002.jpeg': httpx.Response(200, content=b'jpg', headers=JPEG)}, requests
    )

    assert await fetcher.frames('framed', 40) == [b'jpg']
    assert len(requests) == 6
    assert {request.headers['User-Agent'] for request in requests} == {'framed-bot/1.0'}


@pytest.mark.asyncio
async def test_frames_of_early_framed_rounds_make_no_request() -> None:
    requests: list[httpx.Request] = []

    assert await fetcher_for({}, requests).frames('framed', 12) == []
    assert requests == []


@pytest.mark.asyncio
async def test_tmdb_images_drop_non_images_and_oversized_files() -> None:
    base = 'https://image.tmdb.org/t/p/w300'
    fetcher = fetcher_for(
        {
            f'{base}/a.jpg': httpx.Response(200, content=b'a', headers=JPEG),
            f'{base}/b.jpg': httpx.Response(200, content=b'<html>', headers={'Content-Type': 'text/html'}),
            f'{base}/c.jpg': httpx.Response(200, content=b'x' * 1_000_001, headers=JPEG),
        },
        [],
    )

    assert await fetcher.tmdb_images(['/a.jpg', '/b.jpg', '/c.jpg', '/d.jpg']) == [b'a']


@pytest.mark.asyncio
async def test_transport_errors_raise_image_error_without_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('secret detail')

    fetcher = ImageFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), RateLimiter(1000), RateLimiter(1000)
    )

    with pytest.raises(ImageError) as raised:
        await fetcher.frames('episode', 1)

    assert 'secret detail' not in str(raised.value)


def tmdb_for(routes: dict[str, Any]) -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        return httpx.Response(404) if body is None else httpx.Response(200, json=body)

    return TmdbClient(
        'synthetic-tmdb-token', httpx.AsyncClient(transport=httpx.MockTransport(handler)), RateLimiter(1000)
    )


@pytest.mark.asyncio
async def test_image_paths_rank_backdrops_by_votes_and_keep_twelve() -> None:
    backdrops = [{'file_path': f'/b{n}.jpg', 'vote_average': n, 'iso_639_1': None} for n in range(15)]
    tmdb = tmdb_for({'/3/movie/7/images': {'backdrops': backdrops}})

    assert await tmdb.image_paths('movie', 7) == [f'/b{n}.jpg' for n in range(14, 2, -1)]


@pytest.mark.asyncio
async def test_image_paths_add_tv_stills_of_seasons_one_and_two() -> None:
    episodes = [{'still_path': f'/s{n}.jpg'} for n in range(12)] + [{'still_path': None}]
    tmdb = tmdb_for(
        {
            '/3/tv/9/images': {'backdrops': [{'file_path': '/b.jpg'}]},
            '/3/tv/9/season/1': {'episodes': episodes},
        }
    )

    assert await tmdb.image_paths('tv', 9) == ['/b.jpg'] + [f'/s{n}.jpg' for n in range(10)]


@pytest.mark.parametrize(
    'backdrop',
    [
        {'file_path': 'https://evil.test/x.jpg'},
        {'file_path': '/../x.jpg'},
        {'file_path': 7},
        {'file_path': '/a.jpg', 'vote_average': '9'},
        {},
    ],
)
@pytest.mark.asyncio
async def test_image_paths_reject_unexpected_values(backdrop: dict[str, Any]) -> None:
    tmdb = tmdb_for({'/3/movie/7/images': {'backdrops': [backdrop]}})

    with pytest.raises(TmdbError, match='unexpected shape'):
        await tmdb.image_paths('movie', 7)


@pytest.mark.parametrize('host', ['https://episode.wtf/images/5/001.jpeg', 'https://image.tmdb.org/t/p/w300/a.jpg'])
@pytest.mark.asyncio
async def test_http_429_stops_the_downloads_and_pauses_that_host_group(host: str) -> None:
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    site = RateLimiter(1000, clock=lambda: clock[0], sleep=sleep)
    cdn = RateLimiter(1000, clock=lambda: clock[0], sleep=sleep)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '60'})

    fetcher = ImageFetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), site, cdn)

    with pytest.raises(Throttled):
        if 'episode' in host:
            await fetcher.frames('episode', 5)
        else:
            await fetcher.tmdb_images(['/a.jpg', '/b.jpg'])

    assert [str(request.url) for request in requests] == [host]
    paused, other = (site, cdn) if 'episode' in host else (cdn, site)
    await other.wait()
    assert clock[0] == 0.0
    await paused.wait()
    assert clock[0] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_a_declared_oversized_image_is_not_read() -> None:
    base = 'https://image.tmdb.org/t/p/w300'
    headers = {**JPEG, 'Content-Length': '1000001'}
    fetcher = fetcher_for({f'{base}/a.jpg': httpx.Response(200, content=b'x' * 1_000_001, headers=headers)}, [])

    assert await fetcher.tmdb_images(['/a.jpg']) == []


@pytest.mark.asyncio
async def test_candidate_downloads_are_capped() -> None:
    requests: list[httpx.Request] = []
    fetcher = fetcher_for({}, requests)

    await fetcher.tmdb_images([f'/i{n}.jpg' for n in range(40)])

    assert len(requests) == 32


@pytest.mark.asyncio
async def test_image_downloads_do_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://evil.test/a.jpg'})

    fetcher = ImageFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), RateLimiter(1000), RateLimiter(1000)
    )

    assert await fetcher.tmdb_images(['/a.jpg']) == []
    assert [request.url.host for request in requests] == ['image.tmdb.org']


@pytest.mark.parametrize('vote', [True])
@pytest.mark.asyncio
async def test_image_paths_reject_bool_votes(vote: object) -> None:
    tmdb = tmdb_for({'/3/movie/7/images': {'backdrops': [{'file_path': '/a.jpg', 'vote_average': vote}]}})

    with pytest.raises(TmdbError, match='unexpected shape'):
        await tmdb.image_paths('movie', 7)


@pytest.mark.parametrize('literal', ['NaN', 'Infinity'])
@pytest.mark.asyncio
async def test_image_paths_reject_non_finite_votes(literal: str) -> None:
    body = f'{{"backdrops": [{{"file_path": "/a.jpg", "vote_average": {literal}}}]}}'
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
    tmdb = TmdbClient('synthetic-tmdb-token', client, RateLimiter(1000))

    with pytest.raises(TmdbError, match='unexpected shape'):
        await tmdb.image_paths('movie', 7)
