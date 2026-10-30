"""Connector file resolution must not run ahead of message collection."""
import asyncio
import base64
import json

import pytest

from shiori.chat import ChatService, validate_images
from shiori.db import Store
from shiori.onebot import OneBotBridge
from shiori.settings import Settings


SCOPE = 'qq:group:202'
RAW = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings, store = Settings(tmp_path), Store(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'vision', 'vision': True},
        'onebot': {'enabled': True, 'chat_policy': 'blacklist', 'access_token': 'fixture-onebot-token'},
        'debounce': {'enabled': True}, 'stickers': {'enabled': False},
        'memory': {'organization_enabled': False}})
    store.upsert_scope({'id': SCOPE, 'auto_memory': False, 'plan_enabled': False, 'merge_window_ms': 0})
    calls, sent = [], []
    class Model:
        async def complete(self, messages, **kwargs):
            assert kwargs['before_send']()
            calls.append(messages)
            return {'content': '已一起看完。', 'message': {'role': 'assistant', 'content': '已一起看完。'},
                    'tool_calls': [], 'usage': {}}
    chat = ChatService(store, settings, Model())
    bridge = OneBotBridge(store, settings, chat)
    bridge._self_id = '999'
    async def send(scope, text, **kwargs):
        sent.append(text)
        return True
    bridge.send = send
    monkeypatch.setattr('shiori.stickers.download_image', lambda *a, **kw: (RAW, 'image/png'))
    yield store, settings, chat, bridge, calls, sent
    store.close()


def event(identifier, text, *, images=(), user=101):
    return {'post_type': 'message', 'message_type': 'group', 'group_id': 202,
            'self_id': 999, 'user_id': user, 'message_id': identifier,
            'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': text}}]
                       + [{'type': 'image', 'data': {'file': image}} for image in images]}


async def close(runtime):
    _, _, chat, bridge, _, _ = runtime
    bridge._socket = None
    await bridge.stop()
    await chat.close()


@pytest.mark.anyio
async def test_file_image_is_stored_in_order_and_merged_before_lookup(runtime):
    store, _, _, bridge, calls, _ = runtime
    collection, lookup, image_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    async def collect(*args):
        await collection.wait()
    bridge._flow._wait_debounce = collect
    class Socket:
        async def send_json(self, action):
            requests.append(action)
            lookup.set()
            await image_ready.wait()
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0,
                'data': {'url': 'https://gchat.qpic.cn/fixture.png'}})
    bridge._socket = Socket()
    try:
        await bridge.handle_event(event(1, '先看这张图', images=['fixture-image.png']), wait=False)
        await bridge.handle_event(event(2, '再一起解释一下', user=102), wait=False)
        await bridge.handle_event(event(1, '先看这张图', images=['fixture-image.png']), wait=False)
        messages = [m for m in store.list_messages(SCOPE) if m['role'] == 'user']
        assert [m['metadata']['platform_message_id'] for m in messages] == ['1', '2']
        assert messages[0]['metadata']['images'] == ['onebot-image:999:fixture-image.png']
        assert not requests
        collection.set()
        await asyncio.wait_for(lookup.wait(), 3)
        image_ready.set()
        await asyncio.wait_for(asyncio.gather(*list(bridge._flow._jobs)), 3)
        assert len(calls) == 1 and len(requests) == 1
        request = json.dumps(calls[0], ensure_ascii=False)
        assert '先看这张图' in request and '再一起解释一下' in request
        assert 'data:image/png;base64,' in request and 'onebot-image:' not in request
        merged = [e for e in store.list_events() if e['kind'] == 'qq_debounce']
        assert merged[0]['details']['input_count'] == 2
    finally:
        image_ready.set()
        await close(runtime)


