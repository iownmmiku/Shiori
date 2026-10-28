"""Real plugin packages: bounded inspection, atomic replacement, and child execution."""
import base64
import gzip
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.plugin_packages import MAX_FILE, inspect_archive
from shiori.plugins import PluginService
from shiori.runs import TERMINAL
from test_computer_auto_v9 import automatic, wait_for


@pytest.fixture
def anyio_backend(): return 'asyncio'


def manifest(**patch):
    return dict(schema_version=1, id='packaged_example', name='多文件示例', version='1.0.0',
                description='使用本地资源，不联网', entrypoint='main.py',
                input_schema={'type': 'object', 'properties': {'value': {'type': 'integer', 'title': '数字'}},
                              'required': ['value'], 'additionalProperties': False}, **patch)


def files(**patch):
    return {'plugin.json': json.dumps(manifest(), ensure_ascii=False).encode(),
            'main.py': b'from helper import format_value\ndef run(input):\n    return format_value(input["value"])\n',
            'helper.py': b'from pathlib import Path\ndef format_value(value):\n    return {"value": value, "asset": (Path(__file__).parent / "assets/value.txt").read_text("utf-8")}\n',
            'assets/value.txt': '资源文本'.encode(), 'README.md': '# 示例'.encode(), **patch}


def packed(members, kind='zip', wrapper='outer/plugin/'):
    output = io.BytesIO()
    if kind == 'zip':
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, raw in members.items(): archive.writestr(wrapper + name, raw)
    else:
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, raw in members.items():
                entry = tarfile.TarInfo(wrapper + name); entry.size = len(raw)
                archive.addfile(entry, io.BytesIO(raw))
    return output.getvalue()


def install(service, members=None, kind='zip', replace=False):
    scan = service.scan('example.' + ('zip' if kind == 'zip' else 'tar.gz'), packed(members or files(), kind))
    return service.install_scan(scan['scan_id'], scan['candidates'][0]['candidate_id'], replace=replace)


@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_scan_strips_wrapper_identifies_candidates_and_never_imports(automatic, kind):
    service = PluginService(automatic.service.data_dir, automatic.service)
    marker = automatic.service.data_dir / 'must-not-import'
    members = files()
    members['main.py'] = f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\ndef run(input):\n    return input\n'.encode()
    raw = packed(members, kind)
    scan = service.scan('example.' + ('zip' if kind == 'zip' else 'tar.gz'), raw)
    assert not marker.exists() and service.list() == []
    candidate = scan['candidates'][0]
    assert candidate['root'] == 'outer/plugin' and candidate['file_count'] == 5
    item = service.install_scan(scan['scan_id'], candidate['candidate_id'])
    assert item['schema_version'] == 2 and not item['enabled'] and not marker.exists()
    assert item['export_url'].endswith('/packaged_example/export')
    archive = zipfile.ZipFile(io.BytesIO(service.export(item['id'])))
    assert archive.read(item['id'] + '/assets/value.txt') == '资源文本'.encode()
    assert len(inspect_archive('roundtrip.zip', service.export(item['id']))) == 1


@pytest.mark.anyio
async def test_multifile_package_runs_in_existing_child_and_validates_schema(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    item = install(service); service.activate(item['id'], True)
    identity = automatic.access.identity('local', 'shiori')
    with pytest.raises(ValueError, match='不符合定义'):
        service.submit(item['id'], {'value': 'wrong'}, 'web:default', 'shiori', identity)
    job = service.submit(item['id'], {'value': 7}, 'web:default', 'shiori', identity)
    await wait_for(lambda: automatic.runs.get(job['id'])['status'] in TERMINAL)
    result = automatic.runs.get(job['id'])
    assert result['status'] == 'completed', result
    assert json.loads(result['result']['reply']) == {'value': 7, 'asset': '资源文本'}
    await automatic.runs.close()


def test_package_replacement_rollback_and_asset_tampering(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    first = install(service); service.activate(first['id'], True)
    changed = files(); changed['assets/value.txt'] = b'replacement'
    with pytest.raises(ValueError, match='同名'): install(service, changed)
    second = install(service, changed, replace=True)
    assert second['revision'] > first['revision'] and not second['enabled']
    restored = service.rollback(first['id'])
    assert not restored['enabled'] and restored['package_hash'] == first['package_hash']
    resource = service.folder / '.packages' / restored['id'] / restored['package_hash'] / 'assets/value.txt'
    resource.write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='变化'): service.get(first['id'])


def test_failed_metadata_commit_keeps_old_active_package(automatic, monkeypatch):
    service = PluginService(automatic.service.data_dir, automatic.service)
    first = install(service); old = service.activate(first['id'], True)
    changed = files(); changed['assets/value.txt'] = b'new'
    replace = Path.replace
    def fail_commit(path, target):
        if path == service.folder / (first['id'] + '.tmp'): raise OSError('fixture disk failure')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', fail_commit)
    with pytest.raises(OSError, match='disk failure'): install(service, changed, replace=True)
    assert service.get(first['id']) == old


def test_old_single_json_exports_normally_and_schema_creation_is_compatible(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    payload = {'id': 'legacy_plugin', 'name': '旧插件', 'description': '兼容旧保存接口', 'code': 'def run(input):\n    return input\n'}
    old = service.save(payload)
    assert old['schema_version'] == 1
    assert inspect_archive('legacy.zip', service.export(old['id']))[0]['manifest']['id'] == old['id']
    new = service.save(dict(payload, input_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}},
                            readme='聊天生成的说明', version='1.0.1'))
    assert new['schema_version'] == 2 and new['manifest']['version'] == '1.0.1' and not new['enabled']
    service.save(dict(payload, code='def run(input):\n    return {"edited": True}\n'))
    assert service.get(old['id'])['manifest']['input_schema']['properties']['text']['type'] == 'string'


