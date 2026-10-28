"""Versioned local plugins, executed by the existing computer permission boundary.

Plugin code is never imported into the server process. Saving or listing a
plugin has no execution side effects; editing always revokes its activation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4
import zipfile

from fastapi import Body, File, HTTPException, UploadFile
from fastapi.responses import Response

from .computer import schema
from .filesystem import is_link
from .plugin_packages import (DEFAULT_INPUT, MAX_ARCHIVE, MAX_FILE, MAX_FILES, MAX_TOTAL,
                              inspect_archive, safe_name, validate_code, validate_package)

CREATE_TOOL = schema('create_plugin', '仅在本地管理员明确要求创建插件时保存规范插件。定义 run(input)，返回 JSON 结果；建议提供 input_schema 以自动生成参数表单，readme 写使用说明。生成后可从 export_url 下载标准 ZIP。默认停用草稿；已有电脑自动授权且用户明确要求创建并运行时直接启用，再调用 run_plugin。',
    {'id': {'type': 'string', 'minLength': 2, 'maxLength': 48},
     'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
     'description': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
     'code': {'type': 'string', 'minLength': 1, 'maxLength': 10000},
     'version': {'type': 'string', 'minLength': 1, 'maxLength': 40},
     'input_schema': {'type': 'object'},
     'readme': {'type': 'string', 'maxLength': 4000}}, ['id', 'name', 'description', 'code'])
RUN_TOOL = schema('run_plugin', '仅在本地管理员明确要求使用插件时，运行已启用的插件。遵循电脑任务的运行环境、审批、时限与停止设置。',
    {'id': {'type': 'string', 'minLength': 2, 'maxLength': 48},
     'input': {'type': 'object'}}, ['id', 'input'])
LIST_TOOL = schema('list_plugins', '查看本地插件 ID、用途及启用状态。使用插件前先查询，不能猜测插件 ID。', {}, [])
READ_TOOL = schema('get_plugin', '读取本地插件的入口代码、参数或说明，以便修改已有插件。返回修订号与摘要；内容按 section 分页，next_offset 非空时继续读取，不能把截断内容当完整文件。读取不执行插件。',
    {'id': {'type': 'string', 'minLength': 2, 'maxLength': 48},
     'section': {'type': 'string', 'enum': ['code', 'input_schema', 'readme']},
     'offset': {'type': 'integer', 'minimum': 0, 'maximum': MAX_FILE}}, ['id'])
EDIT_TOOL = schema('update_plugin', '仅在本地管理员明确要求修改或修复插件时保存新修订。先用 get_plugin 读取现有代码与参数，提交读取所得 expected_revision、expected_sha256 防止覆盖别人改动。省略的名称、说明、参数及包内辅助文件保留；保存后停用，不自动运行。',
    {**CREATE_TOOL['function']['parameters']['properties'],
     'expected_revision': {'type': 'integer', 'minimum': 1},
     'expected_sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64}},
    ['id', 'code', 'expected_revision', 'expected_sha256'])


def requested(text, *, create=False, edit=False):
    text = str(text).strip()
    if not re.search(r'插件|plugin', text, re.I) or re.match(r'^(?:请)?(?:不要|不用|别)', text):
        return False
    if edit:
        verbs = r'修改|修复|编辑|调整|更新|修好|修一下|fix|edit|update|repair'
        if re.search(r'(?:不要|不用|别|无需|不必|不需要)\s*(?:再|继续)?\s*(?:' + verbs + ')', text, re.I):
            return False
        action = re.sub(r'^(?:(?:可以帮我|能不能|我希望|我需要|帮我|为我|替我|麻烦|能否|我想|现在|给我|请|先|再)\s*)+', '', text, flags=re.I)
        return bool(re.match(r'(?:修改|修复|编辑|调整|更新|修好|修一下|(?:fix|edit|update|repair)\b)', action, re.I)
                    or re.match(r'^(?:把|将|对).{1,100}(?:' + verbs + r')', action, re.I))
    if not re.match(r'^(?:请|麻烦|帮我|为我|替我|能否|可以帮我|能不能|我想|我希望|我需要|现在|先|再|给我|创建|编写|制作|开发|生成|运行|使用|调用|执行|create|build|run|use)', text, re.I):
        return False
    return bool(re.search(r'创建|编写|制作|开发|生成|create|build' if create else r'运行|使用|调用|执行|run|use', text, re.I))


def create_and_run(text):
    """A combined explicit request may use an existing automatic grant."""
    return bool(requested(text, create=True) and requested(text)
                and re.search(r'(?:并(?:且)?|然后|接着|再|直接)(?:帮我|为我)?(?:运行|执行|调用|使用)', str(text))
                and not re.search(r'不(?:要|用|会)?(?:运行|执行|调用|使用)', str(text)))


class PluginService:
    def __init__(self, data_dir, computer):
        self.folder = Path(data_dir) / 'plugins'
        self.computer, self.runs = computer, computer.runs
        self.lock = threading.RLock()
        self._scans = {}
        self.runs.register('plugin', self.run, self.guard)

    def _path(self, plugin_id):
        if not isinstance(plugin_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,47}', plugin_id):
            raise ValueError('插件 ID 需为2—48位小写字母、数字或下划线，并以字母开头')
        if is_link(self.folder) or is_link(self.folder.parent):
            raise ValueError('插件目录不能是链接')
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / (plugin_id + '.json')
        if is_link(path): raise ValueError('插件文件不能是链接')
        return path

    def get(self, plugin_id, *, verify_files=True):
        with self.lock:
            path = self._path(plugin_id)
            if not path.exists(): raise ValueError('插件不存在')
            if path.stat().st_size > 160000: raise ValueError('插件文件过大')
            item = json.loads(path.read_text('utf-8'))
            if (not isinstance(item, dict) or item.get('id') != plugin_id or item.get('schema_version') not in (1, 2)
                    or not isinstance(item.get('code'), str) or len(item['code']) > 10000
                    or self._digest(item) != item.get('sha256') or type(item.get('enabled')) is not bool
                    or type(item.get('revision')) is not int or item['revision'] < 1
                    or not all(isinstance(item.get(key), str) and item[key].strip() for key in ('name', 'description'))):
                raise ValueError('插件内容或摘要已变化，请重新保存并启用')
            if item['schema_version'] == 2:
                if verify_files: self._files(item)
                elif not isinstance(item.get('files'), dict): raise ValueError('插件文件列表损坏')
            return item

    @staticmethod
    def _digest(item):
        keys = ('id', 'name', 'description', 'code')
        if item.get('schema_version') == 2: keys += ('manifest', 'files', 'package_hash')
        raw = json.dumps({key: item.get(key) for key in keys}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _internal(self, *parts):
        root = self.folder
        self._path('validate')
        for part in parts:
            root = root / part
            if is_link(root): raise ValueError('插件存储路径不能是链接')
        return root

    def _files(self, item):
        if item['schema_version'] == 1:
            return self._legacy_files(item)
        digest, records = item.get('package_hash'), item.get('files')
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest) or not isinstance(records, dict) or len(records) > MAX_FILES:
            raise ValueError('插件包文件列表损坏')
        files, total = {}, 0
        for name, record in records.items():
            if safe_name(name) != name or not isinstance(record, dict): raise ValueError('插件文件路径或摘要损坏')
            path = self._internal('.packages', item['id'], digest, *name.split('/'))
            size = record.get('bytes')
            if type(size) is not int or not 0 <= size <= MAX_FILE or not path.is_file() or path.stat().st_size != size:
                raise ValueError('插件文件缺失或大小已变化：' + name)
            total += size
            if total > MAX_TOTAL: raise ValueError('插件文件总大小超出上限')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
                raise ValueError('插件文件摘要已变化：' + name)
            files[name] = raw
        manifest, code, _ = validate_package(item['manifest'], files)
        if code != item['code'] or manifest != item['manifest']:
            raise ValueError('插件入口与清单不一致')
        return files

    @staticmethod
    def _legacy_files(item):
        manifest = dict(schema_version=1, id=item['id'], name=item['name'], description=item['description'],
                        version='1.0.0', entrypoint='main.py', input_schema=DEFAULT_INPUT)
        return {'plugin.json': json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
                'main.py': item['code'].encode(), 'README.md': ('# ' + item['name'] + '\n\n' + item['description'] + '\n\n入口：run(input)，参数为 JSON 对象。\n').encode()}

    def _save(self, item):
        path = self._path(item['id'])
        if path.exists():
            previous = json.loads(path.read_text('utf-8'))
            if previous.get('sha256') != item['sha256']:
                history = self._internal('.history', item['id'])
                history.mkdir(parents=True, exist_ok=True)
                backup = self._internal('.history', item['id'], str(previous['revision']) + '.json')
                backup.write_text(json.dumps(previous, ensure_ascii=False, indent=2), 'utf-8')
        temp = path.with_suffix('.tmp')
        if is_link(temp): raise ValueError('插件临时文件不能是链接')
        temp.write_text(json.dumps(item, ensure_ascii=False, indent=2), 'utf-8')
        temp.replace(path)
        return item

    def list(self):
        with self.lock:
            self._path('validate')
            result = []
            for path in sorted(self.folder.glob('*.json'))[:200]:
                try:
                    item = self.get(path.stem, verify_files=False)
                    result.append({k: v for k, v in item.items() if k != 'code'})
                except (OSError, ValueError, KeyError, TypeError):
                    result.append({'id': path.stem, 'name': path.stem, 'enabled': False, 'error': '文件损坏或摘要不符，未加载'})
            return result

    def save(self, payload):
        from .tool_actions import validate_arguments
        validate_arguments(CREATE_TOOL['function']['parameters'], payload)
        validate_code(payload['code'])
        with self.lock:
            path = self._path(payload['id'])
            old = self.get(payload['id']) if path.exists() else None
            if not old and len(list(self.folder.glob('*.json'))) >= 200: raise ValueError('插件数量已达200个上限')
            if any(key in payload for key in ('input_schema', 'version', 'readme')) or (old or {}).get('schema_version') == 2:
                files = self._files(old) if old else {}
                manifest = dict((old or {}).get('manifest') or dict(schema_version=1, version='1.0.0', entrypoint='main.py'))
                manifest.update({key: payload[key] for key in ('id', 'name', 'description')})
                for key in ('input_schema', 'version'):
                    if key in payload: manifest[key] = payload[key]
                files[manifest['entrypoint']] = payload['code'].encode()
                if 'readme' in payload: files['README.md'] = payload['readme'].encode()
                return self._install(manifest, files)
            item = dict(payload, schema_version=1, revision=(old or {}).get('revision', 0) + 1, enabled=False)
            item['sha256'] = self._digest(item)
            return self._save(item)

    def create(self, payload):
        """Chat creation cannot bypass the revision checks used by editing."""
        with self.lock:
            if self._path(payload.get('id')).exists():
                raise ValueError('同名插件已存在；请先用 get_plugin 读取，再用 update_plugin 修改，不能直接覆盖')
            return self.save(payload)

    def read_for_edit(self, plugin_id, *, section='code', offset=0):
        if section not in ('code', 'input_schema', 'readme') or type(offset) is not int or not 0 <= offset <= MAX_FILE:
            raise ValueError('请选择代码、参数定义或说明，并提供有效读取位置')
        with self.lock:
            item = self.get(plugin_id)
            manifest = item.get('manifest') or {}
            if section == 'code':
                value = item['code']
            elif section == 'input_schema':
                value = json.dumps(manifest.get('input_schema', DEFAULT_INPUT), ensure_ascii=False, indent=2)
            else:
                value = self._files(item).get('README.md', b'').decode('utf-8-sig', errors='replace')
            if offset > len(value):
                raise ValueError('读取位置超过内容长度，请从0开始或使用上次的 next_offset')
            # Keep the whole result below chat's 5000-character JSON budget,
            # including escaping. Never rely on bounded_result's raw preview,
            # which can lose the revision token or silently cut Python source.
            take = min(2500, len(value) - offset)
            result = dict(id=item['id'], name=item['name'], description=item['description'][:200],
                          revision=item['revision'], sha256=item['sha256'], enabled=item['enabled'],
                          entrypoint=manifest.get('entrypoint', 'main.py'), version=manifest.get('version', '1.0.0'),
                          section=section, offset=offset, total_chars=len(value))
            while True:
                result.update(content=value[offset:offset + take],
                              next_offset=offset + take if offset + take < len(value) else None)
                if len(json.dumps(result, ensure_ascii=False, sort_keys=True)) <= 4500:
                    return result
                take //= 2

    def edit(self, payload):
        from .tool_actions import validate_arguments
        validate_arguments(EDIT_TOOL['function']['parameters'], payload)
        with self.lock:
            old = self.get(payload['id'])
            if (old['revision'] != payload['expected_revision']
                    or old['sha256'] != payload['expected_sha256']):
                raise ValueError('插件修订或摘要已变化，未覆盖现有内容；请重新读取后再修改')
            changes = {key: old[key] for key in ('id', 'name', 'description', 'code')}
            changes.update({key: value for key, value in payload.items()
                            if key not in ('expected_revision', 'expected_sha256')})
            return self.save(changes)

    def _install(self, manifest, files):
        manifest, code, warnings = validate_package(manifest, files)
        plugin_id = manifest['id']
        old_path = self._path(plugin_id)
        old = self.get(plugin_id) if old_path.exists() else None
        if not old and len(list(self.folder.glob('*.json'))) >= 200: raise ValueError('插件数量已达200个上限')
        files = dict(files)
        files['plugin.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        files.setdefault('README.md', ('# ' + manifest['name'] + '\n\n' + manifest['description'] + '\n').encode())
        if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL or any(len(raw) > MAX_FILE for raw in files.values()):
            raise ValueError('规范插件最多128个文件、单文件2MB、总大小16MB（含清单和说明）')
        records = {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in sorted(files.items())}
        package_hash = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        root = self._internal('.packages', plugin_id, package_hash)
        # Immutable content directories are committed before the single metadata pointer.
        # A failed metadata replacement leaves the prior installed revision untouched.
        if not root.exists():
            staging = self._internal('.packages', plugin_id, '.stage-' + uuid4().hex)
            staging.mkdir(parents=True)
            try:
                for name, raw in files.items():
                    target = staging.joinpath(*safe_name(name).split('/'))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                staging.replace(root)
            except BaseException:
                # Remove only the directory just created by this operation.
                for target in sorted(staging.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                    if target.is_file(): target.unlink()
                    elif target.is_dir(): target.rmdir()
                if staging.exists(): staging.rmdir()
                raise
        item = dict(id=plugin_id, name=manifest['name'], description=manifest['description'], code=code,
                    schema_version=2, manifest=manifest, files=records, package_hash=package_hash,
                    revision=(old or {}).get('revision', 0) + 1, enabled=False)
        item['sha256'] = self._digest(item)
        self._files(item)
        self._save(item)
        return dict(item, warnings=warnings, export_url='/api/plugins/' + plugin_id + '/export')

    def scan(self, filename, raw):
        candidates = inspect_archive(filename, raw)
        with self.lock:
            now = time.monotonic()
            self._scans = {key: value for key, value in self._scans.items() if now - value['at'] < 600}
            if len(self._scans) >= 4: self._scans.pop(next(iter(self._scans)))
            token = uuid4().hex
            self._scans[token] = dict(at=now, candidates=candidates)
            return {'scan_id': token, 'candidates': [{key: value for key, value in c.items() if key not in ('code', 'files')}
                    | {'file_count': len(c['files']), 'bytes': sum(map(len, c['files'].values())),
                       'replaces_existing': self._path(c['manifest']['id']).exists() if c.get('manifest') else False}
                    for c in candidates]}

    def install_scan(self, scan_id, candidate_id, *, replace=False):
        with self.lock:
            scan = self._scans.get(scan_id)
            if not scan or time.monotonic() - scan['at'] >= 600: raise ValueError('预览已过期，请重新上传插件包')
            selected = next((c for c in scan['candidates'] if c['candidate_id'] == candidate_id), None)
            if not selected or selected.get('error'): raise ValueError('请选择通过校验的插件候选')
            if self._path(selected['manifest']['id']).exists() and replace is not True:
                raise ValueError('同名插件已存在，请明确选择替换；旧修订会保留，可回退')
            item = self._install(selected['manifest'], selected['files'])
            return item

    def export(self, plugin_id):
        with self.lock:
            item = self.get(plugin_id)
            files = self._files(item)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, raw in sorted(files.items()): archive.writestr(plugin_id + '/' + name, raw)
            return buffer.getvalue()

    def rollback(self, plugin_id):
        with self.lock:
            current = self.get(plugin_id)
            history = self._internal('.history', plugin_id)
            candidates = sorted((p for p in history.glob('*.json') if p.stem.isdigit()), key=lambda p: int(p.stem), reverse=True)
            for path in candidates:
                if is_link(path): raise ValueError('插件历史不能是链接')
                if path.stat().st_size > 160000: raise ValueError('插件历史文件过大')
                old = json.loads(path.read_text('utf-8'))
                if old.get('id') != plugin_id or self._digest(old) != old.get('sha256'): raise ValueError('插件历史摘要不符')
                if old['sha256'] == current['sha256']: continue
                self._files(old)
                old.update(revision=current['revision'] + 1, enabled=False)
                return self._save(old)
            raise ValueError('没有可回退的旧插件修订')

    def activate(self, plugin_id, enabled):
        if type(enabled) is not bool: raise ValueError('启用状态无效')
        with self.lock:
            item = self.get(plugin_id)
            item.update(enabled=enabled, revision=item['revision'] + 1)
            return self._save(item)

    def delete(self, plugin_id):
        with self.lock:
            self._path(plugin_id).unlink()

    def allowed(self, identity, scope_id):
        return (isinstance(identity, dict) and identity.get('platform') == 'local'
                and str(scope_id).startswith('web:') and self.computer.allowed(identity, scope_id))

    def submit(self, plugin_id, arguments, scope_id, user_id, identity, *, source_ids=None):
        if not self.allowed(identity, scope_id): raise PermissionError('插件只能由已开启电脑能力的本地聊天管理员运行')
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 12000:
            raise ValueError('插件输入需为12000字以内的 JSON 对象')
        with self.lock:
            item = self.get(plugin_id)
            if not item['enabled']: raise ValueError('插件尚未启用，请先在插件页核对代码后启用')
            if item['schema_version'] == 2:
                from .tool_actions import validate_arguments
                validate_arguments(item['manifest']['input_schema'], arguments)
            permissions = dict(self.computer.settings.get()['computer'])
            job = self.runs.submit('plugin', {'id': plugin_id, 'input': arguments, 'revision': item['revision'],
                'sha256': item['sha256'], 'source_ids': source_ids or [], 'permissions': permissions},
                identity=identity, scope_id=scope_id, user_id=user_id, title='插件：' + item['name'])
            return job

    def guard(self, job):
        try:
            payload = job['_payload']
            # The worker receives a verified, frozen byte snapshot. Polling the
            # job guard must not re-read every resource on every process tick.
            item = self.get(payload['id'], verify_files=False)
            return (self.allowed(job['identity'], job['scope_id']) and self.computer.guard(job)
                    and item['enabled'] and item['revision'] == payload['revision'] and item['sha256'] == payload['sha256']
                    and payload['permissions'] == self.computer.settings.get()['computer'])
        except (OSError, ValueError, KeyError, TypeError): return False

    async def run(self, ctx, payload):
        async with self.computer._slot:
            ctx.check()
            item = self.get(payload['id'])
            root = self.computer.path(self.computer.workspace(), 'plugins/' + item['id'])
            root.mkdir(parents=True, exist_ok=True)
            # JSON strings are encoded as Python literals, never interpolated as
            # source fragments. All code runs only in the computer child worker.
            if item['schema_version'] == 2:
                files = self._files(item)
                packed = {name: base64.b64encode(raw).decode() for name, raw in files.items()}
                code = ('import base64, json, pathlib, runpy, sys\n'
                        + '_root = pathlib.Path.cwd() / ' + repr('_plugin_' + item['package_hash']) + '\n'
                        + '_root.mkdir(exist_ok=True)\n'
                        + 'if _root.is_symlink(): raise ValueError("插件运行目录不能是链接")\n'
                        + 'for _name, _raw in ' + repr(packed) + '.items():\n'
                        + '    _path = _root.joinpath(*_name.split("/"))\n'
                        + '    if any(p.is_symlink() for p in (_path, *_path.parents) if p != pathlib.Path.cwd()): raise ValueError("插件文件不能是链接")\n'
                        + '    _path.parent.mkdir(parents=True, exist_ok=True)\n'
                        + '    _path.write_bytes(base64.b64decode(_raw))\n'
                        + 'sys.path.insert(0, str(_root))\n'
                        + 'sys.path.insert(0, str((_root / ' + repr(item['manifest']['entrypoint']) + ').parent))\n'
                        + '_namespace = runpy.run_path(str(_root / ' + repr(item['manifest']['entrypoint']) + '))\n')
            else:
                code = ('import json\n_namespace = {}\nexec(compile(' + repr(item['code']) + ", '<plugin>', 'exec'), _namespace)\n")
            code += ('_result = _namespace["run"](json.loads(' + repr(json.dumps(payload['input'], ensure_ascii=False)) + '))\n'
                     + 'print(json.dumps(_result, ensure_ascii=False, allow_nan=False))\n')
            ctx.progress(stage='executing', message='正在运行插件：' + item['name'])
            output = await self.computer.execute(ctx, root, 'execute_python', {'code': code})
            ctx.check()
            if not output['ok']: raise ValueError('插件执行失败，请查看任务日志：' + output.get('output', '')[-3000:])
            return {'reply': output.get('output', '').strip() or '插件执行完成，无文本输出。', 'execution': output, 'plugin_id': item['id']}


def install_plugins(app, data_dir, chat):
    service = PluginService(data_dir, app.state.computer)
    app.state.plugins = chat.plugins = service

    def admin():
        if not app.state.access.is_admin(app.state.access.identity('local', 'shiori')):
            raise HTTPException(403, '当前本地账号没有管理员权限')

    @app.get('/api/plugins')
    async def listing():
        admin()
        return {'items': await asyncio.to_thread(service.list), 'runtime': service.computer.settings.get()['computer']}

    @app.post('/api/plugins/import')
    async def inspect_upload(file: UploadFile = File(...)):
        admin()
        chunks, size = [], 0
        try:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE: raise ValueError('插件压缩包最多8MB')
                chunks.append(chunk)
            return await asyncio.to_thread(service.scan, file.filename or '', b''.join(chunks))
        finally: await file.close()

    @app.post('/api/plugins/import/install')
    async def install_upload(payload: dict = Body(...)):
        admin()
        if not all(isinstance(payload.get(k), str) for k in ('scan_id', 'candidate_id')):
            raise ValueError('请选择已预览的插件候选')
        return await asyncio.to_thread(service.install_scan, payload['scan_id'], payload['candidate_id'], replace=payload.get('replace', False))

    @app.get('/api/plugins/{plugin_id}')
    async def detail(plugin_id: str):
        admin()
        return await asyncio.to_thread(service.get, plugin_id)

    @app.get('/api/plugins/{plugin_id}/export')
    async def export(plugin_id: str):
        admin()
        raw = await asyncio.to_thread(service.export, plugin_id)
        return Response(raw, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{plugin_id}.zip"'})

    @app.post('/api/plugins/{plugin_id}/rollback')
    async def rollback(plugin_id: str):
        admin()
        return await asyncio.to_thread(service.rollback, plugin_id)

    @app.post('/api/plugins')
    async def save(payload: dict = Body(...)):
        admin()
        return await asyncio.to_thread(service.save, payload)

    @app.put('/api/plugins/{plugin_id}/enabled')
    async def enabled(plugin_id: str, payload: dict = Body(...)):
        admin()
        return await asyncio.to_thread(service.activate, plugin_id, payload.get('enabled'))

    @app.delete('/api/plugins/{plugin_id}')
    async def delete(plugin_id: str):
        admin()
        await asyncio.to_thread(service.delete, plugin_id)
        return {'ok': True}

    @app.post('/api/plugins/{plugin_id}/run')
    async def run(plugin_id: str, payload: dict = Body(...)):
        admin()
        scope_id = payload.get('scope_id', 'web:default')
        return service.submit(plugin_id, payload.get('input', {}), scope_id, 'shiori', app.state.access.identity('local', 'shiori'))
