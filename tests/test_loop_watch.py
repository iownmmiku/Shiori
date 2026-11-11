"""Loop watch (loop_watch.py): a stalled event loop is reported with where it
was, a loop that keeps turning is not, and the watch can never hurt the loop."""
import asyncio
import time

import pytest

from shiori.db import Store
from shiori.loop_watch import LoopWatch
from shiori.runtime_logs import severity, summary


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def hold_the_loop(seconds):
    time.sleep(seconds)


def watch(seen, **options):
    return LoopWatch(lambda seconds, stack: seen.append((seconds, stack)), stall_seconds=0.2, beat_seconds=0.05, **options)


@pytest.mark.anyio
async def test_a_stalled_loop_is_reported_with_where_it_was():
    seen = []
    watcher = watch(seen)
    await watcher.start()
    await asyncio.sleep(0.15)
    hold_the_loop(0.6)
    await asyncio.sleep(0.2)
    await watcher.stop()
    assert len(seen) == 1 and 0.4 <= seen[0][0] <= 2
    # Innermost first, as project-relative locations without any values.
    assert seen[0][1][0].startswith('tests/test_loop_watch.py:') and seen[0][1][0].endswith(' hold_the_loop')
    assert any(line.endswith(' test_a_stalled_loop_is_reported_with_where_it_was') for line in seen[0][1])


@pytest.mark.anyio
async def test_a_loop_that_keeps_turning_is_not_reported():
    seen = []
    watcher = watch(seen)
    await watcher.start()
    for _ in range(10):
        hold_the_loop(0.02)  # short work between awaits is normal
        await asyncio.sleep(0.03)
    await watcher.stop()
    assert seen == []


@pytest.mark.anyio
async def test_reports_are_capped_and_a_failing_recorder_is_harmless():
    seen = []
    watcher = watch(seen, max_per_hour=1)
    await watcher.start()
    for _ in range(2):
        await asyncio.sleep(0.1)
        hold_the_loop(0.5)
    await asyncio.sleep(0.2)
    await watcher.stop()
    assert len(seen) == 1

    def broken(seconds, stack):
        seen.append('called')
        raise RuntimeError('database is locked')
    watcher = LoopWatch(broken, stall_seconds=0.2, beat_seconds=0.05)
    await watcher.start()
    await asyncio.sleep(0.1)
    hold_the_loop(0.5)
    await asyncio.sleep(0.2)
    assert seen[-1] == 'called' and not watcher._task.done()
    await watcher.stop()
    await watcher.stop()  # stopping twice is fine


def test_the_event_is_stored_and_shown_as_a_warning(tmp_path):
    store = Store(tmp_path)
    stack = ['shiori/db.py:2059 add_event', 'shiori/social.py:1052 plan']
    store.add_event('loop_stall', '主循环停顿，期间所有对话都在等待', seconds=3.2, where=stack[0], stack=stack)
    details = next(item['details'] for item in store.list_events(5) if item['kind'] == 'loop_stall')
    store.close()
    assert details['stack'] == stack and details['seconds'] == 3.2
    assert summary('loop_stall', details) == '停顿 3.2s · shiori/db.py:2059 add_event'
    assert summary('loop_stall', {'seconds': 1.4, 'where': '', 'stack': []}).endswith('位置未知')
    assert severity('loop_stall', details) == 'WARNING'
