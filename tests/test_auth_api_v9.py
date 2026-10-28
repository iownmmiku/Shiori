"""Real HTTP authentication boundaries; no public network or user data."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from auth_helpers import FIXTURE_PASSWORD, password_login
from shiori.api import create_app
from shiori.auth_api import BOOTSTRAP_COOKIE
from shiori.web_auth import COOKIE_NAME


@pytest.mark.parametrize(('host','expected'), [('127.0.0.1',True),('::1',True),('::ffff:127.0.0.1',True),('::ffff:192.168.1.10',False),('192.168.1.10',False)])
def test_peer_loopback_recognizes_ipv4_mapped_ipv6_without_trusting_headers(host, expected):
    from types import SimpleNamespace
    from shiori.auth_api import local_peer
    request = SimpleNamespace(client=SimpleNamespace(host=host), headers={'host':'127.0.0.1:8765','x-forwarded-for':'127.0.0.1'})
    assert local_peer(request) is expected


def test_explicit_cli_port_is_not_misreported_as_a_saved_network_change(tmp_path):
    app = create_app(tmp_path / 'explicit-port', start_workers=False)
    with TestClient(app, base_url='http://127.0.0.1:18769', client=('127.0.0.1',41003)) as client:
        assert client.get('/api/auth/status').json()['restart_required'] is False
        password_login(client, app)
        response = client.put('/api/auth/access', json={'port':8765})
        assert response.status_code == 200
        assert response.json()['restart_required'] is True


def test_port_fallback_exposed_in_bootstrap_and_status_without_rewriting_config(tmp_path):
    app = create_app(tmp_path / 'port-fallback', start_workers=False)
    app.state.listen_host, app.state.listen_port, app.state.requested_port = '127.0.0.1', 8766, 8765
    with TestClient(app, base_url='http://127.0.0.1:8766', client=('127.0.0.1',41003)) as client:
        status = client.get('/api/auth/status').json()
        assert status['requested_port'] == 8765 and status['actual_port'] == 8766
        assert status['port_changed'] is True and 'OneBot' in status['port_warning']
        password_login(client, app)
        binding = client.get('/api/bootstrap').json()['listener']
        assert binding['port_changed'] and binding['actual_port'] == 8766
        access = client.get('/api/auth/access').json()
        assert access['actual_port'] == 8766 and access['port'] == 8765
        assert access['restart_required'] is False
        assert app.state.settings.get()['webui']['port'] == 8765


def test_listener_port_uses_bound_socket_behind_https_proxy(clients):
    local, _, app = clients
    # This fixture toggles remote_enabled after app creation. Model the
    # startup snapshot before checking proxy-induced port confusion.
    app.state.auth_http.initial_access = dict(app.state.settings.get()['webui'])
    app.state.listen_host, app.state.listen_port, app.state.requested_port = '0.0.0.0', 8766, 8765
    password_login(local, app)
    app.state.settings.update({'webui': {'allowed_hosts':['bot.example.com'], 'public_url':'https://bot.example.com'}})
    with TestClient(app, base_url='http://bot.example.com', client=('127.0.0.1',41004)) as proxy:
        proxy.headers['x-forwarded-proto'] = 'https'
        remote_login(proxy)
        # The test transport URL is HTTP after TLS termination; explicitly
        # replay the Secure cookie a browser sends to the public HTTPS proxy.
        proxy.headers['Cookie'] = f'{COOKIE_NAME}={proxy.cookies.get(COOKIE_NAME)}'
        status = proxy.get('/api/auth/status').json()
        assert status['actual_port'] == 8766 and status['requested_port'] == 8765
        access = proxy.get('/api/auth/access').json()
        assert access['actual_port'] == 8766 and access['restart_required'] is False
        assert 'http://127.0.0.1:8766/' in access['urls']
        assert 'https://bot.example.com/' in access['urls']


def test_x_token_is_rejected_and_local_bearer_remains_local_only(clients):
    local, remote, app = clients
    password_login(local, app)
    local.cookies.clear()
    token = app.state.settings.token
    assert local.get('/api/bootstrap', headers={'X-Token':token}).status_code == 401
    assert local.get('/api/bootstrap', headers={'Authorization':'Bearer ' + token}).status_code == 200
    assert remote.get('/api/bootstrap', headers={'Authorization':'Bearer ' + token}).status_code == 401


def test_https_reverse_proxy_keeps_remote_identity_and_requires_declared_origin(clients):
    local, _, app = clients
    password_login(local, app)
    app.state.settings.update({'webui': {'allowed_hosts':['bot.example.com'], 'public_url':'https://bot.example.com'}})
    with TestClient(app, base_url='http://bot.example.com', client=('127.0.0.1',41004)) as proxy:
        assert proxy.get('/api/auth/status').status_code==403
        proxy.headers.update({'x-forwarded-proto':'https'})
        assert proxy.post('/api/auth/bootstrap',json={'token':app.state.settings.token},headers={'Origin':'https://bot.example.com'}).status_code == 403
        assert proxy.get('/api/auth/status').json()['local_setup_allowed'] is False
        assert 'password_file' not in proxy.get('/api/auth/status').json()
        assert proxy.post('/api/auth/login',json={'password':FIXTURE_PASSWORD},headers={'Origin':'https://evil.example'}).status_code == 403
        response = proxy.post('/api/auth/login',json={'password':FIXTURE_PASSWORD},headers={'Origin':'https://bot.example.com'})
        assert response.status_code == 200, response.text
        assert 'Secure' in response.headers['set-cookie']
        token = proxy.cookies.get(COOKIE_NAME)
        assert proxy.get('/api/bootstrap',headers={'Cookie':f'{COOKIE_NAME}={token}'}).status_code == 200


def test_public_url_and_bad_network_settings_are_validated_without_mutation(clients):
    local, _, app = clients
    password_login(local, app)
    previous = app.state.settings.get()['webui']
    for section in ('bad', [], {'public_url':'http://public.example'}, {'public_url':'https://other.example'}):
        assert local.put('/api/settings',json={'webui':section}).status_code == 400
        assert app.state.settings.get()['webui'] == previous


@pytest.fixture
def clients(tmp_path):
    app = create_app(tmp_path / 'auth-api', start_workers=False, allow_remote=True)
    # Simulate an explicitly configured LAN listener. The password gate must
    # still reject remote access while the first password is pending.
    app.state.settings.update({'webui': {'remote_enabled': True}})
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41001)) as local:
        remote = TestClient(app, base_url='https://192.168.1.2:8765', client=('192.168.1.10', 41002))
        try:
            yield local, remote, app
        finally:
            remote.close()


def remote_login(remote):
    response = remote.post('/api/auth/login', json={'password': FIXTURE_PASSWORD})
    assert response.status_code == 200, response.text
    return response


def test_first_bootstrap_proof_is_not_a_logged_in_session(clients):
    local, remote, app = clients
    assert local.get('/api/bootstrap').status_code == 401
    initial = local.get('/api/auth/status').json()
    assert initial['setup_required'] and not initial['authenticated']
    assert 'password_file' not in initial and 'data_dir' not in initial
    proof = local.post('/api/session', json={'token': app.state.settings.token})
    assert proof.status_code == 200
    assert BOOTSTRAP_COOKIE in local.cookies and COOKIE_NAME not in local.cookies
    assert 'qunyu_session' not in local.cookies
    assert local.get('/api/bootstrap').status_code == 401
    assert local.get('/api/auth/status').json()['local_setup_allowed']
    response = local.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD})
    assert response.status_code == 200, response.text
    assert COOKIE_NAME in local.cookies
    assert local.get('/api/auth/status').json()['authenticated']
    assert local.get('/api/bootstrap').status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert FIXTURE_PASSWORD not in response.text


def test_remote_cannot_claim_or_bypass_first_password(clients):
    local, remote, app = clients
    for path, payload in [
        ('/api/auth/setup', {'password': FIXTURE_PASSWORD}),
        ('/api/auth/setup', {'mode': 'local_only'}),
        ('/api/auth/bootstrap', {'token': app.state.settings.token}),
        ('/api/session', {'token': app.state.settings.token}),
    ]:
        response = remote.post(path, json=payload)
        assert response.status_code == (401 if path == '/api/session' else 403)
    remote.cookies.set(BOOTSTRAP_COOKIE, app.state.settings.token)
    remote.cookies.set('qunyu_session', app.state.settings.token)
    assert remote.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD}).status_code == 403
    assert remote.get('/api/bootstrap').status_code == 403
    assert app.state.web_auth.status()['setup_required']


def test_local_setup_requires_proof_not_only_loopback_peer(clients):
    local, remote, app = clients
    assert local.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD}).status_code == 403
    assert local.post('/api/auth/bootstrap', json={'token': 'wrong'}).status_code == 403
    response = local.post('/api/auth/bootstrap', json={'token': app.state.settings.token})
    assert response.status_code == 200
    mismatch = local.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD, 'confirm_password': 'different'})
    assert mismatch.status_code == 400
    assert app.state.web_auth.status()['setup_required']
    assert local.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD}).status_code == 200


def test_remote_password_login_logout_and_no_secret_paths(clients):
    local, remote, app = clients
    password_login(local, app)
    assert remote.get('/api/bootstrap').status_code == 401
    assert remote.post('/api/auth/login', json={'password': 'incorrect'}).status_code == 401
    remote_login(remote)
    assert remote.get('/api/bootstrap').status_code == 200
    status = remote.get('/api/auth/status').json()
    assert status['authenticated'] and 'password_file' not in status and 'data_dir' not in status
    token = remote.cookies.get(COOKIE_NAME)
    assert remote.post('/api/auth/logout').status_code == 200
    assert not remote.get('/api/auth/status').json()['authenticated']
    remote.cookies.set(COOKIE_NAME, token)
    assert remote.get('/api/bootstrap').status_code == 401


def test_change_password_revokes_other_sessions_and_issues_one_new_session(clients):
    local, remote, app = clients
    password_login(local, app)
    remote_login(remote)
    local_old = local.cookies.get(COOKIE_NAME)
    response = local.post('/api/auth/password', json={'current_password': FIXTURE_PASSWORD,
                                                       'new_password': 'new-fixture-password-v9'})
    assert response.status_code == 200, response.text
    assert local.cookies.get(COOKIE_NAME) != local_old
    assert local.get('/api/bootstrap').status_code == 200
    assert remote.get('/api/bootstrap').status_code == 401
    assert remote.post('/api/auth/login', json={'password': FIXTURE_PASSWORD}).status_code == 401
    assert remote.post('/api/auth/login', json={'password': 'new-fixture-password-v9'}).status_code == 200


def test_deleting_password_file_revokes_remote_and_local_sessions(clients):
    local, remote, app = clients
    password_login(local, app)
    remote_login(remote)
    app.state.web_auth.path.unlink()
    assert local.get('/api/bootstrap').status_code == 401
    assert remote.get('/api/bootstrap').status_code == 403
    assert remote.post('/api/auth/setup', json={'password': 'attacker-password'}).status_code == 403
    assert app.state.web_auth.status()['setup_required'] and app.state.web_auth.path.is_file()
    # The original local bootstrap proof remains scoped to auth setup only.
    response = local.post('/api/auth/setup', json={'password': 'reset-fixture-password-v9'})
    assert response.status_code == 200, response.text
    assert local.get('/api/bootstrap').status_code == 200


def test_corrupt_password_file_rejects_sessions_without_remote_reset(clients):
    local, remote, app = clients
    password_login(local, app)
    remote_login(remote)
    app.state.web_auth.path.write_bytes(b'{broken-password-file')
    assert local.get('/api/bootstrap').status_code == 401
    assert remote.get('/api/bootstrap').status_code == 403
    status = local.get('/api/auth/status').json()
    assert status['mode'] == 'error' and not status['setup_required']
    assert local.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD}).status_code == 503
    assert app.state.web_auth.path.read_bytes() == b'{broken-password-file'


def test_explicit_local_only_mode_closes_remote_access(clients):
    local, remote, app = clients
    assert local.post('/api/session', json={'token': app.state.settings.token}).status_code == 200
    response = local.post('/api/auth/setup', json={'mode': 'local_only'})
    assert response.status_code == 200, response.text
    assert local.get('/api/bootstrap').status_code == 200
    assert not app.state.settings.get()['webui']['remote_enabled']
    assert remote.get('/api/bootstrap').status_code == 403
    assert remote.post('/api/auth/login', json={'password': FIXTURE_PASSWORD}).status_code == 403
    response = local.put('/api/auth/access', json={'remote_enabled': True})
    assert response.status_code == 400
    assert not app.state.settings.get()['webui']['remote_enabled']


def test_legacy_bearer_remains_loopback_cli_only(clients):
    local, remote, app = clients
    authorization = {'authorization': 'Bearer ' + app.state.settings.token}
    assert local.get('/api/bootstrap', headers=authorization).status_code == 200
    assert not local.get('/api/auth/status', headers=authorization).json()['authenticated']
    assert remote.get('/api/bootstrap', headers=authorization).status_code == 403
    password_login(local, app)
    assert remote.get('/api/bootstrap', headers=authorization).status_code in (401, 403)
    local.cookies.clear()
    local.cookies.set('qunyu_session', app.state.settings.token)
    assert local.get('/api/bootstrap').status_code == 401


def test_forwarded_headers_cannot_forge_loopback_or_remote_host(clients):
    local, remote, app = clients
    forged = {'forwarded': 'for=127.0.0.1;host=localhost', 'x-forwarded-for': '127.0.0.1',
              'x-forwarded-host': 'localhost', 'authorization': 'Bearer ' + app.state.settings.token,
              'host': 'localhost:8765'}
    assert remote.get('/api/bootstrap', headers=forged).status_code == 403
    assert remote.post('/api/auth/bootstrap', headers=forged,
                       json={'token': app.state.settings.token}).status_code == 403
    assert app.state.web_auth.status()['setup_required']


@pytest.mark.parametrize('headers', [
    {'origin': 'https://evil.example'}, {'sec-fetch-site': 'cross-site'},
    {'host': 'evil.example'}, {'host': 'user@127.0.0.1:8765'},
])
def test_host_and_cross_site_checks_cover_auth_endpoints(clients, headers):
    local, remote, app = clients
    before = app.state.web_auth.path.read_bytes()
    response = local.post('/api/auth/bootstrap', headers=headers,
                          json={'token': app.state.settings.token})
    assert response.status_code == 403
    assert app.state.web_auth.path.read_bytes() == before


def test_remote_named_host_requires_explicit_allowlist(clients):
    local, remote, app = clients
    password_login(local, app)
    remote_login(remote)
    assert remote.get('/api/bootstrap', headers={'host': 'shiori.example:8765'}).status_code == 403
    response = local.put('/api/auth/access', json={'allowed_hosts': ['shiori.example']})
    assert response.status_code == 200, response.text
    assert remote.get('/api/bootstrap', headers={'host': 'shiori.example:8765'}).status_code == 200


def test_password_protection_is_not_exported_or_restored_with_backups(clients):
    local, remote, app = clients
    password_login(local, app)
    remote_login(remote)
    original = app.state.web_auth.path.read_bytes()
    backup = local.get('/api/backup')
    assert backup.status_code == 200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        assert not any('password' in name.lower() or 'admin.token' in name or 'security/' in name
                       for name in archive.namelist())
        assert all(FIXTURE_PASSWORD.encode() not in archive.read(name) for name in archive.namelist())
    response = local.post('/api/backup/restore', files={'file': ('backup.zip', backup.content, 'application/zip')})
    assert response.status_code == 200, response.text
    assert app.state.web_auth.path.read_bytes() == original
    assert local.get('/api/bootstrap').status_code == 200
    assert remote.get('/api/bootstrap').status_code == 403


def test_closing_remote_access_immediately_revokes_remote_api(clients):
    local, remote, app = clients
    password_login(local, app)
    remote_login(remote)
    assert remote.get('/api/bootstrap').status_code == 200
    response = local.put('/api/auth/access', json={'remote_enabled': False})
    assert response.status_code == 200, response.text
    assert not app.state.auth_http.allow_remote
    assert remote.get('/api/bootstrap').status_code == 403
    assert local.get('/api/bootstrap').status_code == 200


def test_local_listener_rejects_remote_even_with_valid_session(tmp_path):
    app = create_app(tmp_path / 'local-listener', start_workers=False)
    with TestClient(app, client=('127.0.0.1', 41001)) as local:
        password_login(local, app)
        remote = TestClient(app, base_url='http://192.168.1.2:8765', client=('192.168.1.10', 41002))
        try:
            remote.cookies.set(COOKIE_NAME, local.cookies.get(COOKIE_NAME))
            assert remote.get('/api/bootstrap').status_code == 403
        finally:
            remote.close()


def test_bad_unicode_bootstrap_and_nonstring_password_have_readable_errors(clients):
    local, remote, app = clients
    for token in ['中文不是凭据', '\ud800', None, {}]:
        response = local.post('/api/auth/bootstrap', content=json.dumps({'token': token}),
                              headers={'content-type': 'application/json'})
        assert response.status_code == 403
    assert local.post('/api/session', json={'token': app.state.settings.token}).status_code == 200
    assert local.post('/api/auth/setup', json={'password': []}).status_code == 400


def test_auth_body_limit_covers_declared_and_chunked_bodies(clients):
    local, remote, app = clients
    oversized = json.dumps({'password': 's' * 9000}).encode()
    assert local.post('/api/auth/login', content=oversized,
                      headers={'content-type': 'application/json'}).status_code == 413
    def chunks():
        yield oversized[:4000]
        yield oversized[4000:8000]
        yield oversized[8000:]
    response = local.post('/api/auth/login', content=chunks(), headers={'content-type': 'application/json'})
    assert response.status_code == 413


def test_remote_password_attempts_are_rate_limited_by_socket_peer(clients):
    local, remote, app = clients
    password_login(local, app)
    for _ in range(5):
        assert remote.post('/api/auth/login', json={'password': 'incorrect'},
                           headers={'x-forwarded-for': 'different-client'}).status_code == 401
    assert remote.post('/api/auth/login', json={'password': FIXTURE_PASSWORD}).status_code == 429


def test_local_only_can_upgrade_to_password_without_a_previous_password(clients):
    local, remote, app = clients
    assert local.post('/api/session', json={'token': app.state.settings.token}).status_code == 200
    assert local.post('/api/auth/setup', json={'mode': 'local_only'}).status_code == 200
    response = local.post('/api/auth/password', json={'current_password': '', 'new_password': FIXTURE_PASSWORD})
    assert response.status_code == 200, response.text
    assert local.get('/api/auth/status').json()['mode'] == 'password'
    assert local.get('/api/bootstrap').status_code == 200
    assert remote.get('/api/bootstrap').status_code == 403
    assert remote.post('/api/auth/login', json={'password': FIXTURE_PASSWORD}).status_code == 403


def test_settings_route_cannot_open_remote_without_password_and_closes_immediately(clients):
    local, remote, app = clients
    authorization = {'authorization': 'Bearer ' + app.state.settings.token}
    app.state.settings.update({'webui': {'remote_enabled': False}})
    response = local.put('/api/settings', json={'webui': {'remote_enabled': True}}, headers=authorization)
    assert response.status_code == 400, response.text
    assert not app.state.settings.get()['webui']['remote_enabled']
    password_login(local, app)
    assert local.put('/api/settings', json={'webui': {'remote_enabled': True}}).status_code == 200
    remote_login(remote)
    assert remote.get('/api/bootstrap').status_code == 200
    assert local.put('/api/settings', json={'webui': {'remote_enabled': False}}).status_code == 200
    assert remote.get('/api/bootstrap').status_code == 403


def test_failed_switch_to_local_only_keeps_existing_access_settings(clients):
    local, remote, app = clients
    password_login(local, app)
    before = app.state.settings.get()['webui']
    response = local.post('/api/auth/setup', json={'mode': 'local_only'})
    assert response.status_code == 409
    assert app.state.settings.get()['webui'] == before
    assert app.state.web_auth.status()['mode'] == 'password'


def test_https_session_cookie_has_secure_flag(clients):
    local, remote, app = clients
    password_login(local, app)
    secure = TestClient(app, base_url='https://127.0.0.1:8765', client=('127.0.0.1', 41003))
    try:
        response = secure.post('/api/auth/login', json={'password': FIXTURE_PASSWORD})
        assert response.status_code == 200, response.text
        assert 'Secure' in response.headers['set-cookie']
        assert secure.get('/api/bootstrap').status_code == 200
    finally:
        secure.close()
