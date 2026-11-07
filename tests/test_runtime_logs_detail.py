"""Readable runtime log rows: event names, chats, summaries, tracebacks, model calls."""
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.runtime_logs import severity, summary


@pytest.fixture
def logs(tmp_path):
    data = tmp_path / 'log-detail'
    app = create_app(data, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        yield client, app, data / 'runtime.log'


def lines(client, **params):
    response = client.get('/api/logs', params=params)
    assert response.status_code == 200, response.text
    return response.json()['lines']


def test_event_rows_name_the_event_the_chat_and_what_mattered(logs):
    client, app, _ = logs
    store = app.state.store
    store.upsert_scope({'id': 'qq:group:202', 'name': '周末搭子群'})
    store.add_event('qq_sent', 'QQ 消息已确认发送', scope_id='qq:group:202', output_part={'index': 2, 'total': 3},
                    send_timing={'ack_wait_ms': 224.2, 'rate_wait_ms': 1500})
    store.add_event('planner_timing', 'Planner 判断分阶段耗时', scope_id='qq:group:202', outcome='skip', elapsed_ms=3159.6,
                    gate={'value': 40, 'threshold': 70.0, 'eagerness': 50, 'scored': True, 'audience': 'member', 'result': 'below_threshold'})
    store.add_event('qq_rate_limited', '群限速拒收本段', scope_id='qq:group:202', limit=5, wait_seconds=15.0, retry=False)
    rows = {row['kind']: row for row in lines(client)}
    sent = rows['qq_sent']
    assert sent['label'] == '已发送' and sent['category'] == 'qq' and sent['scope'] == '周末搭子群'
    assert '第 2/3 段' in sent['summary'] and '回执 224ms' in sent['summary'] and '限速等待 1.5s' in sent['summary']
    plan = rows['planner_timing']
    assert plan['category'] == 'planner' and '价值 40 / 门槛 70.0' in plan['summary']
    assert '对成员说' in plan['summary'] and '低于门槛' in plan['summary'] and '旁听' in plan['summary']
    assert rows['qq_rate_limited']['level'] == 'WARNING' and '每分钟最多 5 条' in rows['qq_rate_limited']['summary']
    assert sent['text'] == 'QQ 消息已确认发送' and '[qq_sent]' in sent['message']  # search/export keep the full line
    assert '"output_part"' in sent['detail']


def test_tracebacks_stay_with_their_error_line(logs):
    client, _, path = logs
    path.write_text('2099-01-01 10:00:00,000 ERROR 自动整理未完成 [abc]\n'
                    'Traceback (most recent call last):\n'
                    '  File "shiori/chat.py", line 1760, in _extract_candidates\n'
                    'ValueError: 事件结构包含不支持的字段\n'
                    '2099-01-01 10:00:01,000 INFO 下一条\n', encoding='utf-8')
    runtime = [row for row in lines(client) if row['source'] == 'runtime']
    assert [row['text'] for row in runtime] == ['自动整理未完成 [abc]', '下一条']
    assert runtime[0]['level'] == 'ERROR' and 'ValueError: 事件结构包含不支持的字段' in runtime[0]['detail']
    exported = client.get('/api/logs/export').text
    assert '    Traceback (most recent call last):' in exported


def test_model_calls_and_category_filter(logs):
    client, app, _ = logs
    app.state.store.record_usage({'scope_id': 'qq:group:202', 'model': 'gemini-3.8-flash', 'purpose': 'planner',
                                  'input_tokens': 3200, 'output_tokens': 115, 'cost': 0.0, 'latency_ms': 2260, 'ttft_ms': 734})
    app.state.store.add_event('memory_error', '自动整理未完成', scope_id='qq:group:202', error='ValueError')
    model = lines(client, category='model')
    assert model and all(row['category'] == 'model' for row in model)
    call = next(row for row in model if row['kind'] == 'model_call')
    assert 'planner' in call['text'] and 'gemini-3.8-flash' in call['text']
    assert '输入 3200 · 输出 115 token' in call['summary'] and '首字 734ms' in call['summary'] and '用时 2.3s' in call['summary']
    memory = lines(client, category='memory')
    assert [row['kind'] for row in memory] == ['memory_error'] and memory[0]['level'] == 'ERROR'
    assert client.get('/api/logs', params={'category': 'nope'}).status_code == 400


def test_severity_and_generic_summary():
    assert severity('stickers_error', {}) == 'ERROR'
    assert severity('qq_delivery', {'delivery_status': 'not_sent'}) == 'WARNING'
    assert severity('stickers', {}) == 'INFO'
    text = summary('stickers_skipped', {'scope_id': 'x', 'reason': 'byte_limit', 'byte_count': 2391732})
    assert 'byte_limit' in text and 'x' not in text.split(' · ')
