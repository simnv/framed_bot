"""Find a game answer on TMDB and read the title data that the bot stores."""

import html
import math
import re
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from . import Kind
from .matching import Candidate
from .ratelimit import API_RATE, RateLimiter, Throttled, retry_after

API_BASE: Final = 'https://api.themoviedb.org/3'
TIMEOUT: Final = 10.0
CAST_LIMIT: Final = 10
YEAR_SUFFIX = re.compile(r'^(?P<name>.+?) \((?P<year>\d{4})\)$')
NOT_WORD = re.compile(r'[\W_]+')
ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
# TMDB image paths look like /abc123.jpg; anything else never reaches a URL.
IMAGE_PATH = re.compile(r'/[A-Za-z0-9_-]{1,64}\.(?:jpg|png)')
BACKDROP_LIMIT: Final = 12
STILL_SEASONS: Final = (1, 2)
STILL_LIMIT: Final = 10
# TV details have no imdb_id without external_ids.
APPEND: Final[dict[Kind, str]] = {'movie': 'credits', 'tv': 'credits,external_ids'}

type Role = Literal['director', 'creator', 'cast']


class TmdbError(RuntimeError):
    pass


class TmdbRateLimited(TmdbError, Throttled):
    """HTTP 429. The client pauses its limiter; the sync run stops."""


class TmdbNotFound(TmdbError):
    pass


class _Malformed(Exception):
    """A TMDB value has an unexpected type. Callers turn it into TmdbError with a fixed message."""


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


def _name(value: object) -> str:
    """A display name. TMDB returns some names with HTML entities, for example 'Sci-Fi &amp; Fantasy'."""
    return html.unescape(_str(value))


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


def _image_path(value: object) -> str:
    path = _str(value)
    if not IMAGE_PATH.fullmatch(path):
        raise _Malformed
    return path


def _vote_average(value: object) -> float:
    if value is None:
        return 0.0
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise _Malformed
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        raise _Malformed from None
    if not math.isfinite(number):
        raise _Malformed
    return number


def _pairs(value: object, key: str) -> list[tuple[int, str]]:
    return [(_int(item['id']), _name(item[key])) for item in map(_dict, _list(value))]


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
            credits.append(Credit(person_id, _name(person['name']), role, order))

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
            name = _name(data['title'])
            original_name = html.unescape(_optional_str(data.get('original_title')) or name)
            year, runtime = _year(data.get('release_date')), _optional_int(data.get('runtime')) or None
            imdb_id = _optional_str(data.get('imdb_id'))
        else:
            name = _name(data['name'])
            original_name = html.unescape(_optional_str(data.get('original_name')) or name)
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
    def __init__(self, token: str, client: httpx.AsyncClient, limiter: RateLimiter | None = None) -> None:
        self._headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        self._client = client
        self._limiter = limiter or RateLimiter(API_RATE)

    async def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        await self._limiter.wait()
        try:
            response = await self._client.get(
                f'{API_BASE}{path}', params=params, headers=self._headers, timeout=TIMEOUT, follow_redirects=False
            )
        except httpx.HTTPError as error:
            raise TmdbError(f'TMDB request {path} failed: {type(error).__name__}') from None
        if response.status_code == 429:
            self._limiter.throttled(retry_after(response.headers.get('Retry-After')))
            raise TmdbRateLimited('TMDB rate limit reached')
        if response.status_code == 404:
            raise TmdbNotFound(f'TMDB request {path} returned HTTP 404')
        if response.status_code != 200:
            raise TmdbError(f'TMDB request {path} returned HTTP {response.status_code}')
        try:
            data = response.json()
        except ValueError:
            raise TmdbError(f'TMDB request {path} returned invalid JSON') from None
        if not isinstance(data, dict):
            raise TmdbError(f'TMDB request {path} returned an unexpected body')
        return data

    async def search(self, kind: Kind, query: str, year: int | None) -> list[Candidate]:
        """Page 1 of /search/{kind}. Every entry is validated."""
        params: dict[str, str | int] = {'query': query}
        if year is not None:
            params['primary_release_year' if kind == 'movie' else 'first_air_date_year'] = year
        data = await self._get(f'/search/{kind}', params)
        name_key, original_key, date_key = (
            ('title', 'original_title', 'release_date')
            if kind == 'movie'
            else ('name', 'original_name', 'first_air_date')
        )
        try:
            candidates = []
            for entry in map(_dict, _list(data['results'])):
                names = {_str(entry[name_key])}
                if original := _optional_str(entry.get(original_key)):
                    names.add(original)
                release = _optional_str(entry.get(date_key))
                if release is not None and not ISO_DATE.fullmatch(release):
                    raise _Malformed
                votes = _optional_int(entry.get('vote_count')) or 0
                candidates.append(Candidate(_int(entry['id']), frozenset(names), release, votes))
            return candidates
        except _Malformed, KeyError:
            raise TmdbError(f'TMDB {kind} search has an unexpected shape') from None

    async def image_paths(self, kind: Kind, tmdb_id: int) -> list[str]:
        """Up to BACKDROP_LIMIT backdrops (any language and none), best voted first; for TV also episode stills."""
        data = await self._get(f'/{kind}/{tmdb_id}/images', {})
        try:
            backdrops = [_dict(entry) for entry in _list(data.get('backdrops', []))]
            # Validate every entry before the ranking drops any of them.
            rated = [(_vote_average(entry.get('vote_average')), _image_path(entry['file_path'])) for entry in backdrops]
            paths = [path for _, path in sorted(rated, key=lambda pair: -pair[0])[:BACKDROP_LIMIT]]
        except _Malformed, KeyError:
            raise TmdbError(f'TMDB {kind} images have an unexpected shape') from None
        if kind == 'tv':
            for season in STILL_SEASONS:
                try:
                    season_data = await self._get(f'/tv/{tmdb_id}/season/{season}', {})
                except TmdbNotFound:
                    continue
                try:
                    episodes = [_dict(entry) for entry in _list(season_data.get('episodes', []))]
                    stills = [entry.get('still_path') for entry in episodes]
                    paths += [_image_path(still) for still in stills if still is not None][:STILL_LIMIT]
                except _Malformed:
                    raise TmdbError('TMDB season has an unexpected shape') from None
        return list(dict.fromkeys(paths))

    async def details(self, kind: Kind, tmdb_id: int) -> TitleData:
        data = await self._get(f'/{kind}/{tmdb_id}', {'append_to_response': APPEND[kind]})
        return title_data(kind, data)
