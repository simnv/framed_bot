"""Opt-in check of the real CLIP model: FRAMED_CLIP_TEST=1 CLIP_MODEL_PATH=/path/vision_model.onnx."""

import hashlib
import io
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from answers.clip_model import DEFAULT_MODEL_PATH, MODEL_SHA256
from answers.embedder import DIMENSIONS, ProcessEmbedder

pytestmark = pytest.mark.skipif(os.environ.get('FRAMED_CLIP_TEST') != '1', reason='Opt-in real CLIP model test')


def model_path() -> Path:
    return Path(os.environ.get('CLIP_MODEL_PATH') or DEFAULT_MODEL_PATH)


def picture(shift: int, background: tuple[int, int, int], shape: str) -> bytes:
    image = Image.new('RGB', (320, 240), background)
    draw = ImageDraw.Draw(image)
    if shape == 'circle':
        draw.ellipse((100 + shift, 60, 220 + shift, 180), fill=(255, 255, 255))
    else:
        for x in range(0, 320, 40):
            draw.rectangle((x, 0, x + 20, 240), fill=(20, 200, 60))
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG')
    return buffer.getvalue()


def test_model_file_has_the_pinned_hash() -> None:
    assert hashlib.sha256(model_path().read_bytes()).hexdigest() == MODEL_SHA256


@pytest.mark.asyncio
async def test_real_model_embeds_unit_vectors_with_a_sensible_order() -> None:
    images = [
        picture(0, (200, 30, 30), 'circle'),
        picture(15, (200, 30, 30), 'circle'),
        picture(0, (20, 20, 120), 'stripes'),
    ]

    embedder = ProcessEmbedder(model_path())
    try:
        vectors = await embedder.embed(images)
    finally:
        await embedder.close()

    assert vectors.shape == (3, DIMENSIONS)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    same, other = float(vectors[0] @ vectors[1]), float(vectors[0] @ vectors[2])
    assert same > 0.85
    assert same - other > 0.1