@pytest.mark.anyio
@pytest.mark.parametrize('stall', ['write', 'receipt'])
async def test_image_batch_timeout_covers_every_write_and_receipt(runtime, monkeypatch, stall):
    _, _, _, bridge, _, _ = runtime
    monkeypatch.setattr('shiori.onebot.IMAGE_RESOLVE_TIMEOUT', .05)
    started = []
    class Socket:
        async def send_json(self, action):
            started.append(action)
            if stall == 'write':
                await asyncio.Event().wait()
    bridge._socket = Socket()
    try:
        with pytest.raises(ValueError, match='超时'):
            await asyncio.wait_for(bridge.resolve_images(SCOPE,
                ['onebot-image:999:first-image.png', 'onebot-image:999:second-image.png']), 1)
        assert len(started) == 2 and not bridge._pending
    finally:
        await close(runtime)


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['permission', 'connection', 'source'])
async def test_changed_authority_during_lookup_cannot_use_image(runtime, change):
    store, settings, _, bridge, _, _ = runtime
    live = True
    class Socket:
        async def send_json(self, action):
            nonlocal live
            if change == 'permission': settings.update({'onebot': {'enabled': False}})
            elif change == 'connection': bridge._socket = object()
            else: live = False
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'data': {'url': 'https://gchat.qpic.cn/fixture.png'}})
    bridge._socket = Socket()
    try:
        with pytest.raises(ValueError, match='变化'):
            await bridge.resolve_images(SCOPE, ['onebot-image:999:fixture-image.png'], before_send=lambda: live)
        assert not bridge._pending
    finally:
        await close(runtime)


def test_internal_image_references_are_not_accepted_by_local_chat():
    with pytest.raises(ValueError, match='HTTPS'):
        validate_images(['onebot-image:999:fixture-image.png'])
    with pytest.raises(ValueError, match='HTTPS'):
        validate_images(['onebot-image:999:../../secret'], allow_onebot_refs=True)


@pytest.mark.anyio
async def test_image_lookup_cancellation_cleans_pending_receipts(runtime):
    _, _, _, bridge, _, _ = runtime
    started = asyncio.Event()
    class Socket:
        async def send_json(self, action): started.set()
    bridge._socket = Socket()
    task = asyncio.create_task(bridge.resolve_images(SCOPE, ['onebot-image:999:fixture-image.png']))
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not bridge._pending
        with pytest.raises(ValueError, match='不属于'):
            await bridge.resolve_images(SCOPE, ['onebot-image:888:fixture-image.png'])
        assert not bridge._pending
    finally:
        await close(runtime)


@pytest.mark.anyio
async def test_sticker_collection_resolves_opaque_reference_and_keeps_source(runtime):
    store, settings, chat, bridge, _, _ = runtime
    settings.update({'stickers': {'enabled': True, 'collect_enabled': True, 'collect_probability': 1}})
    recognized = []
    class Model:
        async def complete(self, messages, **kwargs):
            assert kwargs['before_send']()
            recognized.append(messages)
            return {'content': json.dumps({'is_sticker': True, 'clear': True, 'meaning': '开心地表示赞同',
                'category': '赞同', 'tags': ['开心'], 'confidence': .99})}
    chat.stickers.provider = Model()
    class Socket:
        async def send_json(self, action):
            bridge._pending[action['echo']][1].set_result({'status': 'ok',
                'data': {'url': 'https://gchat.qpic.cn/fixture.png'}})
    bridge._socket = Socket()
    try:
        source = await chat.observe(SCOPE, '101', '图片', images=['onebot-image:999:fixture-image.png'])
        await asyncio.wait_for(asyncio.gather(*list(chat.stickers._jobs)), 3)
        assert len(recognized) == 1 and 'onebot-image:' not in json.dumps(recognized)
        items = chat.stickers.page()['items']
        assert len(items) == 1 and items[0]['meaning'] == '开心地表示赞同'
        assert items[0]['source_id'] == source['id']
        assert chat.stickers._source_current(items[0])
        assert store.get_message(source['id'])['metadata']['images'] == ['onebot-image:999:fixture-image.png']
        assert chat.stickers.quota_status()['vision'] == 1
    finally:
        await close(runtime)
