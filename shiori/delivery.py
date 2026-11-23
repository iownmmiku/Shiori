"""Durable multipart receipts. Unknown deliveries are never replayed."""
from __future__ import annotations
import asyncio
import json
import hashlib
import threading
from collections import OrderedDict
from uuid import uuid4
from fastapi import Body
from .db import message_fingerprint, utc_now
from .settings import resolved_identity


class _NotSent:
    """Falsy send result: the platform refused the part, so it never arrived."""
    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return 'NOT_SENT'


NOT_SENT = _NotSent()


def _delivery_fingerprint(record):
    # Keep legacy memory/source hashes compatible, while binding receipt
    # snapshots to the top-level clock fields used by the current runtime.
    payload={'message':message_fingerprint(record),
             'sent_at':record.get('sent_at'),'received_at':record.get('received_at')}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


# Receipt fingerprints of stored rows, keyed by a digest of the row's exact
# stored JSON. A fingerprint depends on that text alone, so a hit is what a
# fresh decode would give, and an edited row has new text and a new key. Each
# reply of a long private chat cites nearly every message the one before it
# did (1,430 on the server, 45 MB); decoding and re-encoding all of them for
# every reply took seconds of CPU in a worker thread, and the event loop's own
# small reads waited for the interpreter meanwhile.
_STORED_FINGERPRINTS = OrderedDict()
_STORED_FINGERPRINT_LIMIT = 8192
_stored_fingerprint_lock = threading.Lock()


def _stored_fingerprint(data):
    """(id, scope_id, receipt fingerprint) of a message row's stored JSON."""
    digest = hashlib.sha256(data.encode('utf-8', 'surrogatepass')).digest()
    with _stored_fingerprint_lock:
        known = _STORED_FINGERPRINTS.get(digest)
        if known is not None:
            _STORED_FINGERPRINTS.move_to_end(digest)
            return known
    row = json.loads(data)
    known = (row['id'], row.get('scope_id'), _delivery_fingerprint(row))
    with _stored_fingerprint_lock:
        _STORED_FINGERPRINTS[digest] = known
        while len(_STORED_FINGERPRINTS) > _STORED_FINGERPRINT_LIMIT:
            _STORED_FINGERPRINTS.popitem(last=False)
    return known


# The snapshot only guards a resume of parts that never went out. A record with
# no part left `pending` (or still `sending`) is never resumed, so its stored copy
# drops the source list and fingerprints: about 250 KB per reply in a long private
# chat, 172 MB of the server's events table on 2026-11-17.
_SNAPSHOT_FIELDS = ('context_source_ids', 'message_fingerprints')
_DEPENDENCY_SNAPSHOT_FIELDS = ('context_source_ids', 'memory_versions', 'jargon_versions')


def _finished(record):
    parts = record.get('parts')
    return isinstance(parts, list) and all(isinstance(part, dict) and part.get('status') not in ('pending', 'sending')
                                           for part in parts)


def released_copy(record):
    """The stored form of a finished outbox record, or None when there is nothing to drop."""
    dependencies = record.get('dependencies') if isinstance(record.get('dependencies'), dict) else {}
    if not _finished(record) or not (any(key in record for key in _SNAPSHOT_FIELDS)
                                     or any(key in dependencies for key in _DEPENDENCY_SNAPSHOT_FIELDS)):
        return None
    fingerprints = record.get('message_fingerprints')
    copy = {key: value for key, value in record.items() if key not in _SNAPSHOT_FIELDS and key != 'snapshot_version'}
    copy['dependencies'] = {key: value for key, value in dependencies.items() if key not in _DEPENDENCY_SNAPSHOT_FIELDS}
    copy['snapshot_released'] = {'sources': len(fingerprints) if isinstance(fingerprints, dict) else None}
    return copy


