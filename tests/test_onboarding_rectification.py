"""Local admission, inheritance, persona removal and delivery regressions."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.access import chat_allowed, manager_for
from shiori.api import create_app
from shiori.db import Store
from shiori.installer import INSTALL_RECORD, gui_install_target
from shiori.output import split_output, split_transport_output
from shiori.settings import Settings


def test_fresh_blacklist_and_old_whitelist_survive_restart(tmp_path):
    fresh = Settings(tmp_path / 'fresh')
    assert fresh.get()['onebot']['chat_policy'] == 'blacklist'
    assert chat_allowed(fresh.get(), 'onebot', 'private', '123')
    assert not chat_allowed(fresh.get(), 'unknown', 'private', '123')
    fresh.update({'onebot': {'enabled': True, 'access_token': 'isolated-fixture-token', 'blocked_users': ['123']}})
    assert not chat_allowed(Settings(tmp_path / 'fresh').get(), 'onebot', 'private', '123')
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    (legacy / 'settings.json').write_text(json.dumps({'onebot': {'allowed_groups': ['456']}}), 'utf-8')
    old = Settings(legacy)
    assert old.get()['onebot']['chat_policy'] == 'whitelist'
    assert chat_allowed(old.get(), 'onebot', 'group', '456')
    assert not chat_allowed(old.get(), 'onebot', 'group', '789')
    old.update({'paused': True})
    assert Settings(legacy).get()['onebot']['chat_policy'] == 'whitelist'


@pytest.mark.parametrize('platform,section', [('onebot', 'onebot'), ('qqofficial', 'qq_official')])
def test_blacklist_does_not_grant_admin_or_allow_blocked_sender(tmp_path, platform, section):
    settings = Settings(tmp_path)
    settings.update({section: {'blocked_users': ['123'], 'blocked_groups': ['456']}})
    assert not chat_allowed(settings.get(), platform, 'private', '123')
    assert not chat_allowed(settings.get(), platform, 'group', '456')
    assert chat_allowed(settings.get(), platform, 'private', '789')
    access = manager_for(settings)
    assert not access.is_admin(access.identity(platform, '789', '999'))


def test_default_persona_can_be_deleted_and_is_not_reseeded(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        assert client.delete('/api/personas/default').status_code == 200
        assert client.get('/api/personas').json() == []
        assert client.get('/api/scopes').json()[0]['persona_id'] == ''
        scope = client.post('/api/scopes', json={'name': '删除人格后的聊天'}).json()
        assert scope['persona_id'] == ''
        # A model-less local command still works with an empty persona library.
        result = client.post('/api/chat', json={'text': '记住：喜欢音乐', 'scope_id': scope['id']})
        assert result.status_code == 200, result.text
    again = create_app(tmp_path, start_workers=False)
    with TestClient(again) as client:
        password_login(client, again)
        assert client.get('/api/personas').json() == []
        assert client.get('/api/bot-profiles').json()['items'][0]['values']['persona_id'] == ''


def test_deleting_used_persona_updates_all_bindings_to_remaining_persona(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        other = client.post('/api/personas', json={'name': '伙伴', 'system_prompt': '自然说话'}).json()
        assert client.delete('/api/personas/default').status_code == 200
        assert client.get('/api/scopes').json()[0]['persona_id'] == other['id']
        assert client.get('/api/bot-profiles').json()['items'][0]['values']['persona_id'] == other['id']


def test_discovered_denied_chat_inherits_defaults_without_storing_content(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    app.state.settings.update({'onebot': {'enabled': True, 'access_token': 'isolated-fixture-token', 'blocked_groups': ['456']}})
    asyncio.run(app.state.onebot.handle_event({'post_type': 'message', 'self_id': 999, 'user_id': 123,
        'message_type': 'group', 'group_id': 456, 'message_id': 1, 'message': '不能存储的内容'}))
    scope = app.state.store.get_scope('qq:group:456')
    assert scope['profile_id'] == '' and scope['profile_overrides'] == []
    assert app.state.store.list_messages(scope['id']) == []
    app.state.store.close()


def test_restore_inheritance_clears_old_behavior_but_keeps_memory_and_name(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        id = 'qq:group:456'
        app.state.store.upsert_scope({'id': id, 'name': '测试聊天', 'always_reply': True, 'plan_enabled': False,
            'bot_name': '覆盖名字', 'output': {'enabled': False}, 'settings': {'trigger_at': False}, 'memory_space': '保留空间',
            'quiet_start': '00:00', 'quiet_end': '23:59', 'daily_tokens': 1, 'cooldown_seconds': 600})
        message = app.state.store.add_message(id, '123', 'user', '已有记录')
        result = client.post(f'/api/scopes/{id}/inherit', json={})
        assert result.status_code == 200, result.text
        scope = result.json()
        assert scope['always_reply'] is False and scope['plan_enabled'] is None
        assert scope['profile_id'] == '' and scope['profile_overrides'] == []
        assert scope['settings'] == {} and not scope.get('output') and not scope.get('bot_name')
        assert scope['quiet_start'] == scope['quiet_end'] == '' and 'daily_tokens' not in scope and 'cooldown_seconds' not in scope
        assert scope['name'] == '测试聊天' and scope['memory_space'] == '保留空间'
        assert app.state.store.get_message(message['id'])['content'] == '已有记录'


def test_gui_install_parent_adds_one_subfolder_and_preserves_existing_install(tmp_path):
    assert gui_install_target(tmp_path) == tmp_path / 'shiori'
    assert gui_install_target(tmp_path / 'MiKuBot') == tmp_path / 'MiKuBot'
    owned = tmp_path / 'custom-name'
    owned.mkdir()
    (owned / INSTALL_RECORD).write_text('{}', 'utf-8')
    assert gui_install_target(owned) == owned


def test_speech_keeps_sentence_and_closing_quotes_whole():
    sentence = '这是一个很长但是仍然没有结束的完整句子' * 5 + '。'
    assert split_output(sentence, {'output': {'target_chars': 20, 'max_chars': 40}}) == [sentence]
    text = '她说：“今天可以早点睡。”然后我们明天再聊。' * 20
    parts = split_output(text, {'output': {'target_chars': 30, 'max_chars': 55}})
    assert ''.join(parts) == text
    assert all(not part.startswith('”') for part in parts)
    text = '她说：“' + sentence + '”我们明天再聊。'
    parts = split_output(text, {'output': {'target_chars': 20, 'max_chars': 40}})
    assert ''.join(parts) == text and all(not part.startswith('”') for part in parts)


def test_transport_drops_blank_segments_and_edge_blank_lines_preserving_code():
    assert split_transport_output('\n \n\t', {}) == []
    assert split_transport_output('\n\n好的。\n\n', {}) == ['好的。']
    code = '```python\n    print("a")\n\n    print("b")\n```'
    assert split_transport_output('\n' + code + '\n', {}) == [code]
    assert not any(not part.strip() for part in split_output('\n' * 4000 + '你好。'))


def test_blacklist_revocation_stops_remaining_chunks_and_memory_access(tmp_path):
    from shiori.onebot import OneBotBridge
    from shiori.social import SocialEngine
    from shiori.memory import MemoryEngine
    from shiori.context import ContextEngine

    async def scenario():
        settings = Settings(tmp_path)
        settings.update({'onebot': {'enabled': True, 'access_token': 'isolated-fixture-token'},
                         'output': {'mode': 'length', 'target_chars': 20, 'max_chars': 40, 'interval_ms': 0}})
        store = Store(tmp_path)
        scope = 'qq:group:456'
        store.upsert_scope({'id': scope, 'plan_enabled': False})
        class Chat:
            social = SocialEngine(store, settings, None)
            async def observe(self, scope_id, user_id, text, images=None, metadata=None):
                return store.add_message(scope_id, user_id, 'user', text, **metadata)
            async def respond(self, *args, **kwargs):
                return {'reply': '第一段完整的句子。' * 30}
        bridge = OneBotBridge(store, settings, Chat())
        async def send(*args, **kwargs):
            settings.update({'onebot': {'blocked_groups': ['456']}})
            return True
        bridge.send = AsyncMock(side_effect=send)
        await bridge._flow.handle(scope, '123', '初音未来你好', [], {})
        assert bridge.send.await_count == 1
        assert not MemoryEngine(store, settings)._permission(scope, settings.get())
        assert not ContextEngine(store, settings, None)._permission(scope, '123')
        assert any('停止剩余分段' in event['message'] for event in store.list_events())
        await bridge.stop()
        store.close()
    asyncio.run(scenario())


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Tk desktop layout is checked on Windows')
def test_installer_footer_stays_in_short_window_at_high_dpi(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    from shiori.installer import run_gui

    root = tk.Tk()
    root.withdraw()
    monkeypatch.setattr(tk, 'Tk', lambda: root)
    checked = []
    def verify():
        try:
            for scaling in (1.0, 1.5, 2.0):
                root.tk.call('tk', 'scaling', scaling)
                root.geometry('560x420')
                root.update_idletasks()
                buttons = []
                def walk(widget):
                    if isinstance(widget, ttk.Button) and widget.cget('text') == '一键安装':
                        buttons.append(widget)
                    for child in widget.winfo_children():
                        walk(child)
                walk(root)
                assert len(buttons) == 1
                action = buttons[0]
                top = action.winfo_rooty() - root.winfo_rooty()
                assert 0 <= top and top + action.winfo_height() <= root.winfo_height()
                checked.append(scaling)
        finally:
            root.destroy()
    monkeypatch.setattr(root, 'mainloop', verify)
    run_gui(tmp_path / 'unused.zip', {'version': '0.1.1'}, tmp_path / 'shiori')
    assert checked == [1.0, 1.5, 2.0]
