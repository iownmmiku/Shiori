"""Resource fixtures never depend on or modify the host's cgroup filesystem."""
from pathlib import Path
import shutil
import subprocess

import pytest

from shiori.resource_metrics import ResourceMetrics, read_resources


def files(root, values):
    root.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (root / name).write_text(str(value), encoding='utf-8')


def proc_files(root, membership, mounts=''):
    files(root / 'self', {'cgroup': membership, 'mountinfo': mounts})


def test_v2_samples_actual_cpu_time_against_visible_quota(tmp_path, monkeypatch):
    monkeypatch.setattr('shiori.resource_metrics.os.cpu_count', lambda: 16)
    base, proc = tmp_path / 'group', tmp_path / 'proc'
    files(base, {'cpu.stat': 'usage_usec 1000000\nuser_usec 900000\n', 'cpu.max': '50000 100000',
                 'memory.current': 950, 'memory.max': 1000, 'memory.peak': 980,
                 'memory.events': 'oom 2\noom_kill 1\n'})
    now = [10.0]
    sampler = ResourceMetrics(base, proc_root=proc, clock=lambda: now[0])
    first = sampler.read()
    assert first['cpu_percent'] is None and first['cpu_usage_seconds'] == 1
    now[0] = 11
    files(base, {'cpu.stat': 'usage_usec 1250000\n'})
    second = sampler.read()
    assert second['cgroup_version'] == 2 and second['cpu_limit'] == .5
    assert second['cpu_usage_cores'] == .25 and second['cpu_percent'] == 50
    assert second['memory_pressure'] and second['oom_events'] == 2 and second['oom_kills'] == 1
    assert str(tmp_path) not in str(second), 'host paths must not reach the statistics API'


def test_v1_combined_controllers_mount_root_and_nanosecond_usage(tmp_path, monkeypatch):
    monkeypatch.setattr('shiori.resource_metrics.os.cpu_count', lambda: 8)
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    memory, cpu = base / 'memory', base / 'cpu,cpuacct'
    files(memory / 'worker', {'memory.usage_in_bytes': 500, 'memory.limit_in_bytes': 1000,
                             'memory.max_usage_in_bytes': 750, 'memory.failcnt': 3,
                             'memory.oom_control': 'oom_kill_disable 0\nunder_oom 0\noom_kill 2\n'})
    files(cpu / 'worker', {'cpuacct.usage': 1000000000, 'cpu.cfs_quota_us': 100000, 'cpu.cfs_period_us': 100000})
    proc_files(proc, '3:memory:/tenant/worker\n2:cpu,cpuacct:/tenant/worker\n',
               f'1 0 0:1 /tenant {memory.as_posix()} rw - cgroup cgroup rw,memory\n'
               f'2 0 0:2 /tenant {cpu.as_posix()} rw - cgroup cgroup rw,cpu,cpuacct\n')
    now = [2.0]
    sampler = ResourceMetrics(base, proc_root=proc, clock=lambda: now[0])
    first = sampler.read()
    assert first['cgroup_version'] == 1 and first['memory_bytes'] == 500
    assert first['memory_limit_hits'] == 3 and first['oom_kills'] == 2 and first['oom_events'] is None
    files(cpu / 'worker', {'cpuacct.usage': 1500000000})
    now[0] = 4
    second = sampler.read()
    assert second['cpu_usage_cores'] == .25 and second['cpu_percent'] == 25


def test_v2_uses_current_membership_instead_of_host_root_and_parent_limits(tmp_path):
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base, {'cgroup.controllers': 'memory cpu', 'memory.current': 99999,
                 'memory.max': 4000, 'cpu.max': '200000 100000', 'cpu.stat': 'usage_usec 90000000'})
    files(base / 'service', {'memory.current': 800, 'memory.max': 'max', 'cpu.max': 'max 100000',
                            'cpu.stat': 'usage_usec 500000', 'cpuset.cpus.effective': '0-3'})
    proc_files(proc, '0::/service\n', f'1 0 0:1 / {base.as_posix()} rw - cgroup2 cgroup rw\n')
    result = read_resources(base, proc_root=proc)
    assert result['memory_bytes'] == 800 and result['memory_limit_bytes'] == 4000
    assert result['cpu_usage_seconds'] == .5 and result['cpu_limit'] == 2


def test_namespace_root_and_escaped_mount_path_are_supported(tmp_path):
    base, proc = tmp_path / 'space group', tmp_path / 'proc'
    files(base, {'memory.current': 10, 'memory.max': 'max', 'cpu.stat': 'usage_usec 100'})
    mount = base.as_posix().replace(' ', r'\040')
    proc_files(proc, '0::/\n', f'1 0 0:1 /docker/opaque-host-id {mount} rw - cgroup2 cgroup rw\n')
    result = read_resources(base, proc_root=proc)
    assert result['memory_bytes'] == 10 and result['cgroup_version'] == 2
    assert result['memory_limit_bytes'] is None


