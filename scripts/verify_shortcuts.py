"""Exercise production shortcut generation with real Windows WScript COM.

Only the two Windows special-folder lookups are redirected to a QA directory.
The production EncodedCommand is otherwise executed verbatim. No shortcut is
created on the user's actual desktop or in the actual Start menu.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shiori import installer  # noqa: E402


def powershell_string(value: str) -> str:
    """Represent arbitrary Unicode data without interpolating shell syntax."""
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return "([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')))"


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("This QA requires Windows and the real WScript.Shell COM server.")
    qa = ROOT / ".qa-shortcuts-v7" / ("run-" + uuid.uuid4().hex[:12])
    qa.mkdir(parents=True)
    desktop, programs = qa / "Desktop", qa / "Programs"
    target = qa / "安装 中文 'quote' $(whoami) `符号"
    target.mkdir()
    # A harmless placeholder gives COM an existing target; it is never launched.
    (target / "shiori.exe").write_bytes(b"QA shortcut target; never execute")
    real_run = subprocess.run
    calls = []

    def redirected_run(args, **kwargs):
        if args[:6] != ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"] or len(args) != 7:
            raise AssertionError("Unexpected production subprocess invocation")
        original = base64.b64decode(args[6], validate=True).decode("utf-16le")
        replacements = {
            "[Environment]::GetFolderPath('Desktop')": (desktop, 1),
            "[Environment]::GetFolderPath('Programs')": (programs, 2),
        }
        redirected = original
        for expression, (destination, expected_count) in replacements.items():
            assert redirected.count(expression) == expected_count, expression
            redirected = redirected.replace(expression, powershell_string(str(destination)))
        assert "GetFolderPath" not in redirected, "An unredirected special-folder lookup remains"
        (qa / "production.ps1").write_text(original, "utf-8")
        (qa / "redirected.ps1").write_text(redirected, "utf-8")
        encoded = base64.b64encode(redirected.encode("utf-16le")).decode("ascii")
        result = real_run([*args[:6], encoded], **kwargs)
        calls.append(result)
        if result.returncode:
            raise RuntimeError("Shortcut PowerShell failed: " + result.stderr.decode("utf-8", errors="replace"))
        return result

    with patch.object(installer.subprocess, "run", redirected_run):
        warnings = installer.create_shortcuts(target, desktop=True, start_menu=True)
    assert not warnings, warnings
    assert len(calls) == 1
    links = sorted(qa.rglob("*.lnk"))
    expected_links = {desktop / "shiori.lnk", programs / "shiori" / "shiori.lnk", programs / "shiori" / "退出 shiori.lnk"}
    assert set(links) == expected_links, [str(path) for path in links]
    # Read properties through the actual COM server as a separate invocation.
    read_script = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$shell = New-Object -ComObject WScript.Shell
$paths = %s | ConvertFrom-Json
$items = @($paths | ForEach-Object {
    $link = $shell.CreateShortcut($_)
    [PSCustomObject]@{Path=$_; TargetPath=$link.TargetPath; WorkingDirectory=$link.WorkingDirectory; Arguments=$link.Arguments}
})
ConvertTo-Json -InputObject $items -Compress
""" % powershell_string(json.dumps([str(path) for path in links], ensure_ascii=False))
    encoded = base64.b64encode(read_script.encode("utf-16le")).decode("ascii")
    result = real_run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                      capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Shortcut property read failed: " + result.stderr.decode("utf-8", errors="replace"))
    properties = json.loads(result.stdout.decode("utf-8-sig"))
    assert len(properties) == 3
    for item in properties:
        assert Path(item["TargetPath"]) == target / "shiori.exe", item
        assert Path(item["WorkingDirectory"]) == target, item
        expected_arguments = "--stop --no-browser" if Path(item["Path"]).name == "退出 shiori.lnk" else ""
        assert item["Arguments"] == expected_arguments, item
        assert "token" not in item["Arguments"].casefold(), item
    report = {"passed": True, "qa_directory": str(qa), "links": properties,
              "special_folder_redirects": {"Desktop": str(desktop), "Programs": str(programs)},
              "real_user_shortcuts_touched": False,
              "path_characters_checked": ["Chinese", "spaces", "single quote", "$()", "backtick"]}
    (qa / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    (qa.parent / "latest-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