def release_finished_snapshots(store, after='', limit=50):
    """Drop the snapshot from finished outbox records written before this change.

    Walks the outbox in time order from `after`, `limit` rows per call, each row
    rewritten in its own short lock hold. Returns (rewritten, last created_at,
    rows read); a call that reads no row is the end.
    """
    with store._lock:
        rows = store._conn.execute("SELECT id,created_at,data FROM events WHERE kind='qq_outbox' AND created_at>? "
                                   "ORDER BY created_at LIMIT ?", (after, limit)).fetchall()
    rewritten = 0
    for key, created_at, data in rows:
        event = json.loads(data)
        details = event.get('details')
        copy = released_copy(details) if isinstance(details, dict) else None
        if copy is None:
            continue
        with store._lock, store._conn:
            current = store._conn.execute('SELECT data FROM events WHERE id=?', (key,)).fetchone()
            if current is None or current[0] != data:
                continue    # saved again meanwhile; that save decides
            store._write('events', dict(event, details=copy), ('kind', 'created_at'))
        rewritten += 1
    return rewritten, (rows[-1][1] if rows else after), len(rows)


async def release_outbox_history(store, *, pause=0.2):
    """Run release_finished_snapshots over the whole outbox off the event loop, once per start."""
    after, total = '', 0
    while True:
        rewritten, after, read = await asyncio.to_thread(release_finished_snapshots, store, after)
        total += rewritten
        if not read:
            break
        if rewritten:
            await asyncio.sleep(pause)
    if total:
        store.add_event('qq_outbox_released', '已发完的分段发送记录不再保存来源快照', records=total)
    return total


class DeliveryLedger:
    def __init__(self,store):self.store=store

    def save(self,record):
        record['updated_at']=utc_now()
        # The caller keeps the full record in memory; only the stored copy is released.
        stored=released_copy(record) or record
        event={'id':record['id'],'kind':'qq_outbox','message':'QQ 分段发送记录','created_at':record['updated_at'],'details':stored}
        with self.store._lock,self.store._conn:self.store._write('events',event,('kind','created_at'))
        return record

    def create(self,scope,user,parts,metadata,batch):
        # Metadata contains only source IDs and current authority, never callables.
        record={'id':'outbox-'+uuid4().hex,'scope_id':scope,'user_id':str(user),
            'flow_trace_id':metadata.get('flow_trace_id'),
            'assistant_message_id':batch.get('logical_message_id'),'parts':[{'text':p,'status':'pending'} for p in parts],
            'source_ids':[e['message']['id'] for e in batch['entries']],
            'identity':batch['identity'],'dependencies':batch.get('dependencies') or {},
            'metadata':{k:metadata[k] for k in ('platform','platform_message_id','onebot_self_id','source_id') if k in metadata}}
        originals={entry['message']['id']:entry['message'] for entry in batch['entries']}
        reply=batch.get('reply_message')
        if isinstance(reply,dict) and reply.get('id')==record['assistant_message_id']:
            originals[reply['id']]=reply
        elif record['assistant_message_id']:
            try:reply=self.store.get_message(record['assistant_message_id'])
            except KeyError:reply=None
        references=[record['dependencies'],(reply or {}).get('metadata') or {}]
        record['context_source_ids']=list(dict.fromkeys(key for reference in references
            for name in ('source_ids','context_source_ids') for key in reference.get(name,[])))
        keys=self._snapshot_ids(record)
        record['snapshot_version']=1
        fingerprints={}
        if keys is not None:
            # The batch's own copies come first and win over the stored rows.
            for key,row in originals.items():
                if key in keys and row.get('scope_id')==scope:
                    fingerprints[key]=_delivery_fingerprint(row)
            seen=set(originals)
            for _,data in self.store.ordered_message_rows(keys,scope_id=scope):
                key,owner,fingerprint=_stored_fingerprint(data)
                if key in seen:continue
                seen.add(key)
                if key in keys and owner==scope:
                    fingerprints[key]=fingerprint
        record['message_fingerprints']=fingerprints
        return self.save(record)

    @staticmethod
    def _snapshot_ids(record):
        dependencies=record.get('dependencies') or {}
        references=[record.get('source_ids'),record.get('context_source_ids',[]),
                    dependencies.get('source_ids',[]),dependencies.get('context_source_ids',[])]
        if any(not isinstance(ids,list) or any(not isinstance(key,str) or not key for key in ids)
               for ids in references):return None
        if not record['source_ids']:return None
        assistant=record.get('assistant_message_id')
        if not isinstance(assistant,str) or not assistant:return None
        return set(key for ids in references for key in ids)|{assistant}

    @staticmethod
    def has_snapshot(record):
        return type(record.get('snapshot_version')) is int and record['snapshot_version']==1

    def get(self,key):return self.store._get('events',key)['details']

    def list(self,scope=None):
        with self.store._lock:
            rows=self.store._conn.execute("SELECT data FROM events WHERE kind='qq_outbox' ORDER BY created_at DESC LIMIT 100").fetchall()
        records=[json.loads(r[0])['details'] for r in rows]
        return [r for r in records if not scope or r['scope_id']==scope]

    def valid(self,record,flow):
        scope,user=record['scope_id'],record['user_id']
        if not flow.allowed(scope,user) or resolved_identity(flow.settings.get(),flow.store.get_scope(scope))!=record['identity']:return False
        chat=flow.chat;social=getattr(chat,'social',None) or flow._fallback
        if not social._allowed(scope):return False
        keys=self._snapshot_ids(record)
        fingerprints=record.get('message_fingerprints')
        if (not self.has_snapshot(record) or keys is None or not isinstance(fingerprints,dict)
                or set(fingerprints)!=keys):return False
        if set(record['source_ids']) & social._blocked_sources(scope):return False
        try:
            rows={row['id']:row for row in self.store.ordered_messages(keys,scope_id=scope)}
            if set(rows)!=keys or any(_delivery_fingerprint(rows[key])!=fingerprints[key] for key in keys):return False
            if any(rows[key]['role']!='user' for key in record['source_ids']):return False
            if any(not flow.allowed(scope, str(rows[key]['user_id'])) for key in record['source_ids']):return False
            if rows[record['assistant_message_id']]['role']!='assistant':return False
        except KeyError:return False
        dep=record['dependencies']
        if hasattr(chat,'_changed') and chat._changed(dep.get('memory_versions',{}),dep.get('jargon_versions',{}),scope,user):return False
        if hasattr(chat,'_sources_changed') and chat._sources_changed(scope,dep.get('context_source_ids',record['source_ids'])):return False
        if hasattr(chat,'_knowledge_changed') and chat._knowledge_changed(dep.get('persona_id','default'),dep.get('knowledge_refs',{})):return False
        return True


