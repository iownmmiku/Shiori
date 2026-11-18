"""The image generation extension's engine: services, daily counts, queue.

The chat model queues a picture with the `generate_image` tool; it is drawn
in the background and sent after the text reply (see main.py). The adapters
for OpenAI-compatible image APIs, Gemini, NovelAI and ComfyUI follow the
owner's AstrBot plugin 随想画卷 (persona_canvas).

Prompts, reference pictures and upstream bodies are untrusted data. Service
keys are stored protected like the other settings secrets and never leave
this module: errors keep a redacted upstream excerpt for the event log only.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx

from shiori import outgoing_image
from shiori.settings import _protect, _unprotect
from shiori.timezones import local_now

KINDS = {'openai': 'OpenAI 兼容图像接口', 'gemini': 'Gemini 图像模型', 'novelai': 'NovelAI', 'comfyui': 'ComfyUI 工作流'}
DEFAULT_ENDPOINTS = {'openai': 'https://api.openai.com/v1', 'gemini': 'https://generativelanguage.googleapis.com/v1beta',
                     'novelai': 'https://image.novelai.net', 'comfyui': 'http://127.0.0.1:8188'}
DEFAULT_MODELS = {'openai': 'gpt-image-1', 'gemini': '', 'novelai': 'nai-diffusion-4-5-full', 'comfyui': ''}
ASPECTS = ('square', 'portrait', 'landscape')
PIXELS = {'square': (1024, 1024), 'portrait': (832, 1216), 'landscape': (1216, 832)}
GEMINI_RATIOS = {'square': '1:1', 'portrait': '3:4', 'landscape': '4:3'}
TAG_KINDS = ('novelai', 'comfyui')   # prompts are usually comma separated tags
SCOPES = ('qq:', 'qqofficial:')      # chats whose connector can send a picture
QUALITIES = ('', 'auto', 'low', 'medium', 'high', 'standard', 'hd')
WORKFLOW_FIELDS = ('prompt', 'negative_prompt', 'reference', 'width', 'height', 'seed')
MAX_PROMPT = 1500
MAX_PERSONA_PROMPT = 2000
MAX_SERVICES = 8
MAX_PERSONAS = 200
MAX_REFERENCES = 3
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_IMAGE_BYTES = outgoing_image.MAX_IMAGE_BYTES
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
MAX_EXTRA_CHARS = 8000
QUEUE_LIMIT = 6          # pictures waiting or being drawn
PENDING_TTL = 600.0      # a queued picture whose reply never went out is dropped

# Switching the extension on and the daily count are extension settings
# (extension.json); the services and the persona looks are kept here.
CONFIG_DEFAULTS = {'service_id': '', 'services': [], 'personas': {}}
SERVICE_DEFAULTS = {'id': '', 'name': '', 'kind': 'openai', 'endpoint': '', 'api_key': '', 'model': '',
                    'timeout_seconds': 180, 'negative_prompt': '', 'natural_negative': False, 'reference': True,
                    'quality': '', 'steps': 28, 'scale': 5.0, 'sampler': 'k_euler_ancestral', 'strength': 0.6,
                    'extra_body': {}, 'workflow': {}, 'workflow_fields': {}, 'output_node': ''}
_SERVICE_INPUT = frozenset(SERVICE_DEFAULTS) | {'api_key_set', 'clear_api_key'}


class ImageGenError(Exception):
    """A failure whose message is shown to the owner and the chat model;
    `detail` is a redacted upstream excerpt meant for the event log only."""

    def __init__(self, message, *, detail=''):
        super().__init__(message)
        self.detail = detail


def chat_reason(exc):
    """A short reason fit for the chat; configuration details stay in the log."""
    text = str(exc)
    if '超时' in text or '没有完成' in text:
        return '画得太久，超时了'
    if '安全策略' in text:
        return '被生图服务的安全策略拦下了'
    if '限流' in text or '额度' in text:
        return '生图服务现在太忙或额度不够'
    if '停用' in text or '过期' in text:
        return '生图功能刚被停用了'
    return '生图服务出错了'


# ---------- pictures ----------

def image_info(data):
    """(extension, mime, (width, height)) from the actual pixels."""
    try:
        return outgoing_image.image_info(data)
    except ValueError as exc:
        raise ImageGenError('返回的' + str(exc) if str(exc).startswith('图片损坏') else str(exc)) from exc


def decode_data_url(value):
    """Bytes of a base64 data:image URL (the chat turn's prepared pictures)."""
    prefix, separator, payload = str(value).partition(',')
    if not separator or not re.fullmatch(r'data:image/(png|jpeg|webp|gif);base64', prefix):
        raise ImageGenError('参考图格式不受支持')
    if len(payload) > 4 * ((MAX_REFERENCE_BYTES + 2) // 3):
        raise ImageGenError('参考图超过 5 MB')
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ImageGenError('参考图编码无效') from exc
    image_info(raw)
    return raw


def _b64_image(value):
    value = str(value)
    if value.startswith('data:'):
        header, separator, value = value.partition(',')
        if not separator or ';base64' not in header.lower():
            raise ImageGenError('接口返回了不支持的图片地址')
    encoded = re.sub(r'\s+', '', value)
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ImageGenError('接口返回的图片超过 32 MB')
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ImageGenError('接口返回的图片不是有效 Base64') from exc


# ---------- configuration ----------

def _text(value, label, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ValueError(f'{label}须为不超过 {limit} 个字符的文字')
    value = value.strip()
    if required and not value:
        raise ValueError(f'请填写{label}')
    return value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}须为 {low}—{high} 的整数')
    return value


def _number(value, label, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label}须为 {low}—{high} 之间的数')
    return float(value)


def validate_endpoint(value):
    value = str(value).strip().rstrip('/')
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname:
        raise ValueError('接口地址须为有效的 http:// 或 https:// 地址')
    if url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError('接口地址不能包含账号、密码或查询参数；密钥请填在密钥栏')
    try:
        url.port
    except ValueError as exc:
        raise ValueError('接口端口无效') from exc
    return value


def _workflow(value):
    if not isinstance(value, dict) or not value or 'nodes' in value or 'links' in value:
        raise ValueError('ComfyUI 工作流须为「导出 API」格式的 JSON 对象，不是画布 JSON')
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('ComfyUI 工作流须为有效 JSON') from exc
    if len(encoded.encode()) > MAX_WORKFLOW_BYTES or len(value) > 2000:
        raise ValueError('ComfyUI 工作流超过 2 MB 或 2000 个节点')
    for key, node in value.items():
        if (not isinstance(key, str) or not key or not isinstance(node, dict)
                or not isinstance(node.get('class_type'), str) or not isinstance(node.get('inputs'), dict)):
            raise ValueError('ComfyUI 工作流的每个节点都要有 class_type 和 inputs')
    return json.loads(encoded)


def _workflow_fields(fields, workflow):
    if not isinstance(fields, dict) or set(fields) - set(WORKFLOW_FIELDS):
        raise ValueError('ComfyUI 节点映射只能包含 ' + '、'.join(WORKFLOW_FIELDS))
    result = {}
    for key, path in fields.items():
        if path in ('', None):
            continue
        match = re.fullmatch(r'([^.\s]+)\.inputs\.([^.\s]+)', str(path)) if isinstance(path, str) else None
        if not match:
            raise ValueError(f'ComfyUI 映射「{key}」须写成 节点ID.inputs.输入名')
        node = workflow.get(match[1])
        if not isinstance(node, dict) or match[2] not in node['inputs']:
            raise ValueError(f'ComfyUI 映射「{key}」指向不存在的节点输入')
        current = node['inputs'][match[2]]
        if isinstance(current, (list, dict)) or current is None:
            raise ValueError(f'ComfyUI 映射「{key}」不能覆盖节点之间的连线')
        if key in ('prompt', 'negative_prompt', 'reference') and not isinstance(current, str):
            raise ValueError(f'ComfyUI 映射「{key}」须指向文字输入')
        if key in ('width', 'height', 'seed') and (isinstance(current, bool) or not isinstance(current, (int, float))):
            raise ValueError(f'ComfyUI 映射「{key}」须指向数字输入')
        if path in result.values():
            raise ValueError('ComfyUI 两个映射不能指向同一个输入')
        result[key] = path
    if 'prompt' not in result:
        raise ValueError('ComfyUI 至少要把「prompt」映射到正面提示词输入')
    return result


def clean_service(item, previous=None):
    """One validated service; an empty key keeps the saved one."""
    if not isinstance(item, dict) or set(item) - _SERVICE_INPUT:
        raise ValueError('生图服务配置包含不支持的字段')
    previous = previous or {}
    kind = item.get('kind', previous.get('kind', 'openai'))
    if kind not in KINDS:
        raise ValueError('生图服务类型须为 openai、gemini、novelai 或 comfyui')
    service = dict(SERVICE_DEFAULTS, kind=kind)
    for key in SERVICE_DEFAULTS:
        if key in item:
            service[key] = item[key]
        elif key in previous and previous.get('kind') == kind:
            service[key] = previous[key]
    key = service['id'] or 'img_' + uuid.uuid4().hex[:10]
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', key):
        raise ValueError('生图服务 ID 无效')
    service['id'] = key
    service['name'] = _text(service['name'], '服务名称', 60) or KINDS[kind]
    service['endpoint'] = validate_endpoint(service['endpoint'] or DEFAULT_ENDPOINTS[kind])
    api_key = item.get('api_key', '')
    if not isinstance(api_key, str) or len(api_key) > 4000 or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        raise ValueError('API 密钥格式无效')
    service['api_key'] = '' if item.get('clear_api_key') is True else (api_key.strip() or previous.get('api_key', ''))
    service['model'] = _text(service['model'], '模型名称', 200) or DEFAULT_MODELS[kind]
    if kind in ('openai', 'gemini') and not service['model']:
        raise ValueError('请填写绘画模型名称')
    service['timeout_seconds'] = _integer(service['timeout_seconds'], '等待上限', 5, 600)
    service['negative_prompt'] = _text(service['negative_prompt'], '负面提示词', MAX_PERSONA_PROMPT)
    for flag in ('natural_negative', 'reference'):
        if type(service[flag]) is not bool:
            raise ValueError('开关须为布尔值')
    if service['quality'] not in QUALITIES:
        raise ValueError('画质须为 auto、low、medium、high、standard 或 hd')
    service['steps'] = _integer(service['steps'], '采样步数', 1, 100)
    service['scale'] = _number(service['scale'], '提示词引导值', 0, 100)
    if not isinstance(service['sampler'], str) or not re.fullmatch(r'[a-z0-9_+]{1,60}', service['sampler']):
        raise ValueError('采样器名称无效')
    service['strength'] = _number(service['strength'], '改图强度', 0, 1)
    extra = service['extra_body']
    if not isinstance(extra, dict):
        raise ValueError('附加请求参数须为 JSON 对象')
    try:
        if len(json.dumps(extra, ensure_ascii=False, allow_nan=False)) > MAX_EXTRA_CHARS:
            raise ValueError('附加请求参数超过 8000 个字符')
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc) if '8000' in str(exc) else '附加请求参数须为有效 JSON') from exc
    if kind == 'comfyui':
        if extra:
            raise ValueError('ComfyUI 请直接修改工作流，不支持附加请求参数')
        service['workflow'] = _workflow(service['workflow'])
        service['workflow_fields'] = _workflow_fields(service['workflow_fields'], service['workflow'])
        node = service['output_node']
        if not isinstance(node, str) or node and node not in service['workflow']:
            raise ValueError('输出节点须为空或工作流里存在的节点 ID')
    else:
        service.update(workflow={}, workflow_fields={}, output_node='')
    return service


