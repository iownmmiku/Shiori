"""Build shiori's original vector mark and Windows icons with Pillow only.

SVG paths and raster strokes use the same geometry below. No browser, fonts,
network access or SVG runtime is needed; Pillow is already an app dependency.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'shiori' / 'static' / 'assets' / 'brand'
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
INK = '#10464c'
MINT = '#52dfd0'
PAPER = '#dcf8f2'
WHITE = '#ffffff'
PATHS = (
    (('M', 176, 82), ('C', 159, 61, 104, 60, 84, 84),
     ('C', 59, 115, 98, 126, 132, 132),
     ('C', 169, 139, 188, 153, 169, 178),
     ('C', 151, 201, 96, 199, 77, 176)),
)
SPARK = ((201, 44), (205, 55), (216, 59), (205, 63), (201, 74), (197, 63), (186, 59), (197, 55))


def _path_text(commands):
    return ' '.join(str(value) for command in commands for value in command)


def svg(kind: str) -> str:
    background = ('<rect width="256" height="256" rx="58" fill="' + INK + '"/>') if kind == 'app' else (
        '<circle cx="128" cy="128" r="128" fill="' + PAPER + '"/>' if kind == 'avatar' else '')
    color = MINT if kind == 'app' else WHITE if kind == 'tray-light' else INK
    tray = kind in {'tray', 'tray-light'}
    spark = '' if tray else '<polygon points="' + ' '.join(f'{x},{y}' for x, y in SPARK) + '" fill="' + (PAPER if kind == 'app' else color) + '"/>'
    transform = ' transform="translate(-64 -64) scale(1.5)"' if tray else ''
    paths = ''.join(f'<path d="{_path_text(commands)}"/>' for commands in PATHS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="Shiori">'
            f'{background}<g{transform} fill="none" stroke="{color}" stroke-width="24" stroke-linecap="round" stroke-linejoin="round">'
            f'{paths}</g>{spark}</svg>\n')


def _points(commands):
    points = []
    current = (0, 0)
    for command in commands:
        if command[0] in ('M', 'L'):
            current = command[1:]
            points.append(current)
        elif command[0] == 'C':
            x0, y0 = current
            x1, y1, x2, y2, x3, y3 = command[1:]
            for step in range(1, 65):
                t = step / 64
                u = 1 - t
                points.append((u**3*x0 + 3*u*u*t*x1 + 3*u*t*t*x2 + t**3*x3,
                               u**3*y0 + 3*u*u*t*y1 + 3*u*t*t*y2 + t**3*y3))
            current = (x3, y3)
    return points


def render(kind: str, size: int = 256) -> Image.Image:
    # Render at 4x even for the 16px tray: round stroke caps stay legible.
    scale = max(4, math.ceil(size / 256) * 4)
    canvas = Image.new('RGBA', (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(canvas)
    if kind == 'app':
        draw.rounded_rectangle((0, 0, 256 * scale - 1, 256 * scale - 1), radius=58 * scale, fill=INK)
    elif kind == 'avatar':
        draw.ellipse((0, 0, 256 * scale - 1, 256 * scale - 1), fill=PAPER)
    color = MINT if kind == 'app' else WHITE if kind == 'tray-light' else INK
    tray = kind in {'tray', 'tray-light'}
    optical = 1.5 if tray else 1
    offset = -64 if tray else 0
    for commands in PATHS:
        points = [(round((x * optical + offset) * scale), round((y * optical + offset) * scale)) for x, y in _points(commands)]
        draw.line(points, fill=color, width=round(24 * scale * optical), joint='curve')
        radius = round(12 * scale * optical)
        for x, y in (points[0], points[-1]):
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)
    if not tray:
        draw.polygon([(round(x * scale), round(y * scale)) for x, y in SPARK], fill=PAPER if kind == 'app' else color)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def build(destination: Path = ASSETS, *, preview: bool = True):
    destination.mkdir(parents=True, exist_ok=True)
    for kind in ('app', 'avatar', 'mark', 'tray', 'tray-light'):
        (destination / f'{kind}.svg').write_text(svg(kind), encoding='utf-8')
        for size in SIZES:
            render(kind, size).save(destination / f'{kind}-{size}.png', optimize=True)
    render('app').save(destination / 'shiori.ico', sizes=[(size, size) for size in SIZES])
    if preview:
        image = Image.new('RGB', (960, 470), '#f5fbfa')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 19) if Path('C:/Windows/Fonts/segoeui.ttf').is_file() else ImageFont.load_default(size=19)
        heading = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 28) if Path('C:/Windows/Fonts/segoeuib.ttf').is_file() else font
        draw.text((36, 23), 'Shiori', font=heading, fill=INK)
        draw.text((37, 65), 'An original mark for conversation, rhythm and useful work.', font=font, fill='#617e82')
        for x, kind, label in ((46, 'app', 'App / desktop'), (278, 'avatar', 'Chat avatar'), (510, 'mark', 'Brand mark')):
            tile = render(kind, 180)
            image.paste(tile, (x, 115), tile)
            draw.text((x, 315), label, font=font, fill=INK)
        draw.rounded_rectangle((748, 115, 919, 210), radius=16, fill='#ffffff')
        draw.rounded_rectangle((748, 230, 919, 325), radius=16, fill='#183337')
        for top, kind in ((146, 'tray'), (261, 'tray-light')):
            for index, size in enumerate((16, 24, 32)):
                tile = render(kind, size)
                image.paste(tile, (768 + index * 47, top), tile)
        draw.text((747, 345), 'Tray: 16 / 24 / 32', font=font, fill=INK)
        draw.text((37, 414), 'Windows ICO: 16 / 20 / 24 / 32 / 48 / 64 / 128 / 256 px', font=font, fill='#617e82')
        image.save(destination / 'preview.png', optimize=True)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ASSETS)
    args = parser.parse_args()
    print(build(args.output))
