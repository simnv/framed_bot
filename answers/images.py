"""Download the frames of a round and TMDB images of its candidates for the visual tie-break."""

from typing import Final

import httpx

from . import Game
from .ratelimit import RateLimiter, Throttled, retry_after

FRAME_COUNT: Final[dict[Game, int]] = {'framed': 6, 'episode': 10}
FRAME_BASE: Final[dict[Game, str]] = {'framed': 'https://framed.wtf/images', 'episode': 'https://episode.wtf/images'}
# Framed image ids 1..32 return 404.
FIRST_FRAMED_IMAGE: Final = 33
TMDB_IMAGE_BASE: Final = 'https://image.tmdb.org/t/p/w300'
# Frames are about 30 KB and w300 backdrops about 20 KB; a larger body is not a usable image.
MAX_IMAGE_BYTES: Final = 1_000_000
HEADERS: Final = {'User-Agent': 'framed-bot/1.0'}
TIMEOUT: Final = 20.0
# 12 backdrops and 2 x 10 TV stills.
MAX_CANDIDATE_IMAGES: Final = 32


class ImageError(RuntimeError):
    pass


def has_frames(game: Game, image_id: int) -> bool:
    return game == 'episode' or image_id >= FIRST_FRAMED_IMAGE


def frame_urls(game: Game, image_id: int) -> list[str]:
    return [f'{FRAME_BASE[game]}/{image_id}/{number:03d}.jpeg' for number in range(1, FRAME_COUNT[game] + 1)]


class ImageFetcher:
    """Frames come from framed.wtf or episode.wtf (site_limiter), TMDB images from the CDN (cdn_limiter)."""

    def __init__(self, client: httpx.AsyncClient, site_limiter: RateLimiter, cdn_limiter: RateLimiter) -> None:
        self._client = client
        self._site = site_limiter
        self._cdn = cdn_limiter

    async def _image(self, url: str, limiter: RateLimiter) -> bytes | None:
        """Return the image, or None when the server has no usable image at the URL. HTTP 429 raises Throttled."""
        await limiter.wait()
        try:
            async with self._client.stream(
                'GET', url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=False
            ) as response:
                if response.status_code == 429:
                    limiter.throttled(retry_after(response.headers.get('Retry-After')))
                    raise Throttled('Image host returned HTTP 429')
                if response.status_code != 200 or not response.headers.get('Content-Type', '').startswith('image/'):
                    return None
                declared = response.headers.get('Content-Length', '')
                if declared.isascii() and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_IMAGE_BYTES:
                        return None
                return bytes(body)
        except httpx.HTTPError as error:
            raise ImageError(f'Image request failed: {type(error).__name__}') from None

    async def frames(self, game: Game, image_id: int) -> list[bytes]:
        if not has_frames(game, image_id):
            return []
        images = [await self._image(url, self._site) for url in frame_urls(game, image_id)]
        return [image for image in images if image is not None]

    async def tmdb_images(self, paths: list[str]) -> list[bytes]:
        images = [await self._image(TMDB_IMAGE_BASE + path, self._cdn) for path in paths[:MAX_CANDIDATE_IMAGES]]
        return [image for image in images if image is not None]
