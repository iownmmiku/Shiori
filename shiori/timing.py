"""Small per-turn wall-clock measurements; no message text or network hooks."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import math
import time
from uuid import uuid4


_current: ContextVar[TurnTiming | None] = ContextVar('shiori_turn_timing', default=None)


def _active():
    trace = _current.get()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    # Background tasks inherit ContextVars but do not belong to the waiting
    # foreground turn. They must not mutate its timings or share its trace ID.
    return trace if trace and not trace.closed and trace.owner is task else None


class TurnTiming:
    def __init__(self):
        self.id = uuid4().hex[:16]
        self.owner = asyncio.current_task()
        self.started = time.perf_counter()
        self.durations = {}
        self.closed = False

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            self.closed = True
            _current.reset(token)

    def snapshot(self):
        return {'trace_id': self.id, 'total_ms': round((time.perf_counter() - self.started) * 1000, 2),
                **{name: round(seconds * 1000, 2) for name, seconds in self.durations.items()}}


def current_trace_id():
    trace = _active()
    return trace.id if trace else None


def add_duration(name, seconds):
    trace = _active()
    if trace is not None and isinstance(seconds, (int, float)) and math.isfinite(seconds):
        trace.durations[name] = trace.durations.get(name, 0) + max(0, seconds)


@contextmanager
def measure(name):
    started = time.perf_counter()
    try:
        yield
    finally:
        add_duration(name, time.perf_counter() - started)


def timed(name):
    """Measure an async helper without altering its return/error semantics."""
    def decorate(function):
        from functools import wraps
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with measure(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


@asynccontextmanager
async def timed_lock(lock, name):
    """Include cancelled waits without holding the measurement during work."""
    with measure(name):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()
