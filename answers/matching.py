"""Pick the TMDB title of a round from text search results (agent B's algorithm). Pure functions only."""

import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Literal

from rapidfuzz import fuzz

from . import Kind

ROMAN: Final = {'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'}
NUMBER_WORDS: Final = {
    'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6',
    'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12',
}  # fmt: skip
ROMAN_BY_DIGIT: Final = ('', '', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX')
LOOSE_STOP_WORDS: Final = frozenset({'the', 'part', 'a'})
GENERIC_TAILS: Final = frozenset({'movie', 'film', 'the movie', 'series', 'show'})
OVERRIDE_FACTOR: Final = 20
FUZZY_MIN: Final = 92
RELEASE_GRACE: Final = timedelta(days=45)
TIE_VOTES: Final = 150
TIE_SHARE: Final = 0.02
SPLIT_MIN: Final = 7
NOT_WORD = re.compile(r'[\W_]+')
APOSTROPHE = re.compile(r"[’']")
OUR_SUFFIX = re.compile(r'^(\w{2,})our(s?)$')
SEPARATORS = re.compile(r'[-·:]')
TRAILING_DIGIT = re.compile(r'^(.*\D) (\d)$')
TRAILING_ROMAN = re.compile(r'^(.*) (II|III|IV)$')
BALLGAME = re.compile(r'\w[a-z]{3,}ball ?game', re.IGNORECASE)
HYPHEN_LETTER = re.compile(r'(\b\w+)-(\w\b)')
AND_WORD = re.compile(r'\band\b')
RELEASE_YEAR = re.compile(r'[0-9]{4}')

type Tier = Literal[1, 2, 3, 4]


@dataclass(frozen=True, slots=True)
class Candidate:
    tmdb_id: int
    names: frozenset[str]
    release: str | None  # ISO date 'YYYY-MM-DD', or None
    votes: int

    @property
    def year(self) -> int | None:
        return int(self.release[:4]) if self.release and RELEASE_YEAR.match(self.release) else None


@dataclass(frozen=True, slots=True)
class Scored:
    candidate: Candidate
    tier: Tier
    rule: str
    length: int


@dataclass(frozen=True, slots=True)
class TextChoice:
    choice: Scored
    ties: list[Candidate]  # 2+ candidates for a visual tie-break, or [] when there is no tie
    pool: list[Candidate]  # the filtered pool, in search order

    @property
    def confident(self) -> bool:
        return self.choice.tier <= 3


type Search = Callable[[Kind, str, int | None], Awaitable[list[Candidate]]]


def fold(text: str) -> str:
    text = (
        text.replace('⁄', '/').replace('·', ' ').replace('Æ', 'AE').replace('æ', 'ae').replace('Œ', 'OE')
        .replace('œ', 'oe')
    )  # fmt: skip
    return ''.join(char for char in unicodedata.normalize('NFKD', text) if not unicodedata.combining(char))


def _words(text: str) -> list[str]:
    text = APOSTROPHE.sub('', fold(text).casefold().replace('&', ' and '))
    return NOT_WORD.sub(' ', text).split()


def tokens(text: str) -> list[str]:
    words = [ROMAN.get(word, word) if index > 0 else word for index, word in enumerate(_words(text))]
    if len(words) > 1:
        words = [word for word in words if word not in LOOSE_STOP_WORDS]
    return [NUMBER_WORDS.get(word, word) for word in (OUR_SUFFIX.sub(r'\1or\2', word) for word in words)]


def loose_key(text: str) -> str:
    return ''.join(tokens(text))


def strict_key(text: str) -> str:
    return ''.join(OUR_SUFFIX.sub(r'\1or\2', word) for word in _words(text))


def _unique(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def query_variants(query: str) -> list[str]:
    variants = [query]
    folded = fold(query)
    variants.append(folded)
    variants.append(SEPARATORS.sub(' ', folded))
    if '&' in query:
        variants.append(query.replace('&', 'and'))
    if AND_WORD.search(query):
        variants.append(AND_WORD.sub('&', query))
    if (found := TRAILING_DIGIT.search(query)) and found[2] in '23456789':
        variants.append(f'{found[1]} {ROMAN_BY_DIGIT[int(found[2])]}')
    if found := TRAILING_ROMAN.search(query):
        variants.append(f'{found[1]} Part {found[2]}')
        variants.append(f'{found[1]} {ROMAN[found[2].casefold()]}')
    if BALLGAME.search(query):
        variants.append(re.sub(r'(?i)ballgame', 'Ball Game', query))
    if ':' in query:
        head, tail = query.split(':', 1)
        variants += [head.strip(), tail.strip()]
    if HYPHEN_LETTER.search(query):
        variants.append(HYPHEN_LETTER.sub(r'\1·\2', query))
    return _unique(variants)


def split_variants(query: str) -> list[str]:
    """Split each long word in two: 'Ballgame' -> 'Bal lgame', 'Ball game', ..."""
    words = query.split()
    out = []
    for index, word in enumerate(words):
        if len(word) >= SPLIT_MIN and word.isalpha():
            for cut in range(3, len(word) - 2):
                out.append(' '.join([*words[:index], word[:cut], word[cut:], *words[index + 1 :]]))
    return out


def _parts(text: str) -> tuple[str, str]:
    head, _, tail = text.partition(':')
    return head.strip(), tail.strip()


def _name_tier(query: str, name: str) -> tuple[Tier, str, int] | None:
    query_key, name_key = loose_key(query), loose_key(name)
    if strict_key(name) == strict_key(query):
        return 1, 'exact', len(query_key)
    if name_key == query_key:
        return 2, 'exact-loose', len(query_key)
    query_head, query_tail = _parts(query)
    name_head, name_tail = _parts(name)
    if name_tail and name_key.startswith(query_key) and len(query_key) >= 4 and loose_key(name_head) == query_key:
        return 3, 'title-subtitle-prefix', len(query_key)
    if name_tail and loose_key(name_tail) == query_key:
        return 3, 'title-tail==query', len(query_key)
    if query_tail and loose_key(query_tail) == name_key and len(loose_key(query_tail)) >= 4:
        return 3, 'query-tail==title', len(name_key)
    if query_tail and loose_key(query_head) == name_key:
        if len(name_key) >= 2 and loose_key(query_tail) not in {loose_key(tail) for tail in GENERIC_TAILS}:
            return 3, 'query-head==title', len(name_key)
        return 3, 'query-head==title(generic tail)', len(name_key) - 5
    if name_key.startswith(query_key) and len(tokens(query)) >= 2:
        return 3, 'prefix', len(query_key)
    if (
        name_key.endswith(query_key)
        and len(tokens(query)) >= 1
        and len(tokens(name)) <= len(tokens(query)) + 2
        and len(query_key) >= 5
    ):
        return 3, 'suffix', len(query_key)
    ratio = fuzz.ratio(query_key, name_key)
    if ratio >= FUZZY_MIN:
        return 4, f'fuzzy{ratio:.0f}', 0
    return None


def score(query: str, candidate: Candidate) -> Scored | None:
    best = None
    for name in candidate.names:
        found = _name_tier(query, name)
        if found and (best is None or found[0] < best[0]):
            best = found
    return None if best is None else Scored(candidate, *best)


def _order(item: Scored) -> tuple[int, int, int]:
    return item.tier, -item.length if item.tier == 3 else 0, -item.candidate.votes


def choose(query: str, candidates: list[Candidate]) -> Scored | None:
    """Best tier first, then votes. A much more popular tier-3 title beats a tier-1/2 one (OVERRIDE_FACTOR)."""
    scored = sorted((item for item in (score(query, c) for c in candidates) if item), key=_order)
    if not scored:
        return None
    best = scored[0]
    if best.tier <= 2:
        tier3 = [item for item in scored if item.tier == 3]
        if tier3:
            popular = max(tier3, key=lambda item: item.candidate.votes)
            if popular.candidate.votes >= OVERRIDE_FACTOR * max(best.candidate.votes, 5):
                return popular
        return best
    if best.tier == 3:
        tier3 = [item for item in scored if item.tier == 3]
        top = max(item.candidate.votes for item in tier3)
        kept = [item for item in tier3 if item.candidate.votes >= max(10, TIE_SHARE * top)] or tier3
        return kept[0]
    return best


def ties(query: str, candidates: list[Candidate], choice: Scored) -> list[Candidate]:
    """Candidates in the tier of the choice with enough votes; [] when fewer than 2."""
    same = [item.candidate for item in (score(query, c) for c in candidates) if item and item.tier == choice.tier]
    if not same:
        return []
    top = max(candidate.votes for candidate in same)
    tied = [c for c in same if c.votes >= TIE_VOTES or c.votes >= TIE_SHARE * top]
    if choice.candidate not in tied:
        tied.insert(0, choice.candidate)
    return tied if len(tied) >= 2 else []


def filter_pool(
    candidates: list[Candidate], year: int | None, round_date: date, sibling_years: frozenset[int]
) -> list[Candidate]:
    """Drop titles released after the round (with RELEASE_GRACE), also for a year-tagged query.

    For a query without a year, also drop the years that year-tagged rounds of the same title own.
    The result can be empty; the caller then tries the next fallback or marks the round unmatched.
    """
    cutoff = (round_date + RELEASE_GRACE).isoformat()
    pool = [c for c in candidates if not c.release or c.release <= cutoff]
    if year is None and sibling_years:
        pool = [c for c in pool if c.year not in sibling_years]
    return pool


async def text_match(
    search: Search, kind: Kind, query: str, year: int | None, round_date: date, sibling_years: frozenset[int]
) -> TextChoice | None:
    found: dict[int, Candidate] = {}

    async def add(queries: list[str]) -> None:
        for variant in queries:
            for candidate in await search(kind, variant, year):
                found.setdefault(candidate.tmdb_id, candidate)

    def pick() -> TextChoice | None:
        pool = filter_pool(list(found.values()), year, round_date, sibling_years)
        choice = choose(query, pool)
        if choice is None:
            return None
        return TextChoice(choice, ties(query, pool, choice) if choice.tier <= 3 else [], pool)

    await add(query_variants(query))
    result = pick()
    if result is None:
        await add(split_variants(query))
        result = pick()
    return result
