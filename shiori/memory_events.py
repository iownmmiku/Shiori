"""Bounded event memories with searchable prose and a compact recall view.

The structure describes evidence; it never assigns identities or permissions.
Event dates remain separate and are validated against the original messages.
"""
from __future__ import annotations

EPISODE_LIMITS = {'title': 120, 'summary': 500, 'narrative': 3600,
                  'location': 200, 'outcome': 600}


def _text(value, field, limit, *, required=False):
    if not isinstance(value, str):
        raise ValueError(f'事件 {field} 必须为字符串')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f'事件 {field} 须为 {1 if required else 0}—{limit} 字')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
        raise ValueError(f'事件 {field} 不能含控制字符')
    return value


def normalize_episode(value):
    if not isinstance(value, dict) or set(value) - (set(EPISODE_LIMITS) | {'participants'}):
        raise ValueError('事件结构包含不支持的字段')
    result = {key: _text(value.get(key, ''), key, limit, required=key == 'narrative')
              for key, limit in EPISODE_LIMITS.items()}
    people = value.get('participants', [])
    if not isinstance(people, list) or len(people) > 8:
        raise ValueError('事件 participants 须为最多 8 项的称呼列表')
    result['participants'] = list(dict.fromkeys(
        _text(person, 'participants', 80, required=True) for person in people))
    if len(_render(result)) > 6000:
        raise ValueError('事件正文合计不能超过 6000 字')
    return result


def _render(episode):
    fields = [('事件', episode['title']), ('概述', episode['summary']),
              ('参与者（原文称呼）', '、'.join(episode['participants'])),
              ('地点', episode['location']), ('经过', episode['narrative']),
              ('结果 / 后续', episode['outcome'])]
    return '\n\n'.join(f'{label}：{text}' for label, text in fields if text)


def episode_content(value):
    """One canonical body keeps existing lexical/vector indexes authoritative."""
    return _render(normalize_episode(value))


def _clip(text, limit):
    text = str(text or '')
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    # Prefer an existing sentence boundary; never imply a partial excerpt is
    # the complete event. This is a prompt view, not a destructive storage cut.
    end = max(text.rfind(mark, limit // 2, limit - 1) for mark in '\n。！？；.!?')
    return text[:end + 1 if end >= 0 else limit - 1].rstrip() + '…'


def episode_prompt_text(record, excerpt='', *, limit=2200):
    """Retain event context around a matched chunk within the reference budget."""
    content = str(record.get('content') or '')
    episode = record.get('episode')
    if record.get('kind') != 'event' or not isinstance(episode, dict):
        return _clip(excerpt or content, limit)
    try:
        episode = normalize_episode(episode)
    except ValueError:
        return _clip(excerpt or content, limit)
    content = _render(episode)
    if len(content) <= limit:
        return content
    fields = [('事件', _clip(episode['title'], 120)),
              ('概述', _clip(episode['summary'], 300)),
              ('参与者（原文称呼）', _clip('、'.join(episode['participants']), 160)),
              ('地点', _clip(episode['location'], 120)),
              ('结果 / 后续', _clip(episode['outcome'], 300))]
    prefix = '\n'.join(f'{label}：{value}' for label, value in fields if value)
    suffix = '\n（此处为事件摘要与匹配片段，未展示全部经过。）'
    room = max(0, limit - len(prefix) - len(suffix) - len('\n相关经过：'))
    match = _clip(excerpt or episode['narrative'], room) if room else ''
    return _clip(prefix + ('\n相关经过：' + match if match else '') + suffix, limit)
