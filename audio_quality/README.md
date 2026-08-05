# 听音质检

功能三用于接收元宝群 `@Bot #听音检测` 音频附件，归档音频、ASR 转写、DeepSeek 整理角色、Python 规则评分，并写入每日质检 Excel；启用对比模式时还会生成 DeepSeek 语义评分对比表。

主流程：

```text
元宝群消息 -> inbound_adapter -> audio_tasks -> worker -> ASR -> DeepSeek整理 -> Python规则/DeepSeek语义评分 -> 日报与可选对比表
```

当前主 ASR 是 `aliyun_fun_asr`。本地 `faster-whisper` 只保留为显式备用，不在主流程中使用。

启动前建议先确认：

1. `audio_quality_config.json` 中 `enabled=true`。
2. `audio_quality.env` 已填写 API Key 和 OSS 凭据。
3. `audio_quality_runtime\\.venv` 里的依赖可正常运行。
4. 目标群的 `group_id` 已填入配置。

常用命令：

```powershell
audio_quality_runtime\.venv\Scripts\python.exe -m audio_quality.worker --config audio_quality_config.json --once
audio_quality_runtime\.venv\Scripts\python.exe -m audio_quality.worker --config audio_quality_config.json --poll-seconds 3
```

后台方式：

```powershell
.\运行听音质检后台.ps1
.\安装听音质检自动运行.ps1
```

每日报表写入：

```text
audio_quality_output\YYYY-MM-DD_降挽质检情况.xlsx
```

当 `quality.deepseek_mode=parallel_compare` 且 DeepSeek 评分成功、录音文件名包含可识别日期时，还会写入：

```text
audio_quality_output\YYYY-MM-DD_降挽质检方案对比-语义评分.xlsx
```

正式日报默认使用 Python 规则评分，并在“评分来源”列标明来源；对比表同时列出 Python 与 DeepSeek 的评分和证据。支持 `YYYYMMDD`、`YYYY-MM-DD`、`7月8日`、`7月8号` 等日期格式。

日报或对比表导出失败时只重试导出（最多 3 次，间隔 0 秒、2 秒、10 秒），不会重复 ASR 或 DeepSeek；三次失败会记录 `report_export_pending`，当前需人工补导出。

完整原始转录文本另存：

```text
audio_quality_output\transcript_texts\YYYY-MM-DD\QA-..._原始转录.txt
```

单实例锁文件：

```text
state\audio_quality_worker.lock
```
