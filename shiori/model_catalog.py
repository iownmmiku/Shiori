"""One connection source can expose many independently routed model entries.

Legacy inline models retain their IDs, secrets and routing. Their connection
cards are read-only, so viewing the catalog never rewrites old configuration.
"""
from __future__ import annotations

import hashlib
import secrets
from contextlib import nullcontext

from .settings import PROVIDER_DEFAULTS, Settings, resolve_model


PURPOSES = ('chat', 'planner', 'memory', 'gate', 'summary', 'distillation', 'embedding')
_SOURCE_FIELDS = ('name', 'base_url', 'api_key', 'enabled', 'clear_api_key')
_MODEL_FIELDS = ('name', 'model', 'enabled', 'vision', 'tools', 'temperature', 'top_p', 'input_price', 'output_price', 'timeout_seconds', 'model_type',
                 'stream_mode', 'first_token_timeout_seconds', 'total_timeout_seconds', 'timeout_fallback', 'max_tokens',
                 'reasoning_effort', 'custom_params')


class ModelCatalog:
    def __init__(self, settings, provider, store=None):
        self.settings, self.provider, self.store = settings, provider, store
        self._lock = getattr(settings, '_lock', nullcontext())

    @staticmethod
    def _legacy_id(model_id):
        return 'legacy_default' if model_id == 'default' else 'legacy_' + model_id

    @staticmethod
    def _source_public(source, *, legacy=False):
        return {'id': source['id'], 'name': source.get('name') or source['id'],
                'base_url': source.get('base_url', ''), 'api_key_set': bool(source.get('api_key')),
                'enabled': bool(source.get('enabled', True)), 'legacy': legacy, 'editable': not legacy}

    def _source(self, source_id, config=None, *, editable=False):
        config = config or self.settings.get()
        source = next((item for item in config.get('provider_sources', []) if item['id'] == source_id), None)
        if source is not None:
            return dict(source)
        models = [dict(config['model'], id='default', name='原默认模型'), *config.get('providers', [])]
        legacy = next((item for item in models if not item.get('source_id') and self._legacy_id(item['id']) == source_id), None)
        if legacy is not None:
            if editable:
                raise ValueError('旧版连接卡只读；可在原模型设置中编辑，或新建供应商后添加模型')
            return dict(legacy, id=source_id, legacy=True)
        raise KeyError(source_id)

    def _model_public(self, item, sources):
        model_id = item['id']
        source_id = item.get('source_id') or self._legacy_id(model_id)
        source = sources.get(source_id, {})
        return {key: item.get(key, PROVIDER_DEFAULTS.get(key)) for key in ('name', *_MODEL_FIELDS[1:])} | {
            'id': model_id, 'name': item.get('name') or item.get('model') or '默认模型',
            'provider_id': source_id, 'source_id': item.get('source_id', ''),
            'provider_name': source.get('name') or source_id,
            'enabled': bool(item.get('enabled', True)),
            'effective_enabled': bool(item.get('enabled', True) and source.get('enabled', True)),
            'builtin': model_id == 'default'}

    def snapshot(self):
        config = self.settings.get()
        providers = [self._source_public(item) for item in config.get('provider_sources', [])]
        all_models = [dict(config['model'], id='default', name='原默认模型', enabled=True), *config.get('providers', [])]
        for model in all_models:
            if not model.get('source_id'):
                source = dict(model, id=self._legacy_id(model['id']))
                providers.append(self._source_public(source, legacy=True))
        by_source = {item['id']: item for item in providers}
        models = [self._model_public(item, by_source) for item in all_models]
        by_model = {item['id']: item for item in models}
        routes = {}
        for purpose in PURPOSES:
            resolved = resolve_model(config, purpose)
            model_id = resolved.get('id', 'default')
            entry = by_model.get(model_id, {})
            routes[purpose] = {'model_id': model_id, 'provider_id': entry.get('provider_id', ''),
                               'provider_name': entry.get('provider_name', ''),
                               'model': resolved.get('model') or entry.get('model', ''), 'name': entry.get('name', ''),
                               'enabled': bool(resolved.get('enabled', True)),
                               'base_url': resolved.get('base_url', '')}
        embedding = routes['embedding']
        return {'providers': providers, 'models': models, 'routing': config.get('routing', {}), 'routes': routes,
                'embedding': {'enabled': config['memory']['semantic_enabled'], 'model_id': config['routing']['embedding'],
                              'model': embedding['model'], 'provider_name': embedding['provider_name'],
                              'available': bool(embedding['enabled'] and embedding['model'] and embedding['base_url']),
                              'legacy': not bool(config['routing']['embedding']), 'top_k': config['memory']['top_k']}}

    def save_source(self, payload, source_id=None):
        if not isinstance(payload, dict):
            raise ValueError('供应商配置须为对象')
        with self._lock:
            config = self.settings.get()
            existing = self._source(source_id, config, editable=True) if source_id else {}
            source_id = source_id or 'source_' + secrets.token_hex(8)
            source = dict(existing, id=source_id)
            source.update({key: value for key, value in payload.items() if key in _SOURCE_FIELDS})
            source.setdefault('name', '新供应商')
            source.setdefault('base_url', '')
            source.setdefault('api_key', '')
            source.setdefault('enabled', True)
            if not isinstance(source['base_url'], str) or not isinstance(source['api_key'], str):
                raise ValueError('API 地址和密钥须为文本')
            if not source['base_url']:
                raise ValueError('请填写供应商 API 地址')
            sources = [source if item['id'] == source_id else item for item in config.get('provider_sources', [])]
            if not existing:
                sources.append(source)
            self.settings.update({'provider_sources': sources})
            saved = self._source(source_id)
            return self._source_public(saved)

    def delete_source(self, source_id):
        with self._lock:
            config = self.settings.get()
            self._source(source_id, config, editable=True)
            if any(item.get('source_id') == source_id for item in config['providers']):
                raise ValueError('请先删除该供应商下的模型；正在使用的模型须先调整用途或会话选择')
            self.settings.update({'provider_sources': [item for item in config['provider_sources'] if item['id'] != source_id]})
            return {'ok': True}

    async def discover(self, payload, source_id=None):
        if not isinstance(payload, dict):
            raise ValueError('读取模型的配置须为对象')
        source = self._source(source_id) if source_id else {'base_url': '', 'api_key': ''}
        for key in ('base_url', 'api_key'):
            if key in payload and payload[key] not in ('', None):
                source[key] = payload[key]
        if payload.get('clear_api_key'):
            source['api_key'] = ''
        if not isinstance(source.get('base_url'), str) or not isinstance(source.get('api_key'), str):
            raise ValueError('API 地址和密钥须为文本')
        if not source.get('base_url'):
            raise ValueError('请先填写 API 地址，再读取模型列表；也可直接手动添加模型')
        Settings._validate_model_url(source['base_url'])
        result = await self.provider.list_models(source)
        names = []
        for item in result:
            if isinstance(item, str) and item.strip() and len(item.strip()) <= 160 and item.strip() not in names:
                names.append(item.strip())
            if len(names) >= 500:
                break
        return {'models': names, 'provider_id': source_id or '', 'provider_name': source.get('name', '')}

    def add_models(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('models'), list) or not 1 <= len(payload['models']) <= 200:
            raise ValueError('每次请选择或手动填写 1 至 200 个模型')
        source_id = payload.get('provider_id')
        with self._lock:
            config = self.settings.get()
            self._source(source_id, config, editable=True)
            providers = list(config['providers'])
            known = {(item.get('source_id'), item['model']) for item in providers}
            added, skipped = [], []
            for selection in payload['models']:
                item = {'model': selection} if isinstance(selection, str) else selection
                if not isinstance(item, dict) or not isinstance(item.get('model'), str):
                    raise ValueError('模型名称须为文本')
                model = item['model'].strip()
                if not model or len(model) > 160 or any(ord(char) < 32 for char in model):
                    raise ValueError('模型名称须为 1 至 160 个可见字符')
                if (source_id, model) in known:
                    skipped.append(model)
                    continue
                base_id = 'model_' + hashlib.sha256((source_id + '\0' + model).encode()).hexdigest()[:24]
                model_id = base_id
                # An existing model may have changed its actual upstream name.
                # Keep its stable ID and allocate a separate ID for the old name.
                occupied = {candidate['id'] for candidate in providers}
                suffix = 0
                while model_id in occupied:
                    suffix += 1
                    model_id = base_id + '_' + str(suffix)
                entry = dict(PROVIDER_DEFAULTS, **{key: value for key, value in item.items() if key in _MODEL_FIELDS})
                if 'model_type' in payload and 'model_type' not in item:
                    entry['model_type'] = payload['model_type']
                entry.update(id=model_id, model=model, source_id=source_id, base_url='', api_key='')
                entry['name'] = item.get('name') or model
                providers.append(entry)
                known.add((source_id, model))
                added.append(model_id)
            self.settings.update({'providers': providers})
            snapshot = self.snapshot()
            return dict(snapshot, added=[item for item in snapshot['models'] if item['id'] in added], skipped=skipped)

    def update_model(self, model_id, payload):
        if not isinstance(payload, dict):
            raise ValueError('模型配置须为对象')
        payload = dict(payload)
        if 'model' in payload:
            model = payload['model']
            if not isinstance(model, str) or not model.strip() or len(model.strip()) > 160 or any(ord(char) < 32 for char in model):
                raise ValueError('模型名称须为 1 至 160 个可见字符')
            payload['model'] = model.strip()
        with self._lock:
            config = self.settings.get()
            if model_id == 'default':
                if payload.get('enabled') is False:
                    raise ValueError('内置默认模型不能停用；可为用途选择其他模型')
                self.settings.update({'model': {key: value for key, value in payload.items() if key in _MODEL_FIELDS and key not in ('name', 'enabled', 'tools')}})
            else:
                if not any(item['id'] == model_id for item in config['providers']):
                    raise KeyError(model_id)
                if payload.get('model_type') == 'embedding' and self.store and any(scope.get('model_id') == model_id for scope in self.store.list_scopes()):
                    raise ValueError('该模型仍被会话用作聊天模型，请先更换会话模型')
                providers = [dict(item, **{key: value for key, value in payload.items() if key in _MODEL_FIELDS})
                             if item['id'] == model_id else item for item in config['providers']]
                modified = next(item for item in providers if item['id'] == model_id)
                if any(item['id'] != model_id and item.get('source_id') == modified.get('source_id') and item['model'] == modified['model']
                       for item in providers if modified.get('source_id')):
                    raise ValueError('该供应商已添加同名模型')
                self.settings.update({'providers': providers})
            return next(item for item in self.snapshot()['models'] if item['id'] == model_id)

    def delete_model(self, model_id):
        if model_id == 'default':
            raise ValueError('内置默认模型不能删除；可为用途选择其他模型')
        with self._lock:
            config = self.settings.get()
            if not any(item['id'] == model_id for item in config['providers']):
                raise KeyError(model_id)
            used = [purpose for purpose in PURPOSES if config['routing'].get(purpose) == model_id]
            if model_id in config['routing'].get('fallback', []):
                used.append('fallback')
            if self.store and any(scope.get('model_id') == model_id for scope in self.store.list_scopes()):
                used.append('会话')
            used.extend('机器人配置：' + profile['name'] for profile in config['bot_profiles']['items']
                        if profile['values'].get('model_id') == model_id)
            if used:
                raise ValueError('该模型仍用于 ' + '、'.join(used) + '，请先更换选择再删除')
            self.settings.update({'providers': [item for item in config['providers'] if item['id'] != model_id]})
            return {'ok': True}

    def update_routing(self, payload):
        if not isinstance(payload, dict) or any(key not in (*PURPOSES, 'fallback') for key in payload):
            raise ValueError('用途须为 chat/planner/memory/gate/summary/distillation/embedding/fallback')
        with self._lock:
            config = self.settings.get()
            routing = dict(config['routing'], **payload)
            self.settings.update({'routing': routing})
            return self.snapshot()

    def configure_embedding(self, payload):
        if not isinstance(payload, dict) or type(payload.get('enabled')) is not bool:
            raise ValueError('请明确选择是否启用语义检索')
        with self._lock:
            patch = {'memory': {'semantic_enabled': payload['enabled']}}
            if 'model_id' in payload:
                patch['routing'] = {'embedding': payload['model_id']}
            if payload['enabled']:
                config = self.settings.get()
                model_id = payload.get('model_id', config['routing']['embedding'])
                if not isinstance(model_id, str):
                    raise ValueError('请选择已保存的嵌入模型')
                selected = resolve_model(config, 'embedding', model_id=model_id or 'default')
                if not selected.get('enabled', True) or not selected.get('model') or not selected.get('base_url'):
                    raise ValueError('所选嵌入模型不可用，请先启用模型与供应商')
            self.settings.update(patch)
            return self.snapshot()

    async def test_embedding(self, model_id, *, activate=False):
        if type(activate) is not bool:
            raise ValueError('启用选项须为布尔值')
        if not isinstance(model_id, str):
            raise ValueError('请选择已保存的嵌入模型')
        config = self.settings.get()
        if model_id and not any(item['id'] == model_id for item in config['providers']):
            raise ValueError('嵌入模型不存在，请刷新列表')
        cfg = resolve_model(config, 'embedding', model_id=model_id or 'default')
        if cfg.get('model_type') == 'chat':
            raise ValueError('该配置是对话模型，请添加嵌入模型配置')
        if not cfg.get('enabled', True) or not cfg.get('base_url') or not cfg.get('model'):
            raise ValueError('嵌入模型或供应商尚未配置完成或已停用')
        import time
        started = time.monotonic()
        vectors = await self.provider.embed(['Shiori 语义检索连接测试'], config=cfg)
        if activate:
            with self._lock:
                current = resolve_model(self.settings.get(), 'embedding', model_id=model_id or 'default')
                if not self.provider._same_model(cfg, current):
                    raise ValueError('测试期间模型配置已改变，请重新测试；检索设置未修改')
                self.configure_embedding({'model_id': model_id, 'enabled': True})
        return {'ok': True, 'dimension': len(vectors[0]), 'model': cfg['model'],
                'elapsed_ms': round((time.monotonic() - started) * 1000), 'activated': activate}
