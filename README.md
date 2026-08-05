# 工单助手

项目分为三个清晰功能：

1. **功能一：Excel工单催办**：已经实现。读取业务 Excel、排除完成状态、按当前处理人生成原生 `@` 催办消息。
2. **功能二：回单整理**：入口已接入。群内原生 `@Bot #回单整理 ...` 会调用本地处理器，生成 Python 标准版和 DeepSeek 智能版；DeepSeek 校验成功后写入 `replied`。
3. **功能三：录音转写与质检**：已经初步可用，当前主链路是阿里 FunASR + DeepSeek；独立说明见 `audio_quality/README.md`。质检结果、日报和 DeepSeek 对比报表分别处理，报表导出失败不会丢失已完成的质检结果。

## 统一启动入口

日常使用请双击 `launch_console.cmd`，或运行 `work_order_console.ps1`。它们集中提供工单催办、两个自动运行任务、补丁安装和发送审计入口。中文名称的 `工单助手控制台.ps1` 与 `打开工单助手控制台.cmd` 保留为兼容入口；原有 `.ps1` 和 `.cmd` 脚本也继续保留，以免影响已经存在的计划任务或快捷方式。

```powershell
.\work_order_console.ps1
```

也可直接执行某一操作，例如：

```powershell
.\work_order_console.ps1 -Action InstallReminderSchedule -IntervalMinutes 5
```

完整部署状态和验收命令见 `THREE_CHANNELS.md`，永久规则见 `AGENT_RULES_WORK_ORDER.md`。

功能一保留两种运行路径：直接双击 `运行工单催办.cmd` 可随时预览或发送；运行 `install_scheduled_reminders.ps1` 可安装每 5 分钟扫描一次的后台定时任务。两者共用运行锁，撞车时后启动的一方会跳过本轮，不会并发处理。手动发送和当前定时脚本都可使用强制重发参数；定时任务实际以 `运行工单催办定时.ps1` 的参数为准，不应把文档中的送达记录描述理解为绝对去重保证。

Excel 数据不会发送给模型。只有 Python 已经生成的最终催办文本会交给指定模型原样转发。

## 双表催办规则

- 文件名包含“投诉”“当天到期”或“今日到期”：按当前处理人原生 `@`，列出该人的全部“工单流水号｜受理号码”。
- 文件名包含“过期”或“超期”：按当前处理人原生 `@`，只发送已过期未处理数量，不展开工单明细。
- 其他文件名：不处理并明确报错，避免把测试文件或未知业务表按错误模板发送。

双表应包含：`工单流水号`、`受理号码`、`当前处理工作组`、`当前处理人`、`本环节到期时限`。消息超过配置的字符数或人数限制时会自动拆分，不会截断工单。正式发送前会校验每位处理人的元宝 userId 映射。

当前不会按“历史已处理文件”自动跳过。`inbox` 中的支持文件会按文件名规则处理；待处理文件如何流转由人工或后续业务流程决定。

## 必要列

- 处理人：`当前处理人`、`受理人`或`受理员工`之一
- 状态：默认识别`工单状态`或`当前状态`；没有状态列时所有有效工单都视为待处理
- 工单号：默认识别`工单流水号`或`工单编号`；没有工单号列时仍可按行统计

列名、完成状态和昵称映射均可在 `config.json` 中修改。

## 安装

1. 将 `config.example.json` 复制为 `config.json`。
2. 修改目录、元宝群目标和 DeepSeek 模型名。
3. Python 需要 `openpyxl` 才能读取 `.xlsx`；标准库即可读取 `.csv`。本机共享依赖安装在 `D:\PythonPackages` 时，可先设置 `$env:PYTHONPATH='D:\PythonPackages'`，供多个项目复用。
4. 将待处理文件放入配置中的 `inbox` 目录。

## 预览（不会发送）

```powershell
python work_order_reminder.py --config config.json --keep
```

处理单个文件：

```powershell
python work_order_reminder.py --config config.json --file "D:\\export\\orders.xlsx"
```

