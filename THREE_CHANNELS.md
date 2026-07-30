# 三功能通道与部署状态

## 总览

| 功能 | 入口 | 本地处理 | 模型/服务 | 当前状态 |
|---|---|---|---|---|
| 功能一：Excel工单催办 | `inbox`目录 | Python读取、过滤、分组、计数 | DeepSeek仅原样转发 | 已实现 |
| 功能二：回单整理 | 群内 `@Bot #回单整理` | 解析工单号/客户号码、写入已回单状态 | DeepSeek Chat | 框架已建立，输出规则待确认 |
| 功能三：录音转写与质检 | 群内音频文件或`audio-inbox` | 下载、转写任务、结果归档 | 阿里 FunASR + DeepSeek Chat | 已初步可用 |

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

功能二当前改为“回单整理”框架。入口目标是：

```text
@Bot #回单整理 工单号：... 客户号码：... 口语描述...
```

处理器会先解析工单号、客户号码和口语描述；待 DeepSeek 按业务输出规则整理成功后，把共享状态表中的对应工单设置为 `replied`。功能一下一轮催单会按工单号或客户号码跳过这些已回单工单。

当前仍等待真实输入输出样例来确认标准状态、固定格式、禁用表达和缺失字段处理方式。

## 功能三现状

功能三现在已经不是纯占位了，当前可用链路是：

`元宝群消息 -> inbound_adapter -> audio_tasks -> worker -> 阿里 FunASR -> DeepSeek 整理/评分 -> 每日Excel`

元宝插件支持普通文件附件下载，`.mp3/.wav/.m4a/.ogg` 等文件附件可进入处理链；语音消息气泡目前仍只会产生 `[voice]` 占位符，所以录音应尽量作为文件附件发送。

当前主 ASR 是 `aliyun_fun_asr`，本地 `faster-whisper` 仅保留为备用。实际运行请以 `audio_quality_config.json` 为准，`config.json` 里的功能状态字段只是总览，不是音频 worker 的主开关。

推荐启动前先确认：

1. `audio_quality.env` 已填写 `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY` 和阿里云 OSS 凭据。
2. `audio_quality_config.json` 中 `enabled=true`，并填写了真实群 `group_id`。
3. `audio_quality_runtime\\.venv` 可正常运行 `audio_quality.worker`。
4. `state\\logs\\audio_quality_worker.log` 可写。

常用启动命令：

```powershell
audio_quality_runtime\.venv\Scripts\python.exe -m audio_quality.worker --config audio_quality_config.json --once
audio_quality_runtime\.venv\Scripts\python.exe -m audio_quality.worker --config audio_quality_config.json --poll-seconds 3
```

如果要做计划任务，先用 `运行听音质检后台.ps1` 验证后台可持续重启，再装 `安装听音质检自动运行.ps1`。
