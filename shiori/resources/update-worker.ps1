param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$taskConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskResult = @{status='failed'; message='自动更新未完成，请查看安装记录后重试。'; at=[DateTime]::UtcNow.ToString('o')}
$taskResult.download_id = $taskConfig.download_id
$taskParentExited = $false
try {
    if ($taskConfig.parent_pid) {
        $taskParent = $null
        try { $taskParent = [Diagnostics.Process]::GetProcessById([int]$taskConfig.parent_pid) } catch [ArgumentException] {}
        if ($taskParent -and -not $taskParent.WaitForExit(120000)) {
            $taskResult.status='unknown'
            $taskResult.message='原进程仍未退出，未安装或重复启动软件。'
            throw '原进程退出等待超时'
        }
    }
    $taskParentExited = $true
    $taskHashStream = [IO.File]::OpenRead($taskConfig.installer)
    $taskHasher = [Security.Cryptography.SHA256]::Create()
    try { $taskHash = [BitConverter]::ToString($taskHasher.ComputeHash($taskHashStream)).Replace('-', '').ToLowerInvariant() }
    finally { $taskHashStream.Dispose(); $taskHasher.Dispose() }
    if ($taskHash -ne $taskConfig.sha256) { throw '安装文件校验失败，未执行。' }
    $taskInfo = New-Object System.Diagnostics.ProcessStartInfo
    $taskInfo.FileName = $taskConfig.installer
    $taskInfo.Arguments = $taskConfig.arguments
    $taskInfo.WorkingDirectory = Split-Path -LiteralPath $taskConfig.installer
    $taskInfo.UseShellExecute = $false
    $taskInfo.CreateNoWindow = $true
    $taskInfo.RedirectStandardOutput = $true
    $taskInfo.RedirectStandardError = $true
    $taskProcess = [System.Diagnostics.Process]::Start($taskInfo)
    $taskOut = $taskProcess.StandardOutput.ReadToEndAsync()
    $taskErr = $taskProcess.StandardError.ReadToEndAsync()
    if (-not $taskProcess.WaitForExit(1200000)) {
        $taskResult.status='unknown'
        $taskResult.message='安装器仍未退出，请先检查安装进程；未重复启动软件或再次安装。'
    } else {
        $taskResult.exit_code = $taskProcess.ExitCode
        $taskLog = $taskOut.Result + "`r`n" + $taskErr.Result
        if ($taskLog.Length -gt 32000) { $taskLog = $taskLog.Substring($taskLog.Length - 32000) }
        [IO.File]::WriteAllText($taskConfig.log, $taskLog, [Text.UTF8Encoding]::new($false))
        if ($taskProcess.ExitCode -eq 0) {
            $taskResult.status='completed'
            $taskResult.message='安装完成，已尝试重新打开 shiori。'
        } else { $taskResult.message='安装失败，已尝试重新打开原安装目录中的软件。请查看安装记录。' }
    }
} catch {
    # Keep exception details local; the UI exposes only a fixed safe message.
    [IO.File]::WriteAllText($taskConfig.log, [string]$_, [Text.UTF8Encoding]::new($false))
}
# Also reopen the existing program after a failed verification/installer start,
# but never compete with an installer or original process still running.
if ($taskParentExited -and $taskResult.status -ne 'unknown') {
    try {
        $taskLaunch = New-Object System.Diagnostics.ProcessStartInfo
        $taskLaunch.FileName = $taskConfig.application
        $taskLaunch.Arguments = $taskConfig.launch_arguments
        $taskLaunch.WorkingDirectory = Split-Path -LiteralPath $taskConfig.application
        $taskLaunch.UseShellExecute = $false
        $taskLaunch.CreateNoWindow = $true
        [void][System.Diagnostics.Process]::Start($taskLaunch)
    } catch {
        if ($taskResult.status -eq 'completed') { $taskResult.message='安装完成，但未能重新打开；请从快捷方式启动 shiori。' }
        else { $taskResult.message='更新失败，未能重新打开原程序；请查看安装记录并从快捷方式启动。' }
        [IO.File]::AppendAllText($taskConfig.log, "`r`n"+[string]$_, [Text.UTF8Encoding]::new($false))
    }
}
$taskTemporary = $taskConfig.result + '.tmp'
[IO.File]::WriteAllText($taskTemporary, ($taskResult | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $taskTemporary -Destination $taskConfig.result -Force