## 开启投递

先确认 OpenClaw Gateway 和元宝派通道可用。然后将配置中的：

```json
"delivery": {
  "enabled": true
}
```

执行：

```powershell
python work_order_reminder.py --config config.json --send
```

必须同时满足配置 `enabled=true` 和命令行 `--send` 才会投递，防止误发。功能一默认使用 `openclaw message send` 直发固定催办文本，不再依赖模型复述；只有直发失败且 `fallback_to_cron=true` 时才回退到 cron。

## 消息示例

```text
【待处理工单催办】

 @叶于琳 当前待处理 12 单
 @张三 当前待处理 8 单

请及时联系并处理相关工单。
```

昵称必须与元宝派成员显示名称一致。如 Excel 姓名不同，可配置：

```json
"name_aliases": {
  "叶于琳（客服）": "叶于琳"
}
```

## 发送确认与文件流转

- SQLite 记录文件哈希、消息内容、投递引用和投递结果用于审计；后台定时任务遇到相同文件、相同目标群、相同消息内容已成功送达时会跳过，避免重复发同一批。
- 只阻止同一文件、同一目标群、同一消息内容仍处于 `sending` 状态时再次发送，避免重复点击产生并发投递。
- 默认成功后移入 `archive`，失败移入 `failed`。
- 预览模式永不移动文件；正式投递时可用 `--keep` 禁止移动。
- 如果某个文件被拆成多条消息，已成功送达的批次会逐条落库；后续重试只补发未成功的批次。
- 直发返回成功即视为脚本侧送达成功；cron 回退时仍会等待执行结果。最终仍应以群内实际收到和原生 AT 实际生效为验收标准。

## 成员映射

`yuanbao_members.json` 是昵称到 userId 的缓存，不是真实数据源。发送前会尝试从日志学习成员映射。

如果某个处理人查不到 userId，消息仍会发送他的工单，但不会在姓名前加 `@`，并会在本条消息末尾追加：

```text
某某某 没查到，请问是否在本群里面。
```

## Windows 任务计划程序

功能一支持两种并行保留的入口：

1. 手动：双击 `运行工单催办.cmd`。正式发送会强制发送本轮文件，即使同一内容之前发过；默认保留 `inbox` 文件，需要归档时选择归档发送选项。
2. 定时：执行：

```powershell
.\install_scheduled_reminders.ps1 -IntervalMinutes 5
```

定时任务运行日志位于 `state\logs\work_order_reminder_scheduled.log`。手动和定时任务可以同时保留，但不会同时处理同一批 `inbox` 文件；定时任务默认保留源文件，只用送达记录防重复。

## 录音质检报表与评分来源

- `audio_quality_config.json` 中的 `quality.deepseek_mode` 默认为 `parallel_compare`。
- `parallel_compare` 会同时保留 Python 规则评分和 DeepSeek 语义评分：正式日报默认使用 Python 规则评分，单独生成 `YYYY-MM-DD_降挽质检方案对比-语义评分.xlsx` 作为对比表。
- 正式日报新增“评分来源”列，标明当前行使用的是 `Python规则质检` 还是 `DeepSeek语义审核`。
- 录音文件名支持完整日期（如 `20260708`、`2026-07-08`）以及 `7月8日`、`7月8号` 格式。无法识别日期时仍可完成质检，但不会按日期生成对比表。
- 日报或对比表导出失败时，系统只重试报表导出（最多 3 次，间隔 0 秒、2 秒、10 秒），不会重复 ASR 或 DeepSeek 分析；仍失败会记录 `report_export_pending` 事件。当前不会自动建立后台补导出队列，需要根据事件或任务管理命令人工补导出。
- 对比表只有在 `deepseek_scoring.enabled=true`、模式为 `parallel_compare`（或其他对比模式）、DeepSeek 评分成功且文件名能解析出录音日期时才会生成；没有可识别日期的文件仍可完成质检，但不会生成按日期筛选的对比表。
