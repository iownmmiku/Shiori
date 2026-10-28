import asyncio

import pytest

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.output import split_output, strip_memory_citations
from shiori.settings import Settings


KEY = '7ab675d0-1111-2222-3333-444444444444'


@pytest.mark.parametrize('marker', ['[7ab675d0]', '[记忆ID：7ab675d0]', '【来源：7ab675d0】',
                                      '[7AB675D0]', '[' + KEY + ']', '[7ab675d0, ' + KEY + ']'])
def test_known_internal_citations_removed_before_pacing(marker):
    text='当然记得你'+marker+'，你喜欢音乐。'
    clean=strip_memory_citations(text,[KEY])
    assert clean=='当然记得你，你喜欢音乐。'
    assert ''.join(split_output(clean,{'output':{'target_chars':20,'max_chars':40}}))==clean


@pytest.mark.parametrize('text', ['编号是[deadbeef]。', '数值[12345678]。',
    '`[7ab675d0]`', '```python\nvalue = "[7ab675d0]"\n```',
    '[7ab675d0](https://example.com)', 'https://example.com/?value=[7ab675d0]'])
def test_unrelated_ids_code_and_links_are_preserved(text):
    assert strip_memory_citations(text,[KEY])==text


def test_literal_user_example_is_preserved():
    text='你输入的是[7ab675d0]。'
    assert strip_memory_citations(text,[KEY],user_text='解释[7ab675d0]')==text
    assert strip_memory_citations(text,[])==text


def test_chat_cleans_body_but_keeps_evidence_and_old_history(tmp_path):
    store,settings=Store(tmp_path),Settings(tmp_path)
    memory=store.create_memory({'scope_id':'web:default','user_id':'owner','kind':'preference','content':'喜欢音乐'})
    marker='['+memory['id'][:8]+']'
    store.add_message('web:default','assistant','assistant','记得你'+marker+'。',
                      memory_ids=[memory['id']],memory_versions={memory['id']:1})
    class Model:
        async def complete(self,messages,**kwargs):
            assert '附[记忆ID前8位]' not in messages[0]['content']
            assert all(marker not in m.get('content','') for m in messages if m['role']=='assistant')
            return {'content':'当然记得你'+marker+'，你喜欢音乐。','tool_calls':[],
                    'usage':{'input_tokens':10,'output_tokens':10}}
    chat=ChatService(store,settings,Model())
    async def scenario():
        try:
            result=await chat.respond('web:default','owner','还记得我的喜好吗？')
            assert result['reply']=='当然记得你，你喜欢音乐。'
            saved=store.get_message(result['message_id'])
            assert saved['content']==result['reply']
            assert memory['id'] in saved['metadata']['memory_versions']
            assert memory['id'] in saved['metadata']['memory_ids']
        finally:
            await chat.close()
            store.close()
    asyncio.run(scenario())
