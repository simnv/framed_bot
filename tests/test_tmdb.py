from typing import Any

import httpx
import pytest

from answers.ratelimit import RateLimiter, Throttled
from answers.tmdb import Credit, TitleData, TmdbClient, TmdbError, TmdbRateLimited, normalize, split_year

TOKEN = 'synthetic-tmdb-token'  # noqa: S105 - synthetic test value
MOVIE_DETAILS: dict[str, Any] = {
    'id': 424139,
    'title': 'Halloween',
    'original_title': 'Halloween',
    'release_date': '2018-10-18',
    'runtime': 106,
    'imdb_id': 'tt1502407',
    'production_countries': [{'iso_3166_1': 'US', 'name': 'United States of America'}],
    'genres': [{'id': 27, 'name': 'Horror'}, {'id': 53, 'name': 'Thriller'}],
    'production_companies': [{'id': 33, 'name': 'Universal Pictures', 'origin_country': 'US'}],
    'credits': {
        'cast': [
            {'id': 20750, 'name': 'Judy Greer', 'order': 1, 'character': 'Karen'},
            {'id': 8944, 'name': 'Jamie Lee Curtis', 'order': 0, 'character': 'Laurie Strode'},
        ]
        + [{'id': 100 + order, 'name': f'Actor {order}', 'order': order} for order in range(2, 12)],
        'crew': [
            {'id': 64141, 'name': 'David Gordon Green', 'job': 'Director'},
            {'id': 64141, 'name': 'David Gordon Green', 'job': 'Screenplay'},
            {'id': 64141, 'name': 'David Gordon Green', 'job': 'Director'},
        ],
    },
}
TV_DETAILS: dict[str, Any] = {
    'id': 76331,
    'name': 'Succession',
    'original_name': 'Succession',
    'first_air_date': '2018-06-03',
    'episode_run_time': [],
    'production_countries': [{'iso_3166_1': 'US', 'name': 'United States of America'}],
    'genres': [{'id': 18, 'name': 'Drama'}],
    'production_companies': [{'id': 3268, 'name': 'HBO'}],
    'networks': [{'id': 49, 'name': 'HBO'}],
    'created_by': [{'id': 62946, 'name': 'Jesse Armstrong'}],
    'credits': {'cast': [{'id': 239271, 'name': 'Jeremy Strong', 'order': 0}], 'crew': []},
    'external_ids': {'imdb_id': 'tt7660850'},
}


