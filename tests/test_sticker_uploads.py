"""Owner-uploaded stickers, fixed categories and on-demand semantic analysis."""
import asyncio
import base64
import io
import json
import zipfile

from PIL import Image
from fastapi.testclient import TestClient
import pytest

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store
from shiori.settings import Settings
from shiori.social import SocialEngine
from shiori.stickers import (CATEGORIES, CATEGORY_GUIDE, StickerLibrary, StickerReplyError, _json, folder_category,
                             model_picture, vision_image)


def image(color='cyan', fmt='PNG', size=(16, 16)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, format=fmt)
    return out.getvalue()


def gif(frames=6):
    out = io.BytesIO()
    pictures = [Image.new('RGB', (20, 20), (index * 40 % 256, 80, 160)) for index in range(frames)]
    pictures[0].save(out, format='GIF', save_all=True, append_images=pictures[1:], duration=80, loop=0)
    return out.getvalue()


def archive(entries, *, utf8=True):
    out, swaps = io.BytesIO(), []
    with zipfile.ZipFile(out, 'w') as zf:
        for index, (name, data) in enumerate(entries):
            if utf8:
                zf.writestr(name, data)
                continue
            # Windows zip tools store GBK bytes without the UTF-8 flag; zipfile
            # cannot write that, so write a same-length ASCII name and swap it.
            raw = name.encode('gbk')
            placeholder = f'{index:04d}'.ljust(len(raw), 'x')
            zf.writestr(placeholder, data)
            swaps.append((placeholder.encode(), raw))
    value = out.getvalue()
    for placeholder, raw in swaps:
        value = value.replace(placeholder, raw)
    return value


class Vision:
    def __init__(self):
        self.calls = []
        self.answer = {'is_sticker': True, 'clear': True, 'meaning': '害羞地捂脸，适合回应别人的夸奖', 'category': '害羞',
                       'tags': ['害羞', '被夸'], 'visible_text': '才没有', 'confidence': .95}
        self.fail = None
        self.during = None
        self.raw = None                      # a verbatim reply instead of the JSON answer

    async def complete(self, messages, **kwargs):
        assert kwargs['before_send']()
        self.calls.append((messages, kwargs))
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        if self.raw is not None:
            return {'content': self.raw}
        return {'content': json.dumps(self.answer, ensure_ascii=False)}


@pytest.fixture
def library(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'onebot': {'enabled': True, 'access_token': 'isolated-fixture-token', 'chat_policy': 'blacklist'},
                     'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'local-test', 'vision': True},
                     'stickers': {'collect_probability': 1, 'send_probability': 1}})
    model = Vision()
    engine = StickerLibrary(store, settings, model, SocialEngine(store, settings, model)._allowed)
    store.upsert_scope({'id': 'qq:group:1'})
    yield store, settings, model, engine
    asyncio.run(engine.close())
    store.close()


def analyse(engine, **kwargs):
    async def run():
        status = engine.start_analysis(**kwargs)
        await asyncio.gather(*engine._jobs)
        return status
    return asyncio.run(run())


def test_images_and_archives_upload_with_category_hints_and_duplicates(library):
    store, cfg, model, engine = library
    pack = archive([('memes/shy/a.png', image('red')), ('memes/生气/b.jpg', image('blue', 'JPEG')),
                    ('memes/c.webp', image('green', 'WEBP')), ('__MACOSX/memes/._a.png', b'junk'),
                    ('memes/manifest.json', b'{}'), ('memes/broken.png', b'not an image'), ('dup.png', image('cyan'))])
    result = engine.upload([('one.png', image('cyan')), ('pack.zip', pack)], '')
    assert result['added'] == 4 and result['duplicates'] == 0
    reasons = ' '.join(x['reason'] for x in result['rejected'])
    assert '非图片' in reasons and 'broken.png' in ' '.join(x['name'] for x in result['rejected'])
    items = {x['filename'].split('/')[-1]: x for x in engine.page(origin='upload')['items']}
    assert items['a.png']['category'] == '害羞' and items['b.jpg']['category'] == '生气'
    assert items['c.webp']['category'] == '待标注' and items['one.png']['category'] == '待标注'
    assert all(x['origin'] == 'upload' and x['locked'] and not x['clear'] and x['analysis_status'] == 'pending'
               for x in items.values())
    assert engine.page(origin='collected')['total'] == 0
    # Not sendable until its meaning is known, and nothing was analysed yet.
    assert engine.sendable_categories('qq:group:1') == []
    assert model.calls == [] and engine.analysis_status()['pending'] == 4
    again = engine.upload([('one-again.png', image('cyan'))], '开心')
    assert again['added'] == 0 and again['duplicates'] == 1


