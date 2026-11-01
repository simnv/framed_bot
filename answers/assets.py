"""Read the TMDB id of Framed rounds from the titleshot and poster mode challenges on core.framed.wtf."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal, TypeIs

import httpx

from .ratelimit import RateLimiter, Throttled, retry_after

type Mode = Literal['titleshot', 'poster']

MODES: Final[tuple[Mode, ...]] = ('titleshot', 'poster')
# Day 1 of each mode, from the framed.wtf _app bundle: {titleshot:"2025-06-05",poster:"2025-07-25"}.
MODE_FIRST_DAY: Final[dict[Mode, date]] = {'titleshot': date(2025, 6, 5), 'poster': date(2025, 7, 25)}
CORE: Final = 'https://core.framed.wtf'
HEADERS: Final = {'User-Agent': 'framed-bot/1.0', 'Accept': 'application/json'}
TIMEOUT: Final = 20.0
IMDB_ID = re.compile(r'tt[0-9]{1,10}')


class AssetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Asset:
    mode: Mode
    day: int
    classic_day: int | None
    tmdb_id: int | None
    imdb_id: str | None


def current_mode_day(mode: Mode, today: date) -> int:
    """The mode day on a UTC date, as the bundle counts it (day 1 is the first day). 0 before day 1."""
    first = MODE_FIRST_DAY[mode]
    return (today - first).days + 1 if today >= first else 0


def _is_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive(value: object) -> int | None:
    if value is None:
        return None
    if not _is_int(value) or value < 1:
        raise AssetError('Asset id has an unexpected type')
    return value


def parse_asset(mode: Mode, day: int, data: object) -> Asset:
    """Map a challenge response. Raise AssetError when a field has an unexpected type."""
    if not isinstance(data, dict):
        raise AssetError(f'{mode} day {day} has an unexpected shape')
    received = data.get('day')
    if not _is_int(received) or received != day or not isinstance(data.get('asset'), dict):
        raise AssetError(f'{mode} day {day} has an unexpected shape')
    asset: dict[str, Any] = data['asset']
    imdb_id = asset.get('imdb_id')
    if imdb_id is not None and (not isinstance(imdb_id, str) or not IMDB_ID.fullmatch(imdb_id)):
        raise AssetError(f'{mode} day {day} has an unexpected imdb_id')
    if not isinstance(asset.get('type'), str):
        raise AssetError(f'{mode} day {day} has an unexpected asset type')
    tmdb_id = _positive(asset.get('external_id'))
    # A TV asset would carry a TV id; Framed rounds are movies, so keep only movie ids.
    return Asset(mode, day, _positive(asset.get('classic_day')), tmdb_id if asset['type'] == 'MOVIE' else None, imdb_id)


async def fetch_asset(client: httpx.AsyncClient, limiter: RateLimiter, mode: Mode, day: int) -> Asset:
    """Return the asset of one day. A 404 gives an Asset with NULL ids, so the day is not fetched again.

    HTTP 429 pauses the site group and raises Throttled; other failures raise AssetError.
    """
    await limiter.wait()
    try:
        response = await client.get(
            f'{CORE}/{mode}/challenges/day/{day}', headers=HEADERS, timeout=TIMEOUT, follow_redirects=False
        )
    except httpx.HTTPError as error:
        raise AssetError(f'{mode} day {day} request failed: {type(error).__name__}') from None
    if response.status_code == 404:
        return Asset(mode, day, None, None, None)
    if response.status_code == 429:
        limiter.throttled(retry_after(response.headers.get('Retry-After')))
        raise Throttled(f'{mode} day {day} returned HTTP 429')
    if response.status_code != 200:
        raise AssetError(f'{mode} day {day} returned HTTP {response.status_code}')
    try:
        data = response.json()
    except ValueError:
        raise AssetError(f'{mode} day {day} returned invalid JSON') from None
    return parse_asset(mode, day, data)


class AssetFetcher:
    def __init__(self, client: httpx.AsyncClient, limiter: RateLimiter) -> None:
        self._client = client
        self._limiter = limiter

    async def fetch(self, mode: Mode, day: int) -> Asset:
        return await fetch_asset(self._client, self._limiter, mode, day)
