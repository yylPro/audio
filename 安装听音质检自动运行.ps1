param(
    [string]$TaskName = "听音质检后台",
    [string]$ProjectDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
$launcherPath = Join-Path $ProjectDir "运行听音质检后台.ps1"
$powerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "未找到后台启动脚本：$launcherPath"
}

$action = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`"" `
    -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "自动处理元宝派上传的录音，执行转写、质检和报表生成。"
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "已安装并启动：$TaskName"
Write-Host "运行日志：$(Join-Path $ProjectDir 'state\logs\audio_quality_worker.log')"
