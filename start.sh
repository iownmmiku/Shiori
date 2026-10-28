#!/usr/bin/env bash
set -euo pipefail
task_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_python="$task_root/.venv/bin/python"
[[ -x "$task_python" ]] || { echo '请先运行 bash scripts/setup-source.sh --setup-only 准备环境。' >&2; exit 1; }
cd -- "$task_root"
exec "$task_python" -m shiori --no-browser --no-tray "$@"
