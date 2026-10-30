"""Inventory and explicit cleanup of completed update artifacts only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .filesystem import is_link

ID = re.compile(r'[a-f0-9]{32}')
TERMINAL = {'ready', 'failed', 'cancelled', 'interrupted', 'completed'}


def _record(path):
    if is_link(path) or not path.is_file() or path.stat().st_size > 16384:
        return {}
    try:
        value = json.loads(path.read_text('utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _measure(folder, root):
    """Reject links before any delete; include metadata to invalidate stale previews."""
    digest = hashlib.sha256()
    total = backup = count = 0
    for directory, dirs, files in os.walk(folder, followlinks=False):
        dirs.sort()
        current = Path(directory)
        for name in sorted([*dirs, *files]):
            path = current / name
            count += 1
            if count > 100000:
                raise ValueError('文件过多，请手动核对')
            if is_link(path) or not path.resolve().is_relative_to(root):
                raise ValueError('含链接或越界路径，不能自动清理')
            stat = path.stat()
            relative = path.relative_to(folder)
            digest.update(f'{relative.as_posix()}:{stat.st_size}:{stat.st_mtime_ns}\n'.encode())
            if path.is_file():
                total += stat.st_size
                if relative.parts[0] in ('data-backup', 'program-backup'):
                    backup += stat.st_size
    return total, backup, digest.hexdigest()


def inventory(manager):
    root = manager._safe_folder().resolve()
    candidates = []
    for folder in root.iterdir():
        if not ID.fullmatch(folder.name):
            continue
        item = {'id': folder.name, 'bytes': 0, 'backup_bytes': 0, 'can_delete': False, 'reason': '', 'tag': ''}
        if is_link(folder) or not folder.is_dir() or folder.resolve().parent != root:
            item['reason'] = '非普通更新目录，保留以供核对'
            candidates.append(item)
            continue
        artifact = _record(folder / 'update-artifact.json')
        outcome = _record(folder / 'outcome.json')
        if outcome.get('download_id') != folder.name:
            outcome = {}
        item['tag'] = str(artifact.get('tag', ''))[:80]
        item['status'] = outcome.get('status') or artifact.get('status') or 'unknown'
        item['at'] = str(outcome.get('at') or artifact.get('started_at') or '')[:80]
        try:
            item['bytes'], item['backup_bytes'], item['fingerprint'] = _measure(folder, root)
        except (OSError, ValueError):
            item['reason'] = '目录含链接、变化或文件过多，保留以供核对'
        if not item['reason']:
            if folder.name == manager.download.get('id'):
                item['reason'] = '当前下载或最近一次更新，暂时保留'
            elif artifact.get('id') != folder.name:
                item['reason'] = '旧目录缺少管理记录，保留以供核对'
            elif item['status'] not in TERMINAL:
                item['reason'] = '任务未明确结束，保留以供核对'
            elif (folder / 'switch.json').exists() and outcome.get('status') != 'completed':
                item['reason'] = '更新恢复结果未确认，保留程序和数据备份'
            else:
                item['can_delete'] = True
        candidates.append(item)
    completed = [item for item in candidates if item.get('status') == 'completed' and item.get('backup_bytes')]
    if completed:
        latest = max(completed, key=lambda item: (item.get('at', ''), item['id']))
        latest.update(can_delete=False, reason='最近一次成功更新的恢复备份，保留')
    return {'items': sorted(candidates, key=lambda item: (item.get('at', ''), item['id']), reverse=True),
            'total_bytes': sum(item['bytes'] for item in candidates),
            'reclaimable_bytes': sum(item['bytes'] for item in candidates if item['can_delete'])}


def cleanup(manager, selected):
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
        raise ValueError('请选择 1—100 个历史更新目录')
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not ID.fullmatch(item['id'])
           or not isinstance(item.get('fingerprint'), str) for item in selected):
        raise ValueError('更新目录参数无效')
    if len({item['id'] for item in selected}) != len(selected):
        raise ValueError('不能重复选择同一更新目录')
    current = {item['id']: item for item in inventory(manager)['items']}
    for item in selected:
        actual = current.get(item['id'])
        if not actual or not actual['can_delete'] or actual.get('fingerprint') != item['fingerprint']:
            raise ValueError('更新目录状态已改变或仍受保护，请重新读取')
    root = manager._safe_folder().resolve()
    deleted, released = [], 0
    for item in selected:
        target = root / item['id']
        if is_link(target) or target.resolve().parent != root:
            raise ValueError('更新清理路径无效')
        if _measure(target, root)[2] != item['fingerprint']:
            raise ValueError('更新目录内容已变化，未继续清理')
        # The exact resolved target and all descendants have been checked above.
        shutil.rmtree(target)
        deleted.append(item['id'])
        released += current[item['id']]['bytes']
    return {'deleted': deleted, 'released_bytes': released}
