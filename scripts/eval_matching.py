"""Offline evaluation of the round matching pipeline on Framed rounds with known TMDB ids. Not run in CI.

The evaluation runs the production code path (Resolver, TmdbClient.search and image_paths, TmdbVisualSource,
ImageFetcher, ProcessEmbedder) over a replay cache of HTTP responses keyed by method, URL and sorted query
parameters. A request that the cache does not hold fails the evaluation (exit status 2); a recorded 404 is
evidence and replays as 404. With --fetch the script requests the missing responses at the production rate limits
and records them (TMDB_API_KEY from the environment; it is never printed or stored).

Inputs (see docs/superpowers/specs/2026-11-01-better-matching-design.md):
  RESEARCH/A/ground_truth.csv, RESEARCH/A/data/{ts,po}/*.json  known ids and cached core.framed.wtf responses
  RESEARCH/C/data_ids.json                                     bundle image ids of every round
  RESEARCH/answers.csv                                         production matches before this change
  RESEARCH/R/                                                  the replay cache (index.json and bodies/)

Exit status 1 when the full pipeline is below 99% or breaks a row that is correct today (the spec gate).
The result without site ids is printed for information.
"""

import argparse
import asyncio
import collections
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answers.assets import MODES, current_mode_day, parse_asset  # noqa: E402
from answers.embedder import DIMENSIONS, ProcessEmbedder  # noqa: E402
from answers.images import FRAME_BASE, MAX_IMAGE_BYTES, TMDB_IMAGE_BASE, ImageFetcher  # noqa: E402
from answers.pipeline import Resolution, Resolver, TmdbVisualSource, sibling_years  # noqa: E402
from answers.ratelimit import API_RATE, CDN_RATE, SITE_RATE, RateLimiter  # noqa: E402
from answers.tmdb import TmdbClient  # noqa: E402

MODE_DIRS = {'titleshot': 'ts', 'poster': 'po'}
RECORDED = (200, 404)
KEPT_HEADERS = ('content-type', 'content-length', 'retry-after')
FAST = 1_000_000.0


class ReplayMiss(Exception):
    """The replay cache has no response for a request. Not an httpx error, so no production code catches it."""


def request_key(request: httpx.Request) -> str:
    query = urlencode(sorted(request.url.params.multi_items()))
    return f'{request.method} {request.url.scheme}://{request.url.host}{request.url.path}?{query}'


IMAGE_PREFIXES = (*(f'{base}/' for base in FRAME_BASE.values()), TMDB_IMAGE_BASE + '/')


def is_image_request(request: httpx.Request) -> bool:
    """The requests that ImageFetcher makes: frames on the game sites and images on the TMDB CDN."""
    return str(request.url).startswith(IMAGE_PREFIXES)


