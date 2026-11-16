"""Build Shiori's icons from the owner's artwork: 佐藤栞 on a rounded cream tile.

  python scripts/build_icons.py                      # every bundled size from icon-master.png
  python scripts/build_icons.py --source art.webp    # first cut a new master from square artwork

The artwork is a rounded tile drawn on a white page. ``--source`` keys that
white page out from the corners (hair that stands out of the tile is kept) and
stores a 512 px master beside the icons. Everything else is a deterministic
resize of the master, so tests can rebuild the bundled files and compare them.
Pillow is the only dependency; it is already an application dependency.
"""
from __future__ import annotations

import argparse
import base64
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'shiori' / 'static' / 'assets' / 'brand'
MASTER = ASSETS / 'icon-master.png'
MASTER_SIZE = 512
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
KINDS = ('app', 'avatar', 'mark', 'tray', 'tray-light')
CREAM = (243, 230, 220, 255)
# The face in the master, for the round chat avatar: centre and radius in master pixels.
FACE = (250, 228, 168)
SVG_SIZE = 128            # raster embedded in the .svg files (shown at 34–60 px)


def cut_master(source: Path, destination: Path = MASTER) -> Path:
    """Key the white page around the rounded tile and save the square master."""
    image = Image.open(source).convert('RGB')
    side = min(image.size)
    image = image.crop(((image.width - side) // 2, (image.height - side) // 2,
                        (image.width + side) // 2, (image.height + side) // 2))
    px = image.load()
    w, h = image.size
    white = lambda p: min(p) >= 244 and max(p) - min(p) <= 8
    page = bytearray(w * h)                       # 1 = white page reached from the edge
    queue = deque((x, y) for x in range(w) for y in (0, h - 1))
    queue.extend((x, y) for y in range(h) for x in (0, w - 1))
    while queue:
        x, y = queue.popleft()
        if page[y * w + x] or not white(px[x, y]):
            continue
        page[y * w + x] = 1
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not page[ny * w + nx]:
                queue.append((nx, ny))
    mask = Image.frombytes('L', (w, h), bytes(255 if v else 0 for v in page))
    edge = mask.filter(ImageFilter.MaxFilter(3))  # the pixel ring around the page
    rgba = image.convert('RGBA')
    out, mp, ep = rgba.load(), mask.load(), edge.load()
    for y in range(h):
        for x in range(w):
            if mp[x, y]:
                out[x, y] = (0, 0, 0, 0)
            elif ep[x, y]:
                # Colour-to-alpha against white keeps the antialiased border soft.
                r, g, b, _ = out[x, y]
                a = max(1 - r / 255, 1 - g / 255, 1 - b / 255, 0.35)
                out[x, y] = tuple(min(255, max(0, round(255 - (255 - c) / a))) for c in (r, g, b)) + (round(a * 255),)
    rgba.resize((MASTER_SIZE, MASTER_SIZE), Image.Resampling.LANCZOS).save(destination, optimize=True)
    return destination


def _master() -> Image.Image:
    with Image.open(MASTER) as image:
        return image.convert('RGBA')


def render(kind: str, size: int = 256) -> Image.Image:
    master = _master()
    if kind == 'avatar':
        cx, cy, r = FACE
        face = master.crop((cx - r, cy - r, cx + r, cy + r))
        disc = Image.new('RGBA', face.size, CREAM)
        disc.alpha_composite(face)
        circle = Image.new('L', (face.width * 4, face.height * 4))
        ImageDraw.Draw(circle).ellipse((0, 0, circle.width - 1, circle.height - 1), fill=255)
        disc.putalpha(circle.resize(face.size, Image.Resampling.LANCZOS))
        master = disc
    # The tray is a full-colour copy: the same picture reads on light and dark taskbars.
    return master.resize((size, size), Image.Resampling.LANCZOS)


def svg(kind: str) -> str:
    from io import BytesIO
    buffer = BytesIO()
    render(kind, SVG_SIZE).save(buffer, format='PNG', optimize=True)
    data = base64.b64encode(buffer.getvalue()).decode('ascii')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_SIZE} {SVG_SIZE}" '
            f'width="{SVG_SIZE}" height="{SVG_SIZE}"><title>Shiori</title>'
            f'<image width="{SVG_SIZE}" height="{SVG_SIZE}" href="data:image/png;base64,{data}"/></svg>\n')


def build(destination: Path = ASSETS, *, preview: bool = True):
    destination.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        (destination / f'{kind}.svg').write_text(svg(kind), encoding='utf-8')
        for size in SIZES:
            render(kind, size).save(destination / f'{kind}-{size}.png', optimize=True)
    frames = [render('app', size) for size in SIZES]
    frames[-1].save(destination / 'shiori.ico', sizes=[(size, size) for size in SIZES], append_images=frames[:-1])
    if preview:
        image = Image.new('RGB', (960, 470), '#f7f0e9')
        draw = ImageDraw.Draw(image)
        noto = Path('C:/Windows/Fonts/NotoSansSC-VF.ttf')
        font = ImageFont.truetype(str(noto), 19) if noto.is_file() else ImageFont.load_default(size=19)
        heading = ImageFont.truetype(str(noto), 28) if noto.is_file() else font
        draw.text((36, 20), 'Shiori', font=heading, fill='#2a2127')
        draw.text((37, 65), '佐藤栞 on a rounded cream tile; the tray uses the same colour picture.', font=font, fill='#75676a')
        for x, kind, label in ((46, 'app', 'App / desktop'), (278, 'avatar', 'Chat avatar'), (510, 'mark', 'Brand mark')):
            tile = render(kind, 180)
            image.paste(tile, (x, 115), tile)
            draw.text((x, 315), label, font=font, fill='#2a2127')
        draw.rounded_rectangle((748, 115, 919, 210), radius=16, fill='#ffffff')
        draw.rounded_rectangle((748, 230, 919, 325), radius=16, fill='#2a2a30')
        for top, kind in ((146, 'tray'), (261, 'tray-light')):
            for index, size in enumerate((16, 24, 32)):
                tile = render(kind, size)
                image.paste(tile, (768 + index * 47, top), tile)
        draw.text((747, 345), 'Tray: 16 / 24 / 32', font=font, fill='#2a2127')
        draw.text((37, 414), 'Windows ICO: 16 / 20 / 24 / 32 / 48 / 64 / 128 / 256 px', font=font, fill='#75676a')
        image.save(destination / 'preview.png', optimize=True)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ASSETS)
    parser.add_argument('--source', type=Path, help='square artwork on a white page; replaces icon-master.png first')
    args = parser.parse_args()
    if args.source:
        print('master:', cut_master(args.source))
    print(build(args.output))
