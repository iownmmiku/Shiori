"""Extensions: trusted add-ons that run inside the Shiori process.

Unlike the sandboxed local plugins (plugins.py), an extension is imported
into the service and has the same permissions as Shiori itself. It can

- give the chat model tools; a tool may hand back work that runs after the
  reply has gone out (a picture drawn in the background, for example);
- register management APIs under /api/extensions/<id>/api/<path>, behind the
  console's normal sign-in;
- ship pages that the console embeds (/extensions/<id>/pages/<page>/...);
- declare settings in its manifest, which the console turns into a form.

Bundled extensions live in shiori/extensions. Others are folders in
<data>/extensions; they stay off until the owner confirms them, and a folder
whose files changed has to be confirmed again.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .settings import _protect, _unprotect

MANIFEST = 'extension.json'
BUILTIN_ROOT = Path(__file__).resolve().parent / 'extensions'
ID_PATTERN = re.compile(r'[a-z][a-z0-9_]{1,39}')
TOOL_PATTERN = re.compile(r'[A-Za-z][A-Za-z0-9_]{1,63}')
KEY_PATTERN = re.compile(r'[a-z][a-z0-9_]{0,39}')
PAGE_PATTERN = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
API_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,40}(?:/[A-Za-z0-9_-]{1,40}){0,4}')
CONFIG_TYPES = ('bool', 'int', 'float', 'string', 'text', 'select', 'secret')
API_METHODS = ('GET', 'POST', 'PUT', 'DELETE')
PAGE_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
              '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg',
              '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.gif': 'image/gif',
              '.ico': 'image/x-icon', '.woff2': 'font/woff2'}
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FOLDER_FILES = 400
MAX_FOLDER_BYTES = 64 * 1024 * 1024
MAX_PAGE_FILE_BYTES = 8 * 1024 * 1024
JOB_TTL = 600.0          # work handed back by a tool whose reply never went out


class ExtensionError(ValueError):
    """A configuration or manifest problem shown to the owner."""


@dataclass(frozen=True)
class Turn:
    """The chat turn a tool may be offered in."""
    scope_id: str
    user_id: str
    persona_id: str = 'default'
    images: tuple = ()           # data:image URLs the user attached to this turn
    group: bool = False          # several members' messages answered together


@dataclass(frozen=True)
class ToolCall:
    scope_id: str
    user_id: str
    user_key: str                # platform:account, e.g. qq:12345
    persona_id: str
    args: dict
    admin: bool = False
    images: tuple = ()


@dataclass
class ToolResult:
    """What a tool tells the model, and work that runs after the reply went out.

    `after_reply(sender)` runs once the text was delivered; `cancel()` runs
    instead when the reply was not sent, so the extension can give back what
    it reserved."""
    output: dict
    after_reply: Callable | None = None
    cancel: Callable | None = None


@dataclass
class ToolProvider:
    extension: str
    name: str
    schema: Callable
    handler: Callable
    hint: str = ''
    queued: bool = False


@dataclass
class Record:
    id: str
    root: Path
    manifest: dict
    builtin: bool
    digest: str = ''
    error: str = ''
    loaded: bool = False
    context: Any = None
    tools: dict = field(default_factory=dict)
    apis: dict = field(default_factory=dict)
    unload: list = field(default_factory=list)


# ---------- manifest ----------

def _text(value, label, limit, *, required=False):
    if value is None:
        value = ''
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ExtensionError(f'{label}须为不超过 {limit} 个字符的文字')
    value = value.strip()
    if required and not value:
        raise ExtensionError(f'缺少{label}')
    return value


def _inside(root, relative):
    if not isinstance(relative, str) or not relative or relative.startswith(('/', '\\')) or ':' in relative or '\\' in relative:
        return None
    parts = relative.split('/')
    if any(part in ('', '.', '..') or part.startswith('.') for part in parts):
        return None
    path = (root / relative).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def clean_option(field, value, label):
    """One settings value checked against its manifest field."""
    kind = field['type']
    if kind == 'bool':
        if type(value) is not bool:
            raise ExtensionError(f'{label}须为开或关')
        return value
    if kind in ('int', 'float'):
        if type(value) not in ((int,) if kind == 'int' else (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ExtensionError(f'{label}须为{"整数" if kind == "int" else "数字"}')
        if 'min' in field and value < field['min'] or 'max' in field and value > field['max']:
            raise ExtensionError(f"{label}须在 {field.get('min', '不限')}—{field.get('max', '不限')} 之间")
        return value if kind == 'int' else float(value)
    if kind == 'select':
        if value not in [option[0] for option in field['options']]:
            raise ExtensionError(f'{label}不是可选的值')
        return value
    limit = 8000 if kind == 'text' else 4000
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ExtensionError(f'{label}须为不超过 {limit} 个字符的文字')
    return value if kind == 'text' else value.strip()


def _config_schema(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 40:
        raise ExtensionError('config 须为最多 40 项的对象')
    schema = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or not isinstance(item, dict):
            raise ExtensionError(f'设置项 {key!r} 无效')
        kind = item.get('type')
        if kind not in CONFIG_TYPES:
            raise ExtensionError(f'设置项 {key} 的 type 须为 ' + '、'.join(CONFIG_TYPES))
        field = {'type': kind, 'title': _text(item.get('title'), f'设置项 {key} 的标题', 40, required=True),
                 'description': _text(item.get('description'), f'设置项 {key} 的说明', 300)}
        if kind in ('int', 'float'):
            for bound in ('min', 'max'):
                if bound in item:
                    if type(item[bound]) not in (int, float) or isinstance(item[bound], bool):
                        raise ExtensionError(f'设置项 {key} 的 {bound} 须为数字')
                    field[bound] = item[bound]
        if kind == 'select':
            options = item.get('options')
            if not isinstance(options, list) or not 1 <= len(options) <= 50:
                raise ExtensionError(f'设置项 {key} 须有 1—50 个 options')
            field['options'] = []
            for option in options:
                value, title = (option, option) if isinstance(option, str) else (option if isinstance(option, list) and len(option) == 2 else (None, None))
                if not isinstance(value, str) or not isinstance(title, str) or len(value) > 100 or len(title) > 60:
                    raise ExtensionError(f'设置项 {key} 的 options 须为文字或 [值, 名称]')
                field['options'].append([value, title])
        default = item.get('default', {'bool': False, 'int': 0, 'float': 0.0, 'select': field.get('options', [['']])[0][0]}.get(kind, ''))
        if kind == 'secret' and default:
            raise ExtensionError(f'密钥设置项 {key} 不能带默认值')
        field['default'] = clean_option(field, default, f'设置项 {key} 的默认值')
        schema[key] = field
    return schema


def read_manifest(root):
    """The validated manifest of the extension folder `root`."""
    path = root / MANIFEST
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ExtensionError(f'缺少 {MANIFEST} 或文件超过 64 KB')
    try:
        raw = json.loads(path.read_text('utf-8-sig'))
    except (UnicodeError, ValueError) as exc:
        raise ExtensionError(f'{MANIFEST} 不是有效的 JSON') from exc
    if not isinstance(raw, dict):
        raise ExtensionError(f'{MANIFEST} 须为 JSON 对象')
    known = {'id', 'name', 'version', 'description', 'author', 'entry', 'pages', 'config', 'default_enabled'}
    if set(raw) - known:
        raise ExtensionError(f'{MANIFEST} 有不认识的字段：' + '、'.join(sorted(set(raw) - known)))
    key = raw.get('id')
    if not isinstance(key, str) or not ID_PATTERN.fullmatch(key):
        raise ExtensionError('扩展 id 须为 2—40 个小写字母、数字或下划线，以字母开头')
    if key != root.name:
        raise ExtensionError(f'扩展 id「{key}」须与文件夹名「{root.name}」相同')
    entry = raw.get('entry', 'main.py')
    if not isinstance(entry, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,40}\.py', entry) or not (root / entry).is_file():
        raise ExtensionError('entry 须为扩展文件夹里存在的 .py 文件名')
    pages = []
    for item in raw.get('pages') or []:
        if not isinstance(item, dict) or set(item) - {'id', 'title', 'entry'}:
            raise ExtensionError('pages 的每一项须为包含 id、title、entry 的对象')
        page = item.get('id')
        if not isinstance(page, str) or not PAGE_PATTERN.fullmatch(page) or page in [p['id'] for p in pages]:
            raise ExtensionError('页面 id 无效或重复')
        target = _inside(root, item.get('entry'))
        if target is None or target.suffix.lower() != '.html' or not target.is_file():
            raise ExtensionError(f'页面「{page}」的 entry 须为扩展文件夹里存在的 .html 文件')
        pages.append({'id': page, 'title': _text(item.get('title'), '页面标题', 30, required=True), 'entry': item['entry']})
    if len(pages) > 8:
        raise ExtensionError('最多 8 个页面')
    if 'default_enabled' in raw and type(raw['default_enabled']) is not bool:
        raise ExtensionError('default_enabled 须为 true 或 false')
    return {'id': key, 'name': _text(raw.get('name'), '扩展名称', 40, required=True),
            'version': _text(raw.get('version'), '版本号', 20) or '0.0.0',
            'description': _text(raw.get('description'), '扩展说明', 300), 'author': _text(raw.get('author'), '作者', 60),
            'entry': entry, 'pages': pages, 'config': _config_schema(raw.get('config')),
            'default_enabled': raw.get('default_enabled', True)}


def folder_digest(root):
    """A digest of every file in an extension folder. Bytecode caches and
    hidden folders such as .git (a cloned extension) are left out: they hold
    nothing Shiori loads and change without the code changing."""
    digest, count, total = hashlib.sha256(), 0, 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name != '__pycache__' and not name.startswith('.'))
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                raise ExtensionError('扩展文件夹里不能有链接')
            count += 1
            total += path.stat().st_size
            if count > MAX_FOLDER_FILES or total > MAX_FOLDER_BYTES:
                raise ExtensionError('扩展文件夹超过 400 个文件或 64 MB')
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


# ---------- the extension's view of Shiori ----------

class ExtensionContext:
    """Passed to an extension's setup(ext)."""

    def __init__(self, host, record):
        self._host, self._record = host, record
        self.id, self.root, self.name = record.id, record.root, record.manifest['name']
        self.store, self.settings = host.store, host.settings
        self.state = {}          # the extension's own objects

    @property
    def data_dir(self):
        """A folder of its own in the data directory, created on first use."""
        path = self._host.data_dir / 'extension_data' / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def config(self):
        """Current settings from the manifest's config, with defaults filled in."""
        return self._host.config_values(self.id)

    def tool(self, name, schema, handler, *, hint='', queued=False):
        """Offer the chat model a tool.

        `schema(turn)` returns the function schema for this turn, or None to
        leave the tool out. `handler(call)` returns a dict for the model or a
        ToolResult. `hint` is added to the system prompt when the tool is
        offered. A `queued` tool only reserves work for after the reply, so a
        turn that used nothing else may fail or start over like one without
        tools."""
        if (not isinstance(name, str) or not TOOL_PATTERN.fullmatch(name) or name in self._host.reserved
                or name.startswith('mcp_')):
            raise ExtensionError(f'工具名「{name}」无效或与内置工具重名')
        owner = self._host.tool_owner(name)
        if owner and owner != self.id:
            raise ExtensionError(f'工具名「{name}」已被扩展「{owner}」使用')
        if not callable(schema) or not callable(handler):
            raise ExtensionError('工具的 schema 和 handler 须为函数')
        self._record.tools[name] = ToolProvider(self.id, name, schema, handler, _text(hint, '工具提示', 600), bool(queued))

    def api(self, method, path, handler):
        """Serve `handler(request)` at /api/extensions/<id>/api/<path>."""
        method = str(method).upper()
        if method not in API_METHODS or not isinstance(path, str) or not API_PATTERN.fullmatch(path) or not callable(handler):
            raise ExtensionError(f'接口 {method} {path} 无效')
        self._record.apis[(method, path)] = handler

    def on_unload(self, function):
        """Called when the extension is turned off or Shiori stops."""
        self._record.unload.append(function)

    def personas(self):
        try:
            return [{'id': item['id'], 'name': item.get('name') or item['id']} for item in self.store.list_personas()]
        except Exception:
            return []


