"""Portable link checks, including Windows junctions on Python 3.11."""
from pathlib import Path
import stat


def is_link(path):
    path=Path(path)
    try:
        info=path.lstat()
    except NotADirectoryError:
        # A later ancestor check reports the blocking ordinary file. Linux
        # lstat raises ENOTDIR here, while Windows generally reports ENOENT.
        return False
    except FileNotFoundError:
        return path.is_symlink()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0) & 0x400)
