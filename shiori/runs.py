"""Durable, bounded background work with explicit, single-use approvals."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import secrets
import threading
import traceback


def now():
    return datetime.now(timezone.utc).isoformat()


TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


class RunContext:
    def __init__(self, manager, job_id):
        self.manager, self.id = manager, job_id
        self.cancelled = threading.Event()

    @property
    def job(self):
        return self.manager._jobs[self.id]

    def check(self):
        if self.cancelled.is_set() or self.job['status'] in ('cancelled', 'interrupted'):
            raise asyncio.CancelledError()
        guard = self.manager.guards.get(self.job['kind'])
        if guard and not guard(self.job):
            raise PermissionError('权限、会话或能力配置已变化，任务已停止')
        return True

    def progress(self, value=None, **fields):
        self.check()
        data = dict(value or {}, **fields)
        self.manager._event(self.id, 'progress', data)

    def checkpoint(self, **fields):
        self.check()
        self.job.setdefault('_state', {}).update(copy.deepcopy(fields))
        self.manager._save()

    async def approve(self, summary, details):
        self.check()
        approval = {'id': secrets.token_hex(12), 'summary': summary, 'details': details, 'created_at': now(),
                    'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
        future = asyncio.get_running_loop().create_future()
        self.manager._approvals[self.id] = future
        self.job.update(status='awaiting_approval', approval=approval)
        self.manager._event(self.id, 'approval', approval)
        try:
            while not future.done():
                self.check()
                if datetime.now(timezone.utc).isoformat() >= approval['expires_at']:
                    raise ValueError('审批已超过5分钟，请重新创建任务并核对当前状态')
                await asyncio.wait({future}, timeout=.5)
            if not future.result():
                raise asyncio.CancelledError()
            self.check()
            self.job.update(status='running', approval=None)
            self.manager._event(self.id, 'approved', {'approval_id': approval['id']})
        finally:
            self.manager._approvals.pop(self.id, None)


class RunManager:
    def __init__(self, data_dir, *, redact=None):
        self.path = Path(data_dir) / 'jobs.json'
        self.redact = redact or (lambda value: value)
        self._jobs, self._tasks, self._contexts, self._approvals = {}, {}, {}, {}
        self.handlers, self.guards = {}, {}
        self.on_complete = None
        self.on_event = None
        self._notifications=set()
        self.warning = ''
        self._lock = threading.RLock()
        if self.path.exists():
            try:
                if self.path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError('任务记录过大')
                raw = json.loads(self.path.read_text('utf-8'))
                if raw.get('version') != 1 or not isinstance(raw.get('jobs'), list):
                    raise ValueError('任务记录格式不正确')
                for job in raw['jobs'][-200:]:
                    if not isinstance(job, dict) or not isinstance(job.get('id'), str):
                        raise ValueError('任务记录格式不正确')
                    if job.get('status') not in TERMINAL:
                        job.update(status='interrupted', approval=None,
                                   error='程序已重新启动。历史操作不会自动重跑，请核对结果后新建任务。')
                    self._jobs[job['id']] = job
            except (ValueError, OSError, TypeError, AttributeError):
                self.warning = '旧任务记录无法读取，已保留原文件，电脑任务暂停。请先导出诊断并检查记录。'
                self._jobs = {}
            if not self.warning:
                self._save()

    def _save(self):
        if self.warning:
            raise ValueError(self.warning)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            raw = json.dumps({'version': 1, 'jobs': list(self._jobs.values())}, ensure_ascii=False)
            if len(raw.encode()) > 32 * 1024 * 1024:
                raise ValueError('任务记录已达到容量，请导出并清理旧任务后重试')
            temp = self.path.with_suffix('.tmp')
            temp.write_text(raw, 'utf-8')
            temp.replace(self.path)

    def _event(self, job_id, kind, data):
        job = self._jobs[job_id]
        event = {'id': job.get('event_cursor', 0) + 1, 'at': now(), 'type': kind, 'data': self.redact(copy.deepcopy(data))}
        job['event_cursor'] = event['id']
        job.setdefault('events', []).append(event)
        job['events'] = job['events'][-150:]
        job['updated_at'] = event['at']
        if kind == 'progress':
            for key in ('stage', 'message', 'current', 'total'):
                if key in event['data']:
                    job[key] = event['data'][key]
        self._save()
        if self.on_event:
            task=asyncio.create_task(self.on_event(self.public(job),copy.deepcopy(event)))
            self._notifications.add(task)
            def done(future):
                self._notifications.discard(future)
                if not future.cancelled(): future.exception()
            task.add_done_callback(done)

    def register(self, kind, handler, guard=None):
        self.handlers[kind] = handler
        if guard:
            self.guards[kind] = guard

    def public(self, job):
        result=self.redact(copy.deepcopy({k: v for k, v in job.items() if not k.startswith('_')}))
        cache = job.get('_state',{}).get('distillation_cache') or {}
        result['resumable'] = (job.get('kind')=='distillation' and job.get('status') in ('interrupted','failed','cancelled')
                               and not job.get('read_only') and bool(cache.get('results')))
        if result.get('kind')=='computer' and result.get('result') and not result.get('read_only'):
            for index,artifact in enumerate(result['result'].get('artifacts') or []):
                artifact['url']=f"/api/jobs/{job['id']}/files/{index}"
        return result

    def get(self, job_id):
        return self.public(self._jobs[job_id])

    def resume(self, job_id):
        old = self._jobs[job_id]
        if not self.public(old)['resumable']:
            raise ValueError('此任务不能续作；本机命令与恢复的只读历史不会自动重放')
        guard = self.guards.get(old['kind'])
        if guard and not guard(old): raise PermissionError('原任务的授权已失效')
        active = next((item for item in self._jobs.values() if item.get('resumed_from') == job_id
                       and item['status'] not in TERMINAL), None)
        if active is not None:
            return self.public(active)
        current = self.submit(old['kind'], copy.deepcopy(old['_payload']),identity=old['identity'],
            scope_id=old['scope_id'],user_id=old['user_id'],title=old['title'])
        job = self._jobs[current['id']]
        job['_state'] = copy.deepcopy(old['_state'])
        job['resumed_from'] = old['id']
        self._save()
        return self.public(job)

    def list(self, scope_id=None):
        return [self.public(x) for x in reversed(list(self._jobs.values())) if not scope_id or x.get('scope_id') == scope_id]

    def submit(self, kind, payload, *, identity, scope_id='web:default', user_id='owner', title='后台任务'):
        if self.warning:
            raise ValueError(self.warning)
        if kind not in self.handlers:
            raise ValueError('未启用这种任务')
        if sum(x['status'] not in TERMINAL for x in self._jobs.values()) >= 8:
            raise ValueError('最多同时保留8个进行中的任务，请完成或停止现有任务')
        while len(self._jobs) >= 200:
            old = next((key for key, value in self._jobs.items() if value['status'] in TERMINAL), None)
            if old is None:
                raise ValueError('任务容量已满')
            del self._jobs[old]
        job_id = secrets.token_hex(8)
        job = {'id': job_id, 'kind': kind, 'title': str(title)[:120], 'scope_id': scope_id, 'user_id': user_id,
               'identity': copy.deepcopy(identity), 'status': 'queued', 'stage': 'queued', 'message': '任务已排队',
               'created_at': now(), 'updated_at': now(), 'events': [], 'result': None, 'error': '',
               'approval': None, '_payload': copy.deepcopy(payload), '_state': {}}
        self._jobs[job_id] = job
        context = RunContext(self, job_id)
        try:
            context.check()
            self._save()
        except BaseException:
            self._jobs.pop(job_id, None)
            raise
        self._contexts[job_id] = context
        self._tasks[job_id] = asyncio.create_task(self._run(context))
        return self.public(job)

    async def _run(self, context):
        job = context.job
        job['_started'] = True
        try:
            context.check()
            job['status'] = 'running'
            self._event(context.id, 'started', {'message': '开始处理'})
            result = await self.handlers[job['kind']](context, copy.deepcopy(job['_payload']))
            context.check()
            job.update(status='completed', result=self.redact(result), approval=None, stage='completed', message='任务已完成')
            self._event(context.id, 'completed', {'result': self.redact(result)})
        except asyncio.CancelledError:
            job.update(status='interrupted' if job.get('_shutdown') else 'cancelled', approval=None,
                       message='任务已停止', error='已发出的操作不会自动重跑，请检查已有结果')
            self._event(context.id, 'stopped', {'message': job['error']})
        except Exception as exc:
            logging.getLogger(__name__).error('后台任务异常 [%s]\n%s', context.id, self.redact(traceback.format_exc()))
            job.update(status='failed', approval=None, error=str(self.redact(str(exc)))[:2000], message='任务未完成')
            self._event(context.id, 'failed', {'message': job['error']})
        finally:
            self._contexts.pop(context.id, None)
            if self.on_complete:
                try:
                    await self.on_complete(self.public(job))
                except Exception:
                    job['delivery_status'] = 'failed'
                    self._save()
            self._tasks.pop(context.id, None)

    def approve(self, job_id, approval_id):
        job = self._jobs[job_id]
        guard = self.guards.get(job['kind'])
        if guard and not guard(job):
            raise PermissionError('任务发起人的权限已失效')
        future = self._approvals.get(job_id)
        if (job['status'] != 'awaiting_approval' or not future or future.done()
                or (job.get('approval') or {}).get('expires_at', now()) <= now()
                or not secrets.compare_digest(str((job.get('approval') or {}).get('id', '')), str(approval_id))):
            raise ValueError('审批已失效或已经使用，未重复执行')
        future.set_result(True)
        return self.get(job_id)

    def reject(self, job_id, approval_id):
        job = self._jobs[job_id]
        if not secrets.compare_digest(str((job.get('approval') or {}).get('id', '')), str(approval_id)):
            raise ValueError('审批已失效')
        return self.cancel(job_id)

    def cancel(self, job_id):
        job = self._jobs[job_id]
        if job['status'] in TERMINAL:
            return self.get(job_id)
        context = self._contexts.get(job_id)
        if context:
            context.cancelled.set()
        task = self._tasks.get(job_id)
        if task and not task.cancelling():
            task.cancel()
        if not job.get('_started'):
            job.update(status='interrupted' if job.get('_shutdown') else 'cancelled', approval=None,
                       message='任务已停止', error='任务尚未开始，没有执行操作')
            self._event(job_id, 'stopped', {'message': job['error']})
            self._contexts.pop(job_id, None)
            self._tasks.pop(job_id, None)
        return self.get(job_id)

    async def close(self):
        tasks = list(self._tasks.items())
        for job_id, task in tasks:
            self._jobs[job_id]['_shutdown'] = True
            self.cancel(job_id)
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        if self._notifications:
            await asyncio.gather(*list(self._notifications),return_exceptions=True)

    def export_history(self):
        records=[]
        for value in self._jobs.values():
            item=dict(self.public(value),status='interrupted' if value['status'] not in TERMINAL else value['status'],approval=None)
            item.pop('identity',None)
            records.append(item)
        result={'version':1,'jobs':records}
        self.validate_history(result)
        return result

    @staticmethod
    def validate_history(value):
        if not isinstance(value,dict) or value.get('version')!=1 or not isinstance(value.get('jobs'),list) or len(value['jobs'])>200:
            raise ValueError('任务历史备份格式无效')
        if len(json.dumps(value,ensure_ascii=False).encode())>4*1024*1024:
            raise ValueError('任务历史超过4MB，请先单独导出结果并清理历史')
        for item in value['jobs']:
            if (not isinstance(item,dict) or not isinstance(item.get('id'),str) or len(item['id'])>100
                    or item.get('kind') not in ('computer','distillation','tool_summary') or item.get('status') not in TERMINAL
                    or not isinstance(item.get('scope_id'),str) or not item['scope_id'].startswith(('web:','qq:','qqofficial:'))
                    or item.get('result') is not None and not isinstance(item['result'],dict)):
                raise ValueError('任务历史记录无效')
        return value

    def import_history(self,value):
        self.validate_history(value)
        for original in value['jobs']:
            source=original.get('original_id') or original['id']
            import hashlib
            key='history-'+hashlib.sha256((source+str(original.get('created_at',''))).encode()).hexdigest()[:24]
            if key in self._jobs: continue
            item=copy.deepcopy({k:original.get(k) for k in ('kind','title','scope_id','user_id','status','stage','message','created_at','updated_at','error')})
            item.update(id=key,original_id=source,read_only=True,approval=None,identity={},events=[],result=copy.deepcopy(original.get('result')))
            if item.get('result'):
                item['result'].pop('artifact',None)
                item['result'].pop('artifacts',None)
                item['result'].pop('workspace',None)
            self._jobs[key]=self.redact(item)
        while len(self._jobs)>200:
            oldest=next((key for key,item in self._jobs.items() if item.get('status') in TERMINAL),None)
            if oldest is None: raise ValueError('任务历史容量不足')
            del self._jobs[oldest]
        self._save()
