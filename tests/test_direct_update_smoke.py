"""The frozen-update acceptance must await rollback and clean up late helpers."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'smoke_direct_update', Path(__file__).resolve().parents[1] / 'scripts/smoke_direct_update.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class Clock:
    value = 0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Worker:
    def __init__(self, clock, exits_at):
        self.clock = clock
        self.exits_at = exits_at
        self.terminated = False

    def wait(self, seconds=0):
        return self.terminated or self.clock.value >= self.exits_at

    def terminate(self):
        self.terminated = True


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(smoke.time, 'monotonic', value.monotonic)
    monkeypatch.setattr(smoke.time, 'sleep', value.sleep)
    return value


def test_terminal_result_waits_for_helper_to_exit_before_cleanup(tmp_path, clock):
    result = tmp_path / 'result.json'
    result.write_text(json.dumps({'status': 'failed', 'message': 'rolled back'}))
    worker = Worker(clock, 2)
    assert smoke.wait_for_update(result, worker, 30)['status'] == 'failed'
    assert clock.value >= 2
    assert not worker.terminated


def test_long_rollback_gets_the_configured_deadline(tmp_path, clock):
    result = tmp_path / 'result.json'
    result.write_text(json.dumps({'status': 'failed'}))
    assert smoke.wait_for_update(result, Worker(clock, 170), 360)['status'] == 'failed'
    assert clock.value == 170


def test_nonterminal_state_remains_failure_even_if_helper_exits(tmp_path, clock):
    result = tmp_path / 'result.json'
    result.write_text(json.dumps({'status': 'restarting'}))
    with pytest.raises(AssertionError, match='within 30s'):
        smoke.wait_for_update(result, Worker(clock, 0), 30)
    assert clock.value == 30


def test_failure_cleanup_waits_for_a_late_helper(clock):
    worker = Worker(clock, 10)
    assert smoke.settle_worker(worker, 30)
    assert clock.value == 10
    assert not worker.terminated


def test_cleanup_deadline_stops_only_the_captured_helper_and_cannot_pass(clock):
    worker = Worker(clock, 100)
    assert not smoke.settle_worker(worker, 30)
    assert worker.terminated
    assert clock.value == 30


@pytest.mark.parametrize('value', ['29', '901', '-1'])
def test_deadlines_are_bounded(value):
    with pytest.raises(argparse.ArgumentTypeError):
        smoke.bounded_seconds(value)


def test_acceptance_timeout_is_explicit_and_bounded():
    assert smoke.bounded_seconds('360') == 360
