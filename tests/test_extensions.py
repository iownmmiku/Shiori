"""Extensions: trusted add-ons with tools, APIs, pages and settings (owner request, 2026-11-18).

The owner asked for something like AstrBot's plugins with their own WebUI,
with image generation as the first one. Extensions run inside Shiori with
its permissions, unlike the sandboxed local plugins; bundled ones live in
shiori/extensions, others in <data>/extensions and stay off until confirmed.
"""
import asyncio
import json
import textwrap
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.chat import ChatService
from shiori.db import Store
from shiori.extension_host import ExtensionError, ExtensionHost, ToolCall, ToolResult, Turn, read_manifest
from shiori.settings import DEFAULTS

PRIVATE = 'qq:private:303'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['output'].update(mode='model', interval_ms=0)
        self.config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.config['stickers'].update(send_enabled=False)

    def get(self):
        return deepcopy(self.config)


MANIFEST = {
    'id': 'hello', 'name': '挥手', 'version': '0.1.0', 'description': '测试用扩展', 'author': 'fixture',
    'pages': [{'id': 'main', 'title': '挥手设置', 'entry': 'pages/main/index.html'}],
    'config': {
        'greeting': {'type': 'string', 'title': '问候语', 'default': '你好'},
        'times': {'type': 'int', 'title': '次数', 'default': 1, 'min': 1, 'max': 5},
        'mood': {'type': 'select', 'title': '心情', 'options': [['calm', '平静'], ['happy', '开心']], 'default': 'calm'},
        'token': {'type': 'secret', 'title': '令牌'},
    },
}
MAIN = '''
import io
from PIL import Image
from shiori.extension_host import ToolResult
from .helper import PREFIX


def picture():
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, format='PNG')
    return out.getvalue()


def setup(ext):
    ext.state['calls'], ext.state['cancelled'], ext.state['sent'] = [], [], []

    def schema(turn):
        if not turn.scope_id.startswith('qq:'):
            return None
        return {'type': 'function', 'function': {'name': 'wave', 'description': '挥手', 'parameters': {
            'type': 'object', 'properties': {'to': {'type': 'string'}}, 'required': ['to'], 'additionalProperties': False}}}

    def handler(call):
        ext.state['calls'].append(call)

        async def after(send):
            ext.state['sent'].append(await send.text(PREFIX + call.args['to']))
            ext.state['sent'].append(await send.image(picture()))
        return ToolResult({'ok': True, 'status': 'queued'}, after_reply=after,
                          cancel=lambda: ext.state['cancelled'].append(call.args['to']))

    ext.tool('wave', schema, handler, hint='你可以用 wave 挥手。', queued=True)
    ext.api('GET', 'hello', lambda request: {'hello': ext.config()['greeting'], 'query': request.query})
    ext.api('POST', 'echo', lambda request: {'body': request.body})
'''


def write_extension(root, manifest=None, main=MAIN):
    folder = root / 'extensions' / 'hello'
    (folder / 'pages' / 'main').mkdir(parents=True, exist_ok=True)
    (folder / 'extension.json').write_text(json.dumps(manifest or MANIFEST, ensure_ascii=False), 'utf-8')
    (folder / 'main.py').write_text(textwrap.dedent(main), 'utf-8')
    (folder / 'helper.py').write_text("PREFIX = '（挥手）'\n", 'utf-8')
    (folder / 'pages' / 'main' / 'index.html').write_text('<!doctype html><title>挥手</title><script src="app.js"></script>', 'utf-8')
    (folder / 'pages' / 'main' / 'app.js').write_text('console.log("hi")', 'utf-8')
    (root / 'secret.txt').write_text('outside the page folder', 'utf-8')
    return folder


@pytest.fixture
def make_host(tmp_path):
    stores = []

    def build(name='host', *, extension=True, **kwargs):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        if extension:
            write_extension(root, **kwargs)
        store = Store(root)
        stores.append(store)
        host = ExtensionHost(store, Config(), root, reserved={'remember'})
        host.start()
        return host, store, root
    yield build
    for store in stores:
        store.close()


def test_the_bundled_image_extension_is_on_with_its_page_and_setting(make_host):
    host, _, _ = make_host(extension=False)
    item = next(entry for entry in host.list() if entry['id'] == 'imagegen')
    assert item['builtin'] and item['enabled'] and item['loaded'] and not item['error']
    assert item['pages'] == [{'id': 'main', 'title': '生图设置', 'entry': 'pages/main/index.html',
                              'url': '/extensions/imagegen/pages/main/'}]
    assert item['config'] == {'daily_per_user': 5} and item['config_schema']['daily_per_user']['type'] == 'int'
    assert item['tools'] == ['generate_image']
    # No service yet: the tool is not offered.
    assert host.tool_schemas(Turn(PRIVATE, '303')) == []


