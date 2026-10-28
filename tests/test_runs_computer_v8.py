"""Background lifecycle and administrator guards, using isolated temp files."""
import asyncio
import copy
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from shiori.computer import ComputerService
from shiori.computer_worker import execute as execute_worker
from shiori.runs import RunManager


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError('background task did not reach expected state')


@pytest.mark.anyio
async def test_cancel_queued_run_before_coroutine_starts_is_durable(tmp_path):
    runs = RunManager(tmp_path)
    executed = []
    async def handler(ctx, payload):
        executed.append(True)
        return {'ok': True}
    runs.register('test', handler)
    job = runs.submit('test', {}, identity={'platform': 'local', 'user_id': 'shiori'})
    runs.cancel(job['id'])
    await runs.close()
    assert not executed
    assert runs.get(job['id'])['status'] in ('cancelled', 'interrupted')
    assert not runs._contexts and not runs._tasks
    restored = RunManager(tmp_path)
    assert restored.get(job['id'])['status'] in ('cancelled', 'interrupted')


@pytest.mark.anyio
async def test_approval_is_single_use_and_resumes_original_task(tmp_path):
    runs = RunManager(tmp_path)
    writes = []
    async def handler(ctx, payload):
        await ctx.approve('写临时文件', {'path': 'output.txt', 'preview': 'sample'})
        ctx.check()
        writes.append('sample')
        return {'reply': 'completed'}
    runs.register('test', handler)
    job = runs.submit('test', {}, identity={'platform': 'local', 'user_id': 'shiori'})
    await wait_for(lambda: runs.get(job['id'])['status'] == 'awaiting_approval')
    approval = runs.get(job['id'])['approval']['id']
    with pytest.raises(ValueError, match='失效'):
        runs.approve(job['id'], 'different')
    runs.approve(job['id'], approval)
    with pytest.raises(ValueError, match='失效'):
        runs.approve(job['id'], approval)
    await wait_for(lambda: runs.get(job['id'])['status'] == 'completed')
    assert writes == ['sample']
    assert runs.get(job['id'])['result']['reply'] == 'completed'
    await runs.close()


@pytest.mark.anyio
async def test_admin_revocation_before_approval_prevents_action(tmp_path):
    runs = RunManager(tmp_path)
    access = {'allowed': True}
    actions = []
    async def handler(ctx, payload):
        await ctx.approve('操作', {'value': 'sample'})
        actions.append('executed')
    runs.register('test', handler, guard=lambda job: access['allowed'])
    job = runs.submit('test', {}, identity={'platform': 'onebot', 'user_id': '111'})
    await wait_for(lambda: runs.get(job['id'])['status'] == 'awaiting_approval')
    approval = runs.get(job['id'])['approval']['id']
    access['allowed'] = False
    with pytest.raises(PermissionError):
        runs.approve(job['id'], approval)
    await wait_for(lambda: runs.get(job['id'])['status'] == 'failed')
    assert not actions and runs.get(job['id'])['approval'] is None
    await runs.close()


@pytest.mark.anyio
async def test_restarted_run_never_replays_unfinished_operations(tmp_path):
    runs = RunManager(tmp_path)
    blocker = asyncio.Event()
    async def handler(ctx, payload):
        ctx.checkpoint(pending_tool={'name': 'write_file', 'arguments': {'path': 'output.txt'}})
        await blocker.wait()
    runs.register('test', handler)
    job = runs.submit('test', {}, identity={'platform': 'local', 'user_id': 'shiori'})
    await wait_for(lambda: runs.get(job['id'])['status'] == 'running')
    restored = RunManager(tmp_path)
    assert restored.get(job['id'])['status'] == 'interrupted'
    assert not restored._tasks and not restored._contexts
    assert restored.get(job['id'])['approval'] is None
    await runs.close()


@pytest.mark.anyio
async def test_cancel_waiting_approval_never_executes(tmp_path):
    runs = RunManager(tmp_path)
    actions = []
    async def handler(ctx, payload):
        await ctx.approve('操作', {'value': 'sample'})
        actions.append(True)
    runs.register('test', handler)
    job = runs.submit('test', {}, identity={'platform': 'local', 'user_id': 'shiori'})
    await wait_for(lambda: runs.get(job['id'])['status'] == 'awaiting_approval')
    approval = runs.get(job['id'])['approval']['id']
    runs.cancel(job['id'])
    await wait_for(lambda: runs.get(job['id'])['status'] == 'cancelled')
    with pytest.raises(ValueError):
        runs.approve(job['id'], approval)
    assert not actions
    await runs.close()


@pytest.fixture
def computer(tmp_path):
    values = {'paused': False, 'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(tmp_path / 'workspace'),
                                          'max_steps': 5, 'max_output_chars': 8000, 'timeout_seconds': 5}}
    settings = SimpleNamespace(get=lambda: copy.deepcopy(values))
    access = SimpleNamespace(is_admin=lambda identity: bool(identity.get('admin')))
    store = SimpleNamespace(get_scope=lambda scope: {'id': scope, 'enabled': True, 'allow_tools': True})
    runs = RunManager(tmp_path)
    service = ComputerService(store, settings, None, access, runs, tmp_path)
    return service, values, runs


