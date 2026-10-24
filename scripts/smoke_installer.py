"""Exercise the shipped onefile installer against isolated Windows directories."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

from smoke_contract import effective_contract_version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('installer', type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18767)
    parser.add_argument('--mcp-fixture-url', default='')
    parser.add_argument('--version', '--expect-version', '--expect', dest='version', default='0.1.6')
    args = parser.parse_args()
    version = effective_contract_version(args.version)
    root = args.work_dir.resolve()
    if root.exists():
        raise RuntimeError('Use a fresh QA directory')
    root.mkdir(parents=True)
    target = root / '含空格的 安装目录'
    setup = args.installer.resolve()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    def install(destination, expected=0):
        result = subprocess.run([str(setup), '--silent', '--target', str(destination), '--no-shortcuts', '--no-launch'],
                                capture_output=True, timeout=120, creationflags=flags)
        assert result.returncode == expected, f'installer exit {result.returncode}, expected {expected}'

    def record():
        return json.loads((target / '.mikubot-install.json').read_text('utf-8'))

    def snapshot(folder):
        return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in folder.rglob('*') if p.is_file()}

    install(target)
    assert record()['version'] == args.version
    for name, digest in record()['files'].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest, name
    assert (target / '_internal/mikubot/static/quick-start.js').is_file()
    if version >= (0, 8):
        for name in ('experience.js', 'experience.css', 'guide.md'):
            assert (target / '_internal/mikubot/static' / name).is_file(), name
        assert (target / '_internal/mikubot/resources/csp/SKILL.md').is_file()
        assert (target / '_internal/mikubot/resources/csp/references/persona-template.md').is_file()
    assert not (target / 'data').exists()
    smoke = [sys.executable, str(Path(__file__).with_name('smoke_portable.py')),
             str(target / 'mikubot.exe'), '--data-dir', str(target / 'data'),
             '--port', str(args.port), '--expect-version', args.version]
    if args.mcp_fixture_url:
        smoke += ['--mcp-fixture-url', args.mcp_fixture_url]
    result = subprocess.run(smoke, capture_output=True, timeout=180, creationflags=flags)
    assert result.returncode == 0, result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
    (target / '个人笔记.txt').write_text('must survive an upgrade', 'utf-8')
    data = target / 'data'
    (data / 'user-preserve.bin').write_bytes(b'unchanged user data\x00\xff')
    url = f'http://127.0.0.1:{args.port}'
    process = None

    def request(path):
        token = (data / 'admin.token').read_text('utf-8').strip()
        req = urllib.request.Request(url + path, headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def start():
        nonlocal process
        process = subprocess.Popen([str(target / 'mikubot.exe'), '--data-dir', str(data), '--port', str(args.port), '--no-browser', '--no-tray'],
                                   cwd=target, creationflags=flags)
        for _ in range(120):
            try:
                if request('/healthz')['version'] == args.version:
                    return
            except OSError:
                pass
            if process.poll() is not None:
                raise RuntimeError('Installed app exited before ready')
            time.sleep(.25)
        raise RuntimeError('Installed app did not become ready')

    def stop():
        nonlocal process
        result = subprocess.run([str(target / 'mikubot.exe'), '--data-dir', str(data), '--stop', '--no-browser'], cwd=target,
                                capture_output=True, timeout=15, creationflags=flags)
        assert result.returncode == 0
        if process:
            assert process.wait(timeout=20) == 0
            process = None
        assert not (data / 'server.json').exists()

    try:
        start()
        old_record = (target / '.mikubot-install.json').read_bytes()
        install(target, expected=1)
        assert request('/healthz')['version'] == args.version
        assert (target / '.mikubot-install.json').read_bytes() == old_record
        stop()
        before = snapshot(data)
        install(target)
        assert snapshot(data) == before, 'Upgrade modified user data'
        assert (target / '个人笔记.txt').read_text('utf-8') == 'must survive an upgrade'
        start()
        assert request('/api/memories'), 'Restored memories missing after upgrade'
        stop()
    finally:
        if process and process.poll() is None:
            try:
                stop()
            except Exception:
                process.terminate()
                process.wait(timeout=10)

    # Legacy installs lack ownership metadata: all replaced files stay recoverable.
    legacy = root / '旧便携目录'
    legacy.mkdir()
    shutil.copy2(target / 'mikubot.exe', legacy / 'mikubot.exe')
    (legacy / 'README.md').write_text('same-name personal document', 'utf-8')
    (legacy / 'data').mkdir()
    (legacy / 'data/keep.bin').write_bytes(b'legacy-data')
    install(legacy)
    legacy_record = json.loads((legacy / '.mikubot-install.json').read_text('utf-8'))
    recovery = Path(legacy_record['legacy_backup'])
    assert recovery.is_relative_to(root) and recovery.is_dir()
    assert (recovery / 'README.md').read_text('utf-8') == 'same-name personal document'
    assert (legacy / 'data/keep.bin').read_bytes() == b'legacy-data'
    result = {'version': args.version, 'first_install': True, 'portable_smoke': True,
              'running_upgrade_refused': True, 'stop_cli': True, 'data_files_preserved': len(before),
              'unknown_user_file_preserved': True, 'legacy_recovery_preserved': True,
              'program_files_verified': len(record()['files'])}
    if version >= (0, 8):
        portable = json.loads((data / 'portable-smoke.json').read_text('utf-8'))
        assert portable['passed'] and portable['version'] == args.version and portable['experience_jobs']
        result.update(frozen_python_worker=True, computer_approval_roundtrip=True, csp_resources=True,
                      administrators=True, reference_attachments=True, jobs_history_read_only=True)
    (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
