"""Small, bundled brand assets shared by the desktop, tray and installer."""
from __future__ import annotations

from pathlib import Path


def asset_path(name: str) -> Path:
    if name not in {'shiori.ico', 'app-64.png', 'avatar-64.png', 'tray-64.png', 'tray-light-64.png'}:
        raise ValueError('Unknown brand asset')
    return Path(__file__).resolve().parent / 'static' / 'assets' / 'brand' / name


def tray_image(*, dark_theme: bool | None = None):
    from PIL import Image
    if dark_theme is None:
        dark_theme = False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                dark_theme = winreg.QueryValueEx(key, 'SystemUsesLightTheme')[0] == 0
        except (ImportError, OSError):
            pass
    # A copied image closes the resource file before the tray owns the bitmap.
    with Image.open(asset_path('tray-light-64.png' if dark_theme else 'tray-64.png')) as image:
        return image.convert('RGBA').copy()
