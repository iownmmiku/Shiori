"""Per-group QQ send pacing, learned from the platform's own refusals.

Some groups only accept a few messages from the bot per minute. QQ states
the limit in its refusal ("本群每分钟只能发5条消息"), and a refused message was
never delivered, so it is safe to send it again once the window has room.
Without a refusal nothing is limited: the pacer starts open for every group.
"""
from __future__ import annotations

import re
import time
from collections import deque

WINDOW_SECONDS = 60.0
# The platform counts its own minute; a short margin keeps a retried message
# from landing a moment before the oldest counted send leaves that window.
MARGIN_SECONDS = 1.5
DEFAULT_LIMIT = 5
_REJECTED = re.compile(r'rejected: result=(\d+)')
_PER_MINUTE = re.compile(r'每分钟(?:只能|最多)?发(?:送)?\s*(\d{1,3})\s*条')
_RATE_LIMIT_RESULTS = {'299'}


def refusal(response):
    """Classify a failed OneBot action response.

    Returns ``(refused, per_minute)``. ``refused`` means the platform rejected
    the message outright, so it was certainly not delivered. ``per_minute`` is
    the group's limit when the refusal was a rate limit, else ``None``.
    """
    if not isinstance(response, dict) or response.get('status') == 'ok':
        return False, None
    text = ' '.join(str(response.get(key) or '') for key in ('wording', 'message', 'msg'))
    rejected, stated = _REJECTED.search(text), _PER_MINUTE.search(text)
    limit = int(stated.group(1)) if stated else None
    if limit is None and rejected and rejected.group(1) in _RATE_LIMIT_RESULTS:
        limit = DEFAULT_LIMIT
    if limit is not None and not 1 <= limit <= 60:
        limit = None
    return bool(rejected or stated), limit


class SendPacer:
    def __init__(self, clock=time.monotonic, window=WINDOW_SECONDS + MARGIN_SECONDS):
        self.clock, self.window = clock, window
        self.limits = {}
        self._sent = {}
        self._blocked = {}

    def _recent(self, scope_id, now):
        sent = self._sent.get(scope_id)
        if not sent:
            return ()
        while sent and sent[0] <= now - self.window:
            sent.popleft()
        return sent

    def record(self, scope_id):
        """Count a send the platform accepted or may have accepted."""
        self._sent.setdefault(scope_id, deque(maxlen=64)).append(self.clock())

    def learn(self, scope_id, limit):
        self.limits[scope_id] = limit

    def room(self, scope_id):
        """Messages that may go out now, or ``None`` when no limit is known."""
        limit = self.limits.get(scope_id)
        if not limit:
            return None
        now = self.clock()
        if self._blocked.get(scope_id, 0) > now:
            return 0
        return max(0, limit - len(self._recent(scope_id, now)))

    def delay(self, scope_id):
        """Seconds until one more message fits this group's window."""
        limit = self.limits.get(scope_id)
        if not limit:
            return 0.0
        now = self.clock()
        wait = self._blocked.get(scope_id, 0) - now
        recent = self._recent(scope_id, now)
        if len(recent) >= limit:
            wait = max(wait, recent[len(recent) - limit] + self.window - now)
        return max(0.0, wait)

    def refused(self, scope_id, limit):
        """Hold the group until a slot is surely free; returns the wait."""
        self.limits[scope_id] = limit
        now = self.clock()
        recent = self._recent(scope_id, now)
        if len(recent) >= limit:
            until = recent[len(recent) - limit] + self.window
        elif recent:
            # Something outside this process used the budget too; our oldest
            # counted send is the earliest point the platform's window moves.
            until = recent[0] + self.window
        else:
            until = now + self.window / 4
        self._blocked[scope_id] = max(self._blocked.get(scope_id, 0), until)
        return max(0.0, until - now)
