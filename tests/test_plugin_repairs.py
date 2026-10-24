"""Malformed source stays local to its candidate; chat edits retain revision guards."""
import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from mikubot.api import create_app
from mikubot.plugin_packages import inspect_archive, validate_code
from mikubot.plugins import PluginService, requested, create_and_run
from test_computer_auto_v9 import automatic
from test_plugin_packages import files, packed, install


def recursive_source():
    # Only 8026 characters: the old character ceiling did not protect ast.parse.
    return 'def run(input):\n    return ' + '+'.join(['1'] * 4000)


@pytest.mark.parametrize('path', ['main.py', 'helper.py'])
def test_short_recursive_source_is_a_candidate_error_not_a_server_exception(path):
    members = files(); members[path] = recursive_source().encode()
    result = inspect_archive('complex.zip', packed(members))[0]
    assert path in result['error'] and '复杂' in result['error'] and '拆分' in result['error']


@pytest.mark.parametrize('source', [
    'def run(input):\n    return ' + '+'.join(['1'] * 150),
    'x=1\n' * 2100 + 'def run(input):\n    return input\n',
])
def test_successfully_parsed_source_also_has_depth_and_node_budgets(source):
    assert len(source) < 10000
    with pytest.raises(ValueError, match='结构过于复杂'):
        validate_code(source)


