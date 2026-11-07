"""Bounded, redacted runtime diagnostics and message-pipeline events."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from fastapi.responses import Response


CATEGORIES = {'qq': 'QQ 收发', 'planner': '回复判断', 'reply': '回复生成', 'model': '模型调用', 'memory': '记忆',
              'learning': '黑话与表达', 'stickers': '表情包', 'schedule': '角色日程', 'knowledge': '人格知识', 'system': '系统'}
KIND_LABELS = {
    'qq_sent': '已发送', 'qq_delivery': '送达问题', 'qq_output': '分段发送', 'qq_outbox': '分段记录', 'qq_ingress': '收到消息',
    'qq_debounce': '消息合并', 'qq_suppressed': '停止发送', 'qq_reply_timing': '回复耗时', 'qq_rate_limited': '群限速',
    'qq_feedback': '失败提示', 'qq_image_error': '图片读取', 'qq_error': 'QQ 错误', 'qq_protocol_error': '协议错误',
    'planner_timing': '回复判断', 'planner_error': '判断失败', 'planner_format_retry': '格式重试', 'planner_batch_error': '集中判断失败',
    'chat_timing': '生成回复', 'model': '备用模型', 'model_output_budget': '输出额度', 'model_stream_incomplete': '流中断',
    'model_call': '模型调用', 'memory_use': '使用记忆', 'memory_cursor': '整理进度', 'memory_extract_attempt': '开始整理',
    'memory_extract_result': '整理结果', 'memory_error': '整理失败', 'social': '黑话学习', 'social_error': '学习失败',
    'stickers': '收集表情', 'stickers_error': '收集失败', 'stickers_skipped': '未收集', 'stickers_retry': '识图重试',
    'sticker_timeout': '选图超时', 'sticker_send_error': '发送失败', 'knowledge_exploration': '兴趣探索',
    'distillation_discovery': '蒸馏检索', 'settings': '设置变更', 'search_attempt': '联网搜索', 'admin_pairing': '管理员配对',
}
PREFIXES = (('qq', 'qq'), ('planner', 'planner'), ('chat', 'reply'), ('model', 'model'), ('memory', 'memory'),
            ('social', 'learning'), ('jargon', 'learning'), ('expression', 'learning'), ('sticker', 'stickers'),
            ('schedule', 'schedule'), ('knowledge', 'knowledge'), ('distillation', 'knowledge'))
OUTCOMES = {'reply': '回复', 'skip': '旁听', 'failed': '失败', 'cancelled': '已取消'}
GATES = {'reply': '达到门槛', 'below_threshold': '低于门槛', 'addressed': '在对它说', 'addressed_only': '只回直接对它说的'}
AUDIENCES = {'bot': '对它说', 'member': '对成员说', 'group': '对全群说', 'none': '自言自语'}
_SKIP = {'id', 'scope_id', 'user_id', 'flow_trace_id', 'trace_id', 'request_id', 'request_ids', 'source_ids',
         'message_id', 'assistant_message_id', 'platform_message_id', 'traceback', 'error_id', 'sticker_id', 'persona_id'}


def category(kind):
    return next((value for prefix, value in PREFIXES if kind.startswith(prefix)), 'system')


def _ms(value):
    return f'{value / 1000:.1f}s' if isinstance(value, (int, float)) and value >= 1000 else f'{value:.0f}ms' if isinstance(value, (int, float)) else ''


def summary(kind, details):
    """One readable line of the details that matter for this kind of event."""
    d = details or {}
    parts = []
    if kind == 'planner_timing':
        parts.append(OUTCOMES.get(d.get('outcome'), d.get('outcome') or ''))
        gate = d.get('gate') or {}
        if gate:
            parts.append(f"价值 {gate.get('value')} / 门槛 {gate.get('threshold')}")
            if gate.get('audience'):
                parts.append(AUDIENCES.get(gate['audience'], gate['audience']))
            parts.append(GATES.get(gate.get('result'), ''))
        if d.get('error_code'):
            parts.append('错误 ' + str(d['error_code']))
        parts.append('用时 ' + _ms(d.get('elapsed_ms')) if d.get('elapsed_ms') else '')
    elif kind == 'planner_error':
        parts += [str(d.get('code') or ''), '阶段 ' + str(d.get('stage')) if d.get('stage') else '',
                  str(d.get('termination_reason') or '')]
    elif kind in ('qq_sent', 'qq_delivery'):
        part = d.get('output_part') or {}
        timing = d.get('send_timing') or {}
        if part:
            parts.append(f"第 {part.get('index')}/{part.get('total')} 段")
        if timing.get('ack_wait_ms') is not None:
            parts.append('回执 ' + _ms(timing['ack_wait_ms']))
        if timing.get('rate_wait_ms'):
            parts.append('限速等待 ' + _ms(timing['rate_wait_ms']))
        parts += ['平台代码 ' + str(d['platform_code']) if d.get('platform_code') else '',
                  str(d.get('reason') or ''), {'not_sent': '确认未送达', 'unknown': '送达未知'}.get(d.get('delivery_status'), '')]
    elif kind == 'qq_rate_limited':
        parts += [f"每分钟最多 {d.get('limit')} 条", f"等待 {d.get('wait_seconds')} 秒", '已是重试' if d.get('retry') else '']
    elif kind == 'qq_reply_timing':
        parts += ['全程 ' + _ms(d.get('flow_ms')) if d.get('flow_ms') else '', '判断 ' + _ms(d.get('planner_ms')) if d.get('planner_ms') else '',
                  '发送 ' + _ms(d.get('text_delivery_ms')) if d.get('text_delivery_ms') else '']
    elif kind == 'chat_timing':
        timings = d.get('timings') or {}
        parts += [str(d.get('status') or ''), '生成 ' + _ms(timings.get('total_ms')) if timings.get('total_ms') else '']
    elif kind == 'memory_extract_result':
        parts += [str(d.get('status') or ''), f"来源 {d.get('input_sources')} 条" if d.get('input_sources') is not None else '',
                  f"新增 {d.get('inserted')} 条" if d.get('inserted') else '']
    elif kind == 'knowledge_exploration':
        parts += [str(d.get('interest') or ''), '搜索 ' + '；'.join(str(item.get('query')) for item in d.get('searches') or []) if d.get('searches') else '',
                  f"新增 {d.get('created')}" if d.get('created') else '', f"过滤 {d.get('filtered')}" if d.get('filtered') else '']
    elif kind == 'model_call':
        parts += [f"输入 {d.get('input_tokens', 0)} · 输出 {d.get('output_tokens', 0)} token",
                  '首字 ' + _ms(d.get('ttft_ms')) if d.get('ttft_ms') else '', '用时 ' + _ms(d.get('latency_ms')) if d.get('latency_ms') else '',
                  '排队 ' + _ms(d.get('queue_wait_ms')) if d.get('queue_wait_ms') else '']
    else:
        for key, value in d.items():
            if key in _SKIP or isinstance(value, (dict, list)) or value in (None, '', False):
                continue
            parts.append(f'{key} {value}' if not isinstance(value, str) else value if len(value) <= 60 else value[:60] + '…')
            if len(parts) >= 4:
                break
        if d.get('error_message'):
            parts.append(str(d['error_message'])[:120])
    return ' · '.join(part for part in parts if part)


def severity(kind, details):
    code = str((details or {}).get('reason') or (details or {}).get('code') or (details or {}).get('status') or '').lower()
    if kind.endswith('_error') or 'traceback' in (details or {}) or any(x in code for x in ('error', 'failed')):
        return 'ERROR'
    if (kind in ('qq_rate_limited', 'model_output_budget', 'model_stream_incomplete', 'qq_suppressed')
            or (details or {}).get('delivery_status') in ('not_sent', 'unknown')
            or any(x in code for x in ('blocked', 'denied', 'not_allowed', 'missing', 'unconfirmed'))):
        return 'WARNING'
    return 'INFO'


def install_logs(app, settings, store, data_dir):
    path = Path(data_dir) / 'runtime.log'

    def redact(text):
        cfg = settings.get()
        secrets = [settings.token, cfg['model'].get('api_key'), cfg['onebot'].get('access_token'), cfg['qq_official'].get('app_secret')]
        secrets += [x.get('api_key') for x in [*cfg.get('providers',[]), *cfg.get('provider_sources',[]), *cfg.get('search_providers',[])]]
        secrets += [x.get('token') for x in cfg.get('mcp_servers',[])]
        value = str(text)
        for secret in secrets:
            if secret and (len(secret) >= 6 or value == secret): value = value.replace(secret, '[已隐藏]')
        value = re.sub(r'(?i)(Bearer\s+)[a-z0-9_\-.+/=]{8,}',r'\1[已隐藏]',value)
        value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|app[_-]?secret)\s*[=:]\s*)[^\s,;}]+',r'\1[已隐藏]',value)
        return value

    store.exception_redactor = redact

    def scope_names():
        try:
            return {item['id']: str(item.get('name') or item['id'])[:32] for item in store.list_scopes()}
        except Exception:
            return {}

    def snapshot(limit=200, level='', q='', after='', category_filter=''):
        if not 1 <= limit <= 2000 or len(q)>256 or len(after)>160:
            raise ValueError('日志数量须为1—2000，搜索词不超过256字')
        level = level.upper()
        if level not in ('','ALL','DEBUG','INFO','WARNING','WARN','ERROR','CRITICAL'):
            raise ValueError('日志级别无效')
        if category_filter and category_filter not in CATEGORIES:
            raise ValueError('日志类别无效')
        if level == 'WARN': level = 'WARNING'
        rows = []
        names = scope_names()
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                stat = path.stat()
                start = max(0, stat.st_size - 512 * 1024)
                stream.seek(start)
                if start: stream.readline()
                stamp = datetime.fromtimestamp(stat.st_mtime,timezone.utc).isoformat()
                identity = hashlib.sha256(str((stat.st_dev,stat.st_ino)).encode()).hexdigest()[:12]
                for _ in range(4000):
                    offset = stream.tell()
                    raw = stream.readline(32768)
                    if not raw: break
                    text = raw.decode('utf-8',errors='replace').rstrip()
                    match = re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[,.]\d+)?)\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.*)',text)
                    previous = rows[-1] if rows and rows[-1]['source'] == 'runtime' else None
                    if not match and previous is not None and previous.get('_open'):
                        # A traceback belongs to the error line above it.
                        if len(previous['detail']) < 12000:
                            previous['detail'] += ('\n' if previous['detail'] else '') + redact(text)
                        continue
                    kind = 'ERROR' if text.startswith(('Traceback','  File ')) else 'INFO'
                    if match:
                        try: stamp = datetime.fromisoformat(match[1].replace(',','.')).astimezone(timezone.utc).isoformat()
                        except ValueError: pass
                        kind, text = match[2], match[3]
                    rows.append({'id':f'r:{identity}:{offset:016d}','time':stamp,'level':kind,'message':redact(text),'source':'runtime',
                                 'kind':'runtime','label':'运行日志','category':'system','category_label':CATEGORIES['system'],
                                 'text':redact(text),'summary':'','scope':'','detail':'','_open':bool(match)})
        for row in rows:
            row.pop('_open', None)
        for item in store.list_events(limit=1000):
            details = item.get('details') or {}
            kind = str(item['kind'])
            group = category(kind)
            detail = details.get('traceback') or (json.dumps(details, ensure_ascii=False, indent=1) if details else '')
            rows.append({'id':'e:'+item['id'],'time':item['created_at'],'level':severity(kind, details),
                         'message':redact(f"[{kind}] {item.get('message','')}" + (' '+json.dumps(details,ensure_ascii=False) if details else '')),'source':'event',
                         'kind':kind,'label':KIND_LABELS.get(kind, kind),'category':group,'category_label':CATEGORIES[group],
                         'text':redact(item.get('message','')),'summary':redact(summary(kind, details)),
                         'scope':names.get(details.get('scope_id'), details.get('scope_id') or '') if isinstance(details.get('scope_id'), str) else '',
                         'detail':redact(detail)[:12000]})
        # Model calls, so slow or failing models are visible next to events.
        for item in store.list_usage(limit=300):
            purpose = str(item.get('purpose') or '')
            text = f"{purpose or '调用'} · {item.get('model') or ''}"
            rows.append({'id':'u:'+str(item.get('id')),'time':item.get('created_at') or '','level':'INFO',
                         'message':redact(f'[model_call] {text}'),'source':'usage','kind':'model_call','label':KIND_LABELS['model_call'],
                         'category':'model','category_label':CATEGORIES['model'],'text':redact(text),'summary':summary('model_call', item),
                         'scope':names.get(item.get('scope_id'), item.get('scope_id') or '') if isinstance(item.get('scope_id'), str) else '',
                         'detail':''})
        rows.sort(key=lambda item:(item['time'],item['id']))
        cursor = rows[-1]['id'] if rows else after
        reset = not bool(after)
        if after:
            found = next((i for i,value in enumerate(rows) if value['id']==after),None)
            if found is not None: rows = rows[found+1:]
            else: reset = True
        needle = q.casefold()
        selected = [value for value in rows if (level in ('','ALL') or value['level']==level)
                    and (not category_filter or value['category'] == category_filter)
                    and (not needle or needle in (value['message'] + ' ' + value['summary'] + ' ' + value['scope'] + ' ' + value['label']).casefold())]
        return {'lines':selected[-limit:],'cursor':cursor,'reset':reset,'truncated':len(selected)>limit,
                'available':path.exists() or bool(rows),'categories':CATEGORIES,
                'message':'包含运行日志、QQ/回复判断/记忆等事件和模型调用记录；密钥已脱敏'}

    @app.get('/api/logs')
    async def logs(limit:int=200,level:str='',q:str='',after:str='',category:str=''):
        return snapshot(limit,level,q,after,category)

    @app.get('/api/logs/export')
    async def export_logs(level:str='',q:str='',category:str=''):
        data = snapshot(2000,level,q,'',category)
        def line(x):
            head = f"{x['time']} {x['level']} {x['message']}"
            extra = (f"\n    {x['summary']}" if x.get('summary') else '') + (
                '\n' + '\n'.join('    ' + part for part in x['detail'].splitlines()) if x.get('source') == 'runtime' and x.get('detail') else '')
            return head + extra
        text = '\n'.join(line(x) for x in data['lines'])+'\n'
        return Response(text,media_type='text/plain; charset=utf-8',headers={'Content-Disposition':'attachment; filename="shiori-runtime.log"'})

    app.state.log_snapshot = snapshot