@pytest.mark.parametrize('name', ['../outside', '/outside', 'C:/outside', 'a\\outside', 'a/../outside', 'CON.txt', 'a/file.',
                                 'a/file?.py', 'a/file*.py', 'a/file<.py', 'a/file>.py', 'a/file|.py', 'a/file".py'])
@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_unsafe_paths_rejected_before_candidate_or_extraction(name, kind):
    raw = packed({name: b'bad'}, kind, wrapper='')
    if name == 'a\\outside' and kind == 'zip':
        # ZipFile normalizes the host OS separator while writing on Windows.
        raw = raw.replace(b'a/outside', b'a\\outside')
    with pytest.raises(ValueError, match='路径'):
        inspect_archive('bad.' + ('zip' if kind == 'zip' else 'tgz'), raw)


def test_links_duplicate_case_and_bombs_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        entry = zipfile.ZipInfo('linked.py'); entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, b'/tmp/victim')
    with pytest.raises(ValueError, match='链接'): inspect_archive('bad.zip', output.getvalue())
    for type_ in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            entry = tarfile.TarInfo('linked.py'); entry.type = type_; entry.linkname = '/tmp/victim'
            archive.addfile(entry)
        with pytest.raises(ValueError, match='链接'): inspect_archive('bad.tgz', output.getvalue())
    with pytest.raises(ValueError, match='大小写冲突'):
        inspect_archive('bad.zip', packed({'file.py': b'a', 'FILE.py': b'b'}))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('bomb.bin', b'0' * (MAX_FILE + 1))
    with pytest.raises(ValueError, match='单个文件'): inspect_archive('bomb.zip', output.getvalue())
    with pytest.raises(ValueError, match='单个文件'):
        inspect_archive('big.zip', packed({'big.bin': b'x' * (MAX_FILE + 1)}))


def test_valid_highly_compressed_asset_remains_importable_after_export(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    members = files(); members['assets/repetitive.txt'] = b'x' * (MAX_FILE - 100)
    item = install(service, members, kind='tar')
    exported = service.export(item['id'])
    result = inspect_archive('roundtrip.zip', exported)
    assert not result[0].get('error') and result[0]['files']['assets/repetitive.txt'] == members['assets/repetitive.txt']


def test_tar_metadata_decompression_limit_precedes_tar_parser(monkeypatch):
    from shiori import plugin_packages
    output = io.BytesIO()
    # A large PAX record is parsed before tarfile yields the first regular file.
    # Keep this fixture tiny by reducing the configured uncompressed ceiling.
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT,
                      pax_headers={'comment': 'x' * 16000}) as archive:
        entry = tarfile.TarInfo('main.py'); entry.size = 1
        archive.addfile(entry, io.BytesIO(b'x'))
    raw = gzip.compress(output.getvalue())
    assert len(raw) < 1000
    monkeypatch.setattr(plugin_packages, 'MAX_TAR_BYTES', 4096)
    monkeypatch.setattr(plugin_packages.tarfile, 'open', lambda **kwargs: pytest.fail('unbounded TAR metadata reached parser'))
    with pytest.raises(ValueError, match='总解压'):
        inspect_archive('pax-bomb.tgz', raw)


