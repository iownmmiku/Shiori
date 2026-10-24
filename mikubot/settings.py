"""Local configuration; secrets are DPAPI protected on Windows."""
from __future__ import annotations

import base64
import copy
import ctypes
import json
import hashlib
import ipaddress
import math
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit
from .profiles import DEFAULT_PROFILES
from .timezones import config_zone


DEFAULTS = {
    'bot_profiles': DEFAULT_PROFILES,
    'model': {'base_url': '', 'model': '', 'api_key': '', 'vision': False,
              'temperature': 0.7, 'top_p': 1.0, 'input_price': 0.0, 'output_price': 0.0, 'timeout_seconds':90,
              'stream_mode':'auto', 'first_token_timeout_seconds':90, 'total_timeout_seconds':300,
              'timeout_fallback':False, 'max_tokens':0, 'reasoning_effort':'', 'custom_params':{}},
    'onebot': {'enabled': False, 'adapter': 'napcat', 'ws_url': '', 'access_token': '',
               'owner_ids': [], 'allowed_users': [], 'allowed_groups': [], 'chat_policy': 'blacklist',
               'blocked_users': [], 'blocked_groups': [], 'max_message_chars': 0},
    'qq_official': {'enabled': False, 'app_id': '', 'app_secret': '', 'allowed_groups': [], 'allowed_users': [],
                    'chat_policy': 'blacklist', 'blocked_users': [], 'blocked_groups': [], 'max_message_chars': 0},
    'budget': {'daily_tokens': 100000, 'daily_cost': 0.0},
    'time': {'timezone': 'Asia/Shanghai', 'awareness_enabled': True},
    'memory': {'semantic_enabled': False, 'embedding_model': '', 'embedding_price': 0.0,
               'top_k': 8, 'auto_review': True, 'organization_enabled': True,
               'term_cache_mb':64, 'read_concurrency':2, 'read_queue_limit':32,
               'semantic_timeout_seconds':8, 'background_indexing':True,
               'review_mode':'automatic','extraction_messages':6,'extraction_idle_seconds':180,'daily_extractions':60},
    'planner': {'min_interval_ms':1200, 'daily_calls':300, 'daily_tokens':12000, 'max_age_ms':15000,
                'group_batch_ms':1500,'group_batch_messages':4},
    'stickers': {'enabled':True,'collect_enabled':True,'collect_probability':0.1,'send_enabled':True,
                 'send_probability':0.1,'model_id':'','min_confidence':0.85,'daily_recognitions':30,'daily_model_calls':60,'max_items':1000},
    'scheduler': {'model_concurrency':1,'model_queue_limit':32,'background_queue_limit':16,'scope_queue_limit':8,
                  'global_model_concurrency':2,'global_model_queue_limit':32,
                  'request_concurrency':8,'request_queue_limit':16,'request_max_mb':32,
                  'parsing_concurrency':1,'parsing_queue_limit':4},
    'access': {'administrators': []},
    'webui': {'remote_enabled': False, 'host': '0.0.0.0', 'port': 8765, 'allowed_hosts': [], 'public_url': '', 'require_https':True, 'trusted_proxy_ips':[]},
    'computer': {'enabled':False, 'runtime':'docker', 'workspace':'', 'max_steps':12, 'timeout_seconds':60, 'max_output_chars':16000, 'approval_mode':'automatic', 'allow_external_tools':False},
    'providers': [],
    'provider_sources': [],
    'mcp_servers': [],
    'identity': {'name':'初音未来','aliases':['初音','miku'], 'respond_to_at':True,'respond_to_reply':True,'respond_to_name':True,
                 'planner_enabled':True,'proactive_enabled':True},
    'output': {'enabled':True,'mode':'model','target_chars':160,'max_chars':320,'interval_ms':600,'max_parts':8,'platform_max_chars':1800,
               'reply_max_chars':0,'message_max_chars':0,'remove_parentheses':False},
    'exploration': {'enabled':False,'interval_hours':24,'daily_searches':6,'max_results':4,'valid_days':90},
    'routing': {'chat': '', 'planner':'', 'memory': '', 'gate': '', 'summary': '', 'distillation': '', 'embedding': '', 'fallback': []},
    'context': {'enabled': False, 'trigger_messages': 60, 'max_chars': 18000, 'keep_recent': 24,
                'max_input_chars':80000, 'max_history_messages':400, 'max_images':3},
    'paused': False,
    'updates': {'allow_remote':False},
    'language': {'learning_enabled':True,'group_isolation':False,'review_mode':'automatic'},
}

PROVIDER_DEFAULTS = dict(DEFAULTS['model'], id='', name='', enabled=True, tools=True, source_id='', model_type='')
SOURCE_DEFAULTS = {'id':'','name':'','base_url':'','api_key':'','enabled':True}

# Per-model output ceiling. Zero means "no extra ceiling": chat keeps its local
# default and task-specific limits are left untouched, which preserves configs
# written before this setting existed.
MAX_OUTPUT_TOKENS = 100000
# Fields that shape the request itself (output count, budget, stream and tool
# protocol) cannot be smuggled through generic provider parameters. Values that
# would change how many tokens are reserved or billed must be core settings.
CUSTOM_PARAM_RESERVED = frozenset({
    'model', 'messages', 'stream', 'stream_options', 'tools', 'tool_choice',
    'functions', 'function_call', 'parallel_tool_calls',
    'max_tokens', 'max_completion_tokens', 'n', 'best_of',
    'temperature', 'top_p', 'reasoning_effort',
})
CUSTOM_PARAM_MAX_COUNT = 50
CUSTOM_PARAM_MAX_STRING = 2000
CUSTOM_PARAM_MAX_JSON = 8000
CUSTOM_PARAM_MAX_ABS = (1 << 53) - 1


