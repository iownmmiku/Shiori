"""Pictures sent on their own after a reply (extensions such as image generation).

The bytes are untrusted: the real format and size come from the pixels, and a
picture is re-encoded when a connector would refuse it.
"""
from __future__ import annotations

import io
import warnings

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 36_000_000
ONEBOT_MAX_BYTES = 4 * 1024 * 1024
OFFICIAL_MAX_BYTES = 2 * 1024 * 1024
FORMATS = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'), 'WEBP': ('webp', 'image/webp'),
           'GIF': ('gif', 'image/gif')}


def image_info(data):
    """(extension, mime, (width, height)) from the actual pixels; ValueError otherwise."""
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('图片为空或超过 32 MB')
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                size, kind = picture.size, picture.format
                if kind not in FORMATS or min(size) < 1 or size[0] * size[1] > MAX_IMAGE_PIXELS:
                    raise ValueError('图片格式或尺寸不受支持')
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()
    except ValueError as exc:
        if str(exc) == '图片格式或尺寸不受支持':
            raise
        raise ValueError('图片损坏或无法识别') from exc
    except Exception as exc:
        raise ValueError('图片损坏或无法识别') from exc
    return FORMATS[kind][0], FORMATS[kind][1], size


def reencode(raw, *, max_side, limit, keep=('image/png', 'image/jpeg', 'image/gif')):
    """The picture as-is when it fits; otherwise a smaller JPEG."""
    _, mime, size = image_info(raw)
    if mime in keep and len(raw) <= limit and max(size) <= max_side:
        return bytes(raw)
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as picture:
        picture.seek(0)
        frame = picture.convert('RGBA')
    background = Image.new('RGB', frame.size, (255, 255, 255))
    background.paste(frame, mask=frame.split()[-1])
    background.thumbnail((max_side, max_side))
    for quality in (90, 82, 74, 66, 58):
        output = io.BytesIO()
        background.save(output, format='JPEG', quality=quality, optimize=True)
        if output.tell() <= limit:
            return output.getvalue()
        background.thumbnail((int(background.width * .85), int(background.height * .85)))
    raise ValueError('图片压缩后仍然太大，无法发送')


def onebot_image(raw):
    """A picture OneBot connectors accept as a base64 image segment."""
    return reencode(raw, max_side=4096, limit=ONEBOT_MAX_BYTES)


def official_image(raw):
    """A picture the official bot's rich media upload accepts."""
    return reencode(raw, max_side=2048, limit=OFFICIAL_MAX_BYTES)
