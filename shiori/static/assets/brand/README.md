# Shiori brand assets

An original curved `S` for Shiori, with a small spark for curiosity. Deep teal `#10464c`, mint `#52dfd0`, pale mint `#dcf8f2`. No character portrait, font glyph, or third-party logo is embedded.

- `app.svg`, `app-*.png`, `shiori.ico`: application, desktop, installer, favicon.
- `avatar.svg`, `avatar-*.png`: chat avatar.
- `mark.svg`, `mark-*.png`: transparent brand mark.
- `tray.svg` / `tray-light.svg`: one-color marks for light / dark taskbars. The tray mark uses a larger optical size and omits the small spark so it remains legible at 16 px.
- `preview.png`: presentation sheet.

Rebuild from the shared vector geometry using `.venv\Scripts\python.exe scripts/build_icons.py`. Pillow is the only build dependency; it is already used by the application. The script generates SVG, transparent PNG at 16 / 20 / 24 / 32 / 48 / 64 / 128 / 256 px, and a Windows ICO containing all eight sizes. Generated files are bundled, so running Shiori requires no icon generation step.
