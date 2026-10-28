"""Bounded, non-executing inspection of portable local plugin packages."""
from __future__ import annotations

import ast
import hashlib
import gzip
import io
import json
from pathlib import PurePosixPath
import re
import stat
import tarfile
import unicodedata
import zipfile
import zlib

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_FILES = 128
MAX_SOURCE_CHARS = 10000
MAX_AST_NODES = 8000
MAX_AST_DEPTH = 100
# Includes TAR headers, PAX/GNU extension records and block padding, not just
# extracted regular files. Metadata must not bypass the uncompressed limit.
MAX_TAR_BYTES = MAX_TOTAL + 1024 * 1024
DEFAULT_INPUT = {'type': 'object', 'properties': {}, 'additionalProperties': True}


def safe_name(value):
    """Use the same safe portable names on Windows, Linux, and export."""
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('压缩包路径无效，不支持反斜杠或空路径')
    value = unicodedata.normalize('NFC', value.rstrip('/'))
    parts = value.split('/')
    if (value.startswith('/') or len(parts) > 10 or len(value) > 220
            or any(not p or p in ('.', '..') or any(c in p for c in ':*?<>|"') or p.endswith((' ', '.'))
                   or any(ord(c) < 32 for c in p)
                   or re.match(r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', p, re.I)
                   for p in parts)):
        raise ValueError('压缩包包含不安全或不兼容的路径：' + value[:120])
    return '/'.join(parts)


def parse_source(raw, path='main.py'):
    """Inspect bounded syntax without evaluating it, including auxiliary files.

    A short left-associated expression can overflow CPython's AST constructor.
    Character limits alone therefore do not make parsing or later compilation
    safe. Turn parser recursion into a candidate-local validation failure, and
    bound successfully parsed trees before any runtime can compile them.
    """
    try:
        code = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
        if not isinstance(code, str) or len(code) > MAX_SOURCE_CHARS:
            raise ValueError(f'{path}：Python 源码最多10000字，请拆分为多个辅助模块')
        tree = ast.parse(code, filename=path)
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError(f'{path}：Python 语法或编码错误，第{getattr(exc, "lineno", "?")}行：{exc}') from exc
    except RecursionError as exc:
        raise ValueError(f'{path}：Python 表达式或嵌套过于复杂，请拆分长表达式或辅助模块') from exc
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > MAX_AST_DEPTH or count > MAX_AST_NODES:
            raise ValueError(f'{path}：Python 结构过于复杂，第{getattr(node, "lineno", "?")}行附近；请拆分长表达式或辅助模块')
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return code, tree


def validate_code(raw, path='main.py'):
    code, tree = parse_source(raw, path)
    if not any(isinstance(n, ast.FunctionDef) and n.name == 'run' and len(n.args.args) == 1
               and not n.args.vararg and not n.args.kwarg and not n.args.kwonlyargs
               and not n.args.posonlyargs for n in tree.body):
        raise ValueError(f'{path} 需定义同步函数 run(input)，接受一个参数')
    return code


def validate_package(manifest, files):
    from .tool_actions import bounded_schema
    if not isinstance(manifest, dict) or len(json.dumps(manifest, ensure_ascii=False)) > 20000:
        raise ValueError('plugin.json 必须是20000字以内的 JSON 对象')
    allowed = {'schema_version', 'id', 'name', 'description', 'version', 'entrypoint', 'input_schema',
               'capabilities', 'requires_python', 'dependencies'}
    if set(manifest) - allowed:
        raise ValueError('plugin.json 存在不支持的字段：' + ', '.join(sorted(set(manifest) - allowed)))
    item = dict(manifest)
    if type(item.get('schema_version')) is not int or item['schema_version'] != 1:
        raise ValueError('plugin.json 的 schema_version 必须为1')
    if not isinstance(item.get('id'), str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,47}', item['id']):
        raise ValueError('插件 ID 需为2—48位小写字母、数字或下划线，并以字母开头')
    for key, limit in (('name', 80), ('description', 1000), ('version', 40)):
        if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > limit:
            raise ValueError(f'plugin.json 的 {key} 需填写1—{limit}字')
    entry = safe_name(item.get('entrypoint', 'main.py'))
    if not entry.endswith('.py') or entry not in files:
        raise ValueError('入口文件不存在或不是 Python 文件：' + entry)
    item['entrypoint'] = entry
    item['input_schema'] = bounded_schema(item.get('input_schema', DEFAULT_INPUT))
    capabilities = item.get('capabilities', [])
    if not isinstance(capabilities, list) or len(capabilities) > 20 or any(not isinstance(x, str) or len(x) > 80 for x in capabilities):
        raise ValueError('capabilities 需为最多20项的说明列表；声明不会授予权限')
    dependencies = item.get('dependencies', [])
    if not isinstance(dependencies, list) or len(dependencies) > 30 or any(not isinstance(x, str) or len(x) > 120 for x in dependencies):
        raise ValueError('dependencies 需为最多30项的依赖说明列表；不会自动安装')
    if 'requires_python' in item and (not isinstance(item['requires_python'], str) or len(item['requires_python']) > 80):
        raise ValueError('requires_python 格式无效')
    code = validate_code(files[entry], entry)
    # Validate every Python source without importing or executing any of them.
    for name, raw in files.items():
        if name.endswith('.py') and name != entry:
            parse_source(raw, name)
    warnings = []
    if 'README.md' not in files: warnings.append('没有 README.md，导出时会补充基础说明')
    if dependencies or 'requirements.txt' in files: warnings.append('包含第三方依赖声明，不会自动安装；运行环境缺少依赖时任务会失败')
    if capabilities: warnings.append('能力声明仅用于说明，实际权限由电脑能力设置决定')
    return item, code, warnings


def inspect_archive(filename, raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARCHIVE:
        raise ValueError('插件压缩包需为8MB以内的非空文件')
    files, names, total = {}, {}, 0

    def add(name, size, reader, directory=False):
        nonlocal total
        path = safe_name(name)
        folded = path.casefold()
        if folded in names:
            raise ValueError('压缩包包含重复或大小写冲突的路径：' + path)
        if len(names) >= MAX_FILES: raise ValueError('压缩包文件及目录总数不能超过128项')
        # A file cannot also be the parent of another member.
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.' and names.get(str(parent).casefold()) == 'file':
                raise ValueError('压缩包文件与目录路径冲突：' + path)
        if not directory and any(n.startswith(folded + '/') for n in names):
            raise ValueError('压缩包文件与目录路径冲突：' + path)
        names[folded] = 'dir' if directory else 'file'
        if directory:
            if size: raise ValueError('压缩包目录包含异常数据：' + path)
            return
        if size < 0 or size > MAX_FILE or total + size > MAX_TOTAL:
            raise ValueError('插件单个文件最多2MB，解包后总大小最多16MB')
        content = reader(MAX_FILE + 1)
        if len(content) != size: raise ValueError('压缩包文件大小与声明不符：' + path)
        total += size
        files[path] = content

    try:
        if str(filename).lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for info in archive.infolist():
                    # ZipInfo normalizes host separators and truncates NULs in
                    # filename; inspect the original header value first.
                    safe_name(info.orig_filename)
                    mode = info.external_attr >> 16
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                        raise ValueError('不支持链接、特殊文件或加密压缩包：' + info.filename[:120])
                    # Bound expanded bytes before reading, rather than guessing
                    # from ratios (valid repetitive assets compress extremely well).
                    with archive.open(info) as stream:
                        add(info.filename, info.file_size, stream.read, info.is_dir())
        elif str(filename).lower().endswith(('.tar.gz', '.tgz')):
            # tarfile consumes extension headers before yielding a member. A
            # compressed PAX/longname bomb can otherwise exhaust memory before
            # add() ever gets a chance to check that member's declared size.
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode='rb') as compressed:
                tar_bytes = compressed.read(MAX_TAR_BYTES + 1)
            if len(tar_bytes) > MAX_TAR_BYTES:
                raise ValueError('TAR 总解压大小超出上限（包含扩展头、元数据与填充）')
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as archive:
                for info in archive:
                    if not info.isfile() and not info.isdir():
                        raise ValueError('不支持链接或特殊文件：' + info.name[:120])
                    if info.isdir(): add(info.name, info.size, lambda size: b'', True)
                    else:
                        with archive.extractfile(info) as stream: add(info.name, info.size, stream.read)
        else: raise ValueError('请选择 .zip、.tar.gz 或 .tgz 插件包；暂不支持 RAR/7z')
    except (zipfile.BadZipFile, zipfile.LargeZipFile, tarfile.TarError, EOFError, OSError,
            RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ValueError('压缩包损坏、加密或格式不受支持，请重新导出') from exc
    candidates = []
    for path in sorted(name for name in files if PurePosixPath(name).name == 'plugin.json'):
        prefix = path[:-len('plugin.json')]
        subset = {name[len(prefix):]: content for name, content in files.items() if name.startswith(prefix)}
        candidate = {'candidate_id': hashlib.sha256(path.encode()).hexdigest()[:16], 'root': prefix.rstrip('/') or '.', 'files': subset}
        try:
            manifest, code, warnings = validate_package(json.loads(files[path].decode('utf-8-sig')), subset)
            candidate.update(manifest=manifest, code=code, warnings=warnings)
        except RecursionError:
            candidate['error'] = 'plugin.json：JSON 或参数定义嵌套过深，请简化结构'
        except (ValueError, UnicodeError, TypeError) as exc: candidate['error'] = str(exc)
        candidates.append(candidate)
    if not candidates:
        raise ValueError('没有找到 plugin.json。请使用规范插件包，或通过“用聊天创建”生成；单个 Python 文件可在高级编辑中保存')
    if len(candidates) > 20: raise ValueError('一个压缩包最多包含20个插件候选，请拆分导入')
    return candidates
