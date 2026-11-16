"""README / release banner: Shiori's name on the left, 佐藤栞 on the right.

  python -I banner.py <cutout_dir> <out.jpg>
Text uses Noto Sans SC (SIL OFL) so the image carries no font licence question.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

cut, out = Path(sys.argv[1]), Path(sys.argv[2])
W, H = 1600, 560
INK, INK2, CHESTNUT, WASH, CORAL = (42, 33, 39), (79, 66, 71), (140, 91, 76), (245, 231, 222), (196, 73, 63)
NOTO = 'C:/Windows/Fonts/NotoSansSC-VF.ttf'


def font(size, weight):
    f = ImageFont.truetype(NOTO, size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def fit(im, h):
    return im.resize((round(im.width * h / im.height), h), Image.LANCZOS)


canvas = Image.new('RGB', (W, H), (248, 241, 234))
grad = Image.linear_gradient('L').resize((W, H))                       # top light, bottom warmer
warm = Image.new('RGB', (W, H), (242, 230, 220))
canvas = Image.composite(warm, canvas, grad)
glow = Image.new('L', (W, H), 0)
ImageDraw.Draw(glow).ellipse((900, 40, 1500, 640), fill=150)
glow = glow.filter(ImageFilter.GaussianBlur(90))
canvas = Image.composite(Image.new('RGB', (W, H), (240, 220, 207)), canvas, glow)
draw = ImageDraw.Draw(canvas)

# Little five-petal flowers like the ones doodled on the character sheet.
def flower(cx, cy, r, color):
    import math
    for k in range(5):
        a = math.radians(k * 72 - 90)
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        draw.ellipse((x - r * 0.62, y - r * 0.62, x + r * 0.62, y + r * 0.62), outline=color, width=2)
    draw.ellipse((cx - r * 0.3, cy - r * 0.3, cx + r * 0.3, cy + r * 0.3), outline=color, width=2)

for cx, cy, r in ((760, 92, 13), (1520, 120, 11), (860, 470, 10), (64, 470, 12), (1180, 60, 9)):
    flower(cx, cy, r, (226, 205, 192))

# Scarf plaid along the bottom edge.
band = 26
draw.rectangle((0, H - band, W, H), fill=(230, 214, 200))
for x in range(0, W, 64):
    draw.rectangle((x, H - band, x + 22, H), fill=(214, 194, 180))
    draw.line((x + 40, H - band, x + 40, H), fill=(205, 108, 94), width=2)
draw.line((0, H - band + 9, W, H - band + 9), fill=(214, 194, 180), width=5)
draw.line((0, H - band + 18, W, H - band + 18), fill=(205, 108, 94), width=1)

# Name and one line about the app.
draw.text((104, 118), 'Shiori', font=font(140, 700), fill=INK)
dot_x = 104 + draw.textlength('Shiori', font=font(140, 700)) + 4
draw.text((dot_x, 118), '.', font=font(140, 700), fill=CHESTNUT)
draw.text((110, 300), '本机运行的 AI 聊天伙伴', font=font(40, 600), fill=INK2)
draw.text((112, 362), '独立聊天 · QQ 接入 · 长期记忆 · 电脑任务', font=font(24, 450), fill=(117, 103, 106))
pill = font(24, 600)
label = '看板娘　佐藤栞'
tw = draw.textlength(label, font=pill)
draw.rounded_rectangle((110, 420, 110 + tw + 40, 466), radius=23, fill=(239, 221, 209))
draw.text((130, 425), label, font=pill, fill=(107, 67, 55))

# The character: full body, half-body portrait, a pouting chibi.
stand = fit(Image.open(cut / '_full-stand-front.png').convert('RGBA'), 500)
portrait = fit(Image.open(cut / '_full-portrait.png').convert('RGBA'), 548)
pout = fit(Image.open(cut / '_full-chibi-pout.png').convert('RGBA'), 150)
shadow = Image.new('L', (W, H), 0)
ImageDraw.Draw(shadow).ellipse((832, H - band - 18, 1000, H - band + 6), fill=70)
canvas = Image.composite(Image.new('RGB', (W, H), (200, 180, 168)), canvas, shadow.filter(ImageFilter.GaussianBlur(8)))
canvas.paste(stand, (836, H - band - stand.height + 4), stand)
canvas.paste(portrait, (1040, H - portrait.height), portrait)
canvas.paste(pout, (1430, H - band - pout.height + 10), pout)

# Speech bubble from the sheet ("……別に。"), in Chinese.
draw = ImageDraw.Draw(canvas)
bx, by = 1352, 96
bubble_font = font(28, 500)
bw = draw.textlength('……没什么。', font=bubble_font) + 56
draw.rounded_rectangle((bx, by, bx + bw, by + 64), radius=30, fill=(255, 252, 249), outline=(171, 150, 141), width=2)
draw.polygon([(bx + 30, by + 60), (bx + 14, by + 86), (bx + 52, by + 62)], fill=(255, 252, 249))
draw.line([(bx + 30, by + 63), (bx + 14, by + 86), (bx + 50, by + 63)], fill=(171, 150, 141), width=2)
draw.text((bx + 28, by + 12), '……没什么。', font=bubble_font, fill=INK2)

canvas.save(out, quality=90, optimize=True, progressive=True)
print(out, canvas.size, out.stat().st_size)
