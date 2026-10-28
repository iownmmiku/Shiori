"""Request-local, provisional chat text; never a tool or reasoning channel."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

from .output import strip_memory_citations


_sink = ContextVar('shiori_chat_preview', default=None)
_filter = ContextVar('shiori_chat_preview_filter', default=None)
MAX_ADJUSTED_PREVIEW_CHARS = 120000
MAX_ADJUSTED_PREVIEW_CHECKPOINTS = 20


def preview_active():
    return _sink.get() is not None


class _PublicText:
    """Hold incomplete markers so private tags/known IDs never flash on screen."""
    def __init__(self, memory_ids=(), user_text=''):
        self.memory_ids, self.user_text = tuple(memory_ids), user_text
        self.reset()

    def reset(self):
        self.buffer, self.hidden = '', None

    def feed(self, text):
        self.buffer += text
        public = []
        while self.buffer:
            if self.hidden:
                close = re.search(r'</\s*' + self.hidden + r'\s*>', self.buffer, re.I)
                if close:
                    self.buffer, self.hidden = self.buffer[close.end():], None
                    continue
                self.buffer = self.buffer[-80:]
                break
            starts = [index for marker in ('<', '[', '【') if (index := self.buffer.find(marker)) >= 0]
            if not starts:
                public.append(self.buffer)
                self.buffer = ''
                break
            position = min(starts)
            public.append(self.buffer[:position])
            self.buffer = self.buffer[position:]
            delimiter = {'<': '>', '[': ']', '【': '】'}[self.buffer[0]]
            end = self.buffer.find(delimiter)
            if end < 0:
                if len(self.buffer) <= 512:
                    break
                # An ordinary long array/code expression is not an internal
                # marker. Bound buffering without inventing or deleting text.
                public.append(self.buffer[0])
                self.buffer = self.buffer[1:]
                continue
            marker, self.buffer = self.buffer[:end + 1], self.buffer[end + 1:]
            tag = re.fullmatch(r'<\s*(think|analysis)(?:\s[^>]*)?>', marker, re.I)
            if tag:
                self.hidden = tag.group(1).casefold()
            else:
                public.append(strip_memory_citations(marker, self.memory_ids, user_text=self.user_text))
        return ''.join(public)


class _AdjustedPublicText:
    """Preview complete safe paragraphs under the final output policy.

    Each incoming character is scanned once. Only completed paragraph
    checkpoints invoke the authoritative output formatter, at most twenty
    times; unfinished paragraphs and documents wait for the final result.
    """
    def __init__(self, memory_ids=(), user_text='', output_options=None):
        self.public = _PublicText(memory_ids, user_text)
        self.options = dict(output_options or {})
        self.reset()

    def reset(self):
        self.public.reset()
        self._chars, self._closers = [], []
        self._received, self._checkpoints = 0, 0
        self._emitted = ''
        self._disabled, self._seen_text = False, False
        self._escaped, self._after_cr = False, False
        self._delimiter, self._run = '', 0
        self._fence, self._inline = None, 0
        self._newlines, self._boundary_seen = 0, False

    def _disable(self):
        self._disabled = True
        self._chars.clear()
        self._closers.clear()
        self.public.reset()

    def _flush_delimiter(self):
        marker, count = self._delimiter, self._run
        self._delimiter, self._run = '', 0
        if self._fence:
            if marker == self._fence[0] and count >= self._fence[1]:
                self._fence = None
        elif self._inline:
            if marker == '`' and count == self._inline:
                self._inline = 0
        elif count >= 3:
            self._fence = (marker, count)
        elif marker == '`':
            self._inline = count

    def _scan(self, char):
        if self._delimiter and char != self._delimiter:
            self._flush_delimiter()
        if self._escaped:
            self._escaped = False
            self._newlines = 0
            self._boundary_seen = False
            return False
        if char in ('`', '~'):
            if not self._fence and not self._inline or char == (self._fence[0] if self._fence else '`'):
                self._delimiter, self._run = char, self._run + 1
                self._newlines = 0
                self._boundary_seen = False
                return False
        if self._fence or self._inline:
            self._newlines = 0
            return False
        if char == '\\':
            self._escaped = True
        elif char in '（([{【':
            self._closers.append({'（': '）', '(': ')', '[': ']', '{': '}', '【': '】'}[char])
        elif self._closers and char == self._closers[-1]:
            self._closers.pop()
        if char == '\n':
            self._newlines += 1
        elif char not in ' \t':
            self._newlines = 0
            self._boundary_seen = False
        if self._newlines >= 2 and not self._closers and not self._boundary_seen:
            self._boundary_seen = True
            return True
        return False

    def _checkpoint(self):
        from .output import prepare_reply

        complete = ''.join(self._chars).rstrip()
        if not complete:
            return ''
        self._checkpoints += 1
        try:
            reply = prepare_reply(complete, {'output': self.options})['reply']
        except ValueError:
            # The final result owns delivery errors. Never expose half a
            # protected atom or make a preview failure abort the model request.
            self._disable()
            return ''
        if not isinstance(reply, str) or not reply.startswith(self._emitted):
            self._disable()
            return ''
        delta = reply[len(self._emitted):]
        self._emitted = reply
        if self._checkpoints >= MAX_ADJUSTED_PREVIEW_CHECKPOINTS:
            self._disable()
        return delta

    def feed(self, text):
        if self._disabled:
            return ''
        self._received += len(text)
        if self._received > MAX_ADJUSTED_PREVIEW_CHARS:
            self._disable()
            return ''
        deltas = []
        for char in self.public.feed(text):
            if self._disabled:
                break
            if char == '\n' and self._after_cr:
                self._after_cr = False
                continue
            self._after_cr = char == '\r'
            if char == '\r':
                char = '\n'
            self._chars.append(char)
            if not self._seen_text and not char.isspace():
                self._seen_text = True
                if char in '{[':
                    # A complete JSON document is one protected copyable
                    # value. Even its blank lines cannot commit a preview.
                    self._disable()
                    break
            if self._scan(char):
                delta = self._checkpoint()
                if delta:
                    deltas.append(delta)
        return ''.join(deltas)


@contextmanager
def preview_filter(memory_ids=(), *, user_text='', output_options=None):
    if _sink.get() is None:
        yield
        return
    adjusted = output_options is not None and (output_options.get('reply_max_chars', 0)
                or output_options.get('message_max_chars', 0) or output_options.get('remove_parentheses') is True)
    public = _AdjustedPublicText(memory_ids, user_text, output_options) if adjusted else _PublicText(memory_ids, user_text)
    token = _filter.set(public)
    try:
        yield
    finally:
        _filter.reset(token)


@contextmanager
def preview_events(sink):
    token = _sink.set(sink)
    try:
        with preview_filter():
            yield
    finally:
        _sink.reset(token)


def emit_preview(kind, text=''):
    sink = _sink.get()
    if sink is not None:
        public = _filter.get()
        if public is not None:
            if kind == 'reset':
                public.reset()
            elif kind == 'delta':
                text = public.feed(text)
                if not text:
                    return
        sink(kind, text)
