"""Shiori's character art in the web console: present, referenced and shipped."""
import importlib.util
import re
import tomllib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'shiori' / 'static'
ART = STATIC / 'assets' / 'character'


def test_every_character_image_the_stylesheet_uses_exists_and_has_transparency_where_needed():
    css = (STATIC / 'character.css').read_text('utf-8')
    used = set(re.findall(r"/static/assets/character/([\w.-]+)", css))
    assert {'avatar.jpg', 'portrait.png', 'chibi-quiet.png', 'chibi-pout.png', 'chibi-angry.png'} <= used
    for name in used:
        path = ART / name
        assert path.is_file(), name
        with Image.open(path) as image:
            if name.endswith('.png'):
                # Cut-outs sit on cream, plum and pink panels: corners must be see-through.
                alpha = image.convert('RGBA').getchannel('A')
                assert alpha.getpixel((0, 0)) == 0 and alpha.getpixel((image.width - 1, 0)) == 0, name
            assert max(image.size) <= 1100, name
        assert path.stat().st_size < 700 * 1024, name


def test_character_stylesheet_loads_last_with_the_release_label():
    html = (STATIC / 'index.html').read_text('utf-8')
    sheets = re.findall(r'<link rel="stylesheet" href="/static/([\w.-]+)\?', html)
    assert sheets[-1] == 'character.css'


def test_character_art_ships_in_source_and_wheel_packages():
    spec = importlib.util.spec_from_file_location('shiori_package_source', ROOT / 'scripts' / 'package_source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    selected = {path.relative_to(ROOT).as_posix() for path in module.source_files(ROOT)}
    for path in ART.iterdir():
        if path.suffix in {'.png', '.jpg'}:
            assert f'shiori/static/assets/character/{path.name}' in selected, path.name
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))
    assert 'static/assets/character/*' in config['tool']['setuptools']['package-data']['shiori']


def test_the_teal_palette_is_gone_from_the_console_except_the_app_icon():
    teal = re.compile(r'#(?:17a596|0d6c64|e2f3ef|123b3f|52dfd0|39c5bb|138781)', re.I)
    offenders = [p.name for p in STATIC.glob('*.css') if p.name != 'brand.css' and teal.search(p.read_text('utf-8'))]
    assert offenders == []


def test_download_page_and_readme_point_at_images_that_exist():
    site = ROOT / 'site'
    html = (site / 'index.html').read_text('utf-8')
    for src in set(re.findall(r'(?:src|href)="(assets/[^"]+)"', html)):
        # The Pages workflow copies the app's character art into assets/character.
        path = ART / src.split('/', 2)[2] if src.startswith('assets/character/') else site / src
        assert path.is_file(), src
    workflow = (ROOT / '.github' / 'workflows' / 'pages.yml').read_text('utf-8')
    assert 'cp -r shiori/static/assets/character _site/assets/character' in workflow
    readme = (ROOT / 'README.md').read_text('utf-8')
    banner = re.search(r'!\[[^\]]*\]\((site/assets/[^)]+)\)', readme)
    assert banner and (ROOT / banner.group(1)).is_file()
