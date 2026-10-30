"""Generate the bundled guide from docs/USER_GUIDE.md; --check detects drift."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCS_URL = 'https://github.com/iownmmiku/Shiori/blob/main/docs/'


def render_guide(source: str) -> str:
    """Use public absolute links because the in-app renderer rejects local paths."""
    def link(match):
        label, target = match.groups()
        if target.startswith('#') or urlsplit(target).scheme:
            return match.group(0)
        return f'[{label}]({urljoin(DOCS_URL, target)})'

    output, in_code = [], False
    for line in source.splitlines(keepends=True):
        if line.startswith('```'):
            in_code = not in_code
        output.append(line if in_code else re.sub(r'(?<!!)\[([^\]]+)\]\(([^\s)]+)\)', link, line))
    return ''.join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    guide = ROOT/'shiori/static/guide.md'
    expected = render_guide((ROOT/'docs/USER_GUIDE.md').read_text('utf-8'))
    if args.check:
        if guide.read_text('utf-8') != expected:
            parser.exit(1, 'Bundled guide is stale. Run: python scripts/sync_guide.py\n')
        print('Bundled guide matches docs/USER_GUIDE.md')
    else:
        guide.write_text(expected, 'utf-8')
        print('Updated shiori/static/guide.md')


if __name__ == '__main__':
    main()
