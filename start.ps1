[CmdletBinding()]
param(
    [string]$DataDir = '',
    [ValidateRange(1, 65535)][int]$Port,
    [string]$BindHost = '',
    [switch]$NoBrowser,
    [switch]$Browser,
    [switch]$NoTray,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '尚未准备源码环境。请先运行 powershell -ExecutionPolicy Bypass -File .\scripts\setup-source.ps1 -SetupOnly；普通用户也可以使用安装版。'
}
$taskLaunchArgs = @('-m', 'shiori')
if ($NoBrowser -and $Browser) { throw '-NoBrowser 和 -Browser 不能同时使用。' }
if ($Port) { $taskLaunchArgs += @('--port', [string]$Port) }
if ($BindHost) { $taskLaunchArgs += @('--host', $BindHost) }
if ($DataDir) { $taskLaunchArgs += @('--data-dir', [System.IO.Path]::GetFullPath($DataDir)) }
if ($NoBrowser) { $taskLaunchArgs += '--no-browser' }
if ($Browser) { $taskLaunchArgs += '--browser' }
if ($NoTray) { $taskLaunchArgs += '--no-tray' }
if ($Stop) { $taskLaunchArgs += '--stop' }
Push-Location -LiteralPath $taskRoot
try {
    & $taskPython @taskLaunchArgs
    if ($LASTEXITCODE -ne 0) { throw 'shiori 运行失败，请查看所用数据目录中的 runtime.log。' }
} finally { Pop-Location }