@dataclass
class ExtensionRequest:
    method: str
    path: str
    query: dict
    body: Any = None


# ---------- host ----------

class ExtensionHost:
    def __init__(self, store, settings, data_dir, *, builtin_root=BUILTIN_ROOT, reserved=()):
        self.store, self.settings = store, settings
        self.data_dir = Path(data_dir)
        self.builtin_root = Path(builtin_root)
        self.user_root = self.data_dir / 'extensions'
        self.reserved = frozenset(reserved)
        self.state_path = self.data_dir / 'extensions.json'
        self.records = {}
        self._jobs = {}
        self._lock = threading.RLock()
        self._state = self._load_state()

    # -- state --
    def _load_state(self):
        state = {'enabled': {}, 'trusted': {}, 'config': {}}
        try:
            raw = json.loads(self.state_path.read_text('utf-8'))
            if isinstance(raw, dict):
                for key in state:
                    if isinstance(raw.get(key), dict):
                        state[key] = raw[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self._event_error('扩展状态文件无法读取，扩展按默认状态加载', exc)
        return state

    def _save_state(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + '.tmp')
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), 'utf-8')
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def _event_error(self, message, exc, **details):
        try:
            self.store.add_exception('extension_error', message, exc, **details)
        except Exception:
            pass

    # -- discovery and loading --
    def start(self):
        """Find extensions and load the enabled ones."""
        with self._lock:
            self.records = {}
            for root, builtin in ((self.builtin_root, True), (self.user_root, False)):
                if not root.is_dir():
                    continue
                for folder in sorted(root.iterdir()):
                    if not folder.is_dir() or folder.name.startswith(('.', '_')) or not (folder / MANIFEST).exists():
                        continue
                    record = Record(folder.name, folder, {'id': folder.name, 'name': folder.name, 'version': '', 'description': '',
                                                          'author': '', 'pages': [], 'config': {}, 'entry': 'main.py',
                                                          'default_enabled': False}, builtin)
                    try:
                        record.manifest = read_manifest(folder)
                        if not builtin:
                            record.digest = folder_digest(folder)
                        if folder.name in self.records:
                            raise ExtensionError('与已有扩展同名，未加载')
                    except (ExtensionError, OSError) as exc:
                        record.error = str(exc)
                        key = folder.name if folder.name not in self.records else f'{folder.name}~{len(self.records)}'
                        self.records[key] = record
                        continue
                    self.records[record.id] = record
            for record in self.records.values():
                if not record.error and self.enabled(record.id):
                    self._load(record)

    def enabled(self, extension_id):
        record = self.records.get(extension_id)
        if record is None or record.error and not record.loaded:
            return False
        saved = self._state['enabled'].get(extension_id)
        if record.builtin:
            return record.manifest['default_enabled'] if saved is None else saved is True
        return saved is True and self._state['trusted'].get(extension_id) == record.digest

    def _import(self, record):
        stem = record.manifest['entry'][:-3]
        if record.builtin:
            return importlib.import_module(f'{__package__}.extensions.{record.id}.{stem}')
        name = f'shiori_extension_{record.id}'
        for module in [key for key in sys.modules if key == name or key.startswith(name + '.')]:
            sys.modules.pop(module)
        package = importlib.util.spec_from_file_location(name, record.root / '__init__.py', submodule_search_locations=[str(record.root)])
        package_module = importlib.util.module_from_spec(package)
        sys.modules[name] = package_module
        if (record.root / '__init__.py').is_file():
            package.loader.exec_module(package_module)
        spec = importlib.util.spec_from_file_location(f'{name}.{stem}', record.root / record.manifest['entry'])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def _load(self, record):
        record.tools, record.apis, record.unload, record.error = {}, {}, [], ''
        record.context = ExtensionContext(self, record)
        try:
            module = self._import(record)
            setup = getattr(module, 'setup', None)
            if not callable(setup):
                raise ExtensionError('入口文件没有 setup(ext) 函数')
            if inspect.iscoroutinefunction(setup):
                raise ExtensionError('setup(ext) 须为普通函数；需要等待的工作请放进工具或接口里')
            setup(record.context)
            record.loaded = True
        except Exception as exc:
            record.tools, record.apis, record.loaded = {}, {}, False
            record.error = f'加载失败：{type(exc).__name__}: {str(exc)[:200]}'
            self._event_error(f'扩展「{record.manifest["name"]}」加载失败', exc, extension=record.id)

    def _unload(self, record):
        for function in reversed(record.unload):
            try:
                result = function()
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception as exc:
                self._event_error('扩展停止时出错', exc, extension=record.id)
        with self._lock:
            for key, job in list(self._jobs.items()):
                if job['extension'] == record.id:
                    self._discard(key)
        record.tools, record.apis, record.unload, record.loaded = {}, {}, [], False

    def set_enabled(self, extension_id, enabled, *, confirm=False):
        with self._lock:
            record = self.records.get(extension_id)
            if record is None:
                raise KeyError(extension_id)
            if record.error and not record.loaded and enabled:
                raise ExtensionError('这个扩展有错误，修好后重启 Shiori 再启用：' + record.error)
            if enabled and not record.builtin and self._state['trusted'].get(extension_id) != record.digest:
                if not confirm:
                    raise ExtensionError('这个扩展会在 Shiori 进程里运行，权限和 Shiori 相同。请确认它来自你信任的来源后再启用')
                self._state['trusted'][extension_id] = record.digest
            self._state['enabled'][extension_id] = bool(enabled)
            self._save_state()
            if enabled and not record.loaded:
                self._load(record)
            elif not enabled and record.loaded:
                self._unload(record)
            return self.describe(record)

    async def close(self):
        for record in list(self.records.values()):
            if record.loaded:
                self._unload(record)

    # -- settings --
    def config_values(self, extension_id):
        record = self.records[extension_id]
        saved = self._state['config'].get(extension_id) or {}
        values = {}
        for key, item in record.manifest['config'].items():
            value = saved.get(key, item['default'])
            if item['type'] == 'secret':
                try:
                    value = _unprotect(value) if value else ''
                except Exception:
                    value = ''
            try:
                values[key] = clean_option(item, value, item['title'])
            except ExtensionError:
                values[key] = item['default']
        return values

    def set_config(self, extension_id, values, clear=()):
        """Save settings; an empty secret keeps the saved one, `clear` removes it."""
        with self._lock:
            record = self.records.get(extension_id)
            if record is None:
                raise KeyError(extension_id)
            schema = record.manifest['config']
            if not isinstance(values, dict) or set(values) - set(schema) or not isinstance(clear, (list, tuple)) or set(clear) - set(schema):
                raise ExtensionError('设置包含这个扩展没有的项')
            saved = dict(self._state['config'].get(extension_id) or {})
            for key, value in values.items():
                item = schema[key]
                if item['type'] == 'secret':
                    if value in ('', None):
                        continue
                    saved[key] = _protect(clean_option(item, value, item['title']))
                else:
                    saved[key] = clean_option(item, value, item['title'])
            for key in clear:
                if schema[key]['type'] == 'secret':
                    saved.pop(key, None)
            self._state['config'][extension_id] = saved
            self._save_state()
            return self.describe(record)

    # -- descriptions --
    def describe(self, record):
        config = self.config_values(record.id) if not record.error or record.loaded else {}
        schema = record.manifest['config']
        return {'id': record.id, 'name': record.manifest['name'], 'version': record.manifest['version'],
                'description': record.manifest['description'], 'author': record.manifest['author'],
                'builtin': record.builtin, 'enabled': self.enabled(record.id), 'loaded': record.loaded, 'error': record.error,
                'trusted': record.builtin or self._state['trusted'].get(record.id) == record.digest,
                'pages': [dict(page, url=f'/extensions/{record.id}/pages/{page["id"]}/') for page in record.manifest['pages']],
                'tools': sorted(record.tools), 'config_schema': schema,
                'config': {key: value for key, value in config.items() if schema[key]['type'] != 'secret'},
                'secrets_set': {key: bool(config.get(key)) for key, item in schema.items() if item['type'] == 'secret'}}

    def list(self):
        with self._lock:
            return [self.describe(record) for record in self.records.values()]

    def context(self, extension_id):
        record = self.records.get(extension_id)
        return record.context if record is not None and record.loaded else None

    # -- tools --
    def _providers(self):
        for record in list(self.records.values()):
            if record.loaded:
                yield from record.tools.values()

    def tool_owner(self, name):
        return next((record.id for record in self.records.values() if name in record.tools), None)

    def queued_names(self):
        return frozenset(provider.name for provider in self._providers() if provider.queued)

    def has_tool(self, name):
        return any(provider.name == name for provider in self._providers())

    def tool_schemas(self, turn):
        """The extension tools offered in this turn, in a stable order."""
        schemas = []
        for provider in self._providers():
            try:
                schema = provider.schema(turn)
            except Exception as exc:
                self._event_error('扩展工具准备失败，本轮不提供', exc, extension=provider.extension, tool=provider.name)
                continue
            if schema:
                if not isinstance(schema, dict) or (schema.get('function') or {}).get('name') != provider.name:
                    self._event_error('扩展工具定义的名字不一致，本轮不提供', ExtensionError(provider.name), extension=provider.extension)
                    continue
                schemas.append(schema)
        return schemas

    def hints(self, names):
        return [provider.hint for provider in self._providers() if provider.name in names and provider.hint]

    async def call_tool(self, name, call):
        """Run an extension tool; returns what the model sees."""
        provider = next((item for item in self._providers() if item.name == name), None)
        if provider is None:
            raise ValueError('这个扩展工具已停用')
        result = provider.handler(call)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return result
        if not isinstance(result, ToolResult) or not isinstance(result.output, dict):
            raise ValueError('扩展工具没有返回结果')
        output = dict(result.output)
        if result.after_reply is not None:
            key = uuid.uuid4().hex
            with self._lock:
                self._sweep()
                self._jobs[key] = {'extension': provider.extension, 'tool': name, 'after_reply': result.after_reply,
                                   'cancel': result.cancel, 'created': time.monotonic()}
            output['extension_job'] = {'id': key, 'extension': provider.extension}
        return output

    def _discard(self, key):
        job = self._jobs.pop(key, None)
        if job is not None and callable(job['cancel']):
            try:
                job['cancel']()
            except Exception as exc:
                self._event_error('扩展任务撤销时出错', exc, extension=job['extension'])
        return job is not None

    def _sweep(self):
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if now - job['created'] > JOB_TTL:
                self._discard(key)

    def discard(self, job_id):
        """The reply that announced this work did not go out."""
        with self._lock:
            return self._discard(job_id)

    def discard_actions(self, actions):
        for action in actions or []:
            job = action.get('result', {}).get('extension_job') if isinstance(action.get('result'), dict) else None
            if isinstance(job, dict):
                self.discard(job.get('id'))

    def pending(self, job_id):
        with self._lock:
            return job_id in self._jobs

    async def run_job(self, job_id, sender):
        """Run work handed back by a tool, now that its reply was delivered."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        try:
            result = job['after_reply'](sender)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            if callable(job['cancel']):
                try:
                    job['cancel']()
                except Exception:
                    pass
            raise
        except Exception as exc:
            self._event_error('扩展的回复后任务没有完成', exc, extension=job['extension'], tool=job['tool'])
            return False
        return True

    # -- web --
    def api_handler(self, extension_id, method, path):
        record = self.records.get(extension_id)
        if record is None or not record.loaded:
            raise KeyError(extension_id)
        handler = record.apis.get((method, path))
        if handler is None:
            raise KeyError(path)
        return handler

    def page_file(self, extension_id, page_id, asset):
        """(path, content type) of a file from an enabled extension's page folder."""
        record = self.records.get(extension_id)
        if record is None or not record.loaded:
            raise KeyError(extension_id)
        page = next((item for item in record.manifest['pages'] if item['id'] == page_id), None)
        if page is None:
            raise KeyError(page_id)
        entry = _inside(record.root, page['entry'])
        if entry is None:
            raise KeyError(page_id)
        if not asset:
            path = entry
        else:
            path = _inside(entry.parent, asset)
            if path is None:
                raise KeyError(asset)
        kind = PAGE_TYPES.get(path.suffix.lower())
        if kind is None or not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_PAGE_FILE_BYTES:
            raise KeyError(asset)
        return path, kind
