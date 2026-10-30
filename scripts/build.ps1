param([switch]$PortableOnly, [switch]$InstallerOnly, [string]$OutputRoot = '')
$ErrorActionPreference = 'Stop'
if ($PortableOnly -and $InstallerOnly) { throw 'PortableOnly 与 InstallerOnly 不能同时使用' }
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
& $taskPython scripts/sync_guide.py --check
if ($LASTEXITCODE -ne 0) { throw '内置使用指南未同步，请先运行 python scripts/sync_guide.py' }
$taskVersion = (& $taskPython -c 'from shiori import __version__; print(__version__)').Trim()
$taskArtifactRoot = if ($OutputRoot) { [IO.Path]::GetFullPath((Join-Path $taskRoot $OutputRoot)) } else { Join-Path $taskRoot 'dist' }
$taskVersionResources = Join-Path $taskRoot ('build\version-v' + $taskVersion)
$taskAppVersionFile = Join-Path $taskVersionResources 'app.version.txt'
$taskInstallerVersionFile = Join-Path $taskVersionResources 'installer.version.txt'
$taskDistributionRoot = Join-Path $taskArtifactRoot ('v' + $taskVersion)
$taskOutput = Join-Path $taskDistributionRoot 'shiori'
$taskExistingData = Join-Path $taskOutput 'data'
$taskBrandAssets = Join-Path $taskRoot 'shiori\static\assets\brand'
$taskAppIcon = Join-Path $taskBrandAssets 'shiori.ico'
# Some managed Python runtimes load an additional CA policy file from beside
# ssl.py. Preserve that runtime's policy in the frozen app instead of leaving
# SSLContext construction dependent on a file outside the distribution.
$taskSslPolicy = ([string](& $taskPython -c "import ssl; from pathlib import Path; p = Path(ssl.__file__).with_name('codex-rejected-cas.pem'); print(p if p.is_file() else '')")).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Python SSL runtime policy discovery failed' }
$taskRuntimeData = @()
if ($taskSslPolicy) {
    $taskRuntimeData = @('--add-data', ($taskSslPolicy + ';.'))
}
if (-not (Test-Path -LiteralPath $taskAppIcon)) {
    & $taskPython scripts/build_icons.py
    if ($LASTEXITCODE -ne 0) { throw '品牌图标生成失败' }
}
if (Test-Path -LiteralPath $taskExistingData) {
    throw '发行目录已有用户数据。请先退出shiori、备份并将整个 shiori 文件夹移到其他位置，再重新构建。'
}
if (-not $InstallerOnly) {
    & $taskPython scripts/write_windows_version.py $taskAppVersionFile --version $taskVersion --kind app
    if ($LASTEXITCODE -ne 0) { throw 'Windows 程序版本资源生成失败' }
    & $taskPython -m PyInstaller --noconfirm --clean --onedir --windowed --name shiori --icon $taskAppIcon --version-file $taskAppVersionFile --distpath $taskDistributionRoot --add-data 'shiori/static;shiori/static' --add-data 'shiori/resources;shiori/resources' @taskRuntimeData --hidden-import mikubot --hidden-import qunyu --collect-submodules shiori --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --hidden-import pystray._win32 --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium --collect-all webview --collect-all pythonnet --collect-all clr_loader --collect-all botpy --collect-all tzdata --collect-all holidays launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows 打包失败' }
} elseif (-not (Test-Path -LiteralPath (Join-Path $taskOutput 'shiori.exe'))) {
    throw '请先构建完整程序后再使用 InstallerOnly'
}
Copy-Item -LiteralPath (Join-Path $taskRoot 'README.md') -Destination (Join-Path $taskOutput '使用说明.md')
Copy-Item -LiteralPath (Join-Path $taskRoot 'RELEASE_NOTES.md') -Destination (Join-Path $taskOutput '版本说明.md')
Copy-Item -LiteralPath (Join-Path $taskRoot 'README.md') -Destination (Join-Path $taskOutput 'README.md')
Copy-Item -LiteralPath (Join-Path $taskRoot 'shiori\resources\stop-portable.cmd') -Destination (Join-Path $taskOutput '退出 shiori.cmd')
Copy-Item -LiteralPath (Join-Path $taskRoot 'RELEASE_NOTES.md') -Destination (Join-Path $taskOutput 'RELEASE_NOTES.md')
if (Test-Path -LiteralPath (Join-Path $taskRoot 'TEST_REPORT.md')) {
    Copy-Item -LiteralPath (Join-Path $taskRoot 'TEST_REPORT.md') -Destination (Join-Path $taskOutput '测试报告.md')
    Copy-Item -LiteralPath (Join-Path $taskRoot 'TEST_REPORT.md') -Destination (Join-Path $taskOutput 'TEST_REPORT.md')
}
foreach ($taskFolder in @('docs', 'evals')) {
    $taskDestination = Join-Path $taskOutput $taskFolder
    New-Item -ItemType Directory -Path $taskDestination -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $taskRoot $taskFolder) -Force | Copy-Item -Destination $taskDestination -Recurse -Force
}
& $taskPython scripts/collect_notices.py $taskOutput
if ($LASTEXITCODE -ne 0) { throw '依赖声明收集失败' }
$taskArchive = Join-Path $taskArtifactRoot ('shiori-' + $taskVersion + '-Windows-x64.zip')
& $taskPython scripts/package_portable.py $taskOutput $taskArchive
if ($LASTEXITCODE -ne 0) { throw '便携包封装失败' }
Write-Output $taskArchive
if (-not $PortableOnly) {
    $taskInstallerPayload = Join-Path $taskRoot ('build\installer-v' + $taskVersion + '\installer_payload')
    & $taskPython scripts/prepare_installer.py $taskArchive $taskInstallerPayload --version $taskVersion
    if ($LASTEXITCODE -ne 0) { throw '安装包载荷校验失败' }
    $taskInstallerName = 'shiori-' + $taskVersion + '-Windows-Setup'
    & $taskPython scripts/write_windows_version.py $taskInstallerVersionFile --version $taskVersion --kind installer
    if ($LASTEXITCODE -ne 0) { throw 'Windows 安装器版本资源生成失败' }
    & $taskPython -m PyInstaller --noconfirm --clean --onefile --windowed --name $taskInstallerName --icon $taskAppIcon --version-file $taskInstallerVersionFile --distpath $taskArtifactRoot --workpath (Join-Path $taskRoot ('build\setup-v' + $taskVersion)) --add-data ($taskInstallerPayload + ';installer_payload') --add-data ($taskBrandAssets + ';shiori/static/assets/brand') @taskRuntimeData installer_launcher.py
    if ($LASTEXITCODE -ne 0) { throw '一键安装包构建失败' }
    $taskInstaller = Join-Path $taskArtifactRoot ($taskInstallerName + '.exe')
    foreach ($taskArtifact in @($taskArchive, $taskInstaller)) {
        $taskHash = (Get-FileHash -LiteralPath $taskArtifact -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText([IO.Path]::ChangeExtension($taskArtifact, '.sha256'), ($taskHash + '  ' + [IO.Path]::GetFileName($taskArtifact) + "`n"), [Text.Encoding]::ASCII)
    }
    Write-Output $taskInstaller
}
