"""Model-only candidate normalization and transaction-backed outcome receipts."""
from copy import deepcopy
import asyncio
import json
from threading import Event
from types import SimpleNamespace

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def runtime(tmp_path):
    cfg = deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    cfg['memory'].update(organization_enabled=False, daily_extractions=60)
    cfg['stickers']['enabled'] = False
    settings = SimpleNamespace(get=lambda: deepcopy(cfg))
    class Model:
        def __init__(self): self.answer = lambda records: []
        async def complete(self, messages, **kwargs):
            assert kwargs['purpose'] == 'memory_extract' and kwargs['before_send']()
            return {'content': json.dumps(self.answer(json.loads(messages[-1]['content']))), 'usage': {}}
    model = Model()
    store = Store(tmp_path / 'candidate-contract')
    chat = ChatService(store, settings, model)
    records = [store.add_message('web:default', 'owner', 'user', text)
               for text in ('我喜欢蓝莓', '早餐常吃蓝莓', '每周去跑步', '请记住我的习惯')]
    try:
        yield SimpleNamespace(cfg=cfg, store=store, chat=chat, model=model, records=records)
    finally:
        await chat.close()
        store.close()


def candidate(records):
    return {'kind': 'preference', 'content': '用户喜欢蓝莓', 'property': 'preferred_fruit',
            'source_ids': [records[0]['id']], 'confidence': .9}


def report(env):
    events = [x for x in env.store.list_events(50) if x['kind'] == 'memory_extract_result']
    assert len(events) == 1
    return events[0]['details']


@pytest.mark.anyio
@pytest.mark.parametrize('property_value', ['missing', None, '', '  preferred_fruit  '])
async def test_optional_property_contract_accepts_empty_and_null(runtime, property_value):
    e = runtime
    def answer(records):
        item = candidate(records)
        if property_value == 'missing': item.pop('property')
        else: item['property'] = property_value
        return [item]
    e.model.answer = answer
    result = await e.chat._extract('web:default', 'owner')
    assert not result['skipped']
    saved = e.store.list_memories('web:default')
    assert len(saved) == 1 and isinstance(saved[0].get('property', ''), str)
    assert e.chat.memory_lifecycle.pending('web:default', 'owner') == []
    details = report(e)
    assert details['inserted'] == 1 and details['deduplicated'] == 0
    assert details['cursor_advanced'] and details['write_status'] == 'committed'
    assert details['normalized_property_nulls'] == (property_value is None)


@pytest.mark.anyio
@pytest.mark.parametrize('property_value', [False, True, 3, .5, [], {}, 'x' * 161])
async def test_bad_property_still_rejects_the_entire_batch(runtime, property_value):
    e = runtime
    e.model.answer = lambda records: [candidate(records), {**candidate(records), 'property': property_value}]
    assert (await e.chat._extract('web:default', 'owner'))['skipped']
    assert e.store.list_memories('web:default') == []
    assert len(e.chat.memory_lifecycle.pending('web:default', 'owner')) == 4
    details = report(e)
    assert details['status'] == 'failed' and details['failure_field'] == 'property'
    assert details['inserted'] == 0 and not details['cursor_advanced']
    assert e.store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='memory_extract_attempt'").fetchone()[0] == 1


@pytest.mark.anyio
@pytest.mark.parametrize('extras', [{'importance': None}, {'pinned': None},
    {'importance': 1., 'pinned': True, 'shareable': True, 'status': 'active',
     'dependencies': {'not-authorized': 1}, 'id': 'invented', 'valid_to': 'not-a-date'}])
async def test_uncontracted_model_fields_cannot_break_or_elevate_candidates(runtime, extras):
    e = runtime
    e.cfg['memory']['review_mode'] = 'manual'
    e.model.answer = lambda records: [{**candidate(records), **extras}]
    assert not (await e.chat._extract('web:default', 'owner'))['skipped']
    saved = e.store.list_memories('web:default')[0]
    assert saved['importance'] == .5 and saved['pinned'] is False
    assert saved['shareable'] is False and saved['status'] == 'pending'
    assert saved['dependencies'] == {} and saved['id'] != 'invented'
    assert saved['valid_to'] is None
    assert report(e)['pending_candidates'] == 1


@pytest.mark.anyio
async def test_duplicate_model_items_report_one_insert_and_one_reuse(runtime):
    e = runtime
    e.model.answer = lambda records: [candidate(records), candidate(records)]
    result = await e.chat._extract('web:default', 'owner')
    assert not result['skipped'] and len(e.store.list_memories('web:default')) == 1
    details = report(e)
    assert details['model_candidates'] == details['eligible_candidates'] == 2
    assert details['inserted'] == 1 and details['deduplicated'] == 1
    assert details['auto_approved'] == 1 and details['pending_candidates'] == 0
    assert details['cursor_advanced']
    assert '新增 1 条' in result['message'] and '复用 1 条' in result['message']


@pytest.mark.anyio
async def test_existing_duplicate_is_not_reported_as_a_new_memory(runtime):
    e = runtime
    e.cfg['memory']['review_mode'] = 'manual'
    e.store.create_memory({**candidate(e.records), 'subject': 'owner', 'status': 'pending'})
    e.model.answer = lambda records: [candidate(records)]
    assert not (await e.chat._extract('web:default', 'owner'))['skipped']
    details = report(e)
    assert details['inserted'] == 0 and details['deduplicated'] == 1
    assert details['pending_candidates'] == 1 and details['cursor_advanced']


