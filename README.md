# 工单助手

项目分为三个清晰功能：

1. **功能一：Excel工单催办**：已经实现。读取业务 Excel、排除完成状态、按当前处理人生成原生 `@` 催办消息。
2. **功能二：沟通记录整理**：业务规则和输出模板待确认，当前未启用。
3. **功能三：录音转写与质检**：已经初步可用，当前主链路是阿里 FunASR + DeepSeek；独立说明见 `audio_quality/README.md`。

完整部署状态和验收命令见 `THREE_CHANNELS.md`，永久规则见 `AGENT_RULES_WORK_ORDER.md`。

日常使用无需输入 Python 命令，直接双击 `运行工单催办.cmd`，选择预览或正式发送。也可以把单个 `.xlsx` 文件拖到该 CMD 上，只处理该文件。

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
3. Python 需要 `openpyxl` 才能读取 `.xlsx`；标准库即可读取 `.csv`。
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

先确认 OpenClaw 已注册并认证 `deepseek/deepseek-chat`。然后将配置中的：

```json
"delivery": {
  "enabled": true
}
```

执行：

```powershell
python work_order_reminder.py --config config.json --send
```

必须同时满足配置 `enabled=true` 和命令行 `--send` 才会投递，防止误发。每个消息包只创建一个模型任务。

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

- SQLite 记录文件哈希、消息内容、cron job 和投递结果用于审计，不作为历史文件跳过依据。
- 只阻止同一文件、同一目标群、同一消息内容仍处于 `sending` 状态时再次发送，避免重复点击产生并发投递。
- 默认成功后移入 `archive`，失败移入 `failed`。
- 预览模式永不移动文件；正式投递时可用 `--keep` 禁止移动。
- 正式发送会等待 cron 执行结果；`status=ok` 且 `deliveryStatus=delivered` 才视为脚本侧送达成功。最终仍应以群内实际收到和原生 AT 实际生效为验收标准。

## 成员映射

`yuanbao_members.json` 是昵称到 userId 的缓存，不是真实数据源。发送前会尝试从日志学习成员映射。

如果某个处理人查不到 userId，消息仍会发送他的工单，但不会在姓名前加 `@`，并会在本条消息末尾追加：

```text
某某某 没查到，请问是否在本群里面。
```

## Windows 任务计划程序

验证完成后，可每 5 分钟运行：

```powershell
python "D:\\WorkOrderAssistant\\work_order_reminder.py" --config "D:\\WorkOrderAssistant\\config.json" --send
```

第一版建议先人工运行预览，再开启任务计划程序。
