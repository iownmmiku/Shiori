[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$DataDir = '',
    [ValidateRange(1, 65535)][int]$Port,
    [string]$BindHost = '',
    [switch]$SetupOnly,
    [switch]$Reinstall,
    [switch]$NoBrowser,
    [switch]$Browser,
    [switch]$NoTray,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskVenv = Join-Path $taskRoot '.venv'
$taskPython = Join-Path $taskVenv 'Scripts\python.exe'
$taskRequirements = Join-Path $taskRoot 'requirements-desktop.txt'
if ($DataDir) { $DataDir = [System.IO.Path]::GetFullPath($DataDir) }
if ($NoBrowser -and $Browser) { throw '-NoBrowser 和 -Browser 不能同时使用。' }

function Test-TaskPython([string]$Executable, [string[]]$PrefixArguments = @()) {
    try {
        $taskVersion = & $Executable @PrefixArguments -c "import sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor))" 2>$null
        return ($LASTEXITCODE -eq 0 -and ($taskVersion -join '').Trim() -match '^3\.(11|12|13)$')
    } catch { return $false }
}

Push-Location -LiteralPath $taskRoot
try {
    if ($Stop) {
        if (-not (Test-Path -LiteralPath $taskPython)) { throw '尚未准备源码环境；停止操作不会自动安装依赖。' }
        $taskStopArgs = @('-m', 'shiori', '--stop', '--no-browser', '--no-tray')
        if ($DataDir) { $taskStopArgs += @('--data-dir', $DataDir) }
        & $taskPython @taskStopArgs
        if ($LASTEXITCODE -ne 0) { throw '停止请求失败，请查看数据目录中的 runtime.log。' }
        return
    }
    if (Test-Path -LiteralPath $taskPython) {
        if (-not (Test-TaskPython $taskPython)) { throw '已有 .venv 不是 Python 3.11–3.13。请保留 data，手动更换虚拟环境后重试。' }
    } else {
        $taskBootstrap = ''
        $taskBootstrapArgs = @()
        if ($Python) {
            if (-not (Test-TaskPython $Python)) { throw '-Python 指定的解释器须为 Python 3.11–3.13。' }
            $taskBootstrap = $Python
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            foreach ($taskVersionTag in @('-3.12','-3.13','-3.11')) {
                if (Test-TaskPython -Executable 'py' -PrefixArguments @($taskVersionTag)) { $taskBootstrap = 'py'; $taskBootstrapArgs = @($taskVersionTag); break }
            }
        }
        if (-not $taskBootstrap) {
            foreach ($taskCandidate in @('python3.12', 'python3.13', 'python3.11', 'python')) {
                if ((Get-Command $taskCandidate -ErrorAction SilentlyContinue) -and (Test-TaskPython $taskCandidate)) {
                    $taskBootstrap = $taskCandidate; break
                }
            }
        }
        if (-not $taskBootstrap) { throw '未找到 Python 3.11–3.13。请安装 Python 3.11–3.13（勾选 pip），再运行本脚本；无需 Node 或 Git。' }
        Write-Host '正在创建独立 Python 环境，不更改系统 Python，也不覆盖 data。'
        & $taskBootstrap @taskBootstrapArgs -m venv $taskVenv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $taskPython)) { throw '创建虚拟环境失败，请确认 Python 已安装 venv/pip。' }
    }
    $taskDigestText = [System.IO.File]::ReadAllText($taskRequirements) + [System.IO.File]::ReadAllText((Join-Path $taskRoot 'requirements-server.txt'))
    $taskHasher = [System.Security.Cryptography.SHA256]::Create()
    try { $taskDigest = [System.BitConverter]::ToString($taskHasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($taskDigestText))).Replace('-', '').ToLowerInvariant() } finally { $taskHasher.Dispose() }
    $taskMarker = Join-Path $taskVenv '.shiori-requirements.sha256'
    $taskInstalled = if (Test-Path -LiteralPath $taskMarker) { ([System.IO.File]::ReadAllText($taskMarker)).Trim() } else { '' }
    if ($Reinstall -or $taskInstalled -ne $taskDigest) {
        Write-Host '正在安装源码依赖（首次需要网络）。密钥和聊天数据保持原样。'
        & $taskPython -m pip install --disable-pip-version-check -r $taskRequirements
        if ($LASTEXITCODE -ne 0) { throw '安装依赖失败。请检查网络或 pip 镜像；修复后可重复执行本脚本。' }
        [System.IO.File]::WriteAllText($taskMarker, $taskDigest, [System.Text.UTF8Encoding]::new($false))
    }
    & $taskPython -c 'import fastapi, uvicorn, httpx, websockets, multipart, jsonschema, pypdf; import PIL'
    if ($LASTEXITCODE -ne 0) { throw '依赖检查失败，请使用 -Reinstall 重新安装；不会覆盖 data。' }
    if ($SetupOnly) { Write-Host '源码环境已就绪。之后运行 start.ps1，或再次执行此脚本启动。'; return }
    $taskLaunchArgs = @('-m', 'shiori')
    if ($DataDir) { $taskLaunchArgs += @('--data-dir', $DataDir) }
    if ($Port) { $taskLaunchArgs += @('--port', [string]$Port) }
    if ($BindHost) { $taskLaunchArgs += @('--host', $BindHost) }
    if ($NoBrowser) { $taskLaunchArgs += '--no-browser' }
    if ($Browser) { $taskLaunchArgs += '--browser' }
    if ($NoTray) { $taskLaunchArgs += '--no-tray' }
    Write-Host '正在启动 shiori。默认使用独立桌面窗口；关闭后留在托盘，可从托盘退出或使用本脚本 -Stop。'
    & $taskPython @taskLaunchArgs
    if ($LASTEXITCODE -ne 0) { throw 'shiori 启动失败，请查看数据目录中的 runtime.log。' }
} finally { Pop-Location }
