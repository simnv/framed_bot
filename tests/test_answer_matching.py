import os
import subprocess
import sys
from datetime import date

import pytest

from answers import Kind, round_date
from answers.matching import (
    Candidate,
    choose,
    filter_pool,
    fold,
    loose_key,
    query_variants,
    score,
    split_variants,
    strict_key,
    text_match,
    ties,
)


def film(tmdb_id: int, *names: str, release: str | None = '2000-01-01', votes: int = 1000) -> Candidate:
    return Candidate(tmdb_id, frozenset(names), release, votes)


def test_round_date_counts_days_for_framed_and_weeks_for_episode() -> None:
    assert round_date('framed', 1) == date(2022, 3, 12)
    assert round_date('framed', 3) == date(2022, 3, 14)
    assert round_date('episode', 2) == date(2022, 9, 12)


def test_fold_removes_accents_and_ligatures() -> None:
    assert fold('Amélie · Œdipe') == 'Amelie   OEdipe'


def test_keys_normalize_case_punctuation_and_spelling() -> None:
    assert strict_key("The Hitchhiker's Guide") == strict_key('the hitchhikers guide')
    assert strict_key('Colour') == strict_key('Color')
    assert loose_key('The Godfather Part II') == loose_key('Godfather 2')
    assert loose_key('Ocean’s Eleven') == loose_key("Ocean's 11")
    assert strict_key('Godfather 2') != strict_key('The Godfather Part II')


def test_query_variants_cover_the_spec_rewrites() -> None:
    assert query_variants('Léon: The Professional') == [
        'Léon: The Professional',
        'Leon: The Professional',
        'Leon  The Professional',
        'Léon',
        'The Professional',
    ]
    assert query_variants('Rocky 2') == ['Rocky 2', 'Rocky II']
    assert query_variants('Rocky II') == ['Rocky II', 'Rocky Part II', 'Rocky 2']
    assert query_variants('Fast & Furious') == ['Fast & Furious', 'Fast and Furious']
    assert query_variants('Pride and Prejudice') == ['Pride and Prejudice', 'Pride & Prejudice']
    assert query_variants('Spider-Man') == ['Spider-Man', 'Spider Man']


def test_split_variants_split_long_words_only() -> None:
    assert split_variants('Up') == []
    assert split_variants('Ballgame') == ['Bal lgame', 'Ball game', 'Ballg ame']


@pytest.mark.parametrize(
    ('query', 'name', 'tier', 'rule'),
    [
        ('Alien', 'Alien', 1, 'exact'),
        ('Godfather', 'The Godfather', 2, 'exact-loose'),
        ('Borat', 'Borat: Cultural Learnings of America', 3, 'title-subtitle-prefix'),
        ('Andor', 'Star Wars: Andor', 3, 'title-tail==query'),
        ('Toy Story', 'Toy Story That Time Forgot', 3, 'prefix'),
        ('Moonlightt', 'Moonlight', 4, 'fuzzy95'),
    ],
)
def test_score_assigns_tiers(query: str, name: str, tier: int, rule: str) -> None:
    scored = score(query, film(1, name))

    assert scored is not None
    assert (scored.tier, scored.rule) == (tier, rule)


def test_score_returns_none_for_unrelated_titles() -> None:
    assert score('Alien', film(1, 'Jaws')) is None


def test_choose_prefers_the_best_tier_then_votes() -> None:
    remake = film(2, 'Dune', votes=9000)
    original = film(1, 'Dune', votes=3000)
    loose = film(3, 'The Dune', votes=50000)

    chosen = choose('Dune', [original, loose, remake])

    assert chosen is not None
    assert chosen.candidate == remake


def test_choose_lets_a_much_more_popular_tier_three_title_win() -> None:
    exact = film(1, 'Andor', votes=10)
    tail = film(2, 'Star Wars: Andor', votes=5000)

    chosen = choose('Andor', [exact, tail])

    assert chosen is not None
    assert chosen.candidate == tail


def test_ties_need_two_candidates_with_enough_votes() -> None:
    a, b, obscure = film(1, 'Dune', votes=9000), film(2, 'Dune', votes=150), film(3, 'Dune', votes=20)
    chosen = choose('Dune', [a, b, obscure])
    assert chosen is not None

    assert ties('Dune', [a, b, obscure], chosen) == [a, b]
    assert ties('Dune', [a, obscure], chosen) == []


def test_ties_accept_two_percent_of_the_top_votes() -> None:
    a, b = film(1, 'Annie', votes=100), film(2, 'Annie', votes=2)
    chosen = choose('Annie', [a, b])
    assert chosen is not None

    assert ties('Annie', [a, b], chosen) == [a, b]


def test_filter_pool_drops_later_releases_and_sibling_years() -> None:
    old, remake, future = (
        film(1, 'Halloween', release='1978-10-24'),
        film(2, 'Halloween', release='2018-10-18'),
        film(3, 'Halloween', release='2030-01-01'),
    )
    pool = [old, remake, future]

    assert filter_pool(pool, None, date(2022, 3, 12), frozenset()) == [old, remake]
    assert filter_pool(pool, None, date(2022, 3, 12), frozenset({2018})) == [old]


