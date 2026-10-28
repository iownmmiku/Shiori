# Third-party notices and corresponding source

Shiori uses third-party software under each component's own license. Those
licenses and copyright notices are retained independently of the MIT license
for Shiori's original code, selected in the root [LICENSE](LICENSE). The MIT
license does not replace the licenses of bundled third-party components.

## pystray 0.19.5

- Upstream: [moses-palmer/pystray](https://github.com/moses-palmer/pystray).
- Exact release: [pystray 0.19.5](https://pypi.org/project/pystray/0.19.5/).
- Copyright (C) 2016-2022 Moses Palmér.
- License: GNU Lesser General Public License, version 3 or later, as stated in
  the source headers. Both the [GNU GPL](third_party/pystray/COPYING) and
  [GNU LGPL](third_party/pystray/COPYING.LGPL) texts accompany this distribution.
- Use: the Windows system tray icon. Shiori has not modified this library.

The complete Python package source used by this release is included in
[`third_party/pystray/pystray/`](third_party/pystray/pystray), together with
upstream package metadata and a SHA-256 source manifest. These files were copied
from the installed 0.19.5 distribution and checked against its wheel `RECORD`.
The source archive, portable archive and installed program all include these
library sources and license texts. Binary distributions also contain a
`licenses/` directory with notices collected from the installed dependencies.

### Replace the library and rebuild Shiori

The same release page that offers the Windows installer also offers the
corresponding `shiori-<version>-source.zip`, including application code and build
scripts. It is available at the same cost as the binaries. Download that exact
version's source archive, extract it and open PowerShell in its root directory.
Use Python 3.12 and follow [source deployment](docs/SOURCE_DEPLOY.md) to create the
virtual environment and install the runtime and build dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

Copy `third_party/pystray/pystray` to a separate working directory and make the
library changes you want there. Replace `.venv\Lib\site-packages\pystray` with
that modified package, preserving its import-compatible package layout. For
example, if your modified package is `C:\src\modified-pystray\pystray`:

```powershell
Copy-Item -LiteralPath C:\src\modified-pystray\pystray -Destination .venv\Lib\site-packages -Recurse -Force
```

Remove `.venv\Lib\site-packages\pystray\__pycache__` if present so Python will
load your changed source. For source deployment, run
`.\.venv\Scripts\python.exe -m shiori --browser`. To produce a new Windows program
containing that library, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

The build uses the library in that virtual environment. Keep your modified
library source and its notices with any modified binary you distribute, and
update this notice and the source manifest to describe the changes. The original
library copy supplied here remains available for comparison. No activation key,
signing key or approval from Shiori is needed to run or rebuild a local copy.

For the purpose of replacing or modifying this LGPL library, you are permitted
to modify, rebuild, run and recombine the accompanying Shiori application code
with the modified library. Shiori imposes no additional restriction on
modification of the library portions or on reverse engineering necessary to
debug those modifications. This specific permission ensures that the provided
application code can be used for LGPL recombination; it does not replace the
library's license. The rest of Shiori's original code is licensed under MIT.

## Character Skill Producer adaptation

The persona distillation method in `shiori/resources/csp/` is adapted from
Character Skill Producer (CSP), copyright (c) 2026 qian-gugugaga. Its MIT license
is retained in [`shiori/resources/csp/LICENSE`](shiori/resources/csp/LICENSE), and the
adapted Skill identifies the source. The adaptation supports work characters
and uploaded corpus material; it does not grant tool or computer permissions.

## Other runtime components

The binary distribution's `licenses/INDEX.md` lists bundled Python components
and the retained license files. This includes Python, Tcl/Tk used by the
installer, the web server and client libraries, Pillow, pywebview and its Windows
bridge, and QQ's `qq-botpy` SDK. `qq-botpy`'s actual license text is MIT, copyright
(c) 2021 Tencent. Runtime dependencies retain their own licenses; installing or
packaging them does not transfer their ownership to shiori.
