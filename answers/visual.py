"""Visual tie-break: compare the frames of a round with TMDB images of each candidate."""

from dataclasses import dataclass
from typing import Final

import numpy as np

from . import Game

# The best candidate must beat the second by this cosine-similarity gap.
MIN_GAP: Final[dict[Game, float]] = {'framed': 0.02, 'episode': 0.04}


@dataclass(frozen=True, slots=True)
class VisualPick:
    tmdb_id: int
    gap: float


def scores(frames: np.ndarray, candidates: dict[int, np.ndarray]) -> dict[int, float]:
    """For each candidate with images: the mean over frames of the best cosine similarity (rows are unit vectors)."""
    if len(frames) == 0:
        return {}
    return {
        tmdb_id: float((frames @ images.T).max(axis=1).mean()) for tmdb_id, images in candidates.items() if len(images)
    }


def pick(game: Game, frames: np.ndarray, candidates: dict[int, np.ndarray]) -> VisualPick | None:
    """The best candidate when its gap to the second is at least MIN_GAP[game]; None otherwise."""
    ranked = sorted(scores(frames, candidates).items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) < 2:
        return None
    gap = ranked[0][1] - ranked[1][1]
    return VisualPick(ranked[0][0], gap) if gap >= MIN_GAP[game] else None
