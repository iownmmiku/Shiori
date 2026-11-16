"""Move the web console from the teal palette to Shiori's character palette.

  python -I recolor.py <static_dir> [--write]

Teal/green accents become chestnut (dark slabs and shadows become the blazer's
plum-navy), greenish greys become warm mauve greys, yellowish paper neutrals
shift slightly towards the sheet's peach paper. Coral, amber and the purple /
blue category colours are left alone. Prints every change; writes with --write.
"""
import colorsys
import re
import sys
from collections import Counter
from pathlib import Path

static = Path(sys.argv[1])
write = '--write' in sys.argv
HEX = re.compile(r'#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b')


def to_hex(r, g, b):
    return '#%02x%02x%02x' % tuple(max(0, min(255, round(v * 255))) for v in (r, g, b))


def remap(rgb):
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    deg = h * 360
    if 120 <= deg <= 195 and s >= 0.22 and l < 0.97:
        if l < 0.21 or (l < 0.25 and s < 0.6):                    # dark slabs, shadows -> blazer plum-navy
            nh, ns, nl = 258, min(s * 0.32, 0.2), min(l + 0.05, 0.3)
        else:                                                      # accents -> chestnut, washes -> cream peach
            nh = 11 + 14 * l
            ns = s * (0.6 if l >= 0.85 else 0.42)
            nl = l + (0.04 if l < 0.5 else 0)
    elif 120 <= deg <= 195:                                        # greenish greys -> warm mauve greys
        nh, ns, nl = (340 if l < 0.6 else 24), min(s, 0.12), l
    elif 34 <= deg <= 62 and s < 0.32 and l > 0.6:                 # yellowish paper neutrals -> peach paper
        nh, ns, nl = 26, s, l
    else:
        return None
    nr, ng, nb = colorsys.hls_to_rgb(nh / 360, nl, ns)
    return to_hex(nr, ng, nb)[1:]


changes = Counter()
for path in sorted(p for p in [*static.glob('*.css'), *static.glob('*.js')] if p.name != 'brand.css'):  # app icon stays
    text = path.read_bytes().decode('utf-8')                       # keep CRLF / LF as they are

    def swap(match):
        raw = match.group(1).lower()
        if path.suffix == '.js':
            # Only touch hex that sits in a CSS context inside the script.
            before = text[max(0, match.start() - 24):match.start()]
            if not re.search(r'(color|background|border|fill|stroke|shadow|gradient|:)\s*[^;"\'`]*$', before):
                return match.group(0)
        if len(raw) == 3:
            full, alpha = ''.join(c * 2 for c in raw), ''
        else:
            full, alpha = raw[:6], raw[6:]
        new = remap(full)
        if not new:
            return match.group(0)
        changes[(path.name, '#' + raw, '#' + new + alpha)] += 1
        return '#' + new + alpha

    updated = HEX.sub(swap, text)
    if write and updated != text:
        path.write_bytes(updated.encode('utf-8'))
for (name, old, new), n in sorted(changes.items()):
    print(f'{name:28} {old:10} -> {new:10} x{n}')
print('total', sum(changes.values()), 'in', len({k[0] for k in changes}), 'files', '(written)' if write else '(dry run)')
