"""Leaderboard metrics, shared by the queries, the rendering and the bot handlers."""

from enum import IntEnum


class TopType(IntEnum):
    TOP_SCORE = 1
    TOP_FRAME = 2
    TOP_WIN = 3
    TOP_ROUNDS = 4
