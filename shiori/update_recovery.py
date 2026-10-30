"""Reconcile abandoned update workers without accepting a mixed installation."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re

from . import __version__
from .data_paths import application_base
from .update_runtime import (RECEIPT, atomic_json, destination, digest, read_record,
                             recovery_journal, regular, worker_running)

ACTIVE = {'preparing', 'backing_up', 'replacing', 'restarting', 'recovering', 'unknown'}
RECOVERY = 'recovery_required'


def outcome(manager):
    try:
        record = read_record(manager.folder / 'install-result.json', 4096)
        if record.get('download_id') == manager.download.get('id'):
            return record
    except (OSError, ValueError, TypeError):
        pass
    return {}


def context(manager):
    identifier = manager.download.get('id', '')
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('更新恢复编号无效')
    work = regular(manager.folder / identifier)
    cfg = read_record(work / 'switch.json', 32768)
    target = regular(Path(cfg['target']).resolve())
    data = regular(Path(cfg['data']).resolve())
    if (data != manager.data_dir or target != application_base().resolve()
            or cfg.get('download_id') != identifier or work != data / 'updates' / identifier
            or target == data or target.is_relative_to(data)):
        raise ValueError('更新恢复目录与当前程序不一致')
    if cfg.get('service') and not re.fullmatch(r'(?:shiori|mikubot)(?:@[a-zA-Z0-9_-]+)?\.service', cfg['service']):
        raise ValueError('更新恢复服务无效')
    return work, target, data, cfg


def _write(manager, raw, state, message):
    result = {**raw, 'status': state, 'message': message,
              'at': datetime.now(timezone.utc).isoformat()}
    if state == RECOVERY:
        result['interrupted_phase'] = raw.get('interrupted_phase', raw.get('status'))
    atomic_json(manager.folder / 'install-result.json', result)
    work = regular(manager.folder / manager.download['id'])
    if work.is_dir():
        atomic_json(work / 'outcome.json', result)
    return result


def inspect_interruption(manager):
    raw = outcome(manager)
    state = raw.get('status')
    if state not in ACTIVE | {RECOVERY}:
        return {}
    work = regular(manager.folder / manager.download['id'])
    # New workers publish their status only while holding this kernel lease.
    # Old workers have no lease; a timeout alone cannot authorize file writes.
    owned = raw.get('lease_version') == 1
    if owned and work.is_dir() and worker_running(work):
        return {}
    if not owned:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(raw['at'])).total_seconds()
        except (ValueError, KeyError, TypeError):
            age = 1801
        if state != RECOVERY and age < 1800:
            return {}
        message = '旧更新记录无法确认助手状态。请先确认旧助手已退出，再使用匹配版本覆盖程序或从更新目录备份恢复；当前数据已保留。'
        _write(manager, raw, RECOVERY, message)
        return {'message': message, 'can_rollback': False, 'legacy': True}
    try:
        work, target, data, cfg = context(manager)
        journal_path = regular(work / 'program-backup' / 'journal.json')
        phase = raw.get('interrupted_phase', state)
        if phase in {'preparing', 'backing_up'} and not journal_path.exists():
            _write(manager, raw, 'failed', '上次更新助手已退出，程序尚未替换；已恢复正常运行，可重新更新')
            return {}
        # A crash after startup but before its final receipt must not keep the
        # healthy new application gated forever. Check the complete manifest.
        if phase in {'restarting', 'replacing'}:
            try:
                receipt = read_record(target / RECEIPT)
            except (OSError, ValueError):
                receipt = {}
            files = receipt.get('files')
            if (receipt.get('version') == cfg.get('version') == __version__
                    and receipt.get('sha256') == cfg.get('sha256')
                    and isinstance(files, dict) and 0 < len(files) <= 50000
                    and any(name in files for name in ('shiori.exe', 'shiori/__main__.py'))
                    and all(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha)
                            and destination(target, name, data).is_file()
                            and digest(target / name) == sha for name, sha in files.items())):
                _write(manager, raw, 'completed', '更新助手已退出；已重新核验完整程序文件，当前版本恢复正常运行')
                return {}
        recovery_journal(work, target, data)
        can_rollback = True
        message = '上次更新意外中断，程序完整性尚未确认。可以恢复更新前的程序和数据后重新启动；备份已保留。'
    except (OSError, ValueError, TypeError, KeyError):
        # Failure to prove integrity never becomes an instruction to erase the
        # journal or let a half-replaced program start accepting conversations.
        can_rollback = False
        message = '上次更新意外中断，恢复记录或程序文件尚未核验。请保留 updates 中的程序和数据备份，使用对应版本修复程序后重新核验。'
    _write(manager, raw, RECOVERY, message)
    return {'message': message, 'can_rollback': can_rollback, 'legacy': False}


def recovery_config(manager, download_id):
    raw = outcome(manager)
    if (download_id != manager.download.get('id') or raw.get('status') != RECOVERY
            or raw.get('lease_version') != 1):
        raise ValueError('当前更新记录不支持自动恢复，请重新核验')
    work, target, data, cfg = context(manager)
    if worker_running(work):
        raise ValueError('更新助手仍在运行，不能同时恢复')
    recovery_journal(work, target, data)
    if not isinstance(cfg.get('launch'), list) or not cfg['launch'] or not all(isinstance(x, str) for x in cfg['launch']):
        raise ValueError('缺少更新前的启动参数')
    return work, target, {**cfg, 'parent_pid': os.getpid(), 'recover': True}
