"""Keep personal data outside application files, with a non-destructive v0.8 migration."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .filesystem import is_link


def default_data_dir(*, environ=None, platform=None, home=None) -> Path:
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    if platform == 'win32':
        parent = Path(env.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
    elif platform == 'darwin':
        parent = home / 'Library' / 'Application Support'
    else:
        parent = Path(env.get('XDG_DATA_HOME') or home / '.local' / 'share')
    return (parent / 'shiori' / 'data').resolve()


def application_base() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent


def _nonempty(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is not None


def _check_regular_tree(source: Path):
    if is_link(source):
        raise OSError('旧数据目录包含链接。请用 --data-dir 明确指定原目录，或先整理数据后迁移。')
    for entry in source.rglob('*'):
        if is_link(entry) or not (entry.is_file() or entry.is_dir()):
            raise OSError('旧数据包含链接或特殊文件，自动迁移已停止，原文件保持原样。请用 --data-dir 指定原目录。')


def resolve_data_dir(explicit=None, *, base=None, migrate=True) -> Path:
    """Resolve an explicit directory or copy legacy data once; never merge/overwrite it."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    target = default_data_dir()
    # Existing independent data remains authoritative. Reusing it avoids a
    # divergent copy and preserves DPAPI credentials and the cross-version lock.
    if not _nonempty(target):
        for previous in ('mikubot', 'qunyu'):
            old_default = target.parent.parent / previous / 'data'
            if _nonempty(old_default):
                return old_default.resolve()
    legacy = (Path(base) if base is not None else application_base()) / 'data'
    legacy = legacy.resolve()
    if legacy == target or _nonempty(target) or not _nonempty(legacy):
        return target
    if not migrate:
        return legacy
    from .startup import instance_details, data_guard
    # The old launcher uses the same data-specific mutex. It cannot begin writing
    # halfway through this copy, and a running instance must be stopped first.
    with data_guard(legacy):
        if instance_details(legacy, timeout=.7):
            raise OSError('旧版 Shiori 正在使用安装目录内的数据。请先退出旧版，再启动以迁移；原数据不会被删除。')
        _check_regular_tree(legacy)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / ('.data-migrate-' + uuid.uuid4().hex)
        try:
            shutil.copytree(legacy, staging)
            # A PID/port from a stopped old process is never reused as live state.
            (staging / 'server.json').unlink(missing_ok=True)
            (staging / 'migration-v0.9.json').write_text(json.dumps({
                'source': str(legacy), 'migrated_at': datetime.now(timezone.utc).isoformat(),
                'original_preserved': True,
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            if target.exists():
                # rmdir only succeeds for an empty destination, including races.
                target.rmdir()
            staging.rename(target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    return target
