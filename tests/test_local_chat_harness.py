"""One authenticated Chat loop does real local work without a mode selector."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.local_chat import compact_tool_rounds, tool_result_for_model
from shiori.provider import ProviderError
from shiori.tool_actions import encoded


def completion(name=None, arguments=None, *, content='', index=0):
    calls = [{'id': f'call-{index}', 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments or {}, ensure_ascii=False)}}] if name else []
    return {'content': content, 'tool_calls': calls, 'usage': None,
            'message': {'role': 'assistant', 'content': content or None, 'tool_calls': calls}}


def configure(app, tmp_path):
    app.state.settings.update({'computer': {'enabled': True, 'runtime': 'local',
        'approval_mode': 'automatic', 'workspace': str(tmp_path / 'workspace'), 'timeout_seconds': 10}})
    app.state.store.upsert_scope({'id': 'web:default', 'auto_memory': False})


def test_unified_chat_exceeds_three_tool_steps_and_keeps_persona_and_receipts(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    expected = [
        ('list_files', {}),
        ('write_file', {'path': 'source.txt', 'content': 'hello'}),
        ('read_file', {'path': 'source.txt'}),
        ('execute_python', {'code': "from pathlib import Path\nPath('result.txt').write_text(Path('source.txt').read_text().upper())"}),
        ('read_file', {'path': 'result.txt'}),
    ]
    snapshots = []
    async def model(messages, **kwargs):
        assert kwargs['before_send']()
        snapshots.append(copy.deepcopy(messages))
        step = len(snapshots) - 1
        names = {tool['function']['name'] for tool in kwargs['tools'] or []}
        assert 'read_file' in names and 'start_computer_task' not in names
        if step < len(expected):
            name, arguments = expected[step]
            return completion(name, arguments, index=step)
        assert 'HELLO' in messages[-1]['content']
        return completion(content='已经生成 result.txt，并读取核对为 HELLO。')
    monkeypatch.setattr(app.state.provider, 'complete', model)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/chat', json={'scope_id': 'web:default', 'text': 'Work on this project, produce and verify the uppercase file.'})
        assert response.status_code == 200, response.text
        value = response.json()
        assert len(snapshots) == 6 and len(value['actions']) == 5
        assert all(action['ok'] for action in value['actions'])
        assert len(value['jobs']) == 5 and all(job['inline'] and job['status'] == 'completed' for job in value['jobs'])
        assert (tmp_path / 'workspace/result.txt').read_text() == 'HELLO'
        messages = app.state.store.list_messages('web:default')
        assert [item['role'] for item in messages] == ['user', 'assistant']
        assert len(messages[-1]['metadata']['job_ids']) == 5
        assert '本地 Chat 工作台' in snapshots[0][0]['content']
        assert '群消息可能只是旁听背景' not in snapshots[0][0]['content']
        assert '初音未来' in snapshots[0][0]['content']
        assert any('本轮已执行工具的压缩回执' in str(item.get('content')) for item in snapshots[-1])


def test_successful_write_is_not_replayed_when_followup_model_fails(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    count = 0
    async def model(messages, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return completion('write_file', {'path': 'saved.txt', 'content': 'preserved'})
        raise ProviderError('fixture continuation failed')
    monkeypatch.setattr(app.state.provider, 'complete', model)
    with TestClient(app) as client:
        password_login(client, app)
        value = client.post('/api/chat', json={'text': '保存这个文件'}).json()
        assert (tmp_path / 'workspace/saved.txt').read_text() == 'preserved'
        assert count == 2 and len(value['jobs']) == 1
        assert value['actions'][0]['ok'] and '不会自动重跑' in value['reply']


def test_job_progress_is_separate_from_text_stream_and_survives_reset(tmp_path, monkeypatch):
    from shiori.chat_preview import emit_preview
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    count = 0
    async def model(messages, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return completion('write_file', {'path': 'sse.txt', 'content': 'saved'})
        emit_preview('reset')
        emit_preview('delta', '已完成')
        return completion(content='已完成')
    monkeypatch.setattr(app.state.provider, 'complete', model)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/chat', json={'text': '保存这个文件', 'stream': True})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        jobs = [item['data'] for item in events if item['type'] == 'job']
        assert jobs and jobs[-1]['status'] == 'completed' and jobs[-1]['inline']
        assert events[-1]['type'] == 'result'
        assert events[-1]['data']['jobs'][0]['id'] == jobs[-1]['id']
        assert all('sse.txt' not in item.get('text', '') for item in events if item['type'] == 'delta')


def test_read_projection_cursor_covers_every_character_even_with_json_escapes():
    source = ('"\\\n甲😀' * 3000)
    offset, actual = 0, ''
    while offset < len(source):
        value = {'tool': 'read_file', 'job_id': 'test', 'status': 'completed', 'ok': True,
            'output': {'text': source[offset:], 'offset': offset, 'total_characters': len(source),
                       'characters': len(source) - offset, 'truncated': False, 'next_offset': None}}
        page = tool_result_for_model(value, 1200)
        assert len(encoded(page)) <= 1200
        assert value['output']['text'] == source[offset:]
        actual += page['output']['text']
        assert page['output']['characters'] == len(page['output']['text'])
        following = page['output']['next_offset']
        if following is None:
            break
        assert following > offset
        offset = following
    assert actual == source


def test_compaction_preserves_all_call_statuses_and_latest_complete_pairs():
    request = {'role': 'user', 'content': 'Do the project'}
    messages = [{'role': 'system', 'content': 'identity'}, request]
    for index in range(10):
        result = completion('read_file', {'path': f'{index}.txt'}, index=index)
        messages.extend([result['message'], {'role': 'tool', 'tool_call_id': f'call-{index}',
            'content': json.dumps({'job_id': str(index), 'status': 'failed' if index == 0 else 'completed', 'text': 'x' * 5000})}])
        messages = compact_tool_rounds(messages, request)
    assert messages[1] is request
    summary = messages[2]['content']
    assert 'failed' in summary and all(f'call-{index}' in summary for index in range(8))
    assert [item['tool_call_id'] for item in messages if item['role'] == 'tool'] == ['call-8', 'call-9']
    assert all(set(item) <= {'role', 'content', 'tool_call_id', 'tool_calls'} for item in messages)
    assert len(summary) < 10000


def change_source(store, source_id, *, delete=False):
    source = store.get_message(source_id)
    with store._lock, store._conn:
        if delete:
            store._conn.execute('DELETE FROM messages WHERE id=?', (source_id,))
        else:
            source['content'] = 'The old request is withdrawn; do not write its file.'
            store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(source), source_id))


@pytest.mark.parametrize('when', ['first_model', 'next_model', 'same_batch'])
@pytest.mark.parametrize('delete', [False, True])
def test_original_source_change_stops_old_tools_across_the_whole_turn(tmp_path, monkeypatch, when, delete):
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    count = 0
    source_id = None
    async def model(messages, **kwargs):
        nonlocal count, source_id
        count += 1
        assert kwargs['before_send']()
        source_id = next(item['id'] for item in app.state.store.list_messages('web:default') if item['role'] == 'user')
        write = completion('write_file', {'path': 'denied.txt', 'content': 'must not write'}, index=count)
        if when == 'same_batch':
            read = completion('list_files', {}, index=0)
            write['tool_calls'].insert(0, read['tool_calls'][0])
            return write
        if count == 1 and when == 'next_model':
            return completion('list_files', {}, index=0)
        change_source(app.state.store, source_id, delete=delete)
        return write
    monkeypatch.setattr(app.state.provider, 'complete', model)
    original_execute = app.state.chat.local_harness.execute
    async def execute(*args, **kwargs):
        result = await original_execute(*args, **kwargs)
        if when == 'same_batch':
            change_source(app.state.store, source_id, delete=delete)
        return result
    monkeypatch.setattr(app.state.chat.local_harness, 'execute', execute)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/chat', json={'text': 'Work on this project and save the file.'})
        assert response.status_code in (400, 403), response.text
        assert not (tmp_path / 'workspace/denied.txt').exists()
        jobs = app.state.runs.list()
        assert len(jobs) == (0 if when == 'first_model' else 1)
        assert all(job['status'] == 'completed' for job in jobs)


def test_history_edit_during_model_cannot_be_rebound_by_the_next_tool(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    old = app.state.store.add_message('web:default', 'owner', 'user', 'Use the existing project notes.')
    async def model(messages, **kwargs):
        assert kwargs['before_send']()
        assert any('existing project notes' in str(message['content']) for message in messages)
        change_source(app.state.store, old['id'])
        return completion('write_file', {'path': 'denied.txt', 'content': 'old context'})
    monkeypatch.setattr(app.state.provider, 'complete', model)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/chat', json={'text': 'Now save the result.'})
        assert response.status_code in (400, 403), response.text
        assert not app.state.runs.list()
        assert not (tmp_path / 'workspace/denied.txt').exists()


@pytest.mark.parametrize('edit_recalled_source', [False, True])
def test_remember_and_new_recall_sources_extend_but_never_reset_turn_evidence(tmp_path, monkeypatch, edit_recalled_source):
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    old = app.state.store.add_message('web:default', 'owner', 'user', '我喜欢珍珠柠檬。')
    app.state.store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'kind': 'preference',
                                  'content': '我喜欢珍珠柠檬。', 'source_ids': [old['id']], 'visibility': 'private'})
    # Keep that old source out of initial history/automatic recall; only the
    # explicit recall tool admits it into this turn's evidence.
    monkeypatch.setattr(app.state.chat, '_history', lambda *args, **kwargs: [])
    async def no_initial_recall(*args, **kwargs):
        return []
    monkeypatch.setattr(app.state.chat, '_recall_context', no_initial_recall)
    count = 0
    async def model(messages, **kwargs):
        nonlocal count
        count += 1
        assert kwargs['before_send']()
        if count == 1:
            return completion('remember', {'content': '我喜欢茶', 'kind': 'preference'}, index=count)
        if count == 2:
            return completion('recall_memory', {'query': '珍珠柠檬'}, index=count)
        if count == 3:
            assert old['id'] in messages[-1]['content']
            if edit_recalled_source:
                change_source(app.state.store, old['id'])
            return completion('write_file', {'path': 'result.txt', 'content': 'verified'}, index=count)
        return completion(content='已完成。')
    monkeypatch.setattr(app.state.provider, 'complete', model)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/chat', json={'text': '这次请记住我喜欢茶，再查询记忆并保存结果。'})
        if edit_recalled_source:
            assert response.status_code in (400, 403), response.text
            assert not (tmp_path / 'workspace/result.txt').exists()
        else:
            assert response.status_code == 200, response.text
            assert all(action['ok'] for action in response.json()['actions'])
            assert (tmp_path / 'workspace/result.txt').read_text() == 'verified'
        assert any(item['content'] == '我喜欢茶' for item in app.state.store.list_memories())


def test_computer_snapshot_change_blocks_mcp_before_dispatch(tmp_path, monkeypatch):
    from shiori.mcp_client import schema_hash
    app = create_app(tmp_path / 'data', start_workers=False)
    configure(app, tmp_path)
    app.state.settings.update({'mcp_servers': [{'id': 'fixture', 'name': 'Fixture',
        'url': 'http://127.0.0.1:5678/mcp', 'enabled': True, 'allowed_tools': ['append'], 'token': ''}]})
    app.state.store.upsert_scope({'id': 'web:default', 'allow_mcp': True})
    calls = []
    class Client:
        async def discover(self, config):
            schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
            return {'tools': [{'name': 'append', 'inputSchema': schema, 'schema_hash': schema_hash(schema)}]}
        async def call(self, *args, **kwargs):
            calls.append(args)
            return {'isError': False, 'text': 'appended'}
    app.state.chat.tool_actions.client = Client()
    async def model(messages, **kwargs):
        assert kwargs['before_send']()
        alias = next(item['function']['name'] for item in kwargs['tools'] if item['function']['name'].startswith('mcp_'))
        app.state.settings.update({'computer': {'workspace': str(tmp_path / 'different-workspace')}})
        return completion(alias, {})
    monkeypatch.setattr(app.state.provider, 'complete', model)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/chat', json={'text': 'Append the note.'})
        assert response.status_code in (400, 403), response.text
        assert not calls and not app.state.chat.tool_actions.list()
