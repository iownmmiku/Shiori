"""Real loopback TCP HTTP/WebSocket faults; no QQ login or billed provider."""
import asyncio
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from shiori.api import create_app
from shiori.db import Store
from shiori.provider import Provider, ProviderError
from shiori.settings import Settings


@contextmanager
def model_server(responses):
    queue=deque(responses); hits=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            hits.append(self.path)
            code,body,delay,disconnect=queue.popleft()
            time.sleep(delay)
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)+(100 if disconnect else 0)))
            self.end_headers()
            try: self.wfile.write(body)
            except OSError: pass
            self.close_connection=True
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1',hits
    finally: server.shutdown();server.server_close();thread.join(2)


def provider_at(tmp_path,url):
    settings=Settings(tmp_path)
    settings.update({'model':{'base_url':url,'model':'fixture-network'}})
    store=Store(tmp_path)
    return Provider(settings,store),store


def test_real_http_rate_limit_retries_once_then_succeeds(tmp_path):
    body=json.dumps({'choices':[{'message':{'role':'assistant','content':'network-ok'}}],
                     'usage':{'prompt_tokens':10,'completion_tokens':5}}).encode()
    with model_server([(429,b'{}',0,False),(200,body,0,False)]) as (url,hits):
        provider,store=provider_at(tmp_path,url)
        result=asyncio.run(provider.complete([{'role':'user','content':'ping'}]))
        assert result['content']=='network-ok' and len(hits)==2
        assert not provider._reservations
        store.close()


@pytest.mark.parametrize('body,disconnect',[(b'<html>bad gateway</html>',False),
    (b'data: {"choices":[]}\n\n',False),(b'{',True)])
def test_real_http_bad_format_sse_and_disconnect_do_not_replay(tmp_path,body,disconnect):
    with model_server([(200,body,0,disconnect)]) as (url,hits):
        provider,store=provider_at(tmp_path,url)
        with pytest.raises(ProviderError):asyncio.run(provider.complete([{'role':'user','content':'ping'}]))
        assert len(hits)==1 and not provider._reservations
        store.close()


def test_real_http_stall_is_bounded_and_does_not_repeat_dispatched_request(tmp_path,monkeypatch):
    original=httpx.AsyncClient
    def fast_timeout(*args,**kwargs):
        kwargs['timeout']=httpx.Timeout(.15,connect=.15)
        return original(*args,**kwargs)
    monkeypatch.setattr('shiori.provider.httpx.AsyncClient',fast_timeout)
    with model_server([(200,b'{}',.5,False)]) as (url,hits):
        provider,store=provider_at(tmp_path,url)
        with pytest.raises(ProviderError,match='超时'):asyncio.run(provider.complete([{'role':'user','content':'ping'}]))
        assert len(hits)==1 and not provider._reservations
        store.close()


def test_real_websocket_auth_bad_frame_disconnect_and_reconnect(tmp_path,monkeypatch):
    from shiori.qq_pairing import LocalQQPairing
    async def no_external_ports(self):return {'candidates':[]}
    monkeypatch.setattr(LocalQQPairing,'discover',no_external_ports)
    app=create_app(tmp_path,start_workers=False)
    token='fixture-loopback-onebot-token'
    app.state.settings.update({'onebot':{'enabled':True,'access_token':token}})
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    for _ in range(200):
        if server.started:break
        time.sleep(.01)
    assert server.started
    async def scenario():
        uri=f'ws://127.0.0.1:{port}/onebot/v11/ws'
        with pytest.raises(InvalidStatus):
            async with connect(uri,additional_headers={'Authorization':'Bearer invalid'}):pass
        for _ in range(2):
            async with connect(uri,additional_headers={'Authorization':'Bearer '+token}) as client:
                await client.send('not-json')
                await client.send(json.dumps({'post_type':'meta_event','meta_event_type':'heartbeat','self_id':9000}))
                headers={'Authorization':'Bearer '+app.state.settings.token}
                request=asyncio.create_task(asyncio.to_thread(httpx.post,f'http://127.0.0.1:{port}/api/qq/self-test',headers=headers,json={},timeout=6,trust_env=False))
                action=json.loads(await asyncio.wait_for(client.recv(),3))
                assert action['action']=='get_status'
                await client.send(json.dumps({'echo':action['echo'],'status':'ok','retcode':0,'data':{'online':True}}))
                response=await request
                assert response.status_code==200,response.text
                assert next(c for c in response.json()['checks'] if c['id']=='echo')['status']=='passed'
            for _ in range(100):
                if not app.state.onebot.status()['connected']:break
                await asyncio.sleep(.01)
            assert not app.state.onebot.status()['connected']
        assert not app.state.onebot._pending
    try:asyncio.run(scenario())
    finally:
        server.should_exit=True;thread.join(8)
        assert not thread.is_alive()
