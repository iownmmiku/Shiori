"""Prepare a verified, data-free payload for the one-file Windows installer."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath


def prepare(archive: Path, destination: Path, version: str):
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('发行ZIP损坏')
        names = [entry.orig_filename for entry in z.infolist()]
        if 'shiori/shiori.exe' not in names:
            raise ValueError('发行ZIP缺少程序入口')
        for name in names:
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != 'shiori' or '..' in parts or '\\' in name or ':' in name:
                raise ValueError('发行ZIP含不安全路径')
            if any(part.lower() in ('data', 'admin.token') or part.startswith('.qa-') for part in parts):
                raise ValueError('发行ZIP含用户或测试数据')
    destination.mkdir(parents=True, exist_ok=True)
    payload = destination / 'payload.zip'
    shutil.copy2(archive, payload)
    manifest = {'schema': 1, 'version': version, 'sha256': hashlib.sha256(payload.read_bytes()).hexdigest(),
                'root': 'shiori', 'executable': 'shiori.exe'}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.destination, args.version)))
