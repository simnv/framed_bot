"""Render the period top on fixed inputs."""

from types import SimpleNamespace

from rich_tables import block_count, render_period_top
from top_type import TopType

HEADING = '🏆 Framed — итоги октября 2026'
LINE = 'Раунды #1665–#1695, 1–31 октября'


def row(user_id, name, score):
    return SimpleNamespace(user_id=user_id, name=name, score=score)


def test_period_top_has_heading_round_line_and_four_boards_without_buttons():
    boards = {
        TopType.TOP_SCORE: [row(1, 'A <b>', 60), row(2, 'B', 59)],
        TopType.TOP_FRAME: [],
        TopType.TOP_WIN: [row(2, 'B', 10)],
        TopType.TOP_ROUNDS: [row(1, 'A <b>', 12)],
    }

    rich = render_period_top('framed', HEADING, LINE, boards, frame_gate=5)
    html = rich['html']

    assert rich['skip_entity_detection'] is True
    assert html.startswith(f'<h3>{HEADING}</h3><p>{LINE}</p>')
    assert html.count('<table') == 4
    assert html.index('Очки') < html.index('Средний кадр') < html.index('Фильмы') < html.index('Участия')
    assert 'A &lt;b&gt;' in html and '<a href="tg://user?id=1">' in html
    assert 'не меньше 5 раундов' in html
    assert 'Пока нет результатов' in html.split('Средний кадр', 1)[1].split('Фильмы', 1)[0]
    assert '<button' not in html and 'callback' not in html
    assert block_count(html) < 500


def test_period_top_uses_the_episode_win_label_and_formats_the_average_frame():
    boards = {top_type: [row(1, 'A', 3.456)] for top_type in TopType}

    heading = '🏆 Episode — итоги 2026 года'
    line = 'Раунды #1–#2, 1 января – 30 декабря'

    html = render_period_top('episode', heading, line, boards, frame_gate=10)['html']

    assert 'Сериалы' in html and '3.46' in html and 'не меньше 10 раундов' in html