def test_bad_recursive_candidate_does_not_block_installing_a_valid_sibling(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    bad = files(); bad['helper.py'] = recursive_source().encode()
    members = {**{'good/' + name: raw for name, raw in files().items()},
               **{'bad/' + name: raw for name, raw in bad.items()}}
    scan = service.scan('mixed.zip', packed(members, wrapper=''))
    assert len(scan['candidates']) == 2
    invalid = next(c for c in scan['candidates'] if c.get('error'))
    valid = next(c for c in scan['candidates'] if not c.get('error'))
    assert 'helper.py' in invalid['error']
    item = service.install_scan(scan['scan_id'], valid['candidate_id'])
    assert not item['enabled'] and item['id'] == 'packaged_example'


def test_excessively_nested_manifest_is_a_candidate_error():
    invalid = {'plugin.json': ('{"nested":' + '[' * 8000 + '0' + ']' * 8000 + '}').encode()}
    members = {**{'good/' + name: raw for name, raw in files().items()},
               **{'bad/' + name: raw for name, raw in invalid.items()}}
    candidates = inspect_archive('mixed.zip', packed(members, wrapper=''))
    assert len(candidates) == 2
    assert '嵌套过深' in next(c['error'] for c in candidates if c.get('error'))
    assert any(not c.get('error') for c in candidates)


def test_chat_edit_keeps_auxiliary_files_schema_and_history_and_disables(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    item = install(service)
    active = service.activate(item['id'], True)
    read = service.read_for_edit(item['id'])
    replacement = 'def run(input):\n    return {"fixed": input["value"]}\n'
    edited = service.edit({'id': item['id'], 'code': replacement,
                           'expected_revision': read['revision'], 'expected_sha256': read['sha256']})
    assert edited['revision'] == active['revision'] + 1 and not edited['enabled']
    assert edited['code'] == replacement and edited['name'] == active['name']
    assert edited['manifest']['input_schema'] == active['manifest']['input_schema']
    kept = service._files(edited)
    assert kept['helper.py'] == files()['helper.py'] and kept['assets/value.txt'] == files()['assets/value.txt']
    restored = service.rollback(item['id'])
    assert restored['code'] == item['code'] and not restored['enabled']


@pytest.mark.parametrize('stale', ['revision', 'sha256'])
def test_chat_edit_refuses_a_stale_revision_or_digest_without_disabling_it(automatic, stale):
    service = PluginService(automatic.service.data_dir, automatic.service)
    item = install(service); read = service.read_for_edit(item['id'])
    if stale == 'revision':
        current = service.activate(item['id'], True)
    else:
        current = service.get(item['id'])
        read['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='未覆盖'):
        service.edit({'id': item['id'], 'code': 'def run(input):\n    return {}\n',
                      'expected_revision': read['revision'], 'expected_sha256': read['sha256']})
    assert service.get(item['id']) == current


def test_chat_creation_cannot_overwrite_an_existing_id(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    item = install(service)
    with pytest.raises(ValueError, match='先用 get_plugin'):
        service.create({'id': item['id'], 'name': '覆盖', 'description': '不应保存',
                        'code': 'def run(input):\n    return {}\n'})
    assert service.get(item['id'])['sha256'] == item['sha256']


@pytest.mark.parametrize('section', ['code', 'input_schema', 'readme'])
def test_every_read_section_is_losslessly_paged_within_chat_tool_budget(automatic, section):
    service = PluginService(automatic.service.data_dir, automatic.service)
    members = files()
    code = 'def run(input):\n    return ' + repr('"\\' * 1500) + '\n'
    description = json.loads(members['plugin.json'])
    description['input_schema']['description'] = '"\\' * 2500
    members.update({'main.py': code.encode(), 'plugin.json': json.dumps(description).encode(),
                    'README.md': ('"\\' * 10000).encode()})
    item = install(service, members)
    expected = {'code': code, 'input_schema': json.dumps(item['manifest']['input_schema'], ensure_ascii=False, indent=2),
                'readme': members['README.md'].decode()}[section]
    pieces, offset = [], 0
    while True:
        page = service.read_for_edit(item['id'], section=section, offset=offset)
        assert len(json.dumps(page, ensure_ascii=False, sort_keys=True)) <= 4500
        assert page['sha256'] == item['sha256'] and page['revision'] == item['revision']
        pieces.append(page['content'])
        if page['next_offset'] is None: break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert len(pieces) > 1 and ''.join(pieces) == expected


@pytest.mark.parametrize('text,allowed', [
    ('帮我修改 example_plugin 插件，修复重复输出', True),
    ('帮我修复这个插件', True), ('请修复 example_plugin 插件', True),
    ('帮我把 example_plugin 插件修好', True), ('fix this plugin', True),
    ('这个插件是什么？', False), ('请解释如何修复插件', False),
    ('文档要求修改插件是什么意思', False), ('不要修改这个插件', False),
    ('我想不要修复插件', False), ('帮我运行 example_plugin 插件', False),
])
def test_plugin_repair_requires_an_explicit_edit_intent(text, allowed):
    assert requested(text, edit=True) is allowed
    if allowed: assert not create_and_run(text)


def fake_windows_crypto(monkeypatch):
    # API fixture only: do not require Windows DPAPI outside its OS user session.
    if os.name == 'nt':
        monkeypatch.setattr('mikubot.web_auth._protect', lambda value: 'dpapi:' + base64.b64encode(value.encode()).decode())
        monkeypatch.setattr('mikubot.web_auth._unprotect', lambda value: base64.b64decode(value.split(':', 1)[1]).decode())


def test_http_import_returns_errors_for_complex_sources_instead_of_500(tmp_path, monkeypatch):
    fake_windows_crypto(monkeypatch)
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        members = files(); members['helper.py'] = recursive_source().encode()
        result = client.post('/api/plugins/import', files={'file': ('complex.zip', packed(members))})
        assert result.status_code == 200, result.text
        candidate = result.json()['candidates'][0]
        assert 'helper.py' in candidate['error'] and '复杂' in candidate['error']
        assert app.state.plugins.list() == [] and app.state.runs.list() == []


def test_local_chat_reads_and_saves_a_repair_without_reauthorizing_execution(tmp_path, monkeypatch):
    fake_windows_crypto(monkeypatch)
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        app.state.settings.update({'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(tmp_path / 'workspace')}})
        old = install(app.state.plugins)
        app.state.plugins.activate(old['id'], True)
        calls = []
        async def complete(messages, **kwargs):
            assert kwargs['before_send']()
            names = {tool['function']['name'] for tool in kwargs['tools']}
            assert {'get_plugin', 'update_plugin'}.issubset(names)
            assert 'create_plugin' not in names and 'run_plugin' not in names
            calls.append(messages)
            if len(calls) == 1:
                name, args = 'get_plugin', {'id': old['id']}
            elif len(calls) == 2:
                read = json.loads(messages[-1]['content'])
                assert read['content'] == old['code']
                name, args = 'update_plugin', {'id': old['id'], 'code': 'def run(input):\n    return {"fixed": input["value"]}\n',
                    'expected_revision': read['revision'], 'expected_sha256': read['sha256']}
            else:
                result = {'content': '修复已保存为停用的新修订，尚未运行。'}
                return dict(result, usage={}, message={'role': 'assistant', **result})
            request = {'id': 'plugin-edit-' + str(len(calls)), 'type': 'function',
                       'function': {'name': name, 'arguments': json.dumps(args)}}
            return {'usage': {}, 'tool_calls': [request], 'message': {'role': 'assistant', 'content': '', 'tool_calls': [request]}}
        monkeypatch.setattr(app.state.provider, 'complete', complete)
        result = client.post('/api/chat', json={'text': '帮我修复 packaged_example 插件，返回 fixed 字段'})
        assert result.status_code == 200, result.text
        updated = app.state.plugins.get(old['id'])
        assert len(calls) == 3 and not updated['enabled']
        assert updated['manifest']['input_schema'] == old['manifest']['input_schema']
        assert 'fixed' in updated['code'] and app.state.runs.list() == []


def test_plugin_question_does_not_offer_mutation_tools(tmp_path, monkeypatch):
    fake_windows_crypto(monkeypatch)
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        app.state.settings.update({'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(tmp_path / 'workspace')}})
        old = install(app.state.plugins)
        async def complete(messages, **kwargs):
            names = {tool['function']['name'] for tool in kwargs['tools']}
            assert 'get_plugin' in names and not names.intersection({'create_plugin', 'update_plugin', 'run_plugin'})
            return {'content': '这是一项插件说明。', 'usage': {}, 'message': {'role': 'assistant', 'content': '这是一项插件说明。'}}
        monkeypatch.setattr(app.state.provider, 'complete', complete)
        response = client.post('/api/chat', json={'text': '请解释如何修复 packaged_example 插件'})
        assert response.status_code == 200, response.text
        assert app.state.plugins.get(old['id']) == {key: value for key, value in old.items() if key not in ('warnings', 'export_url')}