def test_a_data_folder_extension_stays_off_until_confirmed(make_host):
    host, store, root = make_host()
    item = next(entry for entry in host.list() if entry['id'] == 'hello')
    assert not item['builtin'] and not item['enabled'] and not item['trusted'] and not item['loaded']
    with pytest.raises(ExtensionError, match='权限和 Shiori 相同'):
        host.set_enabled('hello', True)
    item = host.set_enabled('hello', True, confirm=True)
    assert item['enabled'] and item['loaded'] and item['tools'] == ['wave']
    # Restarted unchanged it comes back on; a changed file needs confirming again.
    again = ExtensionHost(store, Config(), root)
    again.start()
    assert again.enabled('hello')
    (root / 'extensions' / 'hello' / 'helper.py').write_text("PREFIX = '（偷偷改了）'\n", 'utf-8')
    changed = ExtensionHost(store, Config(), root)
    changed.start()
    assert not changed.enabled('hello') and not changed.context('hello')
    assert next(entry for entry in changed.list() if entry['id'] == 'hello')['trusted'] is False


@pytest.mark.parametrize('patch, message', [
    ({'id': 'other'}, '文件夹名'),
    ({'id': 'Bad-ID'}, '小写字母'),
    ({'surprise': 1}, '不认识的字段'),
    ({'pages': [{'id': 'main', 'title': '页', 'entry': '../outside.html'}]}, 'entry'),
    ({'pages': [{'id': 'main', 'title': '页', 'entry': 'pages/main/missing.html'}]}, 'entry'),
    ({'config': {'x': {'type': 'color', 'title': '颜色'}}}, 'type'),
    ({'config': {'x': {'type': 'int', 'title': '数', 'default': 9, 'max': 5}}}, '默认值'),
    ({'config': {'x': {'type': 'secret', 'title': '令牌', 'default': 'abc'}}}, '默认值'),
])
def test_manifest_problems_are_reported_and_others_still_load(make_host, patch, message):
    host, _, root = make_host(manifest=dict(MANIFEST, **patch))
    item = next(entry for entry in host.list() if entry['id'].startswith('hello'))
    assert message in item['error'] and not item['loaded']
    with pytest.raises(ExtensionError):
        read_manifest(root / 'extensions' / 'hello')
    assert host.context('imagegen') is not None


def test_settings_follow_the_manifest_and_secrets_stay_protected(make_host):
    host, _, root = make_host()
    host.set_enabled('hello', True, confirm=True)
    ext = host.context('hello')
    assert ext.config() == {'greeting': '你好', 'times': 1, 'mood': 'calm', 'token': ''}
    host.set_config('hello', {'greeting': '早上好', 'times': 3, 'mood': 'happy', 'token': 'tok-fixture-123456'})
    assert ext.config()['token'] == 'tok-fixture-123456' and ext.config()['times'] == 3
    on_disk = (root / 'extensions.json').read_text('utf-8')
    assert 'tok-fixture-123456' not in on_disk and '早上好' in on_disk
    item = next(entry for entry in host.list() if entry['id'] == 'hello')
    assert 'token' not in item['config'] and item['secrets_set'] == {'token': True}
    host.set_config('hello', {'token': ''})                      # empty keeps the saved one
    assert ext.config()['token'] == 'tok-fixture-123456'
    host.set_config('hello', {}, clear=['token'])
    assert ext.config()['token'] == ''
    for values, message in (({'times': 9}, '之间'), ({'times': 2.5}, '整数'), ({'mood': 'angry'}, '可选'),
                            ({'unknown': 1}, '没有的项')):
        with pytest.raises(ExtensionError, match=message):
            host.set_config('hello', values)


