"""Runtime diagnostics expose bounded, authenticated and redacted text only."""
import json

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app


@pytest.fixture
def logs(tmp_path):
    data = tmp_path / 'log-api'
    app = create_app(data, start_workers=False)
    with TestClient(app) as client:
        yield client, app, data / 'runtime.log'


def get(client, **params):
    response = client.get('/api/logs', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_log_list_and_export_require_a_password_session(logs):
    client, app, path = logs
    path.write_text('2099-01-01 10:00:00 INFO private runtime content\n', encoding='utf-8')
    app.state.store.add_event('qq', 'private event', reason='not_allowed')
    assert client.get('/api/logs').status_code == 401
    assert client.get('/api/logs/export').status_code == 401
    assert client.post('/api/auth/bootstrap', json={'token': app.state.settings.token}).status_code == 200
    assert client.get('/api/logs').status_code == 401
    password_login(client, app)
    assert get(client)['lines']


def test_no_runtime_file_still_returns_qq_rejection_events(logs):
    client, app, path = logs
    password_login(client, app)
    assert not path.exists()
    event = app.state.store.add_event('qq', '拒绝当前QQ会话', reason='private_not_allowed', scope_id='qq:private:123')
    value = get(client, q='当前qq会话')
    assert value['available'] and len(value['lines']) == 1
    row = value['lines'][0]
    assert row['id'] == 'e:' + event['id'] and row['source'] == 'event' and row['level'] == 'WARNING'
    assert 'private_not_allowed' in row['message'] and 'qq:private:123' in row['message']


def test_current_secrets_and_generic_credentials_are_hidden_in_list_and_export(logs):
    client, app, path = logs
    password_login(client, app)
    secrets = ['log-model-api-key-39', 'log-inline-api-key-39', 'log-source-api-key-39',
               'log-onebot-access-token-39', 'log-official-app-secret-39', 'log-mcp-token-39',
               app.state.settings.token, 'unconfigured-bearer-value-39', 'unconfigured-password-value-39']
    app.state.settings.update({
        'model': {'api_key': secrets[0]},
        'providers': [{'id': 'old-log-model', 'model': 'old-model', 'api_key': secrets[1], 'base_url': 'https://old.example/v1'}],
        'provider_sources': [{'id': 'source_logs', 'name': '日志测试源', 'base_url': 'https://source.example/v1', 'api_key': secrets[2]}],
        'onebot': {'access_token': secrets[3]}, 'qq_official': {'app_secret': secrets[4]},
        'mcp_servers': [{'id': 'log_mcp', 'name': '日志测试服务', 'url': 'https://mcp.example/mcp', 'token': secrets[5]}]})
    sensitive = ' '.join(secrets[:7]) + ' Bearer ' + secrets[7] + ' password=' + secrets[8]
    path.write_text('2099-01-01 10:00:00 ERROR ' + sensitive + '\n', encoding='utf-8')
    app.state.store.add_event('model', sensitive, reason='request_failed', api_key=secrets[2], token=secrets[3])
    response = client.get('/api/logs')
    export = client.get('/api/logs/export')
    assert response.status_code == export.status_code == 200
    assert export.headers['content-type'].startswith('text/plain') and 'attachment' in export.headers['content-disposition']
    for secret in secrets:
        assert secret not in response.text and secret not in export.text
    assert '[已隐藏]' in response.text and '[已隐藏]' in export.text
    assert {'runtime', 'event'} <= {item['source'] for item in response.json()['lines']}


def test_levels_case_insensitive_search_and_nonmatching_search(logs):
    client, app, path = logs
    password_login(client, app)
    path.write_text('2099-01-01 10:00:00 INFO alpha readable\n'
                    '2099-01-01 10:00:01 WARNING Beta blocked\n'
                    '2099-01-01 10:00:02 ERROR gamma failed\n', encoding='utf-8')
    app.state.store.add_event('qq', 'pipeline Denied reason', reason='denied')
    assert [item['message'] for item in get(client, level='error')['lines']] == ['gamma failed']
    warnings = get(client, level='WARN')['lines']
    assert len(warnings) == 2 and all(item['level'] == 'WARNING' for item in warnings)
    assert [item['message'] for item in get(client, q='bEtA')['lines']] == ['Beta blocked']
    assert get(client, q='unmatched-unique-value-9039')['lines'] == []
    exported = client.get('/api/logs/export', params={'level': 'ERROR', 'q': 'gamma'})
    assert exported.status_code == 200 and 'gamma failed' in exported.text and 'Beta blocked' not in exported.text


def test_cursor_tracks_file_growth_without_repeating_previous_lines(logs):
    client, app, path = logs
    password_login(client, app)
    path.write_text('2099-01-01 10:00:00 INFO cursor-one\n'
                    '2099-01-01 10:00:01 INFO cursor-two\n'
                    '2099-01-01 10:00:02 INFO cursor-three\n', encoding='utf-8')
    initial = get(client, limit=2)
    assert initial['reset'] and initial['truncated']
    assert [item['message'] for item in initial['lines']] == ['cursor-two', 'cursor-three']
    assert get(client, after=initial['cursor'])['lines'] == []
    with path.open('a', encoding='utf-8') as output:
        output.write('2099-01-01 10:00:03 INFO cursor-four\n')
    growth = get(client, after=initial['cursor'])
    assert not growth['reset'] and [item['message'] for item in growth['lines']] == ['cursor-four']
    assert growth['cursor'] != initial['cursor']
    assert get(client, after=growth['cursor'])['lines'] == []
    # A truncated/rotated file loses the old cursor and explicitly asks the UI
    # to reset, rather than silently keeping an unusable position forever.
    path.write_text('2099-01-01 10:00:04 INFO cursor-after-rotation\n', encoding='utf-8')
    rotated = get(client, after=growth['cursor'], q='cursor-')
    assert rotated['reset'] and [item['message'] for item in rotated['lines']] == ['cursor-after-rotation']


def test_event_cursor_advances_over_nonmatching_rows_without_repeated_polling(logs):
    client, app, path = logs
    password_login(client, app)
    app.state.store.add_event('qq', 'matching-event-one')
    first = get(client, q='matching-event')
    assert len(first['lines']) == 1
    app.state.store.add_event('qq', 'unrelated-new-event')
    no_match = get(client, q='matching-event', after=first['cursor'])
    assert no_match['lines'] == [] and no_match['cursor'] != first['cursor']
    assert get(client, q='matching-event', after=no_match['cursor'])['lines'] == []
    app.state.store.add_event('qq', 'matching-event-two')
    second = get(client, q='matching-event', after=no_match['cursor'])
    assert len(second['lines']) == 1 and 'matching-event-two' in second['lines'][0]['message']


@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': 2001}, {'q': 's' * 257},
                                    {'after': 'a' * 161}, {'level': 'SECRET'}])
