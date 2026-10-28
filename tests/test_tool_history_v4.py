import asyncio
from copy import deepcopy
from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import DEFAULTS

class Config:
    def __init__(self): self.data=deepcopy(DEFAULTS)
    def get(self): return deepcopy(self.data)

def test_receipt_query_is_scoped_owned_and_revocable(tmp_path):
    store=Store(tmp_path)
    chat=ChatService(store,Config(),object())
    source=store.add_message('web:default','owner','user','请处理便笺')
    run=chat.tool_actions.begin_local('calculate',{'expression':'1+1'},'web:default','owner',[source['id']])
    chat.tool_actions.finish_local(run,{'result':2})
    other=chat.tool_actions.begin_local('calculate',{},'web:default','other',[source['id']])
    chat.tool_actions.finish_local(other,{'result':'其他用户结果'})
    async def scenario():
        query=store.add_message('web:default','owner','user','刚才的工具结果如何')
        records=await chat._tool('tool_history',{},'web:default','owner',query['content'],query['id'],{})
        assert len(records)==1 and records[0]['result']['result']==2
        memory=store.create_memory({'scope_id':'web:default','user_id':'owner','content':'便笺资料','source_ids':[source['id']]})
        store.delete_memory(memory['id'])
        assert not await chat._tool('tool_history',{},'web:default','owner',query['content'],query['id'],{})
        await chat.close()
    asyncio.run(scenario())
    store.close()

def test_long_escaped_tool_result_is_valid_bounded_json():
    import json
    from shiori.tool_actions import bounded_result
    result=bounded_result({'text':'\\"'*15000},5000)
    assert len(json.dumps(result,ensure_ascii=False,sort_keys=True)) <= 5000
    assert result['truncated']
