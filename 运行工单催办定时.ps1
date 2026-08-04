param(
    [string]$ProjectDir = $PSScriptRoot,
    [int]$IntervalMinutes = 5,
    [switch]$Once
)

$ErrorActionPreference = "Continue"
$pythonPath = Join-Path $ProjectDir "audio_quality_runtime\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
}
$scriptPath = Join-Path $ProjectDir "work_order_reminder.py"
$configPath = Join-Path $ProjectDir "config.json"
$logDir = Join-Path $ProjectDir "state\logs"
$logPath = Join-Path $logDir "work_order_reminder_scheduled.log"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Location -LiteralPath $ProjectDir

function Invoke-ReminderOnce {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$startedAt] 定时工单催办启动"

    & $pythonPath $scriptPath --config $configPath --send --keep *>> $logPath
    $exitCode = $LASTEXITCODE

    $finishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$finishedAt] 定时工单催办结束，代码=$exitCode"
    return $exitCode
}

if ($Once) {
    exit (Invoke-ReminderOnce)
}

while ($true) {
    $exitCode = Invoke-ReminderOnce
    Start-Sleep -Seconds ([Math]::Max(60, $IntervalMinutes * 60))
}
