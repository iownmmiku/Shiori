"""Entry point of the bundled image generation extension.

In QQ chats the chat model gets generate_image. The persona decides whether
to draw; the tool only reserves the picture, and the host runs `deliver`
after the text reply went out (or `cancel`, which gives the count back, when
it did not).
"""
from ...extension_host import ToolResult
from .engine import ASPECTS, MAX_PROMPT, ImageGen, ImageGenError, chat_reason, decode_data_url

HINT = ('你会画画和拍照，可以用 generate_image 生成图片，图要几十秒才画好，会在文字之后单独发出。'
        '对方请你画画、拍照、自拍或改图时，按你的性格和意愿决定，愿意就调用；不要只用文字描述画面假装已经发了图，'
        '调用后也不要说图已经画好或描述成品。')


def tool_schema(reference=False):
    properties = {
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': MAX_PROMPT,
                   'description': '画面描述：主体、动作、表情、服装、场景、光线、画风和构图，越具体越好。'},
        'aspect': {'type': 'string', 'enum': list(ASPECTS),
                   'description': 'square 方图；portrait 竖图（人像、自拍）；landscape 横图（风景、场景）。'},
        'self_portrait': {'type': 'boolean',
                          'description': '画的是你自己（自拍、你的照片、你穿某件衣服）时为 true，会自动加上你的外貌设定，prompt 里不用再写长相。'}}
    if reference:
        properties['use_reference'] = {'type': 'boolean',
                                       'description': '对方这条消息附了图片，要你照着它改、换风格或照着画时为 true。'}
    return {'type': 'function', 'function': {
        'name': 'generate_image',
        'description': '画一张图或拍一张照片，图要几十秒才画好，会在文字回复之后单独发出。'
                       '对方请你画画、拍照、自拍、发照片或改图时，按你自己的意愿决定画不画；愿意才调用，对方没要求时不要主动画。',
        'parameters': {'type': 'object', 'properties': properties, 'required': ['prompt'], 'additionalProperties': False}}}


def setup(ext):
    engine = ImageGen(ext.store, ext.settings, ext.data_dir, options=ext.config)
    ext.state['engine'] = engine

    def schema(turn):
        # A picture is charged to one member, so merged group turns never get it.
        if turn.group or not engine.available(turn.scope_id):
            return None
        return tool_schema(reference=bool(turn.images) and engine.reference_supported())

    def handler(call):
        try:
            references = []
            if call.args.get('use_reference'):
                references = [decode_data_url(item) for item in call.images][:3]
                if not references:
                    raise ValueError('这条消息里没有可用的参考图，请对方重新发送图片')
            job = engine.queue(call.scope_id, call.user_key, admin=call.admin, persona_id=call.persona_id,
                               prompt=call.args['prompt'], aspect=call.args.get('aspect', 'square'),
                               self_portrait=call.args.get('self_portrait') is True, references=references)
        except ImageGenError as exc:
            raise ValueError(str(exc)) from None

        async def deliver(send):
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

    ext.api('GET', 'state', lambda request: engine.public())
    ext.api('PUT', 'config', lambda request: engine.update(request.body))

    async def test(request):
        body = request.body if isinstance(request.body, dict) else {}
        if set(body) - {'prompt', 'service_id', 'aspect'}:
            raise ValueError('测试参数无效')
        service_id, aspect = body.get('service_id') or None, body.get('aspect', 'square')
        if service_id is not None and not isinstance(service_id, str) or aspect not in ASPECTS:
            raise ValueError('测试参数无效')
        try:
            return await engine.test(body.get('prompt', ''), service_id=service_id, aspect=aspect)
        except ImageGenError as exc:
            # The owner sees the reason; the redacted upstream excerpt helps fix the configuration.
            raise ValueError(str(exc) + (f'（接口说明：{exc.detail}）' if exc.detail else '')) from None

    ext.api('POST', 'test', test)
