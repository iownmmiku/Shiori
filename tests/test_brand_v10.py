from __future__ import annotations

import importlib.util
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

from shiori.brand import asset_path, tray_image
from shiori import __version__

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'shiori' / 'static' / 'assets' / 'brand'
SIZES = {16, 20, 24, 32, 48, 64, 128, 256}


def test_release_asset_urls_refresh_cached_older_interfaces():
    import re
    html = (ROOT / 'shiori' / 'static' / 'index.html').read_text('utf-8')
    urls = re.findall(r'(?:src|href)="(/static/[^\"]+)"', html)
    assert urls
    from html import unescape
    from urllib.parse import parse_qs, urlsplit
    assert all(parse_qs(urlsplit(unescape(url)).query) == {'v': [__version__], 'brand': ['shiori']} for url in urls)


def test_windows_icon_contains_all_display_sizes_and_alpha():
    with Image.open(asset_path('shiori.ico')) as image:
        assert image.format == 'ICO'
        assert image.ico.sizes() == {(size, size) for size in SIZES}
        for size in SIZES:
            frame = image.ico.getimage((size, size)).convert('RGBA')
            assert frame.getpixel((0, 0))[3] < 20
            assert frame.getpixel((size // 2, size // 2))[3] > 250


def test_tray_is_the_full_colour_tile_on_any_taskbar():
    light = tray_image(dark_theme=False)
    dark = tray_image(dark_theme=True)
    assert light.size == dark.size == (64, 64)
    # The picture reads on light and dark taskbars alike, so both themes get the same image.
    assert light.tobytes() == dark.tobytes()
    assert light.getpixel((0, 0))[3] == 0 and light.getpixel((32, 32))[3] == 255
    # The rounded tile fills the small canvas instead of shrinking inside it.
    with Image.open(ASSETS / 'tray-16.png') as tiny:
        left, _, right, _ = tiny.getchannel('A').getbbox()
        assert right - left >= 14


def test_icons_come_from_the_owners_artwork_master():
    with Image.open(ASSETS / 'icon-master.png') as master:
        assert master.size == (512, 512) and master.mode == 'RGBA'
        alpha = master.getchannel('A')
        # The white page around the rounded tile is transparent, the tile itself opaque.
        assert alpha.getpixel((0, 0)) == alpha.getpixel((511, 511)) == 0
        assert alpha.getpixel((256, 256)) == 255


def _svg_parts(path):
    """The SVG markup with its embedded PNG taken out, and that PNG's pixels.

    zlib builds differ between platforms, so the PNG bytes inside the SVG are
    not stable; its markup and its pixels are.
    """
    import base64
    import io
    import re
    text = path.read_text('utf-8').replace('\r\n', '\n')
    match = re.search(r'href="data:image/png;base64,([^"]+)"', text)
    assert match, path
    with Image.open(io.BytesIO(base64.b64decode(match.group(1)))) as image:
        pixels = (image.size, image.mode, image.tobytes())
    return text.replace(match.group(1), ''), pixels


def test_asset_builder_reproduces_bundled_vectors_and_rasters(tmp_path):
    spec = importlib.util.spec_from_file_location('shiori_build_icons', ROOT / 'scripts' / 'build_icons.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(tmp_path, preview=False)
    for kind in ('app', 'avatar', 'mark', 'tray', 'tray-light'):
        assert not (tmp_path / f'{kind}.svg').read_bytes().count(b'\r'), 'the builder writes LF'
        assert _svg_parts(tmp_path / f'{kind}.svg') == _svg_parts(ASSETS / f'{kind}.svg')
        ElementTree.parse(tmp_path / f'{kind}.svg')
        for size in SIZES:
            with Image.open(tmp_path / f'{kind}-{size}.png') as rebuilt, Image.open(ASSETS / f'{kind}-{size}.png') as bundled:
                assert rebuilt.size == bundled.size == (size, size)
                assert rebuilt.tobytes() == bundled.tobytes()


def test_source_and_wheel_configuration_include_nested_icons():
    spec = importlib.util.spec_from_file_location('shiori_package_source', ROOT / 'scripts' / 'package_source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    selected = {path.relative_to(ROOT).as_posix() for path in module.source_files(ROOT)}
    assert 'shiori/static/assets/brand/shiori.ico' in selected
    assert 'shiori/static/assets/brand/app.svg' in selected
    assert 'shiori/static/assets/brand/tray-16.png' in selected
    import tomllib
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))
    assert 'static/assets/brand/*' in config['tool']['setuptools']['package-data']['shiori']
