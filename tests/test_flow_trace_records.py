"""Real generated replies and durable receipts retain ingress correlation."""
import pytest

from test_group_unified_security import runtime, anyio_backend, send, drain, close, SCOPE


@pytest.mark.anyio
async def test_group_reply_and_outbox_link_to_the_original_collection(runtime):
    store, _, _, _, _, flow, _ = runtime
    try:
        await send(runtime, '101', '来聊聊今天的天气')
        await send(runtime, '404', '也想听听大家的想法')
        await drain(flow)
        events = store.list_events(limit=100)
        merged = next(item['details'] for item in events if item['kind'] == 'qq_debounce')
        timing = next(item['details'] for item in events if item['kind'] == 'qq_reply_timing')
        outbox = next(item['details'] for item in events if item['kind'] == 'qq_outbox')
        assistant = next(item for item in store.list_messages(SCOPE) if item['role'] == 'assistant')
        trace = merged['flow_trace_id']
        assert trace and len(trace) == 32
        assert assistant['metadata']['flow_trace_id'] == outbox['flow_trace_id'] == timing['flow_trace_id'] == trace
        assert set(merged['source_ids']) == set(assistant['metadata']['source_ids']) == set(outbox['source_ids'])
        assert all(part['status'] == 'confirmed' for part in outbox['parts'])
    finally:
        await close(runtime)
