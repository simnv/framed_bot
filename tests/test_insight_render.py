# Renderers on fixed inputs built from the insight dataclasses. No handler, no database.
import os
import re
import subprocess
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from answers import Game
from insight import Bucket, Choice, GameInsight, HardTitles, League, LeagueRow, Rank, Section, TitleRow
from rich_tables import (
    STATS_BLOCK_LIMIT,
    STATS_TEXT_LIMIT,
    _ResolvedTopResult,
    block_count,
    render_game_stats,
    render_hard_titles,
    render_league,
    render_league_picker,
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


def bucketed(label, strength, rounds=12.0, win_rate=0.5, role=None, key=1):
    return Bucket(
        key=key, label=label, rounds=rounds, points=3.0 + strength, win_rate=win_rate, role=role, strength=strength
    )


def headings(rich):
    return re.findall(r'<h[1-6]>(.*?)</h[1-6]>', rich['html'])


def text_of(fragment):
    return unescape(re.sub(r'<[^>]+>', '', fragment))


def test_game_stats_header_has_the_game_emoji_the_site_link_and_the_linked_owner():
    owner = _ResolvedTopResult(user_id=99, name='Имя & <b>', score=None, public_username='fresh_owner')
    framed = render_game_stats('framed', FRAMED, owner=owner)
    episode = render_game_stats('episode', EPISODE, owner=SimpleNamespace(user_id=99, name='Имя', score=None))
    assert framed['html'].startswith(
        '<h3>🎥 <a href="https://framed.wtf/">Framed</a> · '
        '<a href="https://t.me/fresh_owner?profile">Имя &amp; &lt;b&gt;</a></h3>'
    )
    assert episode['html'].startswith(
        '<h3>📺 <a href="https://episode.wtf/">Episode</a> · <a href="tg://user?id=99">Имя</a></h3>'
    )
    assert framed['skip_entity_detection'] is True


def test_game_stats_without_owner_or_valid_id_name_the_game_only_or_a_plain_name():
    assert headings(render_game_stats('framed', FRAMED)) == ['🎥 <a href="https://framed.wtf/">Framed</a>']
    plain = render_game_stats('framed', FRAMED, owner=SimpleNamespace(user_id=None, name='Имя', score=None))
    assert headings(plain) == ['🎥 <a href="https://framed.wtf/">Framed</a> · Имя']


def test_game_numbers_are_one_line_with_the_win_share_and_dashes_without_wins():
    framed = RichParser(render_game_stats('framed', Stats(1020, 735, 2.4123, 4630, 3.69))['html']).paragraphs
    episode = RichParser(render_game_stats('episode', Stats(4, 0, None, 4, 11.0))['html']).paragraphs
    assert framed[0] == 'Раунды 1020 · Победы 735 (72%) · Очки 4630 · Ср. кадр 2.41 в победах, 3.69 во всех раундах'
    assert episode[0] == 'Раунды 4 · Победы 0 (0%) · Очки 4 · Ср. кадр — в победах, 11.00 во всех раундах'


def test_streak_and_ranks_are_one_line_each_and_the_streak_needs_a_win():
    ranks = (
        Rank('score', 2, 14),
        Rank('frame', 4, 11),
        Rank('win', 2, 14),
        Rank('rounds', 3, 14),
        Rank('genre', 1, 6, 'Horror'),
        Rank('decade', 2, 5, '1990-е'),
    )
    paragraphs = RichParser(
        render_game_stats('framed', FRAMED, insight(current_streak=3, best_streak=17, ranks=ranks))['html']
    ).paragraphs
    assert paragraphs[1] == 'Серия побед: сейчас 3, лучшая 17'
    assert (
        paragraphs[2] == 'Места в /top: очки 2/14 · кадры 4/11 · фильмы 2/14 · участия 3/14 · Horror 1/6 · 1990-е 2/5'
    )
    episode = render_game_stats('episode', EPISODE, insight(ranks=(Rank('win', 1, 3),)))['html']
    assert 'Серия побед' not in episode
    assert 'сериалы 1/3' in episode


def test_genres_keep_the_query_order_and_name_the_strongest_and_weakest_two():
    genres = section(
        [
            bucketed('Drama', 0.123, rounds=260.7, win_rate=0.71),
            bucketed('Thriller', 0.3, rounds=88.3),
            bucketed('Horror', 0.42, rounds=41.5),
            bucketed('Animation', -0.374, rounds=30),
            bucketed('Musical', -0.6, rounds=6.5),
        ]
    )
    rich = render_game_stats('framed', FRAMED, insight(genres=genres))
    parsed = RichParser(rich['html'])
    assert '<h4>Жанры</h4>' in rich['html']
    assert parsed.tables[0] == [
        ['Жанр', 'Раунды', 'Победы', 'Сила'],
        ['Drama', '260.7', '71%', '+0.12'],
        ['Thriller', '88.3', '50%', '+0.30'],
        ['Horror', '41.5', '50%', '+0.42'],
        ['Animation', '30', '50%', '−0.37'],
        ['Musical', '6.5', '50%', '−0.60'],
    ]
    assert 'Сильнее всего: Horror, Thriller. Слабее всего: Musical, Animation.' in parsed.paragraphs


@pytest.mark.parametrize(
    ('labels', 'verdict'),
    [
        (['A'], None),
        (['A', 'B'], 'Сильнее всего: A. Слабее всего: B.'),
        (['A', 'B', 'C'], 'Сильнее всего: A. Слабее всего: C.'),
    ],
)
def test_short_genre_lists_name_one_strongest_and_one_weakest_or_nothing(labels, verdict):
    genres = section([bucketed(label, 1.0 - index) for index, label in enumerate(labels)])
    paragraphs = RichParser(render_game_stats('framed', FRAMED, insight(genres=genres))['html']).paragraphs
    verdicts = [paragraph for paragraph in paragraphs if paragraph.startswith('Сильнее')]
    assert verdicts == ([verdict] if verdict else [])


def test_decades_keep_the_strength_order_and_zero_strength_has_no_sign():
    decades = section([bucketed('1970-е', 0.5), bucketed('1990-е', 0.0), bucketed('2020-е', -0.004)])
    parsed = RichParser(render_game_stats('framed', FRAMED, insight(decades=decades))['html'])
    assert parsed.tables[0] == [
        ['Десятилетие', 'Раунды', 'Победы', 'Сила'],
        ['1970-е', '12', '50%', '+0.50'],
        ['1990-е', '12', '50%', '0.00'],
        ['2020-е', '12', '50%', '0.00'],
    ]


def test_people_show_roles_and_a_gap_row_between_the_best_and_the_worst():
    people = section(
        [bucketed('Nolan', 0.9, rounds=7, role='director'), bucketed('Hanks', 0.5, rounds=9, role='cast')],
        [bucketed('Bay', -0.8, rounds=4, role='director')],
    )
    parsed = RichParser(render_game_stats('framed', FRAMED, insight(people=people))['html'])
    assert parsed.tables[0] == [
        ['Кто', 'Роль', 'Раунды', 'Победы', 'Сила'],
        ['Nolan', 'режиссёр', '7', '50%', '+0.90'],
        ['Hanks', 'актёр', '9', '50%', '+0.50'],
        ['…', '…', '…', '…', '…'],
        ['Bay', 'режиссёр', '4', '50%', '−0.80'],
    ]
    creators = section([bucketed('Gilligan', 0.2, role='creator')])
    assert 'создатель' in render_game_stats('episode', EPISODE, insight(people=creators))['html']


def test_sections_come_in_order_and_empty_sections_are_hidden():
    full = insight(
        genres=section([bucketed('Drama', 0.1)]),
        decades=section([bucketed('1990-е', 0.1)]),
        people=section([bucketed('Nolan', 0.1, role='director')]),
    )
    assert re.findall(r'<h4>(.*?)</h4>', render_game_stats('framed', FRAMED, full)['html']) == [
        'Жанры',
        'Десятилетия',
        'Люди',
    ]
    only_decades = insight(decades=section([bucketed('1990-е', 0.1)]))
    assert re.findall(r'<h4>(.*?)</h4>', render_game_stats('framed', FRAMED, only_decades)['html']) == ['Десятилетия']


def test_every_message_ends_with_a_closed_note_that_explains_the_numbers_and_the_strength():
    rich = render_game_stats('episode', EPISODE, insight(genres=section([bucketed('Drama', 0.1)])))
    html = rich['html']
    assert html.endswith('</details>')
    note = html[html.rindex('<details>') :]
    assert '<details open' not in html
    assert '<summary>Как считаются числа</summary>' in note
    text = text_of(note)
    assert 'кадр 11' in text
    assert 'Сила' in text and '10' in text
    # The note gives a worked example of the smoothing.
    assert '2 раунда' in text


def test_game_stats_without_insight_keep_the_numbers_and_the_note():
    html = render_game_stats('framed', FRAMED)['html']
    assert '<h4>' not in html
    assert 'Раунды 9' in html
    assert html.endswith('</details>')


def huge_insight(width):
    many = [bucketed('G' * width + str(index), 0.1) for index in range(19)]
    people = section([bucketed('P' * width + str(index), 0.1, role='cast') for index in range(5)])
    return insight(genres=section(many), decades=section(many[:10]), people=people)


def test_a_normal_full_message_is_not_trimmed():
    rich = render_game_stats('framed', FRAMED, huge_insight(20))
    assert re.findall(r'<h4>(.*?)</h4>', rich['html']) == ['Жанры', 'Десятилетия', 'Люди']
    assert len(text_of(rich['html'])) <= STATS_TEXT_LIMIT


def test_a_message_over_the_text_limit_drops_the_people_then_all_insight():
    no_people = render_game_stats('framed', FRAMED, huge_insight(880))
    assert re.findall(r'<h4>(.*?)</h4>', no_people['html']) == ['Жанры', 'Десятилетия']
    assert len(text_of(no_people['html'])) <= STATS_TEXT_LIMIT
    nothing = render_game_stats('framed', FRAMED, huge_insight(3000))
    assert '<h4>' not in nothing['html']
    assert 'Раунды 9' in nothing['html']
    assert len(text_of(nothing['html'])) <= STATS_TEXT_LIMIT


def test_a_message_over_the_block_limit_drops_sections():
    many = section([bucketed(f'G{index}', 0.1) for index in range(300)])
    rich = render_game_stats('framed', FRAMED, insight(genres=many, decades=many))
    assert block_count(rich['html']) <= STATS_BLOCK_LIMIT


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
        (
            LeagueRow(5_000_000_123, 'Старое имя', 4.1234, 20.5, 0.6, 0.4567, 0.1),
            LeagueRow(7, '李', 3.0, 15, 0.0, -0.6667, 0.25),
        ),
    )
    profiles = (
        _ResolvedTopResult(5_000_000_123, 'Свежее <имя>', 4.1234, 'fresh_owner'),
        _ResolvedTopResult(7, '李', 3.0, None),
    )
    rich = render_league('framed', league, profiles)
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == 'Framed: лига жанра Drama & Co'
    assert parsed.tables[0] == [
        ['#', 'Имя', 'Раунды', 'Победы', 'Общая', 'Личная'],
        ['1', 'Свежее <имя>', '20.5', '60%', '+0.46', '+0.10'],
        ['2', '李', '15', '0%', '−0.67', '+0.25'],
    ]
    assert parsed.links == [{'href': 'https://t.me/fresh_owner?profile'}, {'href': 'tg://user?id=7'}]
    assert '<table bordered striped compact>' in rich['html']
    assert rich['html'].index('не меньше 15 раундов') > rich['html'].index('</table>')
    note = rich['html'][rich['html'].rindex('<details>') :]
    assert '<summary>Как считается сила</summary>' in note
    # The note explains both columns: against all players and against the own level.
    assert 'Общая' in text_of(note) and 'всех игроков' in text_of(note)
    assert 'Личная' in text_of(note) and '/stats' in text_of(note)
    assert rich['skip_entity_detection'] is True


def test_decade_league_title_and_rows_without_profiles_fall_back_to_the_rows():
    league = League('decade', 1990, '1990-е', (LeagueRow(7, 'Имя', 3.0, 15, 0.5, 0.25, -0.5),))
    rich = render_league('episode', league, league.rows)
    parsed = RichParser(rich['html'])
    assert parsed.paragraphs[0] == 'Episode: лига десятилетия 1990-е'
    assert parsed.tables[0][1] == ['1', 'Имя', '15', '50%', '+0.25', '−0.50']
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
