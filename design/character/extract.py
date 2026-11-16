"""Cut Shiori's character art out of the two reference sheets into transparent PNGs.

  python -I extract.py <images_dir> <out_dir>

Background is keyed from the sheet's own paper colour: pixels connected to the
crop border that stay close to it become transparent (colour-to-alpha keeps the
soft hair edges), enclosed light areas such as the cardigan stay opaque.
Speech bubbles, labels and neighbouring figures are painted out first.
"""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)


def paint(im, box, bg, gray_only=False, light_only=False, chroma=34, min_lum=0):
    """Fill box (sheet coordinates already shifted) with the paper colour.
    gray_only: only low-chroma ink (bubble outlines, text, doodles, floor shadows),
    so brown hair crossing the box survives. light_only: only light pixels."""
    px = im.load()
    x0, y0, x1, y1 = [int(v) for v in box]
    for y in range(max(0, y0), min(im.height, y1)):
        for x in range(max(0, x0), min(im.width, x1)):
            r, g, b = px[x, y]
            if gray_only and not (max(r, g, b) - min(r, g, b) < chroma and (r + g + b) / 3 >= min_lum):
                continue
            if light_only and (r + g + b) / 3 < 140:
                continue
            px[x, y] = bg


def paper_like(im, bg, deviation=8, darker=45, lighter=14):
    """255 where a pixel differs from the paper only in lightness: paper grain,
    compression noise, the white glow around the figure, grey floor shadows.
    Skin, cream knit and light hair all shift hue, so they are kept."""
    w, h = im.size
    mask = Image.new('L', im.size, 0)
    src, dst = im.load(), mask.load()
    br, bgc, bb = bg
    for y in range(h):
        for x in range(w):
            r, g, b = src[x, y]
            dr, dg, db = r - br, g - bgc, b - bb
            mean = (dr + dg + db) / 3
            if -darker <= mean <= lighter and max(abs(dr - mean), abs(dg - mean), abs(db - mean)) <= deviation:
                dst[x, y] = 255
    return mask


def key(im, bg, pocket_area=12, soft=(), keep=()):
    w, h = im.size
    near = paper_like(im, bg)
    for box in soft:
        near.paste(paper_like(im.crop(box), bg, deviation=14, darker=70), box[:2])
    for box in keep:
        near.paste(0, box)
    dist = near.point(lambda v: 0 if v else 255)                       # only used to seed pockets below
    pocket = 0
    # 128 = background reached from the border.
    for x in range(w):
        for y in (0, h - 1):
            if near.getpixel((x, y)) == 255:
                ImageDraw.floodfill(near, (x, y), 128)
    for y in range(h):
        for x in (0, w - 1):
            if near.getpixel((x, y)) == 255:
                ImageDraw.floodfill(near, (x, y), 128)
    # Enclosed pockets of paper (gaps between arm and body, legs) are background too when large.
    pure = near.point(lambda v: 255 if v == 255 else 0)
    data = bytearray(pure.tobytes())
    start = data.find(b'\xff')
    while start != -1:
        x, y = start % w, start // w
        ImageDraw.floodfill(pure, (x, y), 200)
        hist = pure.histogram()
        area = hist[200]
        ImageDraw.floodfill(pure, (x, y), 128 if area >= pocket_area else 60)
        data = bytearray(pure.tobytes())
        start = data.find(b'\xff', start)
    background = Image.eval(near, lambda v: 255 if v == 128 else 0)
    background = ImageChops.lighter(background, pure.point(lambda v: 255 if v == 128 else 0))
    soft = background.filter(ImageFilter.MaxFilter(3))               # one-pixel band around it
    rgba = im.convert('RGBA')
    px, sp, bp = rgba.load(), soft.load(), background.load()
    br, bgc, bb = bg
    for y in range(h):
        for x in range(w):
            if not sp[x, y]:
                continue
            r, g, b, _ = px[x, y]
            a = 0.0
            for c, k in ((r, br), (g, bgc), (b, bb)):
                if c > k:
                    a = max(a, (c - k) / (255 - k or 1))
                elif c < k:
                    a = max(a, (k - c) / (k or 1))
            if bp[x, y]:
                a = 0.0                                                  # paper, glow, grain: gone
            else:
                a = max(a, 0.45)                                         # edge pixels never vanish
            if a <= 0.02:
                px[x, y] = (0, 0, 0, 0)
                continue
            un = [min(255, max(0, round((c - k) / a + k))) for c, k in ((r, br), (g, bgc), (b, bb))]
            px[x, y] = (*un, round(a * 255))
    bbox = rgba.getchannel('A').point(lambda v: 255 if v > 8 else 0).getbbox()
    return rgba.crop(bbox) if bbox else rgba


