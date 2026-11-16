# Download page artwork

Artwork of 佐藤栞 made by the project owner for the download page (`site/index.html`), generated from prompts written for Midjourney niji 6. Everything except the hero is drawn on a white page.

- `hero.webp`: hero picture, the calm left side holds the title.
- `stand.webp`: full-body standing pose for “认识佐藤栞”.
- `feature-chat.webp`, `feature-memory.webp`, `feature-stickers.webp`, `feature-local.webp`: one picture per feature row.
- `chibi-set.webp`: three chibis (happy, annoyed, sleepy) on one sheet.

`build_site_art.py` turns them into the files the page uses: it keys the white page out (also the little pockets enclosed by flyaway hair), trims and resizes, cuts the chibi sheet apart, and draws the README / release / link-preview banner `site/assets/banner.jpg` from the hero with Noto Serif SC and Noto Sans SC (SIL OFL). Run it after replacing any picture here, for example with larger originals:

```
.venv\Scripts\python.exe design/site/build_site_art.py
```

Pillow is the only dependency.
