"""Inline local tools use real files/jobs; no QQ or billed model is contacted."""
import asyncio
import copy
import json
import os
import threading

import pytest

from shiori.api import create_app
from shiori.startup import _process_alive


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def local(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', start_workers=False)
    app.state.settings.update({'computer': {'enabled': True, 'runtime': 'local',
        'approval_mode': 'automatic', 'workspace': str(tmp_path / 'workspace'), 'timeout_seconds': 10}})
    harness = app.state.chat.local_harness
    events = []
    monkeypatch.setattr(harness, '_emit', lambda job: events.append(copy.deepcopy(job)))
    async def forbidden_model(*args, **kwargs):
        pytest.fail('inline tools must not start a separate model loop')
    monkeypatch.setattr(app.state.provider, 'complete', forbidden_model)
    identity = app.state.access.identity('local', 'shiori')
    source = app.state.store.add_message('web:default', 'owner', 'user', 'Work on the fixture project')
    metadata = {'trusted_identity': identity, 'source_ids': [source['id']], '_context_source_ids': [source['id']]}
    async with app.router.lifespan_context(app):
        yield app, harness, metadata, events


async def waiting_job(app):
    async def find():
        while True:
            found = next((job for job in app.state.runs.list() if job['status'] == 'awaiting_approval'), None)
            if found:
                return found
            await asyncio.sleep(.01)
    return await asyncio.wait_for(find(), 3)


@pytest.mark.anyio
async def test_local_tools_reuse_enabled_permissions_without_qq_expansion(local):
    app, harness, metadata, _ = local
    assert {item['function']['name'] for item in harness.tools('web:default', 'owner', metadata)} == {
        'list_files', 'read_file', 'search_files', 'write_file', 'edit_file', 'execute_shell', 'execute_python'}
    assert harness.tools('qq:private:42', 'owner', metadata) == []
    assert harness.tools('web:default', 'owner', {'trusted_identity': {'platform': 'local', 'account_id': 'shiori'}}) == []
    app.state.settings.update({'computer': {'enabled': False}})
    assert harness.tools('web:default', 'owner', metadata) == []
    with pytest.raises(PermissionError):
        await harness.execute('list_files', {}, 'web:default', 'owner', metadata)
    assert not app.state.runs.list()


@pytest.mark.anyio
async def test_multiple_inline_file_tools_return_results_artifacts_and_no_duplicate_reply(local):
    app, harness, metadata, events = local
    async def execute(name, args):
        return await harness.execute(name, args, 'web:default', 'owner', metadata, before_send=lambda: True)
    written = await execute('write_file', {'path': 'notes.txt', 'content': 'first line'})
    assert written['ok'] and written['artifacts'][0]['url'].startswith('/api/jobs/')
    assert written['artifacts'][0]['sha256']
    edited = await execute('edit_file', {'path': 'notes.txt', 'old': 'first', 'new': 'second'})
    assert edited['ok']
    assert (await execute('read_file', {'path': 'notes.txt'}))['output']['text'] == 'second line'
    assert (await execute('list_files', {}))['output'] == [{'name': 'notes.txt', 'directory': False}]
    assert (await execute('search_files', {'query': 'second'}))['output'][0]['line'] == 1
    assert len(app.state.store.list_messages('web:default')) == 1, 'inline completion must not add an assistant reply'
    assert all(job['inline'] and job['delivery_status'] == 'inline' for job in app.state.runs.list())
    assert all(job['status'] == 'completed' for job in app.state.runs.list())
    assert len({event['id'] for event in events}) == 5 and events[-1]['status'] == 'completed'
    assert not harness._active


@pytest.mark.anyio
async def test_tool_error_is_a_durable_receipt_and_next_tool_can_continue(local):
    app, harness, metadata, _ = local
    failed = await harness.execute('read_file', {'path': '../outside.txt'}, 'web:default', 'owner', metadata)
    assert failed['status'] == 'failed' and failed['ok'] is False and failed['error']
    assert app.state.runs.get(failed['job_id'])['status'] == 'failed'
    listed = await harness.execute('list_files', {}, 'web:default', 'owner', metadata)
    assert listed['ok'] and listed['output'] == []
    assert len(app.state.store.list_messages('web:default')) == 1


@pytest.mark.anyio
async def test_file_read_can_continue_by_unicode_characters_and_retains_size_limit(local):
    app, harness, metadata, _ = local
    app.state.settings.update({'computer': {'max_output_chars': 1000}})
    root = harness.computer.workspace()
    text = '甲😀乙' * 700
    (root / 'long.txt').write_text(text, 'utf-8')
    first = (await harness.execute('read_file', {'path': 'long.txt'}, 'web:default', 'owner', metadata))['output']
    assert first['text'] == text[:1000] and first['truncated'] and first['next_offset'] == 1000
    assert first['total_characters'] == len(text) and first['characters'] == 1000
    second = (await harness.execute('read_file', {'path': 'long.txt', 'offset': first['next_offset'], 'limit': 100000},
        'web:default', 'owner', metadata))['output']
    final = (await harness.execute('read_file', {'path': 'long.txt', 'offset': second['next_offset'], 'limit': 100},
        'web:default', 'owner', metadata))['output']
    assert first['text'] + second['text'] + final['text'] == text
    assert not final['truncated'] and final['next_offset'] is None
    (root / 'oversized.txt').write_bytes(b'x' * (1024 * 1024 + 1))
    denied = await harness.execute('read_file', {'path': 'oversized.txt', 'limit': 1}, 'web:default', 'owner', metadata)
    assert denied['status'] == 'failed' and '1MB' in denied['error']


@pytest.mark.anyio
@pytest.mark.parametrize('arguments', [{'offset': -1}, {'limit': 0}, {'offset': True}, {'limit': 100001}])
async def test_read_pagination_rejects_invalid_arguments_before_creating_job(local, arguments):
    app, harness, metadata, _ = local
    with pytest.raises(ValueError, match='参数'):
        await harness.execute('read_file', {'path': 'fixture.txt', **arguments}, 'web:default', 'owner', metadata)
    assert not app.state.runs.list()


@pytest.mark.anyio
async def test_per_action_approval_preserves_pending_state_then_continues(local):
    app, harness, metadata, events = local
    app.state.settings.update({'computer': {'approval_mode': 'per_action'}})
    task = asyncio.create_task(harness.execute('write_file', {'path': 'approved.txt', 'content': 'approved'},
        'web:default', 'owner', metadata))
    try:
        job = await waiting_job(app)
        assert job['inline'] and not (harness.computer.workspace() / 'approved.txt').exists()
        # Let the pending state become visible through the request's event sink.
        async def visible():
            while not any(event['status'] == 'awaiting_approval' for event in events):
                await asyncio.sleep(.01)
        await asyncio.wait_for(visible(), 2)
        app.state.runs.approve(job['id'], job['approval']['id'])
        result = await asyncio.wait_for(task, 3)
        assert result['ok'] and result['output']['characters'] == 8
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['permission', 'source', 'new_message', 'attachment', 'guard'])
async def test_pending_tool_stops_on_revoked_source_permission_or_new_request(local, change):
    app, harness, metadata, _ = local
    app.state.settings.update({'computer': {'approval_mode': 'per_action'}})
    current = {'value': True}
    if change == 'attachment':
        record = app.state.attachments.create('web:default', 'fixture.txt', b'fixture', {'text': 'fixture'}, 'context')
        metadata['reference_attachments'] = app.state.attachments.resolve('web:default', [record['id']])
    task = asyncio.create_task(harness.execute('write_file', {'path': 'denied.txt', 'content': 'must not write'},
        'web:default', 'owner', metadata, before_send=lambda: current['value']))
    try:
        job = await waiting_job(app)
        if change == 'permission':
            app.state.settings.update({'computer': {'enabled': False}})
        elif change == 'source':
            source = app.state.store.get_message(metadata['source_ids'][0])
            source['content'] = 'Do something different'
            with app.state.store._lock, app.state.store._conn:
                app.state.store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(source), source['id']))
        elif change == 'new_message':
            app.state.store.add_message('web:default', 'owner', 'user', 'Stop the old request')
        elif change == 'attachment':
            app.state.attachments.delete('web:default', record['id'])
        else:
            current['value'] = False
        with pytest.raises(PermissionError):
            app.state.runs.approve(job['id'], job['approval']['id'])
        with pytest.raises(PermissionError):
            await asyncio.wait_for(task, 3)
        assert not (harness.computer.workspace() / 'denied.txt').exists()
        assert app.state.runs.get(job['id'])['status'] in ('failed', 'cancelled')
        assert not harness._active and not app.state.runs._tasks
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_real_python_output_and_nonzero_shell_result_return_inline(local):
    app, harness, metadata, _ = local
    owner_thread = threading.get_ident()
    def check():
        assert threading.get_ident() == owner_thread
        asyncio.get_running_loop()
        return True
    result = await harness.execute('execute_python', {'code': "print('fixture inline output')"},
        'web:default', 'owner', metadata, before_send=check)
    assert result['ok'] and 'fixture inline output' in result['output']['output']
    command = 'Write-Output fixture; exit 7' if os.name == 'nt' else 'printf fixture; exit 7'
    result = await harness.execute('execute_shell', {'command': command}, 'web:default', 'owner', metadata, before_send=check)
    assert result['status'] == 'failed' and not result['ok']
    assert result['output']['exit_code'] == 7 and 'fixture' in result['output']['output']


@pytest.mark.anyio
async def test_cancel_chat_drains_real_subprocess_before_return(local):
    app, harness, metadata, _ = local
    root = harness.computer.workspace()
    code = "import os,time\nfrom pathlib import Path\nPath('child.pid').write_text(str(os.getpid()))\ntime.sleep(30)\nPath('should-not-exist').write_text('late')"
    task = asyncio.create_task(harness.execute('execute_python', {'code': code}, 'web:default', 'owner', metadata))
    try:
        async def started():
            while not (root / 'child.pid').exists():
                await asyncio.sleep(.02)
            return int((root / 'child.pid').read_text())
        pid = await asyncio.wait_for(started(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        assert not _process_alive(pid), 'cancel must drain the child before returning to chat'
        assert not (root / 'should-not-exist').exists()
        assert app.state.runs.list()[0]['status'] == 'cancelled'
        assert not harness._active and not app.state.runs._tasks
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
