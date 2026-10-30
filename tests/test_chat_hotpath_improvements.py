"""Live revocation and bounded image preparation on real local chat paths."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from shiori.chat import ChatService
from shiori.context import ContextEngine
from shiori.db import Store, message_fingerprint
from test_reply_timings import Config, Model, anyio_backend
from test_context_v3 import Provider, config as context_config, history
from test_message_debounce import setup, send, drain
from test_mention_reply_modes import configure


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1kAAAAASUVORK5CYII=')
URLS = [f'https://multimedia.nt.qq.com.cn/download?fileid={i}' for i in range(3)]


def note(key):
    now = datetime.now(timezone.utc)
    return {'id': key, 'persona_id': 'default', 'title': '音乐资讯', 'interest': '音乐',
            'text': '音乐节将在本周末举办', 'url': f'https://example.com/{key}',
            'status': 'active', 'retrieved_at': now.isoformat(),
            'valid_until': (now + timedelta(days=2)).isoformat()}


@pytest.mark.anyio
async def test_knowledge_reference_indexes_once_and_checks_live_revocations(tmp_path, monkeypatch):
    store, cfg = Store(tmp_path), Config()
    chat = ChatService(store, cfg, Model())
    knowledge = chat.knowledge

    class CountedList(list):
        scans = 0
        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    rows = CountedList(note(str(i)) for i in range(800))
    knowledge._state['items'] = rows
    ref = {'42': chat._knowledge_fingerprint(rows[42])}
    def no_full_list(*args, **kwargs):
        raise AssertionError('Reference validation must not list or copy the library')
    monkeypatch.setattr(knowledge, 'list', no_full_list)
    try:
        for _ in range(30):
            assert not chat._knowledge_changed('default', ref)
        assert rows.scans == 1
        knowledge._state['sources']['default'] = {'blocked_domains': ['example.com']}
        assert chat._knowledge_changed('default', ref)
        knowledge._state['sources']['default'] = {}
        assert not chat._knowledge_changed('default', ref)
        snapshot = knowledge.referenced('default', ref)
        snapshot['42']['text'] = 'Caller cannot mutate canonical records'
        assert not chat._knowledge_changed('default', ref)
        rows[42]['valid_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        assert chat._knowledge_changed('default', ref)
        rows[42] = note('42')
        knowledge._lookup_cache = None
        ref = {'42': chat._knowledge_fingerprint(rows[42])}
        knowledge.delete('42')
        assert chat._knowledge_changed('default', ref)
        assert chat._knowledge_changed('deleted-persona', ref)
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_failed_knowledge_write_does_not_leave_index_pointing_at_discarded_state(tmp_path, monkeypatch):
    store, cfg = Store(tmp_path), Config()
    chat = ChatService(store, cfg, Model())
    knowledge = chat.knowledge
    knowledge._state['items'] = [note('k1')]
    ref = {'k1': chat._knowledge_fingerprint(knowledge._state['items'][0])}
    assert not chat._knowledge_changed('default', ref)
    def fail():
        raise OSError('fixture disk full')
    monkeypatch.setattr(knowledge, '_save', fail)
    try:
        with pytest.raises(OSError):
            knowledge.delete('k1')
        assert not chat._knowledge_changed('default', ref)
    finally:
        await chat.close()
        store.close()


class ImageDownloads:
    def __init__(self, loop):
        self.release = threading.Event()
        self.entered = asyncio.Event()
        self.loop = loop
        self.lock = threading.Lock()
        self.count = 0

    def __call__(self, url, **kwargs):
        assert kwargs['max_bytes'] == 5 * 1024 * 1024
        with self.lock:
            self.count += 1
            if self.count == 3:
                self.loop.call_soon_threadsafe(self.entered.set)
        assert self.release.wait(3), 'fixture worker was not released'
        return PNG, 'image/png'


@pytest.mark.anyio
async def test_qq_images_download_concurrently_outside_conversation_lock(tmp_path, monkeypatch):
    store, cfg = Store(tmp_path), Config()
    cfg.value['model']['vision'] = True
    scope = 'qq:private:303'
    store.upsert_scope({'id': scope, 'auto_memory': False})
    chat = ChatService(store, cfg, Model())
    downloads = ImageDownloads(asyncio.get_running_loop())
    monkeypatch.setattr('shiori.stickers.download_image', downloads)
    task = asyncio.create_task(chat.respond(scope, '303', '看看这三张图', images=URLS))
    try:
        await asyncio.wait_for(downloads.entered.wait(), 1)
        assert downloads.count == 3 and not chat._locks[scope].locked()
        plain = await asyncio.wait_for(chat.respond(scope, '303', '先聊聊音乐'), 1)
        assert plain['reply'] and not task.done()
        downloads.release.set()
        assert (await asyncio.wait_for(task, 2))['reply']
    finally:
        downloads.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_image_batch_deadline_and_cancel_cannot_multiply_blocked_workers(tmp_path, monkeypatch):
    store, cfg = Store(tmp_path), Config()
    chat = ChatService(store, cfg, Model())
    downloads = ImageDownloads(asyncio.get_running_loop())
    monkeypatch.setattr('shiori.stickers.download_image', downloads)
    monkeypatch.setattr('shiori.chat.QQ_IMAGE_BATCH_TIMEOUT', .08)
    first = asyncio.create_task(chat._prepare_qq_images('qq:private:303', URLS))
    second = None
    try:
        await asyncio.wait_for(downloads.entered.wait(), 1)
        with pytest.raises(ValueError, match='QQ图片未能读取'):
            await asyncio.wait_for(first, .5)
        second = asyncio.create_task(chat._prepare_qq_images('qq:private:303', URLS))
        with pytest.raises(ValueError, match='QQ图片未能读取'):
            await asyncio.wait_for(second, .5)
        assert downloads.count == 3  # Timed-out socket threads still own their slots.
        downloads.release.set()
        prepared = await asyncio.wait_for(chat._prepare_qq_images('qq:private:303', URLS), 1)
        assert downloads.count == 6 and all(item.startswith('data:image/png;base64,') for item in prepared)
    finally:
        downloads.release.set()
        await asyncio.gather(*[task for task in (first, second) if task], return_exceptions=True)
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_image_source_changes_during_prefetch_prevent_model_dispatch(tmp_path, monkeypatch):
    store, cfg = Store(tmp_path), Config()
    cfg.value['model']['vision'] = True
    scope = 'qq:private:303'
    chat = ChatService(store, cfg, Model())
    msg = store.add_message(scope, '303', 'user', '图片内容是什么', images=URLS)
    downloads = ImageDownloads(asyncio.get_running_loop())
    monkeypatch.setattr('shiori.stickers.download_image', downloads)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Changed source must not reach a model')
    monkeypatch.setattr(chat.provider, 'complete', forbidden)
    task = asyncio.create_task(chat.respond(scope, '303', msg['content'], images=URLS,
                                           metadata={'existing_message_id': msg['id']}))
    try:
        await asyncio.wait_for(downloads.entered.wait(), 1)
        with store._lock:
            store._conn.execute("UPDATE messages SET data=json_set(data, '$.content', ?) WHERE id=?", ('已撤回这条问题', msg['id']))
            store._conn.commit()
        downloads.release.set()
        result = await asyncio.wait_for(task, 1)
        assert result['interrupted'] and not result['reply']
    finally:
        downloads.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_failure_feedback_stays_visible_but_never_enters_chat_or_digest(tmp_path):
    store, cfg, provider = Store(tmp_path), Config(), Provider()
    chat = ChatService(store, cfg, Model())
    scope = 'web:default'
    rows = history(store, 10)
    error = store.add_message(scope, 'assistant', 'assistant', 'FAILED_OPERATION_SENTINEL',
                              failure_feedback=True, source_ids=[rows[-2]['id']])
    rows.extend([error, *history(store, 4)])
    context = ContextEngine(store, context_config(), provider)
    try:
        assert store.get_message(error['id'])['content'] == 'FAILED_OPERATION_SENTINEL'
        assert error['id'] not in {row['id'] for row in chat._history(scope, user_id='owner')}
        assert error['id'] not in context.reference(scope, 'owner', rows)['source_ids']
        result = await context.compact(scope, 'owner', history=rows)
        assert result['compressed']
        assert error['id'] not in result['summary']['source_ids']
        assert 'FAILED_OPERATION_SENTINEL' not in json.dumps([messages for messages, _ in provider.calls])
        assert store.get_message(error['id'])['metadata']['failure_feedback'] is True
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_old_digest_containing_failure_receipt_is_ignored_without_deleting_history(tmp_path):
    store, cfg, provider = Store(tmp_path), Config(), Provider()
    cfg.value['context']['enabled'] = True
    chat = ChatService(store, cfg, Model())
    scope = 'web:default'
    store.upsert_scope({'id': scope, 'context_compress': True})
    rows = history(store, 10)
    error = store.add_message(scope, 'assistant', 'assistant', 'FAILED_OPERATION_SENTINEL',
                              failure_feedback=True, source_ids=[rows[-2]['id']])
    sources = rows[:4] + [error]
    digest = store.create_memory({'scope_id': scope, 'user_id': 'owner', 'kind': 'event',
        'property': 'conversation_digest', 'derived': True, 'manual_digest': True,
        'content': 'OLD_DIGEST_CONTAINS_FAILED_OPERATION',
        'source_ids': [row['id'] for row in sources],
        'source_fingerprints': {row['id']: message_fingerprint(row) for row in sources},
        'covered_message_ids': [row['id'] for row in sources]})
    context = ContextEngine(store, context_config(), provider)
    try:
        assert context._latest_summary(scope, 'owner') is None
        assert not chat._reference_allowed(digest, scope, 'owner')
        restored = context._result(scope, 'owner', rows[4:], digest)
        assert restored['summary'] is None
        assert [row['id'] for row in restored['messages']] == [row['id'] for row in rows]
        rebuilt = await context.compact(scope, 'owner', history=[*rows, error])
        assert rebuilt['compressed'] and rebuilt['summary']['id'] != digest['id']
        prompt = json.dumps([messages for messages, _ in provider.calls])
        assert 'FAILED_OPERATION' not in prompt
        assert error['id'] not in rebuilt['summary']['source_ids']
        assert store.get_memory(digest['id'])['content'] == 'OLD_DIGEST_CONTAINS_FAILED_OPERATION'
        assert store.get_message(error['id'])['metadata']['failure_feedback'] is True
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_group_always_reply_combines_members_without_planner_or_new_authority(setup):
    _, _, chat, flow = configure(setup, always_reply=True)
    try:
        await send(flow, '大家今天吃什么', direct=False, user='303')
        await send(flow, '我想吃拉面', direct=False, user='404')
        await drain(flow)
        assert len(chat.calls) == 1 and not chat.provider.calls
        assert chat.calls[0][2] == '大家今天吃什么\n我想吃拉面'
        assert chat.calls[0][4]['trusted_identity'] is None
        assert not flow._planner_batches
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('restriction', ['paused', 'scope', 'admission'])
async def test_group_always_reply_respects_live_admission(setup, restriction):
    store, cfg, chat, flow = configure(setup, always_reply=True)
    try:
        await send(flow, '普通群聊也会进入防抖', direct=False)
        if restriction == 'paused':
            cfg.config['paused'] = True
        elif restriction == 'scope':
            store.upsert_scope({'id': 'qq:group:202', 'enabled': False})
        else:
            cfg.config['onebot']['blocked_groups'] = ['202']
        await drain(flow)
        assert not chat.calls and not chat.provider.calls
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_group_always_reply_respects_schedule_hold(setup):
    _, _, chat, flow = configure(setup, always_reply=True)
    held = []
    class Schedule:
        async def capture(self, message, **kwargs):
            held.append(message)
            return True
    flow.schedule_delivery = Schedule()
    try:
        result = await send(flow, '在忙的时候先等你', direct=False, wait=True)
        assert result['code'] == 'schedule_buffered' and len(held) == 1
        assert not chat.calls and not chat.provider.calls
    finally:
        await flow.close()