def test_gbk_archive_names_and_a_chosen_category(library):
    store, cfg, model, engine = library
    pack = archive([('我的表情/委屈/哭哭.png', image('purple'))], utf8=False)
    engine.upload([('包.zip', pack)], '')
    item = engine.page(origin='upload')['items'][0]
    assert item['filename'] == '我的表情/委屈/哭哭.png' and item['category'] == '委屈'
    engine.upload([('x.png', image('white'))], '得意')
    assert {x['category'] for x in engine.page(origin='upload')['items']} == {'委屈', '得意'}
    with pytest.raises(ValueError, match='固定分类'):
        engine.upload([('y.png', image('black'))], '自创分类')


def test_manual_analysis_labels_uploads_and_makes_them_sendable_without_using_the_daily_quota(library):
    store, cfg, model, engine = library
    cfg.update({'stickers': {'daily_recognitions': 0, 'daily_model_calls': 5}})
    engine.upload([('a.png', image('red'))], '开心')
    analyse(engine)
    item = engine.page(origin='upload')['items'][0]
    assert item['meaning'].startswith('害羞') and item['category'] == '害羞' and item['clear']
    assert item['visible_text'] == '才没有' and item['analysis_status'] == 'done' and item['version'] == 2
    messages, kwargs = model.calls[0]
    assert kwargs['purpose'] == 'sticker_vision' and kwargs['scope_id'] == 'web:default'
    assert '开心' in messages[-1]['content'][0]['text']           # the uploader's category is a prior
    assert all(name in messages[0]['content'] for name in CATEGORIES if name != '待标注')
    status = engine.analysis_status()
    assert status['running'] is False and status['done'] == 1 and status['pending'] == 0
    assert engine.quota_status()['vision'] == 0
    assert engine.sendable_categories('qq:group:1') == ['害羞']
    assert engine.pick('qq:group:1', '害羞', '被夸')['id'] == item['id']
    assert engine.payload(item['id'], 'qq:group:1')[0]['id'] == item['id']


def test_unclear_answers_keep_the_uploaders_category_but_stay_unsent(library):
    store, cfg, model, engine = library
    model.answer.update(clear=False, meaning='看不出来')
    engine.upload([('a.png', image('red'))], '无语')
    analyse(engine)
    item = engine.page(origin='upload')['items'][0]
    assert item['category'] == '无语' and not item['clear'] and item['analysis_status'] == 'done'


def test_failed_analysis_can_be_retried_and_edits_are_never_overwritten(library):
    store, cfg, model, engine = library
    engine.upload([('a.png', image('red')), ('b.png', image('blue'))], '')
    model.fail = ValueError('bad json')
    analyse(engine)
    assert engine.analysis_status()['failed'] == 2 and engine.analysis_status()['pending'] == 2
    assert {x['analysis_status'] for x in engine.page(origin='upload')['items']} == {'failed'}
    model.fail = None
    first = engine.page(origin='upload')['items'][0]
    # The owner labels one by hand while its analysis is in flight.
    model.during = lambda: model.during and engine.update(first['id'], {'meaning': '我自己写的含义', 'clear': True,
                                                                        'category': '得意'}) and setattr(model, 'during', None)
    analyse(engine)
    items = {x['id']: x for x in engine.page(origin='upload')['items']}
    assert items[first['id']]['meaning'] == '我自己写的含义' and items[first['id']]['analysis_status'] == 'manual'
    assert engine.analysis_status()['pending'] == 0


