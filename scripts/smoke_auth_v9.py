"""Exercise password authentication on a built EXE using disposable local data."""
from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import zipfile


class Client:
    def __init__(self, url):
        self.url = url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, payload=None, *, method=None, expected=200, headers=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.url + path, body, method=method,
            headers={'Content-Type':'application/json', **(headers or {})})
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            value = json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
            assert response.code == expected, (path, response.code, value)
            return value

    def cookie(self, name):
        return next((item for item in self.cookies if item.name == name), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exe', type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18773)
    parser.add_argument('--expect-version', default='0.2.0')
    args = parser.parse_args()
    data, exe = args.data_dir.resolve(), args.exe.resolve()
    assert not data.exists(), 'Use a fresh disposable data directory'
    url = f'http://127.0.0.1:{args.port}'
    anonymous, first, second = Client(url), Client(url), Client(url)
    process = None
    token = ''
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    password_file = data / 'security/webui-password.json'
    alpha, beta, gamma, delta = ('QA-Auth-Alpha-39!', 'QA-Auth-Beta-39!', 'QA-Auth-Gamma-39!', 'QA-Auth-Delta-39!')

    def start():
        nonlocal process, token
        process = subprocess.Popen([str(exe), '--data-dir', str(data), '--port', str(args.port),
            '--host','127.0.0.1','--no-browser','--no-tray'], creationflags=flags)
        for _ in range(120):
            try:
                if anonymous.request('/healthz')['version'] == args.expect_version:
                    token = (data / 'admin.token').read_text('utf-8').strip()
                    return
            except OSError:
                pass
            if process.poll() is not None:
                raise AssertionError('Compiled server exited before readiness')
            time.sleep(.2)
        raise AssertionError('Compiled server readiness timed out')

    def stop():
        nonlocal process
        if process and process.poll() is None:
            anonymous.request('/api/shutdown', {}, headers={'Authorization':'Bearer ' + token})
            assert process.wait(timeout=20) == 0
        assert not (data / 'server.json').exists()
        process = None

    try:
        start()
        assert anonymous.request('/api/auth/status')['setup_required']
        anonymous.request('/api/bootstrap', expected=401)
        first.request('/api/auth/bootstrap', {'token':token})
        assert first.request('/api/auth/status')['local_setup_allowed']
        first.request('/api/bootstrap', expected=401)  # Setup proof never logs in.
        first.request('/api/auth/setup', {'mode':'password','password':alpha})
        cookie = first.cookie('shiori_session')
        assert cookie and cookie.has_nonstandard_attr('HttpOnly')
        assert first.request('/api/auth/status')['authenticated']
        record = json.loads(password_file.read_text('utf-8'))
        assert record['protection'] == 'windows-dpapi' and record['payload'].startswith('dpapi:')
        assert alpha not in password_file.read_text('utf-8') and 'credential' not in record
        first.request('/api/chat', {'text':'记住：密码重设验收保留这条记忆'})
        original_messages = first.request('/api/messages')
        original_memories = first.request('/api/memories')
        assert original_messages and original_memories
        second.request('/api/auth/login', {'password':'incorrect'}, expected=401)
        second.request('/api/auth/login', {'password':alpha})
        old_cookie = second.cookie('shiori_session').value
        first.request('/api/auth/password', {'current_password':alpha,'new_password':beta})
        second.request('/api/bootstrap', expected=401)
        anonymous.request('/api/bootstrap', expected=401, headers={'Cookie':'shiori_session=' + old_cookie})
        second.request('/api/auth/login', {'password':alpha}, expected=401)
        second.request('/api/auth/login', {'password':beta})
        second.request('/api/auth/logout', {})
        second.request('/api/bootstrap', expected=401)
        backup = zipfile.ZipFile(io.BytesIO(first.request('/api/backup')))
        assert not any('password' in name or name.startswith('security/') for name in backup.namelist())
        assert all(alpha.encode() not in backup.read(name) and beta.encode() not in backup.read(name) for name in backup.namelist())

        stop()
        start()
        first.request('/api/bootstrap', expected=401)  # No replay of saved cookies.
        first.request('/api/auth/login', {'password':beta})
        before = password_file.read_bytes()
        password_file.unlink()
        first.request('/api/bootstrap', expected=401)
        assert anonymous.request('/api/auth/status')['setup_required']
        assert password_file.exists() and password_file.read_bytes() != before
        first.request('/api/auth/bootstrap', {'token':token})
        first.request('/api/auth/setup', {'mode':'password','password':gamma})
        assert first.request('/api/messages') == original_messages
        assert first.request('/api/memories') == original_memories
        second.request('/api/auth/login', {'password':beta}, expected=401)
        password_file.unlink()
        first.request('/api/bootstrap', expected=401)
        first.request('/api/auth/setup', {'mode':'local_only'})
        assert anonymous.request('/api/auth/status')['mode'] == 'local_only'
        assert anonymous.request('/api/bootstrap')['name'] == 'shiori'
        first.request('/api/auth/access', {'remote_enabled':True}, method='PUT', expected=400)
        first.request('/api/auth/password', {'current_password':'','new_password':delta})
        anonymous.request('/api/bootstrap', expected=401)
        access = first.request('/api/auth/access', {'remote_enabled':True}, method='PUT')
        assert access['restart_required'] and not access['actual_remote_enabled']
        first.request('/api/auth/access', {'remote_enabled':False}, method='PUT')
        assert first.request('/api/memories') == original_memories
        report = {'version':args.expect_version,'passed':True,'first_setup':True,'proof_is_not_login':True,
            'encrypted_verifier':True,'http_only_cookie':True,'wrong_password':True,'change_revokes_sessions':True,
            'logout':True,'restart_revokes_sessions':True,'delete_file_resets':True,'chat_and_memory_preserved':True,
            'backup_excludes_password':True,'explicit_local_only':True,'upgrade_local_only_to_password':True}
        (data / 'auth-smoke.json').write_text(json.dumps(report,indent=2), encoding='utf-8')
        print(json.dumps(report))
    finally:
        try:
            stop()
        except Exception:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            raise


if __name__ == '__main__':
    main()
