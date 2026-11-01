"""Worst-case CLIP memory under the bot's 1 GB limit. Runs inside the bot image; prints one JSON line.

It imports the bot (its real import overhead), then runs a Framed and an Episode tie-break at the same time with
the largest accepted inputs: 10 frames and 8 candidates x 32 images per game, each image at the pixel limit and
close to the byte limit, plus a 10000 x 1 PNG that must be rejected. The first request includes the cold model load.
"""

import asyncio
import io
import json
import os
import resource
import threading
from pathlib import Path

import numpy as np
from PIL import Image

import main  # noqa: F401 - the bot's import overhead belongs to the parent process
from answers import Game
from answers.clip_model import DEFAULT_MODEL_PATH
from answers.embedder import MAX_SIDE, ProcessEmbedder
from answers.images import FRAME_COUNT, MAX_CANDIDATE_IMAGES, MAX_IMAGE_BYTES
from answers.pipeline import LOW_CONFIDENCE_POOL


def largest_image(seed: int) -> bytes:
    """A MAX_SIDE x MAX_SIDE noisy JPEG (the most pixels accepted) at the best quality under the byte limit."""
    rng = np.random.default_rng(seed)
    gradient = np.linspace(0, 200, MAX_SIDE).astype(np.uint8)[None, :, None]
    pixels = gradient + rng.integers(0, 40, (MAX_SIDE, MAX_SIDE, 3), dtype=np.uint8)
    for quality in range(90, 30, -3):
        buffer = io.BytesIO()
        Image.fromarray(pixels).save(buffer, 'JPEG', quality=quality)
        if len(buffer.getvalue()) <= MAX_IMAGE_BYTES:
            return buffer.getvalue()
    raise AssertionError('no quality fits the byte limit')


def narrow_png() -> bytes:
    buffer = io.BytesIO()
    Image.new('L', (10_000, 1)).save(buffer, 'PNG')
    return buffer.getvalue()


def cgroup_peak() -> int | None:
    for name in ('/sys/fs/cgroup/memory.peak', '/sys/fs/cgroup/memory/memory.max_usage_in_bytes'):
        if os.path.exists(name):
            return int(Path(name).read_text().split()[0])
    return None


class AnonSampler:
    """The peak of anonymous memory in the container cgroup (page cache excluded), sampled every 20 ms."""

    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        stat = Path('/sys/fs/cgroup/memory.stat')
        while not self._stop.wait(0.02):
            if stat.exists():
                for line in stat.read_text().splitlines():
                    if line.startswith('anon '):
                        self.peak = max(self.peak, int(line.split()[1]))

    def __enter__(self) -> AnonSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


async def tie_break(embedder: ProcessEmbedder, game: Game, seed: int) -> int:
    frames = [largest_image(seed + n) for n in range(FRAME_COUNT[game])]
    vectors = len(await embedder.embed([*frames, narrow_png()]))
    for candidate in range(LOW_CONFIDENCE_POOL):
        # Downloads arrive one candidate at a time, like TmdbVisualSource.candidate_images.
        images = [largest_image(seed + 100 * candidate + n) for n in range(MAX_CANDIDATE_IMAGES)]
        vectors += len(await embedder.embed(images))
    return vectors


async def run() -> dict[str, object]:
    embedder = ProcessEmbedder(Path(DEFAULT_MODEL_PATH))
    with AnonSampler() as sampler:
        framed, episode = await asyncio.gather(tie_break(embedder, 'framed', 1), tie_break(embedder, 'episode', 5000))
        await embedder.close()
    return {
        'vectors': [framed, episode],
        'image_bytes': len(largest_image(0)),
        'parent_peak_mb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
        'child_peak_mb': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024,
        'container_peak_mb': None if (peak := cgroup_peak()) is None else peak // 2**20,
        'container_anon_peak_mb': sampler.peak // 2**20,
    }


if __name__ == '__main__':
    print(json.dumps(asyncio.run(run()), sort_keys=True))
