"""Cross-component regressions found during the 0.6 runtime review."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from qunyu.db import Store
from shiori.chat import ChatService
from shiori.evaluation import replay
from shiori.onebot import OneBotBridge
from shiori.settings import DEFAULTS
from shiori.tool_actions import ToolActions
from unittest.mock import AsyncMock


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_isolated_replay_supports_original_official_adapter_configuration():
    config = deepcopy(DEFAULTS)
    config['onebot']['adapter'] = 'official'
    config['qq_official']['enabled'] = True
    cases = [{'id': 'official-config', 'text': '初音未来，聊聊音乐', 'expected': {'reply': True},
              'scripted_decision': {'action': 'reply', 'reason': '明确向机器人分享音乐', 'intent': '聊音乐'}}]
    report = await replay(cases, config=config)
    assert report['summary']['errors'] == 0
    assert report['records'][0]['reply'] is True
    assert config['onebot']['adapter'] == 'official'


@pytest.mark.anyio
async def test_importing_old_backup_history_does_not_displace_newer_current_receipts(tmp_path):
    config = deepcopy(DEFAULTS)
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store = Store(tmp_path)
    ledger = ToolActions(SimpleNamespace(store=store, settings=settings))
    def history(prefix, date):
        return {'version': 1, 'runs': [{'id': prefix + str(index), 'scope_id': 'web:default',
            'user_id': 'owner', 'kind': 'local', 'tool_name': 'calculator', 'status': 'succeeded',
            'created_at': date, 'updated_at': date, 'result': {'value': index}} for index in range(200)]}
    try:
        ledger.import_history(history('current-', '2026-10-16T00:00:00.000000Z'))
        ledger.import_history(history('old-backup-', '2026-09-16T00:00:00.000000Z'))
        records = ledger.list()
        assert len(records) == 200
        assert all(x['original_id'].startswith('current-') and x['read_only'] for x in records)
    finally:
        ledger.close()
        store.close()


@pytest.mark.anyio
async def test_offline_reminder_reschedule_survives_configured_but_broken_planner(tmp_path):
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://test.invalid/v1', model='broken')
    config['onebot'].update(enabled=True, allowed_groups=['202'], owner_ids=['101'])
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store = Store(tmp_path)
    store.upsert_scope({'id': 'qq:group:202', 'merge_window_ms': 0})
    task = store.create_task({'scope_id': 'qq:group:202', 'user_id': '101', 'title': '喝水',
                              'kind': 'reminder', 'due_at': '2099-01-01T00:00:00.000000Z'})
    provider = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError('Planner service down')))
    chat = ChatService(store, settings, provider)
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    try:
        await bridge._flow.handle('qq:group:202', '101', '初音未来，把喝水提醒改到明晚八点', [], {'is_owner': True})
        assert not provider.complete.called
        assert store.get_task(task['id'])['due_at'] != task['due_at']
        assert '已改期' in bridge.send.call_args.args[1]
    finally:
        await bridge._flow.close()
        await chat.close()
        store.close()
