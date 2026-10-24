import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mikubot.startup import instance_details, stop_instance


@pytest.fixture
def local_instance(tmp_path):
    token = 'disposable-local-management-token-' * 2
    calls = []
    state = {'identity': hashlib.sha256(token.encode()).hexdigest()[:16], 'redirect': '', 'post_redirect': ''}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            calls.append(('GET', self.path, self.headers.get('Authorization')))
            if state['redirect']:
                self.send_response(302); self.send_header('Location', state['redirect']); self.end_headers(); return
            raw = json.dumps({'identity': state['identity']}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            # Consume the request before closing an HTTP/1.0 response. Leaving
            # unread bytes can turn the intended 302 into a Windows TCP reset.
            assert self.rfile.read(int(self.headers.get('Content-Length', '0'))) == b'{}'
            calls.append(('POST', self.path, self.headers.get('Authorization')))
            if state['post_redirect']:
                self.send_response(302); self.send_header('Location', state['post_redirect']); self.end_headers(); return
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    (tmp_path / 'admin.token').write_text(token, 'utf-8')
    (tmp_path / 'server.json').write_text(json.dumps({'port': server.server_port}), 'utf-8')
    yield tmp_path, server.server_port, token, state, calls
    server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_stop_missing_instance_does_not_create_data(tmp_path):
    absent = tmp_path / 'absent'
    assert stop_instance(absent) is False
    assert not absent.exists()


def test_stop_only_sends_secret_after_matching_identity(local_instance):
    data, _, token, _, calls = local_instance
    assert stop_instance(data)
    assert calls == [('GET', '/healthz', None), ('POST', '/api/shutdown', 'Bearer ' + token)]


def test_stop_unrelated_instance_never_sends_secret(local_instance):
    data, _, _, state, calls = local_instance
    state['identity'] = 'some-other-program'
    assert stop_instance(data) is False
    assert calls == [('GET', '/healthz', None)]


def test_health_redirect_not_followed(local_instance):
    data, port, _, state, calls = local_instance
    state['redirect'] = f'http://127.0.0.1:{port}/other'
    assert instance_details(data) is None
    assert len(calls) == 1


def test_shutdown_redirect_never_forwards_authorization(local_instance):
    import urllib.error
    data, port, _, state, calls = local_instance
    state['post_redirect'] = f'http://127.0.0.1:{port}/other'
    with pytest.raises(urllib.error.HTTPError):
        stop_instance(data)
    assert [x[1] for x in calls] == ['/healthz', '/api/shutdown']


@pytest.mark.parametrize('port', [-1, 0, 65536, True, '18765', None])
def test_invalid_instance_port_is_ignored(tmp_path, port):
    (tmp_path / 'admin.token').write_text('a' * 40, 'utf-8')
    (tmp_path / 'server.json').write_text(json.dumps({'port': port}), 'utf-8')
    assert instance_details(tmp_path) is None
