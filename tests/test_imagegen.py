"""Image generation in QQ chats (owner request, 2026-11-18).

Asked to draw, take a photo, a selfie or edit a picture, the chat model may
call generate_image. The text reply goes out first; the picture is drawn in
the background and sent on its own. Services: OpenAI-compatible image APIs,
Gemini, NovelAI and ComfyUI (adapters follow the owner's AstrBot plugin
随想画卷). A persona's appearance prompt is added to self portraits, and a
picture attached to the message can be used as the reference. Each QQ
account may draw a set number per day; administrators are not limited and a
picture that fails gives its count back.
"""
import asyncio
import base64
import io
import json
import zipfile
from copy import deepcopy

import httpx
import pytest
from PIL import Image

from shiori import imagegen as ig
from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.social import requested_images

PRIVATE, GROUP = 'qq:private:303', 'qq:group:404'
KEY = 'sk-fixture-secret-123456'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def png(color='red', size=(64, 64)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, format='PNG')
    return out.getvalue()


def data_url(raw):
    return 'data:image/png;base64,' + base64.b64encode(raw).decode()


class Config:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['output'].update(mode='model', interval_ms=0)
        self.config['model'].update(base_url='https://fixture.invalid/v1', model='fixture', vision=True)
        self.config['stickers'].update(send_enabled=False)

    def get(self):
        return deepcopy(self.config)


class Recorder:
    """An HTTP stand-in: records requests, answers with `handler`."""

    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)

    def transport(self):
        return httpx.MockTransport(self)


def openai_ok(request):
    return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png('blue')).decode()}]})


def service(kind='openai', **extra):
    return ig.clean_service(dict({'kind': kind, 'api_key': KEY, 'model': {'openai': 'gpt-image-1', 'gemini': 'gemini-image'}.get(kind, '')},
                                 **extra))


@pytest.fixture
def make_gen(tmp_path):
    stores = []

    def build(handler=openai_ok, **patch):
        store = Store(tmp_path / f'gen{len(stores)}')
        stores.append(store)
        recorder = Recorder(handler)
        gen = ig.ImageGen(store, Config(), transport=recorder.transport())
        gen.update(dict({'services': [{'id': 'main', 'kind': 'openai', 'api_key': KEY, 'model': 'gpt-image-1'}],
                         'service_id': 'main', 'enabled': True}, **patch))
        return gen, recorder, store
    yield build
    for store in stores:
        store.close()


# ---------- configuration ----------

def test_keys_are_kept_protected_and_never_shown(make_gen):
    gen, _, store = make_gen()
    on_disk = gen.path.read_text('utf-8')
    assert KEY not in on_disk and 'api_key' in on_disk
    public = gen.public()
    assert public['services'][0]['api_key_set'] is True and 'api_key' not in public['services'][0]
    # Saving without a key keeps it; clearing removes it.
    item = {k: v for k, v in public['services'][0].items() if k not in ('api_key_set', 'supports_reference')}
    gen.update({'services': [dict(item, name='改名')]})
    assert gen.service()['api_key'] == KEY and gen.service()['name'] == '改名'
    # A fresh instance reads the protected key back.
    again = ig.ImageGen(store, Config())
    assert again.service()['api_key'] == KEY and again.service()['name'] == '改名' and again.available(PRIVATE)
    gen.update({'services': [dict(item, clear_api_key=True)]})
    assert gen.service()['api_key'] == ''


@pytest.mark.parametrize('item, message', [
    ({'kind': 'openai', 'endpoint': 'https://user:pass@api.example/v1', 'model': 'm'}, '账号'),
    ({'kind': 'openai', 'endpoint': 'https://api.example/v1?key=1', 'model': 'm'}, '查询参数'),
    ({'kind': 'gemini', 'model': ''}, '模型名称'),             # OpenAI falls back to gpt-image-1
    ({'kind': 'midjourney'}, '类型'),
    ({'kind': 'openai', 'model': 'm', 'timeout_seconds': 3}, '等待上限'),
    ({'kind': 'openai', 'model': 'm', 'surprise': 1}, '不支持的字段'),
    ({'kind': 'openai', 'model': 'm', 'extra_body': []}, 'JSON 对象'),
    ({'kind': 'comfyui', 'workflow': {'nodes': []}}, '导出 API'),
    ({'kind': 'comfyui', 'workflow': {'6': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}}},
      'workflow_fields': {'prompt': '7.inputs.text'}}, '不存在'),
    ({'kind': 'comfyui', 'workflow': {'6': {'class_type': 'KSampler', 'inputs': {'model': ['4', 0], 'text': ''}}},
      'workflow_fields': {'prompt': '6.inputs.model'}}, '连线'),
    ({'kind': 'comfyui', 'workflow': {'6': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}}},
      'workflow_fields': {}}, 'prompt'),
])
def test_invalid_services_are_refused_with_a_reason(item, message):
    with pytest.raises(ValueError, match=message):
        ig.clean_service(item)