@pytest.mark.anyio
async def test_tools_hand_back_work_that_runs_after_the_reply_or_is_cancelled(make_host):
    host, _, _ = make_host()
    host.set_enabled('hello', True, confirm=True)
    assert host.tool_schemas(Turn('web:default', 'owner')) == []
    assert [item['function']['name'] for item in host.tool_schemas(Turn(PRIVATE, '303'))] == ['wave']
    assert host.hints({'wave'}) == ['你可以用 wave 挥手。'] and host.queued_names() == {'wave', 'generate_image'}
    call = ToolCall(PRIVATE, '303', 'qq:303', 'default', {'to': '小明'})
    output = await host.call_tool('wave', call)
    job = output['extension_job']
    assert output['status'] == 'queued' and job['extension'] == 'hello' and host.pending(job['id'])

    class Sender:
        def __init__(self):
            self.sent = []

        async def text(self, text):
            self.sent.append(('text', text))
            return True

        async def image(self, raw):
            self.sent.append(('image', raw[:4]))
            return True
    sender = Sender()
    assert await host.run_job(job['id'], sender)
    assert sender.sent == [('text', '（挥手）小明'), ('image', b'\x89PNG')]
    assert not await host.run_job(job['id'], sender)              # runs once
    other = (await host.call_tool('wave', ToolCall(PRIVATE, '303', 'qq:303', 'default', {'to': '小红'})))['extension_job']
    host.discard_actions([{'tool': 'wave', 'ok': True, 'result': {'extension_job': other}}])
    assert host.context('hello').state['cancelled'] == ['小红'] and not host.pending(other['id'])
    # Turning the extension off cancels what is still waiting and removes the tool.
    waiting = (await host.call_tool('wave', ToolCall(PRIVATE, '303', 'qq:303', 'default', {'to': '小刚'})))['extension_job']
    host.set_enabled('hello', False)
    assert not host.pending(waiting['id']) and not host.has_tool('wave')


def test_a_broken_extension_or_tool_does_not_break_anything_else(make_host):
    host, store, _ = make_host(main='def setup(ext):\n    raise RuntimeError("boom")\n')
    item = host.set_enabled('hello', True, confirm=True)
    assert not item['loaded'] and 'boom' in item['error'] and host.context('imagegen') is not None
    assert any(event['kind'] == 'extension_error' for event in store.list_events(limit=10))
    host2, store2, _ = make_host('second', main=MAIN.replace("if not turn.scope_id.startswith('qq:'):", 'if 1 / 0:'))
    host2.set_enabled('hello', True, confirm=True)
    assert host2.tool_schemas(Turn(PRIVATE, '303')) == []
    assert any(event['kind'] == 'extension_error' for event in store2.list_events(limit=10))


def test_reserved_and_duplicate_tool_names_are_refused(make_host):
    host, _, _ = make_host(main=MAIN.replace("ext.tool('wave'", "ext.tool('remember'"))
    item = host.set_enabled('hello', True, confirm=True)
    assert '重名' in item['error']
    host2, _, _ = make_host('second', main=MAIN.replace("ext.tool('wave'", "ext.tool('generate_image'"))
    assert '已被扩展「imagegen」使用' in host2.set_enabled('hello', True, confirm=True)['error']


def test_extension_api_pages_and_frame_policy(tmp_path):
    write_extension(tmp_path / 'app')
    app = create_app(tmp_path / 'app', start_workers=False)
    with TestClient(app) as client:
        assert client.get('/api/extensions').status_code == 401
        assert client.get('/extensions/imagegen/pages/main/').status_code == 401
        password_login(client, app)
        items = {item['id']: item for item in client.get('/api/extensions').json()['items']}
        assert items['imagegen']['enabled'] and not items['hello']['enabled']
        refused = client.put('/api/extensions/hello', json={'enabled': True})
        assert refused.status_code == 400 and '权限和 Shiori 相同' in refused.json()['detail']
        assert client.put('/api/extensions/hello', json={'enabled': True, 'confirm': True}).json()['loaded']
        assert client.put('/api/extensions/hello', json={'config': {'greeting': '晚上好'}}).json()['config']['greeting'] == '晚上好'
        assert client.get('/api/extensions/hello/api/hello?x=1').json() == {'hello': '晚上好', 'query': {'x': '1'}}
        assert client.post('/api/extensions/hello/api/echo', json={'a': 1}).json() == {'body': {'a': 1}}
        assert client.get('/api/extensions/hello/api/missing').status_code == 404
        assert client.post('/api/extensions/hello/api/hello').status_code == 404       # wrong method
        page = client.get('/extensions/hello/pages/main/')
        assert page.status_code == 200 and page.headers['content-type'].startswith('text/html') and '挥手' in page.text
        assert "frame-ancestors 'self'" in page.headers['content-security-policy']
        assert client.get('/extensions/hello/pages/main/app.js').headers['content-type'].startswith('text/javascript')
        assert client.get('/extensions/hello/pages/main', follow_redirects=False).status_code == 307
        for path in ('/extensions/hello/pages/main/../../../../secret.txt', '/extensions/hello/pages/main/%2e%2e/%2e%2e/main.py',
                     '/extensions/hello/pages/main/../../main.py', '/extensions/hello/pages/other/', '/extensions/nope/pages/main/'):
            assert client.get(path).status_code == 404, path
        # The console itself still may not be framed.
        assert "frame-ancestors 'none'" in client.get('/').headers['content-security-policy']
        imagegen = client.get('/extensions/imagegen/pages/main/')
        assert imagegen.status_code == 200 and '/static/extension-sdk.js' in imagegen.text
        assert client.get('/api/extensions/imagegen/api/state').json()['services'] == []
        client.put('/api/extensions/hello', json={'enabled': False})
        assert client.get('/api/extensions/hello/api/hello').status_code == 404
        assert client.get('/extensions/hello/pages/main/').status_code == 404


