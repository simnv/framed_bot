"""Game answers and TMDB title metadata, stored for later SQL analysis."""

from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal

type Game = Literal['framed', 'episode']
type Kind = Literal['movie', 'tv']
type MatchStatus = Literal['site', 'auto', 'visual', 'low_confidence', 'unmatched', 'manual']

GAMES: Final[tuple[Game, ...]] = ('framed', 'episode')
KIND_BY_GAME: Final[dict[Game, Kind]] = {'framed': 'movie', 'episode': 'tv'}
# Frames that a player can open in one round.
FRAMES: Final[dict[Game, int]] = {'framed': 6, 'episode': 10}
# Round 1 dates. Framed has one round each day, Episode one round each week.
FIRST_DAY: Final[dict[Game, date]] = {'framed': date(2022, 3, 12), 'episode': date(2022, 9, 5)}
DAYS_PER_ROUND: Final[dict[Game, int]] = {'framed': 1, 'episode': 7}


def parse_game(value: str) -> Game | None:
    match value:
        case 'framed':
            return 'framed'
        case 'episode':
            return 'episode'
    return None


def utc_today() -> date:
    return datetime.now(UTC).date()


def current_round(game: Game, today: date) -> int:
    """The round of the game on a UTC date. Before round 1 the value is 0 or less."""
    return (today - FIRST_DAY[game]).days // DAYS_PER_ROUND[game] + 1


def round_limit(game: Game, today: date) -> int:
    """The highest round that the bot accepts: one more than today, for players in later time zones."""
    return current_round(game, today) + 1


def round_date(game: Game, round_number: int) -> date:
    """The UTC date of a round."""
    return FIRST_DAY[game] + timedelta(days=(round_number - 1) * DAYS_PER_ROUND[game])