@dataclass
class ReplayCache:
    root: Path
    index: dict[str, dict[str, Any]]
    added: int = 0

    @classmethod
    def load(cls, root: Path) -> ReplayCache:
        file = root / 'index.json'
        return cls(root, json.loads(file.read_text()) if file.is_file() else {})

    def get(self, key: str) -> tuple[int, dict[str, str], bytes] | None:
        entry = self.index.get(key)
        if entry is None:
            return None
        headers = {'Content-Type': entry['type']} if entry['type'] else {}
        if entry.get('rejected'):
            # A response that production rejects (declared or streamed length over the bound) keeps no body; the
            # declared length makes ImageFetcher reject it the same way.
            headers['Content-Length'] = str(MAX_IMAGE_BYTES + 1)
        elif entry.get('length') is not None:
            headers['Content-Length'] = entry['length']
        return entry['status'], headers, (self.root / 'bodies' / entry['body']).read_bytes()

    def put(
        self,
        key: str,
        status: int,
        content_type: str | None,
        body: bytes,
        length: str | None = None,
        rejected: bool = False,
    ) -> None:
        digest = hashlib.sha256(body).hexdigest()
        (self.root / 'bodies').mkdir(parents=True, exist_ok=True)
        (self.root / 'bodies' / digest).write_bytes(body)
        entry: dict[str, Any] = {'status': status, 'type': content_type, 'body': digest}
        if length is not None:
            entry['length'] = length
        if rejected:
            entry['rejected'] = True
        self.index[key] = entry
        self.added += 1
        if self.added % 200 == 0:
            self.save()

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'index.json').write_text(json.dumps(self.index, sort_keys=True))


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, cache: ReplayCache, live: httpx.AsyncBaseTransport | None) -> None:
        self._cache = cache
        self._live = live
        self.misses: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        if self._cache.get(key) is not None:
            return self._replay(key, request)
        if self._live is None:
            self.misses.append(key)
            raise ReplayMiss(key)
        response = await self._live.handle_async_request(request)
        try:
            body, rejected = await self._read(request, response)
        finally:
            await response.aclose()
        status = response.status_code
        if status in RECORDED:
            self._cache.put(
                key,
                status,
                response.headers.get('Content-Type'),
                body,
                response.headers.get('Content-Length'),
                rejected,
            )
            return self._replay(key, request)
        kept = {name: value for name, value in response.headers.items() if name.lower() in KEPT_HEADERS}
        return httpx.Response(status, headers=kept, content=body, request=request)

    def _replay(self, key: str, request: httpx.Request) -> httpx.Response:
        found = self._cache.get(key)
        if found is None:
            raise ReplayMiss(key)
        status, headers, body = found
        return httpx.Response(status, headers=headers, content=body, request=request)

    @staticmethod
    async def _read(request: httpx.Request, response: httpx.Response) -> tuple[bytes, bool]:
        """Read the body as ImageFetcher does: a rejected image response is not read, an image over the bound stops."""
        if not is_image_request(request):
            return await response.aread(), False
        if response.status_code != 200 or not response.headers.get('Content-Type', '').startswith('image/'):
            return b'', True
        declared = response.headers.get('Content-Length', '')
        if declared.isascii() and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            return b'', True
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_IMAGE_BYTES:
                return b'', True
        return bytes(body), False


class CachingEmbedder:
    """Embed each distinct image once; the work runs in the production CLIP child process."""

    def __init__(self, embedder: ProcessEmbedder) -> None:
        self._embedder = embedder
        self._vectors: dict[bytes, np.ndarray | None] = {}

    async def embed(self, images: list[bytes]) -> np.ndarray:
        for image in dict.fromkeys(images):
            if image not in self._vectors:
                vectors = await self._embedder.embed([image])
                self._vectors[image] = vectors[0] if len(vectors) else None
        rows = [vector for vector in (self._vectors[image] for image in images) if vector is not None]
        return np.stack(rows) if rows else np.zeros((0, DIMENSIONS), dtype=np.float32)

    async def close(self) -> None:
        await self._embedder.close()


def site_ids(research: Path, today: date) -> dict[int, int]:
    """classic_day -> tmdb_id from the cached mode days up to today; days with two different ids are dropped."""
    found: dict[int, set[int]] = collections.defaultdict(set)
    for mode in MODES:
        for day in range(1, current_mode_day(mode, today) + 1):
            file = research / 'A/data' / MODE_DIRS[mode] / f'{mode}_{day}.json'
            if not file.is_file() or file.read_text() == 'null':
                continue
            asset = parse_asset(mode, day, json.loads(file.read_text()))
            if asset.classic_day is not None and asset.tmdb_id is not None:
                found[asset.classic_day].add(asset.tmdb_id)
    return {day: next(iter(ids)) for day, ids in found.items() if len(ids) == 1}


type Outcome = dict[int, tuple[int | None, str]]


def report(label: str, truth: dict[int, int], current: dict[int, int], outcome: Outcome) -> bool:
    good = regressions = fixed = 0
    statuses: collections.Counter[str] = collections.Counter()
    correct: collections.Counter[str] = collections.Counter()
    wrong = []
    for number, want in sorted(truth.items()):
        got, status = outcome[number]
        statuses[status] += 1
        correct[status] += got == want
        good += got == want
        regressions += current[number] == want and got != want
        fixed += current[number] != want and got == want
        if got != want:
            wrong.append((number, status, got, want))
    accuracy = good / len(truth)
    print(f'{label}: {good}/{len(truth)} = {accuracy:.2%}; regressions {regressions}; fixed {fixed}')
    for status, count in statuses.most_common():
        print(f'  {status:<15} coverage {count / len(truth):6.2%} ({count})  accuracy {correct[status] / count:7.2%}')
    for number, status, got, want in wrong:
        print(f'  wrong: round {number} status {status} got {got} want {want}')
    return accuracy >= 0.99 and regressions == 0


