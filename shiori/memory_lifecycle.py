"""Persistent extraction cursors and bounded idle flushes for short conversations."""
from __future__ import annotations
import asyncio
from datetime import datetime,timedelta,timezone
import json
import hashlib
from .timezones import day_bounds
from .settings import resolve_model

def _now():
    return datetime.now(timezone.utc)

def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')

def _date(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

class MemoryLifecycle:
    def __init__(self,chat):
        self.chat=chat;self.store=chat.store;self.task=None;self.pending_jobs={}

    def _key(self,scope,user):
        return 'memory-progress-'+hashlib.sha256((scope+'\x00'+str(user)).encode()).hexdigest()[:32]

    def _details(self,scope,user):
        try:return self.store._get('events',self._key(scope,user))['details']
        except KeyError:return {}

    def _save(self,scope,user,state,**extra):
        from .db import utc_now
        with self.store._lock,self.store._conn:
            details=self._details(scope,user)
            details.update(zip(('cursor_at','cursor_id','last_attempt','last_error'),state))
            details.update(extra,scope_id=scope,user_id=str(user))
            record={'id':self._key(scope,user),'kind':'memory_cursor','created_at':utc_now(),
                    'message':'自动记忆整理进度','details':details}
            self.store._write('events',record,('kind','created_at'))

    def _state(self,scope,user):
        key=self._key(scope,user)
        try:
            record=self.store._get('events',key)['details']
            # Old empty cursors were seeded at now-24h despite never processing
            # any source. They can safely resume from the beginning.
            if not record.get('cursor_id') and record.get('cursor_at'):
                state=('', '', record.get('last_attempt', ''), record.get('last_error', ''))
                self._save(scope,user,state);return state
            return tuple(record.get(field,'') for field in ('cursor_at','cursor_id','last_attempt','last_error'))
        except KeyError:
            state=('','','','');self._save(scope,user,state);return state

    def _pending(self,scope,user,limit=32,*,recent=False):
        cursor_at,cursor_id,_,_=self._state(scope,str(user))
        blocked = self.store.blocked_memory_sources(scope) | set(self._details(scope,user).get('processed_source_ids',[]))
        with self.store._lock:
            anchor = self.store._conn.execute('SELECT rowid FROM messages WHERE scope_id=? AND user_id=? AND id=?', (scope,str(user),cursor_id)).fetchone()
            # Resolve the durable ID each time instead of persisting rowid.
            # A deleted cursor conservatively replays timestamp ties; UUID
            # lexical order must never skip an earlier inserted message.
            order = anchor[0] if anchor else 0
            recent_sql=' AND created_at>=?' if recent else ''
            params=[scope,str(user),cursor_at,cursor_at,order,json.dumps(sorted(blocked))]
            if recent:params.append(_stamp(_now()-timedelta(days=1)))
            params.append(max(1,min(400,int(limit))))
            rows=self.store._conn.execute("SELECT data FROM messages WHERE scope_id=? AND user_id=? AND role='user' AND (created_at>? OR (created_at=? AND rowid>?)) AND id NOT IN (SELECT value FROM json_each(?))"+recent_sql+" ORDER BY created_at,rowid LIMIT ?",params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _recent_turn(self):
        start,end=day_bounds(self.chat.settings,now=_now())
        with self.store._lock:
            count=self.store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='memory_extract_attempt' AND created_at>=? AND created_at<?",(start,end)).fetchone()[0]
        return count%4!=3

    def pending(self,scope,user,limit=32,*,automatic=False):
        if automatic and self._recent_turn():
            recent=self._pending(scope,user,limit,recent=True)
            if recent:return recent
        return self._pending(scope,user,limit)

    def attempt(self,scope,user,error=''):
        with self.store._lock:
            state=list(self._state(scope,user))
            failures=int(self._details(scope,user).get('failure_count',int(bool(state[3]))))
            state[2]=_stamp(_now())
            if error:
                state[3]=error[:100]
                failures+=1
            # Beginning a retry must not erase the previous failures. Only a
            # validated, successfully advanced batch clears its backoff.
            self._save(scope,user,state,failure_count=failures)

    def ready(self,scope,user,*,now=None):
        details=self._details(scope,user)
        failures=int(details.get('failure_count',int(bool(details.get('last_error')))))
        if not failures or not details.get('last_attempt'):return True
        delay=min(3600,300*(2**min(failures-1,4)))
        try:return ((now or _now())-_date(details['last_attempt'])).total_seconds()>=delay
        except (TypeError,ValueError):return True

    def advance(self,scope,user,records,*,automatic=False):
        if not records:return
        ordered=self.store.ordered_messages([item['id'] for item in records],scope_id=scope)
        if not ordered:return
        with self.store._lock:
            state=list(self._state(scope,user))
            processed=set(self._details(scope,user).get('processed_source_ids',[]))
            processed.update(item['id'] for item in ordered if item.get('role')=='user' and str(item.get('user_id'))==str(user))
            blocked=self.store.blocked_memory_sources(scope)
            anchor=self.store._conn.execute('SELECT rowid FROM messages WHERE scope_id=? AND user_id=? AND id=?',(scope,str(user),state[1])).fetchone()
            order=anchor[0] if anchor else 0
            # Recent successes can sit ahead of old backlog. Move the durable
            # cursor only through a contiguous handled prefix, keeping exact
            # source IDs for any completed batches beyond the first gap.
            while True:
                rows=self.store._conn.execute("SELECT id,created_at,rowid FROM messages WHERE scope_id=? AND user_id=? AND role='user' AND (created_at>? OR (created_at=? AND rowid>?)) ORDER BY created_at,rowid LIMIT 400",(scope,str(user),state[0],state[0],order)).fetchall()
                stopped=False
                for row in rows:
                    if row['id'] not in processed and row['id'] not in blocked:
                        stopped=True;break
                    state[:2]=[row['created_at'],row['id']]
                    order=row['rowid']
                    processed.discard(row['id'])
                if stopped or len(rows)<400:break
            if processed:
                present=self.store._conn.execute("SELECT id FROM messages WHERE scope_id=? AND user_id=? AND (created_at>? OR (created_at=? AND rowid>?)) AND id IN (SELECT value FROM json_each(?))",(scope,str(user),state[0],state[0],order,json.dumps(sorted(processed)))).fetchall()
                processed={row[0] for row in present}
            state[3]=''
            self._save(scope,user,state,processed_source_ids=sorted(processed),failure_count=0)

    def allowed(self,require_model=True,*,automatic=False):
        config=self.chat.settings.get()
        limit=config['memory'].get('daily_extractions',60)
        if not limit:return False
        model=resolve_model(config,'memory')
        if require_model and (not model.get('model') or not model.get('base_url')):return False
        now=_now()
        start,end=day_bounds(config,now=now)
        with self.store._lock:
            used=self.store._conn.execute("SELECT COUNT(*) FROM usage WHERE created_at>=? AND created_at<? AND json_extract(data,'$.purpose')='memory_extract'",(start,end)).fetchone()[0]
            claimed=self.store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='memory_extract_attempt' AND created_at>=? AND created_at<?",(start,end)).fetchone()[0]
            if max(used,claimed)>=limit:return False
            if automatic:
                latest=self.store._conn.execute("SELECT created_at FROM events WHERE kind='memory_extract_attempt' ORDER BY created_at DESC LIMIT 1").fetchone()
                interval=(_date(end)-_date(start)).total_seconds()/limit
                if latest and (now-_date(latest[0])).total_seconds()<interval:return False
        return True

    def claim(self,scope,user,*,automatic=False):
        with self.store._lock,self.store._conn:
            if automatic and not self.ready(scope,user):return False
            # Serialize the read-and-claim even across Store connections. The
            # durable claim remains spent on failures and across restarts.
            if not self.store._conn.in_transaction:self.store._conn.execute('BEGIN IMMEDIATE')
            if not self.allowed(require_model=False,automatic=automatic):return False
            self.store.add_event('memory_extract_attempt','记忆整理配额已使用',scope_id=scope,user_id=str(user),automatic=automatic)
            return True

    def observe(self,scope,user):
        cfg=self.chat.settings.get()['memory']
        key=(scope,str(user))
        if key in self.pending_jobs or not self.allowed(automatic=True) or not self.ready(*key):return
        records=self.pending(scope,str(user),cfg.get('extraction_messages',6),automatic=True)
        if len(records)>=cfg.get('extraction_messages',6):
            job=asyncio.create_task(self.chat._extract(scope,str(user),automatic=True))
            self.pending_jobs[key]=job
            self.chat._jobs.add(job);job.add_done_callback(self.chat._jobs.discard)
            job.add_done_callback(lambda done:self._finished(key,done))

    def _report_error(self,exc,scope=None):
        try:self.store.add_exception('memory_error','自动整理未完成；进度保留，稍后重试',exc,scope_id=scope)
        except Exception:pass

    def _finished(self,key,job):
        if self.pending_jobs.get(key) is job:self.pending_jobs.pop(key,None)
        if job.cancelled():return
        error=job.exception()
        if error is not None:
            try:self.attempt(*key,type(error).__name__)
            except Exception:pass
            self._report_error(error,key[0])

    async def _poll_once(self):
        if not self.allowed(automatic=True) or self.chat.settings.get()['paused']:return
        now=_now()
        idle=self.chat.settings.get()['memory'].get('extraction_idle_seconds',180)
        with self.store._lock:
            pairs=self.store._conn.execute("SELECT scope_id,user_id,MAX(created_at) AS latest FROM messages WHERE role='user' GROUP BY scope_id,user_id").fetchall()
            cursors=self.store._conn.execute("SELECT json_extract(data,'$.details.scope_id'),json_extract(data,'$.details.user_id'),json_extract(data,'$.details.last_attempt') FROM events WHERE kind='memory_cursor'").fetchall()
        attempts={(row[0],str(row[1])):row[2] or '' for row in cursors}
        candidates=[]
        for scope,user,latest in pairs:
            try:candidates.append((scope,str(user),_date(latest)))
            except (TypeError,ValueError,OverflowError) as exc:
                if self.ready(scope,str(user),now=now):
                    self.attempt(scope,str(user),type(exc).__name__)
                    self._report_error(exc,scope)
        # Oldest attempted pair wins within each lane; newest active pair wins
        # ties. This remains fair after restart and never jumps an old cursor.
        candidates.sort(key=lambda row:(row[0],row[1]))
        candidates.sort(key=lambda row:row[2],reverse=True)
        candidates.sort(key=lambda row:attempts.get((row[0],row[1]),''))
        recent_turn=self._recent_turn()
        for recent in ((True,False) if recent_turn else (False,)):
            for scope,user,stamp in candidates:
                if not self.allowed(automatic=True):return
                key=(scope,str(user))
                try:
                    if (now-stamp).total_seconds()<idle or (recent and stamp<now-timedelta(days=1)):continue
                    if key in self.pending_jobs or not self.ready(*key,now=now):continue
                    if not self._pending(*key,1,recent=recent):continue
                    await self.chat._extract(*key,automatic=True)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    try:self.attempt(*key,type(exc).__name__)
                    except Exception:pass
                    self._report_error(exc,scope)

    async def start(self):
        if self.task and not self.task.done():return
        async def poll():
            while True:
                await asyncio.sleep(15)
                try:await self._poll_once()
                except asyncio.CancelledError:raise
                except Exception as exc:self._report_error(exc)
        self.task=asyncio.create_task(poll())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