def context(allowed=True):
    def check():
        if not allowed:
            raise PermissionError('revoked')
        return True
    return SimpleNamespace(id='isolated-test', check=check)


def test_computer_admin_gate_and_runtime_switch(computer):
    service, settings, _ = computer
    assert service.allowed({'admin': True}, 'web:test')
    assert not service.allowed({'admin': False}, 'web:test')
    settings['computer']['enabled'] = False
    assert not service.allowed({'admin': True}, 'web:test')
    settings['computer']['enabled'] = True
    settings['paused'] = True
    assert not service.allowed({'admin': True}, 'web:test')


def test_file_path_guard_denies_parent_and_link_escape(computer, tmp_path):
    service, _, _ = computer
    workspace = service.workspace()
    with pytest.raises(ValueError, match='工作目录'):
        service.path(workspace, '../outside.txt')
    assert service.path(workspace, 'nested/result.txt').is_relative_to(workspace)
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (workspace / 'linked').symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # Windows without Developer Mode: traversal check still ran.
    with pytest.raises(ValueError, match='链接'):
        service.path(workspace, 'linked/escape.txt')


def test_process_rechecks_revocation_before_starting_or_creating_script(computer, monkeypatch):
    service, _, _ = computer
    workspace = service.workspace()
    started = []
    def forbidden(*args, **kwargs):
        started.append(True)
        raise AssertionError('must not start a revoked process')
    monkeypatch.setattr('qunyu.computer.subprocess.Popen', forbidden)
    with pytest.raises(PermissionError, match='revoked'):
        service._process(context(False), workspace, 'execute_python', "print('sample')")
    assert not started and not (service.data_dir / 'computer_history').exists()


def test_frozen_python_uses_special_child_entry_without_bot_startup(computer, monkeypatch):
    service, _, _ = computer
    captured = {}
    class Process:
        stdout = io.BytesIO()
        returncode = 0
        def poll(self):
            return 0
    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Process()
    monkeypatch.setattr('qunyu.computer.sys.frozen', True, raising=False)
    monkeypatch.setattr('qunyu.computer.subprocess.Popen', popen)
    result = service._process(context(), service.workspace(), 'execute_python', "print('sample')")
    assert result['ok'] is True
    assert captured['command'][1] == '--computer-python'
    assert '--computer-output' in captured['command']
    assert Path(captured['command'][2]).read_text('utf-8') == "print('sample')"
    assert captured['kwargs']['cwd'] == service.workspace()


def test_python_worker_only_executes_supplied_temp_script(tmp_path):
    script, output = tmp_path / 'script.py', tmp_path / 'output.txt'
    script.write_text("print('isolated worker sample')", 'utf-8')
    assert execute_worker(script, output) == 0
    assert output.read_text('utf-8').strip() == 'isolated worker sample'
    assert set(path.name for path in tmp_path.iterdir()) == {'script.py', 'output.txt'}


def test_python_process_can_run_from_separate_workspace(computer):
    service, _, _ = computer
    workspace = service.workspace()
    result = service._process(context(), workspace, 'execute_python', "from pathlib import Path\nprint('isolated subprocess sample')\nPath('result.txt').write_text('sample', encoding='utf-8')")
    assert result['ok'], result
    assert 'isolated subprocess sample' in result['output']
    assert (workspace / 'result.txt').read_text('utf-8') == 'sample'


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell output encoding')
def test_powershell_chinese_output_is_utf8_across_read_boundaries(computer):
    service, _, _ = computer
    result = service._process(context(), service.workspace(), 'execute_shell',
                              "[Console]::Write('中文输出39'); [Console]::Write('汉' * 2000)")
    assert result['ok'], result
    assert result['output'] == '中文输出39' + '汉' * 2000


@pytest.mark.anyio
async def test_file_changed_while_approval_pending_is_not_overwritten(computer):
    service, _, _ = computer
    workspace = service.workspace()
    target = workspace / 'sample.txt'
    target.write_text('old text', 'utf-8')
    ctx = context()
    async def approve(summary, details):
        target.write_text('changed externally', 'utf-8')
    ctx.approve = approve
    with pytest.raises(ValueError, match='审批期间发生变化'):
        await service.execute(ctx, workspace, 'write_file', {'path': 'sample.txt', 'content': 'new text'})
    assert target.read_text('utf-8') == 'changed externally'


@pytest.mark.anyio
async def test_file_approval_revocation_cannot_write(computer):
    service, _, _ = computer
    workspace = service.workspace()
    revoked = {'value': False}
    ctx = context()
    async def approve(summary, details):
        revoked['value'] = True
    def check():
        if revoked['value']:
            raise PermissionError('revoked')
        return True
    ctx.check, ctx.approve = check, approve
    with pytest.raises(PermissionError):
        await service.execute(ctx, workspace, 'write_file', {'path': 'sample.txt', 'content': 'new text'})
    assert not (workspace / 'sample.txt').exists()
