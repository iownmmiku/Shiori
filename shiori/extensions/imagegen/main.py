"""Entry point of the bundled image generation extension.

In QQ chats the chat model gets generate_image. The persona decides whether
to draw; the tool only reserves the picture, and the host runs `deliver`
after the text reply went out (or `cancel`, which gives the count back, when
it did not). change_outfit remembers what she changed into without a
picture; both tools take effect only once the reply went out.
"""
import asyncio

from shiori.extension_host import ToolResult
from .engine import (ASPECTS, MAX_OUTFIT, MAX_PROMPT, MAX_UPLOAD_BYTES, ImageGen, ImageGenError, chat_reason,
                     clean_outfit, decode_data_url)

HINT = ('你会画画和拍照，可以用 generate_image 生成图片，图要几十秒才画好，会在文字之后单独发出。'
        '对方请你画画、拍照、自拍或改图时，按你的性格和意愿决定，愿意就调用；不要只用文字描述画面假装已经发了图，'
        '调用后也不要说图已经画好或描述成品。'
        '对方让你换衣服时，按你的意愿决定：要拍照就在 generate_image 里填 outfit；只换衣服不拍照就调用 change_outfit。')


def wearing_text(wearing, default):
    if wearing:
        return f'你现在穿着：{wearing}' + (f'（平时穿：{default}）' if default else '') + '。'
    return f'你现在穿着平时的衣服：{default}。' if default else '你的衣服没有设定。'


def tool_schema(reference=False, wearing='', default=''):
    properties = {
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': MAX_PROMPT,
                   'description': '画面描述：主体、动作、表情、服装、场景、光线、画风和构图，越具体越好。'},
        'aspect': {'type': 'string', 'enum': list(ASPECTS),
                   'description': 'square 方图；portrait 竖图（人像、自拍）；landscape 横图（风景、场景）。'},
        'self_portrait': {'type': 'boolean',
                          'description': '画的是你自己（自拍、你的照片、你穿某件衣服）时为 true，会自动加上你的外貌设定、你现在穿的衣服和参考图，prompt 里不用再写长相。'},
        'outfit': {'type': 'string', 'minLength': 1, 'maxLength': MAX_OUTFIT,
                   'description': '画你自己时这张图里穿的衣服，只写衣服和配饰。对方要你换衣服、穿某件衣服，或者聊天里你已经换了衣服时填写，'
                                  '填了就是换上这身，之后的自拍也穿它；不填就穿你现在的衣服。' + wearing_text(wearing, default)}}
    if reference:
        properties['use_reference'] = {'type': 'boolean',
                                       'description': '对方这条消息附了图片，要你照着它改、换风格或照着画时为 true。'}
    return {'type': 'function', 'function': {
        'name': 'generate_image',
        'description': '画一张图或拍一张照片，图要几十秒才画好，会在文字回复之后单独发出。'
                       '对方请你画画、拍照、自拍、发照片或改图时，按你自己的意愿决定画不画；愿意才调用，对方没要求时不要主动画。',
        'parameters': {'type': 'object', 'properties': properties, 'required': ['prompt'], 'additionalProperties': False}}}


def outfit_schema(wearing='', default=''):
    return {'type': 'function', 'function': {
        'name': 'change_outfit',
        'description': '换衣服但不拍照：对方让你换衣服、或者你自己想换（换睡衣、换回平时的衣服等）时调用，记住你现在穿什么，之后的自拍会穿这身。'
                       '要拍照的话直接在 generate_image 里填 outfit，不用再调用这个。' + wearing_text(wearing, default),
        'parameters': {'type': 'object', 'properties': {
            'outfit': {'type': 'string', 'minLength': 1, 'maxLength': MAX_OUTFIT, 'description': '换上的衣服，只写衣服和配饰。'},
            'back_to_default': {'type': 'boolean', 'description': '换回平时的衣服时为 true，不用填 outfit。'}},
            'additionalProperties': False}}}