def test_enabling_needs_a_selected_service(make_gen):
    gen, _, _ = make_gen()
    with pytest.raises(ValueError, match='先添加并选择'):
        gen.update({'service_id': ''})
    with pytest.raises(ValueError, match='不存在'):
        gen.update({'service_id': 'missing'})


def test_a_self_portrait_gets_the_persona_look_in_the_services_style():
    natural, tags = service('openai', negative_prompt='lowres'), service('novelai', negative_prompt='lowres')
    prompt, negative = ig.compose(natural, 'waving at the camera', appearance='brown hair', appearance_negative='glasses')
    assert prompt.startswith('waving at the camera') and 'brown hair' in prompt and 'Avoid' not in prompt
    assert negative == 'lowres, glasses'
    prompt, _ = ig.compose(tags, '1girl, waving', appearance='brown hair')
    assert prompt == 'brown hair, 1girl, waving'
    prompt, _ = ig.compose(service('gemini', negative_prompt='text', natural_negative=True), 'a cat')
    assert prompt.endswith('Avoid: text')


# ---------- adapters ----------

@pytest.mark.anyio
async def test_openai_generation_and_edit_requests():
    recorder = Recorder(openai_ok)
    raw, mime = await ig.generate(service('openai'), 'a cat', aspect='portrait', transport=recorder.transport())
    assert mime == 'image/png' and ig.image_info(raw)[2] == (64, 64)
    request = recorder.requests[-1]
    assert str(request.url) == 'https://api.openai.com/v1/images/generations'
    assert request.headers['Authorization'] == 'Bearer ' + KEY
    body = json.loads(request.content)
    assert body == {'model': 'gpt-image-1', 'prompt': 'a cat', 'n': 1, 'size': '1024x1536'}
    await ig.generate(service('openai'), 'make it night', references=[png('green')], transport=recorder.transport())
    request = recorder.requests[-1]
    assert str(request.url).endswith('/images/edits') and request.headers['Content-Type'].startswith('multipart/form-data')
    assert b'name="image"; filename="reference-0.png"' in request.content and b'make it night' in request.content
    # DALL-E 3 cannot edit, and older models ask for base64 explicitly.
    with pytest.raises(ig.ImageGenError, match='不支持参考图'):
        await ig.generate(service('openai', model='dall-e-3'), 'x', references=[png()], transport=recorder.transport())
    await ig.generate(service('openai', model='dall-e-3'), 'x', aspect='landscape', transport=recorder.transport())
    assert json.loads(recorder.requests[-1].content)['size'] == '1792x1024'
    assert json.loads(recorder.requests[-1].content)['response_format'] == 'b64_json'


@pytest.mark.anyio
async def test_an_image_url_answer_is_downloaded_through_the_public_downloader(monkeypatch):
    fetched = []
    monkeypatch.setattr('shiori.stickers.download_image', lambda url, max_bytes: fetched.append(url) or (png('pink'), 'image/png'))
    recorder = Recorder(lambda request: httpx.Response(200, json={'data': [{'url': 'https://cdn.example/x.png'}]}))
    raw, _ = await ig.generate(service('openai', model='flux-dev'), 'a cat', transport=recorder.transport())
    assert fetched == ['https://cdn.example/x.png'] and ig.image_info(raw)[1] == 'image/png'


