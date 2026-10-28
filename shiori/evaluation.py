"""Isolated chat replay. Offline fixtures are routing regressions, not AI scores."""
from __future__ import annotations
import argparse
import asyncio
import copy
import json
import math
import re
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .chat import ChatService
from .db import Store
from .settings import DEFAULTS, Settings
from .provider import Provider, ProviderError, is_budget_exceeded


class ReplayProvider(Provider):
    """Expose budget vetoes even when the Planner safely turns failures into skip."""
    def __init__(self, settings, store):
        super().__init__(settings, store)
        self.budget_reason = ''

    async def complete(self, *args, **kwargs):
        try:
            return await super().complete(*args, **kwargs)
        except ProviderError as exc:
            reason = str(exc)
            if is_budget_exceeded(exc):
                self.budget_reason = ('Planner 次数或 Token 预算不足' if 'Planner' in reason else
                                      '费用预算不足' if '费用' in reason else '模型 Token 预算或服务额度不足')
            raise


def read_initial_usage(data_dir, *, now=None):
    """Read today's aggregate through SQLite's read-only mode; never open Store."""
    path = Path(data_dir) / 'qunyu.db'
    if not path.exists():
        return {'input_tokens': 0, 'output_tokens': 0, 'tokens': 0, 'cost': 0.0}
    local = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    boundaries = [(start + timedelta(days=offset)).astimezone(timezone.utc)
                  .isoformat(timespec='microseconds').replace('+00:00', 'Z') for offset in (0, 1)]
    try:
        wal = path.with_name(path.name + '-wal')
        # A checkpointed WAL-mode database otherwise creates empty -wal/-shm
        # files even through mode=ro. Immutable reads avoid those side effects.
        # An existing live WAL must remain visible, so use normal read-only
        # SQLite snapshot semantics while that journal contains committed data.
        options = '?mode=ro' if wal.exists() and wal.stat().st_size else '?mode=ro&immutable=1'
        connection = sqlite3.connect(path.resolve().as_uri() + options, uri=True, timeout=5)
        try:
            connection.execute('PRAGMA query_only=ON')
            row = connection.execute('SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),'
                                     'COALESCE(SUM(cost),0) FROM usage WHERE created_at>=? AND created_at<?', boundaries).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError('无法只读取得原数据今日用量，真实模型回放已停止；请检查数据库。') from exc
    return _usage({'input_tokens': row[0], 'output_tokens': row[1], 'cost': row[2]})


def load_replay_config(data_dir):
    """Validate/decrypt a temporary copy so loading never repairs the user's files."""
    path = Path(data_dir) / 'settings.json'
    if not path.is_file():
        raise ValueError('真实模型回放需要已有 settings.json 配置')
    with tempfile.TemporaryDirectory(prefix='shiori-replay-config-') as directory:
        (Path(directory) / 'settings.json').write_bytes(path.read_bytes())
        settings = Settings(Path(directory))
        if settings.warning:
            raise ValueError('原配置不能完整读取，真实模型回放已停止；请先在管理台修复配置。')
        config = settings.get()
    if config['paused']:
        raise ValueError('原配置已暂停，请先检查配置并恢复运行')
    return config


