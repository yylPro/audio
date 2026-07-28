param(
  [string]$TaskName = "工单催办",
  [string]$ProjectDir = "D:\代维\工单提醒"
)

Write-Host "请在 Windows 任务计划程序中配置："
Write-Host "工作目录: $ProjectDir"
Write-Host "命令: python work_order_reminder.py --config config.json --send"
Write-Host "建议先手工运行 运行工单催办.cmd 验证。"
