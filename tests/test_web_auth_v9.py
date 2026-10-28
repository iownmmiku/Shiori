"""WebUI verifier/session checks use isolated data, never the user's password."""
import base64
import concurrent.futures
import json
import os
from pathlib import Path

import pytest

import shiori.web_auth as module
from shiori.web_auth import AuthError, COOKIE_NAME, PASSWORD_FILE, SESSION_SECONDS, WebAuth


BOOTSTRAP = 'isolated-bootstrap-' + 's' * 40
PASSWORD = 'sample-private-password-雾'


@pytest.fixture
def clock():
    class Clock:
        now = 1000.0
        def __call__(self):
            return self.now
    return Clock()


@pytest.fixture
def auth(tmp_path, clock):
    return WebAuth(tmp_path, lambda: BOOTSTRAP, clock=clock)


def setup(auth, password=PASSWORD):
    return auth.setup(password, is_local=True, bootstrap_token=BOOTSTRAP)


def test_first_launch_creates_pending_file_without_authentication(auth):
    assert auth.path.is_file()
    assert str(auth.path).endswith(str(Path(PASSWORD_FILE)))
    state = auth.status()
    assert state['mode'] == 'setup' and state['setup_required']
    assert not state['authenticated'] and not state['password_set']
    assert 'password_file' not in state
    assert auth.status(is_local=True)['password_file'] == str(auth.path)
    assert not auth.authenticate(BOOTSTRAP, is_local=True)
    assert COOKIE_NAME != 'qunyu_session'


@pytest.mark.parametrize('is_local,token', [(False, BOOTSTRAP), (True, ''), (True, 'wrong')])
def test_remote_or_unproven_client_cannot_claim_first_password(auth, is_local, token):
    with pytest.raises(AuthError) as failure:
        auth.setup(PASSWORD, is_local=is_local, bootstrap_token=token)
    assert failure.value.status_code == 403
    assert auth.status()['setup_required']


def test_password_is_hashed_and_windows_verifier_encrypted(auth):
    session = setup(auth)
    raw = auth.path.read_bytes()
    assert PASSWORD.encode() not in raw and BOOTSTRAP.encode() not in raw
    assert session['token'].encode() not in raw
    envelope = json.loads(raw)
    if os.name == 'nt':
        assert envelope['protection'] == 'windows-dpapi'
        assert envelope['payload'].startswith('dpapi:')
        assert b'"salt"' not in raw and b'"digest"' not in raw
        record = json.loads(module._unprotect(envelope['payload']))
    else:
        assert envelope['protection'] == 'scrypt-verifier'
        assert auth.path.stat().st_mode & 0o077 == 0
        record = envelope['record']
    credential = record['credential']
    assert credential['algorithm'] == 'scrypt'
    assert len(base64.b64decode(credential['salt'])) == 32
    assert len(base64.b64decode(credential['digest'])) == 32
    assert auth.authenticate(session['token'])
    assert not auth.authenticate(BOOTSTRAP, is_local=True)


def test_same_password_uses_different_random_salts(tmp_path):
    first, second = WebAuth(tmp_path / 'a', BOOTSTRAP), WebAuth(tmp_path / 'b', BOOTSTRAP)
    setup(first)
    setup(second)
    assert first._record['credential']['salt'] != second._record['credential']['salt']
    assert first._record['credential']['digest'] != second._record['credential']['digest']


@pytest.mark.parametrize('password', ['', '1234567', ' ' * 20, 's' * 257, '\ud800' * 8, None])
def test_password_format_validation_does_not_activate_access(auth, password):
    with pytest.raises(AuthError) as failure:
        setup(auth, password)
    assert failure.value.code == 'invalid_password'
    assert auth.status()['setup_required']


def test_login_logout_expiry_and_restart_revoke_sessions(auth, clock):
    first = setup(auth)
    second = auth.login(PASSWORD, peer='192.0.2.1')
    assert first['token'] != second['token']
    assert auth.status(second['token'])['authenticated']
    auth.logout(first['token'])
    assert not auth.authenticate(first['token'])
    assert auth.authenticate(second['token'])
    clock.now += SESSION_SECONDS + 1
    assert not auth.authenticate(second['token'])
    fresh = auth.login(PASSWORD)
    restarted = WebAuth(auth.data_dir, BOOTSTRAP, clock=clock)
    assert not restarted.authenticate(fresh['token'])
    assert restarted.authenticate(restarted.login(PASSWORD)['token'])