def test_reanalysis_by_ids_or_filter_and_one_job_at_a_time(library):
    store, cfg, model, engine = library
    engine.upload([('a.png', image('red')), ('b.png', image('blue'))], '')
    analyse(engine)
    with pytest.raises(ValueError, match='没有需要分析'):
        engine.start_analysis()
    model.answer.update(category='得意', meaning='得意地叉腰，适合炫耀成功')
    key = engine.page(origin='upload')['items'][0]['id']
    analyse(engine, ids=[key])
    assert {x['id']: x['category'] for x in engine.page(origin='upload')['items']}[key] == '得意'
    analyse(engine, filters={'category': '害羞', 'origin': 'upload'})
    assert {x['category'] for x in engine.page(origin='upload')['items']} == {'得意'}

    async def overlapping():
        engine.start_analysis(filters={})
        with pytest.raises(ValueError, match='已有一个'):
            engine.start_analysis(filters={})
        engine.stop_analysis()
        await asyncio.gather(*engine._jobs)
    asyncio.run(overlapping())


def test_analysis_needs_a_vision_model(library):
    store, cfg, model, engine = library
    engine.upload([('a.png', image('red'))], '')
    cfg.update({'model': {'vision': False}})
    with pytest.raises(ValueError, match='视觉'):
        engine.start_analysis()


def test_a_vision_model_gets_a_gif_as_a_png():
    def url(raw, mime='image/gif'):
        return f'data:{mime};base64,' + base64.b64encode(raw).decode()
    still, frames = model_picture(url(image('red', 'GIF', (3000, 100))))
    assert still.startswith('data:image/png;base64,') and frames == 1
    with Image.open(io.BytesIO(base64.b64decode(still.split(',', 1)[1]))) as picture:
        assert picture.format == 'PNG' and picture.size == (1536, 51)       # no larger than MODEL_SIDE
    strip, frames = model_picture(url(gif(6)))
    assert strip.startswith('data:image/png;base64,') and frames == 4
    other = url(image('red'), 'image/png')
    assert model_picture(other) == (other, 1)
    broken = url(b'GIF89a not really')
    assert model_picture(broken) == (broken, 1)


def test_animated_stickers_are_analysed_from_several_frames(library):
    url, frames = vision_image(gif(6))
    assert frames == 4 and url.startswith('data:image/png;base64,')
    assert vision_image(image('red'))[1] == 1
    store, cfg, model, engine = library
    engine.upload([('moving.gif', gif(6))], '')
    analyse(engine)
    content = model.calls[0][0][-1]['content']
    assert '动图' in content[0]['text'] and content[-1]['image_url']['url'].startswith('data:image/png')


def test_categories_are_fixed_and_old_custom_ones_fold_into_other(tmp_path, library):
    store, cfg, model, engine = library
    engine.upload([('a.png', image('red'))], '')
    key = engine.page()['items'][0]['id']
    with pytest.raises(ValueError, match='固定分类'):
        engine.update(key, {'category': '礼貌'})
    with engine._lock, engine._conn:
        item = engine.get(key)
        item.update(category='礼貌', tags=['谢谢'])
        engine._conn.execute('UPDATE stickers SET data=? WHERE id=?', (json.dumps(item, ensure_ascii=False), key))
    reopened = StickerLibrary(store, cfg, model, engine.allowed)
    try:
        item = reopened.get(key)
        assert item['category'] == '其他' and item['tags'] == ['谢谢', '礼貌']
        page = reopened.page()
        assert page['categories'] == list(CATEGORIES) and page['guide'] == CATEGORY_GUIDE
    finally:
        asyncio.run(reopened.close())


