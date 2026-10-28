from unittest.mock import AsyncMock

import pytest

from shiori.output import prepare_reply, reply_char_count
from test_output_v5 import bridge_setup


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_onebot_emits_the_saved_limited_parts_without_redistribution(bridge_setup):
    store, settings, chat, bridge = bridge_setup
    settings.config['output'].update(mode='model', max_parts=5, message_max_chars=24,
                                     platform_max_chars=20000, interval_ms=0)
    source = '\n\n'.join(chr(65+i)*size for i,size in enumerate([13,4,6,21,6,3,7,17,29,26]))
    result = prepare_reply(source, settings.get(), store.get_scope('qq:group:202'))
    chat.respond = AsyncMock(return_value=dict(result, message_id='fixture'))
    bridge.send = AsyncMock(return_value=True)
    try:
        await bridge._flow.handle('qq:group:202', '303', '初音未来你好', [], {}, direct=True)
        sent = [call.args[1] for call in bridge.send.call_args_list]
        assert sent == result['reply_parts']
        assert all(reply_char_count(part) <= 24 for part in sent)
        assert chat.respond.await_count == 1
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_bracket_only_reply_does_not_trigger_false_qq_failure_notice(bridge_setup):
    _, settings, chat, bridge = bridge_setup
    settings.config['output'].update(mode='model', remove_parentheses=True)
    chat.respond = AsyncMock(return_value=dict(prepare_reply('（微笑）', settings.get()), message_id='fixture'))
    bridge.send = AsyncMock(return_value=True)
    try:
        await bridge._flow.handle('qq:group:202', '303', '初音未来你好', [], {}, direct=True)
        assert chat.respond.await_count == 1
        assert bridge.send.await_count == 0
    finally:
        await bridge.stop()
