import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
from types import SimpleNamespace

import pytest

from mikubot.db import Store
from mikubot.memory import MemoryEngine
from mikubot.provider import _PriorityGate
from mikubot.settings import Settings
from mikubot.runs import RunManager
from test_tool_actions_v4 import environment, propose


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.mark.anyio
async def test_docker_blocks_mcp_and_rechecks_pending_calls(environment):
    store,settings,chat,client=environment
    job,_,_=await propose(environment)
    settings.config['computer'].update(enabled=True,runtime='docker')
    assert not chat.tool_actions.available('qq:group:202','101')
    assert not chat.tool_actions.status()['external_tools_allowed']
    result=await chat.tool_actions.approve(job)
    assert result['status']=='cancelled' and client.call_count==0
    settings.config['computer']['allow_external_tools']=True
    assert chat.tool_actions.available('qq:group:202','101')
    settings.config['computer'].update(allow_external_tools=False,runtime='local')
    assert chat.tool_actions.available('qq:group:202','101')


def test_sandbox_external_permission_requires_real_boolean(tmp_path):
    settings=Settings(tmp_path)
    assert settings.get()['computer']['allow_external_tools'] is False
    with pytest.raises(ValueError):settings.update({'computer':{'allow_external_tools':'yes'}})


def test_tray_notification_is_nonblocking_and_has_no_messagebox(monkeypatch):
    from mikubot import notifications
    called=threading.Event();release=threading.Event()
    def notify(message,title):
        called.set();release.wait(2)
    monkeypatch.setattr(notifications,'os',SimpleNamespace(name='nt'))
    try:
        assert notifications.notify_info('fallback',tray=SimpleNamespace(notify=notify)) is True
        assert called.wait(1) and not release.is_set()
    finally:release.set()


def test_temporary_tray_notification_is_removed_without_a_dialog(monkeypatch):
    from mikubot import notifications
    stopped=threading.Event();seen=[]
    class Icon:
        HAS_NOTIFICATION=True
        def __init__(self,*args):self.visible=False
        def run(self,setup):setup(self)
        def notify(self,message,title):seen.append((message,title,self.visible))
        def stop(self):stopped.set()
    monkeypatch.setattr(notifications,'os',SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules,'pystray',SimpleNamespace(Icon=Icon))
    monkeypatch.setattr('mikubot.brand.tray_image',lambda:object())
    assert notifications.notify_info('browser mode',lifetime=0)
    assert stopped.wait(1)
    assert seen==[('browser mode','mikubot',True)]


@pytest.mark.anyio
async def test_model_queue_cancellation_does_not_retain_unbounded_tombstones():
    gate=_PriorityGate(capacity=1,queue_limit=2)
    await gate.acquire()
    for _ in range(100):
        queued=asyncio.create_task(gate.acquire());await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):await queued
    assert gate.queue==[] and gate.active==1
    gate.release();assert not gate.locked()


@pytest.mark.anyio
async def test_memory_scoring_yields_loop_and_revalidates_deleted_memory(tmp_path,monkeypatch):
    store=Store(tmp_path)
    item=store.create_memory({'scope_id':'web:default','user_id':'owner','kind':'fact','content':'喜欢天文'})
    engine=MemoryEngine(store,{'memory':{'semantic_enabled':False}})
    real=engine._rank_snapshot
    loop=asyncio.get_running_loop();loop_thread=threading.get_ident()
    entered=asyncio.Event();release=threading.Event();snapshots=[];worker_threads=[]
    def rank(*args):
        worker_threads.append(threading.get_ident())
        result=real(*args)
        snapshots.extend(doc.record['id'] for doc in result)
        loop.call_soon_threadsafe(entered.set)
        # This is a deadlock guard, not a cold-index or thread-start benchmark.
        if not release.wait(30):raise RuntimeError('test worker was not released')
        return result
    monkeypatch.setattr(engine,'_rank_snapshot',rank)
    task=asyncio.create_task(engine.inspect('web:default','owner','天文'))
    try:
        # Wait on the worker's explicit snapshot boundary without borrowing a
        # second default-executor worker or depending on CI startup speed.
        await asyncio.wait_for(entered.wait(),30)
        assert worker_threads and all(worker!=loop_thread for worker in worker_threads)
        assert item['id'] in snapshots
        assert not task.done()
        responsive=loop.create_future()
        loop.call_soon(responsive.set_result,True)
        assert await responsive
        assert not release.is_set() and not task.done()
        store.delete_memory(item['id'])
        release.set()
        assert not (await task)['items']
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True)
        engine.close();store.close()


@pytest.mark.anyio
async def test_memory_workers_stay_bounded_after_repeated_cancellation(tmp_path,monkeypatch):
    store=Store(tmp_path);engine=MemoryEngine(store,{})
    ready=threading.Event();release=threading.Event();lock=threading.Lock()
    started=0
    def rank(*args):
        nonlocal started
        with lock:
            started+=1
            if started==2:ready.set()
        if not release.wait(5):raise RuntimeError('test worker was not released')
        return []
    monkeypatch.setattr(engine,'_rank_snapshot',rank)
    tasks=[asyncio.create_task(engine._rank_async('web:default','owner',{},'test')) for _ in range(32)]
    try:
        assert await asyncio.to_thread(ready.wait,2)
        assert engine._pending_reads==32 and started==2
        with pytest.raises(ValueError,match='队列已满'):
            await engine._rank_async('web:default','owner',{},'overflow')
        tasks[0].cancel();await asyncio.sleep(0)
        tasks[0].cancel();await asyncio.sleep(0)
        assert not tasks[0].done() and started==2 and engine._pending_reads==32
        tasks[2].cancel()
        with pytest.raises(asyncio.CancelledError):await tasks[2]
        assert engine._pending_reads==31 and started==2
    finally:
        release.set();await asyncio.gather(*tasks,return_exceptions=True)
        assert engine._pending_reads==0
        engine.close();store.close()


def test_parallel_local_writes_keep_all_records_and_one_writer_guard(tmp_path):
    store=Store(tmp_path)
    def write(index):
        return store.create_memory({'scope_id':f'web:load-{index%8}','user_id':'owner','kind':'fact','content':f'本地并发记录{index}'})['id']
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:ids=list(workers.map(write,range(160)))
        assert len(set(ids))==160
        assert sum(len(store.list_memories(f'web:load-{i}')) for i in range(8))==160
    finally:store.close()


@pytest.mark.anyio
async def test_resume_double_click_reuses_active_task(tmp_path):
    runs=RunManager(tmp_path);entered=asyncio.Event();release=asyncio.Event()
    async def handler(ctx,payload):
        ctx.checkpoint(distillation_cache={'results':{'one':'done'}})
        if not ctx.job.get('resumed_from'):raise ValueError('interrupted')
        entered.set();await release.wait()
        return {'ok':True}
    runs.register('distillation',handler)
    original=runs.submit('distillation',{},identity={})
    await asyncio.gather(runs._tasks[original['id']])
    first=runs.resume(original['id']);second=runs.resume(original['id'])
    assert first['id']==second['id']
    try:
        await asyncio.wait_for(entered.wait(),1)
        assert len(runs._tasks)==1
    finally:release.set();await runs.close()
