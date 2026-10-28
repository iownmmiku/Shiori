import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest

from shiori import installer

real_shortcuts = installer.create_shortcuts
real_running = installer.is_instance_running
real_guard = installer._acquire_application_guard


def test_installer_checks_independent_and_legacy_data_locations(tmp_path, monkeypatch):
    from shiori import data_paths
    personal = tmp_path / 'personal-data'
    monkeypatch.setattr(data_paths, 'default_data_dir', lambda: personal)
    examined = []
    def running(path):
        examined.append(path)
        return path == personal
    monkeypatch.setattr(installer, '_data_instance_running', running)
    assert real_running(tmp_path / 'program')
    assert examined == [(tmp_path / 'program' / 'data').resolve(), personal]


def test_installer_guards_release_legacy_guard_if_personal_data_guard_fails(tmp_path, monkeypatch):
    paths = [tmp_path / 'old-data', tmp_path / 'personal']
    released = []
    monkeypatch.setattr(installer, '_installation_data_locations', lambda target: paths)
    def acquire(path):
        if path == paths[1]: raise installer.InstallError('running personal data')
        return lambda: released.append(path)
    monkeypatch.setattr(installer, '_acquire_data_guard', acquire)
    if installer.os.name == 'nt':
        with pytest.raises(installer.InstallError, match='running personal'):
            real_guard(tmp_path / 'program')
        assert released == paths[:1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(installer, 'is_instance_running', lambda target: False)
    monkeypatch.setattr(installer, '_acquire_application_guard', lambda target: lambda: None)
    monkeypatch.setattr(installer, 'create_shortcuts', lambda *args: [])


def package(folder, files=None, extra=None, version='0.7.0'):
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / 'payload.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        for name, content in (files or {'shiori.exe': b'program', '_internal/runtime.bin': b'runtime'}).items():
            output.writestr('shiori/' + name, content)
        if extra:
            for name, content in extra:
                output.writestr(name, content)
    manifest = {'schema': 1, 'version': version, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'root': 'shiori', 'executable': 'shiori.exe'}
    return archive, manifest


def test_fresh_install_is_offline_and_stages_before_writing(tmp_path):
    payload, manifest = package(tmp_path / 'bundle')
    target = tmp_path / 'install'
    progress = []
    result = installer.install_payload(payload, manifest, target, progress=lambda value, text: progress.append(value))
    assert result['version'] == '0.7.0'
    assert (target / 'shiori.exe').read_bytes() == b'program'
    assert not (target / 'data').exists()
    assert progress == sorted(progress) and progress[-1] == 100
    record = json.loads((target / installer.INSTALL_RECORD).read_text('utf-8'))
    assert record['files']['shiori.exe'] == hashlib.sha256(b'program').hexdigest()
    assert not list(tmp_path.glob('.shiori-*'))


def test_upgrade_preserves_data_and_unknown_files_and_removes_only_owned_obsolete(tmp_path):
    payload, manifest = package(tmp_path / 'old', {'shiori.exe': b'old', '_internal/obsolete.bin': b'old lib', 'edited.txt': b'original'})
    target = tmp_path / 'install'
    installer.install_payload(payload, manifest, target)
    (target / 'data').mkdir()
    (target / 'data' / 'chat.db').write_bytes(b'precious')
    (target / 'custom.txt').write_bytes(b'user')
    (target / 'edited.txt').write_bytes(b'user modified')
    payload, manifest = package(tmp_path / 'new', version='0.7.1')
    installer.install_payload(payload, manifest, target)
    assert (target / 'data' / 'chat.db').read_bytes() == b'precious'
    assert (target / 'custom.txt').read_bytes() == b'user'
    assert (target / 'edited.txt').read_bytes() == b'user modified'
    assert not (target / '_internal/obsolete.bin').exists()
    assert (target / 'shiori.exe').read_bytes() == b'program'


def test_legacy_portable_upgrade_keeps_data_and_extra(tmp_path):
    target = tmp_path / 'portable'
    (target / 'data').mkdir(parents=True)
    (target / 'shiori.exe').write_bytes(b'legacy')
    (target / 'data' / 'config.json').write_bytes(b'original secrets')
    (target / 'local-note.txt').write_bytes(b'note')
    (target / 'README.md').write_bytes(b'personal same-name notes')
    payload, manifest = package(tmp_path / 'bundle', {'shiori.exe': b'new', 'README.md': b'program readme'})
    result = installer.install_payload(payload, manifest, target)
    assert (target / 'data' / 'config.json').read_bytes() == b'original secrets'
    assert (target / 'local-note.txt').read_bytes() == b'note'
    backup = Path(result['legacy_backup'])
    assert (backup / 'shiori.exe').read_bytes() == b'legacy'
    assert (backup / 'README.md').read_bytes() == b'personal same-name notes'
    assert (target / 'README.md').read_bytes() == b'program readme'
    assert str(backup) in result['warnings'][-1]
    assert json.loads((target / installer.INSTALL_RECORD).read_text('utf-8'))['legacy_backup'] == str(backup)


@pytest.mark.parametrize('name', ['shiori/../escape', '/absolute', 'C:/escape', 'shiori/C:bad',
                                  'shiori/a\\escape', 'shiori/a./file', 'shiori/NUL.txt',
                                  'shiori/data/config.json', 'shiori/.shiori-install.json', 'outside/file',
                                  'shiori//double', 'shiori/_internal/../file'])
def test_unsafe_zip_paths_rejected_before_destination_writes(tmp_path, name):
    payload, manifest = package(tmp_path / 'bundle', extra=[(name, b'bad')])
    # ZipInfo normalizes backslashes on Windows when writing. Construct the
    # hostile on-disk header spelling without altering entry lengths or CRCs.
    if '\\' in name:
        payload.write_bytes(payload.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
        manifest['sha256'] = hashlib.sha256(payload.read_bytes()).hexdigest()
    with pytest.raises(installer.InstallError):
        installer.install_payload(payload, manifest, tmp_path / 'install')
    assert not (tmp_path / 'install').exists()
    assert not list(tmp_path.glob('.shiori-*'))


def test_symbolic_link_in_payload_rejected(tmp_path):
    info = zipfile.ZipInfo('shiori/link')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    payload, manifest = package(tmp_path / 'bundle', extra=[(info, b'../../data')])
    with pytest.raises(installer.InstallError, match='链接'):
        installer.install_payload(payload, manifest, tmp_path / 'install')


def test_case_insensitive_duplicates_rejected(tmp_path):
    payload, manifest = package(tmp_path / 'bundle', extra=[('shiori/SHIORI.EXE', b'collision')])
    with pytest.raises(installer.InstallError, match='重复'):
        installer.install_payload(payload, manifest, tmp_path / 'install')


def test_file_and_directory_conflict_rejected(tmp_path):
    payload, manifest = package(tmp_path / 'bundle', extra=[('shiori/_internal', b'bad')])
    with pytest.raises(installer.InstallError, match='冲突'):
        installer.install_payload(payload, manifest, tmp_path / 'install')


def test_sha_failure_preserves_existing_program(tmp_path):
    target = tmp_path / 'install'
    target.mkdir()
    (target / 'shiori.exe').write_bytes(b'old')
    payload, manifest = package(tmp_path / 'bundle')
    manifest['sha256'] = '0' * 64
    with pytest.raises(installer.InstallError, match='校验失败'):
        installer.install_payload(payload, manifest, target)
    assert (target / 'shiori.exe').read_bytes() == b'old'


def test_running_instance_blocks_before_stage_and_is_never_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'is_instance_running', lambda target: True)
    payload, manifest = package(tmp_path / 'bundle')
    with pytest.raises(installer.InstallError, match='正在运行'):
        installer.install_payload(payload, manifest, tmp_path / 'install')
    assert not list(tmp_path.glob('.shiori-*'))


def test_instance_starting_during_staging_blocks_replace(tmp_path, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(installer, 'is_instance_running', lambda target: next(answers))
    payload, manifest = package(tmp_path / 'bundle')
    with pytest.raises(installer.InstallError, match='正在运行'):
        installer.install_payload(payload, manifest, tmp_path / 'install')
    assert not (tmp_path / 'install').exists()


def test_write_failure_rolls_back_and_allows_retry(tmp_path, monkeypatch):
    target = tmp_path / 'install'
    payload, manifest = package(tmp_path / 'old', {'shiori.exe': b'old', '_internal/runtime.bin': b'oldruntime'})
    installer.install_payload(payload, manifest, target)
    original_record = (target / installer.INSTALL_RECORD).read_bytes()
    payload, manifest = package(tmp_path / 'new')
    real_replace = installer.os.replace
    def fail_once(source, destination):
        if '.shiori-stage-' in str(source) and Path(destination) == target / '_internal/runtime.bin':
            raise PermissionError('locked')
        return real_replace(source, destination)
    with monkeypatch.context() as scoped:
        scoped.setattr(installer.os, 'replace', fail_once)
        with pytest.raises(installer.InstallError, match='原程序文件已恢复'):
            installer.install_payload(payload, manifest, target)
    assert (target / 'shiori.exe').read_bytes() == b'old'
    assert (target / '_internal/runtime.bin').read_bytes() == b'oldruntime'
    assert (target / installer.INSTALL_RECORD).read_bytes() == original_record
    assert not list(tmp_path.glob('.shiori-*'))
    installer.install_payload(payload, manifest, target)
    assert (target / '_internal/runtime.bin').read_bytes() == b'runtime'


def test_unknown_collision_on_new_version_is_preserved(tmp_path):
    payload, manifest = package(tmp_path / 'old')
    target = tmp_path / 'install'
    installer.install_payload(payload, manifest, target)
    (target / 'new.txt').write_bytes(b'user')
    payload, manifest = package(tmp_path / 'new', {'shiori.exe': b'next', 'new.txt': b'program'})
    with pytest.raises(installer.InstallError, match='用户文件重名'):
        installer.install_payload(payload, manifest, target)
    assert (target / 'new.txt').read_bytes() == b'user'
    assert (target / 'shiori.exe').read_bytes() == b'program'


def test_initial_unrelated_file_collision_refuses(tmp_path):
    target = tmp_path / 'install'
    (target / '_internal').mkdir(parents=True)
    (target / '_internal/runtime.bin').write_bytes(b'user')
    payload, manifest = package(tmp_path / 'bundle')
    with pytest.raises(installer.InstallError, match='同名文件'):
        installer.install_payload(payload, manifest, target)
    assert (target / '_internal/runtime.bin').read_bytes() == b'user'


def test_invalid_old_record_cannot_target_data(tmp_path):
    target = tmp_path / 'install'
    target.mkdir()
    (target / installer.INSTALL_RECORD).write_text(json.dumps({'schema': 1, 'files': {'data/secret': '0' * 64}}))
    payload, manifest = package(tmp_path / 'bundle')
    with pytest.raises(installer.InstallError, match='旧安装记录损坏'):
        installer.install_payload(payload, manifest, target)


@pytest.mark.parametrize('value', ['', '/', 'C:\\', 'data'])
def test_unsafe_targets_refused(tmp_path, value, monkeypatch):
    # Empty/relative root-like targets are not implicitly expanded to the cwd.
    if value in ('', 'data'):
        monkeypatch.chdir(tmp_path)
    with pytest.raises(installer.InstallError):
        installer.validate_target(value)


def test_shortcut_failure_is_warning_after_success(tmp_path, monkeypatch):
    def broken(*args):
        raise OSError('powershell unavailable')
    monkeypatch.setattr(installer, 'create_shortcuts', broken)
    payload, manifest = package(tmp_path / 'bundle')
    target = tmp_path / 'install'
    result = installer.install_payload(payload, manifest, target)
    assert result['warnings']
    assert (target / 'shiori.exe').exists()


def test_rollback_failure_retains_recovery_copy(tmp_path, monkeypatch):
    target = tmp_path / 'install'
    payload, manifest = package(tmp_path / 'old', {'shiori.exe': b'old', '_internal/runtime.bin': b'oldruntime'})
    installer.install_payload(payload, manifest, target)
    payload, manifest = package(tmp_path / 'new')
    real_replace = installer.os.replace
    def broken(source, destination):
        if Path(destination) == target / '_internal/runtime.bin':
            raise PermissionError('write and rollback both unavailable')
        return real_replace(source, destination)
    monkeypatch.setattr(installer.os, 'replace', broken)
    with pytest.raises(installer.InstallError, match='旧程序保存在'):
        installer.install_payload(payload, manifest, target)
    backups = list(tmp_path.glob('.shiori-backup-*'))
    assert len(backups) == 1
    assert (backups[0] / '_internal/runtime.bin').read_bytes() == b'oldruntime'
    assert not list(tmp_path.glob('.shiori-stage-*'))
    assert not list(tmp_path.glob('*.installing'))


def test_target_ancestor_file_is_preserved_before_program_replacement(tmp_path):
    target = tmp_path / 'install'
    target.mkdir()
    (target / 'shiori.exe').write_bytes(b'old')
    (target / '_internal').write_bytes(b'user file')
    payload, manifest = package(tmp_path / 'bundle')
    with pytest.raises(installer.InstallError, match='同名文件占用'):
        installer.install_payload(payload, manifest, target)
    assert (target / 'shiori.exe').read_bytes() == b'old'
    assert (target / '_internal').read_bytes() == b'user file'


def test_concurrent_install_lock_blocks_and_is_not_removed(tmp_path):
    target = tmp_path / 'install'
    name = '.shiori-' + hashlib.sha256(str(target.resolve()).casefold().encode()).hexdigest()[:16] + '.installing'
    lock = tmp_path / name
    lock.write_bytes(b'other installer')
    payload, manifest = package(tmp_path / 'bundle')
    with pytest.raises(installer.InstallError, match='已有安装任务'):
        installer.install_payload(payload, manifest, target)
    assert lock.read_bytes() == b'other installer'


def test_shortcuts_encode_user_path_as_data_and_have_no_auth_argument(tmp_path, monkeypatch):
    if installer.os.name != 'nt':
        pytest.skip('Windows shortcut entry point')
    import base64
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    target = tmp_path / "quote '$() `literal"
    assert real_shortcuts(target) == []
    args, options = calls[0]
    assert options['creationflags'] == installer.subprocess.CREATE_NO_WINDOW
    script = base64.b64decode(args[-1]).decode('utf-16le')
    assert str(target) not in script
    assert "$link.Arguments = $c.arguments" in script
    assert "--stop --no-browser" in script
    assert 'token' not in script
    encoded = script.split("FromBase64String('")[1].split("')")[0]
    record = json.loads(base64.b64decode(encoded))
    assert record['target'] == str(target) and record['arguments'] == ''
    personal = tmp_path / "custom '$() `data"
    real_shortcuts(target, data_dir=personal)
    custom_script = base64.b64decode(calls[1][0][-1]).decode('utf-16le')
    encoded = custom_script.split("FromBase64String('")[1].split("')")[0]
    assert json.loads(base64.b64decode(encoded))['arguments'] == installer.subprocess.list2cmdline(['--data-dir',str(personal.resolve())])
    assert str(personal) not in custom_script and "'--stop --no-browser ' + $c.arguments" in custom_script


def test_live_windows_mutex_blocks_running_and_guard_then_releases(tmp_path):
    if installer.os.name != 'nt':
        pytest.skip('Windows named mutex')
    target = tmp_path / 'install'
    assert real_running(target) is False
    release = real_guard(target)
    try:
        assert real_running(target) is True
        with pytest.raises(installer.InstallError, match='已启动'):
            real_guard(target)
    finally:
        release()
    assert real_running(target) is False