def install_delivery(app,store,onebot,official):
    ledger=DeliveryLedger(store)
    @app.get('/api/delivery')
    async def deliveries(scope_id:str|None=None):
        return ledger.list(scope_id)

    @app.post('/api/delivery/{id}/resume')
    async def resume(id:str,payload:dict=Body(default={})):
        if payload.get('confirm') is not True:raise ValueError('请明确确认补发从未发送的后续段落')
        record=ledger.get(id)
        if not id.startswith('outbox-') or not isinstance(record.get('parts'),list):raise ValueError('发送记录无效')
        flow=(official if record['scope_id'].startswith('qqofficial:') else onebot)._flow
        async with flow._output_locks[record['scope_id']]:
            record=ledger.get(id)
            if not any(part['status']=='pending' for part in record['parts']):return {'sent':0,'record':record}
            if not ledger.has_snapshot(record):raise ValueError('这条旧发送记录缺少原始来源校验，不能补发；请重新发起请求')
            if not ledger.valid(record,flow):raise ValueError('来源、人格或权限已变化，不能补发旧回复')
            sent=0
            for index,part in enumerate(record['parts'],1):
                if part['status']!='pending':continue
                if sent:
                    import asyncio
                    from .output import output_config, part_gap_seconds
                    await asyncio.sleep(part_gap_seconds(output_config(flow.settings.get(),flow.store.get_scope(record['scope_id'])),part['text']))
                if not ledger.valid(record,flow):break
                part['status']='sending';ledger.save(record)
                try:
                    ok=await flow.emit(record['scope_id'],part['text'],dict(record['metadata'],assistant_message_id=record.get('assistant_message_id'),output_part={'index':index,'total':len(record['parts'])}))
                except BaseException:
                    part['status']='unknown';ledger.save(record);raise
                part['status']='confirmed' if ok else 'pending' if ok is NOT_SENT else 'unknown';ledger.save(record)
                if not ok:break
                sent+=1
            return {'sent':sent,'record':record}
