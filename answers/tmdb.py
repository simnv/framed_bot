"""Find a game answer on TMDB and read the title data that the bot stores."""

import re
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from . import Kind

API_BASE: Final = 'https://api.themoviedb.org/3'
TIMEOUT: Final = 10.0
CAST_LIMIT: Final = 10
YEAR_SUFFIX = re.compile(r'^(?P<name>.+?) \((?P<year>\d{4})\)$')
NOT_WORD = re.compile(r'[\W_]+')
# TV details have no imdb_id without external_ids.
APPEND: Final[dict[Kind, str]] = {'movie': 'credits', 'tv': 'credits,external_ids'}

type Role = Literal['director', 'creator', 'cast']


class TmdbError(RuntimeError):
    pass


class TmdbRateLimited(TmdbError):
    pass


class _Malformed(Exception):
    """A TMDB value has an unexpected type. Callers turn it into TmdbError with a fixed message."""


@dataclass(frozen=True, slots=True)
class Match:
    tmdb_id: int
    status: Literal['auto', 'low_confidence']


@dataclass(frozen=True, slots=True)
class Credit:
    person_id: int
    name: str
    role: Role
    order: int


@dataclass(frozen=True, slots=True)
class TitleData:
    tmdb_id: int
    kind: Kind
    name: str
    original_name: str
    year: int | None
    runtime: int | None
    countries: list[str]
    imdb_id: str | None
    genres: list[tuple[int, str]]
    companies: list[tuple[int, str]]
    credits: list[Credit]
    raw: dict[str, Any]


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Malformed
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise _Malformed
    return value


def _int(value: object) -> int:
    # bool is a subclass of int, but it is never a TMDB id or number.
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Malformed
    return value


def _str(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _Malformed
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _optional_str(value: object) -> str | None:
    if value is None or value == '':
        return None
    return _str(value)


def split_year(source_title: str) -> tuple[str, int | None]:
    """'Halloween (2018)' -> ('Halloween', 2018)."""
    found = YEAR_SUFFIX.match(source_title)
    if found is None:
        return source_title, None
    return found['name'], int(found['year'])


def normalize(name: str) -> str:
    return NOT_WORD.sub(' ', name.casefold()).strip()


def _year(value: object) -> int | None:
    date = _optional_str(value)
    return int(date[:4]) if date and date[:4].isdigit() else None


def _pairs(value: object, key: str) -> list[tuple[int, str]]:
    return [(_int(item['id']), _str(item[key])) for item in map(_dict, _list(value))]


def _people(value: object) -> list[dict[str, Any]]:
    """Validate every credit entry before any filtering or deduplication."""
    people = [_dict(person) for person in _list(value)]
    for person in people:
        _int(person['id'])
        _str(person['name'])
    return people


def _credits(data: dict[str, Any], kind: Kind) -> list[Credit]:
    credits: list[Credit] = []
    seen: set[tuple[int, Role]] = set()

    def add(person: dict[str, Any], role: Role, order: int) -> None:
        # One row for each person and role: TMDB lists a person once for each job or character.
        person_id = _int(person['id'])
        if (person_id, role) not in seen:
            seen.add((person_id, role))
            credits.append(Credit(person_id, _str(person['name']), role, order))

    people = _dict(data.get('credits', {}))
    crew = _people(people.get('crew', []))
    for person in crew:
        _optional_str(person.get('job'))
    directors = [person for person in crew if person.get('job') == 'Director']
    for order, person in enumerate(directors):
        add(person, 'director', order)
    if kind == 'tv':
        for order, person in enumerate(_people(data.get('created_by', []))):
            add(person, 'creator', order)
    cast = sorted(_people(people.get('cast', [])), key=lambda person: _int(person['order']))
    for person in cast[:CAST_LIMIT]:
        add(person, 'cast', person['order'])
    return credits


def title_data(kind: Kind, data: dict[str, Any]) -> TitleData:
    """Map a TMDB details response. Raise TmdbError when a field has an unexpected type."""
    try:
        if kind == 'movie':
            name = _str(data['title'])
            original_name = _optional_str(data.get('original_title')) or name
            year, runtime = _year(data.get('release_date')), _optional_int(data.get('runtime')) or None
            imdb_id = _optional_str(data.get('imdb_id'))
        else:
            name = _str(data['name'])
            original_name = _optional_str(data.get('original_name')) or name
            run_times = [_int(minutes) for minutes in _list(data.get('episode_run_time', []))]
            last_episode = data.get('last_episode_to_air')
            last_runtime = None if last_episode is None else _optional_int(_dict(last_episode).get('runtime'))
            year, runtime = _year(data.get('first_air_date')), run_times[0] if run_times else last_runtime
            imdb_id = _optional_str(_dict(data.get('external_ids', {})).get('imdb_id'))
        return TitleData(
            tmdb_id=_int(data['id']),
            kind=kind,
            name=name,
            original_name=original_name,
            year=year,
            runtime=runtime,
            countries=[
                _str(country['iso_3166_1']) for country in map(_dict, _list(data.get('production_countries', [])))
            ],
            imdb_id=imdb_id,
            genres=_pairs(data.get('genres', []), 'name'),
            companies=_pairs(data.get('production_companies', []), 'name'),
            credits=_credits(data, kind),
            raw=data,
        )
    except _Malformed, KeyError:
        raise TmdbError(f'TMDB {kind} details have an unexpected shape') from None


class TmdbClient:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        self._client = client

    async def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f'{API_BASE}{path}', params=params, headers=self._headers, timeout=TIMEOUT
            )
        except httpx.HTTPError as error:
            raise TmdbError(f'TMDB request {path} failed: {type(error).__name__}') from None
        if response.status_code == 429:
            raise TmdbRateLimited('TMDB rate limit reached')
        if response.status_code != 200:
            raise TmdbError(f'TMDB request {path} returned HTTP {response.status_code}')
        try:
            data = response.json()
        except ValueError:
            raise TmdbError(f'TMDB request {path} returned invalid JSON') from None
        if not isinstance(data, dict):
            raise TmdbError(f'TMDB request {path} returned an unexpected body')
        return data

    async def find(self, kind: Kind, source_title: str) -> Match | None:
        """Return the first result, or None when the result list is empty (unmatched)."""
        query, year = split_year(source_title)
        params: dict[str, str | int] = {'query': query}
        if year is not None:
            params['primary_release_year' if kind == 'movie' else 'first_air_date_year'] = year
        data = await self._get(f'/search/{kind}', params)
        try:
            results = _list(data['results'])
            if not results:
                return None
            first = _dict(results[0])
            name_key, original_key = ('title', 'original_title') if kind == 'movie' else ('name', 'original_name')
            names = {_str(first[name_key]), _optional_str(first.get(original_key))}
            tmdb_id = _int(first['id'])
        except _Malformed, KeyError:
            raise TmdbError(f'TMDB {kind} search has an unexpected shape') from None
        exact = normalize(query) in {normalize(name) for name in names if name}
        return Match(tmdb_id, 'auto' if exact else 'low_confidence')

    async def details(self, kind: Kind, tmdb_id: int) -> TitleData:
        data = await self._get(f'/{kind}/{tmdb_id}', {'append_to_response': APPEND[kind]})
        return title_data(kind, data)
