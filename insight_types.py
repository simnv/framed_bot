"""Insight data shapes and rules, shared by the queries, the rendering and the bot handlers.

This module has no database or configuration access, so rich_tables can import it.
"""

from __future__ import annotations

import dataclasses
from typing import Final, Literal

type Dimension = Literal['genre', 'decade']
type Role = Literal['director', 'creator', 'cast']
type RankBoard = Literal['score', 'frame', 'win', 'rounds', 'genre', 'decade']

SMOOTHING: Final = 10
MIN_STATS_BUCKET: Final = 5
MIN_STATS_PERSON: Final = 3
MIN_LEAGUE_ROUNDS: Final = 15
MIN_LEAGUE_PLAYERS: Final = 3
MIN_TITLE_PLAYERS: Final = 5
LEAGUE_LIMIT: Final = 10
TITLE_LIMIT: Final = 5
STATS_BEST: Final = 3
# The People section: all people up to PEOPLE_BEST + PEOPLE_WORST, else the best and the worst.
PEOPLE_BEST: Final = 5
PEOPLE_WORST: Final = 3
CAST_MAX_ORDER: Final = 2
ELIGIBLE_STATUSES: Final = ('site', 'auto', 'visual', 'low_confidence', 'manual')


@dataclasses.dataclass(frozen=True, slots=True)
class Bucket:
    """One genre, decade or person of one player. rounds is the sum of the weights.

    points is the smoothed points. strength is points minus the own mean: above 0 is better than usual.
    """

    key: int
    label: str
    rounds: float
    points: float
    win_rate: float
    role: Role | None = None
    strength: float = 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class Section:
    """Buckets that pass the minimum. If all buckets fit, best holds all of them and worst is empty."""

    best: tuple[Bucket, ...]
    worst: tuple[Bucket, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class Rank:
    board: RankBoard
    place: int
    total: int
    # The genre name or the decade label for a league rank, else None.
    label: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class GameInsight:
    genres: Section
    decades: Section
    people: Section
    current_streak: int
    best_streak: int
    ranks: tuple[Rank, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class Choice:
    key: int
    label: str


@dataclasses.dataclass(frozen=True, slots=True)
class LeagueRow:
    user_id: int
    name: str | None
    score: float
    rounds: float
    win_rate: float
    # The score minus the mean of all players of the game: above 0 is better than the average player.
    strength: float = 0.0
    # The strength against the own mean of the player, as in /stats: above 0 is better than usual.
    personal: float = 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class League:
    dimension: Dimension
    key: int
    label: str
    rows: tuple[LeagueRow, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class TitleRow:
    round: int
    name: str
    year: int | None
    players: int
    win_rate: float
    points: float


@dataclasses.dataclass(frozen=True, slots=True)
class HardTitles:
    hardest: tuple[TitleRow, ...]
    easiest: tuple[TitleRow, ...]


def decade_label(year: int) -> str:
    return f'{year}-е'