@pytest.mark.anyio
async def test_upstream_errors_keep_the_key_out_of_messages_and_details():
    recorder = Recorder(lambda request: httpx.Response(401, json={'error': {'message': f'invalid key {KEY}'}}))
    with pytest.raises(ig.ImageGenError) as caught:
        await ig.generate(service('openai'), 'a cat', transport=recorder.transport())
    assert 'HTTP 401' in str(caught.value) and '密钥' in str(caught.value)
    assert KEY not in str(caught.value) and KEY not in caught.value.detail and '[REDACTED]' in caught.value.detail
    broken = Recorder(lambda request: httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(b'not an image').decode()}]}))
    with pytest.raises(ig.ImageGenError, match='损坏'):
        await ig.generate(service('openai'), 'a cat', transport=broken.transport())


@pytest.mark.anyio
async def test_gemini_sends_the_aspect_ratio_and_references_inline():
    def answer(request):
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [
            {'text': 'here', 'thought': True}, {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(png()).decode()}}]}}]})
    recorder = Recorder(answer)
    await ig.generate(service('gemini'), 'a cat', aspect='landscape', references=[png('green')], transport=recorder.transport())
    request = recorder.requests[-1]
    assert str(request.url) == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-image:generateContent'
    assert request.headers['x-goog-api-key'] == KEY and 'Authorization' not in request.headers
    body = json.loads(request.content)
    assert body['generationConfig']['imageConfig'] == {'aspectRatio': '4:3'}
    assert body['contents'][0]['parts'][1]['inlineData']['mimeType'] == 'image/png'
    blocked = Recorder(lambda request: httpx.Response(200, json={'promptFeedback': {'blockReason': 'SAFETY'}}))
    with pytest.raises(ig.ImageGenError, match='安全策略'):
        await ig.generate(service('gemini'), 'x', transport=blocked.transport())


@pytest.mark.anyio
async def test_novelai_reads_the_zip_and_edits_with_img2img():
    def answer(request):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('image_0.png', png('purple'))
        return httpx.Response(200, content=output.getvalue(), headers={'Content-Type': 'application/x-zip-compressed'})
    recorder = Recorder(answer)
    raw, _ = await ig.generate(service('novelai', negative_prompt='lowres'), '1girl', 'lowres', aspect='portrait',
                               transport=recorder.transport())
    assert ig.image_info(raw)[1] == 'image/png'
    body = json.loads(recorder.requests[-1].content)
    assert str(recorder.requests[-1].url) == 'https://image.novelai.net/ai/generate-image'
    assert body['action'] == 'generate' and body['model'] == 'nai-diffusion-4-5-full'
    assert (body['parameters']['width'], body['parameters']['height']) == (832, 1216)
    assert body['parameters']['v4_negative_prompt']['caption']['base_caption'] == 'lowres'
    await ig.generate(service('novelai'), '1girl', references=[png('green', (200, 100))], transport=recorder.transport())
    body = json.loads(recorder.requests[-1].content)
    assert body['action'] == 'img2img' and body['parameters']['strength'] == .6
    assert (body['parameters']['width'], body['parameters']['height']) == (1216, 832)   # follows the reference
    assert ig.image_info(base64.b64decode(body['parameters']['image']))[2] == (1216, 832)


COMFY = {'3': {'class_type': 'KSampler', 'inputs': {'seed': 1, 'model': ['4', 0]}},
         '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512, 'height': 512}},
         '6': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
         '7': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
         '10': {'class_type': 'LoadImage', 'inputs': {'image': 'x.png'}},
         '9': {'class_type': 'SaveImage', 'inputs': {'images': ['8', 0]}}}
FIELDS = {'prompt': '6.inputs.text', 'negative_prompt': '7.inputs.text', 'width': '5.inputs.width',
          'height': '5.inputs.height', 'seed': '3.inputs.seed', 'reference': '10.inputs.image'}


