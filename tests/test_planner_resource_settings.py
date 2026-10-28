"""Compatibility settings must validate atomically without touching secrets."""
from copy import deepcopy

import pytest

from shiori.settings import Settings


@pytest.mark.parametrize('mode', ['auto', 'off', 'json_object', 'json_schema'])
def test_planner_format_and_recovery_round_trip(tmp_path, mode):
    settings = Settings(tmp_path)
    settings.update({'planner': {'structured_output': mode, 'format_retry_enabled': True}})
    loaded = Settings(tmp_path).get()
    assert loaded['planner']['structured_output'] == mode
    assert loaded['planner']['format_retry_enabled'] is True
    assert loaded['memory']['foreground_semantic_timeout_seconds'] == 3
    assert loaded['memory']['semantic_timeout_seconds'] == 8


@pytest.mark.parametrize('patch', [
    {'structured_output': 'anything'}, {'structured_output': None},
    {'structured_output': True}, {'structured_output': []},
    {'format_retry_enabled': 'true'}, {'format_retry_enabled': 1},
])
def test_invalid_planner_settings_leave_existing_file_unchanged(tmp_path, patch):
    settings = Settings(tmp_path)
    settings.update({'planner': {'structured_output': 'off'}})
    before = settings.path.read_bytes()
    current = deepcopy(settings.get())
    with pytest.raises(ValueError):
        settings.update({'planner': patch})
    assert settings.path.read_bytes() == before
    assert settings.get() == current
