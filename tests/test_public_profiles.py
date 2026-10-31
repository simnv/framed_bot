"""Fresh public profile resolution with bounded Telegram reads and no live API calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import Forbidden, NetworkError

import rich_tables
from rich_tables import TopType, enrich_top_profiles, render_top


def row(user_id, *, username='stale_owner', name='Name <b>& 👩‍💻', score=2):
    return SimpleNamespace(user_id=user_id, username=username, name=name, score=score)


def chat(user_id, username='fresh_owner', kind='private'):
    return SimpleNamespace(id=user_id, type=kind, username=username)


@pytest.mark.asyncio
async def test_fresh_verified_username_replaces_stale_db_username_and_is_not_cached():
    get_chat = AsyncMock(side_effect=[chat(42, 'new_owner'), chat(42, 'renamed_owner')])
    bot = Mock(get_chat=get_chat)
    results = [row(42)]
    first = await enrich_top_profiles(bot, results)
    second = await enrich_top_profiles(bot, results)
    assert isinstance(first, tuple)
    assert first[0].name == results[0].name and first[0].score == 2 and first[0].user_id == 42
    assert 'https://t.me/new_owner?profile' in render_top(TopType.TOP_SCORE, first)['html']
    assert 'https://t.me/renamed_owner?profile' in render_top(TopType.TOP_SCORE, second)['html']
    assert 'stale_owner' not in render_top(TopType.TOP_SCORE, first)['html']
    assert 'https://t.me/' not in render_top(TopType.TOP_SCORE, results)['html']
    assert '&lt;b&gt;&amp;' in render_top(TopType.TOP_SCORE, first)['html']
    assert get_chat.await_count == 2
    for call in get_chat.await_args_list:
        assert call.args == (42,)
        assert call.kwargs == {'read_timeout': 2, 'write_timeout': 2, 'connect_timeout': 2, 'pool_timeout': 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'response',
    [
        chat(43),
        chat(True),
        chat('42'),
        chat(42, kind='group'),
        None,
        chat(42, None),
        chat(42, ''),
        chat(42, 'bad/name'),
        chat(42, 'evil?x=1'),
        chat(42, 'evil" onclick="x'),
        chat(42, 'юзер'),
        chat(42, 'a' * 33),
        Forbidden('denied'),
        NetworkError('unavailable'),
        TypeError('malformed'),
        ValueError('malformed'),
        AttributeError('malformed'),
    ],
)
async def test_invalid_or_unavailable_lookup_keeps_id_fallback(response):
    lookup = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
    enriched = await enrich_top_profiles(Mock(get_chat=lookup), [row(42)])
    html = render_top(TopType.TOP_SCORE, enriched)['html']
    assert 'href="tg://user?id=42"' in html
    assert 'https://t.me/' not in html


@pytest.mark.asyncio
@pytest.mark.parametrize('username', ['a', 'A_9', 'a' * 32])
async def test_safe_collectible_and_regular_username_paths(username):
    enriched = await enrich_top_profiles(Mock(get_chat=AsyncMock(return_value=chat(42, username))), [row(42)])
    assert f'href="https://t.me/{username}?profile"' in render_top(TopType.TOP_SCORE, enriched)['html']


@pytest.mark.asyncio
async def test_duplicate_ids_coalesce_and_invalid_ids_do_not_trigger_lookup():
    lookup = AsyncMock(side_effect=lambda user_id, **_kwargs: chat(user_id))
    rows = [row(42), row(None), row(True), row(0), row(-1), row('42'), row(42), row(43)]
    enriched = await enrich_top_profiles(Mock(get_chat=lookup), rows)
    assert [call.args[0] for call in lookup.await_args_list] == [42, 43]
    assert [result.user_id for result in enriched] == [result.user_id for result in rows]
    assert [result.score for result in enriched] == [result.score for result in rows]


@pytest.mark.asyncio
async def test_lookup_batch_caps_at_ten_rows_and_three_concurrent_requests():
    active, peak = 0, 0
    calls = []

    async def lookup(user_id, **_kwargs):
        nonlocal active, peak
        calls.append(user_id)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return chat(user_id)
        finally:
            active -= 1

    rows = [row(user_id) for user_id in range(1, 13)]
    enriched = await enrich_top_profiles(Mock(get_chat=lookup), rows)
    assert calls == list(range(1, 11))
    assert peak == 3 and active == 0
    assert len(enriched) == 12
    assert 'href="tg://user?id=12"' in render_top(TopType.TOP_SCORE, enriched)['html']


@pytest.mark.asyncio
async def test_deadline_preserves_completed_lookup_and_cleans_up_remaining_requests(monkeypatch):
    monkeypatch.setattr(rich_tables, '_PROFILE_LOOKUP_DEADLINE', 0.02)
    started, finished = [], []

    async def lookup(user_id, **_kwargs):
        started.append(user_id)
        try:
            if user_id == 1:
                return chat(1, 'finished')
            await asyncio.Event().wait()
        finally:
            finished.append(user_id)

    enriched = await asyncio.wait_for(
        enrich_top_profiles(Mock(get_chat=lookup), [row(i) for i in range(1, 11)]),
        timeout=1,
    )
    assert started == finished
    html = render_top(TopType.TOP_SCORE, enriched)['html']
    assert 'https://t.me/finished?profile' in html
    assert 'href="tg://user?id=2"' in html
    assert len(started) <= 4


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_after_child_requests_are_cleaned_up():
    started, finished = [], []
    ready = asyncio.Event()

    async def lookup(user_id, **_kwargs):
        started.append(user_id)
        if len(started) == 3:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(user_id)

    task = asyncio.create_task(enrich_top_profiles(Mock(get_chat=lookup), [row(i) for i in range(1, 11)]))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(started) == set(finished) == {1, 2, 3}


@pytest.mark.asyncio
async def test_unhashable_invalid_id_uses_plain_name_without_lookup():
    lookup = AsyncMock()
    enriched = await enrich_top_profiles(Mock(get_chat=lookup), [row([])])
    lookup.assert_not_awaited()
    assert '<a ' not in render_top(TopType.TOP_SCORE, enriched)['html']