@pytest.mark.anyio
async def test_comfyui_submits_the_mapped_workflow_and_fetches_the_output():
    polls = []

    def answer(request):
        path = request.url.path
        if path == '/upload/image':
            return httpx.Response(200, json={'name': 'ref.png', 'subfolder': 'shiori', 'type': 'input'})
        if path == '/prompt':
            return httpx.Response(200, json={'prompt_id': 'abc123'})
        if path == '/history/abc123':
            polls.append(1)
            if len(polls) < 2:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={'abc123': {'status': {'completed': True},
                                                        'outputs': {'9': {'images': [{'filename': 'out.png', 'subfolder': '', 'type': 'output'}]}}}})
        if path == '/view':
            return httpx.Response(200, content=png('orange'), headers={'Content-Type': 'image/png'})
        return httpx.Response(404)
    recorder = Recorder(answer)
    comfy = service('comfyui', api_key='', workflow=COMFY, workflow_fields=FIELDS)
    assert ig.supports_reference(comfy)
    raw, _ = await ig.generate(comfy, 'a cat', 'lowres', aspect='landscape', references=[png()], transport=recorder.transport())
    assert ig.image_info(raw)[1] == 'image/png'
    submitted = json.loads(next(r for r in recorder.requests if r.url.path == '/prompt').content)['prompt']
    assert submitted['6']['inputs']['text'] == 'a cat' and submitted['7']['inputs']['text'] == 'lowres'
    assert submitted['10']['inputs']['image'] == 'shiori/ref.png'
    assert (submitted['5']['inputs']['width'], submitted['5']['inputs']['height']) == (1024, 1024)  # square reference
    assert submitted['3']['inputs']['model'] == ['4', 0] and submitted['3']['inputs']['seed'] != 1
    view = next(r for r in recorder.requests if r.url.path == '/view')
    assert view.url.params['filename'] == 'out.png' and view.url.params['type'] == 'output'
    assert 'Authorization' not in view.headers


@pytest.mark.anyio
async def test_a_comfyui_timeout_removes_only_its_own_queued_prompt():
    def answer(request):
        if request.url.path == '/prompt':
            return httpx.Response(200, json={'prompt_id': 'slow1'})
        if request.url.path.startswith('/history/'):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})
    recorder = Recorder(answer)
    comfy = service('comfyui', api_key='', workflow=COMFY, workflow_fields={'prompt': '6.inputs.text'}, timeout_seconds=5)
    with pytest.raises(ig.ImageGenError, match='5 秒'):
        await ig.generate(comfy, 'a cat', transport=recorder.transport())
    deleted = [json.loads(r.content) for r in recorder.requests if r.url.path == '/queue']
    assert deleted == [{'delete': ['slow1']}]


def test_pictures_are_converted_for_each_connector():
    big = io.BytesIO()
    Image.effect_noise((1800, 1800), 120).convert('RGB').save(big, format='PNG')
    assert len(big.getvalue()) > ig.OFFICIAL_MAX_BYTES
    official = ig.official_image(big.getvalue())
    assert len(official) <= ig.OFFICIAL_MAX_BYTES and ig.image_info(official)[1] == 'image/jpeg'
    small = png()
    assert ig.onebot_image(small) == small                       # sent as-is when it fits
    webp = io.BytesIO()
    Image.new('RGB', (32, 32), 'red').save(webp, format='WEBP')
    assert ig.image_info(ig.onebot_image(webp.getvalue()))[1] == 'image/jpeg'


# ---------- queue and daily counts ----------

@pytest.mark.anyio
async def test_each_account_has_a_daily_count_and_administrators_have_none(make_gen):
    gen, _, store = make_gen(daily_per_user=2)
    for _ in range(2):
        job = gen.queue(PRIVATE, 'qq:303', prompt='a cat')
        await gen.run(job['id'])
    assert job['remaining_today'] == 0
    with pytest.raises(ig.ImageGenError, match='每人每天 2 张'):
        gen.queue(PRIVATE, 'qq:303', prompt='a cat')
    assert gen.queue(PRIVATE, 'qq:505', prompt='a cat')['remaining_today'] == 1
    admin = gen.queue(PRIVATE, 'qq:303', admin=True, prompt='a cat')
    assert admin['remaining_today'] is None
    gen.update({'daily_per_user': 0})
    with pytest.raises(ig.ImageGenError, match='只对管理员开放'):
        gen.queue(PRIVATE, 'qq:606', prompt='a cat')
    # The counts survive a restart and start over the next day.
    again = ig.ImageGen(store, Config())
    assert again.remaining('qq:303') == 0
    again._usage['day'] = '2000-01-01'
    assert again.update({'daily_per_user': 2}) and again.remaining('qq:303') == 2


