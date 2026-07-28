# 三功能通道与部署状态

## 总览

| 功能 | 入口 | 本地处理 | 模型/服务 | 当前状态 |
|---|---|---|---|---|
| 功能一：Excel工单催办 | `inbox`目录 | Python读取、过滤、分组、计数 | DeepSeek仅原样转发 | 已实现 |
| 功能二：沟通记录整理 | 待确认 | 无 | DeepSeek Chat | 旧规则已删除，当前未启用 |
| 功能三：录音转写与质检 | 群内音频文件或`audio-inbox` | 下载、转写任务、结果归档 | ASR + DeepSeek Chat | 等待选择ASR |

## DeepSeek官方插件

```powershell
& "D:\OpenClaw\openclaw.cmd" plugins install "@openclaw/deepseek-provider" --pin
& "D:\OpenClaw\openclaw.cmd" plugins registry --refresh
& "D:\OpenClaw\openclaw.cmd" plugins enable deepseek
& "D:\OpenClaw\openclaw.cmd" models auth login --provider deepseek --method api-key
& "D:\OpenClaw\openclaw.cmd" models list --all --provider deepseek --plain
& "D:\OpenClaw\openclaw.cmd" models set "deepseek/deepseek-chat"
& "D:\OpenClaw\openclaw.cmd" models status --json
```

配置完成后停止并重新启动前台 Gateway：

```powershell
& "D:\OpenClaw\openclaw.cmd" gateway
```

## 安装永久规则

推荐运行幂等安装脚本：

```powershell
cd "D:\代维\工单提醒"
.\install_agent_rules.ps1
```

脚本会备份并更新：

```text
D:\OpenClaw\workspace\AGENTS.md
```

它使用边界标记更新自己的规则区块，不覆盖其他已有规则。修改后重启 Gateway。

## 功能一验收

1. 把 Excel 放入 `D:\代维\工单提醒\inbox`。
2. 预览：

```powershell
cd "D:\代维\工单提醒"
& "C:\Users\14137\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\work_order_reminder.py --config .\config.json --keep
```

3. 确认统计结果后，将 `delivery.enabled` 改成 `true`，再执行：

```powershell
& "C:\Users\14137\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\work_order_reminder.py --config .\config.json --send
```

## 功能二现状

原有沟通记录规则和固定输出模板已经删除。业务部门确认输入字段、输出格式、缺失信息处理方式和隐私要求后，再重新设计规则并开展验收。

## 功能三现状

元宝插件支持普通文件附件下载，`.mp3/.wav/.ogg` 可作为文件处理；语音消息气泡目前只产生 `[voice]` 占位符。因此录音应作为文件附件发送。

DeepSeek是文本模型，不能代替ASR。选择阿里云、腾讯云或其他ASR后，还需要实现：提交音频、轮询任务、保存逐字稿，再把逐字稿交给DeepSeek。未配置ASR前，功能三必须保持`enabled=false`。
