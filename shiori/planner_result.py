"""Bounded Planner decoding; formatting compatibility never invents a decision."""
from __future__ import annotations

import json


MAX_PLAN_CHARS = 16000
MAX_PLAN_DEPTH = 32
FIELDS = frozenset(('action', 'target_message_id', 'target_user_id',
                    'addressed_to_bot', 'intent', 'reason', 'needed_info'))


def response_format(mode):
    """Transport constraints supplement, never replace, local validation."""
    if mode in ('auto', 'json_object'):
        return {'type': 'json_object'}
    if mode != 'json_schema':
        return None
    return {'type': 'json_schema', 'json_schema': {'name': 'shiori_planner', 'strict': True,
        'schema': {'type': 'object', 'additionalProperties': False,
            'properties': {'action': {'type': 'string', 'enum': ['reply', 'skip']},
                'target_message_id': {'type': 'string'}, 'target_user_id': {'type': 'string'},
                'addressed_to_bot': {'type': 'boolean'}, 'intent': {'type': 'string'},
                'reason': {'type': 'string'},
                'needed_info': {'type': 'array', 'items': {'type': 'string'}}},
            'required': sorted(FIELDS)}}}


class PlanValidationError(ValueError):
    def __init__(self, code, field='', *, line=None, column=None):
        super().__init__('Planner 返回的计划格式无效，未据此生成回复。')
        self.code = code
        # Unknown JSON keys are untrusted text, unsuitable for diagnostics.
        self.field = field if field in FIELDS else ''
        self.line, self.column = line, column
        self.stage = 'parse' if code in ('invalid_json', 'duplicate_key', 'response_too_large', 'excessive_depth') else 'validation'


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PlanValidationError('duplicate_key', key)
        value[key] = item
    return value


def _constant(_):
    raise PlanValidationError('invalid_json')


def parse_plan(text, *, message_id, user_id):
    if not isinstance(text, str):
        raise PlanValidationError('invalid_type')
    if len(text) > MAX_PLAN_CHARS:
        raise PlanValidationError('response_too_large')
    text = text.strip().lstrip('\ufeff').strip()
    # Avoid overlapping whitespace regexes: an unterminated fence containing
    # a long space run can otherwise monopolize the event loop before JSON
    # parsing or the asynchronous Planner deadline gets a chance to run.
    if text.startswith('```'):
        if len(text) < 6 or not text.endswith('```'):
            raise PlanValidationError('invalid_json')
        text = text[3:-3]
        if text[:4].casefold() == 'json':
            text = text[4:]
        text = text.strip()
    # Bound nesting before invoking the JSON decoder. Braces in strings are
    # ordinary content, including escaped quotes and backslashes.
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{[':
            depth += 1
            if depth > MAX_PLAN_DEPTH:
                raise PlanValidationError('excessive_depth')
        elif char in '}]':
            depth -= 1
    try:
        choice = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise PlanValidationError('invalid_json', line=exc.lineno, column=exc.colno) from None
    except RecursionError:
        raise PlanValidationError('excessive_depth') from None
    except ValueError as exc:
        if isinstance(exc, PlanValidationError):
            raise
        raise PlanValidationError('invalid_json') from None
    if not isinstance(choice, dict):
        raise PlanValidationError('invalid_shape')
    for field in ('action', 'target_message_id', 'target_user_id', 'addressed_to_bot', 'intent', 'reason'):
        if field not in choice:
            raise PlanValidationError('missing_field', field)
    if choice['action'] not in ('reply', 'skip'):
        raise PlanValidationError('invalid_action', 'action')
    if not isinstance(choice['target_message_id'], str) or choice['target_message_id'] != message_id:
        raise PlanValidationError('target_mismatch', 'target_message_id')
    if type(choice['target_user_id']) not in (str, int) or str(choice['target_user_id']) != str(user_id):
        raise PlanValidationError('target_mismatch', 'target_user_id')
    if type(choice['addressed_to_bot']) is not bool:
        raise PlanValidationError('invalid_type', 'addressed_to_bot')
    for field in ('intent', 'reason'):
        if not isinstance(choice[field], str):
            raise PlanValidationError('invalid_type', field)
    if choice['action'] == 'reply' and not choice['intent'].strip():
        raise PlanValidationError('empty_intent', 'intent')
    # Older prompts explicitly permitted omitting this optional local lookup.
    # Missing means no lookup; explicit null/other types remain invalid.
    needed = choice.get('needed_info', [])
    if not isinstance(needed, list) or not all(isinstance(item, str) for item in needed):
        raise PlanValidationError('invalid_type', 'needed_info')
    return {key: choice[key] for key in FIELDS if key != 'needed_info'} | {'needed_info': needed}
