#!/usr/bin/env bash
# 无头源码部署；桌面壳和托盘不属于服务器依赖。
set -euo pipefail
task_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_python="${SHIORI_PYTHON:-python3}"
task_venv="$task_root/.venv"
task_setup_only=0
task_reinstall=0
task_launch_args=()
while (($#)); do
  case "$1" in
    --setup-only) task_setup_only=1; shift ;;
    --reinstall) task_reinstall=1; shift ;;
    --python) (($# >= 2)) || { echo '请在 --python 后填写 Python 解释器路径。' >&2; exit 2; }; task_python="$2"; shift 2 ;;
    --) shift; task_launch_args+=("$@"); break ;;
    *) task_launch_args+=("$1"); shift ;;
  esac
done
cd -- "$task_root"
if [[ ! -x "$task_venv/bin/python" ]]; then
  command -v "$task_python" >/dev/null 2>&1 || { echo '需要 Python 3.11–3.13；可设置 SHIORI_PYTHON 或传入 --python /path/to/python3.12。' >&2; exit 1; }
  [[ "$("$task_python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" =~ ^3\.(11|12|13)$ ]] || { echo '请使用 Python 3.11、3.12 或 3.13。' >&2; exit 1; }
  "$task_python" -m venv "$task_venv"
fi
task_python="$task_venv/bin/python"
[[ "$("$task_python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" =~ ^3\.(11|12|13)$ ]] || { echo '已有 .venv 不是 Python 3.11–3.13；请保留 data，手动更换虚拟环境。' >&2; exit 1; }
task_digest="$("$task_python" -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("requirements-server.txt").read_bytes()).hexdigest())')"
task_marker="$task_venv/.shiori-requirements.sha256"
task_installed=''
if [[ -f "$task_marker" ]]; then IFS= read -r task_installed < "$task_marker" || true; fi
if [[ "$task_reinstall" == 1 || "$task_installed" != "$task_digest" ]]; then
  "$task_python" -m pip install --disable-pip-version-check -r "$task_root/requirements-server.txt"
  printf '%s\n' "$task_digest" > "$task_marker"
fi
"$task_python" -c 'import fastapi, uvicorn, httpx, websockets, multipart, jsonschema, pypdf; import PIL'
if [[ "$task_setup_only" == 1 ]]; then echo '源码环境已就绪，之后运行 bash start.sh。'; exit 0; fi
exec "$task_python" -m shiori --no-browser --no-tray "${task_launch_args[@]}"
