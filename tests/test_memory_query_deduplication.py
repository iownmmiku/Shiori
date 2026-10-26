"""Avoid repeating the same reply retrieval while preserving useful queries."""
from types import SimpleNamespace

import pytest

from mikubot.chat import ChatService


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def recall_service():
    calls = []

    async def recall(scope_id, user_id, query, limit, *, semantic_deadline):
        calls.append((scope_id, user_id, query, limit, semantic_deadline))
        return [{'id': query.strip(), 'content': query.strip()}]

    preferences = [{'id': 'preference', 'content': '喜欢绿茶'}]
    chat = object.__new__(ChatService)
    chat.settings = SimpleNamespace(get=lambda: {'memory': {'top_k': 8, 'semantic_timeout_seconds': 8}})
    chat.memory = SimpleNamespace(recall=recall)
    chat.social = SimpleNamespace(_allowed=lambda scope_id: True)
    def profile_preferences(*args, scope_allowed):
        assert scope_allowed is chat.social._allowed
        return preferences
    chat.store = SimpleNamespace(profile_preferences=profile_preferences)
    return chat, calls


@pytest.mark.anyio
@pytest.mark.parametrize('needed_info', [
    ['咖啡', '咖啡', '咖啡'],
    ['  咖啡  ', '\n咖啡\t', '咖啡'],
    '咖啡',
])
async def test_query_repeated_by_planner_is_retrieved_once(recall_service, needed_info):
    chat, calls = recall_service
    result = await chat._recall_context('qq:group:test', 'member', '咖啡', needed_info)
    assert len(calls) == 1
    assert calls[0][:4] == ('qq:group:test', 'member', '咖啡', 8)
    assert [item['id'] for item in result] == ['preference', '咖啡']


@pytest.mark.anyio
async def test_exact_duplicates_are_skipped_without_expanding_the_three_input_limit(recall_service):
    chat, calls = recall_service
    await chat._recall_context('qq:group:test', 'member', '咖啡',
                               ['咖啡', ' 咖啡 ', '旅行计划', '旅行计划', '音乐节', '饮食偏好', '第四条不再查'])
    assert [call[2] for call in calls] == ['咖啡', '旅行计划']
    assert [call[3] for call in calls] == [8, 3]
    assert len({id(call[4]) for call in calls}) == 1


@pytest.mark.anyio
async def test_meaningful_case_and_internal_whitespace_variants_are_kept(recall_service):
    chat, calls = recall_service
    await chat._recall_context('qq:group:test', 'member', 'US policy',
                               ['us policy', 'US  policy', 'US\npolicy'])
    assert [call[2] for call in calls] == ['US policy', 'us policy', 'US  policy', 'US\npolicy']


@pytest.mark.parametrize('value', [None, 1, True, {}, (1, 2)])
def test_invalid_query_container_remains_empty(value):
    assert ChatService._needed_queries(value) == []


@pytest.mark.anyio
async def test_identical_long_current_query_is_compared_before_clipping(recall_service):
    chat, calls = recall_service
    text = '长查询' * 150
    await chat._recall_context('qq:group:test', 'member', text, [text, text + ' ', '不同查询'])
    assert [call[2] for call in calls] == [text, '不同查询']


@pytest.mark.anyio
async def test_distinct_long_queries_keep_the_existing_300_character_bound(recall_service):
    chat, calls = recall_service
    prefix = '共享前缀' * 100
    await chat._recall_context('qq:group:test', 'member', '问题', [prefix + '甲', prefix + '乙'])
    # Compare identity on the original input; clipping is only the dispatch bound.
    assert [call[2] for call in calls] == ['问题', prefix[:300], prefix[:300]]
    assert [call[3] for call in calls] == [8, 3, 3]