def answer(content='', calls=()):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = list(calls)
    return {'content': content, 'tool_calls': list(calls), 'message': message, 'usage': {'input_tokens': 1, 'output_tokens': 1}}


class Model:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    async def complete(self, messages, **kwargs):
        if kwargs.get('purpose', 'chat') != 'chat':
            return answer('{}')
        self.seen.append({'system': messages[0]['content'], 'tools': kwargs.get('tools') or []})
        return self.turns.pop(0) if self.turns else answer('嗯。')


@pytest.mark.anyio
async def test_a_qq_reply_runs_the_extension_work_after_its_text(tmp_path):
    from shiori.onebot import OneBotBridge
    write_extension(tmp_path)
    store, config = Store(tmp_path), Config()
    store.upsert_scope({'id': PRIVATE, 'auto_memory': False, 'merge_window_ms': 0, 'plan_enabled': False})
    model = Model(answer('来啦～', [{'id': 'c1', 'type': 'function', 'function': {'name': 'wave', 'arguments': '{"to": "你"}'}}]))
    chat = ChatService(store, config, model)
    chat.extensions.set_enabled('hello', True, confirm=True)
    bridge = OneBotBridge(store, config, chat)
    sent = []

    class Socket:
        async def send_json(self, action):
            sent.append(action)
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': len(sent)}})
    bridge._socket = Socket()
    try:
        await asyncio.wait_for(bridge._flow.handle(PRIVATE, '303', '跟我打个招呼', [], {'at_bot': True}, direct=True), 10)
        await asyncio.wait_for(asyncio.gather(*list(bridge._flow._extension_jobs)), 10)
    finally:
        await bridge.stop()
        await chat.close()
    segments = [action['params']['message'][0] for action in sent]
    assert [segment['type'] for segment in segments] == ['text', 'text', 'image']
    assert segments[0]['data']['text'] == '来啦～' and segments[1]['data']['text'] == '（挥手）你'
    assert '你可以用 wave 挥手。' in model.seen[0]['system'] and len(model.seen) == 1
    call = chat.extensions.context('hello').state['calls'][0]
    assert (call.user_key, call.persona_id, call.admin) == ('qq:303', 'default', False)
    store.close()


def test_the_image_extension_also_installs_from_the_data_folder(tmp_path):
    # It is published on its own as well; the same folder must load as a data-folder extension.
    import shutil
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / 'shiori' / 'extensions' / 'imagegen'
    shutil.copytree(source, tmp_path / 'extensions' / 'imagegen', ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path / 'bundled').mkdir()
    store = Store(tmp_path)
    try:
        host = ExtensionHost(store, Config(), tmp_path, builtin_root=tmp_path / 'bundled')
        host.start()
        assert not host.enabled('imagegen')
        item = host.set_enabled('imagegen', True, confirm=True)
        assert item['loaded'] and not item['builtin'] and item['tools'] == ['generate_image'], item['error']
        engine = host.context('imagegen').state['engine']
        assert type(engine).__module__ == 'shiori_extension_imagegen.engine'
        engine.update({'services': [{'id': 'main', 'kind': 'openai', 'api_key': 'sk-fixture', 'model': 'gpt-image-1'}],
                       'service_id': 'main'})
        assert [schema['function']['name'] for schema in host.tool_schemas(Turn(PRIVATE, '303'))] == ['generate_image']
        assert (tmp_path / 'extension_data' / 'imagegen' / 'config.json').is_file()
    finally:
        store.close()
