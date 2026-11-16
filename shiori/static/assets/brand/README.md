# Shiori brand assets

The app icon is the owner's artwork of 佐藤栞, Shiori's mascot: she hugs a little plush with a chestnut on its head, on a rounded cream tile with an "S", cherry blossoms and an acorn. The original is kept in `design/character/icon-source.webp`.

- `icon-master.png`: 512 px master cut from the artwork (white page keyed out; hair that stands out of the tile is kept). Every file below is a resize of it.
- `app.svg`, `app-*.png`, `shiori.ico`: application, desktop, installer, favicon and the console's brand mark.
- `mark.svg`, `mark-*.png`: the same tile as a transparent brand mark.
- `avatar.svg`, `avatar-*.png`: her face in a cream circle (the web console's chat avatar uses `../character/avatar.jpg`).
- `tray.svg`, `tray-light.svg`, `tray-*.png`: the full-colour tile, which reads on both light and dark taskbars.
- `preview.png`: presentation sheet.

Rebuild every size with `.venv\Scripts\python.exe scripts/build_icons.py`; with `--source <square artwork>` it first cuts a new master from a picture drawn on a white page. Pillow is the only build dependency. Generated files are bundled, so running Shiori needs no icon step.
