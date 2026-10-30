import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shiori import __version__, update_recovery as recovery, update_runtime as worker
from shiori.updates import UpdateManager


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    target, data = tmp_path / 'program', tmp_path / 'data'
    target.mkdir(); data.mkdir()
    ident = 'c' * 32
    work = data / 'updates' / ident
    work.mkdir(parents=True)
    config = {'target': str(target), 'data': str(data), 'download_id': ident,
              'version': __version__, 'sha256': 'd' * 64, 'parent_pid': 42,
              'port': 18765, 'launch': ['isolated-fixture'], 'kind': 'portable'}
    worker.atomic_json(work / 'switch.json', config)
    record = {'id': ident, 'tag': 'v' + __version__, 'kind': 'portable',
              'name': f'shiori-{__version__}-Windows-x64.zip', 'status': 'installing'}
    worker.atomic_json(data / 'updates' / 'state.json', record)
    worker.atomic_json(data / 'updates' / 'install-result.json', {
        'download_id': ident, 'status': 'backing_up', 'lease_version': 1,
        'at': '2025-01-01T00:00:00+00:00', 'message': 'fixture'})
    monkeypatch.setattr(recovery, 'application_base', lambda: target)
    manager = UpdateManager(data, runtime={'mode': 'source', 'install_supported': False})
    return manager, target, data, work, config


def change_phase(manager, phase, **values):
    raw = recovery.outcome(manager)
    worker.atomic_json(manager.folder / 'install-result.json', {**raw, 'status': phase, **values})


def partial_program(target, data, work):
    (target / 'shiori.exe').write_bytes(b'partially-replaced')
    backup = work / 'program-backup'; backup.mkdir()
    (backup / 'shiori.exe').write_bytes(b'old-version')
    worker.atomic_json(backup / 'journal.json', {'schema': 2, 'changed': ['shiori.exe'], 'created': ['new.txt'], 'files': {},
                                               'backup_hashes': {'shiori.exe': worker.digest(backup / 'shiori.exe')}})
    (target / 'new.txt').write_text('new version only')
    saved = work / 'data-backup'; saved.mkdir()
    (saved / 'qunyu.db').write_bytes(b'old-data')
    worker.atomic_json(work / 'data-backup-manifest.json', {'schema': 1, 'files': {'qunyu.db': worker.digest(saved / 'qunyu.db')}})
    (data / 'qunyu.db').write_bytes(b'new-data')
    (data / 'new-file.txt').write_text('new-data only')


def test_dead_worker_before_replacement_releases_gate(interrupted):
    manager, *_ = interrupted
    assert manager.startup_pending()
    asyncio.run(manager.reconcile_startup())
    assert not manager.startup_pending()
    assert manager.snapshot()['install_result']['status'] == 'failed'
    assert manager.snapshot()['recovery'] == {}


def test_live_kernel_lease_never_cleared_even_with_ancient_timestamp(interrupted):
    manager, _, _, work, _ = interrupted
    with worker.worker_guard(work):
        asyncio.run(manager.reconcile_startup(force=True))
        assert manager.startup_pending()
        assert manager.snapshot()['automatic_active']
        assert not manager._recovery
    asyncio.run(manager.reconcile_startup(force=True))
    assert not manager.startup_pending()


def test_complete_new_program_is_verified_before_gate_is_released(interrupted):
    manager, target, _, _, config = interrupted
    content = b'complete fixture'
    (target / 'shiori.exe').write_bytes(content)
    worker.atomic_json(target / worker.RECEIPT, {'version': __version__, 'sha256': config['sha256'],
                      'files': {'shiori.exe': hashlib.sha256(content).hexdigest()}})
    change_phase(manager, 'restarting')
    asyncio.run(manager.reconcile_startup())
    assert not manager.startup_pending()
    assert manager.snapshot()['install_result']['status'] == 'completed'


