import io

import numpy as np
import pytest
from PIL import Image

from answers.embedder import DIMENSIONS, embed_images, preprocess
from answers.visual import MIN_GAP, pick, scores


def jpeg(width: int, height: int, colour: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), colour).save(buffer, 'JPEG')
    return buffer.getvalue()


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def test_preprocess_gives_a_normalized_center_crop() -> None:
    pixels = preprocess(jpeg(400, 300))

    assert pixels is not None
    assert pixels.shape == (3, 224, 224)
    assert pixels.dtype == np.float32
    # Pure red: channel 0 is (1 - mean) / std, the others are (0 - mean) / std.
    assert pixels[0].mean() == pytest.approx((1 - 0.48145466) / 0.26862954, abs=0.05)
    assert pixels[2].mean() == pytest.approx(-0.40821073 / 0.27577711, abs=0.05)


@pytest.mark.parametrize('data', [b'', b'not an image', b'\xff\xd8\xff'])
def test_preprocess_drops_undecodable_images(data: bytes) -> None:
    assert preprocess(data) is None


class FakeSession:
    def __init__(self) -> None:
        self.batches: list[int] = []

    def run(self, output_names: list[str], input_feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        assert output_names == ['image_embeds']
        batch = input_feed['pixel_values']
        assert batch.shape[1:] == (3, 224, 224)
        self.batches.append(len(batch))
        return [np.full((len(batch), DIMENSIONS), 3.0, dtype=np.float32)]


def test_embed_images_runs_bounded_batches_and_normalizes() -> None:
    session = FakeSession()

    vectors = embed_images(session, [jpeg(64, 64)] * 10 + [b'broken'])

    assert session.batches == [8, 2]
    assert vectors.shape == (10, DIMENSIONS)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)


def test_embed_images_skips_the_model_when_no_image_is_accepted() -> None:
    session = FakeSession()

    assert embed_images(session, [b'broken']).shape == (0, DIMENSIONS)
    assert session.batches == []


def png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new('L', (width, height)).save(buffer, 'PNG')
    return buffer.getvalue()


@pytest.mark.parametrize('size', [(10_000, 1), (1, 10_000), (2049, 1000), (900, 200), (10, 10)])
def test_preprocess_rejects_sizes_before_any_resize(monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]) -> None:
    # A 10000 x 1 PNG is about 100 bytes; resizing its short side to 224 would ask for 2,240,000 x 224 pixels.
    def no_resize(*args: object, **kwargs: object) -> None:
        raise AssertionError('resize must not run')

    monkeypatch.setattr(Image.Image, 'resize', no_resize)

    assert preprocess(png(*size)) is None


def test_preprocess_accepts_the_largest_bounded_sizes() -> None:
    for size in ((2048, 2048), (2048, 512), (512, 2048), (896, 224)):
        pixels = preprocess(png(*size))
        assert pixels is not None
        assert pixels.shape == (3, 224, 224)


def test_scores_are_the_mean_of_the_best_match_of_each_frame() -> None:
    frames = np.stack([unit(1, 0), unit(0, 1)])
    candidates = {1: np.stack([unit(1, 0)]), 2: np.stack([unit(1, 1), unit(0, 1)]), 3: np.zeros((0, 2))}

    result = scores(frames, candidates)

    assert result == {1: pytest.approx(0.5), 2: pytest.approx((np.sqrt(0.5) + 1) / 2)}


def test_pick_accepts_a_clear_winner_only() -> None:
    frames = np.stack([unit(1, 0)])
    clear = {1: np.stack([unit(1, 0)]), 2: np.stack([unit(1, 1)])}
    close = {1: np.stack([unit(1, 0.01)]), 2: np.stack([unit(1, 0.02)])}

    winner = pick('framed', frames, clear)

    assert winner is not None
    assert winner.tmdb_id == 1
    assert winner.gap >= MIN_GAP['framed']
    assert pick('framed', frames, close) is None


def test_pick_uses_a_wider_gap_for_episode() -> None:
    frames = np.stack([unit(1, 0)])
    # Scores 1.0 and about 0.97: enough for Framed (0.02), not for Episode (0.04).
    candidates = {1: np.stack([unit(1, 0)]), 2: np.stack([unit(1, 0.25)])}

    assert pick('framed', frames, candidates) is not None
    assert pick('episode', frames, candidates) is None


def test_pick_needs_frames_and_two_scored_candidates() -> None:
    frames = np.stack([unit(1, 0)])

    assert pick('framed', np.zeros((0, 2)), {1: frames, 2: frames}) is None
    assert pick('framed', frames, {1: frames, 2: np.zeros((0, 2))}) is None


def test_preprocess_drops_truncated_and_odd_pillow_failures() -> None:
    good = png(64, 64)

    assert preprocess(good[: len(good) // 2]) is None
    assert preprocess(good[:30] + b'\x00' * 40) is None


def test_pick_breaks_exact_ties_by_id_independent_of_order() -> None:
    frames = np.stack([unit(1, 0)])
    # Equal scores give a zero gap, so nothing is picked whichever order the dict has.
    assert pick('framed', frames, {2: frames, 1: frames}) is None