def clean_personas(value):
    if not isinstance(value, dict) or len(value) > MAX_PERSONAS:
        raise ValueError('人格外貌设定格式无效')
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 80 or not isinstance(item, dict) or set(item) - {'prompt', 'negative_prompt'}:
            raise ValueError('人格外貌设定格式无效')
        entry = {'prompt': _text(item.get('prompt', ''), '外貌提示词', MAX_PERSONA_PROMPT),
                 'negative_prompt': _text(item.get('negative_prompt', ''), '外貌负面提示词', MAX_PERSONA_PROMPT)}
        if entry['prompt'] or entry['negative_prompt']:
            result[key] = entry
    return result


def supports_reference(service):
    if not service:
        return False
    if service['kind'] == 'comfyui':
        return bool(service['workflow_fields'].get('reference'))
    if service['kind'] == 'openai' and service['model'].lower().startswith('dall-e-3'):
        return False
    return bool(service['reference'])


def compose(service, prompt, *, appearance='', appearance_negative=''):
    """(prompt, negative) for one picture; the persona's look is added to a self portrait."""
    prompt = str(prompt).strip()
    if appearance:
        prompt = (f'{appearance}, {prompt}' if service['kind'] in TAG_KINDS
                  else f'{prompt}\n\nCharacter appearance (keep it consistent): {appearance}')
    negative = ', '.join(part for part in (service['negative_prompt'], appearance_negative) if part)
    if negative and service['natural_negative'] and service['kind'] in ('openai', 'gemini'):
        prompt += f'\n\nAvoid: {negative}'
    return prompt, negative


