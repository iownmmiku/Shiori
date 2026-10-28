"""Bounded original messages collected by a local WebUI before one reply."""
from .chat import validate_images
from .timezones import ingress_times


def chat_input(payload, *, max_images=3):
    if 'messages' not in payload:
        return None
    entries = payload['messages']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
        raise ValueError('一次最多合并8条本地消息')
    if payload.get('mode', 'chat') != 'chat':
        raise ValueError('电脑任务请单独提交，不与聊天消息合并')
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'text', 'images', 'attachments', 'sent_at'}:
            raise ValueError('本地合并消息格式无效')
        text = entry.get('text', '')
        if not isinstance(text, str):
            raise ValueError('本地消息正文须为文本')
        text = text.strip()
        images = validate_images(entry.get('images', []))
        attachments = entry.get('attachments', [])
        if not isinstance(attachments, list) or any(not isinstance(item, str) for item in attachments):
            raise ValueError('本地消息附件格式无效')
        if not text and not images:
            raise ValueError('合并消息中存在空消息')
        result.append({'text': text, 'images': images, 'attachments': attachments,
                       **ingress_times(entry.get('sent_at'))})
    text = '\n'.join(item['text'] for item in result).strip()
    images = [image for item in result for image in item['images']]
    attachments = list(dict.fromkeys(ident for item in result for ident in item['attachments']))
    if len(text) > 12000 or len(images) > max_images or len(attachments) > 5:
        raise ValueError('合并后最多12000字、当前设置允许的图片数和5份附件，请分次发送')
    # There is one authoritative input representation, not a hidden conflicting
    # top-level command alongside the displayed originals.
    for key, expected in (('text', text), ('images', images), ('attachments', attachments)):
        if key in payload and payload[key] != expected:
            raise ValueError('合并正文、图片或附件与原消息不一致')
    return {'messages': result, 'text': text, 'images': images, 'attachments': attachments}
