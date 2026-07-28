$ErrorActionPreference = "Stop"

$source = Join-Path $PSScriptRoot "AGENT_RULES_WORK_ORDER.md"
$target = "D:\OpenClaw\workspace\AGENTS.md"
$startMarker = "<!-- WORK_ORDER_ASSISTANT_RULES_START -->"
$endMarker = "<!-- WORK_ORDER_ASSISTANT_RULES_END -->"

if (-not (Test-Path -LiteralPath $source)) {
    throw "规则源文件不存在：$source"
}

$targetDirectory = Split-Path -Parent $target
New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null

$existing = ""
if (Test-Path -LiteralPath $target) {
    $existing = Get-Content -LiteralPath $target -Raw -Encoding UTF8
    $backup = "$target.before-work-order-rules.$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
    Copy-Item -LiteralPath $target -Destination $backup -Force
    Write-Host "已备份：$backup"
}

$rules = Get-Content -LiteralPath $source -Raw -Encoding UTF8
$block = "$startMarker`r`n$rules`r`n$endMarker"
$pattern = [regex]::Escape($startMarker) + ".*?" + [regex]::Escape($endMarker)

if ($existing -match $pattern) {
    $updated = [regex]::Replace(
        $existing,
        $pattern,
        [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $block },
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    )
    Write-Host "已更新现有工单助手规则。"
}
elseif ([string]::IsNullOrWhiteSpace($existing)) {
    $updated = $block
    Write-Host "已创建工单助手规则。"
}
else {
    $updated = $existing.TrimEnd() + "`r`n`r`n" + $block
    Write-Host "已追加工单助手规则，其他规则保持不变。"
}

Set-Content -LiteralPath $target -Value $updated -Encoding UTF8
Write-Host "规则位置：$target"
Write-Host "请重启 OpenClaw Gateway 使规则生效。"
