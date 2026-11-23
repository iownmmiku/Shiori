"""The bundled image generation extension (owner request, 2026-11-18).

Asked to draw, take a photo, a selfie or edit a picture, the chat model may
call generate_image. The text reply goes out first; the picture is drawn in
the background and sent on its own. Services: OpenAI-compatible image APIs,
Gemini, NovelAI and ComfyUI (adapters follow the owner's AstrBot plugin
随想画卷). A persona's appearance prompt is added to self portraits, and a
picture attached to the message can be used as the reference. A persona may
also have up to three reference pictures of her own (uploaded on the settings
page, owner request 2026-11-18) that go along with her self portraits. She has
a default outfit, and the outfit she changes into in a chat is remembered for
her later self portraits there (owner request 2026-11-19: changing clothes did
not work, the default clothes and her reference won). When someone sends a
picture, her look and reference pictures go to the chat model too, so she
knows herself in it (owner request 2026-11-23). Each QQ
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

from shiori import outgoing_image
from shiori.extensions.imagegen import engine as ig
from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.social import requested_jobs

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

    def build(handler=openai_ok, daily_per_user=5):
        store = Store(tmp_path / f'gen{len(stores)}')
        stores.append(store)
        recorder = Recorder(handler)
        options = {'daily_per_user': daily_per_user}       # the extension setting
        gen = ig.ImageGen(store, Config(), store.data_dir / 'imagegen', options=lambda: options, transport=recorder.transport())
        gen.options_values = options
        gen.update({'services': [{'id': 'main', 'kind': 'openai', 'api_key': KEY, 'model': 'gpt-image-1'}], 'service_id': 'main'})
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
    again = ig.ImageGen(store, Config(), store.data_dir / 'imagegen')
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


def test_drawing_needs_a_selected_service(make_gen):
    gen, _, _ = make_gen()
    with pytest.raises(ValueError, match='不存在'):
        gen.update({'service_id': 'missing'})
    gen.update({'service_id': ''})
    assert not gen.available(PRIVATE)
    with pytest.raises(ig.ImageGenError, match='还没有选择'):
        gen.queue(PRIVATE, 'qq:303', prompt='a cat')


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
    assert len(big.getvalue()) > outgoing_image.OFFICIAL_MAX_BYTES
    official = outgoing_image.official_image(big.getvalue())
    assert len(official) <= outgoing_image.OFFICIAL_MAX_BYTES and ig.image_info(official)[1] == 'image/jpeg'
    small = png()
    assert outgoing_image.onebot_image(small) == small           # sent as-is when it fits
    webp = io.BytesIO()
    Image.new('RGB', (32, 32), 'red').save(webp, format='WEBP')
    assert ig.image_info(outgoing_image.onebot_image(webp.getvalue()))[1] == 'image/jpeg'


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
    gen.options_values['daily_per_user'] = 0
    with pytest.raises(ig.ImageGenError, match='只对管理员开放'):
        gen.queue(PRIVATE, 'qq:606', prompt='a cat')
    # The counts survive a restart and start over the next day.
    options = {'daily_per_user': 2}
    again = ig.ImageGen(store, Config(), store.data_dir / 'imagegen', options=lambda: options)
    assert again.remaining('qq:303') == 0
    again._usage['day'] = '2000-01-01'
    assert again.remaining('qq:303') == 2


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
    with pytest.raises(ig.ImageGenError, match='还没有选择'):
        gen.queue('web:default', 'qq:303', prompt='a cat')
    for index in range(ig.QUEUE_LIMIT):
        gen.queue(GROUP, f'qq:{index}', prompt='a cat')
    with pytest.raises(ig.ImageGenError, match='太多'):
        gen.queue(GROUP, 'qq:999', prompt='a cat')
    assert gen.available(PRIVATE) and not gen.available('web:default')


@pytest.mark.anyio
async def test_the_management_test_uses_no_count(make_gen):
    gen, recorder, _ = make_gen(daily_per_user=1)
    result = await gen.test('a cat', aspect='landscape')
    assert result['image'].startswith('data:image/png;base64,') and gen.remaining('qq:303') == 1
    assert json.loads(recorder.requests[-1].content)['size'] == '1536x1024'


# ---------- persona reference pictures ----------

def jpeg(color='red', size=(64, 64)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, format='JPEG')
    return out.getvalue()


def edit_images(request):
    """(field name, file bytes) of each picture in a multipart /images/edits request."""
    found, content = [], request.content
    boundary = request.headers['Content-Type'].split('boundary=')[1].encode()
    for part in content.split(b'--' + boundary):
        head, _, body = part.partition(b'\r\n\r\n')
        if b'filename=' in head:
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            found.append((name, body.rstrip(b'\r\n')))
    return found


def test_a_persona_keeps_up_to_three_reference_pictures(make_gen):
    gen, _, _ = make_gen()
    big = io.BytesIO()
    Image.new('RGB', (3000, 2000), 'teal').save(big, format='WEBP')
    state = gen.add_reference('default', big.getvalue())
    [item] = state['personas']['default']['references']
    assert item['thumb'].startswith('data:image/jpeg;base64,')
    stored = gen.reference_dir / item['id']
    # A WebP becomes a JPEG no larger than 1536 pixels; a PNG stays a PNG.
    assert item['id'].endswith('.jpg') and max(ig.image_info(stored.read_bytes())[2]) == 1536
    gen.add_reference('default', png('red'))
    state = gen.add_reference('default', jpeg('blue'))
    assert [x['id'][-3:] for x in state['personas']['default']['references']] == ['jpg', 'png', 'jpg']
    with pytest.raises(ig.ImageGenError, match='最多 3 张'):
        gen.add_reference('default', png('green'))
    with pytest.raises(ig.ImageGenError, match='人格不存在'):
        gen.add_reference('nobody', png('green'))
    with pytest.raises(ig.ImageGenError, match='损坏或无法识别'):
        gen.add_reference('default', b'not a picture')
    assert len(list(gen.reference_dir.glob('*.thumb.jpg'))) == 3


def test_saving_the_looks_keeps_the_pictures_and_removing_one_deletes_its_files(make_gen):
    gen, _, store = make_gen()
    first = gen.add_reference('default', png('red'))['personas']['default']['references'][0]['id']
    gen.add_reference('default', png('blue'))
    # The page sends only the text fields; the pictures stay.
    state = gen.update({'personas': {'default': {'prompt': 'short silver hair', 'negative_prompt': ''}}})
    assert state['personas']['default']['prompt'] == 'short silver hair' and len(state['personas']['default']['references']) == 2
    state = gen.update({'personas': {}})
    assert len(state['personas']['default']['references']) == 2 and not state['personas']['default']['prompt']
    with pytest.raises(ValueError, match='格式无效'):
        gen.update({'personas': {'default': {'prompt': '', 'references': ['0123456789abcdef.png']}}})
    state = gen.remove_reference('default', first)
    assert [x['id'] for x in state['personas']['default']['references']] != [first]
    assert not (gen.reference_dir / first).exists() and not (gen.reference_dir / (first[:-4] + '.thumb.jpg')).exists()
    with pytest.raises(ig.ImageGenError, match='已经不在'):
        gen.remove_reference('default', first)
    # A picture deleted by hand from the folder is dropped on the next start.
    remaining = state['personas']['default']['references'][0]['id']
    (gen.reference_dir / remaining).unlink()
    again = ig.ImageGen(store, Config(), gen.root, options=gen.options)
    assert 'default' not in again.public()['personas'] and not again.warning


def test_each_service_kind_has_its_own_default_for_persona_pictures():
    assert service('openai')['persona_reference'] and service('gemini')['persona_reference']
    assert not service('novelai')['persona_reference']
    assert service('novelai', persona_reference=True)['persona_reference']
    with pytest.raises(ValueError, match='布尔值'):
        service('openai', persona_reference='yes')


def test_the_model_is_told_which_pictures_show_the_character():
    natural, tags = service('openai'), service('novelai')
    prompt, _ = ig.compose(natural, 'a selfie at the beach', persona_images=2)
    assert prompt.endswith("rather than copying that reference.") and 'The last 2 reference images show' in prompt
    prompt, _ = ig.compose(natural, 'wear this dress', user_images=1, persona_images=1)
    assert 'The first reference image is from the request. The last reference image shows' in prompt
    assert ig.compose(tags, '1girl, beach', persona_images=1)[0] == '1girl, beach'


@pytest.mark.anyio
async def test_a_self_portrait_sends_the_persona_pictures_after_the_requesters(make_gen):
    gen, recorder, _ = make_gen()
    gen.update({'personas': {'default': {'prompt': 'long brown hair', 'negative_prompt': ''}}})
    for color in ('red', 'blue'):
        gen.add_reference('default', png(color))
    stored = [(gen.reference_dir / x['id']).read_bytes() for x in gen.public()['personas']['default']['references']]
    job = gen.queue(PRIVATE, 'qq:303', prompt='a selfie at the beach', self_portrait=True)
    assert job['self_portrait'] and not job['reference']
    await gen.run(job['id'])
    request = recorder.requests[-1]
    assert str(request.url).endswith('/images/edits')
    assert [body for _, body in edit_images(request)] == stored
    assert b'The last 2 reference images show the character' in request.content
    event = next(item for item in gen.store.list_events(20) if item['kind'] == 'imagegen')
    assert event['details']['persona_reference'] == 2 and event['details']['reference'] == 0
    # Three pictures from the requester leave room for one of hers.
    given = [jpeg('green'), jpeg('yellow'), jpeg('white')]
    job = gen.queue(PRIVATE, 'qq:303', prompt='wear these', self_portrait=True, references=given)
    await gen.run(job['id'])
    assert [body for _, body in edit_images(recorder.requests[-1])] == given[:2] + stored[:1]
    # Not a self portrait: only the requester's picture.
    job = gen.queue(PRIVATE, 'qq:303', prompt='make it night', references=given[:1])
    await gen.run(job['id'])
    assert [body for _, body in edit_images(recorder.requests[-1])] == given[:1]
    assert b'reference images show' not in recorder.requests[-1].content
    # Without a reference picture the plain generation endpoint is used.
    gen.update({'services': [{'id': 'main', 'kind': 'openai', 'model': 'gpt-image-1', 'persona_reference': False}]})
    job = gen.queue(PRIVATE, 'qq:303', prompt='a selfie at the beach', self_portrait=True)
    await gen.run(job['id'])
    assert str(recorder.requests[-1].url).endswith('/images/generations')


@pytest.mark.anyio
async def test_a_single_picture_service_prefers_the_requesters_picture(make_gen):
    def novelai_ok(request):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('image_0.png', png('purple'))
        return httpx.Response(200, content=archive.getvalue(), headers={'Content-Type': 'application/zip'})
    gen, recorder, _ = make_gen(novelai_ok)
    gen.update({'services': [{'id': 'main', 'kind': 'novelai', 'api_key': KEY, 'persona_reference': True}]})
    gen.add_reference('default', png('red', (832, 1216)))
    job = gen.queue(PRIVATE, 'qq:303', prompt='1girl, selfie', aspect='portrait', self_portrait=True)
    await gen.run(job['id'])
    body = json.loads(recorder.requests[-1].content)
    assert body['action'] == 'img2img'
    assert Image.open(io.BytesIO(base64.b64decode(body['parameters']['image']))).getpixel((5, 5))[:3] == (255, 0, 0)
    job = gen.queue(PRIVATE, 'qq:303', prompt='1girl, wear this', self_portrait=True, references=[png('green', (832, 1216))])
    await gen.run(job['id'])
    body = json.loads(recorder.requests[-1].content)
    assert Image.open(io.BytesIO(base64.b64decode(body['parameters']['image']))).getpixel((5, 5))[:3] == (0, 128, 0)


@pytest.mark.anyio
async def test_the_management_test_can_draw_a_personas_self_portrait(make_gen):
    gen, recorder, _ = make_gen()
    gen.update({'personas': {'default': {'prompt': 'long brown hair', 'negative_prompt': ''}}})
    gen.add_reference('default', png('red'))
    result = await gen.test('a selfie in a cafe', persona_id='default')
    assert result['persona_references'] == 1 and str(recorder.requests[-1].url).endswith('/images/edits')
    assert b'long brown hair' in recorder.requests[-1].content
    result = await gen.test('a cat')
    assert result['persona_references'] == 0 and str(recorder.requests[-1].url).endswith('/images/generations')


@pytest.mark.anyio
async def test_the_page_routes_upload_and_remove_pictures(make_chat):
    store, config, chat, _ = make_chat(Model())
    from shiori.extension_host import ExtensionRequest
    host = chat.extensions

    async def call(method, path, body):
        result = host.api_handler('imagegen', method, path)(ExtensionRequest(method, path, {}, body))
        return await result if asyncio.iscoroutine(result) else result
    state = await call('POST', 'reference', {'persona_id': 'default', 'image': data_url(png('red'))})
    [item] = state['personas']['default']['references']
    with pytest.raises(ValueError, match='格式不受支持'):
        await call('POST', 'reference', {'persona_id': 'default', 'image': 'data:text/plain;base64,aGk='})
    with pytest.raises(ValueError, match='参数无效'):
        await call('POST', 'reference', {'persona_id': 'default'})
    with pytest.raises(ValueError, match='参数无效'):
        await call('POST', 'test', {'prompt': 'x', 'persona_id': 3})
    state = await call('DELETE', 'reference', {'persona_id': 'default', 'id': item['id']})
    assert state['personas']['default']['references'] == []


# ---------- outfits ----------

LOOK = {'prompt': 'long brown hair, amber eyes', 'negative_prompt': '', 'outfit': 'white sailor school uniform'}


def test_the_outfit_replaces_the_clothes_of_the_reference():
    natural, tags = service('openai'), service('novelai')
    prompt, _ = ig.compose(natural, 'a selfie in the bedroom', appearance='long brown hair', outfit='black maid outfit',
                           persona_images=1)
    assert 'Character appearance (keep it consistent): long brown hair\n\nOutfit in this picture: black maid outfit' in prompt
    assert 'dress her in the outfit above, not the clothes in that reference' in prompt
    assert 'sailor' not in prompt
    prompt, _ = ig.compose(natural, 'a selfie', appearance='long brown hair', persona_images=1)
    assert 'dress her' not in prompt and 'hair colour and eye colour' in prompt
    assert ig.compose(tags, '1girl, selfie', appearance='long brown hair', outfit='maid outfit')[0] == \
        'long brown hair, maid outfit, 1girl, selfie'


def test_a_self_portrait_wears_the_outfit_asked_for_else_the_one_she_changed_into_else_her_default(make_gen):
    gen, _, store = make_gen()
    gen.update({'personas': {'default': LOOK}})
    plan = lambda **extra: gen._plan(gen.service(), 'a selfie', persona_id='default', self_portrait=True, **extra)
    assert plan(scope_id=PRIVATE)[3]['outfit'] == 'white sailor school uniform'
    assert gen.set_outfit(PRIVATE, 'default', '  pink   pajamas ') == 'pink pajamas'
    assert plan(scope_id=PRIVATE)[3]['outfit'] == 'pink pajamas'
    assert plan(scope_id=GROUP)[3]['outfit'] == 'white sailor school uniform'      # each chat on its own
    assert plan(scope_id=PRIVATE, outfit='swimsuit')[3]['outfit'] == 'swimsuit'
    assert 'Outfit in this picture: pink pajamas' in plan(scope_id=PRIVATE)[0]
    # Not a self portrait: no clothes are added.
    assert gen._plan(gen.service(), 'a cat', persona_id='default', scope_id=PRIVATE)[3]['outfit'] == ''
    # Remembered across a restart, listed on the page, and forgotten when she changes back.
    again = ig.ImageGen(store, Config(), gen.root, options=gen.options)
    assert again.current_outfit(PRIVATE, 'default') == 'pink pajamas'
    [item] = again.public()['personas']['default']['outfits']
    assert item['scope_id'] == PRIVATE and item['outfit'] == 'pink pajamas' and item['updated'].endswith('Z')
    assert again.set_outfit(PRIVATE, 'default', 'white sailor school uniform') == 'white sailor school uniform'
    assert again.current_outfit(PRIVATE, 'default') == '' and again.public()['personas']['default']['outfits'] == []
    assert again.set_outfit(GROUP, 'default', '') == 'white sailor school uniform'


def test_the_looks_keep_a_default_outfit_and_the_remembered_ones_are_bounded(make_gen, monkeypatch):
    gen, _, _ = make_gen()
    state = gen.update({'personas': {'default': dict(LOOK, outfit=' white  sailor uniform ')}})
    assert state['personas']['default']['outfit'] == 'white sailor uniform'
    with pytest.raises(ValueError, match='默认服装'):
        gen.update({'personas': {'default': dict(LOOK, outfit='x' * 301)}})
    monkeypatch.setattr(ig, 'MAX_OUTFIT_ENTRIES', 3)
    for index in range(5):
        gen.set_outfit(f'qq:private:{index}', 'default', f'outfit {index}')
    assert [item['outfit'] for item in gen.outfits('default')] == ['outfit 4', 'outfit 3', 'outfit 2']
    # A persona with only a remembered outfit still shows on the page.
    gen.set_outfit(PRIVATE, 'other', 'raincoat')
    assert gen.public()['personas']['other']['outfits'][0]['outfit'] == 'raincoat'


@pytest.mark.anyio
async def test_the_management_test_wears_the_default_outfit(make_gen):
    gen, recorder, _ = make_gen()
    gen.update({'personas': {'default': LOOK}})
    gen.set_outfit(PRIVATE, 'default', 'pink pajamas')
    await gen.test('a selfie', persona_id='default')
    body = json.loads(recorder.requests[-1].content)
    assert 'Outfit in this picture: white sailor school uniform' in body['prompt'] and 'pajamas' not in body['prompt']


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
        self.seen.append({'system': messages[0]['content'], 'tools': kwargs.get('tools') or [], 'messages': list(messages)})
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
        engine = chat.extensions.context('imagegen').state['engine']
        chat.imagegen = engine                                    # shorthand for the assertions below
        engine.transport = recorder.transport()
        chat.extensions.set_config('imagegen', {'daily_per_user': patch.pop('daily_per_user', 3)})
        engine.update(dict({'services': [{'id': 'main', 'kind': 'openai', 'api_key': KEY, 'model': 'gpt-image-1'}],
                            'service_id': 'main',
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
        if bridge._flow._extension_jobs:
            await asyncio.wait_for(asyncio.gather(*list(bridge._flow._extension_jobs)), 10)
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
    assert offered(chat._extension_tools(store.get_scope('web:default'), 'owner', {})) is None
    store.upsert_scope({'id': PRIVATE, 'allow_tools': False})
    assert chat._request_tools(store.get_scope(PRIVATE), '303', '画只猫', {}) is None
    chat.extensions.set_enabled('imagegen', False)
    store.upsert_scope({'id': PRIVATE, 'allow_tools': True})
    assert offered(chat._request_tools(store.get_scope(PRIVATE), '303', '画只猫', {})) is None
    chat.extensions.set_enabled('imagegen', True)
    assert offered(chat._request_tools(store.get_scope(PRIVATE), '303', '画只猫', {}))


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


class Remembering(Model):
    """Also keeps every chat prompt, to read what a later turn was told."""

    def __init__(self, *turns):
        super().__init__(*turns)
        self.prompts = []

    async def complete(self, messages, **kwargs):
        if kwargs.get('purpose', 'chat') == 'chat':
            self.prompts.append('\n'.join(str(message['content']) for message in messages))
        return await super().complete(messages, **kwargs)


@pytest.mark.anyio
async def test_the_next_turn_knows_the_picture_was_sent(make_chat):
    # On the server she answered “好看” with “还没看到就说好看”: nothing told her the picture had arrived.
    model = Remembering(answer('行吧，就一张。', [draw(prompt='a selfie in a cafe', aspect='portrait', self_portrait=True,
                                                         outfit='black gothic lolita dress')]),
                        answer('哼，算你有眼光。'))
    store, config, chat, _ = make_chat(model)
    await over_qq(store, config, chat, '换上洛丽塔来张自拍')
    reply = next(m for m in store.list_messages(PRIVATE) if m['role'] == 'assistant')
    event = next(e for e in store.list_events(limit=30) if e['kind'] == 'reply_image_sent')
    assert event['details']['assistant_message_id'] == reply['id']
    assert model.prompts[0].count('【回复之后的图片】') == 0
    await over_qq(store, config, chat, '好看')
    told = model.prompts[1]
    assert '【回复之后的图片】' in told and '你说“行吧，就一张。”之后发出了：你的自拍；穿着 black gothic lolita dress；a selfie in a cafe' in told


@pytest.mark.anyio
async def test_a_picture_that_failed_is_announced_as_not_drawn(make_chat):
    model = Remembering(answer('等我一下。', [draw(prompt='an orange cat')]), answer('嗯。'))
    store, config, chat, _ = make_chat(model, handler=lambda request: httpx.Response(500, json={'error': {'message': 'down'}}))
    segments = await over_qq(store, config, chat, '画只猫')
    assert segments[-1]['data']['text'] == '这张图没画出来（生图服务出错了），次数已经退回，稍后再试吧。'
    await over_qq(store, config, chat, '好看吗')
    assert '你说“等我一下。”之后想画但没画出来（生图服务出错了）：你画的图；an orange cat' in model.prompts[1]
    assert '发出了' not in model.prompts[1]
    assert not [e for e in store.list_events(limit=30) if e['kind'] == 'reply_image_sent']


@pytest.mark.anyio
async def test_a_content_policy_refusal_is_named_and_remembered(make_chat):
    # On the server six refusals read “HTTP 400，请检查接口地址和参数”, and she kept asking again.
    refusal = {'error': {'message': '非常抱歉，生成的图片可能违反了我们的内容政策。如果你认为此判断有误，请重试或修改提示语。'}}
    model = Remembering(answer('好吧。', [draw(prompt='a swimsuit picture', self_portrait=True)]), answer('画不了。'))
    store, config, chat, _ = make_chat(model, handler=lambda request: httpx.Response(400, json=refusal))
    segments = await over_qq(store, config, chat, '来一张')
    assert segments[-1]['data']['text'] == '这张图没画出来（被生图服务的内容政策拒绝了），次数已经退回。'
    failed = next(e for e in store.list_events(limit=30) if e['kind'] == 'imagegen_failed')
    assert failed['message'] == '图片没有生成成功，已退回次数：生图服务按内容政策拒绝了这张图' and '内容政策' in failed['details']['detail']
    await over_qq(store, config, chat, '再来一张')
    assert '想画但没画出来（被生图服务的内容政策拒绝了）' in model.prompts[1] and '换个说法也会被拒' in model.prompts[1]


@pytest.mark.parametrize('status, body, refused', [
    (400, {'error': {'message': '抱歉，我无法生成涉及未成年高中生穿着带有性化意味的学校泳装的图像。'}}, True),
    (400, {'error': {'message': 'Your request was rejected by the safety system.', 'code': 'moderation_blocked'}}, True),
    (400, {'error': {'message': 'Invalid size: must be one of 1024x1024, 1024x1536'}}, False),
    (403, {'error': {'message': 'invalid api key'}}, False),
])
def test_refusals_are_told_apart_from_other_errors(status, body, refused):
    service = ig.clean_service({'id': 'main', 'kind': 'openai', 'api_key': KEY, 'model': 'gpt-image-1'})
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    with pytest.raises(ig.ImageGenError) as caught:
        asyncio.run(ig.generate(service, 'a cat', transport=transport))
    assert (str(caught.value) == ig.REFUSED) is refused


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
async def test_a_selfie_in_qq_carries_the_persona_pictures(make_chat):
    model = Model(answer('拍好啦。', [draw(prompt='a selfie in a cafe', aspect='portrait', self_portrait=True)]))
    store, config, chat, recorder = make_chat(model)
    chat.imagegen.add_reference('default', png('red'))
    segments = await over_qq(store, config, chat, '发张自拍')
    assert [segment['type'] for segment in segments] == ['text', 'image']
    request = recorder.requests[-1]
    assert str(request.url).endswith('/images/edits') and len(edit_images(request)) == 1
    assert b'long brown hair, amber eyes' in request.content


def call(name, call_id='call-2', **arguments):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def tool_named(tools, name):
    return next((tool['function'] for tool in tools or [] if tool['function']['name'] == name), None)


@pytest.mark.anyio
async def test_asked_to_change_clothes_for_a_photo_she_wears_them_and_keeps_them(make_chat):
    model = Model(answer('换好啦，拍给你看。', [draw(prompt='a selfie in the bedroom', self_portrait=True, outfit='black maid outfit')]),
                  answer('再来一张。', [draw(prompt='a selfie by the window', self_portrait=True)]))
    store, config, chat, recorder = make_chat(model, personas={'default': LOOK})
    tool = tool_named(model.seen and model.seen[0]['tools'] or chat._request_tools(store.get_scope(PRIVATE), '303', '', {}), 'generate_image')
    assert 'outfit' in tool['parameters']['properties']
    assert '你现在穿着平时的衣服：white sailor school uniform' in tool['parameters']['properties']['outfit']['description']
    segments = await over_qq(store, config, chat, '换上女仆装拍一张')
    assert [segment['type'] for segment in segments] == ['text', 'image']
    prompt = json.loads(recorder.requests[-1].content)['prompt']
    assert 'Outfit in this picture: black maid outfit' in prompt and 'sailor' not in prompt
    assert chat.imagegen.current_outfit(PRIVATE, 'default') == 'black maid outfit'
    # The next selfie keeps the maid outfit, and the model is told she wears it.
    await over_qq(store, config, chat, '再拍一张')
    description = tool_named(model.seen[-1]['tools'], 'generate_image')['parameters']['properties']['outfit']['description']
    assert '你现在穿着：black maid outfit（平时穿：white sailor school uniform）' in description
    assert 'Outfit in this picture: black maid outfit' in json.loads(recorder.requests[-1].content)['prompt']


@pytest.mark.anyio
async def test_changing_clothes_without_a_photo_is_remembered_after_the_reply(make_chat):
    model = Model(answer('好，换上睡衣了。', [call('change_outfit', outfit='pink pajamas')]),
                  answer('嗯，换回来了。', [call('change_outfit', back_to_default=True)]),
                  answer('', [call('change_outfit', outfit='raincoat')]), answer(''), answer(''), answer(''))
    store, config, chat, recorder = make_chat(model, personas={'default': LOOK})
    tool = tool_named(chat._request_tools(store.get_scope(PRIVATE), '303', '', {}), 'change_outfit')
    assert '换衣服但不拍照' in tool['description'] and 'back_to_default' in tool['parameters']['properties']
    segments = await over_qq(store, config, chat, '换上睡衣吧')
    assert [segment['type'] for segment in segments] == ['text'] and not recorder.requests
    assert chat.imagegen.current_outfit(PRIVATE, 'default') == 'pink pajamas'
    assert any(event['kind'] == 'imagegen_outfit' for event in store.list_events(limit=20))
    await over_qq(store, config, chat, '还是穿回校服吧')
    assert chat.imagegen.current_outfit(PRIVATE, 'default') == ''
    # A reply that never went out changes nothing.
    await over_qq(store, config, chat, '穿雨衣')
    assert chat.imagegen.current_outfit(PRIVATE, 'default') == ''


@pytest.mark.anyio
async def test_the_page_resets_a_remembered_outfit(make_chat):
    store, config, chat, _ = make_chat(Model(), personas={'default': LOOK})
    from shiori.extension_host import ExtensionRequest
    chat.imagegen.set_outfit(GROUP, 'default', 'raincoat')
    handler = chat.extensions.api_handler('imagegen', 'DELETE', 'outfit')
    state = handler(ExtensionRequest('DELETE', 'outfit', {}, {'persona_id': 'default', 'scope_id': GROUP}))
    assert state['personas']['default']['outfits'] == [] and chat.imagegen.current_outfit(GROUP, 'default') == ''
    with pytest.raises(ValueError, match='服装参数无效'):
        handler(ExtensionRequest('DELETE', 'outfit', {}, {'persona_id': 'default'}))


@pytest.mark.anyio
async def test_she_sees_her_own_look_when_someone_sends_a_picture(make_chat):
    model = Model(answer('这不就是我嘛。'), answer('在发呆。'), answer('不认识。'))
    store, config, chat, _ = make_chat(model, personas={'default': LOOK})
    chat.imagegen.add_reference('default', png('red', (600, 900)))
    chat.imagegen.set_outfit(PRIVATE, 'default', 'pink pajamas')
    await over_qq(store, config, chat, '这是谁？', images=[data_url(png('green'))])
    seen = model.seen[0]
    assert '【你自己的样子】外貌：long brown hair, amber eyes；现在穿着：pink pajamas。' in seen['system']
    assert '那就是你' in seen['system']
    content = seen['messages'][-1]['content']
    assert [part['type'] for part in content] == ['text', 'image_url', 'text', 'image_url']
    assert '这是谁' in content[0]['text'] and '系统附上的参考图' in content[2]['text']   # her picture after theirs
    assert '对方这条消息后面附了 1 张你自己的参考图' in seen['system']
    url = content[3]['image_url']['url']
    assert url.startswith('data:image/jpeg;base64,') and url != content[1]['image_url']['url']
    assert max(ig.image_info(base64.b64decode(url.split(',', 1)[1]))[2]) == 512
    # A turn without pictures carries nothing about her look.
    await over_qq(store, config, chat, '在干嘛')
    assert '你自己的样子' not in model.seen[1]['system'] and isinstance(model.seen[1]['messages'][-1]['content'], str)
    # Switched off in the extension settings: nothing either.
    chat.extensions.set_config('imagegen', {'recognize_self': False})
    await over_qq(store, config, chat, '这是谁？', images=[data_url(png('green'))])
    assert '你自己的样子' not in model.seen[2]['system'] and len(model.seen[2]['messages'][-1]['content']) == 2


def test_without_reference_pictures_only_her_look_is_described(make_gen):
    gen, _, _ = make_gen()
    assert gen.self_images('default') == []
    gen.add_reference('default', png('red', (2000, 1000)))
    [url] = gen.self_images('default')
    assert gen.self_images('default') == [url]                        # made once
    name = gen.appearance('default')['references'][0]
    gen.remove_reference('default', name)
    assert gen.self_images('default') == [] and name not in gen._self_cache


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


def test_only_successful_actions_hand_back_work():
    job = {'id': 'abc', 'extension': 'imagegen'}
    assert requested_jobs({'actions': [{'tool': 'generate_image', 'ok': True, 'result': {'extension_job': job}}]}) == [job]
    assert requested_jobs({'actions': [{'tool': 'generate_image', 'ok': False, 'error': '用完了'}]}) == []
    assert requested_jobs({'actions': [{'tool': 'remember', 'ok': True, 'result': {'image_job': job}}]}) == []
    assert requested_jobs({'reply': '好'}) == []


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
    assert chat.imagegen.remaining('qq:303') == 3 and not chat.imagegen._jobs and not chat.extensions._jobs