def test_oversized_or_invalid_filters_return_readable_400(logs, params):
    client, app, path = logs
    password_login(client, app)
    response = client.get('/api/logs', params=params)
    assert response.status_code == 400 and response.json()['detail']


def test_tail_limit_and_partial_utf8_decode_never_return_500(logs):
    client, app, path = logs
    password_login(client, app)
    # The 512 KiB read boundary falls inside a multibyte sequence/long line.
    # A second deliberately malformed line verifies errors='replace'.
    path.write_bytes(('早期不应出现\n' + '汉' * 200000 + '\n').encode('utf-8')
                     + b'2099-01-01 10:00:00 INFO malformed-utf8:\xff\xfe\n'
                     + '2099-01-01 10:00:01 INFO 最终可读日志39\n'.encode('utf-8'))
    value = get(client)
    assert '早期不应出现' not in json.dumps(value, ensure_ascii=False)
    assert any('最终可读日志39' in item['message'] for item in value['lines'])
    assert any('malformed-utf8:' in item['message'] for item in value['lines'])
    assert client.get('/api/logs/export').status_code == 200


def test_user_supplied_paths_cannot_select_an_arbitrary_file(logs, tmp_path):
    client, app, path = logs
    password_login(client, app)
    outside = tmp_path / 'not-runtime.txt'
    outside.write_text('arbitrary-file-must-not-be-exposed-39', encoding='utf-8')
    path.write_text('2099-01-01 10:00:00 INFO fixed-runtime-only-39\n', encoding='utf-8')
    params = {'path': str(outside), 'file': str(outside), 'filename': '../not-runtime.txt'}
    for endpoint in ('/api/logs', '/api/logs/export'):
        response = client.get(endpoint, params=params)
        assert response.status_code == 200 and 'fixed-runtime-only-39' in response.text
        assert 'arbitrary-file-must-not-be-exposed-39' not in response.text