async def evaluate(args: argparse.Namespace) -> int:
    research: Path = args.research
    rows = [r for r in csv.DictReader((research / 'answers.csv').open()) if r['game'] == 'framed']
    truth = {
        int(r['round']): int(r['tmdb_id'])
        for r in csv.DictReader((research / 'A/ground_truth.csv').open())
        if r['game'] == 'framed'
    }
    titles = {int(r['round']): r['source_title'] for r in rows}
    current = {int(r['round']): int(r['tmdb_id'] or 0) for r in rows}
    entries = json.loads((research / 'C/data_ids.json').read_text())['framed']
    image_ids = [int(float(raw)) for _, raw in entries]
    siblings = sibling_years(titles.values())
    site = site_ids(research, args.today)
    cache = ReplayCache.load(research / 'R')
    rate = (SITE_RATE, API_RATE, CDN_RATE) if args.fetch else (FAST, FAST, FAST)
    site_limiter, api_limiter, cdn_limiter = (RateLimiter(value) for value in rate)
    transport = ReplayTransport(cache, httpx.AsyncHTTPTransport() if args.fetch else None)
    token = os.environ.get('TMDB_API_KEY', '') if args.fetch else 'offline'
    if not token:
        raise SystemExit('--fetch needs TMDB_API_KEY in the environment')
    missing: list[int] = []
    resolved: dict[int, Resolution | None] = {}
    async with httpx.AsyncClient(transport=transport) as client:
        tmdb = TmdbClient(token, client, api_limiter)
        visual = TmdbVisualSource(ImageFetcher(client, site_limiter, cdn_limiter), tmdb) if args.model else None
        embedder = CachingEmbedder(ProcessEmbedder(args.model, threads=args.threads)) if args.model else None
        resolver = Resolver(tmdb.search, visual, embedder)
        try:
            for number in sorted(truth):
                image_id = image_ids[number - 1] if number <= len(image_ids) else None
                try:
                    resolved[number] = await resolver.resolve('framed', number, titles[number], siblings, image_id)
                except ReplayMiss:
                    missing.append(number)
        finally:
            await resolver.close()
            if cache.added:
                cache.save()
    print('Text rules (agent B): out-of-sample; B was written before the ground truth existed.')
    print('Visual gap, model and preprocessing: in-sample; they were chosen on these rounds.')
    if missing:
        print(f'Replay cache misses: {len(transport.misses)} requests in {len(missing)} rounds, for example:')
        for key in transport.misses[:5]:
            print(f'  {key}')
        return 2

    def outcome(use_site: bool) -> Outcome:
        result: Outcome = {}
        for number in truth:
            found = resolved[number]
            if use_site and number in site:
                result[number] = (site[number], 'site')
            else:
                result[number] = (None, 'unmatched') if found is None else (found.tmdb_id, found.status)
        return result

    passed = report(
        'full pipeline (the spec gate; site ids are correct by construction)', truth, current, outcome(True)
    )
    if not report('without site ids (information, not a gate)', truth, current, outcome(False)):
        print('  note: without site ids the result is below 99% or has a regression')
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--research', type=Path, required=True, help='for example /tmp/framed_match_research')
    parser.add_argument('--model', type=Path, help='CLIP vision ONNX file; without it the visual step is off')
    parser.add_argument('--fetch', action='store_true', help='request and record the responses the cache lacks')
    parser.add_argument('--today', type=date.fromisoformat, default=date(2026, 11, 1), help='the asset day limit')
    parser.add_argument('--threads', type=int, default=1)
    return asyncio.run(evaluate(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
