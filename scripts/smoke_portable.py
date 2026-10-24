"""Exercise a built Windows application without importing its source packages."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from smoke_contract import effective_contract_version


def smoke_frozen_worker(exe, data):
    """Exercise the frozen interpreter dispatch before starting any bot service."""
    folder = data.parent / (data.name + '-computer-worker')
    if folder.exists():
        raise RuntimeError('Use a fresh worker QA directory')
    folder.mkdir(parents=True)
    script, output = folder / '已批准的 脚本.py', folder / 'python-output.txt'
    script.write_text("from pathlib import Path\nimport sys\nprint('frozen-worker-stdout-39')\n"
                      "print('frozen-worker-stderr-39', file=sys.stderr)\n"
                      "Path('worker-created.txt').write_text('本机子进程验收', encoding='utf-8')\n", 'utf-8')
    unexpected_data = folder / 'must-not-start-server'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    command = [str(exe), '--computer-python', str(script), '--computer-output', str(output),
               '--data-dir', str(unexpected_data), '--no-browser', '--no-tray']
    success = subprocess.run(command, cwd=folder, capture_output=True, timeout=30, creationflags=flags)
    assert success.returncode == 0, 'Frozen Python child did not exit successfully'
    assert 'frozen-worker-stdout-39' in output.read_text('utf-8')
    assert 'frozen-worker-stderr-39' in output.read_text('utf-8')
    assert (folder / 'worker-created.txt').read_text('utf-8') == '本机子进程验收'
    assert not unexpected_data.exists() and not data.exists(), 'Python child started a bot server'
    script.write_text("raise RuntimeError('expected-worker-failure-39')\n", 'utf-8')
    failure = subprocess.run(command, cwd=folder, capture_output=True, timeout=30, creationflags=flags)
    assert failure.returncode == 1, 'Frozen Python child did not propagate failure'
    assert 'expected-worker-failure-39' in output.read_text('utf-8')
    assert not unexpected_data.exists() and not data.exists()


@contextmanager
def local_model_fixture():
    """An offline scripted model, used only by this smoke-test process."""
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *values):
            pass
        def send(self, value):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            self.send({'data': [{'id': 'portable-v8-fixture'}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            messages = body.get('messages', [])
            names = {item['function']['name'] for item in body.get('tools', [])}
            tool_messages = [item for item in messages if item.get('role') == 'tool']
            message = {'role': 'assistant', 'content': '这是本地脚本模型的验收回复，不代表真实模型质量。'}
            if {'write_file', 'execute_python', 'execute_shell'} <= names:
                step = len(tool_messages)
                if step < 3:
                    function = (
                        {'name': 'write_file', 'arguments': {'path': 'approved-write.txt', 'content': '已批准的本机文件写入39'}},
                        {'name': 'execute_python', 'arguments': {'code': "from pathlib import Path\nprint('computer-python-output-39')\nPath('python-created.txt').write_text('已批准的Python结果39', encoding='utf-8')"}},
                        {'name': 'execute_shell', 'arguments': {'command': "[Console]::Write('命令中文验收39');[Console]::Write('汉'*1500)"}},
                    )[step]
                    message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': f'computer-{step}', 'type': 'function',
                        'function': {'name': function['name'], 'arguments': json.dumps(function['arguments'], ensure_ascii=False)}}]}
                else:
                    message['content'] = '已根据实际工具结果完成本地验收：写入文件、运行 Python 并执行中文输出命令。'
            elif any('人格蒸馏分析器' in str(item.get('content', '')) for item in messages):
                message['content'] = '验收人格：根据选中的甲的语料，表达简短、愿意先理解问题再给出建议。观察来自文件f1；其余生平、关系及内心未知。这是脚本模型草稿，需人工审核。'
            requests.append({'tools': sorted(names), 'tool_results': len(tool_messages), 'response': message})
            self.send({'choices': [{'message': message}], 'usage': {'prompt_tokens': 50, 'completion_tokens': 30}})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def smoke_experience(request, upload, data, exe):
    resource = exe.parent / '_internal/mikubot/resources/csp'
    for relative in ('SKILL.md', 'LICENSE', 'references/persona-template.md', 'references/distillation-framework.md',
                     'references/character-mode.md', 'references/corpus-mode.md'):
        assert (resource / relative).is_file(), 'Missing frozen CSP resource: ' + relative
    assert b'MikubotExperience' in request('/static/experience.js')
    assert b'experience.css' in request('/')
    assert '管理员' in request('/static/guide.md').decode('utf-8')
    admins = request('/api/admins')
    assert len(admins) == 1 and admins[0]['platform'] == 'local' and admins[0]['account_id'] == 'mikubot'
    for account in ('24242424', '25252525'):
        request('/api/admins', {'platform': 'onebot', 'account_id': account, 'instance_id': '39393939', 'label': '离线验收管理员'})
    assert len(request('/api/admins')) == 3
    removed = next(item for item in request('/api/admins') if item['account_id'] == '25252525')
    request('/api/admins/' + removed['id'], method='DELETE')
    assert len(request('/api/admins')) == 2
    assert '25252525' in request('/api/settings')['onebot']['allowed_users'], 'Chat access incorrectly coupled to administrator revocation'
    pair = request('/api/admins/pairing', {})
    assert pair['code'] and request('/api/admins/pairing/' + pair['id'])['status'] == 'waiting'
    request('/api/admins/pairing/' + pair['id'], method='DELETE')
    scope = request('/api/scopes', {'name': '便携端新对话', 'kind': 'web', 'private_reply_policy': 'always'})
    assert scope['id'].startswith('web:') and scope['always_reply'] and not scope['plan_enabled']
    request('/api/scopes/' + scope['id'], {'auto_memory': False, 'allow_tools': True, 'allow_mcp': False}, 'PUT')
    memories_before = len(request('/api/memories'))
    attachment = upload('reference.txt', '本次参考验收39'.encode('utf-8'), '/api/chat/attachments', {'scope_id': scope['id'], 'purpose': 'context'})
    assert attachment['id'] and '本次参考验收39' in attachment['preview']
    assert len(request('/api/memories')) == memories_before, 'Temporary reference became a long-term memory'
    corpus = io.BytesIO()
    with zipfile.ZipFile(corpus, 'w') as archive:
        archive.writestr('chat.jsonl', json.dumps({'speaker': '甲', 'text': '我喜欢先理解问题再给建议。'}, ensure_ascii=False) + '\n')
        archive.writestr('skipped.bin', b'not a readable document')
    inventory = upload('corpus.zip', corpus.getvalue(), '/api/persona/distill/corpus')
    assert inventory['readable_files'] == 1 and any(item['status'] == 'skipped' for item in inventory['files'])
    assert any(item['id'] == '甲' for item in inventory['speakers'])
    assert len(request('/api/memories')) == memories_before

    def wait_job(job_id, status, approval=None):
        for _ in range(160):
            current = request('/api/jobs/' + job_id)
            if current['status'] == status and (approval is None or (current.get('approval') or {}).get('details', {}).get('tool') == approval):
                return current
            if current['status'] in ('failed', 'cancelled', 'interrupted'):
                raise AssertionError('Background QA job failed: ' + json.dumps(current, ensure_ascii=False))
            time.sleep(.15)
        raise AssertionError('Background QA job did not reach ' + status)

    workspace = data / 'qa-computer-workspace'
    with local_model_fixture() as (model_url, protocol):
        models = request('/api/models', {'base_url': model_url, 'api_key': 'qa-v8-fixture-key'}, 'POST')
        assert 'portable-v8-fixture' in str(models), 'Model list did not use current form'
        request('/api/settings', {'model': {'base_url': model_url, 'model': 'portable-v8-fixture', 'api_key': 'qa-v8-fixture-key'},
            'routing': {'chat': '', 'fallback': []}, 'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(workspace), 'approval_mode':'per_action'}}, 'PUT')
        job = request('/api/computer/jobs', {'scope_id': scope['id'], 'text': '在本机验收目录写入文件，然后执行 Python 生成结果，最后执行 Shell 命令打印中文。', 'attachments': [attachment['id']]})
        first = wait_job(job['id'], 'awaiting_approval', 'write_file')
        assert not (workspace / 'approved-write.txt').exists(), 'Write executed before approval'
        approval_id = first['approval']['id']
        request('/api/jobs/' + job['id'] + '/approve', {'approval_id': approval_id})
        second = wait_job(job['id'], 'awaiting_approval', 'execute_python')
        assert (workspace / 'approved-write.txt').read_text('utf-8') == '已批准的本机文件写入39'
        assert not (workspace / 'python-created.txt').exists(), 'Python executed before approval'
        try:
            request('/api/jobs/' + job['id'] + '/approve', {'approval_id': approval_id})
            raise AssertionError('Old approval approved the next computer action')
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        request('/api/jobs/' + job['id'] + '/approve', {'approval_id': second['approval']['id']})
        third = wait_job(job['id'], 'awaiting_approval', 'execute_shell')
        assert not any(item['type'] == 'tool_result' and item['data'].get('tool') == 'execute_shell'
                       for item in third['events']), 'Shell executed before approval'
        try:
            request('/api/jobs/' + job['id'] + '/approve', {'approval_id': second['approval']['id']})
            raise AssertionError('Old Python approval approved the Shell action')
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        request('/api/jobs/' + job['id'] + '/approve', {'approval_id': third['approval']['id']})
        done = wait_job(job['id'], 'completed')
        assert (workspace / 'python-created.txt').read_text('utf-8') == '已批准的Python结果39'
        assert 'computer-python-output-39' in json.dumps(done['events'])
        shell_results = [item['data']['result'] for item in done['events']
                         if item['type'] == 'tool_result' and item['data'].get('tool') == 'execute_shell']
        assert len(shell_results) == 1 and shell_results[0]['ok'] and shell_results[0]['exit_code'] == 0
        assert shell_results[0]['output'] == '命令中文验收39' + '汉' * 1500, 'Shell Chinese output was corrupted across the 4096-byte read boundary'
        files = done['result']['artifacts']
        assert any(item['name'] == 'python-created.txt' for item in files)
        artifact = next(item for item in files if item['name'] == 'python-created.txt')
        assert request(artifact['url']).decode('utf-8') == '已批准的Python结果39'
        distill = request('/api/persona/distill/start', {'scope_id': scope['id'], 'mode': 'corpus', 'upload_id': inventory['upload_id'],
            'selected_files': [item['id'] for item in inventory['files'] if item['status'] == 'readable'],
            'target_speaker': '甲', 'name': '便携验收人格', 'confirmed': True, 'max_calls': 6, 'max_total_tokens': 20000})
        draft = wait_job(distill['id'], 'completed')
        assert draft['result']['artifact'] is None and draft['result']['mode'] == 'corpus'
        assert draft['result']['prompt'] and draft['result']['quality']['human_review_required']
        assert request('/api/persona/distill/' + distill['id'] + '/trial', {'text': '你好'})['reply']
        copy = request('/api/persona/distill/' + distill['id'] + '/apply', {})
        assert copy['id'] != 'default' and len(request('/api/personas')) == 2
        assert any('execute_python' in item['tools'] for item in protocol)
        (data / 'qa-model-protocol.json').write_text(json.dumps(protocol, ensure_ascii=False, indent=2), 'utf-8')
    request('/api/chat/attachments/' + attachment['id'] + '?' + urllib.parse.urlencode({'scope_id': scope['id']}), method='DELETE')
    assert not (data / 'chat_attachments' / (attachment['id'] + '.json')).exists()
    return {'computer': job['id'], 'distillation': distill['id']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18767)
    parser.add_argument('--expect-version', '--expect', dest='expect_version', default='0.1.6')
    parser.add_argument('--mcp-fixture-url', default='')
    args = parser.parse_args()
    data = args.data_dir.resolve()
    if data.exists():
        raise RuntimeError('Use a fresh, disposable smoke-test data directory')
    version = effective_contract_version(args.expect_version)
    exe = args.exe.resolve()
    if version >= (0, 8):
        smoke_frozen_worker(exe, data)
    url = f'http://127.0.0.1:{args.port}'
    headers = {}
    def request(path, payload=None, method=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(url + path, body, headers={**headers, 'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
    def upload(name, content, path='/api/files', fields=None):
        boundary = 'qunyu-portable-smoke-boundary'
        prefix = b''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode('utf-8') for key, value in (fields or {}).items())
        body = prefix + (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                + content + f'\r\n--{boundary}--\r\n'.encode())
        req = urllib.request.Request(url + path, body, headers={**headers, 'Content-Type': f'multipart/form-data; boundary={boundary}'}, method='POST')
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    def restore(content):
        boundary='mikubot-smoke-restore-boundary'
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="smoke.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode()
              +content+f'\r\n--{boundary}--\r\n'.encode())
        req=urllib.request.Request(url+'/api/backup/restore',body,headers={**headers,'Content-Type':f'multipart/form-data; boundary={boundary}'},method='POST')
        with urllib.request.urlopen(req,timeout=20) as response: return json.load(response)
    command = [str(exe), '--data-dir', str(data), '--port', str(args.port), '--no-browser', '--no-tray']
    process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(120):
            try:
                assert request('/healthz')['version'] == args.expect_version
                break
            except (OSError, AssertionError):
                if process.poll() is not None:
                    raise RuntimeError('Portable application exited before health check')
                time.sleep(.25)
        else:
            raise RuntimeError('Portable application did not become ready')
        assert b'mikubot' in request('/static/app.js').lower()
        try:
            request('/api/bootstrap')
            raise AssertionError('Unauthenticated request was accepted')
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        headers['Authorization'] = 'Bearer ' + (data / 'admin.token').read_text('utf-8').strip()
        boot = request('/api/bootstrap')
        assert boot['setup_required'] and boot['stats']['messages'] == 0
        if version >= (0, 5):
            assert boot['name'] == 'mikubot' and boot['personas'][0]['name'] == '初音未来' and len(boot['personas']) == 1
            assert b'personaKnowledge' in request('/static/workspace-features.js')
        fixture_key = 'qa-disposable-model-key'
        result = request('/api/settings', {'model': {'api_key': fixture_key}}, 'PUT')
        assert fixture_key not in json.dumps(result)
        assert fixture_key not in (data / 'settings.json').read_text('utf-8')
        assert '已记住' in request('/api/chat', {'text': '记住：这是便携包验收笔记'})['reply']
        assert '已确认' in request('/api/chat', {'text': '我们群里开饭指一起打游戏'})['reply']
        assert '已添加待办' in request('/api/chat', {'text': '添加待办：检查发布包'})['reply']
        backup = zipfile.ZipFile(io.BytesIO(request('/api/backup')))
        assert fixture_key.encode() not in backup.read('settings.json')
        assert 'qunyu.db' in backup.namelist()
        duplicate = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        assert duplicate.wait(timeout=10) == 0
        assert len(request('/api/memories')) == len(request('/api/jargon')) == len(request('/api/tasks')) == 1
        request('/api/shutdown', {}, 'POST')
        assert process.wait(timeout=15) == 0
        assert not (data / 'server.json').exists()
        process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(80):
            try:
                if request('/healthz')['version'] == args.expect_version:
                    break
            except OSError:
                pass
            time.sleep(.25)
        assert len(request('/api/memories')) == 1
        assert request('/api/settings')['model']['api_key_set']
        if version >= (0, 2):
            request('/api/chat', {'text': '记住：我住杭州'})
            try:
                request('/api/chat', {'text': '我现在住苏州'})
            except urllib.error.HTTPError as exc:
                assert exc.code == 400  # Raw source persists; no model is configured.
            source = next(x for x in request('/api/messages') if x['role'] == 'user' and x['content'] == '我现在住苏州')
            candidate = request('/api/memories', {'content': '我现在住苏州', 'property': 'residence_city', 'subject': 'owner', 'status': 'pending', 'source_ids': [source['id']]})
            assert request('/api/memories/' + candidate['id'] + '/conflicts')
            approved = request('/api/memories/' + candidate['id'] + '/approve', {'replace_conflicts': True})
            assert approved['status'] == 'active'
            result = request('/api/memory/search?q=' + urllib.parse.quote('现在住哪里'))
            assert '苏州' in json.dumps(result, ensure_ascii=False) and '杭州' not in json.dumps(result, ensure_ascii=False)
            docx = io.BytesIO()
            with zipfile.ZipFile(docx, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Portable knowledge uses real source references.</w:t></w:r></w:p></w:body></w:document>')
            assert upload('portable.docx', docx.getvalue())['format'] == 'docx'
            from pypdf import PdfWriter
            from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
            writer = PdfWriter()
            page = writer.add_blank_page(width=300, height=200)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 10 100 Td (Portable PDF source verification) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
            pdf = io.BytesIO()
            writer.write(pdf)
            assert upload('portable.pdf', pdf.getvalue())['pages'] == 1
            result = request('/api/memory/search?q=' + urllib.parse.quote('Portable PDF source'))
            assert 'PDF source verification' in json.dumps(result)
        if version >= (0, 3):
            assert request('/api/diagnostics')['checks']
            assert request('/api/context/status')['covered_count'] == 0
            expression = request('/api/jargon', {'entry_type': 'expression', 'term': '先让我捋一捋', 'meaning': '整理讨论时暂缓回答'})
            assert expression['status'] == 'pending' and not expression['use_allowed']
            assert len(request('/api/expressions')) == 1
            assert not any(x.get('entry_type') == 'expression' for x in request('/api/jargon'))
            request('/api/settings', {'providers': [{'id': 'portable', 'name': '便携验收', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'test-only', 'api_key': 'qa-provider-key'}], 'routing': {'chat': 'portable'}}, 'PUT')
            assert not request('/api/bootstrap')['setup_required']
            assert request('/api/providers/status')['routes']['chat']['provider_id'] == 'portable'
            assert 'qa-provider-key' not in (data / 'settings.json').read_text('utf-8')
            archive = zipfile.ZipFile(io.BytesIO(request('/api/backup')))
            assert b'qa-provider-key' not in archive.read('settings.json')
        if version >= (0, 4):
            assert request('/api/mcp/status')['servers'] == []
            assert request('/api/tool-runs') == []
            result=request('/api/settings',{'mcp_servers':[{'id':'smoke','name':'本地测试','url':'http://127.0.0.1:1/mcp','token':'qa-disposable-mcp-token','enabled':False,'allowed_tools':[]}]},'PUT')
            assert result['mcp_servers'][0]['token_set'] and 'token' not in result['mcp_servers'][0]
            assert 'qa-disposable-mcp-token' not in (data/'settings.json').read_text('utf-8')
            assert b'qa-disposable-mcp-token' not in zipfile.ZipFile(io.BytesIO(request('/api/backup'))).read('settings.json')
            if args.mcp_fixture_url:
                fixture=urllib.parse.urlsplit(args.mcp_fixture_url)
                assert fixture.scheme=='http' and fixture.hostname=='127.0.0.1' and not fixture.query and not fixture.fragment
                request('/api/settings',{'model':{'base_url':args.mcp_fixture_url+'/v1','model':'local-fixture'},'routing':{'chat':''},
                        'mcp_servers':[{'id':'smoke','name':'本地协议验收','url':args.mcp_fixture_url+'/mcp','enabled':True,'allowed_tools':['append_note']}]},'PUT')
                request('/api/scopes/web:default',{'allow_mcp':True,'auto_memory':False},'PUT')
                assert len(request('/api/mcp/discover',{'server_id':'smoke'})['tools'])==1
                proposal=request('/api/chat',{'text':'请使用便笺工具追加一条验收记录'})
                assert proposal['pending_actions'] and '尚未执行' in proposal['reply']
                run_id=proposal['pending_actions'][0]['run_id']
                assert request('/api/tool-runs')[0]['status']=='pending'
                assert request('/api/tool-runs/'+run_id+'/approve',{})['status']=='succeeded'
                try:
                    request('/api/tool-runs/'+run_id+'/approve',{})
                    raise AssertionError('Repeated approval was accepted')
                except urllib.error.HTTPError as exc:
                    assert exc.code==400
        if version >= (0, 5):
            request('/api/settings', {'identity': {'aliases':['初音','miku','39'], 'planner_enabled':False},
                                     'output':{'target_chars':120,'max_chars':280}, 'exploration':{'daily_searches':0}}, 'PUT')
            scope = request('/api/scopes/web:default', {'plan_enabled':None,'always_reply':True,'bot_name':'','bot_aliases':[]}, 'PUT')
            assert scope['plan_enabled'] is None and scope['always_reply']
            assert '已记住' in request('/api/chat', {'text':'39，记住：便携包名称触发已验收'})['reply']
            for scope_id in ('web:group:alpha','web:group:beta'):
                request('/api/scopes', {'id':scope_id, 'memory_space':'portable-shared'})
            fact = request('/api/memories', {'scope_id':'web:group:alpha','user_id':'owner','visibility':'group',
                                           'content':'Portable shared fact 39','shareable':True})
            result = request('/api/memory/search?' + urllib.parse.urlencode({'scope_id':'web:group:beta','q':'Portable shared fact 39'}))
            assert fact['id'] in str(result['items'])
            request('/api/scopes/web:group:beta', {'memory_space':''}, 'PUT')
            result = request('/api/memory/search?' + urllib.parse.urlencode({'scope_id':'web:group:beta','q':'Portable shared fact 39'}))
            assert fact['id'] not in str(result['items'])
            explored = request('/api/knowledge/explore', {'persona_id':'default'})
            assert explored['created'] == 0 and explored['skipped']
            assert request('/api/knowledge/status')['searches_today'] == 0
            assert 'persona_knowledge.json' in zipfile.ZipFile(io.BytesIO(request('/api/backup'))).namelist()
            assert request('/api/settings')['output']['target_chars'] == 120
        experience_jobs = smoke_experience(request, upload, data, exe) if version >= (0, 8) else {}
        if version >= (0, 6):
            assert len(request('/api/presets'))==3
            preset=request('/api/scopes/preset',{'scope_id':'web:default','preset_id':'quiet_assistant'})
            assert not preset['proactive_reply'] and not preset['use_persona_knowledge']
            request('/api/settings',{'planner':{'daily_calls':25,'daily_tokens':5000},'output':{'platform_max_chars':1000}},'PUT')
            request('/api/scopes/web:default',{'planner_daily_calls':8,'output':{'platform_max_chars':500}},'PUT')
            assert request('/api/settings')['planner']['daily_calls']==25
            rules=request('/api/knowledge/sources',{'persona_id':'default','rules':{'allowed_domains':['example.org'],'blocked_domains':['blocked.example'],'use_bing':False}},'PUT')
            assert 'example.org' in rules['allowed_domains']
            first=request('/api/memories',{'content':'关系节点：音乐节','kind':'event'})
            second=request('/api/memories',{'content':'关系节点：演出计划','kind':'note'})
            relation=request('/api/memory/relations',{'from_id':first['id'],'to_id':second['id'],'relation_type':'event_link','label':'同一演出安排'})
            assert relation['status']=='pending'
            approved=request('/api/memory/relations/'+relation['id']+'/review',{'action':'approve'})
            assert approved['status']=='active'
            assert request('/api/memory/relations')
            assert request('/api/memory/organization',{})['message']
            history_before=request('/api/tool-runs')
            complete_backup=request('/api/backup')
            archive=zipfile.ZipFile(io.BytesIO(complete_backup))
            assert 'tool_history.json' in archive.namelist()
            if version >= (0, 8):
                assert 'jobs_history.json' in archive.namelist()
                assert 'jobs_history_read_only' in json.loads(archive.read('manifest.json'))['includes']
                history = json.loads(archive.read('jobs_history.json'))
                assert set(experience_jobs.values()) <= {item['id'] for item in history['jobs']}
                assert all('identity' not in item and '_payload' not in item and item['approval'] is None for item in history['jobs'])
            assert restore(complete_backup)['ok']
            assert request('/api/settings')['paused']
            after=request('/api/tool-runs')
            assert len(after)==len(history_before) and all(x['read_only'] for x in after)
            # Runtime relation visibility respects restore's paused state;
            # the authenticated archive view still proves the record survived.
            assert next(x for x in request('/api/memories') if x['id']==relation['id'])['status']=='active'
            if version >= (0, 8):
                assert not request('/api/settings')['computer']['enabled']
                histories = [item for item in request('/api/jobs') if item.get('read_only')]
                assert set(experience_jobs.values()) <= {item['original_id'] for item in histories}
                for item in histories:
                    assert item['approval'] is None and not (item.get('result') or {}).get('artifact')
                    assert not (item.get('result') or {}).get('artifacts')
                    assert not (item.get('result') or {}).get('workspace')
                    try:
                        request('/api/jobs/' + item['id'] + '/approve', {'approval_id': 'old-approval'})
                        raise AssertionError('Restored job history was executable')
                    except urllib.error.HTTPError as exc:
                        assert exc.code in (400, 403)
        request('/api/shutdown', {}, 'POST')
        assert process.wait(timeout=15) == 0
        if version >= (0, 6):
            replay_output=data/'portable-replay.json'
            replay=subprocess.Popen([str(args.exe.resolve()),'--replay',str(args.exe.resolve().parent/'evals/routing-synthetic.json'),
                '--replay-output',str(replay_output)],creationflags=subprocess.CREATE_NO_WINDOW)
            assert replay.wait(timeout=25)==0
            report=json.loads(replay_output.read_text('utf-8'))
            assert report['mode']=='scripted_routing_regression' and report['summary']['errors']==0 and report['summary']['decision_accuracy']==1
        checks = 'standalone mikubot EXE, auth, DPAPI, restart, scoped memory, tools, MCP once-only approval, knowledge, output, replay'
        if version >= (0, 8):
            checks += ', frozen Python worker, administrators, reference attachments, computer approvals, CSP corpus persona, read-only job restore'
        (data / 'portable-smoke.json').write_text(json.dumps({'version': args.expect_version, 'passed': True,
            'checks': checks, 'experience_jobs': experience_jobs}, ensure_ascii=False, indent=2), 'utf-8')
        print('PASS: ' + checks)
    finally:
        if process.poll() is None:
            try:
                request('/api/shutdown', {}, 'POST')
                process.wait(timeout=10)
            except Exception:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
