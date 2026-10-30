"""The in-app guide must not drift from the current source deployment guide."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from shiori import __version__
from scripts.sync_guide import render_guide


ROOT = Path(__file__).resolve().parents[1]


def test_bundled_guide_matches_current_canonical_guide_and_repository():
    source = (ROOT/'docs/USER_GUIDE.md').read_text('utf-8')
    bundled = (ROOT/'shiori/static/guide.md').read_text('utf-8')
    assert bundled == render_guide(source)
    assert f'# Shiori {__version__} 使用指南' in source
    assert f'shiori-{__version__}-Windows-Setup.exe' in bundled
    assert 'github.com/iownmmiku/mikubot' not in bundled
    for url in re.findall(r'\]\(([^\s)]+)\)', bundled):
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' or url.startswith('#'), url
        prefix = '/iownmmiku/Shiori/blob/main/'
        if parsed.netloc == 'github.com' and parsed.path.startswith(prefix):
            assert (ROOT/unquote(parsed.path.removeprefix(prefix))).is_file(), url


def test_guide_generator_keeps_external_links_and_code_examples():
    source = '[部署](SOURCE_DEPLOY.md#linux) [日志](../RELEASE_NOTES.md) [外部](https://example.com/)\n```text\n[示例](example.md)\n```\n'
    result = render_guide(source)
    assert '[部署](https://github.com/iownmmiku/Shiori/blob/main/docs/SOURCE_DEPLOY.md#linux)' in result
    assert '[日志](https://github.com/iownmmiku/Shiori/blob/main/RELEASE_NOTES.md)' in result
    assert '[外部](https://example.com/)' in result
    assert '[示例](example.md)' in result