def test_helper_source_limit_applies_before_ast_parsing_but_resources_keep_file_limit(monkeypatch):
    from shiori import plugin_packages
    parse = plugin_packages.ast.parse
    def bounded_parse(source, *args, **kwargs):
        assert len(source) <= 10000, 'oversized helper reached AST parser'
        return parse(source, *args, **kwargs)
    monkeypatch.setattr(plugin_packages.ast, 'parse', bounded_parse)
    members = files(); members['helper.py'] = b'#' * 10001
    candidate = inspect_archive('large-helper.zip', packed(members))[0]
    assert 'helper.py' in candidate['error'] and '10000' in candidate['error']
    assert '拆分' in candidate['error']
    members = files(); members['assets/data.bin'] = b'x' * 20000
    assert 'error' not in inspect_archive('resource.zip', packed(members))[0]


@pytest.mark.anyio
async def test_nested_entry_imports_sibling_and_keeps_output_in_task_workspace(automatic):
    service = PluginService(automatic.service.data_dir, automatic.service)
    description = manifest(); description['entrypoint'] = 'src/main.py'
    members = {
        'plugin.json': json.dumps(description).encode(),
        'src/main.py': b'from helper import render\ndef run(input):\n    return render(input["value"])\n',
        'src/helper.py': (b'from pathlib import Path\ndef render(value):\n'
                          b'    asset = (Path(__file__).parent / "assets/value.txt").read_text("utf-8")\n'
                          b'    Path("result.txt").write_text(str(value), encoding="utf-8")\n'
                          b'    return {"value": value, "asset": asset, "cwd": Path.cwd().name}\n'),
        'src/assets/value.txt': b'package-resource',
    }
    item = install(service, members); service.activate(item['id'], True)
    identity = automatic.access.identity('local', 'shiori')
    job = service.submit(item['id'], {'value': 9}, 'web:default', 'shiori', identity)
    await wait_for(lambda: automatic.runs.get(job['id'])['status'] in TERMINAL)
    result = automatic.runs.get(job['id'])
    assert result['status'] == 'completed', result
    assert json.loads(result['result']['reply']) == {'value': 9, 'asset': 'package-resource', 'cwd': item['id']}
    assert (automatic.root / 'plugins' / item['id'] / 'result.txt').read_text('utf-8') == '9'
    await automatic.runs.close()


def test_candidates_invalid_manifest_and_stale_preview(automatic, monkeypatch):
    service = PluginService(automatic.service.data_dir, automatic.service)
    valid = files(); invalid = files(); invalid['main.py'] = b'def run(:'
    members = {**{'one/' + key: raw for key, raw in valid.items()}, **{'two/' + key: raw for key, raw in invalid.items()}}
    scan = service.scan('many.zip', packed(members, wrapper=''))
    assert len(scan['candidates']) == 2
    bad = next(c for c in scan['candidates'] if c.get('error'))
    assert 'main.py' in bad['error'] and '1' in bad['error']
    with pytest.raises(ValueError, match='通过校验'): service.install_scan(scan['scan_id'], bad['candidate_id'])
    service._scans[scan['scan_id']]['at'] -= 601
    with pytest.raises(ValueError, match='过期'): service.install_scan(scan['scan_id'], scan['candidates'][0]['candidate_id'])


def test_plugin_package_api_requires_auth_and_installs_only_as_draft(tmp_path, monkeypatch):
    # Keep HTTP authentication real, replace only OS encryption in this transport test.
    if os.name == 'nt':
        monkeypatch.setattr('shiori.web_auth._protect', lambda value: 'dpapi:' + base64.b64encode(value.encode()).decode())
        monkeypatch.setattr('shiori.web_auth._unprotect', lambda value: base64.b64decode(value.split(':', 1)[1]).decode())
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        raw = packed(files())
        assert client.post('/api/plugins/import', files={'file': ('plugin.zip', raw)}).status_code == 401
        password_login(client, app)
        result = client.post('/api/plugins/import', files={'file': ('plugin.zip', raw)})
        assert result.status_code == 200, result.text
        scan = result.json()
        payload = {'scan_id': scan['scan_id'], 'candidate_id': scan['candidates'][0]['candidate_id']}
        item = client.post('/api/plugins/import/install', json=payload)
        assert item.status_code == 200, item.text
        assert not item.json()['enabled'] and app.state.runs.list() == []
        export = client.get('/api/plugins/packaged_example/export')
        assert export.status_code == 200 and export.headers['content-type'] == 'application/zip'
        assert len(inspect_archive('export.zip', export.content)) == 1
        assert client.post('/api/plugins/import/install', json=payload).status_code == 400
