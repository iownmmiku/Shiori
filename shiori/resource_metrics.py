"""Read the current process's cgroup boundaries and counters without host changes."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time


_MAX_FILE_BYTES = 131072


def _text(path):
    try:
        with path.open('rb') as source:
            data = source.read(_MAX_FILE_BYTES + 1)
        return data.decode('utf-8') if len(data) <= _MAX_FILE_BYTES else ''
    except (OSError, UnicodeError):
        return ''


def _integer(base, name, *, unlimited=False):
    try:
        value = _text(base / name).strip()
        number = int(value)
        return None if number < 0 or number >= (1 << 64) or unlimited and number >= (1 << 60) else number
    except (ValueError, TypeError):
        return None


def _pairs(base, name):
    result = {}
    for line in _text(base / name).splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                number = int(fields[1])
                if 0 <= number < (1 << 64):
                    result[fields[0]] = number
            except ValueError:
                pass
    return result


def _path_parts(value):
    # Kernel cgroup membership and mount roots are POSIX paths even on fixture hosts.
    value = str(value)
    if not value.startswith('/') or '\x00' in value or '\\' in value:
        return None
    parts = PurePosixPath(value).parts[1:]
    return None if '..' in parts else parts


def _inside(base, parts):
    if parts is None:
        return None
    candidate = base.joinpath(*parts)
    try:
        return candidate if candidate.resolve().is_relative_to(base.resolve()) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _unescape_mount(value):
    return re.sub(r'\\(040|011|012|134)', lambda match: chr(int(match[1], 8)), value)


def _layout(base, proc):
    members = {}
    for line in _text(proc / 'self/cgroup').splitlines():
        fields = line.split(':', 2)
        if len(fields) != 3 or _path_parts(fields[2]) is None:
            continue
        for controller in fields[1].split(','):
            members[controller or 'v2'] = fields[2]
    v2, v1, boundaries = None, {}, {}
    for line in _text(proc / 'self/mountinfo').splitlines():
        before, separator, after = line.partition(' - ')
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 5 or len(filesystem) < 3:
            continue
        mount = Path(_unescape_mount(fields[4]))
        try:
            if not mount.resolve().is_relative_to(base.resolve()):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        root_parts = _path_parts(_unescape_mount(fields[3]))
        if root_parts is None:
            continue
        controllers = ['v2'] if filesystem[0] == 'cgroup2' else filesystem[2].split(',') if filesystem[0] == 'cgroup' else []
        for controller in controllers:
            if controller not in members:
                continue
            member_parts = _path_parts(members[controller])
            # A cgroup namespace can report '/' while mountinfo still has a host root.
            if member_parts[:len(root_parts)] == root_parts:
                member_parts = member_parts[len(root_parts):]
            elif member_parts:
                continue
            path = _inside(mount, member_parts)
            if path is not None:
                boundaries[path] = mount
                if controller == 'v2':
                    v2 = path
                elif controller in {'memory', 'cpu', 'cpuacct', 'cpuset'}:
                    v1[controller] = path
    # Direct paths support ordinary cgroup mounts and isolated, read-only fixtures.
    if v2 is None and any((base / name).is_file() for name in ('memory.current', 'cpu.stat', 'cgroup.controllers')):
        member = _path_parts(members.get('v2', '/'))
        candidate = _inside(base, member)
        if candidate is not None and any((candidate / name).is_file() for name in ('memory.current', 'cpu.stat', 'cgroup.controllers')):
            v2 = candidate
            boundaries[v2] = base
    for controller in ('memory', 'cpu', 'cpuacct', 'cpuset'):
        if controller in v1:
            continue
        for name in (controller, 'cpu,cpuacct', 'cpuacct,cpu') if controller in {'cpu', 'cpuacct'} else (controller,):
            mount = base / name
            candidate = _inside(mount, _path_parts(members.get(controller, '/')))
            if candidate is not None and candidate.is_dir():
                v1[controller] = candidate
                boundaries[candidate] = mount
                break
    if not v1 and (base / 'memory.usage_in_bytes').is_file():
        v1 = {name: base for name in ('memory', 'cpu', 'cpuacct', 'cpuset')}
        boundaries[base] = base
    return v2, v1, boundaries


def _ancestors(path, boundary):
    for _ in range(128):
        yield path
        if path == boundary or path == path.parent:
            return
        path = path.parent


def _quota(path, boundary, version):
    quotas = []
    for current in _ancestors(path, boundary):
        if version == 2:
            fields = _text(current / 'cpu.max').split()
            try:
                quota, period = map(int, fields) if len(fields) == 2 else (0, 0)
            except ValueError:
                continue
        else:
            quota, period = _integer(current, 'cpu.cfs_quota_us'), _integer(current, 'cpu.cfs_period_us')
        if quota and 0 < quota < (1 << 64) and period and 0 < period < (1 << 64):
            quotas.append(quota / period)
    return min(quotas) if quotas else None


def _memory_limit(path, boundary, version):
    values = [_integer(current, 'memory.max' if version == 2 else 'memory.limit_in_bytes', unlimited=version == 1)
              for current in _ancestors(path, boundary)]
    return min((value for value in values if value is not None), default=None)


def _cpus(value):
    """Count a bounded cpuset list without expanding potentially huge ranges."""
    if not value.strip():
        return None
    ranges = []
    try:
        for part in value.strip().split(','):
            fields = part.split('-')
            first = int(fields[0])
            last = first if len(fields) == 1 else int(fields[1])
            if len(fields) > 2 or not 0 <= first <= last <= 1048575:
                return None
            ranges.append((first, last))
        ranges.sort()
        total, end = 0, -1
        for first, last in ranges:
            total += max(0, last - max(first, end + 1) + 1)
            end = max(end, last)
        return total or None
    except ValueError:
        return None


def _read_resources(root=None, *, proc_root=None):
    result = {'available': False, 'cgroup_version': None, 'memory_bytes': None, 'memory_limit_bytes': None,
              'memory_peak_bytes': None, 'cpu_limit': None, 'cpu_capacity': None, 'cpu_usage_seconds': None,
              'cpu_usage_cores': None, 'cpu_percent': None, 'cpu_percent_basis': 'share_of_cgroup_cpu_capacity',
              'oom_events': None, 'oom_kills': None, 'memory_limit_hits': None, 'memory_pressure': False}
    base, proc = Path(root) if root is not None else Path('/sys/fs/cgroup'), Path(proc_root) if proc_root is not None else Path('/proc')
    try:
        v2, v1, boundaries = _layout(base, proc)
        paths, cpuset = [], None
        if v2 is not None:
            result['cgroup_version'] = 2
            paths.append(v2)
            result.update(memory_bytes=_integer(v2, 'memory.current'), memory_limit_bytes=_memory_limit(v2, boundaries[v2], 2),
                          memory_peak_bytes=_integer(v2, 'memory.peak'))
            result['cpu_limit'] = _quota(v2, boundaries[v2], 2)
            usage = _pairs(v2, 'cpu.stat').get('usage_usec')
            if usage is not None:
                result['cpu_usage_seconds'] = usage / 1000000
            events = _pairs(v2, 'memory.events')
            result['oom_events'], result['oom_kills'] = events.get('oom'), events.get('oom_kill')
            cpuset = _cpus(_text(v2 / 'cpuset.cpus.effective'))
        elif v1:
            result['cgroup_version'] = 1
            paths.extend(v1.values())
            if memory := v1.get('memory'):
                result.update(memory_bytes=_integer(memory, 'memory.usage_in_bytes'), memory_limit_bytes=_memory_limit(memory, boundaries[memory], 1),
                              memory_peak_bytes=_integer(memory, 'memory.max_usage_in_bytes'), memory_limit_hits=_integer(memory, 'memory.failcnt'))
                result['oom_kills'] = _pairs(memory, 'memory.oom_control').get('oom_kill')
            if cpu := v1.get('cpu'):
                result['cpu_limit'] = _quota(cpu, boundaries[cpu], 1)
            if cpuacct := v1.get('cpuacct'):
                usage = _integer(cpuacct, 'cpuacct.usage')
                if usage is not None:
                    result['cpu_usage_seconds'] = usage / 1000000000
            if cpus := v1.get('cpuset'):
                cpuset = _cpus(_text(cpus / 'cpuset.effective_cpus')) or _cpus(_text(cpus / 'cpuset.cpus'))
        limits = [value for value in (result['cpu_limit'], cpuset, os.cpu_count() or 1) if value is not None and value > 0]
        result['cpu_capacity'] = min(limits) if result['cgroup_version'] is not None else None
        result['available'] = result['memory_bytes'] is not None or result['cpu_usage_seconds'] is not None
        limit = result['memory_limit_bytes']
        result['memory_pressure'] = bool(limit and result['memory_bytes'] is not None and result['memory_bytes'] >= limit * .9)
        # Identity is used only by the sampler to reset on migration; host paths never reach the API.
        identity = hashlib.sha256('\n'.join(sorted(str(path.resolve()) for path in paths)).encode()).hexdigest() if paths else None
        return result, identity
    except (OSError, ValueError, TypeError, RuntimeError):
        return result, None


def read_resources(root=None, *, proc_root=None):
    """Read cumulative counters. CPU percentages require two ResourceMetrics samples."""
    return _read_resources(root, proc_root=proc_root)[0]


class ResourceMetrics:
    def __init__(self, root=None, *, proc_root=None, clock=time.monotonic):
        self.root, self.proc_root, self.clock = root, proc_root, clock
        self.sample = None
        self.lock = threading.Lock()

    def read(self):
        # Serialize the read as well as the sample update so overlapping HTTP calls cannot reorder it.
        with self.lock:
            result, identity = _read_resources(self.root, proc_root=self.proc_root)
            now, usage = self.clock(), result['cpu_usage_seconds']
            previous = self.sample
            self.sample = (identity, now, usage, result['cpu_capacity']) if usage is not None else None
            if previous is not None and self.sample is not None and identity == previous[0] and now > previous[1] and usage >= previous[2] and result['cpu_capacity'] == previous[3]:
                cores = (usage - previous[2]) / (now - previous[1])
                result['cpu_usage_cores'] = cores
                if result['cpu_capacity']:
                    result['cpu_percent'] = cores * 100 / result['cpu_capacity']
            return result
