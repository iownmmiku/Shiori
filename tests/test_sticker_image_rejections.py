import asyncio
import base64
import binascii
import io
import json
import struct
from types import SimpleNamespace
from urllib.parse import urlsplit

from PIL import Image
import pytest

from shiori.db import Store
from shiori.settings import Settings
from shiori import stickers


def png_bytes():
    stream = io.BytesIO()
    Image.new('RGB', (16, 16), 'cyan').save(stream, format='PNG')
    return stream.getvalue()


def large_png_bytes():
    stream = io.BytesIO()
    Image.new('RGB', (1024, 1024), 'cyan').save(stream, format='PNG', compress_level=0)
    raw = stream.getvalue()
    assert stickers.MAX_IMAGE < len(raw) < 5 * 1024 * 1024
    return raw


def data_url(raw):
    return 'data:image/png;base64,' + base64.b64encode(raw).decode()


def declared_png_size(width, height):
    raw = png_bytes()
    header = struct.pack('>II', width, height) + raw[24:29]
    return raw[:16] + header + struct.pack('>I', binascii.crc32(b'IHDR' + header)) + raw[33:]


@pytest.mark.parametrize('raw,reason,count', [
    (None, 'invalid_type', None), (bytearray(b'x'), 'invalid_type', None),
    (b'', 'empty', 0), (b'x' * 101, 'byte_limit', 101),
    (b'not an image', 'decode', 12),
])
def test_rejection_reasons_distinguish_bytes_empty_size_and_decode(raw, reason, count):
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.image_bytes(raw, 100)
    assert caught.value.reason == reason
    assert caught.value.byte_count == count
    assert caught.value.byte_limit == 100


def test_unsupported_format_and_pixel_limits_remain_separate():
    stream = io.BytesIO()
    Image.new('RGB', (16, 16), 'cyan').save(stream, format='BMP')
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.image_bytes(stream.getvalue())
    assert caught.value.reason == 'format'
    for size in ((4000, 4000), (100_000, 100_000)):
        with pytest.raises(stickers.StickerImageRejected) as caught:
            stickers.image_bytes(declared_png_size(*size))
        assert caught.value.reason == 'pixel_limit'


@pytest.mark.parametrize('cap', [0, -2, True, 1.5, stickers.MAX_IMAGE_INPUT + 1])
def test_invalid_limits_cannot_make_a_download_unbounded(cap, monkeypatch):
    def forbidden(*args):
        raise AssertionError('invalid cap must be rejected before a connection')
    monkeypatch.setattr(stickers, 'public_addresses', forbidden)
    for operation in (lambda: stickers.image_bytes(png_bytes(), cap),
                      lambda: stickers.download_image('https://example.invalid/img', cap)):
        with pytest.raises(ValueError) as caught:
            operation()
        assert not isinstance(caught.value, stickers.StickerImageRejected)


def test_data_urls_respect_custom_byte_limit_before_and_after_decode():
    raw = png_bytes()
    assert stickers.download_image(data_url(raw), len(raw)) == (raw, 'image/png')
    for cap in (1, len(raw)-1):
        with pytest.raises(stickers.StickerImageRejected) as caught:
            stickers.download_image(data_url(raw), cap)
        assert caught.value.reason == 'byte_limit'
        assert caught.value.byte_limit == cap
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.download_image(data_url(b''), 100)
    assert caught.value.reason == 'empty'
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.download_image('data:image/png;base64,not-valid', 100)
    assert caught.value.reason == 'encoding'


class Response:
    status = 200
    def __init__(self, body, length=None):
        self.body, self.length, self.reads = body, length, []
    def getheader(self, key):
        return self.length if key == 'Content-Length' else None
    def read(self, size):
        self.reads.append(size)
        return self.body[:size]


def fake_connection(monkeypatch, response):
    connection = SimpleNamespace(request=lambda *a, **k: None, getresponse=lambda: response,
                                 closed=False)
    def close():
        connection.closed = True
    connection.close = close
    monkeypatch.setattr(stickers, 'public_addresses', lambda url:
        (urlsplit(url), 'cdn.example', 443, ['8.8.8.8']))
    monkeypatch.setattr(stickers, '_PinnedHTTPS', lambda *a: connection)
    return connection


@pytest.mark.parametrize('header', ['101', '9' * 5000])
def test_declared_oversize_rejects_before_read_and_closes(monkeypatch, header):
    response = Response(b'not read', header)
    connection = fake_connection(monkeypatch, response)
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.download_image('https://cdn.example/img?signature=private', 100)
    assert caught.value.reason == 'byte_limit'
    assert response.reads == [] and connection.closed


@pytest.mark.parametrize('header', [None, '1', 'not-a-size', '-1'])
def test_missing_false_or_invalid_length_keeps_bounded_body_read(monkeypatch, header):
    response = Response(b'x' * 102, header)
    connection = fake_connection(monkeypatch, response)
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.download_image('https://cdn.example/img', 100)
    assert caught.value.reason == 'byte_limit'
    assert caught.value.byte_count == 101
    assert response.reads == [101] and connection.closed


def test_http_empty_and_valid_images_are_checked_after_bounded_read(monkeypatch):
    response = Response(b'', '0')
    fake_connection(monkeypatch, response)
    with pytest.raises(stickers.StickerImageRejected) as caught:
        stickers.download_image('https://cdn.example/img', 100)
    assert caught.value.reason == 'empty' and response.reads == [101]
    raw = png_bytes(); response = Response(raw, str(len(raw)))
    fake_connection(monkeypatch, response)
    assert stickers.download_image('https://cdn.example/img', len(raw)) == (raw, 'image/png')