# ---------- transport ----------

def _secrets(service):
    found = {service.get('api_key') or ''}
    return sorted((value for value in found if len(value) >= 4), key=len, reverse=True)


def redact(service, text):
    value = str(text)
    for secret in _secrets(service):
        value = value.replace(secret, '[REDACTED]').replace(quote(secret, safe=''), '[REDACTED]')
    value = re.sub(r'(?i)(bearer\s+)[^\s"\'<>]+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)(api[_-]?key|token|authorization|x-goog-api-key)(["\'\s:=]+)[^\s,}"\']+', r'\1\2[REDACTED]', value)
    return value[:300]


def _detail(service, raw):
    text = ''
    try:
        payload = json.loads(raw[:65536].decode('utf-8', 'replace'))
    except ValueError:
        payload = None
        text = raw[:300].decode('utf-8', 'replace')
    if isinstance(payload, dict):
        error = payload.get('error')
        for candidate in (error.get('message') if isinstance(error, dict) else error, payload.get('message'), payload.get('detail')):
            if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool) and str(candidate).strip():
                text = str(candidate)
                break
    return redact(service, re.sub(r'\s+', ' ', text).strip())


def _url(service, route):
    return service['endpoint'].rstrip('/') + '/' + route.lstrip('/')


def _headers(service, *, json_body=True):
    headers = {'Accept': 'application/json', 'User-Agent': 'shiori'}
    if json_body:
        headers['Content-Type'] = 'application/json'
    if service['api_key']:
        if service['kind'] == 'gemini':
            headers['x-goog-api-key'] = service['api_key']
        else:
            headers['Authorization'] = 'Bearer ' + service['api_key']
    return headers


