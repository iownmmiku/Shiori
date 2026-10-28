"""A small OpenAI-compatible HTTP provider, with no synthetic replies."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import sys
import heapq
import uuid
import hashlib
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
from .settings import (resolve_model, MAX_OUTPUT_TOKENS, CUSTOM_PARAM_RESERVED,
                       CUSTOM_PARAM_MAX_ABS, CUSTOM_PARAM_MAX_STRING, CUSTOM_PARAM_MAX_JSON,
                       CUSTOM_PARAM_MAX_COUNT)
from .timezones import config_zone, day_bounds, refresh_dispatch_time
from .model_stream import (receive_chat, ModelStreamError, IncompleteModelStreamError,
                           usage_token_count, preserve_completed_body)
from .chat_preview import emit_preview, preview_active
from .timing import add_duration


class ProviderError(ValueError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class ProviderStreamError(ProviderError):
    """Incomplete transport result; callers may retry side-effect-free analysis."""


class ProviderContentFilterError(ProviderError):
    """The upstream refused the request; do not relay its body or retry elsewhere."""
    def __init__(self, *, usage=None):
        super().__init__('模型服务对本次请求触发了内容过滤，未采用返回内容或执行工具。')
        self.usage = usage


class ProviderOutputBudgetError(ProviderError):
    """The transport finished but the model exhausted its generation allowance."""
    def __init__(self, message='模型输出额度已用完，思考未结束或正文被截断；请增加输出额度或选择适合此任务的模型', *, usage=None, retryable=False):
        super().__init__(message,retryable=retryable)
        self.usage=usage


class ProviderBudgetExceededError(ProviderError):
    """A local budget or Planner quota actually vetoed the request."""


class ProviderTimeoutError(ProviderError):
    """A connection or response deadline expired."""


class ProviderConnectionError(ProviderError):
    """The connection failed, possibly after dispatch."""


class ProviderRateLimitError(ProviderError):
    """An upstream 429 is not proof of a local budget veto."""


def is_budget_exceeded(exc):
    if isinstance(exc, (ProviderTimeoutError, ProviderConnectionError, ProviderRateLimitError, ProviderOutputBudgetError, ProviderContentFilterError)):
        return False
    if isinstance(exc, ProviderBudgetExceededError):
        return True
    # Compatibility with integrations still raising plain ValueError/ProviderError.
    # An accounting estimate following an uncertain completion is not exhaustion.
    text = str(exc)
    if '已保留预算估算' in text:
        return False
    return bool(re.search(r'(?:预算|额度).{0,12}(?:不足|已达|已用完|上限)|次数已达', text))


def request_timeout(config):
    seconds = config.get('timeout_seconds', 90)
    if type(seconds) is not int or not 5 <= seconds <= 600:
        raise ProviderError('模型等待超时须为5—600秒的整数')
    return httpx.Timeout(seconds, connect=min(12, seconds))


BACKGROUND_PURPOSES = {'knowledge', 'memory_extract', 'memory', 'jargon_learning', 'expression_learning', 'context_digest', 'distillation', 'sticker_vision', 'sticker_select', 'embedding_index'}
_queue_wait_ms = ContextVar('model_queue_wait_ms', default=0)
_request_trace = ContextVar('model_request_trace', default=None)


@contextmanager
def model_request_trace(*, deadline=None):
    """A caller-owned, text-free trace; inherited background tasks cannot edit it."""
    state = {'trace_id': uuid.uuid4().hex[:16], '_started': time.monotonic(),
             '_owner': asyncio.current_task(), '_deadline': deadline, '_durations': {}}
    token = _request_trace.set(state)
    try:
        yield state
    finally:
        _request_trace.reset(token)


def mark_model_stage(stage, *, request_id=None):
    if stage not in ('recall', 'queue', 'prepare', 'dispatch', 'response', 'parse'):
        return
    state = _request_trace.get()
    if state is None or state.get('_owner') is not asyncio.current_task():
        return
    now = time.monotonic()
    previous = state.get('stage')
    if previous:
        totals = state['_durations']
        totals[previous] = totals.get(previous, 0) + max(0, now - state['_stage_started'])
    state.update(stage=stage, _stage_started=now)
    if isinstance(request_id, str) and re.fullmatch(r'[0-9a-fA-F-]{1,64}', request_id):
        state['request_id'] = request_id
        ids = state.setdefault('request_ids', [])
        if request_id not in ids and len(ids) < 8:
            ids.append(request_id)
    if stage == 'dispatch':
        state['dispatched'] = True


def model_trace_snapshot(state):
    if not state:
        return {}
    now = time.monotonic()
    durations = dict(state.get('_durations', {}))
    if state.get('stage'):
        stage = state['stage']
        durations[stage] = durations.get(stage, 0) + max(0, now - state['_stage_started'])
    return {**{key: state[key] for key in ('trace_id', 'stage', 'request_id', 'dispatched',
                'structured_output', 'format_compatibility_fallbacks', 'format_retry_count') if key in state},
            'request_ids': list(state.get('request_ids', [])),
            'elapsed_ms': round(max(0, now - state['_started']) * 1000, 1),
            'timings_ms': {key: round(value * 1000, 1) for key, value in durations.items()}}


def _request_termination(error):
    if isinstance(error, asyncio.CancelledError):
        state = _request_trace.get()
        if state is not None and state.get('_owner') is not asyncio.current_task():
            state = None
        deadline = state.get('_deadline') if state else None
        guard = state.get('_timeout_guard') if state else None
        expired = guard.expired() if guard is not None else deadline is not None and asyncio.get_running_loop().time() >= deadline
        return 'planner_deadline' if expired else 'cancelled'
    if isinstance(error, ProviderTimeoutError):
        return 'provider_timeout'
    if isinstance(error, ProviderStreamError):
        return 'stream_incomplete'
    if isinstance(error, ProviderConnectionError):
        return 'connection_error'
    return 'request_failed' if error is not None else 'unknown'


class _PriorityGate:
    """Bounded parallel requests, foreground ahead of background; no preemption."""
    def __init__(self, capacity=1, queue_limit=32):
        self.busy, self.counter, self.queue = False, 0, []
        self.capacity, self.queue_limit, self.active = capacity, queue_limit, 0

    def locked(self):
        return self.busy

    async def acquire(self, priority=0):
        self._prune_cancelled()
        future = asyncio.get_running_loop().create_future()
        entry = {'future': future, 'granted': False, 'priority': priority}
        self.counter += 1
        if self.active < self.capacity and not self.queue:
            self.busy = entry['granted'] = True
            self.active += 1
            future.set_result(None)
        else:
            if sum(not item[2]['future'].done() for item in self.queue)>=self.queue_limit:
                raise ProviderError('模型请求队列已满，请稍后重试；未发送本次模型请求。')
            heapq.heappush(self.queue, (priority, self.counter, entry))
        try:
            await future
        except BaseException:
            if entry['granted']:
                self.release()
            else:
                future.cancel()
                self._prune_cancelled()
            raise

    def _prune_cancelled(self):
        self.queue = [item for item in self.queue if not item[2]['future'].done()]
        heapq.heapify(self.queue)

    def release(self):
        self.active = max(0,self.active-1)
        while self.queue and self.active < self.capacity:
            _, _, entry = heapq.heappop(self.queue)
            if not entry['future'].done():
                entry['granted'] = True
                self.active += 1
                entry['future'].set_result(None)
        self.busy = self.active>0

    def counts(self):
        # Planner has an intermediate priority but remains foreground work:
        # it must not consume background capacity or disappear from its status.
        return {'foreground': sum(not e.get('background', p > 0) and not e['future'].done()
                                  for p, _, e in self.queue),
                'background': sum(e.get('background', p > 0) and not e['future'].done()
                                  for p, _, e in self.queue)}


class _ModelScheduler:
    """Grant global, model and background capacity together, never partially.

    A queued request owns no scarce resource. Ready foreground work therefore
    bypasses both a saturated model and background work awaiting another slot.
    Replies precede queued planning, and planning precedes background learning;
    requests already holding their slots are never preempted.
    The existing gates remain read-only scheduling/status views here; standalone
    _PriorityGate users keep their original acquire/release behavior.
    """
    def __init__(self, provider):
        self.provider, self.counter, self.waiting = provider, 0, []

    def _configure(self):
        config = self.provider.settings.get().get('scheduler', {})
        self.provider._global_gate.capacity = config.get('global_model_concurrency', 2)
        self.provider._global_gate.queue_limit = config.get('global_model_queue_limit', 32)
        self.provider._background_gate.queue_limit = config.get('background_queue_limit', 16)
        for gate in self.provider._provider_locks.values():
            gate.capacity = config.get('model_concurrency', 1)
            gate.queue_limit = config.get('model_queue_limit', 32)

    def _views(self):
        self.provider._global_gate.queue = list(self.waiting)
        self.provider._background_gate.queue = [item for item in self.waiting if item[2]['background']]
        for key, gate in self.provider._provider_locks.items():
            gate.queue = [item for item in self.waiting if item[2]['key'] == key]
        for gate in (self.provider._global_gate, self.provider._background_gate, *self.provider._provider_locks.values()):
            gate.busy = gate.active > 0

    def _drain(self):
        self.waiting = [item for item in self.waiting if not item[2]['future'].done()]
        self.waiting.sort(key=lambda item: item[:2])
        global_gate, background_gate = self.provider._global_gate, self.provider._background_gate
        for item in list(self.waiting):
            if global_gate.active >= global_gate.capacity:
                break
            entry = item[2]
            gate = self.provider._provider_locks[entry['key']]
            if gate.active >= gate.capacity or (entry['background'] and background_gate.active >= background_gate.capacity):
                continue
            self.waiting.remove(item)
            entry['granted'] = True
            global_gate.active += 1
            gate.active += 1
            if entry['background']:
                background_gate.active += 1
            entry['future'].set_result(None)
        self._views()

    async def acquire(self, key, background, *, priority=0):
        self.provider._provider_locks.setdefault(key, _PriorityGate())
        self._configure()
        self.counter += 1
        entry = {'key': key, 'background': background, 'granted': False, 'released': False,
                 'future': asyncio.get_running_loop().create_future()}
        item = (10 if background else priority, self.counter, entry)
        self.waiting.append(item)
        self._drain()
        if not entry['granted']:
            gate = self.provider._provider_locks[key]
            if (len(self.waiting) > self.provider._global_gate.queue_limit or
                    len(gate.queue) > gate.queue_limit or
                    (background and len(self.provider._background_gate.queue) > self.provider._background_gate.queue_limit)):
                self.waiting.remove(item)
                entry['future'].cancel()
                self._views()
                raise ProviderError('模型请求队列已满，请稍后重试；未发送本次模型请求。')
        try:
            await entry['future']
        except BaseException:
            if entry['granted']:
                self.release(entry)
            else:
                entry['future'].cancel()
                self.waiting = [item for item in self.waiting if item[2] is not entry]
                self._drain()
            raise
        return entry

    def release(self, entry):
        if entry['released']:
            return
        entry['released'] = True
        self.provider._global_gate.active -= 1
        self.provider._provider_locks[entry['key']].active -= 1
        if entry['background']:
            self.provider._background_gate.active -= 1
        # Lower limits never interrupt dispatched requests. They apply to new
        # grants after those requests finish; higher limits admit queued work.
        self._configure()
        self._drain()


def endpoint(base: str, resource='chat/completions') -> str:
    base = base.strip().rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError('请填写有效的模型服务地址，例如 https://服务地址/v1')
    if base.endswith('/chat/completions'):
        base = base[:-len('/chat/completions')]
    return base + '/' + resource


def input_size(messages, tools=None):
    """Shared text/image accounting for prompt trimming and final dispatch."""
    chars, images = 0, 0
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'image_url':
                    images += 1
                elif isinstance(part, dict):
                    chars += len(str(part.get('text', '')))
        else:
            chars += len(str(content or ''))
        chars += sum(len(json.dumps(message[key], ensure_ascii=False))
                     for key in ('tool_calls', 'function_call') if message.get(key))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    return chars, images


def estimate_tokens(messages, tools=None):
    chars, image_tokens = 0, 0
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, list):
            for part in content:
                if part.get('type') == 'image_url':
                    image_tokens += 1024
                else:
                    chars += len(str(part.get('text', '')))
        else:
            chars += len(str(content))
        for key in ('tool_calls','function_call','name','tool_call_id'):
            if message.get(key): chars+=len(json.dumps(message[key],ensure_ascii=False))
        chars+=8
    if tools: chars+=len(json.dumps(tools,ensure_ascii=False))
    return max(1, chars // 2 + image_tokens)


class Provider:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        if hasattr(store, 'configure_timezone'):
            store.configure_timezone(settings)
        self._call_lock = asyncio.Lock()
        self._provider_locks = {}
        self._background_gate = _PriorityGate()
        self._global_gate = _PriorityGate(2, 32)
        self._scheduler = _ModelScheduler(self)
        self._reservations = {}
        self._health = {}
        self._planner_format_rejections = OrderedDict()

    @staticmethod
    def _format_key(cfg):
        # Changing service accounts must not inherit another account's feature
        # negotiation. Only a digest is retained, never its key or endpoint.
        value = [endpoint(cfg['base_url']), cfg.get('model'), cfg.get('api_key', '')]
        return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _unsupported_format(response):
        if response.status_code not in (400, 422):
            return False
        text = response.text[:4000].casefold()
        try:
            body = response.json()
            error = body.get('error') if isinstance(body, dict) else None
        except (ValueError, TypeError):
            error = None
        if isinstance(error, dict):
            code = str(error.get('code', '')).casefold()
            if any(marker in code for marker in ('content_filter', 'safety', 'policy', 'refusal')):
                return False
            text = str(error.get('message', ''))[:4000].casefold()
            if any(marker in text for marker in ('safety', 'policy', 'content_filter', 'content filter', 'refusal', 'moderation', '安全', '审核', '过滤')):
                return False
            param = error.get('param')
            if param is not None and param != 'response_format':
                return False
            if param == 'response_format' and code in ('unsupported_parameter', 'unknown_parameter', 'unrecognized_parameter'):
                return True
            text = str(error.get('message', ''))[:4000].casefold()
        # The rejection must refer to this parameter, not an unrelated error
        # which happens to contain both words elsewhere in its response body.
        if any(marker in text for marker in ('safety', 'policy', 'content_filter', 'content filter', 'refusal', 'moderation', '安全', '审核', '过滤')):
            return False
        unsupported = r'(?:not supported|unsupported|does not support|unknown parameter|unknown field|unrecognized|不支持|未知参数)'
        return bool(re.search(r'response_format.{0,60}' + unsupported + '|' + unsupported + r'.{0,60}response_format', text, re.S))

    def _planner_format(self, cfg, purpose):
        if purpose != 'planner':
            return 'off', None, None
        from .planner_result import response_format
        mode = self.settings.get().get('planner', {}).get('structured_output', 'auto')
        if mode not in ('auto', 'off', 'json_object', 'json_schema'):
            raise ProviderError('Planner 结构化输出模式无效。')
        key = self._format_key(cfg)
        rejected = self._planner_format_rejections
        now = time.monotonic()
        for stale in [item for item, until in rejected.items() if until <= now]:
            rejected.pop(stale, None)
        if mode == 'auto' and key in rejected:
            return mode, None, key
        return mode, response_format(mode), key

    def _reject_planner_format(self, key):
        rejected = self._planner_format_rejections
        rejected[key] = time.monotonic() + 600
        rejected.move_to_end(key)
        while len(rejected) > 128:
            rejected.popitem(last=False)

    def resolve(self, purpose='chat', scope_id=None):
        selected = None
        if scope_id and purpose in ('chat','computer','tool_summary'):
            selected = self.store.get_scope(scope_id).get('model_id') or None
        return resolve_model(self.settings.get(), purpose, model_id=selected)

    def status(self):
        config = self.settings.get()
        items = [dict(config['model'], id='default', name='默认模型', enabled=True), *[resolve_model(config,model_id=x['id']) for x in config.get('providers', [])]]
        now = time.monotonic()
        return {'scheduler': {'background_concurrency': 1, 'background_running': self._background_gate.locked(),
                              'global_concurrency': self._global_gate.capacity, 'global_running': self._global_gate.active,
                              'global_queued': sum(self._global_gate.counts().values()),
                              'background_queued': sum(self._background_gate.counts().values()),
                              'foreground_queued': sum(x.counts()['foreground'] for x in self._provider_locks.values())},
                'routes': {key: {**{k: v for k, v in self.resolve(key).items() if k in ('id', 'name', 'model', 'enabled', 'vision', 'tools')}, 'provider_id': self.resolve(key).get('id', 'default')} for key in ('chat', 'planner', 'memory', 'gate', 'summary', 'distillation', 'embedding')},
                'reserved': {'requests': len(self._reservations), 'tokens': sum(x['input_tokens'] + x['output_tokens'] for x in self._reservations.values()),
                             'cost': sum(x['cost'] for x in self._reservations.values())},
                'providers': [{**{k: item.get(k) for k in ('id', 'name', 'model', 'enabled')},
                               **{k: v for k, v in self._health.get(item['id'], {}).items() if k != 'until'},
                               'configured': bool(item.get('base_url') and item.get('model')),
                               'busy': bool(self._provider_locks.get(item['id']) and self._provider_locks[item['id']].locked()),
                               'circuit_open': self._health.get(item['id'], {}).get('until', 0) > now} for item in items]}

    @asynccontextmanager
    async def _provider_slot(self, candidate, purpose='chat'):
        started = time.monotonic()
        mark_model_stage('queue')
        try:
            key = candidate.get('id', 'default')
            if purpose == 'planner':
                entry = await self._scheduler.acquire(key, False, priority=5)
            else:
                entry = await self._scheduler.acquire(key, purpose in BACKGROUND_PURPOSES)
        finally:
            # A cancelled/expired waiter still spent time in the queue, even
            # though it never reserved budget or obtained a dispatch slot.
            waiting_seconds = max(0, time.monotonic() - started)
            add_duration('model_queue_ms', waiting_seconds)
        waited = round(waiting_seconds * 1000)
        mark_model_stage('prepare')
        token = _queue_wait_ms.set(waited)
        try:
            yield waited
        finally:
            _queue_wait_ms.reset(token)
            self._scheduler.release(entry)

    def planner_totals(self, scope_id):
        start, end = day_bounds(self.settings)
        with self.store._lock:
            row = self.store._conn.execute("SELECT COUNT(*),COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE json_extract(data,'$.scope_id')=? AND created_at>=? AND created_at<? AND json_extract(data,'$.purpose')='planner'",
                                           (scope_id, start, end)).fetchone()
        return {'calls': int(row[0]), 'tokens': int(row[1])}

    def _planner_limits(self, scope_id):
        defaults = {'min_interval_ms': 1200, 'daily_calls': 300, 'daily_tokens': 12000, 'max_age_ms': 15000,
                    **self.settings.get().get('planner', {})}
        scope = self.store.get_scope(scope_id)
        return {key: scope.get('planner_' + key) if scope.get('planner_' + key) is not None else value for key, value in defaults.items()}

    def _check_planner_budget(self, scope_id, estimated_input=0, max_output=0, pending=None):
        limits, used = self._planner_limits(scope_id), self.planner_totals(scope_id)
        pending = list(self._reservations.values()) if pending is None else pending
        reserved = [x for x in pending if x['purpose'] == 'planner' and x['scope_id'] == scope_id]
        if limits['daily_calls'] and used['calls'] + len(reserved) + (1 if estimated_input else 0) > limits['daily_calls']:
            raise self._budget_error('当前会话今日 Planner 次数已达上限。')
        tokens = used['tokens'] + sum(x['input_tokens'] + x['output_tokens'] for x in reserved) + estimated_input + max_output
        if limits['daily_tokens'] and tokens > limits['daily_tokens']:
            raise self._budget_error('当前会话今日 Planner Token 预算不足。')

    @staticmethod
    def _same_model(left, right):
        fields = ('id', 'base_url', 'model', 'api_key', 'enabled', 'vision', 'tools', 'temperature', 'top_p', 'input_price', 'output_price', 'timeout_seconds', 'model_type',
                  'stream_mode', 'first_token_timeout_seconds', 'total_timeout_seconds', 'timeout_fallback', 'max_tokens', 'reasoning_effort', 'custom_params')
        return all(left.get(key) == right.get(key) for key in fields)

    def _check_send(self, before_send):
        if getattr(self, '_updating', False): raise ProviderError('正在退出并安装更新，请稍后重新打开聊天。')
        if before_send is not None and before_send() is False:
            raise ProviderError('本次请求来源、权限或模型配置已变化，未发送模型请求。')

    def _scope_limit(self, scope_id):
        if scope_id.startswith('persona:'):
            # Internal accounting labels are not editable conversations and
            # must not create ghost scopes in the management UI.
            scope = next((x for x in self.store.list_scopes() if x['id'] == scope_id), {})
        else:
            scope = self.store.get_scope(scope_id)
        return float(scope.get('daily_tokens') or 0)

    def _budget_error(self, message):
        if hasattr(self.store, '_conn'):
            from .usage_ledger import breakdown
            start, end = day_bounds(self.settings)
            pending = breakdown(self.store, start, end)['unconfirmed']
            if pending['calls']:
                message += f" 其中{pending['calls']}次消费待核实，暂保留{pending['tokens']} Token、¥{pending['cost']:.4f}；可在本机数据统计中核对。"
        return ProviderBudgetExceededError(message)

    def _check_reserved_limits(self, reservation):
        # Synchronous final check: settings may have lowered a limit while the
        # HTTP client or another request was awaiting. No await follows this
        # check until the actual send starts.
        config = self.settings.get()
        budget = config['budget']
        totals = self.store.usage_totals()
        pending = list(self._reservations.values())
        if budget['daily_tokens'] and totals['tokens'] + sum(x['input_tokens'] + x['output_tokens'] for x in pending) > budget['daily_tokens']:
            raise self._budget_error('当前 Token 预算已不足以容纳在途请求，本次未发送。')
        if budget['daily_cost'] and totals['cost'] + sum(x['cost'] for x in pending) > budget['daily_cost'] + 1e-12:
            raise self._budget_error('当前费用预算已不足以容纳在途请求，本次未发送。')
        current = self._reservations.get(reservation)
        if current is None:
            raise ProviderError('本次预算预留已失效。')
        scope = current['scope_id']
        if current['purpose'] == 'planner':
            self._check_planner_budget(scope)
        limit = self._scope_limit(scope)
        scope_reserved = sum(x['input_tokens'] + x['output_tokens'] for x in pending if x['scope_id'] == scope)
        if limit and self.store.usage_totals(scope)['tokens'] + scope_reserved > limit:
            raise self._budget_error('当前会话预算已不足以容纳在途请求，本次未发送。')

    async def _reserve(self, cfg, scope_id, purpose, estimated_input, max_output, *, replace=None):
        async with self._call_lock:
            config = self.settings.get()
            budget = config['budget']
            # Every active reservation counts even across a local midnight: a
            # request can finish and be billed after its original start day.
            pending = [value for key, value in self._reservations.items() if key is not replace]
            if purpose == 'planner':
                self._check_planner_budget(scope_id, estimated_input, max_output, pending)
            totals = self.store.usage_totals()
            reserved_tokens = sum(x['input_tokens'] + x['output_tokens'] for x in pending)
            reserved_cost = sum(x['cost'] for x in pending)
            allowed_output = int(max_output)
            min_output = min(16, allowed_output) if allowed_output else 0
            if budget['daily_tokens']:
                remaining = int(budget['daily_tokens'] - totals['tokens'] - reserved_tokens - estimated_input)
                if remaining < min_output or remaining < 0:
                    raise self._budget_error('今日剩余 Token 预算不足以完成本次请求。')
                allowed_output = min(allowed_output, remaining)
            scope_limit = self._scope_limit(scope_id)
            if scope_limit:
                scope_used = self.store.usage_totals(scope_id)['tokens']
                scope_reserved = sum(x['input_tokens'] + x['output_tokens'] for x in pending if x['scope_id'] == scope_id)
                remaining = int(scope_limit - scope_used - scope_reserved - estimated_input)
                if remaining < min_output or remaining < 0:
                    raise self._budget_error('当前会话的今日 Token 预算不足。')
                allowed_output = min(allowed_output, remaining)
            input_price, output_price = float(cfg.get('input_price', 0)), float(cfg.get('output_price', 0))
            input_cost = estimated_input * input_price / 1000000
            if budget['daily_cost']:
                remaining_cost = budget['daily_cost'] - totals['cost'] - reserved_cost - input_cost
                # Input cost is checked independently, including free output.
                if remaining_cost < -1e-12:
                    raise self._budget_error('剩余费用预算不足以承担本次输入。')
                if output_price > 0:
                    affordable = int(max(0, remaining_cost) * 1000000 / output_price)
                    if affordable < min_output:
                        raise self._budget_error('剩余费用预算不足以完成本次请求。')
                    allowed_output = min(allowed_output, affordable)
            key = replace if replace is not None else object()
            request_id = self._reservations.get(key, {}).get('request_id') or uuid.uuid4().hex
            self._reservations[key] = {'request_id': request_id, 'scope_id': scope_id, 'purpose': purpose, 'model': cfg['model'],
                'provider_id': cfg.get('id', 'default'), 'input_tokens': estimated_input, 'output_tokens': allowed_output,
                'cost': input_cost + allowed_output * output_price / 1000000,'cost_known':bool(input_price or output_price),
                'queue_wait_ms': _queue_wait_ms.get(), '_started': time.monotonic()}
            return key, allowed_output

    async def _settle(self, reservation, record=None, *, uncertain=False, termination_reason='unknown'):
        async with self._call_lock:
            pending = self._reservations.get(reservation)
            if pending is None:
                return  # One request settles once, including repeated cleanup.
            if record is not None:
                self.store.record_usage(dict(record, request_id=record.get('request_id') or pending['request_id']))
            elif uncertain and pending is not None:
                # A read timeout/cancellation after dispatch is not proof that
                # the provider billed nothing. Retain the reservation estimate
                # as explicit unknown usage instead of silently freeing budget.
                self.store.record_usage({**{key: value for key, value in pending.items() if not key.startswith('_')},
                    'estimated': True, 'unconfirmed': True,
                    'latency_ms': round(max(0, time.monotonic() - pending.get('_started', time.monotonic())) * 1000),
                    'termination_reason': termination_reason})
            # Keep the reservation if persistence fails: unknown consumption
            # must not silently become available budget after a database error.
            self._reservations.pop(reservation, None)

    async def complete(self, messages, *, tools=None, config=None, purpose='chat', scope_id='web:default', max_tokens=None, before_send=None):
        primary = dict(config) if config is not None else self.resolve(purpose, scope_id)
        seconds = primary.get('total_timeout_seconds', 300)
        if type(seconds) is not int or not 5 <= seconds <= 1800:
            raise ProviderError('模型总等待时限须为5—1800秒的整数')
        try:
            async with asyncio.timeout(seconds):
                return await self._complete_candidates(messages, tools=tools, config=config, purpose=purpose,
                                                       scope_id=scope_id, max_tokens=max_tokens, before_send=before_send)
        except TimeoutError as exc:
            if purpose == 'chat': emit_preview('reset')
            raise ProviderTimeoutError('本轮模型请求达到总等待时限，已停止等待；请查看用量记录确认消费状态。') from exc
        except BaseException:
            if purpose == 'chat': emit_preview('reset')
            raise

    async def _complete_candidates(self, messages, *, tools=None, config=None, purpose='chat', scope_id='web:default', max_tokens=None, before_send=None):
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS):
            raise ProviderError(f'max_tokens 须为 1—{MAX_OUTPUT_TOKENS} 的整数')
        primary = dict(config) if config is not None else self.resolve(purpose, scope_id)
        if primary.get('model_type') == 'embedding':
            raise ProviderError('嵌入模型不能生成聊天回复，请选择对话模型')
        route_snapshot = dict(primary)
        candidates = [primary]
        if config is None:
            cfg = self.settings.get()
            by_id = {x['id']: x for x in cfg.get('providers', [])}
            for provider_id in cfg.get('routing', {}).get('fallback', []):
                item = resolve_model(cfg, purpose, model_id=provider_id) if provider_id in by_id else None
                if item and item.get('enabled', True) and provider_id != primary.get('id'):
                    candidates.append(item)
        last_error = None
        image_request = any(isinstance(m.get('content'), list) and any(p.get('type') == 'image_url' for p in m['content'] if isinstance(p, dict)) for m in messages)
        for index, candidate in enumerate(candidates):
            if purpose == 'chat': emit_preview('reset')
            key = candidate.get('id', 'default')
            if image_request and not candidate.get('vision'):
                if index == 0:
                    raise ProviderError('当前任务模型未启用视觉输入，请在模型池确认能力')
                continue
            state = self._health.setdefault(key, {'failures': 0, 'last_error': '', 'last_attempt_at': '', 'last_success_at': ''})
            if config is None and state.get('until', 0) > time.monotonic():
                last_error = ProviderError('模型暂时熔断，请稍后重试或在模型池手动测试连接。', retryable=True)
                continue
            state['last_attempt_at'] = datetime.now(timezone.utc).isoformat()
            def guard(candidate=candidate, index=index):
                self._check_send(before_send)
                if config is not None:
                    return True
                current_cfg = self.settings.get()
                if current_cfg.get('paused'):
                    return False
                if not self._same_model(route_snapshot, self.resolve(purpose, scope_id)):
                    return False
                current = self.resolve(purpose, scope_id) if index == 0 else resolve_model(current_cfg, purpose, model_id=candidate.get('id'))
                if index and candidate.get('id') not in current_cfg.get('routing', {}).get('fallback', []):
                    return False
                return self._same_model(candidate, current)
            try:
                async with self._provider_slot(candidate, purpose):
                    self._check_send(guard)
                    result = await self._complete(messages, tools=tools if candidate.get('tools', True) else None,
                                                  config=candidate, purpose=purpose, scope_id=scope_id, max_tokens=max_tokens, before_send=guard)
                    # An in-flight response may arrive after its connection,
                    # selected session model, or initiating authority changed.
                    # Usage has already been settled by _complete; keep that
                    # receipt, but never expose stale tool calls for execution.
                    try:
                        self._check_send(guard)
                    except ProviderError as exc:
                        raise ProviderError('模型响应已收到，但来源、权限或模型配置已变化，未继续使用其结果。') from exc
            except ProviderError as exc:
                state['last_error'] = str(exc)
                # A health failure and permission to repeat an uncertain
                # generation are separate decisions.
                if exc.retryable or isinstance(exc, (ProviderTimeoutError, ProviderConnectionError)):
                    state['failures'] += 1
                    if state['failures'] >= 2:
                        state['until'] = time.monotonic() + 30
                last_error = exc
                if not exc.retryable:
                    raise
                continue
            state.update(failures=0, last_error='', last_success_at=datetime.now(timezone.utc).isoformat(), until=0)
            current_cfg = self.settings.get()
            current = resolve_model(current_cfg, purpose, model_id=key)
            if result.get('tool_calls') and (not current or not tools or not candidate.get('tools', True) or not current.get('tools', True) or not current.get('enabled', True)):
                raise ProviderError('实际响应模型未获准使用工具，返回的工具请求未执行。')
            if index:
                self.store.add_event('model', f'主模型暂不可用，本次使用已配置备用模型：{candidate.get("name", key)}')
            return result
        raise last_error or ProviderError('没有可用的模型，请检查任务路由与模型能力。')

    @staticmethod
    def _max_output_tokens(cfg, purpose, requested):
        """Resolve the single output-budget rule shared by reserve and request.

        The configured ceiling is zero for legacy/default configurations. Chat
        without an explicit caller budget uses the configured ceiling when set,
        otherwise the historical 1000-token chat default. Every explicit task
        budget is respected but never exceeds a user-configured ceiling.
        """
        configured = cfg.get('max_tokens', 0)
        if type(configured) is not int or not 0 <= configured <= MAX_OUTPUT_TOKENS:
            configured = 0
        if requested is None:
            requested = configured if purpose == 'chat' and configured else 1000
        if configured:
            requested = min(requested, configured)
        return requested

    @staticmethod
    def _custom_params(cfg):
        """Return JSON-safe provider parameters without request-shaping fields."""
        raw = cfg.get('custom_params') or {}
        if not isinstance(raw, dict) or len(raw) > CUSTOM_PARAM_MAX_COUNT:
            raise ProviderError('自定义参数须为不超过50项的对象。')
        cleaned = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', key):
                raise ProviderError('自定义参数键名无效。')
            if key in CUSTOM_PARAM_RESERVED:
                continue
            if value is None or isinstance(value, bool):
                cleaned[key] = value
            elif isinstance(value, int) and abs(value) <= CUSTOM_PARAM_MAX_ABS:
                cleaned[key] = value
            elif isinstance(value, float) and math.isfinite(value) and abs(value) <= CUSTOM_PARAM_MAX_ABS:
                cleaned[key] = value
            elif isinstance(value, str) and len(value) <= CUSTOM_PARAM_MAX_STRING:
                cleaned[key] = value
            else:
                raise ProviderError('自定义参数值无效或超出支持范围。')
        try:
            encoded = json.dumps(cleaned, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        except (TypeError, ValueError) as exc:
            raise ProviderError('自定义参数无法序列化为 JSON，请检查参数值。') from exc
        if len(encoded) > CUSTOM_PARAM_MAX_JSON:
            raise ProviderError('自定义参数整体过大，请精简后重试。')
        return cleaned

    @staticmethod
    def _check_payload(payload):
        """Fail before reservation when the request cannot be serialized to JSON."""
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProviderError('模型请求无法序列化为 JSON，请检查模型参数、消息或工具定义。') from exc

    async def _complete(self, messages, *, tools=None, config=None, purpose='chat', scope_id='web:default', max_tokens=None, before_send=None):
        messages = refresh_dispatch_time(messages, self.settings, purpose=purpose)
        cfg = dict(config or self.settings.get()['model'])
        if not cfg.get('base_url') or not cfg.get('model'):
            raise ProviderError('请先在设置中填写模型服务地址与模型名称，并测试连接。')
        limits = self.settings.get().get('context', {})
        chars, images = input_size(messages, tools)
        if chars > limits.get('max_input_chars', 80000) or images > limits.get('max_images', 3):
            raise ProviderError('本次模型输入超过文字或图片总上限，请减少资料、历史或工具输出后重试。')
        url = endpoint(cfg['base_url'])
        timeout = request_timeout(cfg)
        requested_max_tokens = self._max_output_tokens(cfg, purpose, max_tokens)
        payload = self._custom_params(cfg)
        payload.update({'model': cfg['model'], 'messages': messages, 'temperature': cfg.get('temperature', 0.7), 'top_p': cfg.get('top_p', 1.0), 'max_tokens': requested_max_tokens})
        if cfg.get('reasoning_effort'):
            payload['reasoning_effort'] = cfg['reasoning_effort']
        stream_mode = cfg.get('stream_mode', 'off')
        if stream_mode != 'off':
            payload['stream'] = True
            payload['stream_options'] = {'include_usage': True}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        format_mode, structured_format, format_key = self._planner_format(cfg, purpose)
        if purpose == 'planner':
            # Custom parameters cannot override the Planner's selected contract.
            payload.pop('response_format', None)
            if structured_format:
                payload['response_format'] = structured_format
            trace = _request_trace.get()
            if trace is not None and trace.get('_owner') is asyncio.current_task():
                trace['structured_output'] = structured_format['type'] if structured_format else 'off'
        self._check_payload(payload)
        reservation, max_tokens = await self._reserve(cfg, scope_id, purpose, estimate_tokens(messages, tools), requested_max_tokens)
        payload['max_tokens'] = max_tokens
        headers = {'Accept': 'application/json'}
        if cfg.get('api_key'):
            headers['Authorization'] = 'Bearer ' + cfg['api_key']
        started = time.monotonic()
        record, dispatched, ttft_ms = None, False, None
        previewed = False
        def preview(kind, text):
            nonlocal previewed
            try:
                self._check_send(before_send)
            except ProviderError as exc:
                raise ProviderError('模型响应已收到，但来源、权限或模型配置已变化，未继续使用其结果。') from exc
            if purpose == 'chat':
                previewed = True
                emit_preview(kind, text)
        connect_failures = transient_failures = format_fallbacks = 0
        # Never repeat a tool continuation or an executing computer task after
        # an unknown result. Initial text generation can opt into extra cost.
        uncertain_fallback = bool(cfg.get('timeout_fallback', False)) and purpose in ('chat', 'planner', 'summary') and not any(
            message.get('role') == 'tool' or message.get('tool_calls') for message in messages)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                    event_hooks={'response': [preserve_completed_body]}) as client:
                response = None
                for attempt in range(5):
                    self._check_reserved_limits(reservation)
                    self._check_send(before_send)
                    payload['messages'] = refresh_dispatch_time(messages, self.settings, purpose=purpose, for_transport=True)
                    try:
                        dispatched = True
                        mark_model_stage('dispatch', request_id=self._reservations.get(reservation, {}).get('request_id'))
                        if payload.get('stream'):
                            response, ttft_ms = await receive_chat(client, url, payload, headers,
                                first_token_seconds=cfg.get('first_token_timeout_seconds', 90), check=lambda: self._check_send(before_send),
                                on_event=preview if purpose == 'chat' and preview_active() else None)
                        else:
                            response = await client.post(url, json=payload, headers=headers)
                        mark_model_stage('response')
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        dispatched = False
                        connect_failures += 1
                        if connect_failures < 2:
                            await asyncio.sleep(.4)
                            continue
                        if isinstance(exc, httpx.ConnectTimeout):
                            raise ProviderTimeoutError('连接模型服务超时，请检查地址、网络或本地模型是否启动。', retryable=True) from exc
                        raise ProviderConnectionError('无法连接模型服务，请检查地址、网络或本地模型是否启动。', retryable=True) from exc
                    except httpx.TimeoutException as exc:
                        raise ProviderTimeoutError('模型响应超时，本次消费无法确认，已保留预算估算，请稍后重试。', retryable=uncertain_fallback) from exc
                    except httpx.HTTPError as exc:
                        raise ProviderConnectionError('模型连接中断，本次消费无法确认，已保留预算估算。') from exc
                    except IncompleteModelStreamError as exc:
                        self.store.add_event('model_stream_incomplete',
                            '模型流缺少完整结束标记；本次消费待确认，未采用残缺结果',
                            scope_id=scope_id, purpose=purpose, provider_id=cfg.get('id', 'default'),
                            **exc.diagnostics)
                        raise ProviderStreamError(str(exc), retryable=uncertain_fallback) from exc
                    except ModelStreamError as exc:
                        raise ProviderError(str(exc)) from exc
                    if response.status_code >= 300:
                        dispatched = False
                    if (purpose == 'planner' and format_mode == 'auto' and format_fallbacks == 0
                            and payload.get('response_format') and self._unsupported_format(response)):
                        # A definite 400/422 rejected the parameter before a
                        # generation. Reuse this reservation; never repeat an
                        # accepted or uncertain response for feature negotiation.
                        format_fallbacks += 1
                        payload.pop('response_format', None)
                        self._reject_planner_format(format_key)
                        trace = _request_trace.get()
                        if trace is not None and trace.get('_owner') is asyncio.current_task():
                            trace['structured_output'] = 'off'
                            trace['format_compatibility_fallbacks'] = format_fallbacks
                        self.store.add_event('planner_format_compatibility',
                            '模型明确不支持结构化输出参数，本次按兼容模式判断',
                            scope_id=scope_id, mode='auto', status=response.status_code)
                        continue
                    if response.status_code in (429, 502, 503) and transient_failures == 0:
                        transient_failures += 1
                        await asyncio.sleep(.5)
                        continue
                    if response.status_code == 400 and payload.get('stream'):
                        # A 400 explicitly rejected this request. Capability
                        # negotiation must never repeat an accepted generation.
                        error_text = response.text[:4000].casefold()
                        if 'stream_options' in error_text and 'stream_options' in payload:
                            payload.pop('stream_options')
                            continue
                        if stream_mode == 'auto' and 'stream' in error_text:
                            payload.pop('stream', None)
                            payload.pop('stream_options', None)
                            self.store.add_event('model', '模型不支持流式参数，本次改用普通响应')
                            continue
                    if response.status_code == 400 and payload.get('tools'):
                        payload.pop('tools', None)
                        payload.pop('tool_choice', None)
                        _, payload['max_tokens'] = await self._reserve(cfg, scope_id, purpose,
                            estimate_tokens(messages), payload['max_tokens'], replace=reservation)
                        self.store.add_event('model', '模型拒绝工具参数，本次回退到文本回答；本地命令仍可使用')
                        continue
                    break
            if response is None:
                raise ProviderError('模型服务未返回响应')
            if response.status_code >= 300:
                if response.status_code == 429:
                    raise ProviderRateLimitError('模型服务返回429：服务限流或额度受限，请检查服务商用量或稍后重试。', retryable=True)
                reason = {401: '密钥无效或未授权', 403: '无访问权限', 404: '地址或模型不存在', 429: '服务限流或额度不足'}.get(response.status_code, '服务请求失败')
                raise ProviderError(f'模型服务返回 {response.status_code}：{reason}。', retryable=response.status_code == 429 or 500 <= response.status_code <= 599)
            try:
                data = response.json()
                choice = data['choices'][0]
                if not isinstance(choice, dict):
                    raise ValueError()
                finish_reason = choice.get('finish_reason')
                message = choice.get('message')
                filtered = (finish_reason == 'content_filter'
                            or isinstance(message, dict) and bool(message.get('refusal')))
                if data.get('error') and not filtered:
                    raise ValueError()
                if not isinstance(message, dict):
                    if not filtered:
                        raise ValueError()
                    message = {}
                raw_usage = data.get('usage') or {}
                if not isinstance(raw_usage, dict):
                    if not filtered:
                        raise ValueError()
                    raw_usage = {}
                usage_incomplete = any(raw_usage.get(key) is None for key in ('prompt_tokens', 'completion_tokens'))
                def usage_value(key, fallback):
                    nonlocal usage_incomplete
                    try:
                        return self._usage_count(raw_usage.get(key), fallback)
                    except ValueError:
                        if not filtered:
                            raise
                        usage_incomplete = True
                        return fallback
                input_tokens = usage_value('prompt_tokens', estimate_tokens(messages, payload.get('tools')))
                # A policy stop with no trustworthy usage must not be treated as
                # free. Keep the reserved estimate, marked as unconfirmed.
                output_estimate = self._reservations.get(reservation, {}).get('output_tokens', max_tokens) if filtered else estimate_tokens([message])
                output_tokens = usage_value('completion_tokens', output_estimate)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ProviderError('服务返回内容不是兼容的聊天响应，请检查 API 地址。') from exc
            cost = (input_tokens * float(cfg.get('input_price', 0)) + output_tokens * float(cfg.get('output_price', 0))) / 1000000
            record = {'scope_id': scope_id, 'model': cfg['model'], 'provider_id': cfg.get('id', 'default'), 'purpose': purpose,
                      'input_tokens': input_tokens, 'output_tokens': output_tokens, 'cost': cost,
                      'cost_known':bool(cfg.get('input_price') or cfg.get('output_price')),
                      'request_id': self._reservations.get(reservation, {}).get('request_id'), 'ttft_ms': ttft_ms,
                      'estimated': usage_incomplete,
                      'queue_wait_ms': _queue_wait_ms.get(),
                      'latency_ms': round((time.monotonic() - started) * 1000)}
            if filtered:
                if usage_incomplete:
                    record['unconfirmed'] = True
                self.store.add_event('model_content_filter', '模型返回内容过滤标记，未采用正文或执行工具',
                    scope_id=scope_id, purpose=purpose, provider_id=cfg.get('id', 'default'),
                    reason='content_filter' if finish_reason == 'content_filter' else 'refusal',
                    output_tokens=output_tokens, usage_confirmed=not usage_incomplete)
                if purpose == 'chat':
                    emit_preview('reset')
                raise ProviderContentFilterError(usage=record)
            if message.get('tool_calls') and not payload.get('tools'):
                raise ProviderError('实际请求未允许工具，服务返回的工具调用未执行。')
            content = message.get('content') or ''
            if isinstance(content, list):
                content = '\n'.join(str(x.get('text', '')) for x in content if isinstance(x, dict))
            if finish_reason=='length':
                self.store.add_event('model_output_budget','模型正常结束，但输出额度耗尽，未采用截断内容或执行工具',
                    scope_id=scope_id,purpose=purpose,provider_id=cfg.get('id','default'),
                    output_tokens=output_tokens,has_content=bool(str(content).strip()),
                    reasoning_tokens=(raw_usage.get('completion_tokens_details') or {}).get('reasoning_tokens') if isinstance(raw_usage.get('completion_tokens_details'),dict) else None)
                safe_fallback=purpose in ('chat','planner','summary','memory_extract','jargon_learning','expression_learning','sticker_select') and not any(m.get('role')=='tool' or m.get('tool_calls') for m in messages)
                raise ProviderOutputBudgetError(usage=record,retryable=safe_fallback)
            if not str(content).strip() and not message.get('tool_calls'):
                raise ProviderError('模型已正常结束但没有返回正文；用量已记录，请检查模型参数与任务路由')
            if purpose == 'chat' and preview_active():
                if message.get('tool_calls'): preview('reset', '')
                elif not previewed and content: preview('delta', str(content))
            return {'content': str(content), 'tool_calls': message.get('tool_calls') or [], 'message': message,
                    'usage': record, 'finish_reason': finish_reason}
        finally:
            await asyncio.shield(self._settle(reservation, record, uncertain=dispatched and record is None,
                                            termination_reason=_request_termination(sys.exception())))

    @staticmethod
    def _usage_count(value, fallback):
        if value is None:
            return fallback
        return usage_token_count(value)

    async def list_models(self, config=None):
        cfg = config or self.resolve()
        headers = {'Authorization': 'Bearer ' + cfg['api_key']} if cfg.get('api_key') else {}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(endpoint(cfg.get('base_url', ''), 'models'), headers=headers)
            if response.status_code != 200:
                raise ProviderError(f'模型列表读取失败（HTTP {response.status_code}），仍可手动填写模型名。')
            data = response.json()
            return [str(x['id']) for x in data.get('data', []) if isinstance(x, dict) and x.get('id')][:500]
        except httpx.HTTPError as exc:
            raise ProviderError('无法读取模型列表，仍可手动填写模型名。') from exc

    async def embed(self, texts, *, scope_id='web:default', before_send=None, config=None, background=False):
        """Embeddings share atomic budget reservations with chat requests."""
        if not isinstance(texts, list) or not 1 <= len(texts) <= 32 or any(not isinstance(x, str) or not x.strip() or len(x) > 6000 for x in texts) or sum(map(len, texts)) > 48000:
            raise ProviderError('向量请求须为 1—32 个非空片段，每段最多 6000 字、合计最多 48000 字')
        current_config = self.settings.get()
        testing = config is not None
        cfg = dict(config) if testing else resolve_model(current_config, 'embedding')
        memory = current_config.get('memory', {})
        async with self._provider_slot(cfg, 'embedding_index' if background else 'embedding'):
            if (not testing and not memory.get('semantic_enabled')) or not cfg.get('model') or not cfg.get('base_url') or not cfg.get('enabled', True):
                raise ProviderError('语义检索未启用或未填写 Embedding 模型')
            if cfg.get('model_type') == 'chat':
                raise ProviderError('语义检索需要嵌入模型，请检查模型类型')
            url = endpoint(cfg['base_url'], 'embeddings')
            def guard():
                self._check_send(before_send)
                current = self.settings.get()
                selected = resolve_model(current, 'embedding', model_id=cfg.get('id', 'default')) if testing else resolve_model(current, 'embedding')
                return not current.get('paused') and (testing or current.get('memory', {}).get('semantic_enabled')) and self._same_model(cfg, selected)
            self._check_send(guard)
            estimated = sum(max(1, len(x) // 2) for x in texts)
            price = float(cfg.get('input_price', 0))
            reservation, _ = await self._reserve(cfg, scope_id, 'embedding', estimated, 0)
            headers = {'Authorization': 'Bearer ' + cfg['api_key']} if cfg.get('api_key') else {}
            started = time.monotonic()
            record, dispatched = None, False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.get('timeout_seconds', 90), connect=min(10, cfg.get('timeout_seconds', 90))), follow_redirects=False) as client:
                    self._check_reserved_limits(reservation)
                    self._check_send(guard)
                    dispatched = True
                    response = await client.post(url, headers=headers,
                                                 json={'model': cfg['model'], 'input': texts, 'encoding_format': 'float'})
                if response.status_code != 200:
                    dispatched = False
                    raise ProviderError(f'Embedding 服务返回 HTTP {response.status_code}，请检查模型能力与配置')
                try:
                    payload = response.json()
                    entries = sorted(payload['data'], key=lambda x: x['index'])
                    if [x['index'] for x in entries] != list(range(len(texts))):
                        raise ValueError()
                    vectors = [x['embedding'] for x in entries]
                    if not vectors or not 1 <= len(vectors[0]) <= 8192:
                        raise ValueError()
                    if any(not isinstance(v, list) or len(v) != len(vectors[0]) or any(type(x) not in (float, int) or not math.isfinite(x) for x in v) or not any(v) for v in vectors):
                        raise ValueError()
                    raw_usage = payload.get('usage') or {}
                    if not isinstance(raw_usage, dict):
                        raise ValueError()
                    tokens = self._usage_count(raw_usage.get('prompt_tokens', raw_usage.get('total_tokens')), estimated)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ProviderError('Embedding 服务未返回有效的有序向量') from exc
                record = {'scope_id': scope_id, 'purpose': 'embedding', 'model': cfg['model'], 'provider_id': cfg.get('id', 'default'),
                          'input_tokens': tokens, 'output_tokens': 0, 'cost': tokens * price / 1000000,
                          'estimated': 'prompt_tokens' not in raw_usage and 'total_tokens' not in raw_usage,
                          'queue_wait_ms': _queue_wait_ms.get(),
                          'latency_ms': round((time.monotonic() - started) * 1000)}
                self._check_send(guard)
                return vectors
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                dispatched = False
                raise ProviderError('Embedding 服务连接失败，本次使用本地词法检索') from exc
            except httpx.HTTPError as exc:
                raise ProviderError('Embedding 服务连接中断或超时，已保留预算估算，本次使用本地词法检索') from exc
            finally:
                await asyncio.shield(self._settle(reservation, record, uncertain=dispatched and record is None,
                                                termination_reason=_request_termination(sys.exception())))