def tmdb_for(responses: dict[str, httpx.Response], requests: list[httpx.Request]) -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.get(request.url.path, httpx.Response(404))

    return TmdbClient(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def search(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={'page': 1, 'results': list(results), 'total_results': len(results)})


def test_split_year() -> None:
    assert split_year('Halloween (2018)') == ('Halloween', 2018)
    assert split_year('Blade Runner 2049') == ('Blade Runner 2049', None)
    assert split_year('(500) Days of Summer') == ('(500) Days of Summer', None)


def test_normalize_folds_case_and_strips_punctuation() -> None:
    assert normalize('Spider-Man: No Way Home') == normalize('spider man  no way home')
    assert normalize("Ocean's Eleven") == 'ocean s eleven'


@pytest.mark.asyncio
async def test_search_uses_bearer_token_and_sends_query_only_without_year() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for({'/3/search/movie': search({'id': 278, 'title': 'The Shawshank Redemption'})}, requests)

    [found] = await tmdb.search('movie', 'The Shawshank Redemption', None)

    assert found.tmdb_id == 278
    [request] = requests
    assert request.url.host == 'api.themoviedb.org'
    assert request.headers['Authorization'] == f'Bearer {TOKEN}'
    assert dict(request.url.params) == {'query': 'The Shawshank Redemption'}


@pytest.mark.asyncio
async def test_movie_details_map_to_title_data() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for({'/3/movie/424139': httpx.Response(200, json=MOVIE_DETAILS)}, requests)

    data = await tmdb.details('movie', 424139)

    assert dict(requests[0].url.params) == {'append_to_response': 'credits'}
    assert data == TitleData(
        tmdb_id=424139,
        kind='movie',
        name='Halloween',
        original_name='Halloween',
        year=2018,
        runtime=106,
        countries=['US'],
        imdb_id='tt1502407',
        genres=[(27, 'Horror'), (53, 'Thriller')],
        companies=[(33, 'Universal Pictures')],
        credits=[
            Credit(64141, 'David Gordon Green', 'director', 0),
            Credit(8944, 'Jamie Lee Curtis', 'cast', 0),
            Credit(20750, 'Judy Greer', 'cast', 1),
        ]
        + [Credit(100 + order, f'Actor {order}', 'cast', order) for order in range(2, 10)],
        raw=MOVIE_DETAILS,
    )


@pytest.mark.asyncio
async def test_tv_details_map_creators_and_first_run_time() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for({'/3/tv/76331': httpx.Response(200, json=TV_DETAILS | {'episode_run_time': [60, 55]})}, requests)

    data = await tmdb.details('tv', 76331)

    assert dict(requests[0].url.params) == {'append_to_response': 'credits,external_ids'}
    assert (data.kind, data.name, data.year, data.runtime) == ('tv', 'Succession', 2018, 60)
    assert data.imdb_id == 'tt7660850'
    assert data.companies == [(3268, 'HBO')]
    assert data.credits == [
        Credit(62946, 'Jesse Armstrong', 'creator', 0),
        Credit(239271, 'Jeremy Strong', 'cast', 0),
    ]


@pytest.mark.asyncio
async def test_tv_details_without_run_time_have_no_runtime() -> None:
    tmdb = tmdb_for({'/3/tv/76331': httpx.Response(200, json=TV_DETAILS)}, [])

    assert (await tmdb.details('tv', 76331)).runtime is None


@pytest.mark.parametrize('run_times', [None, [], [60, 55]], ids=['absent', 'empty', 'preferred'])
@pytest.mark.parametrize('last_episode', [None, {}, {'runtime': None}, {'runtime': 42}])
@pytest.mark.asyncio
async def test_tv_runtime_falls_back_to_last_episode(
    run_times: list[int] | None, last_episode: dict[str, Any] | None
) -> None:
    body = TV_DETAILS | {'last_episode_to_air': last_episode}
    if run_times is None:
        del body['episode_run_time']
    else:
        body['episode_run_time'] = run_times
    tmdb = tmdb_for({'/3/tv/76331': httpx.Response(200, json=body)}, [])

    data = await tmdb.details('tv', 76331)

    expected = 60 if run_times else (42 if last_episode == {'runtime': 42} else None)
    assert data.runtime == expected


@pytest.mark.parametrize('last_episode', [[], 'bad', 1, {'runtime': '42'}, {'runtime': True}, {'runtime': []}])
@pytest.mark.asyncio
async def test_tv_runtime_rejects_malformed_last_episode(last_episode: Any) -> None:
    body = TV_DETAILS | {'last_episode_to_air': last_episode}
    tmdb = tmdb_for({'/3/tv/76331': httpx.Response(200, json=body)}, [])

    with pytest.raises(TmdbError, match='^TMDB tv details have an unexpected shape$'):
        await tmdb.details('tv', 76331)


@pytest.mark.asyncio
async def test_rate_limit_raises_tmdb_rate_limited_and_pauses_the_client() -> None:
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={'Retry-After': '9'})

    limiter = RateLimiter(1000, clock=lambda: clock[0], sleep=sleep)
    tmdb = TmdbClient(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)), limiter)

    with pytest.raises(TmdbRateLimited) as raised:
        await tmdb.search('movie', 'Halloween', None)

    assert isinstance(raised.value, Throttled)
    await limiter.wait()
    assert clock[0] == pytest.approx(9.0)