def test_failed_login_limited_and_then_recovers(auth, clock):
    setup(auth)
    for _ in range(5):
        with pytest.raises(AuthError) as failure:
            auth.login('incorrect', peer='192.0.2.10')
        assert failure.value.status_code == 401
    with pytest.raises(AuthError) as blocked:
        auth.login(PASSWORD, peer='192.0.2.10')
    assert blocked.value.status_code == 429
    assert auth.authenticate(auth.login(PASSWORD, peer='192.0.2.11')['token'])
    clock.now += 61
    assert auth.authenticate(auth.login(PASSWORD, peer='192.0.2.10')['token'])


def test_global_attempt_limit_cannot_be_bypassed_by_rotating_peer(auth):
    setup(auth)
    # Populate the limit directly to avoid 100 intentionally expensive hashes.
    auth._global_failures = [auth._clock()] * 100
    with pytest.raises(AuthError) as blocked:
        auth.login(PASSWORD, peer='unseen-client')
    assert blocked.value.code == 'rate_limited'


def test_delete_password_file_revokes_every_session_and_recreates_pending_file(auth):
    first = setup(auth)
    second = auth.login(PASSWORD)
    auth.path.unlink()
    assert not auth.authenticate(first['token'], is_local=True)
    assert not auth.authenticate(second['token'])
    assert auth.path.is_file() and auth.status()['setup_required']
    with pytest.raises(AuthError) as remote:
        auth.setup('replacement-password', is_local=False, bootstrap_token=BOOTSTRAP)
    assert remote.value.code == 'local_setup_required'
    replacement = setup(auth, 'replacement-password')
    assert auth.authenticate(replacement['token'])
    with pytest.raises(AuthError):
        auth.login(PASSWORD)


def test_overwritten_or_corrupt_file_fails_closed_without_reinitialization(auth):
    session = setup(auth)
    auth.path.write_bytes(b'{invalid-data')
    assert not auth.authenticate(session['token'], is_local=True)
    state = auth.status(is_local=True)
    assert state['mode'] == 'error' and not state['setup_required']
    assert not state['authenticated']
    with pytest.raises(AuthError) as failure:
        setup(auth)
    assert failure.value.status_code == 503
    assert auth.path.read_bytes() == b'{invalid-data'


def test_restoring_old_password_record_still_revokes_old_sessions(auth):
    original = setup(auth)
    old_bytes = auth.path.read_bytes()
    replacement = auth.change_password(PASSWORD, 'another-password', token=original['token'])
    assert not auth.authenticate(original['token'])
    auth.path.write_bytes(old_bytes)
    assert not auth.authenticate(replacement['token'])
    assert not auth.authenticate(original['token'])
    assert auth.authenticate(auth.login(PASSWORD)['token'])


def test_password_change_requires_session_and_old_password(auth):
    first = setup(auth)
    second = auth.login(PASSWORD)
    for token, old_password in [('not-a-session', PASSWORD), (first['token'], 'incorrect')]:
        with pytest.raises(AuthError):
            auth.change_password(old_password, 'new-private-password', token=token)
    updated = auth.change_password(PASSWORD, 'new-private-password', token=first['token'])
    assert not auth.authenticate(first['token']) and not auth.authenticate(second['token'])
    assert auth.authenticate(updated['token'])
    assert auth.authenticate(auth.login('new-private-password')['token'])
    with pytest.raises(AuthError):
        setup(auth)