def _usage(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError('初始用量须为对象')
    inputs = value.get('input_tokens', 0)
    outputs = value.get('output_tokens', 0)
    if any(type(x) is not int or x < 0 for x in (inputs, outputs)):
        raise ValueError('初始用量须为非负有效 Token 与费用')
    tokens = value.get('tokens', inputs + outputs)
    cost = value.get('cost', 0.0)
    if (any(type(x) is not int or x < 0 for x in (inputs, outputs, tokens))
            or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0 or tokens < inputs + outputs):
        raise ValueError('初始用量须为非负有效 Token 与费用')
    return {'input_tokens': inputs, 'output_tokens': outputs, 'tokens': tokens, 'cost': float(cost)}


def _remaining_budget(config, initial, consumed):
    """A zero configured cap means unlimited; an exhausted positive cap never becomes zero."""
    budget = dict(config['budget'])
    for field, usage_field, reason in (('daily_tokens', 'tokens', '今日 Token 预算已用完'),
                                       ('daily_cost', 'cost', '今日费用预算已用完')):
        limit = budget.get(field, 0)
        if (type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0
                or (field == 'daily_tokens' and type(limit) is not int)):
            raise ValueError('回放预算配置无效')
        if limit:
            remaining = limit - initial[usage_field] - consumed[usage_field]
            if remaining <= 0:
                return None, reason
            budget[field] = int(remaining) if field == 'daily_tokens' else remaining
    return budget, ''


class ReplaySettings:
    def __init__(self,config): self.config=copy.deepcopy(config)
    def get(self): return copy.deepcopy(self.config)


class ScriptedProvider:
    def __init__(self,case): self.case=case; self.calls=[]
    async def complete(self,messages,**kwargs):
        if kwargs.get('before_send') and not kwargs['before_send'](): raise ValueError('回放来源已失效')
        purpose=kwargs.get('purpose','chat'); self.calls.append(purpose)
        if purpose=='planner':
            decision=dict(self.case.get('scripted_decision',{'action':'skip','reason':'回归脚本未指定回复'}))
            # The target is the fixture's current message, never an invented ID.
            payload=json.loads(messages[-1]['content'])
            decision.setdefault('target_message_id',payload['current_message_id'])
            decision.setdefault('target_user_id',payload['current_user_id'])
            decision.setdefault('addressed_to_bot',decision.get('action')=='reply')
            decision.setdefault('intent','参与当前交流' if decision.get('action')=='reply' else '')
            decision.setdefault('needed_info',[])
            content=json.dumps(decision,ensure_ascii=False)
        else: content=self.case.get('scripted_reply','收到。')
        return {'content':content,'tool_calls':[],'message':{'role':'assistant','content':content},'usage':{}}


def validate_cases(value):
    if not isinstance(value,list) or not 1<=len(value)<=500: raise ValueError('回放集须含1—500个案例')
    seen=set()
    for case in value:
        if not isinstance(case,dict) or not isinstance(case.get('id'),str) or case['id'] in seen: raise ValueError('案例ID须唯一')
        seen.add(case['id'])
        if not isinstance(case.get('text'),str) or not 1<=len(case['text'])<=12000: raise ValueError('案例消息无效')
        expected=case.get('expected')
        if not isinstance(expected,dict) or type(expected.get('reply')) is not bool: raise ValueError('每个案例须标注expected.reply')
        history=case.get('history',[])
        if not isinstance(history,list) or len(history)>100 or any(not isinstance(x,dict) or not isinstance(x.get('text'),str) or len(x['text'])>12000 for x in history): raise ValueError('案例历史无效')
        if case.get('scope_kind','group') not in ('group','private'): raise ValueError('scope_kind须为group或private')
    return value


async def replay(cases,*,config=None,live=False,generate=False,initial_usage=None):
    validate_cases(cases)
    base_config=copy.deepcopy(config or DEFAULTS)
    initial=_usage(initial_usage)
    consumed=_usage()
    records=[]; stopped_reason=''
    for case in cases:
        budget, reason = _remaining_budget(base_config, initial, consumed) if live else (base_config['budget'], '')
        if reason:
            stopped_reason=reason
            records.append({'id':case['id'],'error':'ReplayBudgetLimit','reason':reason,'not_evaluated':True,
                            'decision_correct':False,'usage':_usage()})
            break
        with tempfile.TemporaryDirectory(prefix='shiori-replay-') as directory:
            store=Store(Path(directory)); cfg=copy.deepcopy(base_config)
            cfg['budget']=budget
            user=str(case.get('user_id','alice'))
            scope_id='qq:group:900001' if case.get('scope_kind','group')=='group' else 'qq:private:'+user
            cfg['paused']=False
            # No connector is constructed. These IDs authorize only the isolated
            # replay, and no task/tool can contact an external action service.
            cfg['onebot'].update(enabled=True,adapter='napcat',allowed_groups=['900001'],owner_ids=[user])
            cfg['memory']['organization_enabled']=False
            cfg['exploration']['enabled']=False
            if not live: cfg['model'].update(base_url='https://fixture.invalid/v1',model='scripted')
            settings=ReplaySettings(cfg)
            provider=ReplayProvider(settings,store) if live else ScriptedProvider(case)
            chat=ChatService(store,settings,provider)
            scope=store.upsert_scope({'id':scope_id,**case.get('scope',{}),'auto_memory':False,
                                      'allow_tools':False,'allow_mcp':False,'dictionary_learn':False,'expression_learn':False})
            try:
                for message in case.get('history',[]):
                    role=message.get('role','user')
                    store.add_message(scope_id,str(message.get('user_id','bob')),role,message['text'])
                observed=await chat.observe(scope_id,user,case['text'],metadata=case.get('metadata',{}))
                if not live:
                    # Fill current target outside the model script, preserving
                    # the same strict target validation as normal runtime.
                    provider.case=copy.deepcopy(case)
                    provider.case.setdefault('scripted_decision',{})['target_message_id']=observed['id']
                started=time.monotonic()
                plan=await chat.social.plan(scope,observed,direct=bool(case.get('direct',False)))
                result=None
                if generate and plan.get('reply'):
                    result=await chat.respond(scope_id,user,case['text'],metadata={'existing_message_id':observed['id'],
                        'merged_message_ids':[observed['id']],'planner':plan,'request_generation':plan.get('generation')})
                if live and provider.budget_reason:
                    raise ProviderError(provider.budget_reason)
                expected=case['expected']; answered=bool(plan.get('reply'))
                checks=expected.get('useful_contains',[])
                reply=(result or {}).get('reply','')
                records.append({'id':case['id'],'expected_reply':expected['reply'],'reply':answered,
                    'decision_correct':answered==expected['reply'],
                    'target_correct':None if not answered else plan.get('target_user_id')==expected.get('target_user_id',user),
                    'usefulness_proxy':None if not generate or not checks else all(x in reply for x in checks),
                    'latency_ms':round((time.monotonic()-started)*1000,2),
                    'plan':plan,'output':reply,'human_usefulness':None})
            except Exception as exc:
                if live and provider.budget_reason:
                    stopped_reason=provider.budget_reason
                records.append({'id':case['id'],'error':'ReplayBudgetLimit' if stopped_reason else type(exc).__name__,
                                'reason':stopped_reason,'not_evaluated':True,'decision_correct':False})
            finally:
                # Unknown dispatched usage is conservatively settled by Provider.
                # It must count even when a failed case produces no final reply.
                await chat.close()
                usage=store.usage_totals()
                if records and records[-1]['id']==case['id']:
                    records[-1]['usage']=usage
                    usage_records=store.list_usage(10000)
                    records[-1]['usage_estimated']=any(x.get('estimated') for x in usage_records)
                    records[-1]['usage_unconfirmed']=any(x.get('unconfirmed') for x in usage_records)
                for field in consumed:
                    consumed[field]+=usage[field]
                store.close()
        if stopped_reason:
            break
    valid=[x for x in records if 'error' not in x]
    predicted=[x for x in valid if x['reply']]; expected_count=sum(x['expected_reply'] for x in valid)
    useful=[x['usefulness_proxy'] for x in valid if x['usefulness_proxy'] is not None]
    return {'version':1,'mode':'live_model' if live else 'scripted_routing_regression',
        'dataset_origin':'user supplied or authored synthetic fixtures; no production chat implied',
        'summary':{'cases':len(cases),'attempted_cases':len(records),'completed_cases':len(valid),
            'unrun_cases':len(cases)-len(records),'errors':len(records)-len(valid),'budget_stopped':bool(stopped_reason),
            'decision_accuracy':sum(x['decision_correct'] for x in valid)/len(valid) if valid else None,
            'reply_precision':sum(x['expected_reply'] for x in predicted)/len(predicted) if predicted else None,
            'reply_recall':sum(x['reply'] and x['expected_reply'] for x in valid)/expected_count if expected_count else None,
            'target_accuracy':sum(x['target_correct'] for x in predicted)/len(predicted) if predicted else None,
            'usefulness_proxy':sum(useful)/len(useful) if useful else None,
            'latency_p50_ms':statistics.median(x['latency_ms'] for x in valid) if valid else None,
            'input_tokens':consumed['input_tokens'],'output_tokens':consumed['output_tokens'],
            'tokens':consumed['tokens'],'cost':consumed['cost']},'records':records,
        'budget':{'initial_usage':initial,'run_usage':consumed,'configured_limits':base_config['budget'],
                  'stopped_reason':stopped_reason},
        'limitations':['离线脚本仅验证路由与控制逻辑，不代表模型理解能力。','usefulness_proxy只是标注词匹配；真实有用性须人工审阅输出。','时延为本机单次回放，不能替代线上QQ实测。',
                       '真实模型回放按原库今日用量快照及本次累计用量扣减预算；不与其他进程共享原子额度，也不回写原库账单。',
                       '预算不足而未完成的案例不计入判断准确率；费用依赖所配置价格，模型未报告用量时采用保守估算。']}


def main():
    parser=argparse.ArgumentParser(description='shiori isolated chat replay')
    parser.add_argument('dataset',type=Path); parser.add_argument('--output',type=Path,default=Path('replay-report.json'))
    parser.add_argument('--live',action='store_true',help='Explicitly call configured model; may incur charges')
    parser.add_argument('--data-dir',type=Path,help='Read model configuration; never modify the original data')
    parser.add_argument('--generate',action='store_true',help='Also generate text with all action tools disabled')
    args=parser.parse_args()
    config=None; initial_usage=None
    if args.live:
        if args.data_dir is None: parser.error('--live requires --data-dir')
        config=load_replay_config(args.data_dir)
        initial_usage=read_initial_usage(args.data_dir)
    cases=json.loads(args.dataset.read_text('utf-8-sig'))
    report=asyncio.run(replay(cases,config=config,live=args.live,generate=args.generate,initial_usage=initial_usage))
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report['summary'],ensure_ascii=False))

if __name__=='__main__': main()
