"""Read the answer list of each game from its public Next.js bundles.

The bundle format is not a contract: every unexpected shape raises ScrapeError.
"""

import re
from typing import Final

import httpx

from . import Game

SITES: Final[dict[Game, str]] = {'framed': 'https://framed.wtf', 'episode': 'https://episode.wtf'}
HEADERS: Final = {'User-Agent': 'framed-bot/1.0'}
MIN_TITLES: Final = 100
TIMEOUT: Final = 30.0

APP_SCRIPT = re.compile(r'src="(/_next/static/chunks/pages/_app-[0-9a-f]+\.js)"')
WEBPACK_SCRIPT = re.compile(r'src="(/_next/static/chunks/webpack-[0-9a-f]+\.js)"')
# The webpack runtime maps lazy chunk ids to hashes: "static/chunks/"+e+"."+{87:"0111d1a5ef9290c6"}[e]+".js"
CHUNK_MAP = re.compile(r'"static/chunks/"\+\w+\+"\."\+\{([^{}]+)\}\[\w+\]\+"\.js"')
CHUNK_ENTRY = re.compile(r'(\d+):"([0-9a-f]+)"')
_ENTRY = r'\{title:"(?:[^"\\]|\\.)*",id:[\d.e]+(?:,occurrences?:\d+)?\}'
TITLE_ARRAY = re.compile(r'=\[(' + _ENTRY + r'(?:,' + _ENTRY + r')*)\]')
TITLE_ENTRY = re.compile(r'\{title:"((?:[^"\\]|\\.)*)",id:([\d.e]+)')
JS_ESCAPE = re.compile(r'\\(x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|.)', re.DOTALL)
SIMPLE_ESCAPES: Final = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}


class ScrapeError(RuntimeError):
    pass


def _unescape_char(match: re.Match[str]) -> str:
    code = match.group(1)
    if code.startswith('u{'):
        return chr(int(code[2:-1], 16))
    if len(code) == 5 and code[0] == 'u' or len(code) == 3 and code[0] == 'x':
        return chr(int(code[1:], 16))
    return SIMPLE_ESCAPES.get(code, code)


def js_unescape(raw: str) -> str:
    try:
        text = JS_ESCAPE.sub(_unescape_char, raw)
        # \uD83C\uDFAC pairs become two surrogates; join them into one character.
        return text.encode('utf-16', 'surrogatepass').decode('utf-16')
    except ValueError:  # UnicodeError is a ValueError; chr() raises ValueError too
        raise ScrapeError('Malformed escape in answer title') from None


def _round_id(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ScrapeError('Malformed id in answer array') from None


def find_titles(script: str) -> list[str] | None:
    """Return the answer array of a script, or None when the script has no answer array."""
    candidates = []
    for array in TITLE_ARRAY.finditer(script):
        entries = TITLE_ENTRY.findall(array.group(1))
        # Answer arrays start at round 1; mode arrays (titleshot, one-frame) start elsewhere.
        if _round_id(entries[0][1]) == 1:
            candidates.append([js_unescape(title) for title, _ in entries])
    if not candidates:
        return None
    if len(candidates) > 1:
        raise ScrapeError(f'Found {len(candidates)} answer arrays in one script')
    return candidates[0]


def extract_titles(script: str) -> list[str]:
    titles = find_titles(script)
    if titles is None:
        raise ScrapeError('Answer array not found')
    return checked(titles)


def checked(titles: list[str]) -> list[str]:
    if len(titles) < MIN_TITLES:
        raise ScrapeError(f'Answer array has only {len(titles)} entries')
    return titles


def chunk_urls(base: str, runtime: str) -> list[str]:
    chunk_map = CHUNK_MAP.search(runtime)
    if chunk_map is None:
        raise ScrapeError('Lazy chunk map not found in the webpack runtime')
    return [
        f'{base}/_next/static/chunks/{chunk}.{digest}.js' for chunk, digest in CHUNK_ENTRY.findall(chunk_map.group(1))
    ]


def script_path(page: str, pattern: re.Pattern[str], name: str) -> str:
    found = pattern.search(page)
    if found is None:
        raise ScrapeError(f'{name} script not found on the home page')
    return found.group(1)


async def _get(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


async def fetch_titles(game: Game, client: httpx.AsyncClient) -> list[str]:
    """Return the answers of all rounds in the bundle: round N is element N-1. Some rounds are in the future."""
    base = SITES[game]
    page = await _get(client, f'{base}/')
    if game == 'framed':
        return extract_titles(await _get(client, base + script_path(page, APP_SCRIPT, 'App bundle')))
    runtime = await _get(client, base + script_path(page, WEBPACK_SCRIPT, 'Webpack runtime'))
    candidates = []
    for url in chunk_urls(base, runtime):
        titles = find_titles(await _get(client, url))
        if titles is not None:
            candidates.append(titles)
    # Several chunks may hold the same array (the site shares it); differing arrays are ambiguous.
    if not candidates or any(titles != candidates[0] for titles in candidates):
        raise ScrapeError('Lazy chunks lack one answer array or hold differing ones')
    return checked(candidates[0])
