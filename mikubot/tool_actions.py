"""Bounded local tool ledger and administrator-approved MCP proposals."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jsonschema import Draft202012Validator
from .db import message_fingerprint, utc_now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def bounded_schema(schema):
    if not isinstance(schema, dict) or schema.get('type') != 'object' or len(encoded(schema)) > 12000:
        raise ValueError('工具参数定义必须是有界的object schema')
    count = 0
    def walk(value, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > 1000:
            raise ValueError('工具参数定义过于复杂')
        if isinstance(value, dict):
            if any(key in value for key in ('$ref', '$dynamicRef', 'pattern', 'patternProperties')):
                raise ValueError('本版不支持带引用或正则的工具参数定义')
            for child in value.values(): walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value: walk(child, depth + 1)
    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError('工具参数定义无效') from exc
    return schema


def validate_arguments(schema, arguments):
    bounded_schema(schema)
    if not isinstance(arguments, dict) or len(encoded(arguments)) > 12000:
        raise ValueError('工具参数须为不超过12000字的JSON对象')
    def walk(value, depth=0):
        if depth > 12:
            raise ValueError('工具参数嵌套过深')
        if isinstance(value, (dict, list)):
            if len(value) > 200:
                raise ValueError('工具参数集合过大')
            for child in value.values() if isinstance(value, dict) else value: walk(child, depth+1)
    walk(arguments)
    if next(Draft202012Validator(schema).iter_errors(arguments), None) is not None:
        raise ValueError('工具参数不符合定义，请检查类型、必填项和额外字段')


def bounded_result(value, limit=5000):
    raw = encoded(value)
    if len(raw) <= limit: return value
    result = {'truncated':True,'preview':raw[:max(0,limit-100)]}
    while len(encoded(result)) > limit:
        result['preview'] = result['preview'][:len(result['preview'])//2]
    return result


class ToolActions:
    def __init__(self, chat, client=None):
        from .mcp_client import MCPClient
        self.chat, self.store, self.settings = chat, chat.store, chat.settings
        self.client = client or MCPClient()
        self.path = self.store.data_dir / 'tool_runs.json'
        self._lock = threading.RLock()
        self._catalog, self._status, self._runs = {}, {}, []
        self._closed = False
        self.recovery_warning = ''
        self.mcp_suspended = False
        if self.path.exists():
            try:
                if self.path.stat().st_size > 5*1024*1024: raise ValueError()
                records = json.loads(self.path.read_text('utf-8'))
                if not isinstance(records, list): raise ValueError()
                self._runs = [x for x in records[-200:] if isinstance(x, dict) and isinstance(x.get('id'), str)]
                for item in self._runs:
                    if item.get('status') in ('pending', 'running'):
                        item.update(status='unknown' if item['status'] == 'running' else 'cancelled', error='上次运行已结束，操作不会自动重放', updated_at=utc_now())
                self._save()
            except Exception:
                self._runs=[]
                self.mcp_suspended=True
                backup=self.path.with_name('tool_runs.corrupt-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
                try:
                    self.path.replace(backup)
                    self._save()
                    self.recovery_warning='旧工具记录损坏，已保留原文件并暂停MCP；原操作不会重放。检查后可确认恢复。'
                except OSError:
                    self.recovery_warning='工具记录无法读取或备份，已暂停MCP，请检查data目录权限。'
                self.store.add_event('tool_recovery',self.recovery_warning)

    def _config(self, server_id):
        item = next((x for x in self.settings.get().get('mcp_servers', []) if x['id'] == server_id), None)
        if not item: raise ValueError('MCP 服务已删除')
        return item

    @staticmethod
    def _identity(config):
        return hashlib.sha256(encoded({k: config.get(k) for k in ('id', 'url', 'token')}).encode()).hexdigest()

    def _redact(self, value):
        cfg = self.settings.get()
        secrets = [getattr(self.settings, 'token', ''), cfg['model'].get('api_key'), cfg['onebot'].get('access_token'), cfg['qq_official'].get('app_secret')]
        secrets += [x.get('api_key') for x in cfg.get('providers', [])] + [x.get('token') for x in cfg.get('mcp_servers', [])]
        secrets += [x.get('api_key') for x in cfg.get('provider_sources', [])]
        secrets += [x.get('api_key') for x in cfg.get('search_providers', [])]
        def clean(item):
            if isinstance(item, str):
                for secret in secrets:
                    if secret and (len(secret) >= 6 or item == secret): item = item.replace(secret, '[已隐藏]')
                return item
            if isinstance(item, dict): return {clean(k): clean(v) for k, v in item.items()}
            if isinstance(item, list): return [clean(x) for x in item]
            return item
        return clean(value)

    def _save(self):
        while len(encoded(self._runs[-200:])) > 4*1024*1024:
            removable = next((x for x in self._runs if x.get('status') not in ('pending','running')), None)
            if removable is None: raise ValueError('工具记录空间已满，请先处理待审批操作')
            self._runs.remove(removable)
        data = [self._public_run(x) for x in self._runs[-200:]]
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(encoded(data), 'utf-8')
        tmp.replace(self.path)

    def _get(self, run_id):
        record = next((x for x in self._runs if x['id'] == run_id), None)
        if not record: raise KeyError(run_id)
        return record

    def _add(self, **fields):
        with self._lock:
            if sum(x.get('status') in ('pending','running') for x in self._runs) >= 30:
                raise ValueError('待处理工具已达上限，请先检查工具记录')
            item = dict(id=str(uuid4()), created_at=utc_now(), updated_at=utc_now(), **fields)
            self._runs.append(item)
            self._runs = self._runs[-200:]
            self._save()
            return copy.deepcopy(item)

    def _update(self, run_id, **patch):
        with self._lock:
            record = self._get(run_id)
            record.update(patch, updated_at=utc_now())
            self._save()
            return copy.deepcopy(record)

    def list(self, scope_id=None, status=None):
        with self._lock:
            return [self._public_run(x) for x in reversed(self._runs) if (not scope_id or x.get('scope_id') == scope_id) and (not status or x.get('status') == status)]

    def _public_run(self, item):
        result = copy.deepcopy(item)
        for key in ('arguments','result','error','tool_name','server_name'):
            if key in result: result[key] = self._redact(result[key])
        return result

    def export_history(self):
        """Export evidence for human review, without executable requests or secrets."""
        from .db import _redact
        fields = ('id','created_at','updated_at','scope_id','user_id','kind','tool_name','server_name',
                  'status','arguments','result','error','read_only','original_status','original_id')
        with self._lock:
            runs = [_redact(self._redact({k:x[k] for k in fields if k in x})) for x in self._runs[-200:]]
        return encoded({'version':1,'exported_at':utc_now(),'runs':runs})

    @staticmethod
    def validate_history(state):
        if not isinstance(state,dict) or state.get('version') != 1 or not isinstance(state.get('runs'),list) or len(state['runs'])>200:
            raise ValueError('工具历史备份格式无效')
        if len(encoded(state).encode('utf-8'))>4*1024*1024:
            raise ValueError('工具历史备份过大')
        seen=set()
        for item in state['runs']:
            if not isinstance(item,dict) or not isinstance(item.get('id'),str) or not 1<=len(item['id'])<=160 or item['id'] in seen:
                raise ValueError('工具历史ID无效或重复')
            seen.add(item['id'])
            if item.get('status') not in ('pending','running','succeeded','failed','cancelled','unknown','rejected','approved'):
                raise ValueError('工具历史状态无效')
            for key in ('scope_id','user_id','tool_name','kind','error'):
                if key in item and not isinstance(item[key],str): raise ValueError('工具历史字段格式无效')
            if 'original_id' in item and (not isinstance(item['original_id'],str) or not 1<=len(item['original_id'])<=160): raise ValueError('原始工具历史ID无效')
        return state

    def import_history(self, state):
        self.validate_history(state)
        from .db import _redact
        with self._lock:
            previous=copy.deepcopy(self._runs)
            ids={x['id'] for x in self._runs}
            imported=0
            try:
                for record in state['runs']:
                    # A restored receipt is evidence only. Original requests and
                    # provenance are deliberately discarded, so it cannot execute
                    # or become authoritative model context in a new timeline.
                    original_id=record.get('original_id') or record['id']
                    key='history-'+hashlib.sha256(original_id.encode()).hexdigest()[:32]
                    if record['id'] in ids or key in ids: continue
                    item=_redact(self._redact({k:v for k,v in record.items() if k in
                        ('created_at','updated_at','scope_id','user_id','kind','tool_name','server_name','status','arguments','result','error')}))
                    original=item['status']
                    item.update(id=key,original_id=original_id,read_only=True,original_status=record.get('original_status') or original,provenance_complete=False,
                                status='cancelled' if original=='pending' else 'unknown' if original=='running' else original)
                    self._runs.append(item); ids.add(key); imported+=1
                def order(item):
                    try:
                        date=datetime.fromisoformat(str(item.get('updated_at') or item.get('created_at') or ''))
                        timestamp=(date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
                    except (ValueError,OverflowError,OSError): timestamp=0
                    return (item.get('status') in ('pending','running'),timestamp,item['id'])
                self._runs=sorted(self._runs,key=order)[-200:]
                self._save()
            except Exception:
                self._runs=previous
                raise
        return imported

    def status(self):
        allowed = self.external_tools_allowed()
        return {'warning':self.recovery_warning or ('' if allowed else 'Docker隔离模式已停用沙箱外MCP工具，可在电脑任务高级设置中明确放行。'),
                'external_tools_allowed':allowed,'suspended':self.mcp_suspended,'servers': [dict(id=x['id'], name=x['name'], enabled=x['enabled'], configured=bool(x['url']),
                                tools_count=len(self._catalog.get(x['id'], {}).get('tools', [])), **self._status.get(x['id'], {})) for x in self.settings.get().get('mcp_servers', [])]}

    async def discover(self, server_id):
        config = self._config(server_id)
        try:
            result = await self.client.discover(dict(config,enabled=True))
            if self._identity(config) != self._identity(self._config(server_id)) or self._closed:
                raise ValueError('发现期间配置已变化，请重新发现')
            accepted = []
            for item in result['tools']:
                try: bounded_schema(item['inputSchema'])
                except ValueError: continue
                accepted.append(item)
            self._catalog[server_id] = dict(identity=self._identity(config), tools=accepted, expires=time.monotonic()+900)
            self._status[server_id] = dict(last_error='', last_checked_at=utc_now())
            return self._redact(dict(result, tools=accepted, message=f'发现{len(accepted)}个可用工具；不支持的复杂参数定义已过滤。调用遵循电脑控制权限和当前执行模式。'))
        except Exception as exc:
            self._status[server_id] = dict(last_error='发现失败，请检查协议、地址和认证', last_checked_at=utc_now())
            raise ValueError('工具发现失败，请检查服务地址、认证和受支持的协议版本') from exc

    def _identity_for(self,scope_id,user_id,identity=None):
        access = getattr(self.chat,'access',None)
        if access is None: return identity
        if identity is None and scope_id.startswith('web:') and user_id=='owner':
            return access.identity('local','mikubot')
        return identity

    def external_tools_allowed(self):
        computer = self.settings.get().get('computer', {})
        return not (computer.get('enabled') and computer.get('runtime') == 'docker'
                    and not computer.get('allow_external_tools', False))

    def available(self, scope_id, user_id, identity=None):
        if self.mcp_suspended or not self.external_tools_allowed(): return []
        access=getattr(self.chat,'access',None)
        if access is not None and not access.is_admin(self._identity_for(scope_id,user_id,identity)): return []
        scope = self.store.get_scope(scope_id)
        if not scope.get('allow_mcp', False) or not scope.get('allow_tools', True) or not self.chat.social._allowed(scope_id): return []
        items = []
        for cfg in self.settings.get().get('mcp_servers', []):
            cache = self._catalog.get(cfg['id'], {})
            if not cfg['enabled'] or cache.get('identity') != self._identity(cfg) or cache.get('expires',0) < time.monotonic(): continue
            for tool in cache['tools']:
                if tool['name'] not in cfg['allowed_tools']: continue
                alias = 'mcp_' + hashlib.sha256((cfg['id']+':'+tool['name']).encode()).hexdigest()[:20]
                computer = getattr(self.chat, 'computer', None)
                automatic = bool(computer and computer.automatic() and computer.allowed(self._identity_for(scope_id,user_id,identity),scope_id))
                execution = '已开启电脑控制，按管理员当前权限直接执行，以实际回执为准。' if automatic else '需管理员逐次批准，以实际回执为准。'
                items.append({'type':'function','function':{'name':alias,'description':f"调用{cfg['name']}的{tool['name']}。{execution}" + str(tool.get('description',''))[:250], 'parameters':tool['inputSchema']}, '_server_id':cfg['id'], '_tool':tool})
                if len(items) >= 16: return items
        return items

    def propose(self, alias, arguments, scope_id, user_id, metadata):
        identity=self._identity_for(scope_id,user_id,metadata.get('trusted_identity'))
        entry = next((x for x in self.available(scope_id, user_id,identity) if x['function']['name'] == alias), None)
        if not entry: raise ValueError('MCP 工具未允许、未发现或目录已过期')
        validate_arguments(entry['_tool']['inputSchema'], arguments)
        if self._redact(arguments) != arguments: raise ValueError('MCP 参数含本地配置密钥，拒绝发送')
        cfg = self._config(entry['_server_id'])
        sources = list(metadata.get('source_ids', []))
        context = list(metadata.get('_context_source_ids', sources))
        if not sources or self.chat._sources_changed(scope_id, context): raise ValueError('操作来源已失效')
        fingerprints = {x:message_fingerprint(self.store.get_message(x)) for x in context}
        item = self._add(scope_id=scope_id, user_id=user_id, server_id=cfg['id'], server_name=cfg['name'], tool_name=entry['_tool']['name'],
                         arguments=copy.deepcopy(arguments), status='pending', source_ids=sources, context_source_ids=context,
                         source_fingerprints=fingerprints, memory_versions=metadata.get('_memory_versions',{}).copy(), jargon_versions=metadata.get('_jargon_versions',{}).copy(),
                         knowledge_refs=metadata.get('_knowledge_refs',{}).copy(), persona_id=metadata.get('_persona_id','default'), provenance_complete=True,
                         request_generation=metadata.get('request_generation'), config_identity=self._identity(cfg), schema_hash=entry['_tool']['schema_hash'],
                         trusted_identity=identity,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
        return {'status':'pending_approval','executed':False,'run_id':item['id'],'message':'仅提出操作，尚未执行。请在本地 MCP 页检查参数并批准。'}

    def _check(self, item):
        if item.get('read_only'): raise ValueError('恢复的工具回执仅供查看，不能重新执行')
        if self.mcp_suspended: raise ValueError('MCP执行因记录故障暂停')
        if not self.external_tools_allowed():
            raise PermissionError('Docker隔离模式未允许沙箱外MCP工具，本次未调用服务')
        scope_id, user_id = item['scope_id'], item['user_id']
        access=getattr(self.chat,'access',None)
        if access is not None and not access.is_admin(self._identity_for(scope_id,user_id,item.get('trusted_identity'))):
            raise PermissionError('任务发起人的管理员权限已撤销')
        scope = self.chat._allowed(scope_id, user_id, {})
        self.chat._guard_tool(scope_id,user_id,{'request_generation':item.get('request_generation'),'source_ids':item['source_ids'],
                                              '_context_source_ids':item['context_source_ids'],'_memory_versions':item['memory_versions'],'_jargon_versions':item['jargon_versions'],
                                              '_knowledge_refs':item.get('knowledge_refs',{}),'_persona_id':item.get('persona_id','default')})
        if self._closed or not scope.get('allow_mcp',False) or not scope.get('allow_tools',True) or not self.chat._request_current(scope_id,user_id,{'request_generation':item.get('request_generation')}):
            raise ValueError('请求已替换、暂停或关闭 MCP 权限')
        cfg = self._config(item['server_id'])
        if not cfg['enabled'] or item['tool_name'] not in cfg['allowed_tools'] or item['config_identity'] != self._identity(cfg): raise ValueError('服务配置或工具权限已变化')
        if datetime.fromisoformat(item['expires_at']) <= datetime.now(timezone.utc): raise ValueError('审批已过期，请重新提出请求')
        if self.chat._sources_changed(scope_id,item['context_source_ids']) or self.chat._changed(item['memory_versions'],item['jargon_versions'],scope_id,user_id): raise ValueError('审批来源或引用版本已变化')
        if item.get('request_generation') is None:
            latest = next((x for x in reversed(self.store.list_messages(scope_id,limit=1000)) if x['role']=='user' and x['user_id']==user_id), None)
            if latest is None or latest['id'] not in item['source_ids']: raise ValueError('已有后续用户消息，请重新提出操作')
        if any(message_fingerprint(self.store.get_message(k)) != v for k,v in item['source_fingerprints'].items()): raise ValueError('审批原始消息已变化')
        return cfg

    async def approve(self, run_id):
        with self._lock:
            item = copy.deepcopy(self._get(run_id))
            if item['status'] != 'pending': raise ValueError('此操作不在待批准状态，不会重复执行')
            try: cfg = self._check(item)
            except (ValueError,KeyError,PermissionError):
                return self._update(run_id,status='cancelled',error='权限、配置、来源或请求已变化，本次未执行')
            self._update(run_id,status='running')
        called = False
        def before_call():
            nonlocal called
            self._check(item)
            called = True
        try:
            result = await self.client.call(cfg,item['tool_name'],item['arguments'],expected_schema_hash=item['schema_hash'],before_call=before_call)
            return self._update(run_id,status='failed' if result.get('isError') else 'succeeded',result=self._redact(bounded_result(result,8000)))
        except asyncio.CancelledError:
            self._update(run_id,status='unknown' if called else 'cancelled',error='操作被中断；已发出的请求需人工核对，不会自动重试')
            raise
        except Exception:
            return self._update(run_id,status='unknown' if called else 'cancelled',error='已发送请求但结果无法确认，请核对目标服务，不会重试' if called else '调用前检查失败，本次未执行，请重新发现工具或检查权限')

    def reject(self, run_id):
        with self._lock:
            if self._get(run_id)['status'] != 'pending': raise ValueError('只能拒绝待批准操作')
            return self._update(run_id,status='rejected',error='管理员未批准，未执行')

    def begin_local(self, name, arguments, scope_id, user_id, sources, metadata=None):
        metadata=metadata or {}
        return self._add(scope_id=scope_id,user_id=user_id,server_id='',server_name='内置工具',tool_name=name,arguments=self._redact(arguments),source_ids=list(sources),
                         context_source_ids=list(metadata.get('_context_source_ids') or sources),memory_versions=dict(metadata.get('_memory_versions') or {}),
                         jargon_versions=dict(metadata.get('_jargon_versions') or {}), knowledge_refs=dict(metadata.get('_knowledge_refs') or {}),
                         persona_id=metadata.get('_persona_id', 'default'), provenance_complete=True,status='running')['id']

    def finish_local(self, run_id, result, error=None, *, context_source_ids=None,memory_versions=None,jargon_versions=None,knowledge_refs=None):
        patch={}
        for key,value in (('context_source_ids',context_source_ids),('memory_versions',memory_versions),('jargon_versions',jargon_versions),('knowledge_refs',knowledge_refs)):
            if value is not None: patch[key]=copy.deepcopy(value)
        return self._update(run_id,status='failed' if error else 'succeeded',result=self._redact(bounded_result(result,8000)),error=error or '',**patch)

    def resume(self):
        if self._closed: raise ValueError('运行已结束')
        self._save()
        self.mcp_suspended=False
        self.recovery_warning=''
        return {'ok':True,'message':'已恢复工具记录功能，旧操作不会重放，请重新发现工具。'}

    def cancel_request(self, scope_id, user_id, generation):
        with self._lock:
            for item in self._runs:
                if item.get('status') == 'pending' and item.get('scope_id') == scope_id and item.get('user_id') == user_id and item.get('request_generation') == generation:
                    item.update(status='cancelled',error='请求等待已取消',updated_at=utc_now())
            self._save()

    def close(self):
        self._closed = True
        with self._lock:
            for item in self._runs:
                if item['status'] == 'pending': item.update(status='cancelled',error='运行已结束，待审批不会重放',updated_at=utc_now())
            self._save()
