from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .communication_processor import (
    CommunicationError,
    IncomingMessage,
    build_python_result,
    format_dual_reply,
    generate_deepseek_result,
    init_db,
    load_json,
    mark_generation_failed,
    normalize_text,
    parse_submission,
    reserve_submission,
    save_model_result,
)


KNOWN_FEATURE_TRIGGERS = {
    "#回单整理",
    "#沟通记录",
    "#听音检测",
    "#听音质检",
    "#录音质检",
    "#工单催办",
}


def _communication_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("communication", {})
    if not isinstance(value, dict):
        raise CommunicationError("根配置缺少 communication 对象")
    return value


def _load_env_file(path: Path | None) -> None:
    if not path or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _validate_event(event: dict[str, Any], config: dict[str, Any], rules: dict[str, Any]) -> IncomingMessage:
    communication = _communication_config(config)
    if not communication.get("enabled", False):
        raise CommunicationError("回单整理功能未启用")

    group_id = normalize_text(event.get("group_id"))
    if not group_id:
        raise CommunicationError("事件缺少群ID")

    text = str(event.get("text", ""))
    trigger = normalize_text(communication.get("trigger") or rules.get("trigger") or "#回单整理")
    if trigger not in text:
        raise CommunicationError(f"消息缺少触发词：{trigger}")
    if any(item in text for item in KNOWN_FEATURE_TRIGGERS - {trigger}):
        raise CommunicationError("一条消息只能执行一个功能，请只保留一个触发词")

    if communication.get("require_native_at", True) and not bool(event.get("is_at_bot")):
        raise CommunicationError("请使用元宝派原生 AT Bot 后再发送 #回单整理")

    message = IncomingMessage(
        message_id=normalize_text(event.get("message_id")),
        group_id=group_id,
        sender_user_id=normalize_text(event.get("sender_user_id")),
        sender_name=normalize_text(event.get("sender_name")) or "未提供姓名",
        text=text,
    )
    for field_name in ("message_id", "sender_user_id"):
        if not getattr(message, field_name):
            raise CommunicationError(f"事件缺少字段：{field_name}")
    return message


def _resolve_path(config: dict[str, Any], communication: dict[str, Any], key: str, default: Path) -> Path:
    value = normalize_text(communication.get(key))
    return Path(value) if value else default


def _model_name(communication: dict[str, Any]) -> str:
    configured = normalize_text(communication.get("model") or "deepseek-chat")
    return configured.rsplit("/", 1)[-1]


def _failure_reply(order_id: str, python_result_text: str) -> str:
    return "\n".join(
        [
            "【回单整理】",
            f"工单号：{order_id}",
            "",
            "【Python标准版】",
            python_result_text,
            "",
            "DeepSeek智能版暂未生成，当前未登记为已回单，请稍后重试。",
        ]
    )


def handle_event(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise CommunicationError("入站事件必须是 JSON 对象")

    communication = _communication_config(config)
    env_file = normalize_text(communication.get("env_file"))
    if env_file:
        _load_env_file(Path(env_file))
    rules_path = _resolve_path(
        config,
        communication,
        "rules",
        Path(__file__).with_name("communication_rules.json"),
    )
    prompt_path = _resolve_path(
        config,
        communication,
        "prompt",
        Path(__file__).with_name("prompts") / "communication_record.md",
    )
    rules = load_json(rules_path)
    message = _validate_event(event, config, rules)
    submission = parse_submission(message.text, rules)

    paths = config.get("paths", {})
    if not isinstance(paths, dict) or not normalize_text(paths.get("state_db")):
        raise CommunicationError("根配置缺少 paths.state_db")
    database_path = Path(str(paths["state_db"]))
    business_type = normalize_text(communication.get("business_type")) or "default"

    connection = init_db(database_path)
    try:
        task_id, created = reserve_submission(
            connection,
            message,
            submission,
            business_type,
            require_assignment_match=bool(communication.get("require_order_assignment_match", False)),
        )
        if not created:
            return {
                "handled": True,
                "ok": True,
                "created": False,
                "duplicate": True,
                "task_id": task_id,
                "reply": f"【回单整理】\n该消息已处理，无需重复提交。任务编号：{task_id}。",
            }

        python_result = build_python_result(submission, rules)
        prompt_text = prompt_path.read_text(encoding="utf-8-sig")
        api_key_env = normalize_text(communication.get("api_key_env")) or "DEEPSEEK_API_KEY"
        base_url = normalize_text(communication.get("base_url")) or "https://api.deepseek.com/chat/completions"
        timeout_seconds = max(10, int(communication.get("timeout_seconds", 90)))
        try:
            deepseek_result = generate_deepseek_result(
                submission,
                rules,
                prompt_text,
                api_key=os.environ.get(api_key_env),
                base_url=base_url,
                model=_model_name(communication),
                timeout_seconds=timeout_seconds,
            )
            save_model_result(connection, task_id, deepseek_result, message.sender_user_id, business_type)
        except Exception as exc:
            mark_generation_failed(
                connection,
                task_id,
                f"{api_key_env}: {exc}",
                message.sender_user_id,
                business_type,
            )
            return {
                "handled": True,
                "ok": False,
                "created": True,
                "task_id": task_id,
                "generation_failed": True,
                "reply": _failure_reply(submission.order_id, python_result.text),
            }
    finally:
        connection.close()

    return {
        "handled": True,
        "ok": True,
        "created": True,
        "task_id": task_id,
        "status": "replied",
        "reply": format_dual_reply(submission.order_id, python_result, deepseek_result),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Feature two Yuanbao communication-record ingress adapter.")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
        result = handle_event(event, load_json(args.config.resolve()))
    except Exception as exc:
        result = {
            "handled": True,
            "ok": False,
            "reply": f"【回单整理】处理失败：{exc}",
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
