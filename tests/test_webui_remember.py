"""'Keep me signed in' WebUI sessions: survive restarts, renew while used,
end on sign-out or a password change. Isolated data directories only."""
import pytest
from fastapi.testclient import TestClient

from auth_helpers import FIXTURE_PASSWORD, password_login
from shiori.api import create_app
from shiori.web_auth import COOKIE_NAME, RENEW_AFTER_SECONDS, SESSION_SECONDS, WebAuth

BOOT = 'b' * 40
PASSWORD = 'fixture-password-remember'
DAY = 86400


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def make(tmp_path, clock):
    return WebAuth(tmp_path, BOOT, clock=clock)


@pytest.fixture
def auth(tmp_path):
    clock = Clock()
    first = make(tmp_path, clock)
    first.setup(PASSWORD, is_local=True, bootstrap_token=BOOT)
    return tmp_path, clock, first


def test_kept_session_survives_a_restart_and_an_ordinary_one_does_not(auth):
    path, clock, first = auth
    kept = first.login(PASSWORD, remember_seconds=30 * DAY)
    ordinary = first.login(PASSWORD)
    assert kept['remember'] and kept['max_age'] == 30 * DAY and not ordinary['remember']
    restarted = make(path, clock)
    assert restarted.authenticate(kept['token'])
    assert not restarted.authenticate(ordinary['token'])
    assert restarted.status(kept['token'])['session_remembered'] is True
    # Only a digest is stored, never the token itself.
    assert kept['token'].encode() not in restarted.sessions_path.read_bytes()


def test_sign_out_and_expiry_end_a_kept_session(auth):
    path, clock, first = auth
    gone = first.login(PASSWORD, remember_seconds=30 * DAY)
    first.logout(gone['token'])
    assert not make(path, clock).authenticate(gone['token'])
    short = first.login(PASSWORD, remember_seconds=2 * DAY)
    clock.now += 2 * DAY + 1
    assert not first.authenticate(short['token'])
    assert not make(path, clock).authenticate(short['token'])


def test_password_change_ends_kept_sessions_but_keeps_the_changing_device(auth):
    path, clock, first = auth
    other = first.login(PASSWORD, remember_seconds=30 * DAY)
    mine = first.login(PASSWORD, remember_seconds=30 * DAY)
    old_file = first.sessions_path.read_bytes()
    renewed = first.change_password(PASSWORD, 'another-fixture-password', token=mine['token'])
    assert renewed['remember'] and renewed['max_age'] == 30 * DAY
    # Restoring the old sessions file cannot revive sessions of the old password.
    first.sessions_path.write_bytes(old_file)
    restarted = make(path, clock)
    assert not restarted.authenticate(other['token']) and not restarted.authenticate(mine['token'])
    first.sessions_path.unlink()
    first.change_password('another-fixture-password', 'third-fixture-password', token=renewed['token'])


def test_sign_out_everywhere_keeps_only_the_caller(auth):
    path, clock, first = auth
    a = first.login(PASSWORD, remember_seconds=30 * DAY)
    b = first.login(PASSWORD, remember_seconds=30 * DAY)
    c = first.login(PASSWORD)
    assert first.logout_everywhere(a['token']) == 3  # b, c and the setup session
    assert first.authenticate(a['token']) and not first.authenticate(b['token']) and not first.authenticate(c['token'])
    restarted = make(path, clock)
    assert restarted.authenticate(a['token']) and not restarted.authenticate(b['token'])


def test_a_kept_session_in_use_is_renewed_at_most_daily(auth):
    path, clock, first = auth
    kept = first.login(PASSWORD, remember_seconds=7 * DAY)
    ordinary = first.login(PASSWORD)
    clock.now += RENEW_AFTER_SECONDS - 60
    assert first.renew(kept['token']) is None and first.renew(ordinary['token']) is None
    clock.now += 120
    renewed = first.renew(kept['token'])
    assert renewed['expires_at'] == clock.now + 7 * DAY and renewed['max_age'] == 7 * DAY
    clock.now += 6 * DAY  # past the original expiry, inside the renewed one
    assert make(path, clock).authenticate(kept['token'])


def test_unreadable_sessions_file_only_means_signing_in_again(auth):
    path, clock, first = auth
    kept = first.login(PASSWORD, remember_seconds=30 * DAY)
    first.sessions_path.write_bytes(b'{not json')
    restarted = make(path, clock)
    assert not restarted.authenticate(kept['token'])
    assert restarted.authenticate(restarted.login(PASSWORD, remember_seconds=DAY)['token'])


def max_age(response):
    cookie = response.headers['set-cookie']
    return int(next(part.split('=')[1] for part in cookie.split(';') if part.strip().lower().startswith('max-age')))


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'remember-api', start_workers=False)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41010)) as local:
        password_login(local, app)
        yield local, app


def test_login_cookie_lifetime_follows_the_choice_and_setting(client):
    local, app = client
    assert local.get('/api/auth/status').json()['remember_days'] == 30
    plain = local.post('/api/auth/login', json={'password': FIXTURE_PASSWORD})
    assert max_age(plain) == SESSION_SECONDS and plain.json()['remembered'] is False
    kept = local.post('/api/auth/login', json={'password': FIXTURE_PASSWORD, 'remember': True})
    assert max_age(kept) == 30 * DAY and kept.json()['remembered'] is True
    assert local.get('/api/auth/status').json()['session_remembered'] is True
    assert local.put('/api/auth/access', json={'remember_days': 7}).status_code == 200
    kept = local.post('/api/auth/login', json={'password': FIXTURE_PASSWORD, 'remember': True})
    assert max_age(kept) == 7 * DAY
    for bad in (0, 366, '7'):
        assert local.put('/api/auth/access', json={'remember_days': bad}).status_code == 400
    assert local.post('/api/auth/login', json={'password': FIXTURE_PASSWORD, 'remember': 'yes'}).status_code == 400


def test_opening_the_page_renews_the_cookie_of_a_kept_session(client):
    local, app = client
    local.post('/api/auth/login', json={'password': FIXTURE_PASSWORD, 'remember': True})
    assert 'set-cookie' not in local.get('/api/auth/status').headers
    clock = Clock()
    clock.now = app.state.web_auth._clock() + RENEW_AFTER_SECONDS + 1
    app.state.web_auth._clock = clock
    response = local.get('/api/auth/status')
    assert response.json()['authenticated'] and max_age(response) == 30 * DAY
    assert COOKIE_NAME in response.headers['set-cookie']


def test_sign_out_other_devices_route(client, tmp_path):
    local, app = client
    local.post('/api/auth/login', json={'password': FIXTURE_PASSWORD, 'remember': True})
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41011)) as other:
        assert other.post('/api/auth/login', json={'password': FIXTURE_PASSWORD, 'remember': True}).status_code == 200
        assert other.get('/api/bootstrap').status_code == 200
        response = local.post('/api/auth/logout-others')
        assert response.status_code == 200 and response.json()['signed_out'] >= 1
        assert other.get('/api/bootstrap').status_code == 401
        assert local.get('/api/bootstrap').status_code == 200
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41012)) as stranger:
        assert stranger.post('/api/auth/logout-others').status_code == 401
