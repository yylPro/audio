# 听音质检

功能三用于接收元宝群 `@Bot #听音检测` 音频附件，归档音频、ASR 转写、DeepSeek 整理角色、Python 规则评分，并写入每日质检 Excel。

主流程：

```text
元宝群消息 -> inbound_adapter -> audio_tasks -> worker -> ASR -> DeepSeek整理 -> Python规则 -> 每日Excel
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

完整原始转录文本另存：

```text
audio_quality_output\transcript_texts\YYYY-MM-DD\QA-..._原始转录.txt
```

单实例锁文件：

```text
state\audio_quality_worker.lock
```
