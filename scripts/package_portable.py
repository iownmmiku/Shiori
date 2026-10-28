"""Build portable ZIPs with canonical forward-slash names on every host."""
import argparse
from pathlib import Path
import zipfile


def package(source: Path, archive: Path):
    source, archive = Path(source).resolve(), Path(archive).resolve()
    if not (source / 'shiori.exe').is_file():
        raise ValueError('发行目录缺少 shiori.exe')
    files = sorted(p for p in source.rglob('*') if p.is_file())
    for p in files:
        parts = p.relative_to(source).parts
        if p.is_symlink() or any(x.casefold() in ('data', 'admin.token') or x.startswith('.qa-') for x in parts):
            raise ValueError('发行目录包含用户数据、测试数据或链接')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for p in files:
            output.write(p, 'shiori/' + p.relative_to(source).as_posix())
    return len(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    print(f'Packaged {package(args.source, args.archive)} files')