def test_partial_program_stays_gated_with_actionable_recovery(interrupted):
    manager, target, data, work, _ = interrupted
    partial_program(target, data, work)
    change_phase(manager, 'replacing')
    asyncio.run(manager.reconcile_startup())
    state = manager.snapshot()
    assert manager.startup_pending() and not state['automatic_active']
    assert state['install_result']['status'] == 'recovery_required'
    assert state['recovery']['can_rollback'] is True
    with pytest.raises(ValueError, match='恢复'):
        manager.start_download(12, 'portable')
    assert (data / 'qunyu.db').read_bytes() == b'new-data'


def test_corrupt_backup_is_never_offered_for_automatic_rollback(interrupted):
    manager, target, data, work, _ = interrupted
    partial_program(target, data, work)
    (work / 'program-backup' / 'shiori.exe').unlink()
    change_phase(manager, 'replacing')
    asyncio.run(manager.reconcile_startup())
    assert manager.startup_pending()
    assert not manager._recovery['can_rollback']


def test_legacy_worker_is_not_declared_dead_or_rolled_back_by_age_alone(interrupted):
    manager, *_ = interrupted
    change_phase(manager, 'restarting', lease_version=None)
    asyncio.run(manager.reconcile_startup())
    assert manager.startup_pending()
    assert manager._recovery['legacy'] and not manager._recovery['can_rollback']
    assert not manager.snapshot()['automatic_active']
    with pytest.raises(ValueError, match='不支持自动恢复'):
        recovery.recovery_config(manager, manager.download['id'])


def test_worker_can_restore_partial_program_and_data_after_shutdown(interrupted, monkeypatch):
    manager, target, data, work, config = interrupted
    partial_program(target, data, work)
    change_phase(manager, 'replacing')
    asyncio.run(manager.reconcile_startup())
    _, _, config = recovery.recovery_config(manager, manager.download['id'])
    worker.atomic_json(work / 'recover.json', config)
    order = []
    monkeypatch.setattr(worker, 'wait_parent', lambda *args, **kwargs: order.append('exited'))
    def launch(*args, **kwargs):
        assert order == ['exited']
        assert (target / 'shiori.exe').read_bytes() == b'old-version'
        assert (data / 'qunyu.db').read_bytes() == b'old-data'
        assert not (target / 'new.txt').exists()
        assert not (data / 'new-file.txt').exists()
        order.append('restarted')
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    assert worker.run(work / 'recover.json') is True
    assert order == ['exited', 'restarted']
    assert recovery.outcome(manager)['status'] == 'failed'
    assert (work / 'data-backup' / 'qunyu.db').read_bytes() == b'old-data'


def test_journal_traversal_is_rejected_before_any_restore(interrupted):
    manager, target, data, work, _ = interrupted
    partial_program(target, data, work)
    worker.atomic_json(work / 'program-backup' / 'journal.json',
                       {'schema': 2, 'changed': [], 'created': ['../outside'], 'files': {}, 'backup_hashes': {}})
    change_phase(manager, 'replacing')
    asyncio.run(manager.reconcile_startup())
    assert not manager._recovery['can_rollback']
    assert (data / 'qunyu.db').read_bytes() == b'new-data'


@pytest.mark.parametrize('path', ['program-backup/shiori.exe', 'data-backup/qunyu.db'])
def test_truncated_backup_never_overwrites_current_files(interrupted, path):
    manager, target, data, work, _ = interrupted
    partial_program(target, data, work)
    (work / path).write_bytes(b'')
    change_phase(manager, 'replacing')
    asyncio.run(manager.reconcile_startup())
    assert not manager._recovery['can_rollback']
    assert (data / 'qunyu.db').read_bytes() == b'new-data'


def test_unknown_health_result_keeps_gate_and_offers_verified_backup(interrupted):
    manager, target, data, work, _ = interrupted
    partial_program(target, data, work)
    change_phase(manager, 'unknown')
    assert manager.startup_pending()
    asyncio.run(manager.reconcile_startup())
    assert manager.startup_pending()
    assert manager._recovery['can_rollback']
