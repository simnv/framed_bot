import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'eval_matching.py'
spec = importlib.util.spec_from_file_location('eval_matching', SCRIPT)
assert spec is not None and spec.loader is not None
eval_matching = importlib.util.module_from_spec(spec)
sys.modules['eval_matching'] = eval_matching
spec.loader.exec_module(eval_matching)


def request(url: str = 'https://api.example/search/movie?query=a b&year=1999&alpha=1') -> httpx.Request:
    return httpx.Request('GET', url)


def test_request_key_sorts_and_encodes_parameters() -> None:
    assert eval_matching.request_key(request()) == ('GET https://api.example/search/movie?alpha=1&query=a+b&year=1999')


def test_cache_round_trip_keeps_status_type_and_body(tmp_path: Path) -> None:
    cache = eval_matching.ReplayCache.load(tmp_path)
    cache.put('GET k', 404, 'application/json', b'{"x":1}')
    cache.save()
    again = eval_matching.ReplayCache.load(tmp_path)
    assert again.get('GET k') == (404, {'Content-Type': 'application/json'}, b'{"x":1}')
    assert again.get('GET other') is None


@pytest.mark.asyncio
async def test_replay_returns_recorded_404_and_raises_on_a_miss(tmp_path: Path) -> None:
    cache = eval_matching.ReplayCache.load(tmp_path)
    cache.put(eval_matching.request_key(request()), 404, None, b'')
    transport = eval_matching.ReplayTransport(cache, None)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get(str(request().url))).status_code == 404
        with pytest.raises(eval_matching.ReplayMiss):
            await client.get('https://api.example/missing')
    assert transport.misses == ['GET https://api.example/missing?']


@pytest.mark.asyncio
async def test_live_responses_are_recorded_only_for_200_and_404(tmp_path: Path) -> None:
    def live(req: httpx.Request) -> httpx.Response:
        return httpx.Response(int(req.url.params['status']), content=b'body')

    cache = eval_matching.ReplayCache.load(tmp_path)
    transport = eval_matching.ReplayTransport(cache, httpx.MockTransport(live))
    async with httpx.AsyncClient(transport=transport) as client:
        for status in (200, 404, 429, 500):
            assert (await client.get(f'https://x.example/a?status={status}')).status_code == status
    assert sorted(entry['status'] for entry in cache.index.values()) == [200, 404]


class CountingEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[bytes]] = []

    async def embed(self, images: list[bytes]) -> np.ndarray:
        self.calls.append(images)
        return np.array([[float(image[0]), 0.0] for image in images], dtype=np.float32)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_caching_embedder_embeds_each_image_once_and_keeps_order() -> None:
    inner = CountingEmbedder()
    embedder: Any = eval_matching.CachingEmbedder(inner)
    first = await embedder.embed([b'\x01', b'\x02', b'\x01'])
    second = await embedder.embed([b'\x02', b'\x03'])
    assert first[:, 0].tolist() == [1.0, 2.0, 1.0]
    assert second[:, 0].tolist() == [2.0, 3.0]
    assert inner.calls == [[b'\x01'], [b'\x02'], [b'\x03']]


def test_site_ids_drop_days_with_two_different_ids(tmp_path: Path) -> None:
    mode = eval_matching.MODES[0]
    assert mode == 'titleshot'
    folder = tmp_path / 'A/data/ts'
    folder.mkdir(parents=True)
    (folder / 'titleshot_1.json').write_text('null')
    result = eval_matching.site_ids(tmp_path, date(2026, 11, 1))
    assert result == {}


def test_report_gate_needs_99_percent_and_no_regression(capsys: pytest.CaptureFixture[str]) -> None:
    truth = {number: number for number in range(1, 101)}
    current = dict(truth)
    perfect = {number: (number, 'auto') for number in truth}
    assert eval_matching.report('all', truth, current, perfect)
    broken = dict(perfect)
    broken[1] = (999, 'auto')
    assert not eval_matching.report('one wrong', truth, current, broken)
    out = capsys.readouterr().out
    assert 'regressions 1' in out
    assert json.dumps('wrong: round 1 status auto got 999 want 1')[1:-1] in out
