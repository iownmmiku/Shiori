"""Bound expensive admission before reading request bodies or parsing files."""
from contextlib import asynccontextmanager
import asyncio

from .provider import _PriorityGate, ProviderError


class WorkBusy(ValueError):
    pass


class WorkLimiter:
    def __init__(self, settings, prefix, concurrency, queue):
        self.settings, self.prefix = settings, prefix
        self.defaults = concurrency, queue
        self.gate = _PriorityGate(concurrency, queue)
        self.rejected = 0

    @asynccontextmanager
    async def slot(self):
        config = self.settings.get().get('scheduler', {})
        self.gate.capacity = config.get(self.prefix + '_concurrency', self.defaults[0])
        self.gate.queue_limit = config.get(self.prefix + '_queue_limit', self.defaults[1])
        try:
            await asyncio.wait_for(self.gate.acquire(), 15)
        except (ProviderError, TimeoutError) as exc:
            self.rejected += 1
            raise WorkBusy('当前请求或文件处理队列已满，请稍后重试；尚未开始本次处理') from exc
        try:
            yield
        finally:
            self.gate.release()

    def status(self):
        return {'active': self.gate.active, 'queued': sum(self.gate.counts().values()),
                'limit': self.gate.capacity, 'rejected': self.rejected}
