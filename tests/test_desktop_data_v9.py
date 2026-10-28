from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from shiori import data_paths, startup
from shiori.desktop import DesktopShell, external_link, local_origin, navigation_allowed


def test_default_data_separate_from_application(tmp_path):
    home = tmp_path / 'user'
    assert data_paths.default_data_dir(environ={'LOCALAPPDATA': str(home / 'local')}, platform='win32', home=home) == home / 'local' / 'shiori' / 'data'
    assert data_paths.default_data_dir(environ={}, platform='win32', home=home) == home / 'AppData' / 'Local' / 'shiori' / 'data'
    assert data_paths.default_data_dir(environ={'XDG_DATA_HOME': str(home / 'xdg')}, platform='linux', home=home) == home / 'xdg' / 'shiori' / 'data'


@pytest.fixture
def paths(tmp_path, monkeypatch):
    base, target = tmp_path / 'application', tmp_path / 'userdata' / 'shiori' / 'data'
    legacy = base / 'data'
    legacy.mkdir(parents=True)
    monkeypatch.setattr(data_paths, 'default_data_dir', lambda: target)
    monkeypatch.setattr(startup, 'instance_details', lambda *a, **k: None)
    return base, legacy, target


def test_migration_copies_preserves_original_and_clears_stale_runtime(paths):
    base, legacy, target = paths
    (legacy / 'settings.json').write_bytes(b'user-settings')
    (legacy / 'server.json').write_text('{"pid":123}', encoding='utf-8')
    (legacy / 'nested').mkdir()
    (legacy / 'nested' / 'private.bin').write_bytes(b'\x00\xff\x01')
    assert data_paths.resolve_data_dir(base=base) == target
    assert (target / 'nested' / 'private.bin').read_bytes() == b'\x00\xff\x01'
    assert (legacy / 'settings.json').read_bytes() == (target / 'settings.json').read_bytes()
    assert (legacy / 'server.json').exists() and not (target / 'server.json').exists()
    assert json.loads((target / 'migration-v0.9.json').read_text('utf-8'))['original_preserved'] is True
    assert not list(target.parent.glob('.data-migrate-*'))


def test_explicit_directory_never_migrates(paths):
    base, legacy, target = paths
    (legacy / 'chat.db').write_bytes(b'old')
    explicit = base.parent / 'chosen'
    assert data_paths.resolve_data_dir(explicit, base=base) == explicit
    assert not explicit.exists() and not target.exists()


def test_existing_data_is_never_merged_or_overwritten(paths):
    base, legacy, target = paths
    (legacy / 'chat.db').write_bytes(b'old')
    target.mkdir(parents=True)
    (target / 'chat.db').write_bytes(b'new')
    assert data_paths.resolve_data_dir(base=base) == target
    assert (target / 'chat.db').read_bytes() == b'new'
    assert not (target / 'migration-v0.9.json').exists()


def test_empty_target_migration_and_stop_does_not_copy(paths):
    base, legacy, target = paths
    (legacy / 'chat.db').write_bytes(b'old')
    assert data_paths.resolve_data_dir(base=base, migrate=False) == legacy
    assert not target.exists()
    target.mkdir(parents=True)
    assert data_paths.resolve_data_dir(base=base) == target
    assert (target / 'chat.db').read_bytes() == b'old'


def test_live_legacy_refuses_migration(paths, monkeypatch):
    base, legacy, target = paths
    (legacy / 'chat.db').write_bytes(b'old')
    monkeypatch.setattr(startup, 'instance_details', lambda *a, **k: {'port': 8765})
    with pytest.raises(OSError, match='正在使用'):
        data_paths.resolve_data_dir(base=base)
    assert not target.exists()
    assert (legacy / 'chat.db').read_bytes() == b'old'


def test_migration_refuses_linked_files(paths):
    base, legacy, target = paths
    original = base / 'outside.txt'
    original.write_text('private', encoding='utf-8')
    try:
        (legacy / 'linked.txt').symlink_to(original)
    except OSError:
        pytest.skip('Windows 当前账号未启用创建符号链接')
    with pytest.raises(OSError, match='链接'):
        data_paths.resolve_data_dir(base=base)
    assert not target.exists()


def test_failed_copy_leaves_legacy_and_no_partial_destination(paths, monkeypatch):
    base, legacy, target = paths
    (legacy / 'chat.db').write_bytes(b'old')
    def fail(source, dest):
        dest.mkdir()
        (dest / 'partial').write_bytes(b'partial')
        raise OSError('disk full')
    monkeypatch.setattr(data_paths.shutil, 'copytree', fail)
    with pytest.raises(OSError, match='disk full'):
        data_paths.resolve_data_dir(base=base)
    assert not target.exists() and not list(target.parent.glob('.data-migrate-*'))
    assert (legacy / 'chat.db').read_bytes() == b'old'


