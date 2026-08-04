from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .audio_quality_processor import (
    AudioQualityError,
    AudioSubmission,
    IncomingAudioMessage,
    attach_archived_file,
    extract_accepted_number,
    extract_employee_name,
    format_received_reply,
    init_db,
    load_json,
    reserve_task,
    sha256_file,
    validate_attachment,
)
from .text_compat import repair_multiline_text, repair_text


def _normalize(value: Any) -> str:
    return repair_text(value)


def _validate_event(event: dict[str, Any], config: dict[str, Any]) -> tuple[list[Path], str]:
    if not config.get("enabled", False):
        raise AudioQualityError("听音质检功能未启用")
    group_id = _normalize(event.get("group_id"))
    allowed = {_normalize(item) for item in config.get("allowed_group_ids", []) if _normalize(item)}
    if allowed and group_id not in allowed:
        raise AudioQualityError("当前群未启用听音质检")
    text = repair_multiline_text(event.get("text"))
    # Keep canonical commands enabled even when an older config was saved with
    # a legacy code-page conversion and its aliases became mojibake.
    triggers = [_normalize(item) for item in config.get("trigger_aliases", []) if _normalize(item)]
    triggers.extend(["#听音检测", "#听音质检", "#录音质检"])
    if not any(trigger in text for trigger in triggers):
        raise AudioQualityError("消息缺少听音检测触发词")
    known = {item for item in ("#沟通记录", "#听音检测", "#听音质检", "#录音质检", "#工单催办") if item in text}
    if known - set(triggers):
        raise AudioQualityError("一条消息只能执行一个功能，请只保留一个触发词")
    if config.get("require_structured_at", True) and not bool(event.get("is_at_bot")):
        raise AudioQualityError("请使用元宝派原生 AT Bot 后再发送 #听音检测")
    media_paths = event.get("media_paths")
    if not isinstance(media_paths, list):
        raise AudioQualityError("消息附件字段格式不合法")
    maximum = max(1, int(config.get("max_attachments", 10)))
    if not media_paths:
        raise AudioQualityError("消息缺少音频附件")
    if len(media_paths) > maximum:
        raise AudioQualityError(f"每条听音检测消息最多可包含 {maximum} 个音频附件")
    # Paths are already UTF-8 filesystem paths.  Do not run mojibake repair on
    # them: a valid Chinese filename can be transformed into a nonexistent one.
    sources = [Path(str(item)) for item in media_paths]
    if any(not source.is_file() for source in sources):
        raise AudioQualityError("元宝派临时音频不存在或已被清理")
    message_id = _normalize(event.get("message_id"))
    if not message_id:
        raise AudioQualityError("消息ID为空，无法进行幂等处理")
    return sources, message_id


def _archive_audio(source: Path, digest: str, inbox: Path) -> Path:
    destination_dir = inbox / digest
    destination = destination_dir / source.name
    destination_dir.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if sha256_file(destination) != digest:
            raise AudioQualityError("归档路径已存在但文件哈希不一致")
        return destination
    temporary = destination_dir / f".{source.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != digest:
            raise AudioQualityError("附件复制后哈希校验失败")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def ingest_event(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    sources, message_id = _validate_event(event, config)
    group_id = _normalize(event.get("group_id"))
    sender_name = repair_text(event.get("sender_name"))
    text = repair_multiline_text(event.get("text"))
    attachment_rules = {
        "supported_audio_suffixes": config.get("supported_audio_suffixes", [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr"]),
        "max_file_size_bytes": config.get("max_file_size_bytes", 200 * 1024 * 1024),
    }
    pending: list[tuple[Path, str, IncomingAudioMessage, AudioSubmission]] = []
    for source in sources:
        digest = sha256_file(source)
        message = IncomingAudioMessage(
            message_id=message_id,
            group_id=group_id,
            sender_user_id=_normalize(event.get("sender_user_id")),
            sender_name=sender_name,
            text=text,
            attachment_id=digest,
            filename=source.name,
            file_size=source.stat().st_size,
            attachment_hash=digest,
        )
        validate_attachment(message, attachment_rules)
        submission = AudioSubmission(
            extract_employee_name(message.filename, message.text) or message.sender_name,
            None,
            extract_accepted_number(message.text, message.filename),
        )
        pending.append((source, digest, message, submission))

    results: list[tuple[str, bool]] = []
    connection = init_db(Path(config["database_path"]))
    try:
        for source, digest, message, submission in pending:
            existing = connection.execute(
                "SELECT task_id FROM audio_tasks WHERE group_id=? AND attachment_hash=? ORDER BY created_at LIMIT 1",
                (group_id, digest),
            ).fetchone()
            if existing:
                task_id, created = reserve_task(connection, message, submission)
                results.append((task_id, created))
                continue
            archived = _archive_audio(source, digest, Path(config["audio_inbox"]))
            task_id, created = reserve_task(connection, message, submission)
            if created:
                attach_archived_file(connection, task_id, archived)
            results.append((task_id, created))
    finally:
        connection.close()
    task_ids = [task_id for task_id, _ in results]
    created_ids = [task_id for task_id, created in results if created]
    duplicate_ids = [task_id for task_id, created in results if not created]
    if len(results) == 1:
        task_id, created = results[0]
        reply = format_received_reply(task_id) if created else f"【听音质检】\n该音频已接收，无需重复提交。任务编号：{task_id}。"
    elif created_ids:
        reply = f"【听音质检】\n已接收 {len(created_ids)} 条录音，正在处理。任务编号：{'、'.join(created_ids)}。"
        if duplicate_ids:
            reply += f"其中 {len(duplicate_ids)} 条重复音频未重复创建任务。"
    else:
        reply = f"【听音质检】\n本次 {len(results)} 条录音均已接收，无需重复提交。任务编号：{'、'.join(task_ids)}。"
    return {
        "handled": True,
        "ok": True,
        "created": bool(created_ids),
        "task_id": task_ids[0],
        "task_ids": task_ids,
        "created_task_ids": created_ids,
        "duplicate_task_ids": duplicate_ids,
        "reply": reply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic Yuanbao audio-quality ingress adapter.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise AudioQualityError("入站事件必须是 JSON 对象")
        result = ingest_event(event, load_json(args.config))
    except Exception as exc:
        result = {"handled": True, "ok": False, "reply": f"【听音质检】接收失败：{exc}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
