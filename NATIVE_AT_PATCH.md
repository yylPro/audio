# 元宝派原生 @ 自动补丁

## 新成员自动发现

发送前，补丁会通过元宝插件的 `getGroupMemberList` 接口刷新目标群成员，并将“昵称 -> userId”合并写入 `yuanbao_members.json`。接口内置 5 分钟缓存；查询失败时保留旧映射和日志学习结果，不删除已有成员。

如果消息中存在无法解析的 `@昵称`，发送会明确失败，不会把普通文本 `@` 误报为原生 `@` 成功。

适用版本：`openclaw-plugin-yuanbao 2.17.0`

## 方式一：PowerShell

把整个“工单提醒”目录复制到目标电脑，在 PowerShell 中执行：

```powershell
cd "D:\代维\工单提醒"

.\install_yuanbao_native_at_patch.ps1 `
  -OpenClawCmd "D:\OpenClaw\openclaw.cmd"
```

如果项目不在当前目录：

```powershell
.\install_yuanbao_native_at_patch.ps1 `
  -OpenClawCmd "D:\OpenClaw\openclaw.cmd" `
  -ProjectDir "E:\工单提醒"
```

脚本会自动定位当前加载的元宝插件、检查版本、备份文件、应用补丁、校验 JavaScript，并重启 Gateway。

## 方式二：CMD / 双击

在资源管理器中双击：

```text
安装原生AT补丁.cmd
```

或在 CMD 中执行：

```cmd
cd /d D:\代维\工单提醒
安装原生AT补丁.cmd --openclaw D:\OpenClaw\openclaw.cmd
```

CMD 会自动寻找 Codex 内置 Python、`py -3` 或系统 `python`。

## 方式三：直接使用 Python

Windows、Linux 或无法执行 PowerShell 脚本的环境可以运行：

```cmd
python install_yuanbao_native_at_patch.py ^
  --openclaw D:\OpenClaw\openclaw.cmd ^
  --project-dir D:\代维\工单提醒
```

如果 OpenClaw CLI 无法查询插件位置，可以直接指定插件根目录：

```cmd
python install_yuanbao_native_at_patch.py ^
  --plugin-root "D:\OpenClaw\state\npm\projects\...\node_modules\openclaw-plugin-yuanbao" ^
  --project-dir "D:\代维\工单提醒" ^
  --skip-restart
```

成员发言或被人工 @ 后，昵称和 userId 会自动写入 `yuanbao_members.json`。

## 恢复

```powershell
.\install_yuanbao_native_at_patch.ps1 `
  -OpenClawCmd "D:\OpenClaw\openclaw.cmd" `
  -Restore
```

Python 恢复方式：

```cmd
python install_yuanbao_native_at_patch.py --restore
```

## 更新后处理

OpenClaw 或元宝插件更新后，先确认插件版本。如果仍为 `2.17.0`，重新运行安装命令。版本不一致时脚本会停止，避免破坏新版插件。