async def _send(http, service, method, url, *, limit=MAX_RESPONSE_BYTES, **kwargs):
    """(body, content type) of a successful response read under `limit`."""
    try:
        async with http.stream(method, url, **kwargs) as response:
            declared = response.headers.get('Content-Length', '')
            if declared.isdigit() and int(declared) > limit:
                raise ImageGenError('接口响应超过大小上限')
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ImageGenError('接口响应超过大小上限')
                chunks.append(chunk)
            raw = b''.join(chunks)
            if not 200 <= response.status_code < 300:
                hint = ('请检查密钥、权限和模型名称' if response.status_code in (401, 403)
                        else '接口限流或额度不足，请稍后再试' if response.status_code in (402, 429)
                        else '请检查接口地址和参数')
                raise ImageGenError(f'生图接口返回 HTTP {response.status_code}，{hint}', detail=_detail(service, raw))
            return raw, response.headers.get('Content-Type', '')
    except ImageGenError:
        raise
    except httpx.TimeoutException as exc:
        raise ImageGenError('生图接口响应超时') from exc
    except httpx.HTTPError as exc:
        raise ImageGenError('无法连接生图接口，请检查地址和网络', detail=redact(service, f'{type(exc).__name__}: {exc}')) from exc


def _json(service, raw):
    try:
        payload = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ImageGenError('生图接口没有返回有效 JSON 或图片') from exc
    if isinstance(payload, dict) and payload.get('error'):
        raise ImageGenError('生图接口返回了错误，请检查配置和额度', detail=_detail(service, raw))
    return payload


def _merge_extra(body, extra, protected):
    def merge(target, patch, prefix=''):
        for key, value in patch.items():
            path = f'{prefix}.{key}' if prefix else str(key)
            if any(path == item or path.startswith(item + '.') or item.startswith(path + '.') and not isinstance(value, dict)
                   for item in protected):
                raise ImageGenError(f'附加请求参数不能覆盖 {path}')
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value, path)
            else:
                target[key] = copy.deepcopy(value)
    result = copy.deepcopy(body)
    merge(result, extra or {})
    return result


async def _image_value(value):
    """Bytes of a b64 field, data URL or public https image URL."""
    if not isinstance(value, str) or not value.strip():
        raise ImageGenError('生图接口的响应里没有图片')
    if value.startswith(('https://', 'http://')):
        from shiori.stickers import download_image
        try:
            raw, _ = await asyncio.to_thread(download_image, value, max_bytes=5 * 1024 * 1024)
        except Exception as exc:
            raise ImageGenError('生图接口返回了图片链接，但下载失败（只支持 5 MB 以内的公网图片）') from exc
        return raw
    return await asyncio.to_thread(_b64_image, value)


def _openai_size(model, aspect):
    model = model.lower()
    if model.startswith('dall-e-3'):
        return {'square': '1024x1024', 'portrait': '1024x1792', 'landscape': '1792x1024'}[aspect]
    if model.startswith('dall-e-2'):
        return '1024x1024'
    return {'square': '1024x1024', 'portrait': '1024x1536', 'landscape': '1536x1024'}[aspect]


async def _openai(http, service, prompt, negative, aspect, references):
    model = service['model']
    gpt_image = model.lower().startswith(('gpt-image', 'chatgpt-image'))
    body = {'model': model, 'prompt': prompt, 'n': 1, 'size': _openai_size(model, aspect)}
    if not gpt_image:
        body['response_format'] = 'b64_json'
    if service['quality']:
        body['quality'] = service['quality']
    body = _merge_extra(body, service['extra_body'], {'model', 'prompt', 'n', 'image', 'image[]', 'mask', 'stream'})
    if references:
        files = []
        for index, data in enumerate(references):
            extension, mime, _ = image_info(data)
            if extension not in ('png', 'jpg', 'webp'):
                raise ImageGenError('这个接口改图只支持 PNG、JPEG、WebP 参考图')
            files.append(('image[]' if len(references) > 1 else 'image', (f'reference-{index}.{extension}', bytes(data), mime)))
        form = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value)
                for key, value in body.items()}
        raw, content_type = await _send(http, service, 'POST', _url(service, 'images/edits'),
                                        headers=_headers(service, json_body=False), data=form, files=files)
    else:
        raw, content_type = await _send(http, service, 'POST', _url(service, 'images/generations'),
                                        headers=_headers(service), json=body)
    if content_type.lower().startswith('image/'):
        return raw
    payload = _json(service, raw)
    data = payload.get('data') if isinstance(payload, dict) else None
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    return await _image_value(first.get('b64_json') or first.get('url'))


async def _gemini(http, service, prompt, negative, aspect, references):
    model = service['model'].removeprefix('models/')
    parts = [{'text': prompt}]
    for data in references:
        _, mime, _ = image_info(data)
        parts.append({'inlineData': {'mimeType': mime, 'data': base64.b64encode(data).decode('ascii')}})
    body = {'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'responseModalities': ['TEXT', 'IMAGE'], 'imageConfig': {'aspectRatio': GEMINI_RATIOS[aspect]}}}
    body = _merge_extra(body, service['extra_body'], {'contents', 'generationConfig.responseModalities'})
    raw, _ = await _send(http, service, 'POST', _url(service, f'models/{quote(model, safe="-._")}:generateContent'),
                         headers=_headers(service), json=body)
    payload = _json(service, raw)
    candidates = payload.get('candidates') if isinstance(payload, dict) else None
    candidates = candidates if isinstance(candidates, list) else []
    for candidate in candidates:
        content = candidate.get('content') if isinstance(candidate, dict) else None
        parts = content.get('parts') if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict) or part.get('thought'):
                continue
            inline = part.get('inlineData', part.get('inline_data'))
            if isinstance(inline, dict) and inline.get('data'):
                return await asyncio.to_thread(_b64_image, inline['data'])
    blocked = (payload.get('promptFeedback') or {}).get('blockReason') if isinstance(payload, dict) else None
    reason = blocked or next((c.get('finishReason') for c in candidates if isinstance(c, dict) and c.get('finishReason')), '')
    raise ImageGenError('Gemini 没有返回图片' + ('，可能被安全策略拦下了' if reason not in ('', 'STOP') else '；请确认模型支持图片输出'),
                        detail=redact(service, str(reason or '')))