def test_explicit_local_only_alternative_never_accepts_remote_clients(auth):
    with pytest.raises(AuthError):
        auth.enable_local_only(is_local=False, bootstrap_token=BOOTSTRAP)
    auth.enable_local_only(is_local=True, bootstrap_token=BOOTSTRAP)
    assert auth.status(is_local=True)['mode'] == 'local_only'
    assert auth.authenticate(is_local=True)
    assert not auth.authenticate(is_local=False)
    assert not auth.authenticate(BOOTSTRAP, is_local=False)
    with pytest.raises(AuthError):
        auth.login(PASSWORD)
    auth.path.unlink()
    assert not auth.authenticate(is_local=True)
    assert auth.status()['setup_required']
    session = setup(auth)
    with pytest.raises(AuthError):
        auth.enable_local_only(is_local=True, bootstrap_token=BOOTSTRAP)
    assert auth.authenticate(session['token'])


def test_setup_is_single_winner_under_concurrent_requests(auth):
    def attempt(index):
        try:
            return auth.setup(f'valid-password-{index}', is_local=True, bootstrap_token=BOOTSTRAP)
        except AuthError as error:
            return error.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    winners = [item for item in results if isinstance(item, dict)]
    assert len(winners) == 1 and 'already_configured' in results
    assert auth.authenticate(winners[0]['token'])


def test_failed_atomic_replace_preserves_old_password_and_session(auth, monkeypatch):
    session = setup(auth)
    raw = auth.path.read_bytes()
    def fail_replace(*args):
        raise OSError('isolated simulated write failure')
    monkeypatch.setattr(module.os, 'replace', fail_replace)
    with pytest.raises(OSError):
        auth.change_password(PASSWORD, 'replacement-password', token=session['token'])
    assert auth.path.read_bytes() == raw
    assert auth.authenticate(session['token'])
    assert not list(auth.path.parent.glob('.password-*'))


def test_parameter_tampering_and_unknown_encryption_fail_closed(auth):
    session = setup(auth)
    envelope = json.loads(auth.path.read_bytes())
    envelope['protection'] = 'plain-text'
    envelope['record'] = {'mode': 'local_only', 'generation': 'a' * 32}
    auth.path.write_text(json.dumps(envelope), encoding='utf-8')
    assert not auth.authenticate(session['token'], is_local=True)
    assert auth.status()['mode'] == 'error'


def test_oversized_or_non_utf8_file_fails_closed(auth):
    session = setup(auth)
    for raw in [b'x' * 17000, b'\xff\xfe\xfd']:
        auth.path.write_bytes(raw)
        assert not auth.authenticate(session['token'], is_local=True)
        assert auth.status()['mode'] == 'error'
        assert auth.path.read_bytes() == raw


def test_malformed_unicode_secrets_cannot_crash_authority(auth):
    with pytest.raises(AuthError):
        auth.setup(PASSWORD, is_local=True, bootstrap_token='错误凭据')
    with pytest.raises(AuthError):
        auth.setup(PASSWORD, is_local=True, bootstrap_token='\ud800')
    setup(auth)
    assert not auth.authenticate('\ud800')
    auth.logout('\ud800')
    with pytest.raises(AuthError) as failure:
        auth.login('\ud800')
    assert failure.value.status_code == 401


def test_credential_cost_parameters_cannot_be_changed_by_file_content(auth):
    setup(auth)
    record = dict(auth._record)
    record['credential'] = dict(record['credential'], parameters={'n': 2 ** 30, 'r': 8, 'p': 1, 'dklen': 32})
    auth._write(record)
    assert auth.status()['mode'] == 'error'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI account binding')
def test_inability_to_decrypt_is_not_treated_as_missing_password(auth, monkeypatch):
    setup(auth)
    def deny(*args):
        raise OSError('isolated different Windows account')
    monkeypatch.setattr(module, '_unprotect', deny)
    restarted = WebAuth(auth.data_dir, BOOTSTRAP)
    assert restarted.status()['mode'] == 'error'
    assert not restarted.authenticate(is_local=True)


def test_symlink_password_file_is_rejected_without_modifying_target(auth, tmp_path):
    setup(auth)
    auth.path.unlink()
    target = tmp_path / 'unrelated.json'
    target.write_text('unrelated', encoding='utf-8')
    try:
        auth.path.symlink_to(target)
    except OSError:
        pytest.skip('symbolic links not permitted for this test account')
    assert not auth.authenticate(is_local=True)
    assert auth.status()['mode'] == 'error'
    assert target.read_text('utf-8') == 'unrelated'
