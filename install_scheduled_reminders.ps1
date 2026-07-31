param(
  [string]$TaskName = "工单催办",
  [string]$ProjectDir = $PSScriptRoot,
  [int]$IntervalMinutes = 5,
  [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$launcherPath = Join-Path $ProjectDir "运行工单催办定时.ps1"
$powerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $launcherPath)) {
  throw "未找到定时启动脚本：$launcherPath"
}

$action = New-ScheduledTaskAction `
  -Execute $powerShellPath `
  -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -IntervalMinutes {1}' -f $launcherPath, $IntervalMinutes) `
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
  -Description "每隔 $IntervalMinutes 分钟扫描 inbox 并发送工单催办；与手动 CMD 共用运行锁。"
if ($WhatIf) {
  Write-Host "Would install scheduled task: $TaskName"
  Write-Host "Interval: $IntervalMinutes minutes"
  return
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "已安装并启动：$TaskName"
Write-Host "间隔：$IntervalMinutes 分钟"
$logDisplayPath = Join-Path $ProjectDir 'state\logs\work_order_reminder_scheduled.log'
Write-Host "运行日志：$logDisplayPath"