@pytest.mark.parametrize('large', [False, True])
def test_shared_qq_five_mib_limit_supports_valid_http_and_data_images(monkeypatch, large):
    raw = large_png_bytes() if large else png_bytes()
    cap = 5 * 1024 * 1024
    assert stickers.image_bytes(raw, cap) == (raw, 'image/png')
    assert stickers.download_image(data_url(raw), cap) == (raw, 'image/png')
    response = Response(raw, str(len(raw)))
    fake_connection(monkeypatch, response)
    assert stickers.download_image('https://cdn.example/img', cap) == (raw, 'image/png')
    assert response.reads == [cap+1]
    if large:
        for operation in (lambda: stickers.image_bytes(raw), lambda: stickers.download_image(data_url(raw))):
            with pytest.raises(stickers.StickerImageRejected) as caught:
                operation()
            assert caught.value.reason == 'byte_limit' and caught.value.byte_limit == stickers.MAX_IMAGE


@pytest.mark.parametrize('large', [False, True])
def test_real_qq_chat_prepares_remote_images_with_its_existing_five_mib_limit(tmp_path, monkeypatch, large):
    from shiori.chat import ChatService
    raw = large_png_bytes() if large else png_bytes()
    remote = 'https://cdn.example/img?signature=fixture'
    response = Response(raw, str(len(raw)))
    connection = fake_connection(monkeypatch, response)
    calls = []
    class Vision:
        async def complete(self, messages, **kwargs):
            assert kwargs['before_send']()
            calls.append(messages)
            return {'content':'看到了图片。', 'tool_calls':[], 'usage':{},
                    'message':{'role':'assistant','content':'看到了图片。'}}
    store = Store(tmp_path); settings = Settings(tmp_path)
    settings.update({'model':{'model':'fixture-vision','base_url':'https://fixture.invalid/v1','vision':True},
                     'onebot':{'enabled':True,'chat_policy':'blacklist','access_token':'fixture-image-token'},
                     'stickers':{'enabled':False}})
    store.upsert_scope({'id':'qq:private:42','auto_memory':False,'always_reply':True})
    async def run():
        chat = ChatService(store, settings, Vision())
        try:
            result = await chat.respond('qq:private:42', '42', '看看图片', images=[remote])
            assert result['reply'] == '看到了图片。'
        finally:
            await chat.close()
    try:
        asyncio.run(run())
        parts = [part for messages in calls for message in messages
                 if isinstance(message.get('content'), list) for part in message['content']]
        urls = [part['image_url']['url'] for part in parts if part.get('type') == 'image_url']
        assert urls == [data_url(raw)] and remote not in json.dumps(calls)
        assert response.reads == [5*1024*1024+1] and connection.closed
        assert not [e for e in store.list_events() if e['kind'] == 'qq_image_error']
    finally:
        store.close()


def test_redirect_target_still_goes_through_public_address_checks(monkeypatch):
    response = Response(b'')
    response.status = 302
    response.getheader = lambda name: 'http://127.0.0.1/private?token=hidden' if name == 'Location' else None
    connection = fake_connection(monkeypatch, response)
    seen = []
    def gate(url):
        seen.append(url)
        if len(seen) > 1:
            raise ValueError('internal target blocked')
        return urlsplit(url), 'cdn.example', 443, ['8.8.8.8']
    monkeypatch.setattr(stickers, 'public_addresses', gate)
    with pytest.raises(ValueError, match='internal target blocked'):
        stickers.download_image('https://cdn.example/img')
    assert len(seen) == 2 and connection.closed


@pytest.mark.parametrize('error,expected_kind', [
    (stickers.StickerImageRejected('empty', byte_count=0, byte_limit=100), 'stickers_skipped'),
    (RuntimeError('unexpected internal failure'), 'stickers_error'),
    (ValueError('unexpected value failure'), 'stickers_error'),
])
def test_collection_rejections_are_safe_and_do_not_spend_model_quota(tmp_path, monkeypatch, error, expected_kind):
    store = Store(tmp_path)
    settings = Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture', 'vision': True}})
    calls = []
    async def complete(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError('rejected image must not reach a model')
    library = stickers.StickerLibrary(store, settings, SimpleNamespace(complete=complete), lambda scope: True)
    url = 'https://cdn.example/image?signature=private-image-value'
    source = store.add_message('qq:group:fixture', 'fixture-user', 'user', '', images=[url])
    key = (source['id'], 'fixture'); library._pending.add(key)
    def reject(*args, **kwargs):
        raise error
    monkeypatch.setattr(stickers, 'download_image', reject)
    try:
        asyncio.run(library._collect(source, url, key))
        events = [e for e in store.list_events() if e['kind'].startswith('stickers_')]
        assert len(events) == 1 and events[0]['kind'] == expected_kind
        assert url not in json.dumps(events) and 'private-image-value' not in json.dumps(events)
        if expected_kind == 'stickers_skipped':
            assert events[0]['details']['reason'] == 'empty'
            assert events[0]['details']['byte_count'] == 0
            assert events[0]['details']['byte_limit'] == 100
            assert 'traceback' not in events[0]['details']
        else:
            assert events[0]['details']['error'] == type(error).__name__
            assert events[0]['details']['traceback']
        assert calls == [] and library.quota_status()['total'] == 0
        assert key not in library._pending and library.page()['total'] == 0
    finally:
        asyncio.run(library.close()); store.close()
