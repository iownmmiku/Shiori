"""Malformed whitespace-heavy model fences cannot block the event loop."""
import subprocess
import sys


def test_whitespace_heavy_fences_reject_without_unbounded_backtracking():
    # A subprocess bounds a regression even if it reintroduces CPU-bound regex
    # work, which an asyncio timeout in the same thread cannot interrupt.
    code = '''
from shiori.planner_result import parse_plan, PlanValidationError
fence = chr(96) * 3
for text in (fence + ' ' * 15000 + 'x',
             fence + 'json' + ' ' * 15000 + 'x',
             fence + ' ' * 15000 + 'x' + fence):
    try:
        parse_plan(text, message_id='m', user_id='u')
    except PlanValidationError as exc:
        assert exc.code == 'invalid_json'
    else:
        raise AssertionError('Malformed plan must not be accepted')
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