def test_data_guard_blocks_other_process_semantics(tmp_path):
    # Windows CreateMutex detects all open handles, including the same process.
    if sys.platform != 'win32':
        pytest.skip('Windows mutex compatibility test')
    with startup.data_guard(tmp_path):
        with pytest.raises(OSError, match='已有'):
            with startup.data_guard(tmp_path):
                pytest.fail('must not enter')
    with startup.data_guard(tmp_path):
        pass


def test_private_activation_requires_secret_and_no_origin():
    shown = threading.Event()
    server = startup.ActivationServer(shown.set)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), startup._NoRedirect())
    url = f'http://127.0.0.1:{server.record["port"]}/show'
    try:
        for headers in ({}, {'Authorization': 'Bearer invalid'},
                        {'Authorization': 'Bearer ' + server.secret, 'Origin': 'http://evil.example'}):
            with pytest.raises(urllib.error.HTTPError) as result:
                opener.open(urllib.request.Request(url, b'{}', headers=headers, method='POST'))
            assert result.value.code == 403
        assert not shown.is_set()
        assert startup.wake_instance({'activation': server.record, 'opener': opener})
        assert shown.wait(1)
    finally:
        server.close()


@pytest.mark.parametrize('bad', [None, {}, {'port': True, 'secret': 's' * 40},
                                  {'port': 8765, 'secret': 'short'}, {'port': 65536, 'secret': 's' * 40}])
def test_wake_rejects_invalid_records(bad):
    assert startup.wake_instance({'activation': bad}) is False


@pytest.mark.parametrize('url', ['https://127.0.0.1:8765/', 'http://localhost:8765/',
                                'http://example.com:8765/', 'file:///test',
                                'http://user:pass@127.0.0.1:8765/', 'http://127.0.0.1/'])
def test_desktop_accepts_only_explicit_private_origin(url):
    with pytest.raises(ValueError):
        local_origin(url)


def test_desktop_navigation_and_external_protocols():
    origin = local_origin('http://127.0.0.1:8765/#bootstrap=secret')
    assert navigation_allowed('http://127.0.0.1:8765/static/help', origin)
    for url in ['http://127.0.0.1:8766/', 'http://127.0.0.1.evil.example:8765/',
                'http://evil.example/', 'file:///C:/private.txt', 'javascript:alert(1)',
                'http://user:pass@127.0.0.1:8765/']:
        assert not navigation_allowed(url, origin)
    assert external_link('https://example.com/help')
    assert not external_link('file:///C:/private.txt')
    assert not external_link('javascript:alert(1)')
    assert not external_link('ms-settings:')


def test_desktop_close_behaviour_and_programmatic_exit(tmp_path):
    stopped, hidden, destroyed = [], [], []
    shell = DesktopShell('http://127.0.0.1:8765/', tmp_path, stop=lambda: stopped.append(True), keep_in_tray=True)
    shell.window = SimpleNamespace(hide=lambda: hidden.append(True), destroy=lambda: destroyed.append(True))
    assert shell._closing() is False
    assert hidden == [True] and stopped == []
    shell.close()
    assert destroyed == [True]
    assert shell._closing() is True and stopped == [True]
    shell2 = DesktopShell('http://127.0.0.1:8765/', tmp_path, stop=lambda: stopped.append(True))
    assert shell2._closing() is True and len(stopped) == 2


def test_desktop_navigations_are_blocked_before_load(tmp_path, monkeypatch):
    class NativeEvent:
        def __init__(self): self.handlers = []
        def __iadd__(self, value): self.handlers.append(value); return self
        def __isub__(self, value): self.handlers.remove(value); return self
    control = SimpleNamespace(NavigationStarting=NativeEvent(), CoreWebView2InitializationCompleted=NativeEvent(), CoreWebView2=None)
    shell = DesktopShell('http://127.0.0.1:8765/', tmp_path, stop=lambda: None)
    shell.window = SimpleNamespace(native=SimpleNamespace(webview=control))
    opened = []
    monkeypatch.setattr('qunyu.desktop.webbrowser.open', opened.append)
    shell._protect_navigation()
    handler = control.NavigationStarting.handlers[0]
    for target, user, allowed in [('http://127.0.0.1:8765/', False, True), ('https://example.com/', False, False),
                                  ('https://example.com/', True, False), ('file:///secret.txt', True, False)]:
        event = SimpleNamespace(Uri=target, IsUserInitiated=user, Cancel=False)
        handler(None, event)
        assert event.Cancel is (not allowed)
    assert opened == ['https://example.com/']


def test_lan_listener_preserves_local_desktop_endpoint():
    from shiori.__main__ import _bind_listeners
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    # A second loopback address stands in for a selected LAN interface. Both
    # listeners are real sockets but never start an app or leave test data.
    listeners, actual = _bind_listeners('127.0.0.2', port)
    try:
        assert actual >= port
        assert {x.getsockname()[0] for x in listeners} == {'127.0.0.1', '127.0.0.2'}
    finally:
        for listener in listeners:
            listener.close()
