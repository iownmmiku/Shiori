"""Revocation during an awaited command applies to the very next operation."""
import asyncio

import pytest

from test_group_unified_security import SCOPE, runtime, anyio_backend, send, drain, close


@pytest.mark.anyio
async def test_group_rechecks_admin_between_adjacent_same_author_commands(runtime):
    store, settings, model, _, _, flow, _ = runtime
    victim = store.create_memory({'scope_id': SCOPE, 'user_id': '404',
        'content': '其他成员仍然拥有的群内记忆', 'status': 'active', 'visibility': 'group'})
    complete = model.complete
    revoked_during_summary = False

    async def revoke_after_dispatch(messages, **kwargs):
        nonlocal revoked_during_summary
        if kwargs.get('purpose') == 'summary':
            assert kwargs['before_send']()
            # A local UI revocation can arrive while a summary model is awaited.
            settings.data['access']['administrators'] = []
            revoked_during_summary = True
            await asyncio.sleep(0)
        return await complete(messages, **kwargs)

    model.complete = revoke_after_dispatch
    try:
        await send(runtime, '101', '总结最近聊天')
        await send(runtime, '101', '删除记忆：' + victim['id'])
        await send(runtime, '404', '/帮助')
        await drain(flow)
        assert revoked_during_summary
        assert store.get_memory(victim['id'])['status'] == 'active'
    finally:
        await close(runtime)