def validate_custom_params(custom_params) -> dict:
    """Validate provider-specific parameters and return a JSON-safe copy.

    The object is deliberately flat: arbitrary nested structures would make the
    request body unbounded and are not needed by current providers.
    """
    if not isinstance(custom_params, dict) or len(custom_params) > CUSTOM_PARAM_MAX_COUNT:
        raise ValueError('自定义参数须为不超过50项的对象')
    cleaned = {}
    for custom_key, custom_value in custom_params.items():
        if not isinstance(custom_key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', custom_key):
            raise ValueError('自定义参数键名无效')
        if custom_key in CUSTOM_PARAM_RESERVED:
            raise ValueError(f'自定义参数不能覆盖模型请求核心字段（{custom_key}）')
        if custom_value is None or isinstance(custom_value, bool):
            cleaned[custom_key] = custom_value
        elif isinstance(custom_value, int):
            if abs(custom_value) > CUSTOM_PARAM_MAX_ABS:
                raise ValueError('自定义参数整数超出安全范围')
            cleaned[custom_key] = custom_value
        elif isinstance(custom_value, float):
            if not math.isfinite(custom_value) or abs(custom_value) > CUSTOM_PARAM_MAX_ABS:
                raise ValueError('自定义参数数值须为有限数字')
            cleaned[custom_key] = custom_value
        elif isinstance(custom_value, str):
            if len(custom_value) > CUSTOM_PARAM_MAX_STRING:
                raise ValueError('自定义参数字符串过长')
            cleaned[custom_key] = custom_value
        else:
            raise ValueError('自定义参数值目前只支持字符串、数字、布尔值或空值')
    try:
        encoded = json.dumps(cleaned, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError('自定义参数无法序列化为 JSON') from exc
    if len(encoded) > CUSTOM_PARAM_MAX_JSON:
        raise ValueError('自定义参数整体过大，请精简后保存')
    return cleaned


def resolved_identity(config, scope=None):
    scope = scope or {}
    identity = dict(DEFAULTS['identity'],**config.get('identity',{}))
    def effective(key, global_key):
        value=scope.get(key,(scope.get('settings') or {}).get(key))
        return identity[global_key] if value is None else bool(value)
    aliases=scope.get('bot_aliases') or identity['aliases']
    name=str(scope.get('bot_name') or identity['name']).strip()
    return {'name':name,'aliases':list(dict.fromkeys([name,*aliases])),
            'trigger_at':effective('trigger_at','respond_to_at'),'trigger_reply':effective('trigger_reply','respond_to_reply'),
            'trigger_name':effective('trigger_name','respond_to_name'),'plan_enabled':effective('plan_enabled','planner_enabled'),
            'proactive_reply':effective('proactive_reply','proactive_enabled'),'always_reply':bool(scope.get('always_reply',False))}


def resolve_model(config, purpose='chat', *, model_id=None):
    """An empty task route keeps the original single-model configuration."""
    aliases = {'jargon_learning': 'memory', 'expression_learning': 'memory', 'memory_extract': 'memory', 'context_digest': 'summary','knowledge':'summary','sticker_select':'planner'}
    route = aliases.get(purpose, purpose)
    route = route if route in ('planner','memory', 'gate', 'summary', 'distillation', 'embedding') else 'chat'
    provider_id = config.get('routing', {}).get(route, '') if model_id is None else model_id
    if route == 'distillation' and model_id is None and not provider_id:
        return resolve_model(config, 'chat')
    if provider_id and provider_id != 'default':
        found = next((x for x in config.get('providers', []) if x.get('id') == provider_id), None)
        if found and found.get('enabled', True):
            if found.get('source_id'):
                source = next((x for x in config.get('provider_sources', []) if x['id'] == found['source_id']), None)
                if not source or not source.get('enabled', True):
                    return dict(found, base_url='', model='', enabled=False)
                return dict(found, base_url=source['base_url'], api_key=source['api_key'], source_name=source['name'])
            return dict(found)
        return {'id': provider_id, 'name': '停用或缺失的模型', 'base_url': '', 'model': '', 'enabled': False}
    result = dict(config['model'], id='default', name='默认模型', enabled=True, tools=True)
    if route == 'embedding':
        result.update(model=config.get('memory', {}).get('embedding_model', ''),
                      input_price=config.get('memory', {}).get('embedding_price', 0), output_price=0)
    return result


class _Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _protect(value: str) -> str:
    if not value:
        return ''
    if os.name != 'nt':
        return 'local:' + base64.b64encode(value.encode()).decode()
    raw = value.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    src = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise OSError('无法使用 Windows 保护密钥')
    try:
        return 'dpapi:' + base64.b64encode(ctypes.string_at(dest.data, dest.size)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(dest.data)


def _unprotect(value: str) -> str:
    if not value:
        return ''
    prefix, _, payload = value.partition(':')
    if prefix == 'local':
        return base64.b64decode(payload).decode()
    if prefix != 'dpapi' or os.name != 'nt':
        return ''
    raw = base64.b64decode(payload)
    buf = ctypes.create_string_buffer(raw)
    src = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise OSError('密钥不能在当前 Windows 账户解密，请重新填写')
    try:
        return ctypes.string_at(dest.data, dest.size).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(dest.data)


class Settings:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / 'settings.json'
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self.warning = ''
        self.recovery_path = None
        self._recovery_pending = False
        if self.path.exists():
            try:
                self._load_file()
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
                self._recover_file()
        token_path = self.data_dir / 'admin.token'
        if not token_path.exists():
            try:
                with token_path.open('x', encoding='utf-8') as token_file:
                    token_file.write(secrets.token_urlsafe(36))
            except FileExistsError:
                pass
            if os.name != 'nt':
                token_path.chmod(0o600)
        self.token = token_path.read_text('utf-8').strip()
        if len(self.token) < 32:
            raise ValueError('本地管理凭据无效，请保留数据并删除 admin.token 后重启')

    def _load_file(self):
        raw = json.loads(self.path.read_text('utf-8-sig'))
        if not isinstance(raw, dict):
            raise ValueError('配置须为对象')
        for section, default in DEFAULTS.items():
            if section in raw and isinstance(default, dict) and not isinstance(raw[section], dict):
                raise ValueError('配置段须为对象')
            if section in raw and isinstance(default, list) and not isinstance(raw[section], list):
                raise ValueError('配置列表格式无效')
        if 'paused' in raw and type(raw['paused']) is not bool:
            raise ValueError('暂停开关须为布尔值')
        for section in DEFAULTS:
            if isinstance(DEFAULTS[section], dict):
                self._data[section].update(raw.get(section, {}))
            elif section in raw:
                self._data[section] = raw[section]
        # Older owner_ids combined private-chat access and privileged identity.
        # Migrate once, then clear that obsolete authority source so revocation
        # cannot silently re-create an administrator on the next startup.
        if 'access' not in raw:
            legacy = self._data['onebot'].get('owner_ids') or []
            if isinstance(legacy, str):
                legacy = re.split(r'[,，;\s]+', legacy)
            self._data['access']['administrators'] = [
                {'platform':'onebot', 'account_id':str(value), 'instance_id':'', 'label':'旧版管理员'}
                for value in legacy if str(value).isdigit()]
            if 'allowed_users' not in raw.get('onebot', {}):
                self._data['onebot']['allowed_users'] = list(legacy)
        self._data['onebot']['owner_ids'] = []
        for section in ('onebot', 'qq_official'):
            if 'chat_policy' not in raw.get(section, {}):
                self._data[section]['chat_policy'] = 'whitelist'
        for section, key in [('model', 'api_key'), ('onebot', 'access_token'), ('qq_official', 'app_secret')]:
            try:
                self._data[section][key] = self._decode_secret(self._data[section].get(key, ''))
            except Exception as exc:
                raise ValueError('配置密钥无法读取') from exc
        providers = self._data.get('providers', [])
        sources = self._data.get('provider_sources', [])
        self._data['provider_sources'] = []
        for item in sources:
            try:
                source = dict(SOURCE_DEFAULTS, **{k:v for k,v in item.items() if k in SOURCE_DEFAULTS})
                source['api_key'] = self._decode_secret(source['api_key'])
                self._validate_model_url(source['base_url'])
                self._data['provider_sources'].append(source)
            except Exception:
                self.warning = '一个供应商连接无法读取，已忽略；请重新配置'
        self._data['providers'] = []
        for item in providers if isinstance(providers, list) else []:
            try:
                provider = dict(PROVIDER_DEFAULTS, **{k: v for k, v in item.items() if k in PROVIDER_DEFAULTS})
                provider['api_key'] = self._decode_secret(provider.get('api_key', ''))
                self._validate_model_url(provider['base_url'])
                self._data['providers'].append(provider)
            except Exception:
                self.warning = '一个模型配置无法读取，已停用；请重新填写'
        servers = self._data.get('mcp_servers', [])
        self._data['mcp_servers'] = []
        for item in servers if isinstance(servers, list) else []:
            try:
                server = {k: item.get(k) for k in ('id', 'name', 'url', 'enabled', 'allowed_tools')}
                server['token'] = self._decode_secret(item.get('token', ''))
                self._validate_mcp_url(server['url'])
                self._data['mcp_servers'].append(server)
            except Exception:
                self.warning = '一个 MCP 服务配置无法读取，已忽略；请重新配置'
        # Older or hand-edited files must never expose URL-embedded credentials.
        try:
            self._validate_ws_url(self._data['onebot']['ws_url'])
        except ValueError:
            self._data['onebot']['ws_url'] = ''
            self._data['onebot']['enabled'] = False
            self.warning = 'OneBot 地址含不支持的凭据或参数，已停用；请使用独立 Access Token 字段重新配置'
        try:
            self._validate_model_url(self._data['model']['base_url'])
        except ValueError:
            self._data['model']['base_url'] = ''
            self.warning = '模型地址含不支持的凭据或参数，已清空；请使用独立 API 密钥字段重新配置'
        self._data = self.update({}, dry_run=True)
        if self.warning:
            self._preserve_original()
            self._data['paused'] = True
            self._data['onebot']['enabled'] = False
            self._data['qq_official']['enabled'] = False
            self._data['exploration']['enabled'] = False
            for server in self._data['mcp_servers']:
                server['enabled'] = False
            self.warning += '；原配置已保留于 ' + self.recovery_path.name + '，程序已暂停，请重新配置或从备份恢复。'

    @staticmethod
    def _decode_secret(value):
        if not isinstance(value, str):
            raise ValueError('密钥格式无效')
        decoded = _unprotect(value)
        if value and not decoded:
            raise ValueError('密钥不能在当前账户读取')
        return decoded

    def _preserve_original(self):
        raw = self.path.read_bytes()
        # Deterministic content name avoids producing another copy on every
        # startup. The original stays untouched until an explicit user save.
        name = 'settings.corrupt-' + hashlib.sha256(raw).hexdigest()[:16] + '.json'
        candidate = self.data_dir / name
        if candidate.exists():
            if candidate.read_bytes() != raw:
                raise OSError('损坏配置备份冲突')
        else:
            with candidate.open('xb') as output:
                output.write(raw)
        self.recovery_path = candidate
        self._recovery_pending = False

    def _recover_file(self):
        self._data = copy.deepcopy(DEFAULTS)
        self._data['paused'] = True
        self._recovery_pending = True
        try:
            self._preserve_original()
            self.warning = '配置损坏或格式无效，已保留原件于 ' + self.recovery_path.name + '；程序以暂停默认配置启动。请重新配置或从备份恢复。'
        except OSError:
            self.warning = '配置无法读取；原 settings.json 保持不动，程序以暂停默认配置启动。请保留原件后重新配置或从备份恢复。'

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self) -> dict:
        data = self.get()
        data['model']['api_key_set'] = bool(data['model'].pop('api_key', ''))
        data['onebot']['access_token_set'] = bool(data['onebot'].pop('access_token', ''))
        data['qq_official']['app_secret_set'] = bool(data['qq_official'].pop('app_secret', ''))
        for item in data['providers']:
            item['api_key_set'] = bool(item.pop('api_key', ''))
        for item in data['provider_sources']:
            item['api_key_set'] = bool(item.pop('api_key', ''))
        for item in data['mcp_servers']:
            item['token_set'] = bool(item.pop('token', ''))
        return data

    @staticmethod
    def _validate_model_url(value):
        if not value:
            return
        url = urlsplit(str(value))
        if url.scheme not in ('http', 'https') or not url.hostname:
            raise ValueError('模型 API 地址须为有效的 http:// 或 https:// 地址')
        if url.username is not None or url.password is not None or url.query or url.fragment:
            raise ValueError('模型 API 地址不能包含账号、密码或查询参数；请使用独立 API 密钥字段')
        try:
            url.port
        except ValueError as exc:
            raise ValueError('模型 API 端口无效') from exc

    @staticmethod
    def _validate_ws_url(value):
        if not value:
            return
        url = urlsplit(str(value))
        if url.scheme not in ('ws', 'wss') or not url.hostname:
            raise ValueError('QQ 正向连接地址须为有效的 ws:// 或 wss:// 地址')
        if url.username is not None or url.password is not None or url.query or url.fragment:
            raise ValueError('QQ 连接地址不能包含账号、密码或查询参数；请使用独立 Access Token 字段')
        try:
            url.port
        except ValueError as exc:
            raise ValueError('QQ 连接端口无效') from exc

    @staticmethod
    def _validate_mcp_url(value):
        from .mcp_client import validate_mcp_url
        validate_mcp_url(value,resolve=False)

    def update(self, patch: dict, *, dry_run=False) -> dict:
        with self._lock:
            if not isinstance(patch, dict):
                raise ValueError('配置必须为对象')
            updated = self.get()
            # Compatibility for pre-0.8 local clients explicitly saving the old
            # owner field. New clients submit the two independent lists.
            legacy_patch = patch.get('onebot') or {}
            if isinstance(legacy_patch, dict) and 'owner_ids' in legacy_patch and 'allowed_users' not in legacy_patch and 'access' not in patch:
                legacy_ids = legacy_patch['owner_ids']
                if isinstance(legacy_ids, str):
                    legacy_ids = re.split(r'[,，;\s]+', legacy_ids)
                if isinstance(legacy_ids, list):
                    patch = copy.deepcopy(patch)
                    patch['onebot']['allowed_users'] = legacy_ids
                    other = [x for x in updated['access']['administrators'] if x.get('platform') != 'onebot' or x.get('label') != '旧版管理员']
                    patch['access'] = {'administrators':other + [
                        {'platform':'onebot','account_id':str(value),'instance_id':'','label':'旧版管理员'}
                        for value in legacy_ids if str(value).isdigit()]}
            for section in DEFAULTS:
                if section not in patch:
                    continue
                if section == 'mcp_servers':
                    items = patch[section]
                    if not isinstance(items, list) or len(items) > 8:
                        raise ValueError('MCP 服务最多 8 个')
                    previous = {x['id']: x for x in updated['mcp_servers']}
                    servers, seen = [], set()
                    for item in items:
                        if not isinstance(item, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', str(item.get('id', ''))) or item['id'] in seen:
                            raise ValueError('MCP 服务 ID 须唯一，且仅包含字母数字、下划线或短横线')
                        seen.add(item['id'])
                        server = {k: item.get(k, default) for k, default in {'id':'', 'name':'', 'url':'', 'token':'', 'enabled':False, 'allowed_tools':[]}.items()}
                        server['url'] = str(server['url']).strip()
                        self._validate_mcp_url(server['url'])
                        server['name'] = str(server['name']).strip()[:80] or server['id']
                        if type(server['enabled']) is not bool or not isinstance(server['allowed_tools'], list) or len(server['allowed_tools']) > 50 or any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in server['allowed_tools']):
                            raise ValueError('MCP 服务开关或工具允许列表格式错误')
                        server['allowed_tools'] = list(dict.fromkeys(server['allowed_tools']))
                        server['token'] = str(server['token'] or previous.get(server['id'], {}).get('token', ''))
                        if item.get('clear_token'):
                            server['token'] = ''
                        servers.append(server)
                    updated['mcp_servers'] = servers
                elif section == 'provider_sources':
                    if not isinstance(patch[section],list) or len(patch[section]) > 50:
                        raise ValueError('供应商最多配置50个')
                    previous = {x['id']:x for x in updated['provider_sources']}
                    sources, seen = [], set()
                    for item in patch[section]:
                        if not isinstance(item,dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',str(item.get('id',''))) or item['id'] in seen or item['id'].startswith('legacy_'):
                            raise ValueError('供应商ID须唯一且仅含字母数字、下划线、短横线，不能以 legacy_ 开头')
                        seen.add(item['id'])
                        source = dict(SOURCE_DEFAULTS, **{k:v for k,v in item.items() if k in SOURCE_DEFAULTS})
                        if type(source['enabled']) is not bool or not isinstance(source['name'],str) or not isinstance(source['base_url'],str) or not isinstance(source['api_key'],str):
                            raise ValueError('供应商名称、地址、密钥或开关格式错误')
                        source['name'] = source['name'].strip()[:80] or source['id']
                        source['base_url'] = source['base_url'].strip().rstrip('/')
                        self._validate_model_url(source['base_url'])
                        source['api_key'] = source['api_key'] or previous.get(source['id'],{}).get('api_key','')
                        if item.get('clear_api_key'): source['api_key'] = ''
                        sources.append(source)
                    updated['provider_sources'] = sources
                elif section == 'providers':
                    items = patch[section]
                    if not isinstance(items, list) or len(items) > 200:
                        raise ValueError('模型池最多配置200个模型')
                    previous = {x['id']: x for x in updated['providers']}
                    providers, seen = [], set()
                    for item in items:
                        if not isinstance(item, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', str(item.get('id', ''))) or item['id'] == 'default' or item['id'] in seen:
                            raise ValueError('模型 ID 须为唯一的字母、数字、下划线或短横线，不能使用 default')
                        seen.add(item['id'])
                        provider = dict(PROVIDER_DEFAULTS, **{k: v for k, v in item.items() if k in PROVIDER_DEFAULTS})
                        if not item.get('api_key'):
                            provider['api_key'] = previous.get(item['id'], {}).get('api_key', '')
                        if item.get('clear_api_key'):
                            provider['api_key'] = ''
                        providers.append(provider)
                    updated['providers'] = providers
                elif isinstance(DEFAULTS[section], dict):
                    if not isinstance(patch[section], dict):
                        raise ValueError('配置段必须为对象')
                    for key, value in patch[section].items():
                        if key not in DEFAULTS[section]:
                            continue
                        if key in ('api_key', 'access_token', 'app_secret') and not value:
                            continue
                        updated[section][key] = value
                    if patch[section].get('clear_api_key'):
                        updated[section]['api_key'] = ''
                    if patch[section].get('clear_access_token'):
                        updated[section]['access_token'] = ''
                    if patch[section].get('clear_app_secret'):
                        updated[section]['app_secret'] = ''
                else:
                    updated[section] = bool(patch[section])
            model = updated['model']
            source_ids = {x['id'] for x in updated['provider_sources']}
            for item in updated['providers']:
                if not isinstance(item['source_id'],str) or (item['source_id'] and item['source_id'] not in source_ids):
                    raise ValueError('模型引用的供应商不存在')
            for candidate in [model, *updated['providers']]:
                if candidate.get('model_type', '') not in ('', 'chat', 'embedding'):
                    raise ValueError('模型类型须为 chat 或 embedding；旧配置可留空')
                candidate['base_url'] = str(candidate['base_url']).strip().rstrip('/')
                self._validate_model_url(candidate['base_url'])
                candidate['model'] = str(candidate['model']).strip()[:160]
                candidate['api_key'] = str(candidate['api_key'])
                if type(candidate['timeout_seconds']) is not int or not 5 <= candidate['timeout_seconds'] <= 600:
                    raise ValueError('模型等待超时须为5—600秒的整数')
                for key, low, high in (('first_token_timeout_seconds',5,600),('total_timeout_seconds',5,1800)):
                    if type(candidate[key]) is not int or not low <= candidate[key] <= high:
                        raise ValueError(f'{key}须为{low}—{high}秒的整数')
                if candidate['stream_mode'] not in ('auto','on','off'):
                    raise ValueError('流式模式须为auto/on/off')
                if type(candidate['timeout_fallback']) is not bool:
                    raise ValueError('超时备用模型开关须为布尔值')
                for key, low, high in (('temperature', 0, 2), ('top_p', 0, 1), ('input_price', 0, 1000000), ('output_price', 0, 1000000)):
                    try:
                        candidate[key] = float(candidate[key])
                    except (ValueError, TypeError) as exc:
                        raise ValueError('模型温度和价格须为数字') from exc
                    if not math.isfinite(candidate[key]) or not low <= candidate[key] <= high:
                        raise ValueError('模型温度或价格超出支持范围')
                if type(candidate.get('max_tokens')) is not int or not 0 <= candidate['max_tokens'] <= MAX_OUTPUT_TOKENS:
                    raise ValueError('最大 Tokens 须为0—100000的整数；0 表示不额外限制')
                if not isinstance(candidate.get('reasoning_effort'), str) or len(candidate['reasoning_effort']) > 40:
                    raise ValueError('思考强度须为不超过40个字符的文本')
                candidate['custom_params'] = validate_custom_params(candidate.get('custom_params', {}))
                for key in ('vision', 'tools', 'enabled'):
                    if key in candidate and type(candidate[key]) is not bool:
                        raise ValueError('模型能力开关须为布尔值')
                if 'name' in candidate:
                    candidate['name'] = str(candidate['name']).strip()[:80] or candidate['id']
            config_zone(updated)
            memory_policy=updated['memory']
            if memory_policy['review_mode'] not in ('automatic','manual'):raise ValueError('记忆审核模式须为 automatic 或 manual')
            language=updated['language']
            if type(language['learning_enabled']) is not bool or type(language['group_isolation']) is not bool:raise ValueError('黑话学习与隔离开关须为布尔值')
            if language['review_mode'] not in ('automatic','manual'):raise ValueError('黑话审核模式须为 automatic 或 manual')
            for key,low,high in (('extraction_messages',2,50),('extraction_idle_seconds',30,3600),('daily_extractions',0,1000)):
                if type(memory_policy[key]) is not int or not low<=memory_policy[key]<=high:raise ValueError('自动记忆整理参数无效')
            if type(updated['updates']['allow_remote']) is not bool:
                raise ValueError('远程更新开关须为布尔值')
            if type(updated['time']['awareness_enabled']) is not bool:
                raise ValueError('感知现实时间开关须为布尔值')
            routing = updated['routing']
            ids = {x['id'] for x in updated['providers']}
            for key in ('chat', 'planner', 'memory', 'gate', 'summary', 'distillation', 'embedding'):
                if not isinstance(routing[key], str) or routing[key] and routing[key] not in ids:
                    raise ValueError('任务路由引用了不存在的模型；删除模型前请调整路由')
                selected = next((item for item in updated['providers'] if item['id'] == routing[key]), {})
                if selected.get('model_type') == ('chat' if key == 'embedding' else 'embedding'):
                    raise ValueError('嵌入模型仅用于语义检索，对话模型不能作为嵌入模型')
            fallback = routing['fallback']
            if not isinstance(fallback, list) or len(fallback) > 3 or any(x not in ids for x in fallback) or len(set(fallback)) != len(fallback):
                raise ValueError('备用模型须为模型池中最多 3 个不同的模型')
            if any(item.get('model_type') == 'embedding' and item['id'] in fallback for item in updated['providers']):
                raise ValueError('嵌入模型不能作为聊天备用模型')
            context = updated['context']
            for key, low, high in (('max_input_chars',2000,200000),('max_history_messages',1,400),('max_images',0,3)):
                if type(context[key]) is not int or not low <= context[key] <= high:
                    raise ValueError(f'上下文参数{key}须为{low}—{high}的整数')
            if type(context['enabled']) is not bool:
                raise ValueError('长对话摘要开关须为布尔值')
            for key, low, high in (('trigger_messages', 20, 400), ('max_chars', 4000, 60000), ('keep_recent', 6, 60)):
                try:
                    value = int(context[key])
                    if isinstance(context[key], bool) or not low <= value <= high:
                        raise ValueError()
                except (ValueError, TypeError) as exc:
                    raise ValueError(f'长对话配置 {key} 超出范围 {low}—{high}') from exc
                context[key] = value
            if context['keep_recent'] >= context['trigger_messages']:
                raise ValueError('保留原文条数须少于摘要触发条数')
            identity=updated['identity']
            identity['name']=str(identity['name']).strip()
            if not 1<=len(identity['name'])<=40 or not isinstance(identity['aliases'],list) or len(identity['aliases'])>12 or any(not isinstance(x,str) or not 1<=len(x.strip())<=30 for x in identity['aliases']):
                raise ValueError('机器人名称须1—40字，别名最多12个、每个1—30字')
            identity['aliases']=list(dict.fromkeys(x.strip() for x in identity['aliases']))
            for key in ('respond_to_at','respond_to_reply','respond_to_name','planner_enabled','proactive_enabled'):
                if type(identity[key]) is not bool: raise ValueError('身份与触发开关须为布尔值')
            for key, minimum, maximum in (('min_interval_ms',0,60000),('daily_calls',0,100000),('daily_tokens',0,10000000),('max_age_ms',1000,120000),('group_batch_ms',0,10000),('group_batch_messages',2,20)):
                value=updated['planner'][key]
                if type(value) is not int or not minimum <= value <= maximum:
                    raise ValueError(f'Planner {key}须为{minimum}—{maximum}的整数')
            stickers=updated['stickers']
            for key in ('enabled','collect_enabled','send_enabled'):
                if type(stickers[key]) is not bool: raise ValueError('表情包开关须为布尔值')
            for key in ('collect_probability','send_probability','min_confidence'):
                if isinstance(stickers[key],bool) or not isinstance(stickers[key],(int,float)) or not math.isfinite(stickers[key]) or not 0<=stickers[key]<=1:
                    raise ValueError('表情包概率须在0到1之间')
            for key,maximum in (('daily_recognitions',1000),('daily_model_calls',10000),('max_items',5000)):
                if type(stickers[key]) is not int or not (1 if key=='max_items' else 0)<=stickers[key]<=maximum:
                    raise ValueError('表情包数量配置无效')
            if not isinstance(stickers['model_id'],str) or stickers['model_id'] and stickers['model_id']!='default' and stickers['model_id'] not in ids:
                raise ValueError('表情包识图模型不存在')
            for section in ('onebot','qq_official'):
                value=updated[section]['max_message_chars']
                if type(value) is not int or value != 0 and not 40 <= value <= 20000:
                    raise ValueError('通道单条长度须为0（继承）或40—20000的整数')
            for section, ranges in (('output',{'target_chars':(20,1000),'max_chars':(40,4000),'interval_ms':(0,5000),'max_parts':(1,20),'platform_max_chars':(40,20000)}),
                                    ('exploration',{'interval_hours':(1,720),'daily_searches':(0,100),'max_results':(1,8),'valid_days':(1,365)})):
                if type(updated[section]['enabled']) is not bool: raise ValueError('功能开关须为布尔值')
                for key,(minimum,maximum) in ranges.items():
                    value=updated[section][key]
                    if isinstance(value,bool): raise ValueError('长度或频率须为数字')
                    try: value=int(value)
                    except (TypeError,ValueError) as exc: raise ValueError('长度或频率格式错误') from exc
                    if not minimum<=value<=maximum: raise ValueError(f'{key}超出范围{minimum}—{maximum}')
                    updated[section][key]=value
            if updated['output'].get('mode') not in ('model','length'): raise ValueError('输出分段模式须为model或length')
            if updated['output']['target_chars']>updated['output']['max_chars']: raise ValueError('分段目标长度不能超过最大长度')
            for key, maximum in (('reply_max_chars',20000),('message_max_chars',4000)):
                value = updated['output'][key]
                if type(value) is not int or not 0 <= value <= maximum:
                    raise ValueError(f'输出{key}须为0—{maximum}的整数；0表示不限制')
            if type(updated['output']['remove_parentheses']) is not bool:
                raise ValueError('删除括号及内容须为布尔值')
            memory = updated['memory']
            for key, low, high in (('term_cache_mb',0,256),('read_concurrency',1,8),('read_queue_limit',1,200),('semantic_timeout_seconds',1,120)):
                if type(memory[key]) is not int or not low <= memory[key] <= high:
                    raise ValueError(f'记忆参数{key}须为{low}—{high}的整数')
            for key in ('semantic_enabled', 'auto_review', 'organization_enabled', 'background_indexing'):
                if type(memory[key]) is not bool:
                    raise ValueError('记忆功能开关须为布尔值')
            memory['embedding_model'] = str(memory['embedding_model']).strip()[:160]
            try:
                memory['embedding_price'] = float(memory['embedding_price'])
                memory['top_k'] = int(memory['top_k'])
            except (ValueError, TypeError) as exc:
                raise ValueError('记忆检索数量或向量价格格式不正确') from exc
            if not math.isfinite(memory['embedding_price']) or memory['embedding_price'] < 0 or not 1 <= memory['top_k'] <= 20:
                raise ValueError('向量价格必须非负，检索数量须在 1—20 之间')
            if memory['semantic_enabled'] and not resolve_model(updated, 'embedding').get('model'):
                raise ValueError('开启语义检索前请填写独立的 Embedding 模型名')
            for key in ('daily_tokens', 'daily_cost'):
                updated['budget'][key] = max(0, float(updated['budget'][key]))
                if not math.isfinite(updated['budget'][key]):
                    raise ValueError('预算须为有限数字')
            price_models = [resolve_model(updated, key) for key in ('chat', 'planner', 'memory', 'gate', 'summary', 'distillation')]
            if memory['semantic_enabled']:
                price_models.append(resolve_model(updated, 'embedding'))
            price_models += [x for x in updated['providers'] if x['id'] in routing['fallback'] and x['enabled']]
            if updated['budget']['daily_cost'] and any(x.get('base_url') and x.get('model') and not (x.get('input_price') or x.get('output_price')) for x in price_models):
                raise ValueError('启用费用预算前请填写模型单价；未知价格时可使用 Token 预算')
            qq = updated['onebot']
            for section in ('onebot', 'qq_official'):
                if updated[section].get('chat_policy') not in ('blacklist', 'whitelist'):
                    raise ValueError('聊天接收规则须为黑名单或白名单')
            for key in ('owner_ids', 'allowed_users', 'allowed_groups', 'blocked_users', 'blocked_groups'):
                vals = qq[key]
                if isinstance(vals, str):
                    vals = vals.replace('，', ',').replace('\n', ',').split(',')
                if not isinstance(vals, list):
                    raise ValueError('QQ 号码列表格式不正确')
                qq[key] = list(dict.fromkeys(str(x).strip() for x in vals if str(x).strip()))
                if any(not x.isdigit() for x in qq[key]):
                    raise ValueError('QQ 号与群号只能使用数字')
            qq['enabled'] = bool(qq['enabled'])
            if qq.get('adapter') not in ('napcat', 'snowluma', 'official'):
                raise ValueError('未知 QQ 接入方式')
            qq['ws_url'] = str(qq['ws_url']).strip()
            self._validate_ws_url(qq['ws_url'])
            qq['owner_ids'] = []
            if qq['enabled'] and qq['adapter'] != 'official' and len(qq['access_token']) < 12:
                raise ValueError('启用 QQ 前，请填写至少 12 位连接密钥')
            from .access import normalize_administrators
            updated['access']['administrators'] = normalize_administrators(updated['access']['administrators'])
            computer = updated['computer']
            if computer['approval_mode'] not in ('automatic','per_action'):
                raise ValueError('电脑任务执行模式须为automatic或per_action')
            webui = updated['webui']
            if type(webui['remote_enabled']) is not bool or type(webui['require_https']) is not bool or type(webui['port']) is not int or not 1 <= webui['port'] <= 65535:
                raise ValueError('WebUI远程开关须为布尔值，端口须为1—65535的整数')
            if not isinstance(webui['host'], str):
                raise ValueError('WebUI监听地址须为本机IP或localhost')
            webui['host'] = webui['host'].strip().lower()
            try:
                if webui['host'] != 'localhost': ipaddress.ip_address(webui['host'])
            except ValueError as exc:
                raise ValueError('WebUI监听地址须为本机IP或localhost') from exc
            if (not isinstance(webui['allowed_hosts'], list) or len(webui['allowed_hosts']) > 20
                    or any(not isinstance(host,str) or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?',host) for host in webui['allowed_hosts'])):
                raise ValueError('WebUI允许域名须为最多20项的域名列表')
            webui['allowed_hosts'] = list(dict.fromkeys(host.lower() for host in webui['allowed_hosts']))
            proxies = webui['trusted_proxy_ips']
            if not isinstance(proxies, list) or len(proxies) > 16:
                raise ValueError('受信任代理须为最多16个独立IP地址，不接受网段或通配符')
            try:
                if any(not isinstance(value, str) for value in proxies): raise ValueError()
                addresses = [ipaddress.ip_address(value) for value in proxies]
                if any(value.is_unspecified or value.is_multicast for value in addresses): raise ValueError()
                webui['trusted_proxy_ips'] = list(dict.fromkeys(str(value) for value in addresses))
            except ValueError as exc:
                raise ValueError('受信任代理须为独立IP地址，不接受网段或通配符') from exc
            if not isinstance(webui['public_url'], str):
                raise ValueError('WebUI公网入口须为HTTPS地址')
            webui['public_url'] = webui['public_url'].strip().rstrip('/')
            if webui['public_url']:
                parsed = urlsplit(webui['public_url'])
                try: parsed.port
                except ValueError as exc: raise ValueError('WebUI公网入口端口无效') from exc
                if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                        or parsed.path or parsed.query or parsed.fragment or parsed.hostname not in webui['allowed_hosts']):
                    raise ValueError('WebUI公网入口须为无路径的HTTPS地址，并将域名加入allowed_hosts')
            if type(computer['enabled']) is not bool or computer['runtime'] not in ('local', 'docker', 'none'):
                raise ValueError('电脑任务开关须为布尔值，执行环境须为本机、Docker隔离或关闭')
            if type(computer['allow_external_tools']) is not bool:
                raise ValueError('允许沙箱外工具须为布尔值')
            if not isinstance(computer['workspace'], str) or len(computer['workspace']) > 2000 or '\x00' in computer['workspace']:
                raise ValueError('电脑任务工作目录格式不正确')
            computer['workspace'] = computer['workspace'].strip()
            if computer['runtime'] == 'docker' and ',' in computer['workspace']:
                raise ValueError('Docker 沙箱工作目录不能含逗号，请另选专用文件夹')
            for key, low, high in (('max_steps',1,100),('timeout_seconds',1,600),('max_output_chars',1000,100000)):
                if type(computer[key]) is not int or not low <= computer[key] <= high:
                    raise ValueError(f'电脑任务 {key}须为{low}—{high}的整数')
            official = updated['qq_official']
            for key in ('allowed_groups', 'allowed_users', 'blocked_users', 'blocked_groups'):
                vals = official[key]
                if isinstance(vals, str):
                    vals = vals.replace('，', ',').replace('\n', ',').split(',')
                if not isinstance(vals, list):
                    raise ValueError('官方机器人的 openid 允许列表须为列表')
                official[key] = [str(x).strip() for x in vals if str(x).strip()]
            official['enabled'] = bool(official['enabled'])
            if official['enabled'] and (not official['app_id'] or not official['app_secret']):
                raise ValueError('启用官方机器人前请填写 AppID 和 AppSecret')
            from .profiles import validate_profiles
            validate_profiles(updated['bot_profiles'])
            embedding_ids = {item['id'] for item in updated['providers'] if item.get('model_type') == 'embedding'}
            if any(profile['values'].get('model_id') in embedding_ids for profile in updated['bot_profiles']['items']):
                raise ValueError('机器人配置的聊天模型不能使用嵌入模型')
            if updated['stickers']['model_id'] in embedding_ids:
                raise ValueError('表情包识图需要对话视觉模型，不能使用嵌入模型')
            for key,low,high in (('model_concurrency',1,8),('model_queue_limit',1,200),('background_queue_limit',1,100),('scope_queue_limit',1,32),
                                 ('global_model_concurrency',1,16),('global_model_queue_limit',1,200),
                                 ('request_concurrency',1,64),('request_queue_limit',1,200),('request_max_mb',1,64),
                                 ('parsing_concurrency',1,8),('parsing_queue_limit',1,100)):
                value=updated['scheduler'][key]
                if type(value) is not int or not low<=value<=high:
                    raise ValueError(f'调度参数 {key}须为{low}—{high}的整数')
            if dry_run:
                return updated
            previous = self._data
            self._data = updated
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
            return self.public()

    def _save(self):
        if self._recovery_pending:
            self._preserve_original()
        raw = self.get()
        for section, key in [('model', 'api_key'), ('onebot', 'access_token'), ('qq_official', 'app_secret')]:
            raw[section][key] = _protect(raw[section][key])
        for provider in raw['providers']:
            provider['api_key'] = _protect(provider['api_key'])
        for source in raw['provider_sources']:
            source['api_key'] = _protect(source['api_key'])
        for server in raw['mcp_servers']:
            server['token'] = _protect(server['token'])
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(self.path)
        if os.name != 'nt':
            self.path.chmod(0o600)