@pytest.mark.anyio
@pytest.mark.parametrize('confidence,status', [(None, 'empty'), (.74, 'low_confidence')])
async def test_empty_and_low_confidence_outcomes_advance_but_do_not_claim_new_memory(runtime, confidence, status):
    e = runtime
    e.model.answer = lambda records: [] if confidence is None else [{**candidate(records), 'confidence': confidence}]
    assert not (await e.chat._extract('web:default', 'owner'))['skipped']
    details = report(e)
    assert details['status'] == status and details['inserted'] == details['deduplicated'] == 0
    assert details['low_confidence'] == (confidence is not None)
    assert details['cursor_advanced'] and e.chat.memory_lifecycle.pending('web:default', 'owner') == []


@pytest.mark.anyio
async def test_receipt_is_not_published_if_write_is_rolled_back(runtime):
    e = runtime
    receipt = {}
    def current():
        return e.store._conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    with pytest.raises(PermissionError):
        e.chat.memory.ingest_candidates([candidate(e.records)], scope_id='web:default', user_id='owner',
            source_ids=[x['id'] for x in e.records], before_write=current, outcome=receipt)
    assert receipt == {} and e.store.list_memories('web:default') == []
    assert e.store._conn.execute('SELECT COUNT(*) FROM memory_versions').fetchone()[0] == 0
    assert len(e.chat.memory_lifecycle.pending('web:default', 'owner')) == 4


@pytest.mark.anyio
async def test_observability_contains_counts_and_failure_not_source_or_candidate_text(runtime):
    e = runtime
    e.model.answer = lambda records: [candidate(records)]
    assert not (await e.chat._extract('web:default', 'owner'))['skipped']
    rendered = json.dumps(report(e), ensure_ascii=False)
    assert '蓝莓' not in rendered and 'preferred_fruit' not in rendered
    assert all(row['content'] not in rendered for row in e.records)


@pytest.mark.anyio
async def test_diagnostic_failure_cannot_change_a_successful_extraction(runtime):
    e = runtime
    e.model.answer = lambda records: [candidate(records)]
    add = e.store.add_event
    def fail_report(kind, message, **details):
        if kind == 'memory_extract_result': raise OSError('fixture log unavailable')
        return add(kind, message, **details)
    e.store.add_event = fail_report
    assert not (await e.chat._extract('web:default', 'owner'))['skipped']
    assert len(e.store.list_memories('web:default')) == 1
    assert e.chat.memory_lifecycle.pending('web:default', 'owner') == []


@pytest.mark.anyio
async def test_low_confidence_does_not_hide_an_invalid_optional_field(runtime):
    e = runtime
    e.model.answer = lambda records: [{**candidate(records), 'confidence': .2, 'property': {'invalid': 'type'}}]
    assert (await e.chat._extract('web:default', 'owner'))['skipped']
    assert len(e.chat.memory_lifecycle.pending('web:default', 'owner')) == 4
    details = report(e)
    assert details['failure_field'] == 'property' and details['low_confidence'] == 0


@pytest.mark.anyio
async def test_cancelled_write_wait_does_not_report_unconfirmed_inserts_as_zero(runtime):
    e = runtime
    e.model.answer = lambda records: [candidate(records)]
    loop = asyncio.get_running_loop()
    entered, release, finished = asyncio.Event(), Event(), Event()
    original = e.chat.memory.ingest_candidates
    def queued(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        try: return original(*args, **kwargs)
        finally: finished.set()
    e.chat.memory.ingest_candidates = queued
    task = asyncio.create_task(e.chat._extract('web:default', 'owner'))
    try:
        await asyncio.wait_for(entered.wait(), 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        details = report(e)
        assert details['status'] == 'cancelled' and details['write_status'] == 'unconfirmed'
        assert details['inserted'] is details['deduplicated'] is None
        assert not details['cursor_advanced']
        # The normal write guard must still observe revocation while its
        # worker is queued; the diagnostic cannot bypass that check.
        e.cfg['paused'] = True
        release.set()
        assert await asyncio.to_thread(finished.wait, 4)
        assert e.store.list_memories('web:default') == []
        assert len(e.chat.memory_lifecycle.pending('web:default', 'owner')) == 4
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('fault', ['read_error', 'deleted_record', 'missing_status'])
async def test_pending_count_read_failure_cannot_fail_an_already_advanced_turn(runtime, fault):
    e = runtime
    e.cfg['memory']['review_mode'] = 'manual'
    e.model.answer = lambda records: [candidate(records)]
    advance = e.chat.memory_lifecycle.advance
    get_memory = e.store.get_memory
    reads = []
    def fail_read(key):
        reads.append(key)
        if fault == 'read_error': raise OSError('fixture diagnostic read unavailable')
        if fault == 'missing_status': return {}
        return get_memory(key)
    def advance_then_change(*args, **kwargs):
        advance(*args, **kwargs)
        if fault == 'deleted_record':
            # Model a row disappearing between committing the extraction
            # cursor and the optional statistics lookup, without tombstones
            # that deliberately change source eligibility.
            with e.store._conn:
                e.store._conn.execute('DELETE FROM memories')
        e.store.get_memory = fail_read
    e.chat.memory_lifecycle.advance = advance_then_change
    try:
        result = await e.chat._extract('web:default', 'owner')
    finally:
        e.store.get_memory = get_memory
    assert result['skipped'] is False
    assert result['outcome']['status'] == 'completed'
    assert result['outcome']['cursor_advanced'] is True
    assert result['outcome']['pending_candidates'] is None
    assert len(reads) == 1, 'An optional diagnostic must not retry or scan more records'
    details = report(e)
    assert details['status'] == 'completed' and details['pending_candidates'] is None
    assert details['cursor_advanced'] and details['write_status'] == 'committed'
    assert not any(x['kind'] == 'memory_error' for x in e.store.list_events(50))
    assert e.chat.memory_lifecycle.pending('web:default', 'owner') == []
    assert e.chat.memory_lifecycle._details('web:default', 'owner')['last_error'] == ''
