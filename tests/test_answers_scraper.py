from pathlib import Path

import httpx
import pytest

from answers.ratelimit import RateLimiter, Throttled
from answers.scraper import (
    BundleEntry,
    ScrapeError,
    extract_entries,
    extract_titles,
    fetch_entries,
    fetch_titles,
    js_unescape,
)

FIXTURES = Path(__file__).with_name('fixtures')
FAST = RateLimiter(1000)
FRAMED_ROUTES = {
    'https://framed.wtf/': 'framed_home.html',
    'https://framed.wtf/_next/static/chunks/pages/_app-073c1f0998963a1e.js': 'framed_app.js',
}
EPISODE_ROUTES = {
    'https://episode.wtf/': 'episode_home.html',
    'https://episode.wtf/_next/static/chunks/webpack-fe87f2cbe8ee01db.js': 'episode_webpack.js',
    'https://episode.wtf/_next/static/chunks/380.7727e6affb0592d6.js': 'episode_380.js',
    'https://episode.wtf/_next/static/chunks/87.0111d1a5ef9290c6.js': 'episode_87.js',
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def client_for(routes: dict[str, str], requests: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        name = routes.get(str(request.url))
        if name is None:
            return httpx.Response(404)
        return httpx.Response(200, text=fixture(name))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def short_array(count: int, first_id: int = 1) -> str:
    entries = ','.join(f'{{title:"Film {number}",id:{number}}}' for number in range(first_id, first_id + count))
    return 'var n;var r=[' + entries + ']},9953:function(e,t,i){}'


@pytest.mark.asyncio
async def test_framed_round_n_is_entry_n_minus_one() -> None:
    requests: list[httpx.Request] = []
    async with client_for(FRAMED_ROUTES, requests) as client:
        titles = await fetch_titles('framed', client, FAST)

    assert len(titles) == 114
    assert titles[0] == 'The Shawshank Redemption'
    assert titles[1] == 'Reservoir Dogs'
    assert [request.headers['User-Agent'] for request in requests] == ['framed-bot/1.0'] * 2


@pytest.mark.asyncio
async def test_framed_keeps_year_suffix_and_decodes_escapes() -> None:
    async with client_for(FRAMED_ROUTES, []) as client:
        titles = await fetch_titles('framed', client, FAST)

    # The trimmed fixture appends real later entries after the first 110 rounds.
    assert titles[110:] == ['A Star Is Born (2018)', "You've Got Mail", 'Toy Story 3', 'Halloween (2018)']
    assert titles[59] == 'Les Misérables'


def test_framed_skips_mode_arrays_that_do_not_start_at_round_one() -> None:
    # The fixture also holds a real 5-entry mode array that starts at id 423.
    assert len(extract_titles(fixture('framed_app.js'))) == 114
    with pytest.raises(ScrapeError):
        extract_titles(short_array(150, first_id=423))


@pytest.mark.asyncio
async def test_episode_finds_answers_chunk_through_webpack_runtime() -> None:
    requests: list[httpx.Request] = []
    async with client_for(EPISODE_ROUTES, requests) as client:
        titles = await fetch_titles('episode', client, FAST)

    assert len(titles) == 112
    assert titles[0] == 'Game of Thrones'
    # Entries with occurrences:2 are normal rounds that repeat an earlier show.
    assert titles[110:] == ['The Wire', 'The Boys']
    assert [str(request.url) for request in requests] == list(EPISODE_ROUTES)


@pytest.mark.parametrize('script', ['var r=1;', short_array(99)], ids=['missing', 'short'])
def test_extract_titles_fails_on_missing_or_short_array(script: str) -> None:
    with pytest.raises(ScrapeError):
        extract_titles(script)


def test_extract_titles_accepts_one_hundred_entries() -> None:
    assert len(extract_titles(short_array(100))) == 100


def test_extract_titles_fails_on_two_answer_arrays() -> None:
    with pytest.raises(ScrapeError):
        extract_titles(short_array(100) + short_array(100))


@pytest.mark.asyncio
async def test_episode_fails_when_no_chunk_has_answers() -> None:
    routes = dict(EPISODE_ROUTES)
    routes['https://episode.wtf/_next/static/chunks/87.0111d1a5ef9290c6.js'] = 'episode_380.js'
    async with client_for(routes, []) as client:
        with pytest.raises(ScrapeError):
            await fetch_titles('episode', client, FAST)


@pytest.mark.asyncio
async def test_framed_fails_when_home_page_has_no_app_bundle() -> None:
    routes = dict(FRAMED_ROUTES)
    routes['https://framed.wtf/'] = 'episode_380.js'
    async with client_for(routes, []) as client:
        with pytest.raises(ScrapeError):
            await fetch_titles('framed', client, FAST)


@pytest.mark.asyncio
async def test_http_error_propagates() -> None:
    async with client_for({}, []) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_titles('framed', client, FAST)


def test_js_unescape() -> None:
    assert js_unescape(r'Pok\xe9mon \u00e9 \u{1F3AC} \ud83c\udfac \"q\" it\'s') == 'Pokémon é 🎬 🎬 "q" it\'s'


def text_client(texts: dict[str, str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = texts.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def array_with(first_title: str = 'Film 1', first_id: str = '1') -> str:
    entries = [f'{{title:"{first_title}",id:{first_id}}}']
    entries += [f'{{title:"Film {number}",id:{number}}}' for number in range(2, 101)]
    return 'var r=[' + ','.join(entries) + ']},9953:function(e,t,i){}'


@pytest.mark.asyncio
async def test_episode_rejects_answer_arrays_in_two_chunks() -> None:
    texts = {
        'https://episode.wtf/': fixture('episode_home.html'),
        'https://episode.wtf/_next/static/chunks/webpack-fe87f2cbe8ee01db.js': (
            'n.u=function(e){return"static/chunks/"+e+"."+{87:"0111d1a5ef9290c6",91:"0123456789abcdef"}[e]+".js"}'
        ),
        'https://episode.wtf/_next/static/chunks/87.0111d1a5ef9290c6.js': short_array(100),
        'https://episode.wtf/_next/static/chunks/91.0123456789abcdef.js': short_array(100).replace('Film', 'Other'),
    }
    async with text_client(texts) as client:
        with pytest.raises(ScrapeError):
            await fetch_titles('episode', client, FAST)


@pytest.mark.parametrize(
    'script',
    [array_with(first_title=r'\u{110000}'), array_with(first_title=r'\ud800'), array_with(first_id='1e')],
    ids=['code-point-out-of-range', 'lone-surrogate', 'malformed-first-id'],
)
def test_extract_titles_rejects_malformed_values(script: str) -> None:
    with pytest.raises(ScrapeError):
        extract_titles(script)


@pytest.mark.parametrize('raw', [r'\u{110000}', r'\ud800'], ids=['code-point-out-of-range', 'lone-surrogate'])
def test_js_unescape_rejects_malformed_escapes(raw: str) -> None:
    with pytest.raises(ScrapeError):
        js_unescape(raw)


@pytest.mark.asyncio
async def test_episode_accepts_identical_answer_arrays_in_two_chunks() -> None:
    texts = {
        'https://episode.wtf/': fixture('episode_home.html'),
        'https://episode.wtf/_next/static/chunks/webpack-fe87f2cbe8ee01db.js': (
            'n.u=function(e){return"static/chunks/"+e+"."+{87:"0111d1a5ef9290c6",88:"0123456789abcdef"}[e]+".js"}'
        ),
        'https://episode.wtf/_next/static/chunks/87.0111d1a5ef9290c6.js': short_array(100),
        'https://episode.wtf/_next/static/chunks/88.0123456789abcdef.js': short_array(100),
    }
    async with text_client(texts) as client:
        titles = await fetch_titles('episode', client, FAST)

    assert titles == [f'Film {number}' for number in range(1, 101)]


@pytest.mark.asyncio
async def test_framed_entries_carry_image_ids_written_as_js_numbers() -> None:
    async with client_for(FRAMED_ROUTES, []) as client:
        entries = await fetch_entries('framed', client, RateLimiter(1000))

    assert entries[0] == BundleEntry('The Shawshank Redemption', 1)
    assert BundleEntry('Toy Story 3', 1000) in entries


def test_extract_entries_reads_exponent_ids() -> None:
    script = short_array(100).replace('id:100}', 'id:1e3}')

    assert extract_entries(script)[-1] == BundleEntry('Film 100', 1000)


@pytest.mark.parametrize('raw_id', ['1.5', '0', '1e9'])
def test_extract_entries_rejects_ids_that_are_not_image_ids(raw_id: str) -> None:
    script = short_array(100).replace('id:100}', f'id:{raw_id}}}')

    with pytest.raises(ScrapeError, match='Malformed id'):
        extract_entries(script)


@pytest.mark.asyncio
async def test_every_bundle_request_waits_for_the_shared_limiter() -> None:
    waits: list[str] = []

    class Recording(RateLimiter):
        async def wait(self) -> None:
            waits.append('wait')

    requests: list[httpx.Request] = []
    async with client_for(EPISODE_ROUTES, requests) as client:
        await fetch_entries('episode', client, Recording(1))

    assert len(waits) == len(requests) == 4


@pytest.mark.asyncio
async def test_a_throttled_bundle_request_pauses_the_site_group() -> None:
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    limiter = RateLimiter(1000, clock=lambda: clock[0], sleep=sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={'Retry-After': '42'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Throttled):
            await fetch_entries('framed', client, limiter)

    await limiter.wait()
    assert clock[0] == pytest.approx(42.0)


@pytest.mark.asyncio
async def test_bundle_requests_do_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://evil.test/'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_entries('framed', client, RateLimiter(1000))

    assert [request.url.host for request in requests] == ['framed.wtf']


def test_extract_entries_rejects_non_ascii_digit_ids() -> None:
    script = short_array(100).replace('id:100}', 'id:١٠٠}')

    with pytest.raises(ScrapeError):
        extract_entries(script)
