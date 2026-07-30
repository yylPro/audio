from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio_quality_processor import AudioQualityError, normalize_text


def build_delivery_prompt(message: str) -> str:
    marker = "[[YB_LINE_BREAK]]"
    if marker in message:
        raise AudioQualityError(f"完成通知包含保留换行标记：{marker}")
    encoded = message.replace("\r\n", "\n").replace("\r", "\n").replace("\n", marker)
    return f"将下面文本中的每个{marker}替换为一个真实换行，然后原样输出。不要解释、改写、增删或调用工具，只输出还原后的听音检测完成通知：" + encoded


def schedule_completion_notification(config: dict[str, Any], task_id: str, message: str) -> str | None:
    if not config or not config.get("enabled", False):
        return None
    openclaw_cmd = normalize_text(config.get("openclaw_cmd"))
    target = normalize_text(config.get("target"))
    if not openclaw_cmd or not Path(openclaw_cmd).exists():
        raise AudioQualityError(f"OpenClaw 命令不存在：{openclaw_cmd}")
    if not target.startswith("group:"):
        raise AudioQualityError(f"听音检测完成通知目标不合法：{target}")
    command = [
        openclaw_cmd, "cron", "add", "--name", f"audio-quality-done-{datetime.now():%Y%m%d-%H%M%S}-{task_id}",
        "--at", normalize_text(config.get("delay")) or "+5s", "--session", "isolated", "--light-context",
        "--model", normalize_text(config.get("model")) or "deepseek/deepseek-chat", "--thinking", "off",
        "--tools", normalize_text(config.get("tools_allow")) or "session_status",
        "--timeout-seconds", str(max(30, int(config.get("agent_timeout_seconds", 120)))),
        "--announce", "--channel", normalize_text(config.get("channel")) or "yuanbao",
        "--account", normalize_text(config.get("account")) or "default", "--to", target,
        "--delete-after-run", "--json", "--message", build_delivery_prompt(message),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(30, int(config.get("agent_timeout_seconds", 120))) + 30,
        env=env,
    )
    if completed.returncode != 0:
        raise AudioQualityError((completed.stderr or completed.stdout).strip() or "OpenClaw 完成通知创建失败")
    start = completed.stdout.find("{")
    if start < 0:
        raise AudioQualityError(f"OpenClaw 未返回 JSON：{completed.stdout.strip()}")
    payload = json.loads(completed.stdout[start:])
    job_id = payload.get("id")
    if not job_id:
        raise AudioQualityError(f"OpenClaw 完成通知缺少任务 ID：{payload}")
    return str(job_id)