def test_a_chat_gets_the_other_stickers_before_one_comes_again(library):
    store, cfg, model, engine = library
    store.upsert_scope({'id': 'qq:group:2'})
    engine.upload([('a.png', image('red')), ('b.png', image('blue'))], '')
    analyse(engine)
    first = engine.pick('qq:group:1', '害羞', '被夸')
    engine.note_sent(first['id'], 'qq:group:1', first['version'])
    second = engine.pick('qq:group:1', '害羞', '被夸')
    assert second['id'] != first['id']
    engine.note_sent(second['id'], 'qq:group:1', second['version'])
    # Both went out here recently: the earlier one comes back instead of nothing.
    assert engine.pick('qq:group:1', '害羞', '被夸')['id'] == first['id']
    # Each chat keeps its own list of recent stickers.
    engine.note_sent(second['id'], 'qq:group:2', second['version'])
    assert engine.pick('qq:group:2', '害羞', '被夸')['id'] == first['id']


def test_deleted_uploads_come_back_when_uploaded_again_and_full_libraries_say_so(library):
    store, cfg, model, engine = library
    engine.upload([('a.png', image('red'))], '')
    key = engine.page()['items'][0]['id']
    engine.delete(key)
    assert engine.upload([('a.png', image('red'))], '')['added'] == 1
    assert engine.get(key)['status'] == 'active' and engine.get(key)['version'] == 3
    cfg.update({'stickers': {'max_items': 1, 'auto_prune': False}})
    result = engine.upload([('b.png', image('blue'))], '')
    assert result['added'] == 0 and '已满' in result['rejected'][-1]['reason']


def test_upload_and_analysis_api(tmp_path):
    app = create_app(tmp_path / 'app', start_workers=False)
    app.state.settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'test', 'vision': True}})
    app.state.chat.stickers.provider = Vision()
    files = [('files', ('a.png', image('red'), 'image/png')),
             ('files', ('pack.zip', archive([('happy/b.png', image('blue'))]), 'application/zip'))]
    with TestClient(app) as client:
        assert client.post('/api/stickers/upload', files=files).status_code == 401
        assert client.post('/api/stickers/analysis', json={}).status_code == 401
        password_login(client, app)
        result = client.post('/api/stickers/upload', files=files, data={'category': ''}).json()
        assert result['added'] == 2 and result['analysis']['pending'] == 2
        page = client.get('/api/stickers?origin=upload&category=开心').json()
        assert page['total'] == 1 and page['analysis']['pending'] == 2
        assert client.get('/api/stickers?origin=elsewhere').status_code == 400
        assert client.post('/api/stickers/upload', files=files, data={'category': '乱填'}).status_code == 400
        assert client.post('/api/stickers/analysis', json={'ids': ['x'], 'filters': {}}).status_code == 400
        started = client.post('/api/stickers/analysis', json={}).json()
        assert started['running'] is True and started['total'] == 2
        status = client.get('/api/stickers/analysis').json()
        assert status['total'] == 2
        client.post('/api/stickers/analysis/stop')



# 2026-11-16 on the server: 25 of 54 uploads failed because the model quoted the
# translated line with plain double quotes inside the JSON string.
@pytest.mark.parametrize('reply', [
    '{"is_sticker": true, "clear": true, "meaning": "表示"了解"、"收到"的意思", "category": "赞同", "tags": ["了解"], "visible_text": "了解っ", "confidence": 0.95}',
    '```json\n{"meaning": "用"OK"回应", "category": "赞同", "tags": ["OK", "收到"], "confidence": 0.9}\n```',
    '好的：\n{"meaning": "说"哼"表示不服气，\n嘴硬", "category": "生气", "tags": [], "confidence": 0.9}',
    '{"meaning": "说"好", 然后答应", "category": "赞同", "tags": [], "confidence": 0.9}',
])
def test_replies_with_bare_quotes_inside_strings_are_still_read(reply):
    value = _json(reply)
    assert value['category'] in CATEGORIES and '"' in value['meaning']
    assert isinstance(value['tags'], list) and isinstance(value['confidence'], float)


