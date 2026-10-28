"""Listener ownership, observable fallback, and fixed-port server deployment."""
import errno
import logging
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

import pytest

import shiori.__main__ as launcher


def close_listeners(listeners):
    for listener in listeners:
        listener.close()


def test_busy_listener_fallback_warns_and_does_not_share_port(caplog):
    with socket.socket() as owner:
        owner.bind(('127.0.0.1', 0))
        owner.listen()
        requested = owner.getsockname()[1]
        if requested > 65505:
            pytest.skip('Ephemeral port too close to the upper bound for fallback')
        with caplog.at_level(logging.WARNING):
            listeners, actual = launcher._bind_listeners('127.0.0.1', requested)
        try:
            assert requested < actual <= requested + 29
            assert listeners[0].getsockname()[1] == actual
            assert len(caplog.records) == 1
            assert str(requested) in caplog.text and str(actual) in caplog.text
            assert 'OneBot' in caplog.text and '--strict-port' in caplog.text
            assert owner.getsockname()[1] == requested
        finally:
            close_listeners(listeners)


def test_fixed_port_refuses_fallback_and_explains_conflict(caplog):
    with socket.socket() as owner:
        owner.bind(('127.0.0.1', 0))
        owner.listen()
        port = owner.getsockname()[1]
        with pytest.raises(OSError, match=f'{port}.*--strict-port') as raised:
            launcher._bind_listeners('127.0.0.1', port, strict_port=True)
        assert isinstance(raised.value.__cause__, OSError)
        assert '已改用' not in caplog.text


@pytest.mark.parametrize('platform', ['posix', 'nt'])
def test_platform_socket_options_before_bind_and_partial_cleanup(monkeypatch, platform):
    sockets, calls = [], []
    class Listener:
        def __init__(self, family, kind):
            self.index, self.closed = len(sockets), False
            sockets.append(self)
        def setsockopt(self, *args): calls.append((self.index, 'option', args))
        def bind(self, target):
            calls.append((self.index, 'bind', target))
            if self.index == 1:
                raise OSError(errno.EADDRINUSE, 'local desktop listener occupied')
        def listen(self, backlog): pass
        def setblocking(self, value): pass
        def close(self): self.closed = True
    facade = SimpleNamespace(socket=Listener, AF_INET=socket.AF_INET, AF_INET6=socket.AF_INET6,
        SOCK_STREAM=socket.SOCK_STREAM, SOL_SOCKET=socket.SOL_SOCKET,
        SO_REUSEADDR=socket.SO_REUSEADDR, SO_EXCLUSIVEADDRUSE=-5,
        IPPROTO_IPV6=socket.IPPROTO_IPV6, IPV6_V6ONLY=socket.IPV6_V6ONLY)
    monkeypatch.setattr(launcher, 'socket', facade)
    monkeypatch.setattr(launcher, 'os', SimpleNamespace(name=platform))
    listeners, port = launcher._bind_listeners('127.0.0.2', 8765)
    try:
        assert port == 8766
        assert len(sockets) == 4 and all(item.closed for item in sockets[:2])
        expected = facade.SO_REUSEADDR if platform == 'posix' else facade.SO_EXCLUSIVEADDRUSE
        for index in range(4):
            operations = [item for item in calls if item[0] == index]
            assert operations[0] == (index, 'option', (socket.SOL_SOCKET, expected, 1))
            assert operations[1][1] == 'bind'
    finally:
        close_listeners(listeners)


def test_exhausted_range_closes_every_listener_and_preserves_error(monkeypatch):
    sockets = []
    class Busy:
        def __init__(self, *args): self.closed = False; sockets.append(self)
        def setsockopt(self, *args): pass
        def bind(self, target): raise OSError(errno.EADDRINUSE, 'occupied')
        def close(self): self.closed = True
    monkeypatch.setattr(launcher.socket, 'socket', Busy)
    with pytest.raises(OSError, match='65535') as raised:
        launcher._bind_listeners('127.0.0.1', 65535)
    assert len(sockets) == 1 and sockets[0].closed
    assert raised.value.__cause__.errno == errno.EADDRINUSE


@pytest.mark.skipif(os.name == 'nt', reason='POSIX TIME_WAIT reuse is exercised on Linux CI')
def test_posix_reuses_port_after_server_active_close():
    listeners, port = launcher._bind_listeners('127.0.0.1', 0, strict_port=True)
    listener = listeners[0]
    port = listener.getsockname()[1]
    try:
        listener.setblocking(True)
        listener.settimeout(3)
        with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b''
    finally:
        close_listeners(listeners)
    reused, actual = launcher._bind_listeners('127.0.0.1', port, strict_port=True)
    try:
        assert actual == port
        assert reused[0].getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    finally:
        close_listeners(reused)


@pytest.mark.parametrize('strict', [False, True])
def test_real_launcher_reports_actual_port_or_exits_without_starting(tmp_path, strict):
    data = tmp_path / 'isolated-server'
    with socket.socket() as owner:
        owner.bind(('127.0.0.1', 0))
        owner.listen()
        requested = owner.getsockname()[1]
        if requested > 65505:
            pytest.skip('Ephemeral port too close to the upper bound for fallback')
        command = [sys.executable, '-m', 'shiori', '--data-dir', str(data),
                   '--port', str(requested), '--no-browser', '--no-tray']
        if strict:
            command.append('--strict-port')
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                if strict:
                    _, stderr = process.communicate(timeout=20)
                    assert process.returncode != 0
                    assert '--strict-port' in stderr.decode('utf-8', errors='replace')
                    assert not (data / 'server.json').exists()
                    return
                record = None
                deadline = time.monotonic() + 20
                client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        record = json.loads((data / 'server.json').read_text('utf-8'))
                        with client.open(f'http://127.0.0.1:{record["port"]}/healthz', timeout=.5) as response:
                                assert json.load(response)['name'] == 'Shiori'
                        break
                    except (OSError, ValueError):
                        time.sleep(.1)
                else:
                    pytest.fail('Isolated launcher did not become healthy')
                assert record['requested_port'] == requested and record['port'] > requested
                root = f'http://127.0.0.1:{record["port"]}'
                headers = {'Authorization': 'Bearer ' + (data / 'admin.token').read_text('utf-8').strip()}
                with client.open(urllib.request.Request(root + '/api/bootstrap', headers=headers), timeout=5) as response:
                    binding = json.load(response)['listener']
                assert binding['port_changed'] and binding['requested_port'] == requested
                assert binding['actual_port'] == record['port']
                log = (data / 'runtime.log').read_text('utf-8')
                assert 'WARNING' in log and 'OneBot' in log
                assert str(requested) in log and str(record['port']) in log
                with client.open(urllib.request.Request(root + '/api/shutdown', headers=headers, method='POST'), timeout=5):
                    pass
                process.wait(timeout=20)
                assert process.returncode == 0
                assert not (data / 'server.json').exists()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
