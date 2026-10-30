"""Verify v0.9 workflows against an EXE with isolated data and a local fake model.

This does not import shiori source, log in to QQ, contact a paid model, enable
remote management, or change autostart/firewall settings. All computer actions
write only inside the fresh QA data directory selected by the caller.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


MODEL_NAMES = ['workflow-alpha', 'workflow-beta']
SOURCE_KEY = 'workflow-local-fixture-source-key-393939'
ONEBOT_TOKEN = 'workflow-local-fixture-onebot-token-393939'
NORMAL_MARKER = 'WORKFLOW-CHAT-39'
COMPUTER_MARKER = 'WORKFLOW-COMPUTER-39'
STOP_MARKER = 'WORKFLOW-STOP-39'
QQ_MARKER = 'WORKFLOW-QQ-39'
WRITE_TEXT = 'workflow-v9-write：本机文件结果'
PYTHON_TEXT = 'workflow-v9-python：真实子进程结果'
PYTHON_OUTPUT = 'workflow-v9-python-stdout-39'


def tool_call(name, arguments, id):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': id, 'type': 'function', 'function': {'name': name,
            'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


def fixture_reply(body):
    """A deterministic protocol fixture, never a substitute for model-quality QA."""
    names = {item.get('function', {}).get('name') for item in body.get('tools', [])}
    messages = body.get('messages', [])
    tool_results = [item for item in messages if item.get('role') == 'tool']
    last_user = next((str(item.get('content', '')) for item in reversed(messages) if item.get('role') == 'user'), '')
    if {'write_file', 'execute_python'} <= names:
        initiating_text = str(messages[1].get('content', '')) if len(messages) > 1 else ''
        if STOP_MARKER in initiating_text:
            if not tool_results:
                code = ("from pathlib import Path\nimport os, time\n"
                        "Path('workflow-stop-pid.txt').write_text(str(os.getpid()), encoding='utf-8')\n"
                        "print('workflow-stop-child-started', flush=True)\n"
                        "time.sleep(20)\n"
                        "Path('workflow-stop-forbidden.txt').write_text('must-not-run-after-cancel', encoding='utf-8')\n")
                return tool_call('execute_python', {'code': code}, 'workflow-stop-python'), 'computer_stop'
            return {'role': 'assistant', 'content': '隔离停止测试的 Python 已返回。'}, 'computer_stop_finished'
        if not tool_results:
            return tool_call('write_file', {'path': 'workflow-note.txt', 'content': WRITE_TEXT}, 'workflow-write'), 'computer_write'
        prior = [json.loads(item.get('content', '{}')) for item in tool_results]
        if any(not isinstance(item, dict) or item.get('ok') is not True for item in prior):
            return {'role': 'assistant', 'content': '脚本模型检测到实际工具失败，已有结果需检查。'}, 'computer_failed'
        if len(tool_results) == 1:
            code = ("from pathlib import Path\n"
                    f"print({PYTHON_OUTPUT!r})\n"
                    f"Path('workflow-python.txt').write_text({PYTHON_TEXT!r}, encoding='utf-8')\n")
            return tool_call('execute_python', {'code': code}, 'workflow-python'), 'computer_python'
        return {'role': 'assistant', 'content': '依据实际工具回执，文件写入和 Python 子进程均已完成。'}, 'computer_completed'
    if 'start_computer_task' in names and (COMPUTER_MARKER in last_user or STOP_MARKER in last_user):
        if not tool_results:
            return tool_call('start_computer_task', {'request': last_user}, 'workflow-start-task'), 'start_computer_task'
        return {'role': 'assistant', 'content': '后台任务已创建，可以在任务记录中查看实际进度或停止。'}, 'task_submitted'
    return {'role': 'assistant', 'content': 'workflow-chat:' + str(body.get('model', ''))}, 'chat'


@contextmanager
def local_model_fixture():
    protocol = []
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def send(self, status, value):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def authorize(self):
            valid = self.headers.get('Authorization') == 'Bearer ' + SOURCE_KEY
            if not valid: self.send(401, {'error': 'fixture authorization mismatch'})
            return valid
        def do_GET(self):
            if not self.authorize(): return
            if self.path != '/v1/models': self.send(404, {'error': 'fixture route missing'}); return
            with lock: protocol.append({'phase': 'models', 'method': 'GET', 'authorization_matched': True})
            self.send(200, {'data': [{'id': name} for name in MODEL_NAMES]})
        def do_POST(self):
            if not self.authorize(): return
            if self.path != '/v1/chat/completions': self.send(404, {'error': 'fixture route missing'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 * 1024: raise ValueError('invalid size')
                body = json.loads(self.rfile.read(length))
                if body.get('model') not in MODEL_NAMES: raise ValueError('unexpected model')
                message, phase = fixture_reply(body)
            except (ValueError, TypeError, KeyError):
                self.send(400, {'error': 'fixture request does not match protocol'}); return
            with lock:
                protocol.append({'phase': phase, 'model': body['model'], 'authorization_matched': True,
                    'tool_names': sorted(item['function']['name'] for item in body.get('tools', [])),
                    'tool_results': sum(item.get('role') == 'tool' for item in body.get('messages', []))})
            self.send(200, {'choices': [{'message': message}], 'usage': {'prompt_tokens': 40, 'completion_tokens': 20}})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', protocol
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


class Client:
    def __init__(self, url):
        self.url = url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies))
        self.last_headers = None
    def request(self, path, payload=None, method=None, *, expected=200, headers=None):
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(self.url + path, raw, method=method,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(8 * 1024 * 1024 + 1)
            assert len(body) <= 8 * 1024 * 1024, 'Unexpectedly large smoke-test response'
            value = json.loads(body) if 'application/json' in response.headers.get('Content-Type', '') else body
            self.last_headers = response.headers
            assert response.code == expected, (path, response.code, value)
            return value


def wait_until(predicate, *, timeout=25, message='Workflow condition timed out'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(.08)
    raise AssertionError(message)


def wait_job(client, id, statuses, *, timeout=30):
    observed = set()
    def poll():
        job = client.request('/api/jobs/' + id)
        observed.add(job['status'])
        assert job['status'] != 'awaiting_approval', 'Automatic computer task unexpectedly required approval'
        assert not any(event['type'] == 'approval' for event in job.get('events', [])), 'Automatic job produced an approval event'
        if job['status'] in statuses: return job
        if job['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
            raise AssertionError('Unexpected job terminal state: ' + json.dumps(job, ensure_ascii=False))
        return None
    return wait_until(poll, timeout=timeout), observed


def task_from_chat(result):
    matches = [item['result']['job_id'] for item in result.get('actions', [])
        if item.get('tool') == 'start_computer_task' and item.get('ok') and isinstance(item.get('result'), dict) and item['result'].get('job_id')]
    assert len(matches) == 1, 'Normal chat did not create exactly one START_TOOL computer task'
    return matches[0]


def process_active(pid):
    if os.name != 'nt':
        try: os.kill(pid, 0)
        except ProcessLookupError: return False
        return True
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return False
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)): raise OSError('Cannot verify isolated child process exit')
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


def smoke_qq(client, model_id, port):
    from websockets.sync.client import connect
    client.request('/api/settings', {'onebot': {'enabled': True, 'adapter': 'napcat', 'ws_url': '',
        'access_token': ONEBOT_TOKEN, 'allowed_users': ['101'], 'allowed_groups': []}}, 'PUT')
    client.request('/api/scopes/qq:private:101', {'model_id': model_id, 'allow_tools': False, 'auto_memory': False}, 'PUT')
    with connect(f'ws://127.0.0.1:{port}/onebot/v11/ws', additional_headers={
            'Authorization': 'Bearer ' + ONEBOT_TOKEN, 'X-Self-ID': '999'}, proxy=None, open_timeout=5) as ws:
        ws.send(json.dumps({'post_type': 'meta_event', 'meta_event_type': 'heartbeat', 'self_id': 999}))
        ws.send(json.dumps({'post_type': 'message', 'message_type': 'private', 'self_id': 999,
            'message_id': 3939, 'user_id': 101, 'message': [{'type': 'text', 'data': {'text': QQ_MARKER}}]}))
        action = json.loads(ws.recv(timeout=15))
        assert action['action'] == 'send_private_msg' and action['params']['user_id'] == 101
        assert action['params']['message'][0]['data']['text'] == 'workflow-chat:workflow-beta'
        ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': action['echo'], 'data': {'message_id': 9393}}))
        status = wait_until(lambda: (value if (value.get('last_message_result') or {}).get('code') == 'sent' else None)
            if (value := client.request('/api/qq/status')) else None)
        assert status['message_events_received'] == 1 and status['heartbeat_events_received'] == 1
        scope = next(item for item in client.request('/api/scopes') if item['id'] == 'qq:private:101')
        assert scope['always_reply'] and scope['plan_enabled'] is False
    client.request('/api/settings', {'onebot': {'enabled': False}}, 'PUT')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18915)
    parser.add_argument('--expect-version', default='0.2.1-beta')
    args = parser.parse_args()
    exe, data = args.exe.resolve(), args.data_dir.resolve()
    if not exe.is_file(): parser.error('--exe must point to the compiled shiori executable')
    if data.exists(): parser.error('Use a fresh, nonexistent, disposable QA data directory')
    if not 1 <= args.port <= 65535: parser.error('Invalid port')
    client = Client(f'http://127.0.0.1:{args.port}')
    process = None
    token = ''
    report = {'version': args.expect_version, 'passed': False}
    with local_model_fixture() as (model_url, protocol):
        try:
            process = subprocess.Popen([str(exe), '--data-dir', str(data), '--port', str(args.port), '--host', '127.0.0.1',
                '--no-browser', '--no-tray'], cwd=exe.parent, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def ready():
                if process.poll() is not None: raise AssertionError('Compiled app exited before startup')
                try:
                    record = json.loads((data / 'server.json').read_text('utf-8'))
                    if record.get('pid') != process.pid: return None
                    client.url = f'http://127.0.0.1:{record["port"]}'
                    health = client.request('/healthz')
                    return record if health.get('version') == args.expect_version else None
                except (OSError, ValueError): return None
            record = wait_until(ready, timeout=35, message='Compiled app did not become ready')
            token = (data / 'admin.token').read_text('utf-8').strip()
            client.request('/api/bootstrap', expected=401)
            client.request('/api/auth/bootstrap', {'token': token})
            client.request('/api/bootstrap', expected=401)
            client.request('/api/auth/setup', {'mode': 'password', 'password': 'QA-Workflow-Password-39!'})
            assert client.request('/api/auth/status')['authenticated']

            source = client.request('/api/model-providers', {'name': '离线验收供应商', 'base_url': model_url,
                'api_key': SOURCE_KEY, 'enabled': True})
            assert source['api_key_set'] and SOURCE_KEY not in json.dumps(source)
            discovered = client.request('/api/model-providers/' + source['id'] + '/discover', {})
            assert discovered['models'] == MODEL_NAMES
            catalog = client.request('/api/model-catalog/models', {'provider_id': source['id'], 'models': discovered['models']})
            assert len(catalog['added']) == 2
            by_model = {item['model']: item['id'] for item in catalog['added']}
            client.request('/api/model-catalog/routing', {'chat': by_model['workflow-alpha']}, 'PUT')
            scope = client.request('/api/scopes', {'name': '完整功能编译验收', 'kind': 'web', 'auto_memory': False,
                'model_id': by_model['workflow-beta'], 'allow_tools': True, 'allow_mcp': False})
            settings_raw = (data / 'settings.json').read_text('utf-8')
            assert SOURCE_KEY not in settings_raw
            reply = client.request('/api/chat', {'scope_id': scope['id'], 'text': NORMAL_MARKER})
            assert reply['reply'] == 'workflow-chat:workflow-beta'
            assert protocol[-1]['model'] == 'workflow-beta' and protocol[-1]['authorization_matched']
            report.update(provider_saved=True, model_list_discovered=True, bulk_models_added=2, session_model_routed=True)

            default_computer = client.request('/api/settings')['computer']
            assert default_computer['approval_mode'] == 'automatic' and default_computer['enabled'] is False
            workspace = data / 'workflow-computer-workspace'
            client.request('/api/settings', {'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(workspace),
                'max_steps': 8, 'timeout_seconds': 30}}, 'PUT')
            request_text = '请在本机目录写入文件并运行 Python 脚本生成结果。' + COMPUTER_MARKER
            result = client.request('/api/chat', {'scope_id': scope['id'], 'text': request_text})
            job_id = task_from_chat(result)
            done, observed = wait_job(client, job_id, {'completed'})
            assert (workspace / 'workflow-note.txt').read_text('utf-8') == WRITE_TEXT
            assert (workspace / 'workflow-python.txt').read_text('utf-8') == PYTHON_TEXT
            tool_events = [item['data'] for item in done['events'] if item['type'] == 'tool_result']
            assert [item['tool'] for item in tool_events] == ['write_file', 'execute_python']
            assert all(item['result'].get('ok') is True for item in tool_events)
            assert PYTHON_OUTPUT in tool_events[1]['result']['output']
            authorized = [item['data'] for item in done['events'] if item['type'] == 'authorized']
            assert len(authorized) == 2 and all(item['mode'] == 'automatic' for item in authorized)
            artifact = next(item for item in done['result']['artifacts'] if item['name'] == 'workflow-python.txt')
            assert client.request(artifact['url']).decode('utf-8') == PYTHON_TEXT
            report.update(chat_started_computer_task=True, automatic_execution=True, awaiting_approval=False,
                ordered_real_tools=['write_file', 'execute_python'], artifact_download=True)

            stop_result = client.request('/api/chat', {'scope_id': scope['id'],
                'text': '请运行本机 Python 脚本执行隔离的停止功能验收。' + STOP_MARKER})
            stop_id = task_from_chat(stop_result)
            pid_file = workspace / 'workflow-stop-pid.txt'
            def child_started():
                try:
                    value = pid_file.read_text('utf-8').strip()
                    return int(value) if value.isdecimal() and int(value) > 0 else None
                except OSError:
                    return None
            child_pid = wait_until(child_started, timeout=15, message='Stop-test Python child did not start')
            assert process_active(child_pid)
            client.request('/api/jobs/' + stop_id + '/cancel', {})
            stopped, _ = wait_job(client, stop_id, {'cancelled'})
            wait_until(lambda: not process_active(child_pid), timeout=8, message='Cancelled job left its Python child running')
            assert not (workspace / 'workflow-stop-forbidden.txt').exists()
            report.update(stop_cancelled=True, cancelled_child_exited=True)

            assert smoke_qq(client, by_model['workflow-beta'], record['port'])
            report.update(qq_reverse_private_reply=True, qq_delivery_ack=True)

            marker = 'workflow-log-redaction-39'
            with (data / 'runtime.log').open('a', encoding='utf-8') as stream:
                stream.write(f'2026-10-18 12:00:00 ERROR {marker} api_key={SOURCE_KEY} Bearer {token}\n')
            logs = client.request('/api/logs?' + urllib.parse.urlencode({'q': marker, 'level': 'ERROR'}))
            assert logs['lines'] and any(item['source'] == 'runtime' for item in logs['lines'])
            log_json = json.dumps(logs, ensure_ascii=False)
            assert SOURCE_KEY not in log_json and token not in log_json and '[已隐藏]' in log_json
            exported = client.request('/api/logs/export?' + urllib.parse.urlencode({'q': marker})).decode('utf-8')
            assert marker in exported and SOURCE_KEY not in exported and token not in exported
            assert 'attachment;' in client.last_headers.get('Content-Disposition', '')
            report.update(runtime_logs_readable=True, runtime_logs_redacted=True, runtime_logs_export=True)
        finally:
            if process and process.poll() is None:
                try:
                    if token:
                        client.request('/api/shutdown', {}, headers={'Authorization': 'Bearer ' + token})
                        assert process.wait(timeout=20) == 0
                    else:
                        process.terminate(); process.wait(timeout=5)
                except BaseException:
                    if process.poll() is None:
                        process.terminate(); process.wait(timeout=5)
                    raise
            if data.exists():
                assert not (data / 'server.json').exists(), 'Server runtime record was not cleaned up'
        report['passed'] = True
        (data / 'workflow-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (data / 'workflow-mock-protocol.json').write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
