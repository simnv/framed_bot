from typing import Any

import httpx
import pytest

from answers.tmdb import Credit, Match, TitleData, TmdbClient, TmdbError, TmdbRateLimited, normalize, split_year

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
async def test_find_movie_uses_bearer_token_and_primary_release_year() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for(
        {'/3/search/movie': search({'id': 424139, 'title': 'Halloween', 'original_title': 'Halloween'})}, requests
    )

    match = await tmdb.find('movie', 'Halloween (2018)')

    assert match == Match(424139, 'auto')
    [request] = requests
    assert request.url.host == 'api.themoviedb.org'
    assert request.headers['Authorization'] == f'Bearer {TOKEN}'
    assert dict(request.url.params) == {'query': 'Halloween', 'primary_release_year': '2018'}


@pytest.mark.asyncio
async def test_find_movie_without_year_sends_query_only() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for({'/3/search/movie': search({'id': 278, 'title': 'The Shawshank Redemption'})}, requests)

    assert await tmdb.find('movie', 'The Shawshank Redemption') == Match(278, 'auto')
    assert dict(requests[0].url.params) == {'query': 'The Shawshank Redemption'}


@pytest.mark.asyncio
async def test_find_tv_uses_tv_search() -> None:
    requests: list[httpx.Request] = []
    tmdb = tmdb_for(
        {'/3/search/tv': search({'id': 76331, 'name': 'Succession', 'original_name': 'Succession'})}, requests
    )

    assert await tmdb.find('tv', 'Succession') == Match(76331, 'auto')
    assert dict(requests[0].url.params) == {'query': 'Succession'}


@pytest.mark.asyncio
async def test_find_accepts_original_name_match() -> None:
    tmdb = tmdb_for({'/3/search/movie': search({'id': 129, 'title': 'Spirited Away', 'original_title': 'Amélie'})}, [])

    assert await tmdb.find('movie', 'Amélie') == Match(129, 'auto')


@pytest.mark.asyncio
async def test_find_marks_different_first_result_as_low_confidence() -> None:
    tmdb = tmdb_for(
        {
            '/3/search/movie': search(
                {'id': 1, 'title': 'Halloween Kills', 'original_title': 'Halloween Kills'},
                {'id': 424139, 'title': 'Halloween', 'original_title': 'Halloween'},
            )
        },
        [],
    )

    assert await tmdb.find('movie', 'Halloween') == Match(1, 'low_confidence')


@pytest.mark.asyncio
async def test_find_returns_none_without_results() -> None:
    tmdb = tmdb_for({'/3/search/movie': search()}, [])

    assert await tmdb.find('movie', 'Not A Film') is None


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


@pytest.mark.asyncio
async def test_rate_limit_raises_tmdb_rate_limited() -> None:
    tmdb = tmdb_for({'/3/search/movie': httpx.Response(429)}, [])

    with pytest.raises(TmdbRateLimited):
        await tmdb.find('movie', 'Halloween')


@pytest.mark.parametrize(
    'response',
    [httpx.Response(500), httpx.Response(200, text='not json'), httpx.Response(200, json=[1])],
    ids=['server-error', 'invalid-json', 'not-an-object'],
)
@pytest.mark.asyncio
async def test_bad_responses_raise_tmdb_error(response: httpx.Response) -> None:
    tmdb = tmdb_for({'/3/search/movie': response}, [])

    with pytest.raises(TmdbError):
        await tmdb.find('movie', 'Halloween')


@pytest.mark.asyncio
async def test_timeout_raises_tmdb_error_without_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('timed out', request=request)

    tmdb = TmdbClient(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(TmdbError) as error:
        await tmdb.find('movie', 'Halloween')
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
        await tmdb.find('movie', 'Halloween')
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
