# 功能二：回单整理

当前状态：回单整理框架已建立，业务输入输出样例和最终规则尚未确认，正式入口保持禁用。

## 边界

- OpenClaw Gateway 是唯一常驻进程。
- 本目录中的 Python 处理器由入站消息按需调用，执行完即退出。
- 每条消息必须自包含，不读取群历史。
- 输入消息格式：`@Bot #回单整理 工单号：... 客户号码：... 口语描述...`。也支持从口语里自动识别类似 `20260703174229X749589475` 的工单流水号和 11 位客户号码。
- 工单整理成功并写入 `replied` 后，功能一会按工单号或客户号码跳过对应催单。
- DeepSeek 生成失败只影响规范文本，不写入 `replied`。
- V1 不写外部工单系统，不物理删除 Excel 行。

## 待业务确认

启用前必须补齐：

1. 真实员工输入样本和最终认可文本。
2. 联系状态枚举。
3. 固定前后缀、标准称谓和禁用表达。
4. 各场景必填信息。
5. 工单处理人和元宝 `userId` 的授权规则。

配置位置：

- `communication_rules.json`：可维护业务规则。
- `prompts/communication_record.md`：DeepSeek 精简提示词。
- 根目录 `config.json` 的 `communication`：运行参数，当前 `enabled=false`。

`allowed_contact_statuses` 为空是故意的安全锁。业务规则未补齐时，处理器会拒绝模型结果。
当前规则已根据 `回单整理.xlsx` 中的少量样例进入 `draft` 状态，但仍需要更多真实输入输出继续校准。

## 入站事件契约

OpenClaw 适配层应给处理器提供以下字段：

```json
{
  "message_id": "元宝消息唯一ID",
  "group_id": "群ID",
  "sender_user_id": "发送者userId",
  "sender_name": "发送者昵称",
  "text": "@Bot #回单整理 工单号：... 客户号码：... 口语描述..."
}
```

适配层还必须确认消息包含对 Bot 的结构化原生 AT。当前 CLI 只用于离线验证，不替代插件侧 AT 校验。

## DeepSeek 调用契约

模型输入只包含固定提示词、当前场景的少量规则和当前消息的口语描述。不发送群历史、Excel、成员表或其他工单。

模型仅返回：

```json
{
  "status": "业务确认后的状态",
  "text": "可直接复制的规范文本",
  "missing": [],
  "warnings": []
}
```

不设置低 `max_tokens`；配置中的 `max_tokens` 保持 `null`。通过精简输入和输出格式控制消耗。

## 离线命令

仅验证输入，不写数据库：

```powershell
python communication\communication_processor.py validate --text "@Bot #回单整理 工单号：A1234 客户号码：13800000000 口语描述：已联系用户"
```

创建功能二数据表：

```powershell
python communication\communication_processor.py init-db --db state\assistant_v2.sqlite3
```

生成给模型的提示词：

```powershell
python communication\communication_processor.py prompt --text "@Bot #回单整理 18376697569，20260703174229X749589475，我处已于2026年7月11日18:24分电话13457142185联系客户..."
```

`process` 命令接收事件 JSON 和预生成的模型响应 JSON，用于业务规则确定后的端到端离线测试。当前占位规则会阻止正式模型结果通过。