@pytest.mark.anyio
async def test_a_failed_or_dropped_picture_gives_its_count_back(make_gen):
    gen, _, store = make_gen(lambda request: httpx.Response(500, json={'error': 'boom'}), daily_per_user=1)
    job = gen.queue(PRIVATE, 'qq:303', prompt='a cat')
    assert gen.remaining('qq:303') == 0
    with pytest.raises(ig.ImageGenError):
        await gen.run(job['id'])
    assert gen.remaining('qq:303') == 1
    assert any(event['kind'] == 'imagegen_failed' for event in store.list_events(limit=20))
    job = gen.queue(PRIVATE, 'qq:303', prompt='a cat')
    assert gen.discard(job['id']) and gen.remaining('qq:303') == 1
    job = gen.queue(PRIVATE, 'qq:303', prompt='a cat')
    gen._jobs[job['id']].created -= ig.PENDING_TTL + 1          # its reply never went out
    gen.queue(PRIVATE, 'qq:303', prompt='again')
    assert job['id'] not in gen._jobs and gen.remaining('qq:303') == 0


def test_the_queue_is_bounded_and_only_qq_chats_may_queue(make_gen):
    gen, _, _ = make_gen(daily_per_user=100)
    with pytest.raises(ig.ImageGenError, match='没有开启'):
        gen.queue('web:default', 'qq:303', prompt='a cat')
    for index in range(ig.QUEUE_LIMIT):
        gen.queue(GROUP, f'qq:{index}', prompt='a cat')
    with pytest.raises(ig.ImageGenError, match='太多'):
        gen.queue(GROUP, 'qq:999', prompt='a cat')
    gen.update({'enabled': False})
    assert not gen.available(PRIVATE)


@pytest.mark.anyio
async def test_the_management_test_uses_no_count(make_gen):
    gen, recorder, _ = make_gen(daily_per_user=1)
    result = await gen.test('a cat', aspect='landscape')
    assert result['image'].startswith('data:image/png;base64,') and gen.remaining('qq:303') == 1
    assert json.loads(recorder.requests[-1].content)['size'] == '1536x1024'


# ---------- chat and QQ ----------

def answer(content='', calls=()):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = list(calls)
    return {'content': content, 'tool_calls': list(calls), 'message': message, 'usage': {'input_tokens': 1, 'output_tokens': 1}}


def draw(**arguments):
    return {'id': 'call-1', 'type': 'function', 'function': {'name': 'generate_image',
                                                              'arguments': json.dumps(arguments, ensure_ascii=False)}}


class Model:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    async def complete(self, messages, **kwargs):
        if kwargs.get('purpose', 'chat') != 'chat':
            return answer('{}')
        self.seen.append({'system': messages[0]['content'], 'tools': kwargs.get('tools') or []})
        return self.turns.pop(0) if self.turns else answer('嗯。')


@pytest.fixture
def make_chat(tmp_path):
    made = []

    def build(model, handler=openai_ok, **patch):
        store, config = Store(tmp_path / f'chat{len(made)}'), Config()
        for scope in (PRIVATE, GROUP):
            store.upsert_scope({'id': scope, 'auto_memory': False, 'merge_window_ms': 0, 'plan_enabled': False})
        chat = ChatService(store, config, model)
        recorder = Recorder(handler)
        chat.imagegen.transport = recorder.transport()
        chat.imagegen.update(dict({'services': [{'id': 'main', 'kind': 'openai', 'api_key': KEY, 'model': 'gpt-image-1'}],
                                   'service_id': 'main', 'enabled': True, 'daily_per_user': 3,
                                   'personas': {'default': {'prompt': 'long brown hair, amber eyes', 'negative_prompt': 'glasses'}}},
                                  **patch))
        made.append((store, chat))
        return store, config, chat, recorder
    yield build
    for store, chat in made:
        asyncio.run(chat.close())
        store.close()


def offered(tools):
    return next((tool['function'] for tool in tools or [] if tool['function']['name'] == 'generate_image'), None)


async def over_qq(store, config, chat, text, scope=PRIVATE, images=(), user='303'):
    """One message through the OneBot flow; returns the segments that went out."""
    from shiori.onebot import OneBotBridge
    bridge = OneBotBridge(store, config, chat)
    sent = []

    class Socket:
        async def send_json(self, action):
            sent.append(action)
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': len(sent)}})
    bridge._socket = Socket()
    try:
        await asyncio.wait_for(bridge._flow.handle(scope, user, text, list(images), {'at_bot': True}, direct=True), 10)
        if bridge._flow._image_jobs:
            await asyncio.wait_for(asyncio.gather(*list(bridge._flow._image_jobs)), 10)
    finally:
        await bridge.stop()
    return [action['params']['message'][0] for action in sent]


