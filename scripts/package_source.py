"""按白名单生成源码发行包，不遍历虚拟环境或用户数据目录。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import zipfile


ROOT_FILES = {
    '.gitignore', 'README.md', 'RELEASE_NOTES.md', 'TEST_REPORT.md', 'CONTRACT.md',
    'THIRD_PARTY_NOTICES.md', 'LICENSE',
    'pyproject.toml', 'requirements.txt', 'requirements-dev.txt', 'requirements-lock.txt', 'requirements-server.txt', 'requirements-desktop.txt',
    'Dockerfile', 'compose.yaml', 'compose.low-memory.yaml', '.dockerignore',
    'start.ps1', 'start.sh', 'launcher.py', 'installer_launcher.py',
}
TREE_TYPES = {
    '.github/workflows': {'.yml', '.yaml'},
    'qunyu': {'.py'},
    'mikubot': {'.py'},
    'shiori': {'.py'},
    'docs': {'.md', '.png', '.jpg', '.jpeg', '.svg'},
    'evals': {'.json'},
    'tests': {'.py', '.cjs', '.json'},
    'scripts': {'.py', '.ps1', '.sh'},
    # Visit only this reviewed dependency source, never arbitrary third-party data.
    'third_party/pystray': {'.py', '.md', '.json', '.txt'},
}
EXCLUDED_DIRS = {'data', 'build', 'dist', '__pycache__', 'node_modules', 'licenses', 'desktop-profile'}
EXCLUDED_NAMES = {'settings.json', 'admin.token', 'server.json', 'jobs.json', 'persona_knowledge.json', 'webui-password.json'}
RESOURCE_NOTICES = {'LICENSE', 'NOTICE', 'COPYING', 'LICENSE.txt', 'NOTICE.txt'}
STATIC_TYPES = {'.html', '.js', '.css', '.md', '.svg', '.png', '.jpg', '.jpeg', '.ico'}


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(),'st_file_attributes',0) & 0x400)


def _excluded_name(name: str) -> bool:
    folded = name.casefold()
    stem = Path(folded).stem
    return (folded in EXCLUDED_NAMES or folded.startswith('.')
            or stem in {'secret', 'secrets', 'credential', 'credentials', 'password', 'passwords', 'client_secret'}
            or folded.endswith(('.token', '.key', '.pem', '.pfx', '.log', '.pyc', '.db', '.sqlite', '.sqlite3')))


def release_version(root: Path) -> str:
    tree = ast.parse((root / 'shiori' / '__init__.py').read_text('utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == '__version__' for x in node.targets):
            value = ast.literal_eval(node.value)
            import re
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?', value):
                return value
    raise ValueError('源码版本须为 major.minor.patch 格式，可附加预发行标识')


def source_files(root: Path) -> list[Path]:
    root = root.resolve()
    selected = []
    for name in sorted(ROOT_FILES):
        path = root / name
        if not path.is_file():
            raise ValueError(f'源码发行缺少必要文件：{name}')
        if _linked(path):
            raise ValueError(f'源码发行不能包含文件链接：{name}')
        selected.append(path)
    # Only explicit trees are visited. In particular, root/.venv is never walked.
    for tree, types in TREE_TYPES.items():
        base = root / tree
        if not base.is_dir() or _linked(base):
            raise ValueError(f'源码目录缺少或包含链接：{tree}')
        for directory, names, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            kept = []
            for name in names:
                if name.startswith('.') or name.casefold() in EXCLUDED_DIRS:
                    continue
                child = parent / name
                if _linked(child):
                    raise ValueError(f'源码发行不能包含目录链接：{child.relative_to(root)}')
                kept.append(name)
            names[:] = sorted(kept)
            for name in sorted(files):
                if _excluded_name(name):
                    continue
                path = parent / name
                relative = path.relative_to(base)
                resource = tree == 'shiori' and relative.parts[0] == 'resources'
                static = tree == 'shiori' and relative.parts[0] == 'static'
                allowed = (path.suffix.casefold() in types
                           or (static and path.suffix.casefold() in STATIC_TYPES)
                           or (resource and (path.suffix.casefold() in {'.md', '.ps1', '.cmd'} or name in RESOURCE_NOTICES))
                           or (tree == 'third_party/pystray' and name in {'COPYING', 'COPYING.LGPL'}))
                if not allowed:
                    continue
                if _linked(path) or not path.is_file() or not path.resolve().is_relative_to(root):
                    raise ValueError(f'源码发行不能包含外部文件：{path.relative_to(root)}')
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError(f'源码文件过大，请检查是否混入用户资料：{path.relative_to(root)}')
                selected.append(path)
    if len(selected) > 2000:
        raise ValueError('源码文件数异常，请检查是否混入用户资料')
    return sorted(selected, key=lambda value: value.relative_to(root).as_posix())


def package(root: Path, archive: Path | None = None) -> dict:
    root = Path(root).resolve()
    version = release_version(root)
    prefix = f'shiori-{version}-source'
    archive = Path(archive).resolve() if archive else root / 'dist' / f'{prefix}.zip'
    if archive.suffix.casefold() != '.zip':
        raise ValueError('源码包输出路径必须为 ZIP')
    if any(archive.is_relative_to(root / name) for name in TREE_TYPES) or archive in {root / name for name in ROOT_FILES}:
        raise ValueError('源码包不能写入源码白名单目录或覆盖源码文件')
    files = source_files(root)
    manifest = {'version': version, 'format': 'shiori-source-v1', 'files': {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for path in files:
                output.write(path, prefix + '/' + path.relative_to(root).as_posix())
            output.writestr(prefix + '/SOURCE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise ValueError('源码压缩包校验失败')
            for path, digest in manifest['files'].items():
                if hashlib.sha256(check.read(prefix + '/' + path)).hexdigest() != digest:
                    raise ValueError('源码压缩包文件校验失败：' + path)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.sha256').write_text(digest + '  ' + archive.name + '\n', 'utf-8')
    return {'archive': str(archive), 'version': version, 'files': len(files) + 1,
            'bytes': archive.stat().st_size, 'sha256': digest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成不包含用户数据和运行环境的 Shiori 源码包')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(json.dumps(package(args.root, args.output), ensure_ascii=False))
