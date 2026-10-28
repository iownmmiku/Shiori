"""Generate PyInstaller's Windows version resource using only the stdlib."""
from __future__ import annotations

import argparse
from pathlib import Path
import re


def render_version_info(version: str, kind: str) -> str:
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Windows release version must be major.minor.patch')
    parts = tuple(int(part) for part in version.split('.'))
    if any(part > 65535 for part in parts):
        raise ValueError('Windows version components must be at most 65535')
    if kind not in {'app', 'installer'}:
        raise ValueError('Version resource kind must be app or installer')
    numeric = (*parts, 0)
    name = 'shiori' if kind == 'app' else f'shiori-{version}-Windows-Setup'
    description = 'Shiori Desktop' if kind == 'app' else 'Shiori Setup'
    strings = {
        'FileDescription': description,
        'FileVersion': version,
        'InternalName': name,
        'OriginalFilename': name + '.exe',
        'ProductName': 'Shiori',
        'ProductVersion': version,
    }
    entries = ',\n'.join(f'          StringStruct({key!r}, {value!r})' for key, value in strings.items())
    return f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric!r},
    prodvers={numeric!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904b0', [
{entries}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'''


def write_version_info(destination: Path, version: str, kind: str) -> None:
    content = render_version_info(version, kind)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--kind', choices=('app', 'installer'), required=True)
    args = parser.parse_args()
    write_version_info(args.destination, args.version, args.kind)
