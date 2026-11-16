# 佐藤栞 character sources

Original character sheets supplied by the project owner, and the scripts that cut them into the app and site art.

- `sheet-expressions.webp`: portrait, expressions, belongings (1672×941).
- `sheet-turnaround.webp`: full-body front and back views (1024×1536).
- `extract.py <dir with 1.webp/2.webp> <out>`: keys the paper background into transparent PNGs (`_full-*.png` are the full-colour results used by the app). Rename or copy the sheets to `1.webp` / `2.webp` first.
- `banner.py <out> <banner.jpg>`: the README / release banner in `site/assets/` (Noto Sans SC, SIL OFL).
- `recolor.py <shiori/static> [--write]`: the one-off move from the teal palette to the character palette (already applied).

Final art lives in `shiori/static/assets/character/`; see the README there for where each piece is used.