def _fit_reference(data, size):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as picture:
        fitted = ImageOps.fit(picture.convert('RGB'), size)
    output = io.BytesIO()
    fitted.save(output, format='PNG')
    return output.getvalue()


def _nearest_aspect(data):
    _, _, (width, height) = image_info(data)
    return min(ASPECTS, key=lambda key: abs(math.log((PIXELS[key][0] / PIXELS[key][1]) / (width / height))))


def _zip_image(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for entry in archive.infolist()[:32]:
                if entry.is_dir() or not entry.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    continue
                if entry.file_size > MAX_IMAGE_BYTES:
                    raise ImageGenError('压缩包里的图片超过 32 MB')
                with archive.open(entry) as handle:
                    return handle.read(MAX_IMAGE_BYTES + 1)
    except ImageGenError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ImageGenError('接口返回了无效的图片压缩包') from exc
    raise ImageGenError('接口返回的压缩包里没有图片')


async def _novelai(http, service, prompt, negative, aspect, references):
    model = service['model'] or DEFAULT_MODELS['novelai']
    if references:
        aspect = _nearest_aspect(references[0])
    width, height = PIXELS[aspect]
    seed = secrets.randbelow(2 ** 32)
    params = {'width': width, 'height': height, 'steps': service['steps'], 'scale': service['scale'],
              'sampler': service['sampler'], 'n_samples': 1, 'negative_prompt': negative, 'seed': seed}
    if 'nai-diffusion-4' in model or 'nai-diffusion-5' in model:
        params.update(params_version=3,
                      v4_prompt={'caption': {'base_caption': prompt, 'char_captions': []}, 'use_coords': False, 'use_order': True},
                      v4_negative_prompt={'caption': {'base_caption': negative, 'char_captions': []}, 'legacy_uc': False})
    if references:
        reference = await asyncio.to_thread(_fit_reference, references[0], (width, height))
        params.update(image=base64.b64encode(reference).decode('ascii'), strength=service['strength'],
                      noise=0, extra_noise_seed=seed)
    body = _merge_extra({'input': prompt, 'model': model, 'action': 'img2img' if references else 'generate',
                         'parameters': params},
                        service['extra_body'], {'input', 'model', 'action', 'parameters.image', 'parameters.n_samples'})
    raw, content_type = await _send(http, service, 'POST', _url(service, 'ai/generate-image'),
                                    headers=_headers(service), json=body)
    if raw.startswith(b'PK\x03\x04'):
        return await asyncio.to_thread(_zip_image, raw)
    if content_type.lower().startswith('image/'):
        return raw
    _json(service, raw)
    raise ImageGenError('NovelAI 没有返回图片')


def _path_set(workflow, path, value):
    node, _, name = path.partition('.inputs.')
    workflow[node]['inputs'][name] = value


def _comfy_file(value, *, upload=False):
    if not isinstance(value, dict):
        raise ImageGenError('ComfyUI 返回的文件信息无效')
    name, folder = value.get('name' if upload else 'filename'), value.get('subfolder', '')
    kind = value.get('type', 'input' if upload else 'output')
    if (not isinstance(name, str) or not name or len(name) > 255 or any(char in name for char in '/\\\0:') or '..' in name
            or not isinstance(folder, str) or len(folder) > 255 or any(char in folder for char in '\\\0:')
            or folder.startswith('/') or '..' in folder.split('/')
            or kind not in (('input',) if upload else ('output', 'temp'))):
        raise ImageGenError('ComfyUI 返回的文件信息无效')
    return name, folder, kind


def _comfy_json(service, raw, operation):
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ImageGenError(f'ComfyUI {operation}没有返回有效 JSON') from exc
    if not isinstance(value, dict):
        raise ImageGenError(f'ComfyUI {operation}的响应格式不对')
    if value.get('error') or value.get('node_errors'):
        raise ImageGenError(f'ComfyUI {operation}失败，请检查工作流节点、模型和映射', detail=_detail(service, raw))
    return value


def _comfy_output(service, history):
    status = history.get('status') or {}
    if not isinstance(status, dict):
        raise ImageGenError('ComfyUI 任务状态格式不对')
    messages = status.get('messages') if isinstance(status.get('messages'), list) else []
    if status.get('status_str') in ('error', 'failed', 'interrupted') or any(
            isinstance(item, (list, tuple)) and item and item[0] in ('execution_error', 'execution_interrupted') for item in messages):
        raise ImageGenError('ComfyUI 工作流执行失败或被中断，请查看 ComfyUI 日志')
    outputs = history.get('outputs') if isinstance(history.get('outputs'), dict) else {}
    node = service['output_node']
    for output in ([outputs.get(node)] if node else list(outputs.values())):
        images = output.get('images') if isinstance(output, dict) else None
        if isinstance(images, list) and images:
            return images[0]
    if status.get('completed') is True or status.get('status_str') == 'success':
        raise ImageGenError('ComfyUI 已执行完但没有图片输出，请检查保存图片节点和输出节点设置')
    return None


async def _comfyui(http, service, prompt, negative, aspect, references, *, poll=0.5, max_poll=2.0):
    workflow = copy.deepcopy(service['workflow'])
    fields = service['workflow_fields']
    _path_set(workflow, fields['prompt'], prompt)
    if fields.get('negative_prompt'):
        _path_set(workflow, fields['negative_prompt'], negative)
    width, height = PIXELS[_nearest_aspect(references[0]) if references else aspect]
    for key, value in (('width', width), ('height', height), ('seed', secrets.randbits(63))):
        if fields.get(key):
            _path_set(workflow, fields[key], value)
    headers = _headers(service)
    prompt_id = ''
    try:
        if references and fields.get('reference'):
            extension, mime, _ = image_info(references[0])
            raw, _ = await _send(http, service, 'POST', _url(service, 'upload/image'), limit=1024 * 1024,
                                 headers=_headers(service, json_body=False),
                                 data={'type': 'input', 'subfolder': 'shiori', 'overwrite': 'false'},
                                 files=[('image', (f'reference.{extension}', bytes(references[0]), mime))])
            name, folder, _ = _comfy_file(_comfy_json(service, raw, '上传参考图'), upload=True)
            _path_set(workflow, fields['reference'], f'{folder}/{name}' if folder else name)
        raw, _ = await _send(http, service, 'POST', _url(service, 'prompt'), limit=1024 * 1024, headers=headers,
                             json={'prompt': workflow, 'client_id': 'shiori-' + secrets.token_hex(8)})
        prompt_id = _comfy_json(service, raw, '提交工作流').get('prompt_id')
        if not isinstance(prompt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', prompt_id):
            prompt_id = ''
            raise ImageGenError('ComfyUI 提交后没有返回任务编号')
        delay = poll
        while True:
            raw, _ = await _send(http, service, 'GET', _url(service, 'history/' + quote(prompt_id, safe='')),
                                 limit=8 * 1024 * 1024, headers=headers)
            history = _comfy_json(service, raw, '读取任务').get(prompt_id)
            image = _comfy_output(service, history) if isinstance(history, dict) else None
            if image is not None:
                name, folder, kind = _comfy_file(image)
                query = urlencode({'filename': name, 'subfolder': folder, 'type': kind})
                raw, _ = await _send(http, service, 'GET', _url(service, 'view') + '?' + query, limit=MAX_IMAGE_BYTES,
                                     headers=_headers(service, json_body=False))
                return raw
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, max_poll)
    except BaseException:
        if prompt_id:
            # Only this queued prompt is removed; a running one may finish anyway.
            try:
                await asyncio.wait_for(http.post(_url(service, 'queue'), headers=headers, json={'delete': [prompt_id]}), 2)
            except BaseException:
                pass
        raise


ADAPTERS = {'openai': _openai, 'gemini': _gemini, 'novelai': _novelai, 'comfyui': _comfyui}


async def generate(service, prompt, negative='', aspect='square', references=(), *, transport=None):
    """Bytes and MIME type of one picture from `service`."""
    if aspect not in ASPECTS:
        aspect = 'square'
    references = [bytes(item) for item in references or []][:MAX_REFERENCES]
    if references and not supports_reference(service):
        raise ImageGenError('当前生图服务不支持参考图改图')
    if service['kind'] in ('novelai', 'comfyui'):
        references = references[:1]
    timeout = service['timeout_seconds']
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(15, timeout)), follow_redirects=False,
                                 transport=transport) as http:
        try:
            raw = await asyncio.wait_for(ADAPTERS[service['kind']](http, service, prompt, negative, aspect, references), timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ImageGenError(f'生图超过 {timeout} 秒没有完成') from exc
    _, mime, _ = await asyncio.to_thread(image_info, raw)
    return raw, mime


# ---------- service ----------

@dataclass
class Job:
    id: str
    scope_id: str
    user_key: str
    admin: bool
    service_id: str
    prompt: str
    negative: str
    aspect: str
    request: str
    references: list = field(default_factory=list)
    day: str = ''
    counted: bool = False
    created: float = field(default_factory=time.monotonic)


class ImageGen:
    """Configuration, per-user daily counts and the background drawing queue."""

    def __init__(self, store, settings, root, *, options=None, transport=None):
        self.store, self.settings = store, settings
        self.options = options or (lambda: {'daily_per_user': 5})   # the extension's settings
        self.transport = transport          # tests replace the HTTP transport
        self.root = Path(root)
        self.path = self.root / 'config.json'
        self.usage_path = self.root / 'usage.json'
        self._lock = threading.RLock()
        self._jobs = {}
        self._running = 0
        self._slot = None
        self.warning = ''
        self._config = self._load()
        self._usage = self._load_usage()

    # -- storage --
    def _load(self):
        config = copy.deepcopy(CONFIG_DEFAULTS)
        if not self.path.exists():
            return config
        try:
            raw = json.loads(self.path.read_text('utf-8-sig'))
            if not isinstance(raw, dict):
                raise ValueError('配置须为对象')
            services = []
            for item in raw.get('services') or []:
                stored = dict(item)
                stored['api_key'] = _unprotect(stored.get('api_key', ''))
                if item.get('api_key') and not stored['api_key']:
                    raise ValueError('密钥不能在当前账户读取')
                services.append(clean_service(stored))
            config.update(service_id=str(raw.get('service_id', '')), services=services[:MAX_SERVICES],
                          personas=clean_personas(raw.get('personas') or {}))
        except Exception as exc:
            self.warning = '生图配置无法读取，已停用；请在生图设置页重新配置'
            config = copy.deepcopy(CONFIG_DEFAULTS)
            try:
                self.store.add_exception('imagegen_config_error', self.warning, exc)
            except Exception:
                pass
        return config

    def _save(self, config):
        self.root.mkdir(parents=True, exist_ok=True)
        stored = copy.deepcopy(config)
        for item in stored['services']:
            item['api_key'] = _protect(item['api_key'])
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2), 'utf-8')
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.path)

    def _day(self):
        return local_now(self.settings).date().isoformat()

    def _load_usage(self):
        try:
            raw = json.loads(self.usage_path.read_text('utf-8'))
            counts = raw.get('counts') if isinstance(raw, dict) else None
            if isinstance(raw.get('day'), str) and isinstance(counts, dict):
                return {'day': raw['day'], 'counts': {str(k): int(v) for k, v in counts.items() if type(v) is int and v > 0}}
        except (OSError, ValueError, AttributeError):
            pass
        return {'day': '', 'counts': {}}

    def _counts(self):
        day = self._day()
        if self._usage['day'] != day:
            self._usage = {'day': day, 'counts': {}}
        return day, self._usage['counts']

    def _save_usage(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.usage_path.with_name(self.usage_path.name + '.tmp')
        temporary.write_text(json.dumps(self._usage, ensure_ascii=False), 'utf-8')
        temporary.replace(self.usage_path)

    def daily_limit(self):
        try:
            value = self.options().get('daily_per_user', 5)
        except Exception:
            value = 5
        return value if type(value) is int and value >= 0 else 5

    # -- configuration --
    def config(self):
        with self._lock:
            return copy.deepcopy(self._config)

    def public(self):
        config = self.config()
        for item in config['services']:
            item['api_key_set'] = bool(item.pop('api_key', ''))
            item['supports_reference'] = supports_reference(item)
        with self._lock:
            day, counts = self._counts()
            usage = {'day': day, 'counts': dict(counts), 'queued': len(self._jobs), 'running': self._running}
        personas = []
        try:
            personas = [{'id': item['id'], 'name': item.get('name') or item['id']} for item in self.store.list_personas()]
        except Exception:
            pass
        return dict(config, kinds=KINDS, default_endpoints=DEFAULT_ENDPOINTS, usage=usage, daily_per_user=self.daily_limit(),
                    persona_list=personas, warning=self.warning)

    def update(self, patch):
        if not isinstance(patch, dict) or set(patch) - set(CONFIG_DEFAULTS):
            raise ValueError('生图设置包含不支持的字段')
        with self._lock:
            config = self.config()
            if 'services' in patch:
                items = patch['services']
                if not isinstance(items, list) or len(items) > MAX_SERVICES:
                    raise ValueError(f'最多配置 {MAX_SERVICES} 个生图服务')
                previous = {item['id']: item for item in config['services']}
                services, seen = [], set()
                for item in items:
                    key = item.get('id') if isinstance(item, dict) else None
                    service = clean_service(item, previous.get(key))
                    if service['id'] in seen:
                        raise ValueError('生图服务 ID 重复')
                    seen.add(service['id'])
                    services.append(service)
                config['services'] = services
            if 'service_id' in patch:
                if not isinstance(patch['service_id'], str):
                    raise ValueError('当前服务无效')
                config['service_id'] = patch['service_id']
            if config['service_id'] and config['service_id'] not in {item['id'] for item in config['services']}:
                raise ValueError('选择的生图服务不存在')
            if 'personas' in patch:
                config['personas'] = clean_personas(patch['personas'])
            self._save(config)
            self._config = config
            self.warning = ''
        return self.public()

    def service(self, service_id=None):
        with self._lock:
            key = self._config['service_id'] if service_id is None else service_id
            found = next((item for item in self._config['services'] if item['id'] == key), None)
            return copy.deepcopy(found)

    def available(self, scope_id):
        with self._lock:
            return bool(scope_id.startswith(SCOPES) and self.service())

    def reference_supported(self):
        return supports_reference(self.service())

    def appearance(self, persona_id):
        with self._lock:
            return dict(self._config['personas'].get(persona_id) or {})

    def remaining(self, user_key, admin=False):
        """Pictures left today for this user; None means unlimited."""
        if admin:
            return None
        with self._lock:
            _, counts = self._counts()
            return max(0, self.daily_limit() - counts.get(user_key, 0))

    # -- queue --
    def _refund(self, job):
        if job.counted:
            day, counts = self._counts()
            if job.day == day and counts.get(job.user_key, 0) > 0:
                counts[job.user_key] -= 1
                if not counts[job.user_key]:
                    counts.pop(job.user_key)
                self._save_usage()
            job.counted = False

    def _sweep(self):
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if now - job.created > PENDING_TTL:
                self._jobs.pop(key)
                self._refund(job)

    def queue(self, scope_id, user_key, *, admin=False, persona_id='default', prompt='', aspect='square',
              self_portrait=False, references=()):
        """Reserve one of today's pictures and hold the request until the
        reply that announced it has gone out; returns the job summary."""
        prompt = str(prompt).strip()
        if not prompt or len(prompt) > MAX_PROMPT:
            raise ImageGenError(f'画面描述须为 1—{MAX_PROMPT} 个字')
        with self._lock:
            self._sweep()
            service = self.service()
            if not service or not scope_id.startswith(SCOPES):
                raise ImageGenError('还没有选择生图服务')
            if references and not supports_reference(service):
                raise ImageGenError('当前生图服务不支持参考图改图')
            if len(self._jobs) + self._running >= QUEUE_LIMIT:
                raise ImageGenError('现在排队画的图太多了，请稍后再试')
            day, counts = self._counts()
            limit = self.daily_limit()
            if not admin and counts.get(user_key, 0) >= limit:
                raise ImageGenError(f'今天的画图次数用完了（每人每天 {limit} 张），明天再来吧' if limit else '画图目前只对管理员开放')
            look = self._config['personas'].get(persona_id) or {} if self_portrait else {}
            final, negative = compose(service, prompt, appearance=look.get('prompt', ''),
                                      appearance_negative=look.get('negative_prompt', ''))
            job = Job(uuid.uuid4().hex, scope_id, user_key, admin, service['id'], final, negative,
                      aspect if aspect in ASPECTS else 'square', prompt, list(references), day)
            if not admin:
                counts[user_key] = counts.get(user_key, 0) + 1
                job.counted = True
                self._save_usage()
            self._jobs[job.id] = job
            remaining = None if admin else max(0, limit - counts.get(user_key, 0))
        return {'id': job.id, 'aspect': job.aspect, 'self_portrait': bool(look.get('prompt')),
                'reference': bool(references), 'remaining_today': remaining}

    def discard(self, job_id):
        """Drop a queued picture that will not be drawn and give the count back."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is not None:
                self._refund(job)
        return job is not None

    def pending(self, job_id):
        with self._lock:
            return job_id in self._jobs

    async def run(self, job_id):
        """Draw a queued picture: (bytes, mime). A failure gives the count back."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is None:
                raise ImageGenError('这张图的排队已经过期')
            self._running += 1
        started = time.monotonic()
        try:
            if self._slot is None:
                self._slot = asyncio.Semaphore(1)
            async with self._slot:
                service = self.service(job.service_id)
                if service is None:
                    raise ImageGenError('生图服务已被停用或删除')
                raw, mime = await generate(service, job.prompt, job.negative, job.aspect, job.references,
                                           transport=self.transport)
        except BaseException as exc:
            with self._lock:
                self._refund(job)
            if isinstance(exc, ImageGenError):
                self.store.add_event('imagegen_failed', '图片没有生成成功，已退回次数：' + str(exc), scope_id=job.scope_id,
                                     user=job.user_key, service_id=job.service_id, request=job.request[:300],
                                     detail=getattr(exc, 'detail', ''), elapsed_ms=round((time.monotonic() - started) * 1000))
            elif isinstance(exc, Exception):
                self.store.add_exception('imagegen_failed', '图片没有生成成功，已退回次数', exc, scope_id=job.scope_id,
                                         user=job.user_key, service_id=job.service_id)
                raise ImageGenError('生图时出现内部错误') from exc
            raise
        finally:
            with self._lock:
                self._running -= 1
        self.store.add_event('imagegen', '图片已生成', scope_id=job.scope_id, user=job.user_key, service_id=job.service_id,
                             request=job.request[:300], aspect=job.aspect, reference=bool(job.references),
                             bytes=len(raw), elapsed_ms=round((time.monotonic() - started) * 1000))
        return raw, mime

    async def test(self, prompt, *, service_id=None, aspect='square'):
        """A test picture from the management page; it uses no daily count."""
        prompt = str(prompt or '').strip()
        if not prompt or len(prompt) > MAX_PROMPT:
            raise ImageGenError(f'画面描述须为 1—{MAX_PROMPT} 个字')
        service = self.service(service_id)
        if service is None:
            raise ImageGenError('请先保存一个生图服务')
        final, negative = compose(service, prompt)
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        started = time.monotonic()
        async with self._slot:
            try:
                raw, mime = await generate(service, final, negative, aspect, transport=self.transport)
            except ImageGenError as exc:
                self.store.add_event('imagegen_failed', '测试图没有生成成功：' + str(exc), service_id=service['id'],
                                     detail=getattr(exc, 'detail', ''))
                raise
        elapsed = round((time.monotonic() - started) * 1000)
        self.store.add_event('imagegen', '测试图已生成', service_id=service['id'], request=prompt[:300], elapsed_ms=elapsed)
        return {'image': f'data:{mime};base64,' + base64.b64encode(raw).decode(), 'mime': mime, 'elapsed_ms': elapsed,
                'bytes': len(raw)}
