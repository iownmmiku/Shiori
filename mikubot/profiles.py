"""Reusable bot configurations, independent of memory sharing boundaries."""
import copy
import re

FIELDS = {'persona_id', 'model_id', 'activity', 'trigger_at', 'trigger_reply',
          'trigger_name', 'plan_enabled', 'proactive_reply', 'always_reply',
          'allow_tools', 'allow_mcp', 'use_persona_knowledge', 'private_always_reply',
          'auto_memory', 'dictionary_learn', 'dictionary_use', 'dictionary_understand',
          'expression_learn', 'expression_use', 'context_compress',
          'debounce_enabled', 'debounce_seconds', 'debounce_max_wait_seconds'}
DEFAULT_PROFILES = {'default_id': 'default', 'items': [
    {'id': 'default', 'name': '默认配置', 'values': {'persona_id': 'default', 'model_id': ''}}]}


def validate_profiles(value):
    if not isinstance(value, dict) or set(value) != {'default_id', 'items'}:
        raise ValueError('机器人配置格式无效')
    items = value['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 30:
        raise ValueError('机器人配置须为1—30份')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id', 'name', 'values'}:
            raise ValueError('配置须包含ID、名称与设置')
        key = item['id']
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key) or key in seen:
            raise ValueError('配置ID格式错误或重复')
        seen.add(key)
        if not isinstance(item['name'], str) or not 1 <= len(item['name'].strip()) <= 80:
            raise ValueError('配置名称须为1—80字')
        values = item['values']
        if not isinstance(values, dict) or set(values) - FIELDS:
            raise ValueError('配置包含不支持的设置')
        for field, data in values.items():
            if field in ('persona_id', 'model_id'):
                if not isinstance(data, str) or len(data) > 100:
                    raise ValueError('人格或模型ID格式错误')
            elif field == 'activity':
                if data not in ('quiet', 'balanced', 'active'): raise ValueError('回应方式无效')
            elif field in ('debounce_seconds', 'debounce_max_wait_seconds'):
                from math import isfinite
                low, high = (.1, 60) if field == 'debounce_seconds' else (1, 300)
                if data is not None and (type(data) not in (int, float) or not isfinite(data) or not low <= data <= high):
                    raise ValueError(f'消息防抖 {field} 须为空值或 {low}—{high} 秒')
            elif field == 'debounce_enabled':
                if data is not None and type(data) is not bool:
                    raise ValueError('消息防抖开关须为布尔值或空值')
            elif type(data) is not bool and not (data is None and field in ('trigger_at','trigger_reply','trigger_name','plan_enabled','proactive_reply')):
                raise ValueError('配置开关格式错误')
        if (values.get('debounce_seconds') is not None and values.get('debounce_max_wait_seconds') is not None
                and values['debounce_seconds'] > values['debounce_max_wait_seconds']):
            raise ValueError('消息防抖最长等待时间不能少于等待秒数')
    if value['default_id'] not in seen: raise ValueError('默认配置不存在')


def resolve_scope(scope, settings):
    config = settings.get()['bot_profiles']
    profile_id = scope.get('profile_id')
    if profile_id is None:  # Existing explicit choices stay intact until bound.
        return dict(scope, profile_name='旧会话独立设置', profile_legacy=True)
    profile = next((p for p in config['items'] if p['id'] == (profile_id or config['default_id'])), None)
    if profile is None: raise ValueError('会话绑定的机器人配置不存在')
    overrides = scope.get('profile_overrides', [])
    values = {k: v for k, v in profile['values'].items() if k not in overrides}
    if 'dictionary_learn' not in overrides:
        values.setdefault('dictionary_learn',settings.get().get('language',{}).get('learning_enabled',True))
    if ':private:' in scope['id'] or scope['id'].startswith('web:'):
        if 'always_reply' not in overrides:
            values['always_reply'] = profile['values'].get('private_always_reply', True)
        if values.get('always_reply') and 'plan_enabled' not in overrides and 'plan_enabled' not in values:
            values['plan_enabled'] = False
    return dict(scope, **values,
                profile_name=profile['name'], effective_profile_id=profile['id'], profile_legacy=False)


def install_profiles(app, store, settings):
    from fastapi import Body
    store.scope_resolver = lambda scope: resolve_scope(scope, settings)
    if store.created:
        with store._lock, store._conn:
            for raw in store._list('scopes'):
                raw.update(profile_id='', profile_overrides=[])
                store._write('scopes', raw, ())

    @app.get('/api/bot-profiles')
    async def profiles():
        config = copy.deepcopy(settings.get()['bot_profiles'])
        config['scopes'] = [{'id': s['id'], 'name': s['name'], 'profile_id': s.get('profile_id'),
                             'overrides': s.get('profile_overrides', [])} for s in store.list_scopes()]
        return config

    @app.put('/api/bot-profiles')
    async def save(payload: dict = Body(...)):
        validate_profiles(payload)
        ids = {p['id'] for p in payload['items']}
        for scope in store.list_scopes():
            if scope.get('profile_id') and scope['profile_id'] not in ids:
                raise ValueError('请先重新绑定使用该配置的会话，再删除配置')
        for profile in payload['items']:
            store.get_persona(profile['values'].get('persona_id', 'default'))
            model = profile['values'].get('model_id', '')
            if model and model != 'default' and not any(p['id'] == model for p in settings.get()['providers']):
                raise ValueError('配置使用的模型不存在')
            if any(p['id'] == model and p.get('model_type') == 'embedding' for p in settings.get()['providers']):
                raise ValueError('机器人配置的聊天模型不能使用嵌入模型')
        settings.update({'bot_profiles': payload})
        return payload

    @app.post('/api/bot-profiles/bind')
    async def bind(payload: dict = Body(...)):
        ids = payload.get('scope_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200 or any(not isinstance(s, str) for s in ids):
            raise ValueError('请选择1—200个会话')
        config = settings.get()['bot_profiles']
        profile_id = payload.get('profile_id', '')
        if profile_id and profile_id not in {p['id'] for p in config['items']}: raise ValueError('配置不存在')
        known = {s['id'] for s in store.list_scopes()}
        if set(ids) - known: raise ValueError('所选会话不存在')
        # Validate the complete selection before writing, then commit together.
        with store._lock, store._conn:
            for key in dict.fromkeys(ids):
                raw = store._get('scopes', key)
                raw.update(profile_id=profile_id, profile_overrides=[])
                store._write('scopes', raw, ())
        return {'ok': True, 'count': len(set(ids))}