def test_an_unreadable_reply_keeps_its_start_for_the_run_log():
    with pytest.raises(StickerReplyError, match='识图返回格式不正确，回复开头：抱歉，我无法'):
        _json('抱歉，我无法识别这张图片。')
    with pytest.raises(StickerReplyError, match='回复为空'):
        _json('   ')


def test_analysis_labels_an_upload_whose_reply_quotes_the_line_with_plain_quotes(library):
    store, settings, model, engine = library
    model.raw = ('```json\n{"is_sticker": true, "clear": true, "meaning": "说"了解"，表示收到、没问题", '
                 '"category": "赞同", "tags": ["收到"], "visible_text": "了解っ", "confidence": 0.95}\n```')
    engine.upload([('a.png', image('red'))], '')
    status = analyse(engine)
    item = engine.page(origin='upload')['items'][0]
    assert status['total'] == 1 and engine.analysis_status()['done'] == 1 and engine.analysis_status()['failed'] == 0
    assert item['meaning'] == '说"了解"，表示收到、没问题' and item['category'] == '赞同' and item['analysis_status'] == 'done'


def test_a_failed_reply_is_shown_in_the_run_log(library):
    store, settings, model, engine = library
    store.exception_redactor = str                       # the app installs its secret redactor at start
    model.raw = '这张图我看不清。'
    engine.upload([('a.png', image('red'))], '')
    analyse(engine)
    event = next(x for x in store.list_events(50) if x['kind'] == 'stickers_error')
    assert '回复开头：这张图我看不清' in event['details']['error_message']
    assert engine.page(origin='upload')['items'][0]['analysis_status'] == 'failed'


def test_the_prompt_asks_for_corner_brackets_when_quoting(library):
    store, settings, model, engine = library
    engine.upload([('a.png', image('red'))], '')
    analyse(engine)
    assert '用「」' in model.calls[0][0][0]['content']


@pytest.mark.parametrize('folder,category', [
    ('01-日常问候', '问候'), ('02-开心撒娇', '开心'), ('04-惊讶好奇', '惊讶'), ('06-日常动作和安慰', '安慰'),
    ('03-傲娇闹别扭', None), ('05-困倦放松', None), ('2. happy', '开心'), ('其他', '其他'), ('开心与难过', None)])
def test_numbered_and_descriptive_folder_names_become_category_hints(folder, category):
    assert folder_category(folder) == category


def test_numbered_folders_in_a_pack_label_their_images(library):
    store, settings, model, engine = library
    pack = archive([('表情/01-日常问候/02-やっほー.png', image('red')), ('表情/03-傲娇闹别扭/01-ふんっ.png', image('blue'))])
    engine.upload([('pack.zip', pack)], '')
    items = {x['filename'].split('/')[-1]: x for x in engine.page(origin='upload')['items']}
    assert items['02-やっほー.png']['category'] == '问候' and items['01-ふんっ.png']['category'] == '待标注'


def test_uploads_stored_without_a_hint_use_their_folder_when_analysed(library):
    store, settings, model, engine = library
    engine.upload([('a.png', image('red'))], '')
    key = engine.page(origin='upload')['items'][0]['id']
    with engine._lock, engine._conn:                     # as packs uploaded before this fix were stored
        record = json.loads(engine._conn.execute('SELECT data FROM stickers WHERE id=?', (key,)).fetchone()[0])
        record.update(filename='01-日常问候/02-やっほー.png', prior_category='', category='待标注')
        engine._conn.execute('UPDATE stickers SET data=? WHERE id=?', (json.dumps(record, ensure_ascii=False), key))
    model.answer.update(clear=False, confidence=.4)
    analyse(engine)
    assert '问候' in model.calls[-1][0][-1]['content'][0]['text']
    assert engine.page(origin='upload')['items'][0]['category'] == '问候'
