"""Automatic administrator tasks retain guards and optional step approvals.

All writes and the one real Python child stay in isolated temporary folders.
Shell execution uses a test double, never a real command.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.access import AccessManager
from shiori.api import create_app
from shiori.computer import ComputerService
from qunyu.db import Store
from shiori.runs import RunContext, RunManager, TERMINAL
from shiori.settings import Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def automatic(tmp_path):
    settings = Settings(tmp_path)
    settings.update({'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(tmp_path / 'workspace'),
                                  'max_steps': 5, 'timeout_seconds': 5},
                     'access': {'administrators': [{'platform': 'onebot', 'account_id': '101', 'instance_id': '9000'}]}})
    store = Store(tmp_path)
    runs = RunManager(tmp_path)
    access = AccessManager(settings)
    provider = SimpleNamespace(complete=None)
    service = ComputerService(store, settings, provider, access, runs, tmp_path)
    runs.register('computer', service.run, service.guard)
    identity = access.identity('onebot', '101', '9000')
    yield SimpleNamespace(service=service, settings=settings, store=store, runs=runs, access=access,
                          provider=provider, identity=identity, root=tmp_path / 'workspace')
    store.close()


def call(name, arguments):
    return {'tool_calls': [{'id': 'isolated-' + name, 'type': 'function',
                           'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


async def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(.01)
    raise AssertionError('isolated computer job did not reach expected state')


async def completed(fixture, job_id):
    await wait_for(lambda: fixture.runs.get(job_id)['status'] in TERMINAL)
    job = fixture.runs.get(job_id)
    assert job['status'] == 'completed', job
    return job


def submit(fixture, text='请在本机生成验收文件'):
    source = fixture.store.add_message('web:default', '101', 'user', text)
    return fixture.service.submit('web:default', '101', fixture.identity, text, source_ids=[source['id']])


@pytest.mark.anyio
async def test_default_automatic_file_and_mock_shell_execute_without_approval(automatic, monkeypatch):
    item = automatic
    assert item.settings.get()['computer']['approval_mode'] == 'automatic' and item.service.automatic()
    async def forbidden_approval(*args, **kwargs):
        raise AssertionError('automatic tasks must not ask for per-action approval')
    monkeypatch.setattr(RunContext, 'approve', forbidden_approval)
    steps = [call('write_file', {'path': 'auto-result.txt', 'content': 'automatic actual write'}),
             call('execute_shell', {'command': 'fixture-only-command-not-executed'}),
             {'content': '实际写入与模拟命令回执已完成'}]
    async def complete(messages, **kwargs):
        assert kwargs['before_send']()
        return steps.pop(0)
    observed = []
    def fake_process(ctx, root, name, value):
        ctx.check()
        observed.append((name, value))
        return {'ok': True, 'exit_code': 0, 'output': 'mock-command-receipt'}
    item.provider.complete = complete
    monkeypatch.setattr(item.service, '_process', fake_process)
    try:
        job = submit(item)
        result = await completed(item, job['id'])
        assert (item.root / 'auto-result.txt').read_text('utf-8') == 'automatic actual write'
        assert observed == [('execute_shell', 'fixture-only-command-not-executed')]
        types = [event['type'] for event in result['events']]
        assert types.count('authorized') == 2 and 'approval' not in types and 'approved' not in types
        assert result['approval'] is None
        assert all(event['data']['mode'] == 'automatic' for event in result['events'] if event['type'] == 'authorized')
    finally:
        await item.runs.close()


@pytest.mark.anyio
async def test_real_temporary_python_child_completes_automatically(automatic, monkeypatch):
    item = automatic
    async def forbidden_approval(*args, **kwargs):
        raise AssertionError('automatic Python unexpectedly waited for approval')
    monkeypatch.setattr(RunContext, 'approve', forbidden_approval)
    steps = [call('execute_python', {'code': "from pathlib import Path\nprint('auto-python-receipt-39')\nPath('python-auto.txt').write_text('temporary Python output', encoding='utf-8')"}),
             {'content': '已根据实际 Python 回执完成'}]
    async def complete(messages, **kwargs):
        return steps.pop(0)
    item.provider.complete = complete
    try:
        job = submit(item, '请在本机运行 Python 生成验收文件')
        result = await completed(item, job['id'])
        assert (item.root / 'python-auto.txt').read_text('utf-8') == 'temporary Python output'
        assert 'auto-python-receipt-39' in json.dumps(result['events'])
        assert not any(event['type'] == 'approval' for event in result['events'])
    finally:
        await item.runs.close()


def test_automatic_mode_still_rejects_untrusted_or_nonadministrator_identity(automatic):
    item = automatic
    for identity in (item.access.identity('onebot', '202', '9000'), {'admin': True},
                     {'platform': 'local', 'account_id': 'shiori', 'proof': 'forged'}):
        with pytest.raises(PermissionError):
            item.service.submit('web:default', '202', identity, '请运行电脑命令')
    assert item.runs.list() == []


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['disable', 'revoke', 'source_deleted', 'scope_disabled', 'tools_disabled', 'pause', 'stop', 'mode_changed'])
async def test_guard_changes_after_auto_authorization_prevent_the_write(automatic, monkeypatch, change):
    item = automatic
    async def complete(messages, **kwargs):
        return call('write_file', {'path': 'must-not-exist.txt', 'content': 'revoked operation'})
    item.provider.complete = complete
    original = item.runs._event
    def event(job_id, kind, data):
        original(job_id, kind, data)
        if kind != 'authorized':
            return
        if change == 'disable':
            item.settings.update({'computer': {'enabled': False}})
        elif change == 'revoke':
            item.settings.update({'access': {'administrators': []}})
        elif change == 'source_deleted':
            item.store.clear_messages('web:default')
        elif change == 'scope_disabled':
            item.store.upsert_scope({'id': 'web:default', 'enabled': False})
        elif change == 'tools_disabled':
            item.store.upsert_scope({'id': 'web:default', 'allow_tools': False})
        elif change == 'pause':
            item.settings.update({'paused': True})
        elif change == 'stop':
            item.runs.cancel(job_id)
        else:
            item.settings.update({'computer': {'approval_mode': 'per_action'}})
    monkeypatch.setattr(item.runs, '_event', event)
    try:
        job = submit(item)
        await wait_for(lambda: item.runs.get(job['id'])['status'] in TERMINAL)
        result = item.runs.get(job['id'])
        assert result['status'] == ('cancelled' if change == 'stop' else 'failed'), result
        assert not (item.root / 'must-not-exist.txt').exists()
        assert not any(event['type'] == 'approval' for event in result['events'])
    finally:
        await item.runs.close()


@pytest.mark.anyio
async def test_stop_terminates_a_running_automatic_python_child(automatic):
    item = automatic
    async def complete(messages, **kwargs):
        return call('execute_python', {'code': "from pathlib import Path\nimport time\nPath('child-started.txt').write_text('started')\ntime.sleep(4)\nPath('must-not-finish.txt').write_text('late effect')"})
    item.provider.complete = complete
    try:
        job = submit(item, '请在本机运行临时 Python 脚本')
        await wait_for(lambda: (item.root / 'child-started.txt').exists())
        item.runs.cancel(job['id'])
        await wait_for(lambda: item.runs.get(job['id'])['status'] == 'cancelled')
        await wait_for(lambda: job['id'] not in item.runs._tasks)
        assert not (item.root / 'must-not-finish.txt').exists()
    finally:
        await item.runs.close()


@pytest.mark.anyio
async def test_optional_per_action_mode_still_waits_for_an_explicit_approval(automatic):
    item = automatic
    item.settings.update({'computer': {'approval_mode': 'per_action'}})
    assert not item.service.automatic()
    steps = [call('write_file', {'path': 'per-action.txt', 'content': 'approved isolated write'}), {'content': '完成'}]
    async def complete(messages, **kwargs):
        return steps.pop(0)
    item.provider.complete = complete
    try:
        job = submit(item)
        await wait_for(lambda: item.runs.get(job['id'])['status'] == 'awaiting_approval')
        assert not (item.root / 'per-action.txt').exists()
        pending = item.runs.get(job['id'])['approval']['id']
        item.runs.approve(job['id'], pending)
        result = await completed(item, job['id'])
        assert (item.root / 'per-action.txt').read_text('utf-8') == 'approved isolated write'
        assert not any(event['type'] == 'authorized' for event in result['events'])
    finally:
        await item.runs.close()


def test_regular_chat_explicit_computer_request_starts_an_automatic_background_task(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'chat-computer', start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        workspace = tmp_path / 'chat-output'
        result = client.put('/api/settings', json={'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(workspace)}})
        assert result.status_code == 200, result.text
        counts = {'chat': 0, 'computer': 0}
        async def complete(messages, **kwargs):
            purpose = kwargs.get('purpose', 'chat')
            assert kwargs['before_send']()
            counts[purpose] += 1
            if purpose == 'computer':
                response = call('write_file', {'path': 'from-chat.txt', 'content': 'ordinary chat triggered automatic task'}) if counts[purpose] == 1 else {'content': '已实际生成文件'}
            elif counts[purpose] == 1:
                assert 'start_computer_task' in {item['function']['name'] for item in kwargs['tools']}
                response = call('start_computer_task', {'request': 'model-substituted task must be ignored'})
            else:
                response = {'content': '电脑任务已在后台处理'}
            message = dict(role='assistant', content=response.get('content', ''))
            if response.get('tool_calls'):
                message['tool_calls'] = response['tool_calls']
            return dict(response, usage={}, message=message)
        monkeypatch.setattr(app.state.provider, 'complete', complete)
        text = '请在本机创建文件 from-chat.txt'
        response = client.post('/api/chat', json={'text': text})
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            jobs = client.get('/api/jobs').json()
            if jobs and jobs[0]['status'] in TERMINAL:
                break
            time.sleep(.01)
        assert len(jobs) == 1 and jobs[0]['kind'] == 'computer' and jobs[0]['status'] == 'completed', jobs
        assert (workspace / 'from-chat.txt').read_text('utf-8') == 'ordinary chat triggered automatic task'
        assert app.state.runs._jobs[jobs[0]['id']]['_payload']['text'] == text
        assert not any(event['type'] == 'approval' for event in jobs[0]['events'])