def test_the_tool_is_offered_only_where_a_picture_can_be_sent(make_chat):
    store, config, chat, _ = make_chat(Model())
    tool = offered(chat._request_tools(store.get_scope(PRIVATE), '303', '画只猫', {}))
    assert tool['parameters']['required'] == ['prompt'] and 'use_reference' not in tool['parameters']['properties']
    assert '按你自己的意愿决定' in tool['description']
    with_picture = offered(chat._request_tools(store.get_scope(PRIVATE), '303', '改一下', {'_turn_images': [data_url(png())]}))
    assert 'use_reference' in with_picture['parameters']['properties']
    assert chat._image_tool('web:default') is None
    store.upsert_scope({'id': PRIVATE, 'allow_tools': False})
    assert chat._request_tools(store.get_scope(PRIVATE), '303', '画只猫', {}) is None
    chat.imagegen.update({'enabled': False})
    store.upsert_scope({'id': PRIVATE, 'allow_tools': True})
    assert offered(chat._request_tools(store.get_scope(PRIVATE), '303', '画只猫', {})) is None


@pytest.mark.anyio
@pytest.mark.parametrize('scope', [PRIVATE, GROUP])
async def test_asked_for_a_picture_the_text_goes_first_and_the_picture_follows(make_chat, scope):
    model = Model(answer('好呀，等我一下～', [draw(prompt='an orange cat on a windowsill', aspect='landscape')]))
    store, config, chat, recorder = make_chat(model)
    segments = await over_qq(store, config, chat, '给我画只猫', scope)
    assert [segment['type'] for segment in segments] == ['text', 'image']
    assert segments[0]['data']['text'] == '好呀，等我一下～' and segments[1]['data']['file'].startswith('base64://')
    assert len(model.seen) == 1                                   # the text came with the call
    assert 'generate_image' in model.seen[0]['system'] and offered(model.seen[0]['tools'])
    body = json.loads(recorder.requests[-1].content)
    assert body['prompt'] == 'an orange cat on a windowsill' and body['size'] == '1536x1024'
    assert chat.imagegen.remaining('qq:303') == 2
    kinds = [event['kind'] for event in store.list_events(limit=30)]
    assert 'imagegen' in kinds and 'imagegen_sent' in kinds


@pytest.mark.anyio
async def test_a_selfie_uses_the_persona_look(make_chat):
    model = Model(answer('拍好啦。', [draw(prompt='a selfie in a cafe', aspect='portrait', self_portrait=True)]))
    store, config, chat, recorder = make_chat(model)
    await over_qq(store, config, chat, '发张自拍')
    prompt = json.loads(recorder.requests[-1].content)['prompt']
    assert prompt.startswith('a selfie in a cafe') and 'long brown hair, amber eyes' in prompt


@pytest.mark.anyio
async def test_a_picture_in_the_message_is_used_as_the_reference(make_chat):
    model = Model(answer('我试试。', [draw(prompt='turn it into a night scene', use_reference=True)]))
    store, config, chat, recorder = make_chat(model)
    segments = await over_qq(store, config, chat, '把这张改成夜景', images=[data_url(png('green'))])
    assert [segment['type'] for segment in segments] == ['text', 'image']
    request = recorder.requests[-1]
    assert str(request.url).endswith('/images/edits') and b'turn it into a night scene' in request.content


@pytest.mark.anyio
async def test_over_the_daily_count_the_model_is_told_and_nothing_is_drawn(make_chat):
    model = Model(answer('', [draw(prompt='a cat')]), answer('今天画不了啦，明天再来吧。'))
    store, config, chat, recorder = make_chat(model, daily_per_user=0)
    segments = await over_qq(store, config, chat, '画只猫')
    assert [segment['type'] for segment in segments] == ['text'] and not recorder.requests
    assert segments[0]['data']['text'] == '今天画不了啦，明天再来吧。'


@pytest.mark.anyio
async def test_an_administrator_is_not_counted(make_chat):
    model = Model(answer('画好了就发你。', [draw(prompt='a cat')]))
    store, config, chat, recorder = make_chat(model, daily_per_user=0)

    class Access:
        def is_admin(self, identity):
            return True
    chat.access = Access()
    segments = await over_qq(store, config, chat, '画只猫')
    assert [segment['type'] for segment in segments] == ['text', 'image']