@pytest.mark.parametrize(
    'response',
    [httpx.Response(500), httpx.Response(200, text='not json'), httpx.Response(200, json=[1])],
    ids=['server-error', 'invalid-json', 'not-an-object'],
)
@pytest.mark.asyncio
async def test_bad_responses_raise_tmdb_error(response: httpx.Response) -> None:
    tmdb = tmdb_for({'/3/search/movie': response}, [])

    with pytest.raises(TmdbError):
        await tmdb.search('movie', 'Halloween', None)


@pytest.mark.asyncio
async def test_timeout_raises_tmdb_error_without_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('timed out', request=request)

    tmdb = TmdbClient(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(TmdbError) as error:
        await tmdb.search('movie', 'Halloween', None)
    assert TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_details_with_unexpected_shape_raise_tmdb_error() -> None:
    tmdb = tmdb_for({'/3/movie/1': httpx.Response(200, json={'id': 1})}, [])

    with pytest.raises(TmdbError):
        await tmdb.details('movie', 1)


@pytest.mark.parametrize(
    'body',
    [
        {},
        {'results': None},
        {'results': {}},
        {'results': [None]},
        {'results': [{}]},
        {'results': [{'id': '1', 'title': 'X'}]},
        {'results': [{'id': True, 'title': 'X'}]},
        {'results': [{'id': 1, 'title': None}]},
    ],
    ids=[
        'no-results',
        'null-results',
        'object-results',
        'null-entry',
        'empty-entry',
        'string-id',
        'bool-id',
        'null-title',
    ],
)
@pytest.mark.asyncio
async def test_malformed_search_raises_fixed_tmdb_error(body: dict[str, Any]) -> None:
    tmdb = tmdb_for({'/3/search/movie': httpx.Response(200, json=body)}, [])

    with pytest.raises(TmdbError) as error:
        await tmdb.search('movie', 'Halloween', None)
    assert str(error.value) == 'TMDB movie search has an unexpected shape'


@pytest.mark.parametrize(
    'change',
    [
        {'title': None},
        {'id': None},
        {'credits': ['bad']},
        {'credits': {'cast': [{'id': 1, 'name': 'A'}]}},
        {'genres': [{'id': 'x', 'name': 'Horror'}]},
        {'production_countries': [None]},
        {'runtime': '106'},
        {'production_companies': None},
    ],
    ids=[
        'null-title',
        'null-id',
        'credits-list',
        'cast-without-order',
        'string-genre-id',
        'null-country',
        'string-runtime',
        'null-companies',
    ],
)
@pytest.mark.asyncio
async def test_malformed_details_raise_fixed_tmdb_error(change: dict[str, Any]) -> None:
    tmdb = tmdb_for({'/3/movie/424139': httpx.Response(200, json=MOVIE_DETAILS | change)}, [])

    with pytest.raises(TmdbError) as error:
        await tmdb.details('movie', 424139)
    assert str(error.value) == 'TMDB movie details have an unexpected shape'


@pytest.mark.parametrize(
    'credits',
    [
        {'cast': [], 'crew': [{'id': 1, 'name': 'Director', 'job': ['Director']}]},
        {'cast': [{'id': 1, 'name': 'Actor', 'order': 0}, {'id': 1, 'name': None, 'order': 1}], 'crew': []},
    ],
    ids=['crew-job-list', 'duplicate-cast-null-name'],
)
@pytest.mark.asyncio
async def test_movie_credit_entries_are_validated_before_selection(credits: dict[str, Any]) -> None:
    tmdb = tmdb_for({'/3/movie/424139': httpx.Response(200, json=MOVIE_DETAILS | {'credits': credits})}, [])

    with pytest.raises(TmdbError) as error:
        await tmdb.details('movie', 424139)
    assert str(error.value) == 'TMDB movie details have an unexpected shape'


@pytest.mark.parametrize(
    'created_by',
    [
        [{'id': 1, 'name': 'Creator'}, {'id': 1, 'name': None}],
        [{'id': True, 'name': 'Creator'}],
    ],
    ids=['duplicate-null-name', 'bool-id'],
)
@pytest.mark.asyncio
async def test_tv_creator_entries_are_validated_before_selection(created_by: list[dict[str, Any]]) -> None:
    tmdb = tmdb_for({'/3/tv/76331': httpx.Response(200, json=TV_DETAILS | {'created_by': created_by})}, [])

    with pytest.raises(TmdbError) as error:
        await tmdb.details('tv', 76331)
    assert str(error.value) == 'TMDB tv details have an unexpected shape'


@pytest.mark.asyncio
async def test_search_returns_validated_candidates_and_passes_the_year() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for(
        {
            '/3/search/movie': search(
                {'id': 1, 'title': 'Dune', 'original_title': 'Dune', 'release_date': '1984-12-14', 'vote_count': 2500},
                {'id': 2, 'title': 'Dune', 'original_title': '', 'release_date': '', 'vote_count': None},
            )
        },
        requests,
    )

    found = await tmdb.search('movie', 'Dune', 1984)

    assert [(c.tmdb_id, c.names, c.release, c.votes) for c in found] == [
        (1, frozenset({'Dune'}), '1984-12-14', 2500),
        (2, frozenset({'Dune'}), None, 0),
    ]
    assert requests[0].url.params['query'] == 'Dune'
    assert requests[0].url.params['primary_release_year'] == '1984'
    assert 'page' not in requests[0].url.params


@pytest.mark.asyncio
async def test_search_uses_tv_names_and_air_dates() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for(
        {'/3/search/tv': search({'id': 5, 'name': 'Dark', 'original_name': 'Dark', 'first_air_date': '2017-12-01'})},
        requests,
    )

    [found] = await tmdb.search('tv', 'Dark', 2017)

    assert (found.tmdb_id, found.release, found.votes) == (5, '2017-12-01', 0)
    assert requests[0].url.params['first_air_date_year'] == '2017'


@pytest.mark.parametrize(
    'entry',
    [
        {'id': '1', 'title': 'Dune'},
        {'id': True, 'title': 'Dune'},
        {'id': 1, 'title': ''},
        {'id': 1, 'title': 'Dune', 'release_date': '1984'},
        {'id': 1, 'title': 'Dune', 'vote_count': '9'},
        {'title': 'Dune'},
    ],
)
@pytest.mark.asyncio
async def test_search_rejects_malformed_entries(entry: dict[str, Any]) -> None:
    tmdb = tmdb_for({'/3/search/movie': search(entry)}, [])

    with pytest.raises(TmdbError, match='unexpected shape'):
        await tmdb.search('movie', 'Dune', None)


@pytest.mark.asyncio
async def test_client_requests_use_the_rate_limiter() -> None:
    calls: list[float] = []

    class CountingLimiter(RateLimiter):
        async def wait(self) -> None:
            calls.append(1)

    def handler(request: httpx.Request) -> httpx.Response:
        return search()

    client = TmdbClient(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)), CountingLimiter(1))

    await client.search('movie', 'A', None)
    await client.search('movie', 'B', None)

    assert len(calls) == 2


@pytest.mark.parametrize(
    'entry',
    [
        {'id': 1, 'title': 'Dune', 'release_date': '１９８４-１２-１４'},
        {'id': 1, 'title': 'Dune', 'vote_count': 9.0},
        {'id': 1, 'title': 'Dune', 'vote_count': True},
        {'id': 1.0, 'title': 'Dune'},
    ],
)
@pytest.mark.asyncio
async def test_search_rejects_non_ascii_dates_floats_and_bools(entry: dict[str, Any]) -> None:
    tmdb = tmdb_for({'/3/search/movie': search(entry)}, [])

    with pytest.raises(TmdbError, match='unexpected shape'):
        await tmdb.search('movie', 'Dune', None)


@pytest.mark.asyncio
async def test_requests_do_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://evil.example/x'})

    tmdb = TmdbClient(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(TmdbError, match='HTTP 302'):
        await tmdb.search('movie', 'Dune', None)

    assert len(requests) == 1
