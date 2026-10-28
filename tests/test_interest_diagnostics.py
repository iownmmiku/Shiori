"""Persistent, bounded exploration diagnostics without model/network calls."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from shiori.db import Store
from shiori.knowledge import PersonaKnowledge
import shiori.knowledge as module


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.save_persona({'id': 'music', 'name': '兴趣测试', 'system_prompt': '自然交流',
                       'interests': ['音乐'], 'exploration_enabled': True})
    cfg = {'paused': False, 'time': {'timezone': 'Asia/Shanghai'},
           'exploration': {'enabled': True, 'interval_hours': 24, 'daily_searches': 6, 'max_results': 8, 'valid_days': 90}}
    async def search(query):
        return {'links': ['https://public.example/music']}
    async def read(url):
        return {'url': url, 'title': '音乐创作的新进展', 'text': '音乐团队分享歌曲编排和乐器录制的方法，介绍演唱与音乐制作的协作流程，并讨论如何安排合奏。' * 3,
                'published_at': datetime.now(timezone.utc).isoformat()}
    service = PersonaKnowledge(store, cfg, None, search, read)
    try:
        yield store, cfg, service
    finally:
        store.close()


@pytest.mark.anyio
async def test_completed_exploration_diagnostics_survive_restart_and_export(env):
    store, cfg, service = env
    cfg['exploration']['enabled'] = False  # Manual work remains possible.
    result = await service.explore('music', manual=True)
    detail = result['last_result']
    assert detail['created'] == 1 and detail['pending'] == 1
    assert detail['reason'] == 'collected' and detail['interest'] == '音乐'
    assert '音乐' in detail['query'] and datetime.fromisoformat(detail['at']).tzinfo
    restored = PersonaKnowledge(store, cfg, None)
    status = restored.status()['personas']['music']
    assert status['last_result'] == detail
    assert status['disabled_reason'] == 'global_disabled'
    assert not status['enabled']
    PersonaKnowledge.validate_state(json.loads(restored.export_state()))


@pytest.mark.anyio
async def test_rejections_are_bounded_and_never_persist_source_body_or_url_query_secrets(env):
    store, cfg, service = env
    async def search(query):
        return {'links': ['https://public.example/page/' + str(i) + '?token=PRIVATE_QUERY' for i in range(24)]}
    async def read(url):
        return {'url': url, 'title': '手柄连接设置',
                'text': 'PRIVATE_PAGE_BODY 这里介绍手柄连接、设备驱动更新、控制器按键映射设置和硬件故障排查的方法。' * 3}
    service.search_fn, service.read_fn = search, read
    result = await service.explore('music', manual=True)
    detail = result['last_result']
    assert detail['reason'] == 'no_relevant_sources'
    assert detail['filtered'] == 24 and len(detail['rejections']) == 12 and detail['truncated']
    assert len(result['rejections']) == 12
    encoded = service.export_state()
    assert 'PRIVATE_QUERY' not in encoded and 'PRIVATE_PAGE_BODY' not in encoded
    assert all('?' not in item['url'] for item in detail['rejections'])
    assert PersonaKnowledge(store, cfg, None).status()['personas']['music']['last_result'] == detail


@pytest.mark.anyio
async def test_cancelled_attempt_is_not_left_as_running_after_restart(env):
    store, cfg, service = env
    started = asyncio.Event()
    async def waiting(url):
        started.set()
        await asyncio.Event().wait()
    service.read_fn = waiting
    task = asyncio.create_task(service.explore('music', manual=True))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = PersonaKnowledge(store, cfg, None).status()['personas']['music']
    assert state['last_result']['reason'] == 'cancelled'
    assert '正在探索' not in state['last_message']
    assert state['last_result']['created'] == 0 and state['searches_today'] == 1


@pytest.mark.anyio
async def test_configuration_change_during_search_is_recorded_without_importing_pages(env):
    _, cfg, service = env
    async def search(query):
        cfg['paused'] = True
        return {'links': ['https://public.example/music']}
    async def must_not_read(url):
        raise AssertionError('No page may be read after permission changes')
    service.search_fn, service.read_fn = search, must_not_read
    result = await service.explore('music', manual=True)
    assert result['created'] == 0 and result['last_result']['reason'] == 'cancelled'
    assert service.status()['personas']['music']['disabled_reason'] == 'paused'


def test_legacy_history_is_not_given_fabricated_diagnostics(env):
    _, _, service = env
    service._state['personas']['music'] = {'last_message': '旧的探索结果',
        'last_attempt_at': '2026-10-24T17:58:00+08:00', 'cursor': 0}
    exported = json.loads(service.export_state())
    PersonaKnowledge.validate_state(exported)
    status = service.status()['personas']['music']
    assert status['last_attempt_at'].startswith('2026-10-24')
    assert 'last_result' not in status


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['read', 'model'])
async def test_configuration_change_during_page_or_summary_finishes_cancelled(env, phase):
    store, cfg, service = env
    read = service.read_fn
    if phase == 'read':
        async def stop_during_read(url):
            result = await read(url)
            cfg['paused'] = True
            return result
        service.read_fn = stop_during_read
    else:
        class SummaryModel:
            async def complete(self, *args, **kwargs):
                cfg['paused'] = True
                return {'content': '音乐团队合作完成歌曲的编排，讨论音乐创作的方法以及乐器录制过程。' * 3}
        service.provider = SummaryModel()
    result = await service.explore('music', manual=True)
    assert result['last_result']['reason'] == 'cancelled' and result['created'] == 0
    assert not service.list('music', status='')
    saved = PersonaKnowledge(store, cfg, None).status()['personas']['music']
    assert saved['last_result'] == result['last_result']
    assert '正在探索' not in saved['last_message']


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(reason=[]), lambda value: value.update(at='unknown'),
    lambda value: value.update(filtered=True), lambda value: value.update(created=-1),
    lambda value: value.update(rejections=[{'url': 'file:///secret', 'reason': 'bad'}]),
    lambda value: value.update(rejections=[{'url': 'https://user:secret@example.org', 'reason': 'bad'}]),
    lambda value: value.update(rejections=[{'url': 'http://127.0.0.1', 'reason': 'bad'}]),
    lambda value: value.pop('query'), lambda value: value.update(truncated='false'),
])
def test_backup_validation_rejects_invalid_diagnostic_data(env, mutation):
    _, _, service = env
    detail = service._exploration_result('音乐', '音乐', reason='no_sources')
    mutation(detail)
    state = json.loads(service.export_state())
    state['personas']['music'] = {'last_result': detail}
    with pytest.raises(ValueError):
        PersonaKnowledge.validate_state(state)


def test_status_explains_each_gate_without_changing_switches_or_quota(env):
    store, cfg, service = env
    assert service.status()['personas']['music']['disabled_reason'] == ''
    cfg['paused'] = True
    assert service.status()['personas']['music']['disabled_reason'] == 'paused'
    cfg['paused'] = False
    cfg['exploration']['enabled'] = False
    assert service.status()['personas']['music']['disabled_reason'] == 'global_disabled'
    cfg['exploration']['enabled'] = True
    store.save_persona({'id': 'music', 'exploration_enabled': False})
    assert service.status()['personas']['music']['disabled_reason'] == 'persona_disabled'
    store.save_persona({'id': 'music', 'exploration_enabled': True, 'interests': []})
    assert service.status()['personas']['music']['disabled_reason'] == 'no_interests'
    store.save_persona({'id': 'music', 'interests': ['音乐']})
    store.save_persona({'id': 'music', 'excluded_topics': ['音乐']})
    assert service.status()['personas']['music']['disabled_reason'] == 'excluded_topics'
    store.save_persona({'id': 'music', 'interests': ['音乐与动画']})
    assert service.status()['personas']['music']['disabled_reason'] == ''
    store.save_persona({'id': 'music', 'excluded_topics': []})
    cfg['exploration']['daily_searches'] = 0
    assert service.status()['personas']['music']['disabled_reason'] == 'daily_limit'
    assert service.status()['searches_today'] == 0


def test_total_diagnostic_storage_keeps_recent_records_and_retains_legacy_messages(env, monkeypatch):
    _, _, service = env
    monkeypatch.setattr(module, 'MAX_EXPLORATION_DIAGNOSTIC_BYTES', 2500)
    for i in range(12):
        detail = service._exploration_result('音乐', '音乐', rejected=[
            {'url': 'https://public.example/' + str(i) + '/' + 'x' * 600, 'reason': '网页主段与兴趣主题关联不足'}],
            reason='no_relevant_sources')
        service._finish('historical-' + str(i), '历史探索结果', result=detail)
    state = json.loads(service.export_state())
    kept = [value['last_result'] for value in state['personas'].values() if 'last_result' in value]
    assert 0 < len(kept) < 12
    assert sum(len(json.dumps(value, ensure_ascii=False).encode('utf-8')) for value in kept) <= 2500
    assert 'last_result' in state['personas']['historical-11']
    assert all(value['last_message'] == '历史探索结果' for value in state['personas'].values())


def test_diagnostic_details_yield_to_whole_file_capacity(env, monkeypatch):
    store, cfg, service = env
    service._state['personas']['music'] = {'last_message': '正在探索公开网页。'}
    service._save()
    base_bytes = len(service.export_state().encode('utf-8'))
    monkeypatch.setattr(module, 'MAX_FILE_BYTES', base_bytes + 160)
    detail = service._exploration_result('音乐', '音乐', rejected=[
        {'url': 'https://public.example/' + 'x' * 700, 'reason': '网页主段与兴趣主题关联不足'}],
        reason='no_relevant_sources')
    service._finish('music', '探索完成，未取得可用资料。', success=True, result=detail)
    saved = PersonaKnowledge(store, cfg, None).status()['personas']['music']
    assert saved['last_message'] == '探索完成，未取得可用资料。'
    assert saved['last_success_at'] and 'last_result' not in saved
    assert len(service.export_state().encode('utf-8')) <= module.MAX_FILE_BYTES
