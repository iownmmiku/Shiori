"""Planner compatibility and failure diagnostics without any external calls."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from mikubot.db import Store
from mikubot.planner_result import PlanValidationError, parse_plan
from mikubot.settings import DEFAULTS
from mikubot.social import SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def plan(**changes):
    result = {'action': 'reply', 'target_message_id': 'current-message',
              'target_user_id': '303', 'addressed_to_bot': True,
              'intent': '回应当前问题', 'reason': '正在向机器人提问', 'needed_info': []}
    result.update(changes)
    return result


def parse(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return parse_plan(text, message_id='current-message', user_id='303')


def test_only_omitted_retrieval_terms_receive_the_empty_compatibility_default():
    value = plan()
    del value['needed_info']
    assert parse(value)['needed_info'] == []
    assert 'needed_info' not in value


@pytest.mark.parametrize('value', [None, 'search', {}, [None], [1], [True]])
def test_explicit_invalid_retrieval_terms_are_rejected(value):
    with pytest.raises(PlanValidationError) as caught:
        parse(plan(needed_info=value))
    assert caught.value.code == 'invalid_type'
    assert caught.value.field == 'needed_info'


@pytest.mark.parametrize('field', [
    'action', 'target_message_id', 'target_user_id', 'addressed_to_bot', 'intent', 'reason',
])
def test_required_fields_cannot_be_inferred_from_a_direct_trigger(field):
    value = plan()
    del value[field]
    with pytest.raises(PlanValidationError) as caught:
        parse(value)
    assert caught.value.code == 'missing_field'
    assert caught.value.field == field


@pytest.mark.parametrize('changes,code,field', [
    ({'action': 'wait'}, 'invalid_action', 'action'),
    ({'action': 'Reply'}, 'invalid_action', 'action'),
    ({'action': ' reply '}, 'invalid_action', 'action'),
    ({'target_message_id': 'another-message'}, 'target_mismatch', 'target_message_id'),
    ({'target_user_id': '999'}, 'target_mismatch', 'target_user_id'),
    ({'addressed_to_bot': 'true'}, 'invalid_type', 'addressed_to_bot'),
    ({'intent': None}, 'invalid_type', 'intent'),
    ({'reason': None}, 'invalid_type', 'reason'),
    ({'intent': '   '}, 'empty_intent', 'intent'),
])
def test_invalid_or_redirecting_plan_is_rejected(changes, code, field):
    with pytest.raises(PlanValidationError) as caught:
        parse(plan(**changes))
    assert caught.value.code == code
    assert caught.value.field == field


def test_explicit_semantic_skip_can_have_a_blank_intent():
    assert parse(plan(action='skip', intent=''))['action'] == 'skip'


def test_numeric_user_id_remains_compatible_with_the_same_current_speaker():
    assert parse(plan(target_user_id=303))['target_user_id'] == 303


@pytest.mark.parametrize('target', [True, 303.0, ['303'], {'id': '303'}])
def test_non_identifier_values_cannot_match_the_current_speaker(target):
    with pytest.raises(PlanValidationError) as caught:
        parse(plan(target_user_id=target))
    assert caught.value.code == 'target_mismatch'
    assert caught.value.field == 'target_user_id'


def test_structural_depth_bound_ignores_brackets_and_escaped_quotes_in_text():
    value = plan(reason='[{' * 100 + '他说："请回答"，路径 C:\\notes\\file')
    assert parse(value)['reason'].startswith('[{')


@pytest.mark.parametrize('wrapper', [
    lambda text: '\ufeff' + text,
    lambda text: '```json\n' + text + '\n```',
    lambda text: '```JSON\n' + text + '\n```',
    lambda text: '```\n' + text + '\n```',
    lambda text: '\ufeff```JSON\n' + text + '\n```',
])
def test_bom_and_whole_json_fences_preserve_the_same_plan(wrapper):
    expected = plan()
    assert parse(wrapper(json.dumps(expected, ensure_ascii=False))) == expected


@pytest.mark.parametrize('text,code', [
    ('[]', 'invalid_shape'),
    ('null', 'invalid_shape'),
    ('{"action":"skip","action":"reply"}', 'duplicate_key'),
    ('{"not-a-public-field":"private-value","not-a-public-field":1}', 'duplicate_key'),
    ('{"action":NaN}', 'invalid_json'),
    ('{"action":"reply"', 'invalid_json'),
    ('Here is the plan: {"action":"reply"}', 'invalid_json'),
    ('{}\n{}', 'invalid_json'),
    ('```json\n{}\n```\nextra explanation', 'invalid_json'),
    ('[' * 100 + '0' + ']' * 100, 'excessive_depth'),
    ('x' * 100000, 'response_too_large'),
], ids=['array', 'null', 'duplicate-action', 'private-duplicate', 'nan', 'truncated',
        'prose-prefix', 'multiple-objects', 'fence-suffix', 'depth', 'oversized'])
def test_invalid_json_is_not_repaired_or_partially_extracted(text, code):
    with pytest.raises(PlanValidationError) as caught:
        parse(text)
    assert caught.value.code == code
    assert caught.value.field != 'not-a-public-field'
    assert 'private-value' not in str(caught.value)


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.value['onebot'].update(enabled=True, chat_policy='whitelist', allowed_groups=['202'])
        self.value['planner'].update(min_interval_ms=0, group_batch_ms=0)

    def get(self):
        return deepcopy(self.value)


class Planner:
    def __init__(self):
        self.transform = lambda value: value
        self.after_completion = None
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        assert kwargs['before_send']()
        payload = json.loads(messages[-1]['content'])
        choice = self.transform(plan(target_message_id=payload['current_message_id'],
                                     target_user_id=payload['current_user_id']))
        if self.after_completion:
            self.after_completion()
        return {'content': choice if isinstance(choice, str) else json.dumps(choice, ensure_ascii=False),
                'usage': {'provider_id': 'fixture', 'model': 'fixture', 'request_id': 'request-1',
                          'input_tokens': 100, 'output_tokens': 30}}


@pytest.fixture
def runtime(tmp_path):
    store, config, provider = Store(tmp_path), Config(), Planner()
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True,
                               'cooldown_seconds': 0, 'proactive_reply': True})
    social = SocialEngine(store, config, provider)
    try:
        yield store, config, provider, scope, social
    finally:
        store.close()


@pytest.mark.anyio
async def test_valid_direct_plan_with_omitted_retrieval_terms_replies(runtime):
    store, _, provider, scope, social = runtime
    provider.transform = lambda value: {key: item for key, item in value.items() if key != 'needed_info'}
    message = store.add_message(scope['id'], '303', 'user', '你好', at_bot=True)
    result = await social.plan(scope, message, direct=True)
    assert result['reply'] and result['semantic']
    assert result['needed_info'] == []
    assert len(provider.calls) == 1
    assert not [event for event in store.list_events() if event['kind'] == 'planner_error']


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['json', 'schema'])
@pytest.mark.parametrize('direct', [True, False])
async def test_failed_plan_has_safe_diagnostics_and_never_replies(runtime, failure, direct):
    store, _, provider, scope, social = runtime
    private = 'PRIVATE-PLANNER-CONTENT-4891'
    secret = 'https://secret.invalid/?api_key=PRIVATE-TOKEN-4891'
    if failure == 'json':
        provider.transform = lambda value: '{"' + private + '":"' + secret + '"'
        code, field = 'invalid_json', None
    else:
        provider.transform = lambda value: dict(value, addressed_to_bot=private,
                                                 intent=secret, reason=private)
        code, field = 'invalid_type', 'addressed_to_bot'
    message = store.add_message(scope['id'], '303', 'user', '今天重听了那张电子音乐专辑', at_bot=direct)
    result = await social.plan(scope, message, direct=direct)
    assert result['action'] == 'skip' and not result['reply']
    assert result['semantic'] is False
    assert result['error_code'] == code
    assert bool(result.get('error_feedback')) is direct
    assert len(provider.calls) == 1
    events = [event for event in store.list_events() if event['kind'] == 'planner_error']
    assert len(events) == 1
    assert events[0]['details']['code'] == code
    if field:
        assert events[0]['details']['field'] == field
    assert events[0]['details']['stage']
    assert social.status(scope['id'])['last_plan']['error_code'] == code
    diagnostic_text = json.dumps({'result': result, 'events': events}, ensure_ascii=False)
    assert private not in diagnostic_text
    assert secret not in diagnostic_text
    assert 'PRIVATE-TOKEN-4891' not in diagnostic_text


@pytest.mark.anyio
@pytest.mark.parametrize('revoke', ['permission', 'source'])
async def test_revoked_source_or_permission_suppresses_parse_failure_feedback(runtime, revoke):
    store, config, provider, scope, social = runtime
    provider.transform = lambda value: 'private malformed output that must never be diagnosed'
    if revoke == 'permission':
        provider.after_completion = lambda: config.value['onebot'].update(allowed_groups=[])
    else:
        provider.after_completion = lambda: store.clear_messages(scope['id'])
    message = store.add_message(scope['id'], '303', 'user', '你好', at_bot=True)
    result = await social.plan(scope, message, direct=True)
    assert not result['reply']
    assert not result.get('error_feedback')
    assert not result.get('error_code')
    assert not [event for event in store.list_events() if event['kind'] == 'planner_error']


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['omitted-needed-info', 'invalid-direct', 'invalid-passive'])
async def test_real_chat_and_onebot_preserve_planner_failure_boundaries(tmp_path, case):
    from mikubot.chat import ChatService
    from mikubot.onebot import OneBotBridge

    config = Config()
    config.value['memory']['organization_enabled'] = False
    config.value['exploration']['enabled'] = False
    config.value['stickers']['enabled'] = False
    config.value['output'].update(mode='model', interval_ms=0)
    calls, ingress = [], []
    reply = '收到，这段旋律的节奏很轻快。'

    class Model:
        async def complete(self, messages, **kwargs):
            assert kwargs['before_send']()
            purpose = kwargs.get('purpose', 'chat')
            calls.append(purpose)
            if purpose == 'planner':
                payload = json.loads(messages[-1]['content'])
                value = plan(target_message_id=payload['current_message_id'],
                             target_user_id=payload['current_user_id'])
                if case == 'omitted-needed-info':
                    del value['needed_info']
                else:
                    value['addressed_to_bot'] = 'true'
                    value['reason'] = 'PRIVATE-MODEL-REASON-4882'
                return {'content': json.dumps(value, ensure_ascii=False)}
            assert purpose == 'chat', f'unexpected extra model purpose: {purpose}'
            return {'content': reply, 'message': {'role': 'assistant', 'content': reply},
                    'tool_calls': [], 'usage': {}}

    store = Store(tmp_path)
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True, 'auto_memory': False,
                        'dictionary_learn': False, 'expression_learn': False,
                        'merge_window_ms': 0, 'cooldown_seconds': 0, 'proactive_reply': True})
    chat = ChatService(store, config, Model())
    bridge = OneBotBridge(store, config, chat)
    bridge.send = AsyncMock(return_value=True)
    handle = bridge._flow.handle

    async def capture_ingress(*args, **kwargs):
        result = await handle(*args, **kwargs)
        ingress.append(result)
        return result

    bridge._flow.handle = capture_ingress
    parts = [{'type': 'text', 'data': {'text': '今天重听了这段旋律'}}]
    if case != 'invalid-passive':
        parts.insert(0, {'type': 'at', 'data': {'qq': '999'}})
    try:
        await bridge.handle_event({'post_type': 'message', 'message_type': 'group',
                                  'self_id': 999, 'group_id': 202, 'user_id': 303,
                                  'message_id': 1, 'message': parts})
        assert len(ingress) == 1
        assistants = [item for item in store.list_messages('qq:group:202')
                      if item['role'] == 'assistant']
        errors = [event for event in store.list_events() if event['kind'] == 'planner_error']
        if case == 'omitted-needed-info':
            assert calls == ['planner', 'chat']
            assert ingress[0]['accepted'] is True
            bridge.send.assert_awaited_once()
            assert bridge.send.call_args.args[1] == reply
            assert not bridge.send.call_args.kwargs['metadata'].get('failure_feedback')
            assert len(assistants) == 1 and assistants[0]['content'] == reply
            assert not assistants[0]['metadata'].get('failure_feedback')
            assert not errors
        else:
            assert calls == ['planner']
            assert ingress[0] == {'accepted': False, 'code': 'planner_error'}
            assert bridge.status()['last_message_result']['code'] == 'planner_error'
            assert len(errors) == 1 and errors[0]['details']['code'] == 'invalid_type'
            if case == 'invalid-direct':
                bridge.send.assert_awaited_once()
                sent = bridge.send.call_args
                assert sent.kwargs['metadata']['failure_feedback'] is True
                assert '回复判断' in sent.args[1]
                assert len(assistants) == 1 and assistants[0]['metadata']['failure_feedback'] is True
            else:
                bridge.send.assert_not_awaited()
                assert assistants == []
            assert 'PRIVATE-MODEL-REASON-4882' not in json.dumps(store.list_events(), ensure_ascii=False)
    finally:
        await bridge.stop()
        await chat.close()
        store.close()