def save(im, name, height=None, width=None):
    if height and im.height > height:
        im = im.resize((round(im.width * height / im.height), height), Image.LANCZOS)
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    q = im.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    q.save(out / name, optimize=True)
    full = out / ('_full-' + name)
    im.save(full, optimize=True)
    print(name, im.size, (out / name).stat().st_size, 'bytes (full', full.stat().st_size, ')')
    return im


def asset(sheet, box, cleanups, name, soft=(), keep=(), pocket_area=12, **size):
    bg = sheet.getpixel((5, 5))
    im = sheet.crop(box).copy()
    for c in cleanups:
        b, opts = c[0], c[1] if len(c) > 1 else {}
        paint(im, (b[0] - box[0], b[1] - box[1], b[2] - box[0], b[3] - box[1]), bg, **opts)
    shift = lambda b: (b[0] - box[0], b[1] - box[1], b[2] - box[0], b[3] - box[1])
    return save(key(im, bg, pocket_area=pocket_area, soft=[shift(b) for b in soft], keep=[shift(b) for b in keep]), name, **size)


one = Image.open(src / '1.webp').convert('RGB')
two = Image.open(src / '2.webp').convert('RGB')
G = {'gray_only': True}

# Full body, front view (second sheet).
stand_bg = two.getpixel((5, 5))
stand = two.crop((28, 0, 528, 1536)).copy()
paint(stand, (60 - 28, 300, 112 - 28, 350), stand_bg)                 # flower doodles
paint(stand, (48 - 28, 1022, 104 - 28, 1080), stand_bg)
sp = stand.load()
for y in range(stand.height):                                          # thin vertical rule at x≈38
    for x in range(30 - 28, 47 - 28):
        r, g, b = sp[x, y]
        if (r + g + b) / 3 > 160 and max(r, g, b) - min(r, g, b) < 40:
            sp[x, y] = stand_bg
save(key(stand, stand_bg, soft=[(0, 1440, stand.width, stand.height)],
         keep=[(190 - 28, 140, 370 - 28, 270), (240 - 28, 290, 360 - 28, 470)]), 'stand-front.png', height=1100)

# Half-body portrait (first sheet, centre).
asset(one, (612, 0, 1266, 941), [
    ((612, 0, 674, 560),),                                             # back-view figure, doodles
    ((674, 262, 689, 410),),                                           # its bag
    ((1084, 4, 1212, 118),),                                           # "……別に。" bubble (no hair up here)
    ((1084, 118, 1212, 184), {'gray_only': True, 'chroma': 40}),       # its lower part, crossed by hair
    ((1172, 146, 1214, 196),),                                         # floating anger mark
    ((1236, 0, 1266, 520),),                                           # pen case / school bag edge, flower
    ((612, 740, 630, 941), G),                                         # edge of the neighbouring bubble
    ((1238, 520, 1266, 660), {'gray_only': True, 'min_lum': 150}),     # doodle remnants beside the sleeve
], 'portrait.png', keep=[(840, 210, 1090, 430), (1050, 540, 1195, 725)], width=640)

# Chibi: quiet "……".
asset(one, (50, 696, 264, 941), [
    ((40, 726, 60, 802),),                                             # small "……" bubble
    ((236, 696, 264, 800), G),                                         # loafer shadow
], 'chibi-quiet.png', keep=[(85, 795, 235, 885)])

# Chibi: pouting "べ、別に…".
asset(one, (318, 712, 541, 941), [
    ((318, 712, 398, 757),),                                           # loafers of the full-body views
    ((478, 712, 541, 751),),
    ((318, 750, 412, 800), G),                                         # their floor shadows
    ((480, 745, 541, 800), G),
    ((530, 788, 541, 912), G),                                         # bubble outline
], 'chibi-pout.png', keep=[(355, 815, 505, 905)])

# Chibi: angry "もーっ…!".
asset(one, (1296, 476, 1672, 941), [
    ((1296, 476, 1340, 640), {'gray_only': True, 'chroma': 22, 'min_lum': 170}),  # cloud doodles
    ((1490, 476, 1580, 548), G),                                       # thought-cloud outline behind the mark
    ((1296, 860, 1305, 941),),                                         # edge of the big portrait's sleeve
    ((1574, 440, 1632, 640), G),                                       # bubble
    ((1632, 440, 1672, 640),),
    ((1606, 806, 1664, 858), G),                                       # flower doodle
], 'chibi-angry.png', keep=[(1375, 645, 1605, 835), (1515, 815, 1605, 898)])

# Iced americano.
asset(one, (1505, 26, 1660, 252), [
    ((1495, 10, 1599, 68),),                                           # label
    ((1626, 48, 1660, 86),),                                           # red accent marks
], 'item-coffee.png', pocket_area=10**7)
