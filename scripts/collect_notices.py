"""Bundle installed dependency license files with a portable distribution."""
from importlib import metadata
from pathlib import Path
import hashlib
import json
import re
import shutil
import sys

destination = Path(sys.argv[1]) / 'licenses'
destination.mkdir(parents=True, exist_ok=True)
rows = ['# Bundled Python dependencies', '', 'License texts are retained in the subdirectories below. This index is generated from installed package metadata.', '']
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
    name = dist.metadata['Name']
    if name.casefold() == 'shiori':
        continue  # The application is not a bundled third-party dependency.
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    copied = []
    for file in dist.files or []:
        if not any(word in str(file).lower() for word in ('license', 'copying', 'notice')):
            continue
        source = Path(dist.locate_file(file))
        if source.is_file() and source.suffix.lower() not in ('.py', '.pyc', '.pyd', '.dll'):
            target = destination / safe_name / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target.name)
    rows.append(f'- {name} {dist.version}: ' + (', '.join(sorted(set(copied))) or 'See upstream package metadata'))
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    shutil.copyfile(python_license, destination / 'Python-LICENSE.txt')
tk_license = Path(sys.base_prefix) / 'tcl' / 'tk8.6' / 'license.terms'
if tk_license.exists():
    shutil.copyfile(tk_license, destination / 'Tcl-Tk-LICENSE.txt')
    rows.append('- Tcl/Tk (Windows installer): Tcl-Tk-LICENSE.txt; additional runtime notices in Python-LICENSE.txt')
(destination / 'INDEX.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')

# The tray library is LGPL. Ship its exact source and recombination instructions
# beside the binary, in addition to the general dependency license collection.
project_root = Path(__file__).resolve().parent.parent
library_root = project_root / 'third_party' / 'pystray'
manifest = json.loads((library_root / 'SOURCE_MANIFEST.json').read_text('utf-8'))
if manifest.get('name') != 'pystray' or manifest.get('version') != '0.19.5':
    raise ValueError('Unexpected pystray corresponding source version')
source_names = set(manifest['files']) | {'SOURCE_MANIFEST.json', 'README.md'}
for name in sorted(source_names):
    source = library_root / name
    if (not source.is_file() or source.is_symlink()
            or not source.resolve().is_relative_to(library_root.resolve())):
        raise ValueError('Unsafe pystray corresponding source path')
    expected = manifest['files'].get(name)
    if expected and hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError('pystray corresponding source checksum mismatch: ' + name)
    target = Path(sys.argv[1]) / 'third_party' / 'pystray' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
shutil.copyfile(project_root / 'THIRD_PARTY_NOTICES.md', Path(sys.argv[1]) / 'THIRD_PARTY_NOTICES.md')
shutil.copyfile(project_root / 'LICENSE', Path(sys.argv[1]) / 'LICENSE')
