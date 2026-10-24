"""A public version reset must not silently skip packaged-app acceptance checks."""
import ast
from pathlib import Path
import re

import pytest

from mikubot import __version__
from scripts.smoke_contract import effective_contract_version


ROOT = Path(__file__).resolve().parents[1]


def test_first_public_release_keeps_all_current_packaged_app_checks():
    public = effective_contract_version('0.1.0')
    assert public == effective_contract_version('0.10.0')
    # These contracts introduced knowledge import, routing, MCP, isolation,
    # backup/replay, and the frozen worker/admin/computer/distillation suite.
    assert all(public >= threshold for threshold in ((0, 2), (0, 3), (0, 4), (0, 5), (0, 6), (0, 8)))


@pytest.mark.parametrize('version', ['0.1.1', '0.1.2', '0.1.999'])
def test_public_patch_releases_keep_the_full_contract(version):
    assert effective_contract_version(version) == effective_contract_version('0.10.0')


@pytest.mark.parametrize('version, expected', [
    ('0.2.0', (0, 2)), ('0.3.0', (0, 3)), ('0.4.0', (0, 4)),
    ('0.5.0', (0, 5)), ('0.6.0', (0, 6)), ('0.8.0', (0, 8)),
    ('0.9.0', (0, 9)), ('0.10.0', (0, 10)), ('1.0.0', (1, 0)),
])
def test_internal_release_acceptance_contracts_stay_compatible(version, expected):
    assert effective_contract_version(version) == expected


@pytest.mark.parametrize('name', ['smoke_portable.py', 'smoke_installer.py'])
def test_actual_smoke_feature_guards_use_the_full_public_contract(name):
    tree = ast.parse((ROOT / 'scripts' / name).read_text('utf-8-sig'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    assignment = next(node for node in ast.walk(main) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'version' for target in node.targets))
    assert isinstance(assignment.value, ast.Call)
    assert assignment.value.func.id == 'effective_contract_version'
    guards = []
    for node in ast.walk(main):
        if not isinstance(node, ast.Compare) or not any(isinstance(op, (ast.GtE, ast.Gt, ast.Lt, ast.LtE)) for op in node.ops):
            continue
        # Do not let a later check bypass the public-to-internal mapping.
        assert not any(isinstance(part, ast.Attribute) and part.attr in ('version', 'expect_version')
                       for part in ast.walk(node)), ast.unparse(node)
        if any(isinstance(part, ast.Name) and part.id == 'version' for part in ast.walk(node)):
            guards.append(node)
    assert guards, 'Packaged-app feature acceptance gates unexpectedly disappeared'
    for guard in guards:
        enabled = eval(compile(ast.Expression(body=guard), name, 'eval'), {'__builtins__': {}},
                       {'version': effective_contract_version('0.1.0')})
        assert enabled, 'Public release skipped: ' + ast.unparse(guard)


def test_release_version_and_web_asset_cache_labels_match():
    assert __version__ == '0.1.6'
    versions = re.findall(r'\?v=([^"\s]+)', (ROOT / 'mikubot/static/index.html').read_text('utf-8'))
    assert versions and set(versions) == {__version__}


@pytest.mark.parametrize('name', ['smoke_auth_v9.py', 'smoke_workflow_v9.py', 'smoke_portable.py', 'smoke_installer.py'])
def test_packaged_acceptance_defaults_target_the_public_release(name):
    tree = ast.parse((ROOT / 'scripts' / name).read_text('utf-8-sig'))
    options = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument'
               and any(isinstance(arg, ast.Constant) and arg.value == '--expect-version' for arg in node.args)]
    assert len(options) == 1
    assert next(item.value.value for item in options[0].keywords if item.arg == 'default') == __version__