def test_filter_pool_applies_the_date_cutoff_to_year_tagged_queries() -> None:
    remake, future = film(2, 'Halloween', release='2018-10-18'), film(3, 'Halloween', release='2030-01-01')

    assert filter_pool([remake, future], 2030, date(2022, 3, 12), frozenset({2018})) == [remake]


def test_an_empty_filtered_pool_stays_empty() -> None:
    future, sibling = film(3, 'Halloween', release='2030-01-01'), film(2, 'Halloween', release='2018-10-18')

    assert filter_pool([future], None, date(2022, 3, 12), frozenset()) == []
    assert filter_pool([sibling], None, date(2022, 3, 12), frozenset({2018})) == []


def test_filter_pool_keeps_releases_within_the_grace_period() -> None:
    soon = film(1, 'X', release='2022-04-26')

    assert filter_pool([soon], None, date(2022, 3, 12), frozenset()) == [soon]
    assert filter_pool([soon, film(2, 'X')], None, date(2022, 3, 11), frozenset())[0].tmdb_id == 2


class FakeSearch:
    def __init__(self, results: dict[str, list[Candidate]]) -> None:
        self.results = results
        self.queries: list[tuple[Kind, str, int | None]] = []

    async def __call__(self, kind: Kind, query: str, year: int | None) -> list[Candidate]:
        self.queries.append((kind, query, year))
        return self.results.get(query, [])


@pytest.mark.asyncio
async def test_text_match_finds_a_title_through_a_variant() -> None:
    search = FakeSearch({'Leon': [film(101, 'Léon')]})

    result = await text_match(search, 'movie', 'Leon', None, date(2022, 3, 12), frozenset())

    assert result is not None
    assert (result.choice.candidate.tmdb_id, result.choice.tier, result.confident) == (101, 1, True)


@pytest.mark.asyncio
async def test_text_match_passes_the_year_and_uses_split_words_as_a_fallback() -> None:
    search = FakeSearch({'Ball game': [film(7, 'Ball Game', release='1999-01-01')]})

    result = await text_match(search, 'movie', 'Ballgame', 1999, date(2022, 3, 12), frozenset())

    assert result is not None
    assert result.choice.candidate.tmdb_id == 7
    assert ('movie', 'Ballgame', 1999) in search.queries
    assert ('movie', 'Ball game', 1999) in search.queries


@pytest.mark.asyncio
async def test_text_match_returns_none_without_candidates() -> None:
    assert await text_match(FakeSearch({}), 'tv', 'Nothing', None, date(2022, 9, 5), frozenset()) is None


@pytest.mark.asyncio
async def test_text_match_reports_ties_for_same_title_remakes() -> None:
    search = FakeSearch({'Dune': [film(1, 'Dune', release='1984-12-14'), film(2, 'Dune', release='2021-09-15')]})

    result = await text_match(search, 'movie', 'Dune', None, date(2022, 3, 12), frozenset())

    assert result is not None
    assert {candidate.tmdb_id for candidate in result.ties} == {1, 2}


@pytest.mark.asyncio
async def test_text_match_moves_to_the_split_queries_when_every_candidate_is_filtered() -> None:
    search = FakeSearch({'Ballgame': [film(1, 'Ballgame', release='2030-01-01')], 'Ball game': [film(2, 'Ball Game')]})

    result = await text_match(search, 'movie', 'Ballgame', None, date(2022, 3, 12), frozenset())

    assert result is not None
    assert result.choice.candidate.tmdb_id == 2
    assert ('movie', 'Ball game', None) in search.queries


@pytest.mark.asyncio
async def test_text_match_is_unmatched_when_every_candidate_is_filtered() -> None:
    search = FakeSearch({'Halloween': [film(3, 'Halloween', release='2030-01-01')]})

    assert await text_match(search, 'movie', 'Halloween', None, date(2022, 3, 12), frozenset()) is None


def test_candidate_year_reads_ascii_digits_only() -> None:
    assert film(1, 'X', release='1984-12-14').year == 1984
    assert film(1, 'X', release=None).year is None
    assert film(1, 'X', release='１９８４-12-14').year is None


@pytest.mark.parametrize('seed', ['0', '1', '2', '3', '4', '5'])
def test_choice_does_not_depend_on_the_hash_seed(seed: str) -> None:
    code = (
        'from answers.matching import Candidate, choose\n'
        "one = Candidate(1, frozenset({'Alien', 'Resurrection'}), '2000-01-01', 100)\n"
        "two = Candidate(2, frozenset({'Alien'}), '2000-01-01', 1000)\n"
        "chosen = choose('Alien: Resurrection', [one, two])\n"
        'print(chosen.candidate.tmdb_id, chosen.rule, chosen.length)\n'
    )
    run = subprocess.run(  # noqa: S603 - fixed code and interpreter
        [sys.executable, '-c', code], env={**os.environ, 'PYTHONHASHSEED': seed}, capture_output=True, text=True
    )

    assert run.stdout.split() == ['1', 'query-tail==title', '12']
