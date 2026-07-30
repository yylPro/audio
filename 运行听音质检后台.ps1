param(
    [string]$ProjectDir = $PSScriptRoot,
    [int]$RestartDelaySeconds = 10
)

$ErrorActionPreference = "Continue"
$pythonPath = Join-Path $ProjectDir "audio_quality_runtime\.venv\Scripts\python.exe"
$configPath = Join-Path $ProjectDir "audio_quality_config.json"
$logDir = Join-Path $ProjectDir "state\logs"
$logPath = Join-Path $logDir "audio_quality_worker.log"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Location -LiteralPath $ProjectDir

while ($true) {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$startedAt] 听音质检后台启动"

    & $pythonPath -m audio_quality.worker --config $configPath --poll-seconds 3 --debug --traceback *>> $logPath
    $exitCode = $LASTEXITCODE

    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$stoppedAt] 听音质检后台退出，代码=$exitCode，${RestartDelaySeconds}秒后重启"
    Start-Sleep -Seconds ([Math]::Max(1, $RestartDelaySeconds))
}
