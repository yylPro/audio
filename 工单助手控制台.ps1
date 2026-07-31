[CmdletBinding()]
param(
    [ValidateSet("Menu", "Reminder", "InstallReminderSchedule", "AudioWorker", "InstallAudioSchedule", "NativeAtPatch", "CommunicationIngressPatch", "InstallRules", "Audit")]
    [string]$Action = "Menu",
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot

function Invoke-ProjectScript([string]$Name, [string[]]$Arguments = @()) {
    $path = Join-Path $projectDir $Name
    if (-not (Test-Path -LiteralPath $path)) { throw "未找到脚本：$path" }
    & $path @Arguments
}

function Invoke-Action([string]$SelectedAction) {
    switch ($SelectedAction) {
        "Reminder" { & (Join-Path $projectDir "运行工单催办.cmd") }
        "InstallReminderSchedule" { Invoke-ProjectScript "install_scheduled_reminders.ps1" @("-IntervalMinutes", $IntervalMinutes) }
        "AudioWorker" { Invoke-ProjectScript "运行听音质检后台.ps1" }
        "InstallAudioSchedule" { Invoke-ProjectScript "安装听音质检自动运行.ps1" }
        "NativeAtPatch" { Invoke-ProjectScript "install_yuanbao_native_at_patch.ps1" }
        "CommunicationIngressPatch" {
            $python = Join-Path $projectDir "audio_quality_runtime\.venv\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $python)) { throw "未找到已配置 Python 环境：$python" }
            & $python (Join-Path $projectDir "install_yuanbao_communication_ingress_patch.py")
            if ($LASTEXITCODE -ne 0) { throw "回单整理入站补丁安装失败，退出码：$LASTEXITCODE" }
        }
        "InstallRules" { Invoke-ProjectScript "install_agent_rules.ps1" }
        "Audit" { & (Join-Path $projectDir "查看发送审计.cmd") }
        default { throw "不支持的操作：$SelectedAction" }
    }
}

if ($Action -ne "Menu") {
    Invoke-Action $Action
    exit 0
}

while ($true) {
    Clear-Host
    Write-Host "工单助手控制台" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "1. 立即运行工单催办"
    Write-Host "2. 安装/更新工单催办定时任务"
    Write-Host "3. 前台运行听音质检后台"
    Write-Host "4. 安装/更新听音质检自动运行"
    Write-Host "5. 安装元宝原生 @ 补丁"
    Write-Host "6. 安装回单整理入站补丁"
    Write-Host "7. 安装 OpenClaw 工单规则"
    Write-Host "8. 查看发送审计"
    Write-Host "0. 退出"
    $choice = Read-Host "请选择"

    $actions = @{ "1" = "Reminder"; "2" = "InstallReminderSchedule"; "3" = "AudioWorker"; "4" = "InstallAudioSchedule"; "5" = "NativeAtPatch"; "6" = "CommunicationIngressPatch"; "7" = "InstallRules"; "8" = "Audit" }
    if ($choice -eq "0") { break }
    if (-not $actions.ContainsKey($choice)) {
        Write-Host "无效选择。" -ForegroundColor Yellow
        Start-Sleep -Seconds 1
        continue
    }

    try { Invoke-Action $actions[$choice] }
    catch { Write-Host "操作失败：$($_.Exception.Message)" -ForegroundColor Red }
    Read-Host "按 Enter 返回菜单" | Out-Null
}