def setup(ext):
    engine = ImageGen(ext.store, ext.settings, ext.data_dir, options=ext.config)
    ext.state['engine'] = engine

    def offered(turn):
        # A picture is charged to one member, so merged group turns never get it.
        return not turn.group and engine.available(turn.scope_id)

    def clothes(turn):
        return engine.current_outfit(turn.scope_id, turn.persona_id), engine.appearance(turn.persona_id).get('outfit', '')

    def schema(turn):
        if not offered(turn):
            return None
        return tool_schema(bool(turn.images) and engine.reference_supported(), *clothes(turn))

    def handler(call):
        try:
            references = []
            if call.args.get('use_reference'):
                references = [decode_data_url(item) for item in call.images][:3]
                if not references:
                    raise ValueError('这条消息里没有可用的参考图，请对方重新发送图片')
            outfit = clean_outfit(call.args.get('outfit', ''))
            job = engine.queue(call.scope_id, call.user_key, admin=call.admin, persona_id=call.persona_id,
                               prompt=call.args['prompt'], aspect=call.args.get('aspect', 'square'),
                               self_portrait=call.args.get('self_portrait') is True, references=references, outfit=outfit)
        except ImageGenError as exc:
            raise ValueError(str(exc)) from None

        async def deliver(send):
            if outfit:
                # She said she would wear it; later self portraits keep it even if this picture fails.
                engine.set_outfit(call.scope_id, call.persona_id, outfit)
            try:
                raw, _ = await engine.run(job['id'])
            except ImageGenError as exc:
                await send.text(f'这张图没画出来（{chat_reason(exc)}），次数已经退回，稍后再试吧。')
                return
            sent = await send.image(raw)
            ext.store.add_event('imagegen_sent' if sent else 'imagegen_unsent',
                                '图片已发送' if sent else '图片已生成，但没有发出或没有得到送达确认',
                                scope_id=call.scope_id, bytes=len(raw))

        remaining = job['remaining_today']
        return ToolResult(
            output={'ok': True, 'status': 'queued', 'image_job': job,
                    'message': '图片已开始排队，大约几十秒后在你的文字回复之后单独发出。现在照常写回复，'
                               '不要说图已经画好，也不要描述成品。'
                               + (f'对方今天还能再画 {remaining} 张。' if remaining is not None else '')},
            after_reply=deliver, cancel=lambda: engine.discard(job['id']))

    ext.tool('generate_image', schema, handler, hint=HINT, queued=True)

    def change_schema(turn):
        return outfit_schema(*clothes(turn)) if offered(turn) else None

    def change(call):
        back = call.args.get('back_to_default') is True
        outfit = '' if back else clean_outfit(call.args.get('outfit', ''))
        if not back and not outfit:
            raise ValueError('请填写换上的衣服，或者把 back_to_default 设为 true')
        default = engine.appearance(call.persona_id).get('outfit', '')

        async def remember(send):
            worn = engine.set_outfit(call.scope_id, call.persona_id, outfit)
            ext.store.add_event('imagegen_outfit', '换了衣服：' + (worn or '平时的衣服'), scope_id=call.scope_id,
                                persona_id=call.persona_id)
        now = outfit or default or '平时的衣服'
        return ToolResult(output={'ok': True, 'status': 'queued', 'outfit': now,
                                  'message': f'回复发出后会记下你现在穿着：{now}。之后的自拍都穿这身。照常写回复。'},
                          after_reply=remember, cancel=lambda: None)

    ext.tool('change_outfit', change_schema, change, queued=True)

    ext.api('GET', 'state', lambda request: engine.public())
    ext.api('PUT', 'config', lambda request: engine.update(request.body))

    async def test(request):
        body = request.body if isinstance(request.body, dict) else {}
        if set(body) - {'prompt', 'service_id', 'aspect', 'persona_id'}:
            raise ValueError('测试参数无效')
        service_id, aspect, persona_id = body.get('service_id') or None, body.get('aspect', 'square'), body.get('persona_id') or None
        if (service_id is not None and not isinstance(service_id, str) or aspect not in ASPECTS
                or persona_id is not None and not isinstance(persona_id, str)):
            raise ValueError('测试参数无效')
        try:
            return await engine.test(body.get('prompt', ''), service_id=service_id, aspect=aspect, persona_id=persona_id)
        except ImageGenError as exc:
            # The owner sees the reason; the redacted upstream excerpt helps fix the configuration.
            raise ValueError(str(exc) + (f'（接口说明：{exc.detail}）' if exc.detail else '')) from None

    ext.api('POST', 'test', test)

    def body_of(request, keys, error='参考图参数无效'):
        body = request.body if isinstance(request.body, dict) else {}
        if set(body) != keys or not all(isinstance(body[key], str) for key in keys):
            raise ValueError(error)
        return body

    async def add_reference(request):
        body = body_of(request, {'persona_id', 'image'})
        try:
            return await asyncio.to_thread(
                lambda: engine.add_reference(body['persona_id'], decode_data_url(body['image'], limit=MAX_UPLOAD_BYTES)))
        except ImageGenError as exc:
            raise ValueError(str(exc)) from None

    def remove_reference(request):
        body = body_of(request, {'persona_id', 'id'})
        try:
            return engine.remove_reference(body['persona_id'], body['id'])
        except ImageGenError as exc:
            raise ValueError(str(exc)) from None

    ext.api('POST', 'reference', add_reference)
    ext.api('DELETE', 'reference', remove_reference)

    def forget_outfit(request):
        body = body_of(request, {'persona_id', 'scope_id'}, '服装参数无效')
        engine.set_outfit(body['scope_id'], body['persona_id'], '')
        return engine.public()

    ext.api('DELETE', 'outfit', forget_outfit)
