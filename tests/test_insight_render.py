# Renderers on fixed inputs built from the insight dataclasses. No handler, no database.
import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from answers import Game
from insight import Bucket, Choice, GameInsight, HardTitles, League, LeagueRow, Rank, Section, TitleRow
from rich_tables import (
    STATS_HTML_LIMIT,
    _ResolvedTopResult,
    render_hard_titles,
    render_league,
    render_league_picker,
    render_stats,
)
from stats import Stats


class RichParser(HTMLParser):
    """Collect tables as lists of rows, paragraphs as texts and links as attribute dicts, in document order."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.paragraphs = []
        self.links = []
        self.cell = None
        self.paragraph = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))
        if tag == 'table':
            self.tables.append([])
        if tag == 'tr':
            self.tables[-1].append([])
        if tag in ('th', 'td'):
            self.cell = ''
        if tag == 'p':
            self.paragraph = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data
        elif self.paragraph is not None:
            self.paragraph += data

    def handle_endtag(self, tag):
        if tag in ('th', 'td'):
            self.tables[-1][-1].append(self.cell)
            self.cell = None
        if tag == 'p':
            self.paragraphs.append(self.paragraph)
            self.paragraph = None


FRAMED = Stats(9, 5, 2.4, 29, 4.0)
EPISODE = Stats(4, 0, None, 4, 11.0)


def bucket(label, points, rounds=12.0, win_rate=0.5, role=None, key=1):
    return Bucket(key=key, label=label, rounds=rounds, points=points, win_rate=win_rate, role=role)


def section(best, worst=()):
    return Section(best=tuple(best), worst=tuple(worst))


EMPTY = Section(best=())


def insight(genres=EMPTY, decades=EMPTY, people=EMPTY, current_streak=0, best_streak=0, ranks=()):
    return GameInsight(
        genres=genres,
        decades=decades,
        people=people,
        current_streak=current_streak,
        best_streak=best_streak,
        ranks=tuple(ranks),
    )


def insights(framed: GameInsight | None = None, episode: GameInsight | None = None) -> dict[Game, GameInsight]:
    result: dict[Game, GameInsight] = {}
    if framed is not None:
        result['framed'] = framed
    if episode is not None:
        result['episode'] = episode
    return result


def test_stats_without_owner_and_insights_keep_the_old_output():
    assert render_stats(FRAMED, EPISODE, owner=None, insights=None) == render_stats(FRAMED, EPISODE)


def test_stats_with_an_empty_insight_map_add_nothing():
    assert render_stats(FRAMED, EPISODE, insights=insights()) == render_stats(FRAMED, EPISODE)
    assert render_stats(FRAMED, EPISODE, insights=insights(framed=insight())) == render_stats(FRAMED, EPISODE)


def test_stats_owner_is_named_with_an_id_mention_in_the_first_line():
    owner = SimpleNamespace(user_id=99, name='Имя & <b>', score=None)
    rich = render_stats(FRAMED, EPISODE, owner=owner)
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == 'Статистика для Имя & <b>'
    assert rich['html'].startswith('<p>Статистика для <a href="tg://user?id=99">Имя &amp; &lt;b&gt;</a></p><table')
    assert 'Твоя статистика' not in rich['html']
    assert parsed.links[0] == {'href': 'tg://user?id=99'}
    assert rich['html'].endswith('</p>')
    assert rich['skip_entity_detection'] is True


def test_stats_owner_with_a_fresh_public_username_links_to_the_profile():
    owner = _ResolvedTopResult(user_id=99, name='Имя', score=None, public_username='fresh_owner')
    rich = render_stats(FRAMED, EPISODE, owner=owner)
    assert rich['html'].startswith('<p>Статистика для <a href="https://t.me/fresh_owner?profile">Имя</a></p>')


def test_stats_owner_without_a_valid_id_stays_a_plain_name():
    rich = render_stats(FRAMED, EPISODE, owner=SimpleNamespace(user_id=None, name='Имя', score=None))
    assert rich['html'].startswith('<p>Статистика для Имя</p>')
    assert '<a ' not in rich['html'].split('<table', 1)[0]


def test_genre_section_lists_best_and_worst_with_a_gap_and_a_verdict():
    genres = section(
        [bucket('Drama', 4.123, 12.5, 0.6), bucket('Comedy & Co', 3.9, 7, 0.5), bucket('Action', 3.5, 5.25, 0.4)],
        [bucket('Horror', 2.1, 6, 0.2), bucket('War', 1.9, 5, 0.1), bucket('Western', 2.4, 5.5, 0.3)],
    )
    rich = render_stats(FRAMED, EPISODE, insights=insights(framed=insight(genres=genres)))
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[2:] == ['Framed: жанры', 'Лучшие жанры: Drama, Comedy & Co.']
    assert parsed.tables[1] == [
        ['Жанр', 'Очки', 'Раунды', 'Победы'],
        ['Drama', '4.12', '12.5', '60%'],
        ['Comedy & Co', '3.90', '7', '50%'],
        ['Action', '3.50', '5.2', '40%'],
        ['…', '…', '…', '…'],
        ['Western', '2.40', '5.5', '30%'],
        ['Horror', '2.10', '6', '20%'],
        ['War', '1.90', '5', '10%'],
    ]
    assert 'Comedy &amp; Co' in rich['html']


def test_genre_section_with_all_buckets_has_no_gap_and_a_singular_verdict_for_one_bucket():
    rich = render_stats(FRAMED, EPISODE, insights=insights(framed=insight(genres=section([bucket('Drama', 4.0)]))))
    parsed = RichParser(rich['html'])
    assert parsed.tables[1] == [['Жанр', 'Очки', 'Раунды', 'Победы'], ['Drama', '4.00', '12', '50%']]
    assert parsed.paragraphs[-1] == 'Лучший жанр: Drama.'
    assert '…' not in rich['html']


def test_decade_section_uses_its_own_column_and_verdict():
    decades = section([bucket('1990-е', 4.0), bucket('2000-е', 3.5)])
    rich = render_stats(FRAMED, EPISODE, insights=insights(episode=insight(decades=decades)))
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[2:] == ['Episode: десятилетия', 'Лучшие десятилетия: 1990-е, 2000-е.']
    assert parsed.tables[1][0] == ['Десятилетие', 'Очки', 'Раунды', 'Победы']


def test_people_section_shows_russian_roles():
    people = section(
        [
            bucket('Christopher Nolan', 5.0, 4, 0.75, role='director'),
            bucket('Vince Gilligan', 4.5, 3, 1.0, role='creator'),
            bucket('Tom Hanks', 4.0, 3, 0.67, role='cast'),
        ]
    )
    rich = render_stats(FRAMED, EPISODE, insights=insights(framed=insight(people=people)))
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[2] == 'Framed: люди'
    assert parsed.tables[1] == [
        ['Кто', 'Роль', 'Очки', 'Раунды', 'Победы'],
        ['Christopher Nolan', 'режиссёр', '5.00', '4', '75%'],
        ['Vince Gilligan', 'создатель', '4.50', '3', '100%'],
        ['Tom Hanks', 'актёр', '4.00', '3', '67%'],
    ]
    assert len(parsed.paragraphs) == 3


def test_streaks_are_one_line_and_hidden_without_a_win():
    rich = render_stats(FRAMED, EPISODE, insights=insights(framed=insight(current_streak=3, best_streak=7)))
    assert RichParser(rich['html']).paragraphs[2:] == ['Framed: серия побед сейчас 3, лучшая 7.']
    rich = render_stats(FRAMED, EPISODE, insights=insights(framed=insight(current_streak=0, best_streak=0)))
    assert 'серия' not in rich['html']


def test_ranks_name_the_boards_and_the_leagues_of_the_game():
    ranks = (
        Rank('score', 3, 25),
        Rank('frame', 10, 20),
        Rank('win', 1, 25),
        Rank('rounds', 25, 25),
        Rank('genre', 2, 5, 'Drama'),
        Rank('decade', 1, 3, '1990-е'),
    )
    rich = render_stats(FRAMED, EPISODE, insights=insights(episode=insight(ranks=ranks)))
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[2] == 'Episode: места в /top'
    assert parsed.tables[1] == [
        ['Таблица', 'Место'],
        ['Очки', '3 из 25'],
        ['Средний кадр', '10 из 20'],
        ['Сериалы', '1 из 25'],
        ['Участия', '25 из 25'],
        ['Жанр Drama', '2 из 5'],
        ['Десятилетие 1990-е', '1 из 3'],
    ]


def test_sections_follow_the_spec_order_with_framed_before_episode():
    full = insight(
        genres=section([bucket('Drama', 4.0)]),
        decades=section([bucket('1990-е', 4.0)]),
        people=section([bucket('Tom Hanks', 4.0, role='cast')]),
        current_streak=1,
        best_streak=2,
        ranks=(Rank('score', 1, 2),),
    )
    owner = SimpleNamespace(user_id=99, name='Имя', score=None)
    rich = render_stats(FRAMED, EPISODE, owner=owner, insights=insights(episode=full, framed=full))
    titles = rendered_titles(rich)
    assert titles == [
        'Framed: жанры',
        'Framed: десятилетия',
        'Framed: люди',
        'Framed: серия побед сейчас 1, лучшая 2.',
        'Framed: места в /top',
        'Episode: жанры',
        'Episode: десятилетия',
        'Episode: люди',
        'Episode: серия побед сейчас 1, лучшая 2.',
        'Episode: места в /top',
    ]
    # The explanation of the main table stays directly under it.
    assert rich['html'].index('Прочерк значит') < rich['html'].index('Framed: жанры')


def long_insight(width):
    """Six genre buckets and six decade buckets with labels of the given width, plus people and ranks."""
    best = [bucket('B' * width + str(index), 5.0 - index) for index in range(3)]
    worst = [bucket('W' * width + str(index), 2.0 - index * 0.1) for index in range(3)]
    people = section([bucket('P' * width + str(index), 4.0 - index, role='cast') for index in range(3)])
    return insight(
        genres=section(best, worst),
        decades=section(best, worst),
        people=people,
        current_streak=1,
        best_streak=2,
        ranks=(Rank('score', 1, 2), Rank('genre', 1, 3, 'Drama')),
    )


def rendered_titles(rich):
    return [
        paragraph for paragraph in RichParser(rich['html']).paragraphs if paragraph.startswith(('Framed', 'Episode'))
    ]


def test_short_insights_are_not_trimmed():
    rich = render_stats(FRAMED, EPISODE, insights=insights(long_insight(1), long_insight(1)))
    assert len(rich['html']) <= STATS_HTML_LIMIT
    assert len(RichParser(rich['html']).tables[1]) == 8
    assert len(rendered_titles(rich)) == 10


def test_long_insights_first_shrink_best_and_worst_to_two():
    rich = render_stats(FRAMED, EPISODE, insights=insights(long_insight(10), long_insight(10)))
    assert len(rich['html']) <= STATS_HTML_LIMIT
    genres = RichParser(rich['html']).tables[1]
    assert [row[0] for row in genres[1:]] == ['B' * 10 + '0', 'B' * 10 + '1', '…', 'W' * 10 + '1', 'W' * 10 + '2']
    assert len(rendered_titles(rich)) == 10


def test_longer_insights_then_drop_the_people_sections():
    rich = render_stats(FRAMED, EPISODE, insights=insights(long_insight(30), long_insight(30)))
    assert len(rich['html']) <= STATS_HTML_LIMIT
    titles = rendered_titles(rich)
    assert 'Framed: люди' not in titles and 'Episode: люди' not in titles
    assert len(titles) == 8


def test_longest_insights_then_drop_the_episode_sections():
    rich = render_stats(FRAMED, EPISODE, insights=insights(long_insight(80), long_insight(80)))
    assert len(rich['html']) <= STATS_HTML_LIMIT
    titles = rendered_titles(rich)
    assert all(title.startswith('Framed') for title in titles)
    assert 'Framed: люди' not in titles
    assert len(titles) == 4


def test_insights_that_never_fit_are_left_out_so_telegram_accepts_the_message():
    rich = render_stats(FRAMED, EPISODE, insights=insights(long_insight(400), long_insight(400)))
    assert len(rich['html']) <= STATS_HTML_LIMIT
    assert rendered_titles(rich) == []
    # The main table and its explanation stay.
    assert 'Прочерк значит' in rich['html']


def test_trimmed_section_with_all_buckets_keeps_two_best_and_two_worst():
    genres = section([bucket(f'G{index}' + 'x' * 90, 5.0 - index) for index in range(6)])
    both = insight(genres=genres, decades=genres)
    rich = render_stats(FRAMED, EPISODE, insights=insights(both, both))
    assert len(rich['html']) <= STATS_HTML_LIMIT
    rows = RichParser(rich['html']).tables[1]
    assert [row[0][:2] for row in rows[1:]] == ['G0', 'G1', '…', 'G4', 'G5']


@pytest.mark.parametrize(
    ('game', 'dimension', 'title', 'hint'),
    [
        ('framed', 'genre', 'Framed: лиги по жанрам', 'Выбери жанр'),
        ('episode', 'decade', 'Episode: лиги по десятилетиям', 'Выбери десятилетие'),
    ],
)
def test_league_picker_names_the_game_and_the_dimension(game, dimension, title, hint):
    rich = render_league_picker(game, dimension, [Choice(18, 'Drama'), Choice(35, 'Comedy')])
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == title
    assert hint in parsed.paragraphs[1]
    assert 'не меньше 15 раундов' in rich['html'] and 'не меньше 3' in rich['html']
    assert rich['skip_entity_detection'] is True


def test_league_picker_without_leagues_says_so():
    rich = render_league_picker('framed', 'genre', [])
    assert 'Пока нет ни одной лиги' in rich['html']
    assert 'Выбери' not in rich['html']


def test_league_takes_numbers_from_rows_and_names_from_profiles():
    league = League(
        'genre',
        18,
        'Drama & Co',
        (LeagueRow(5_000_000_123, 'Старое имя', 4.1234, 20.5, 0.6), LeagueRow(7, '李', 3.0, 15, 0.0)),
    )
    profiles = (
        _ResolvedTopResult(5_000_000_123, 'Свежее <имя>', 4.1234, 'fresh_owner'),
        _ResolvedTopResult(7, '李', 3.0, None),
    )
    rich = render_league('framed', league, profiles)
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == 'Framed: лига жанра Drama & Co'
    assert parsed.tables[0] == [
        ['#', 'Имя', 'Очки', 'Раунды', 'Победы'],
        ['1', 'Свежее <имя>', '4.12', '20.5', '60%'],
        ['2', '李', '3.00', '15', '0%'],
    ]
    assert parsed.links == [{'href': 'https://t.me/fresh_owner?profile'}, {'href': 'tg://user?id=7'}]
    assert '<table bordered striped compact>' in rich['html']
    assert rich['html'].index('не меньше 15 раундов') > rich['html'].index('</table>')
    assert rich['skip_entity_detection'] is True


def test_decade_league_title_and_rows_without_profiles_fall_back_to_the_rows():
    league = League('decade', 1990, '1990-е', (LeagueRow(7, 'Имя', 3.0, 15, 0.5),))
    rich = render_league('episode', league, league.rows)
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == 'Episode: лига десятилетия 1990-е'
    assert parsed.tables[0][1] == ['1', 'Имя', '3.00', '15', '50%']
    assert parsed.links == [{'href': 'tg://user?id=7'}]
    assert 'делит' not in rich['html']


def test_hard_titles_show_two_ranked_lists_with_years():
    titles = HardTitles(
        hardest=(TitleRow(10, 'Primer', 2004, 12, 0.08, 1.25), TitleRow(9, 'Старое & новое', None, 5, 0.2, 1.8)),
        easiest=(TitleRow(8, 'Titanic', 1997, 12, 1.0, 5.75),),
    )
    rich = render_hard_titles('framed', titles)
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[:3] == ['Framed: сложные фильмы', 'Самые сложные', 'Самые лёгкие']
    assert parsed.tables == [
        [
            ['#', 'Фильм', 'Игроки', 'Победы', 'Очки'],
            ['1', 'Primer (2004)', '12', '8%', '1.25'],
            ['2', 'Старое & новое', '5', '20%', '1.80'],
        ],
        [['#', 'Фильм', 'Игроки', 'Победы', 'Очки'], ['1', 'Titanic (1997)', '12', '100%', '5.75']],
    ]
    assert 'не меньше 5 игроков' in parsed.paragraphs[-1]
    assert rich['skip_entity_detection'] is True


def test_hard_titles_for_episode_use_the_series_noun_and_say_when_empty():
    rich = render_hard_titles('episode', HardTitles(hardest=(), easiest=()))
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == 'Episode: сложные сериалы'
    assert parsed.tables == []
    assert 'Пока нет раундов' in rich['html']


def test_rich_tables_does_not_load_the_database_or_the_configuration():
    code = 'import sys, rich_tables; sys.exit(int(any(m in sys.modules for m in ("insight", "models.db", "config"))))'
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, '-c', code], cwd=root, env={**os.environ}, check=False
    )
    assert result.returncode == 0
