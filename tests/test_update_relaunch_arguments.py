"""Repeated updates must not keep appending the data directory and port."""
from shiori.updates import relaunch_arguments


def test_existing_copies_are_replaced_not_repeated(tmp_path):
    argv = ['--no-browser', '--host', '0.0.0.0', '--port', '8765', '--data-dir', '/old', '--strict-port',
            '--data-dir', '/old', '--port', '8765', '--strict-port']
    once = relaunch_arguments(argv, tmp_path, 8766)
    assert once == ['--no-browser', '--host', '0.0.0.0', '--data-dir', str(tmp_path), '--port', '8766', '--strict-port']
    assert relaunch_arguments(once, tmp_path, 8766) == once


def test_equals_form_and_plain_launch(tmp_path):
    assert relaunch_arguments(['--port=9000', f'--data-dir={tmp_path}', '--minimized'], tmp_path, 9000) == [
        '--minimized', '--data-dir', str(tmp_path), '--port', '9000', '--strict-port']
    assert relaunch_arguments([], tmp_path, 8765) == ['--data-dir', str(tmp_path), '--port', '8765', '--strict-port']
