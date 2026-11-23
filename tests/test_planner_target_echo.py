"""The Planner's target fields only echo the turn back, and its failures say which upstream answer it was."""
from copy import deepcopy
import json

import pytest

from shiori.db import Store
from shiori.planner_result import PlanValidationError, parse_plan
from shiori.provider import ProviderError
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def plan(**changes):
    return {'action': 'reply', 'target_message_id': 'latest', 'target_user_id': '303', 'addressed_to_bot': True,
            'intent': '回应当前问题', 'reason': '正在向机器人提问', 'needed_info': [], **changes}


def parse(value, **kwargs):
    return parse_plan(json.dumps(value, ensure_ascii=False), message_id='latest', user_id='303', **kwargs)


def test_an_exact_echo_is_returned_unchanged():
    result = parse(plan())
    assert result['target_message_id'] == 'latest' and 'target_corrected' not in result


def test_a_message_answered_in_the_same_turn_is_the_same_target():
    result = parse(plan(target_message_id='earlier'), source_ids=['earlier', 'latest'])
    assert result['action'] == 'reply' and result['target_corrected'] == 'merged'
    assert result['target_message_id'] == 'latest' and result['target_user_id'] == '303'


@pytest.mark.parametrize('changes', [{'target_message_id': 'latesх'}, {'target_message_id': None},
                                     {'target_user_id': '999'}, {'target_message_id': 'older', 'target_user_id': 7}])
def test_a_skip_with_a_miscopied_echo_stays_a_skip(changes):
    result = parse(plan(action='skip', intent='', **changes), source_ids=['latest'])
    assert result['action'] == 'skip' and result['target_corrected'] == 'skip'
    assert result['target_message_id'] == 'latest' and result['target_user_id'] == '303'


@pytest.mark.parametrize('changes,field', [({'target_message_id': 'another-message'}, 'target_message_id'),
                                           ({'target_user_id': '999'}, 'target_user_id'),
                                           ({'target_message_id': 'earlier', 'target_user_id': '999'}, 'target_user_id')])
def test_a_reply_must_still_name_this_turn(changes, field):
    with pytest.raises(PlanValidationError) as caught:
        parse(plan(**changes), source_ids=['earlier', 'latest'])
    assert (caught.value.code, caught.value.field) == ('target_mismatch', field)


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['identity'].update(respond_to_at=False, respond_to_name=False)
        self.value['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.value['onebot'].update(enabled=True, chat_policy='whitelist', allowed_groups=['202'])
        self.value['planner'].update(min_interval_ms=0, group_batch_ms=0)

    def get(self):
        return deepcopy(self.value)


class Planner:
    def __init__(self):
        self.answer = lambda payload: plan(target_message_id=payload['current_message_id'],
                                           target_user_id=payload['current_user_id'])
        self.failure = None

    async def complete(self, messages, **kwargs):
        assert kwargs['before_send']()
        if self.failure:
            raise self.failure
        payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps(self.answer(payload), ensure_ascii=False),
                'usage': {'provider_id': 'fixture', 'model': 'fixture', 'request_id': 'request-1',
                          'input_tokens': 100, 'output_tokens': 30}}


@pytest.fixture
def runtime(tmp_path):
    store, config, provider = Store(tmp_path), Config(), Planner()
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True,
                               'cooldown_seconds': 0, 'proactive_reply': True})
    try:
        yield store, provider, scope, SocialEngine(store, config, provider)
    finally:
        store.close()


def events(store, kind):
    return [event for event in store.list_events() if event['kind'] == kind]


@pytest.mark.anyio
async def test_a_passive_skip_with_a_miscopied_message_id_is_a_quiet_skip(runtime):
    store, provider, scope, social = runtime
    provider.answer = lambda payload: plan(action='skip', intent='', reason='成员之间在聊',
                                           target_message_id=payload['current_message_id'][:-1] + 'x',
                                           target_user_id=payload['current_user_id'])
    message = store.add_message(scope['id'], '303', 'user', '你们晚饭吃什么')
    result = await social.plan(scope, message)
    assert result['action'] == 'skip' and not result['reply'] and not result.get('error_code')
    assert result['semantic'] is not False
    assert not events(store, 'planner_error')
    timing = events(store, 'planner_timing')
    assert timing and timing[0]['details']['target_corrected'] == 'skip'


@pytest.mark.anyio
@pytest.mark.parametrize('status', [502, 503, 429])
async def test_a_failed_upstream_answer_names_its_status(runtime, status):
    store, provider, scope, social = runtime
    provider.failure = ProviderError(f'模型服务返回 {status}：服务请求失败。', retryable=True)
    provider.failure.status_code = status
    message = store.add_message(scope['id'], '303', 'user', '有人知道这个怎么弄吗')
    result = await social.plan(scope, message)
    assert not result['reply']
    details = events(store, 'planner_error')[0]['details']
    assert details['code'] == 'request_failed' and details['status'] == status
    assert 'circuit_open' not in details


@pytest.mark.anyio
async def test_a_paused_model_is_named_as_such(runtime):
    store, provider, scope, social = runtime
    provider.failure = ProviderError('模型暂时熔断，请稍后重试或在模型池手动测试连接。', retryable=True)
    provider.failure.circuit_open = True
    message = store.add_message(scope['id'], '303', 'user', '有人知道这个怎么弄吗')
    await social.plan(scope, message)
    details = events(store, 'planner_error')[0]['details']
    assert details['circuit_open'] is True and 'status' not in details