def test_unlimited_v1_memory_and_cpuset_capacity_are_not_synthetic_ooms(tmp_path, monkeypatch):
    monkeypatch.setattr('shiori.resource_metrics.os.cpu_count', lambda: 12)
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base / 'memory', {'memory.usage_in_bytes': 42, 'memory.limit_in_bytes': 9223372036854771712})
    files(base / 'cpu', {'cpu.cfs_quota_us': -1, 'cpu.cfs_period_us': 100000})
    files(base / 'cpuacct', {'cpuacct.usage': 1000000000})
    files(base / 'cpuset', {'cpuset.cpus': '0-2,2-3,8'})
    result = read_resources(base, proc_root=proc)
    assert result['cpu_capacity'] == 5 and result['cpu_limit'] is None
    assert result['memory_limit_bytes'] is None and result['oom_kills'] is None


def test_sample_resets_after_counter_rollback_or_capacity_change(tmp_path):
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base, {'cpu.stat': 'usage_usec 2000000', 'cpu.max': '100000 100000'})
    now = [1.0]
    sampler = ResourceMetrics(base, proc_root=proc, clock=lambda: now[0])
    sampler.read()
    now[0] = 2
    files(base, {'cpu.stat': 'usage_usec 1000000'})
    assert sampler.read()['cpu_percent'] is None
    now[0] = 3
    files(base, {'cpu.stat': 'usage_usec 1500000', 'cpu.max': '50000 100000'})
    assert sampler.read()['cpu_percent'] is None
    now[0] = 4
    files(base, {'cpu.stat': 'usage_usec 1750000'})
    assert sampler.read()['cpu_percent'] == 50


def test_sample_resets_when_process_moves_to_another_group(tmp_path):
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base, {'cgroup.controllers': 'cpu'})
    for name, usage in [('first', 1000000), ('second', 9000000)]:
        files(base / name, {'cpu.stat': f'usage_usec {usage}', 'cpu.max': '100000 100000'})
    proc_files(proc, '0::/first\n')
    now = [1.0]
    sampler = ResourceMetrics(base, proc_root=proc, clock=lambda: now[0])
    sampler.read()
    proc_files(proc, '0::/second\n')
    now[0] = 2
    assert sampler.read()['cpu_percent'] is None


def test_unavailable_or_bad_metrics_remain_unknown_and_read_only(tmp_path):
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base, {'cpu.stat': 'usage_usec -1\n', 'cpu.max': 'broken', 'memory.current': 'not an integer',
                 'memory.max': '-1', 'memory.events': 'oom_kill not-a-number\n'})
    before = {file.name: file.read_bytes() for file in base.iterdir()}
    result = read_resources(base, proc_root=proc)
    assert not result['available'] and result['cpu_percent'] is None
    assert result['memory_bytes'] is None and result['oom_kills'] is None
    assert before == {file.name: file.read_bytes() for file in base.iterdir()}


def test_membership_cannot_escape_mount_or_read_arbitrary_parent_files(tmp_path):
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base, {'cgroup.controllers': 'cpu memory'})
    files(tmp_path / 'outside', {'memory.current': 999, 'cpu.stat': 'usage_usec 9000000'})
    proc_files(proc, '0::/../outside\n', f'1 0 0:1 / {base.as_posix()} rw - cgroup2 cgroup rw\n')
    result = read_resources(base, proc_root=proc)
    assert result['memory_bytes'] is None and result['cpu_usage_seconds'] is None


def test_missing_current_group_does_not_silently_display_host_metrics(tmp_path):
    base, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    files(base, {'memory.current': 999999, 'cpu.stat': 'usage_usec 9000000'})
    proc_files(proc, '0::/missing-group\n')
    result = read_resources(base, proc_root=proc)
    assert not result['available']


def test_statistics_ui_distinguishes_cgroup_and_external_connector_metrics():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_cgroup_resources.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_statistics_api_keeps_resource_boundaries_and_background_index_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from auth_helpers import password_login
    from shiori.api import create_app
    resource = {'available': True, 'cgroup_version': 1, 'cpu_percent': 25, 'cpu_usage_cores': .25,
                'cpu_limit': 1, 'memory_bytes': 100, 'memory_limit_bytes': 200}
    monkeypatch.setattr(ResourceMetrics, 'read', lambda self: resource)
    app = create_app(tmp_path, start_workers=False)
    indexing = {'completed': 3, 'failed': 1, 'skipped': 2, 'queued_batches': 1, 'pending_chunks': 4, 'running': True}
    monkeypatch.setattr(app.state.chat.memory, 'indexing_status', lambda: indexing, raising=False)
    with TestClient(app) as client:
        assert client.get('/api/statistics').status_code == 401
        password_login(client, app)
        response = client.get('/api/statistics')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['resources'] == resource and body['memory_indexing'] == indexing
        assert body['connector_resources']['available'] is False
        assert body['connector_resources']['reason'] == 'external_process_identity_unknown'