@pytest.mark.anyio
async def test_a_failed_picture_is_reported_briefly_and_refunded(make_chat):
    model = Model(answer('等我画。', [draw(prompt='a cat')]))
    store, config, chat, _ = make_chat(model, lambda request: httpx.Response(503, json={'error': {'message': 'busy'}}))
    segments = await over_qq(store, config, chat, '画只猫')
    assert [segment['type'] for segment in segments] == ['text', 'text']
    assert '没画出来' in segments[1]['data']['text'] and '次数已经退回' in segments[1]['data']['text']
    assert KEY not in segments[1]['data']['text'] and 'HTTP' not in segments[1]['data']['text']
    assert chat.imagegen.remaining('qq:303') == 3


@pytest.mark.anyio
async def test_a_merged_group_turn_does_not_draw(make_chat):
    model = Model(answer('一起聊。'))
    store, config, chat, _ = make_chat(model)
    entries = []
    for user, text in (('303', '画只猫'), ('505', '我也要')):
        message = store.add_message(GROUP, user, 'user', text)
        entries.append({'message': message, 'text': text, 'images': []})
    await chat.respond_group(GROUP, entries)
    assert not offered(model.seen[0]['tools'])


def test_only_successful_generate_image_actions_are_taken():
    job = {'id': 'abc', 'remaining_today': 1}
    assert requested_images({'actions': [{'tool': 'generate_image', 'ok': True, 'result': {'image_job': job}}]}) == [job]
    assert requested_images({'actions': [{'tool': 'generate_image', 'ok': False, 'error': '用完了'}]}) == []
    assert requested_images({'actions': [{'tool': 'remember', 'ok': True, 'result': {'image_job': job}}]}) == []
    assert requested_images({'reply': '好'}) == []


@pytest.mark.anyio
async def test_the_official_bot_uploads_the_picture_then_sends_it(tmp_path):
    from types import SimpleNamespace
    from shiori.official import OfficialBridge
    from shiori.settings import Settings
    store, cfg = Store(tmp_path), Settings(tmp_path)
    cfg.update({'onebot': {'adapter': 'official'},
                'qq_official': {'enabled': True, 'app_id': '1234567890', 'app_secret': 'isolated-fixture-secret'}})
    scope = 'qqofficial:group:group-openid'
    store.upsert_scope({'id': scope})
    calls = []

    class HTTP:
        async def request(self, route, json):
            calls.append(('upload', json))
            return {'file_info': 'fixture-file-info'}

    class API:
        _http = HTTP()

        async def post_group_message(self, **kwargs):
            calls.append(('send', kwargs))
            return {'id': 'fixture-sent'}
    bridge = OfficialBridge(store, cfg, SimpleNamespace())
    bridge._running = bridge._connected = True
    bridge._client = SimpleNamespace(api=API())
    picture = png('teal', (300, 200))
    assert await bridge._emit(scope, '', {'platform_message_id': 'incoming', 'output_part': {'index': 3}, 'image': picture})
    assert calls[0][0] == 'upload' and calls[0][1]['file_type'] == 1 and calls[0][1]['srv_send_msg'] is False
    assert base64.b64decode(calls[0][1]['file_data']) == picture       # small pictures go as they are
    assert calls[1][1]['msg_type'] == 7 and calls[1][1]['msg_seq'] == 3 and calls[1][1]['media'] == {'file_info': 'fixture-file-info'}
    # A chat that stopped allowing the picture during the upload gets nothing.
    assert not await bridge._emit(scope, '', {'image': picture, 'image_current': lambda: False})
    assert len(calls) == 2
    store.close()


@pytest.mark.anyio
async def test_a_picture_call_without_any_text_draws_nothing_and_keeps_the_count(make_chat):
    model = Model(answer('', [draw(prompt='a cat')]), answer(''), answer(''), answer(''))
    store, config, chat, recorder = make_chat(model)
    segments = await over_qq(store, config, chat, '画只猫')
    assert all(segment['type'] != 'image' for segment in segments) and not recorder.requests
    texts = [segment['data'].get('text', '') for segment in segments if segment['type'] == 'text']
    assert not any('generate_image' in text or '已执行' in text for text in texts)
    assert chat.imagegen.remaining('qq:303') == 3 and not chat.imagegen._jobs
