"""Notices when the event loop stops turning.

The whole service runs on one asyncio loop. A synchronous call that takes
seconds (a slow query, a held lock, a blocking request) freezes every chat at
once, and nothing else in the logs says so.

A heartbeat task stamps the time a few times a second. When it wakes up much
later than it asked to, the loop was stalled for that long, and a
``loop_stall`` event is recorded. A watcher thread looks at the same stamp
from outside: when it goes stale it notes where the loop thread is at that
moment, so the event can say what was running.

Only code locations (file, line, function) are kept, never values.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from collections import deque
from pathlib import Path

BEAT_SECONDS = 0.25
STALL_SECONDS = 1.0
MAX_PER_HOUR = 30
FRAMES = 16
_ROOT = Path(__file__).resolve().parent.parent


def _location(filename, line, name):
    """`shiori/db.py:2059 add_event`; library paths keep their last two parts."""
    path = Path(filename)
    try:
        short = path.resolve().relative_to(_ROOT).as_posix()
    except (ValueError, OSError):
        short = '/'.join(path.parts[-2:])
    return f'{short}:{line} {name}'


class LoopWatch:
    """`record(seconds, stack)` is called on the loop after each stall.

    `stack` lists code locations innermost first; it is empty when the watcher
    thread could not look in time, which happens when the stalled call also
    kept other threads from running.
    """

    def __init__(self, record, *, stall_seconds=STALL_SECONDS, beat_seconds=BEAT_SECONDS, max_per_hour=MAX_PER_HOUR):
        self._record = record
        self._stall, self._interval, self._max = float(stall_seconds), float(beat_seconds), int(max_per_hour)
        self._beat = 0.0
        self._sample = None     # (the beat it was taken after, locations)
        self._recent = deque()
        self._task = self._thread = self._loop_thread = None
        self._stop = threading.Event()

    async def start(self):
        if self._task is not None:
            return
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._stop.clear()
        self._task = asyncio.create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name='shiori-loop-watch', daemon=True)
        self._thread.start()

    async def stop(self):
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 2)

    async def _heartbeat(self):
        while True:
            previous = self._beat
            await asyncio.sleep(self._interval)
            now = time.monotonic()
            self._beat = now
            stalled = now - previous - self._interval
            if stalled >= self._stall:
                sample = self._sample
                self._report(stalled, sample[1] if sample and sample[0] == previous else [])

    def _report(self, seconds, stack):
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        if len(self._recent) >= self._max:
            return
        self._recent.append(now)
        try:
            self._record(round(seconds, 2), stack)
        except Exception:
            pass  # A diagnostic must never take the loop down with it.

    def _watch(self):
        while not self._stop.wait(self._interval):
            beat = self._beat
            if time.monotonic() - beat < self._stall or (self._sample and self._sample[0] == beat):
                continue
            frame = sys._current_frames().get(self._loop_thread)
            stack = []
            while frame is not None and len(stack) < FRAMES:
                stack.append(_location(frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name))
                frame = frame.f_back
            self._sample = (beat, stack)
