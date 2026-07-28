from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_RULES = Path(__file__).with_name("communication_rules.json")
DEFAULT_PROMPT = Path(__file__).with_name("prompts") / "communication_record.md"


class CommunicationError(ValueError):
    """Raised when an incoming record or model result cannot be accepted."""


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    group_id: str
    sender_user_id: str
    sender_name: str
    text: str


@dataclass(frozen=True)
class ParsedSubmission:
    order_id: str
    raw_content: str


@dataclass(frozen=True)
class ModelResult:
    status: str
    text: str
    missing: list[str]
    warnings: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise CommunicationError(f"JSON root must be an object: {path}")
    return value


def normalize_text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).strip().split())


def parse_submission(text: str, rules: dict[str, Any]) -> ParsedSubmission:
    trigger = normalize_text(rules.get("trigger", "#沟通记录"))
    if trigger not in text:
        raise CommunicationError(f"消息缺少触发词 {trigger}")

    order_match = re.search(r"(?:工单号|工单流水号|工单编号)\s*[：:]\s*([^\s，,；;]+)", text)
    if not order_match:
        raise CommunicationError("消息缺少工单号，格式示例：工单号：202607240001")
    order_id = normalize_text(order_match.group(1))
    pattern = str(rules.get("order_id_pattern", r"[A-Za-z0-9_-]{4,64}"))
    if not re.fullmatch(pattern, order_id):
        raise CommunicationError(f"工单号格式不合法：{order_id}")

    content_match = re.search(r"(?:沟通情况|原始情况|回单内容)\s*[：:]\s*(.+)", text, re.DOTALL)
    if not content_match:
        raise CommunicationError("消息缺少沟通情况，格式示例：沟通情况：已联系用户……")
    raw_content = normalize_text(content_match.group(1))
    minimum = int(rules.get("minimum_content_characters", 4))
    if len(raw_content) < minimum:
        raise CommunicationError(f"沟通情况过短，至少需要 {minimum} 个字符")
    return ParsedSubmission(order_id=order_id, raw_content=raw_content)


def validate_model_result(payload: dict[str, Any], rules: dict[str, Any]) -> ModelResult:
    statuses = rules.get("allowed_contact_statuses", [])
    if not statuses:
        raise CommunicationError("业务规则尚未配置：allowed_contact_statuses 为空")

    status = normalize_text(payload.get("status"))
    text = normalize_text(payload.get("text"))
    missing = payload.get("missing", [])
    warnings = payload.get("warnings", [])
    if status not in statuses:
        raise CommunicationError(f"模型返回了未允许的联系状态：{status or '<空>'}")
    if not text:
        raise CommunicationError("模型未返回规范文本")
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise CommunicationError("模型字段 missing 必须是字符串数组")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise CommunicationError("模型字段 warnings 必须是字符串数组")
    forbidden = [phrase for phrase in rules.get("forbidden_phrases", []) if phrase and phrase in text]
    if forbidden:
        raise CommunicationError("规范文本包含禁用表达：" + "、".join(forbidden))
    return ModelResult(status=status, text=text, missing=missing, warnings=warnings)


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS work_order_status (
            business_type TEXT NOT NULL,
            order_id TEXT NOT NULL,
            handler_user_id TEXT,
            status TEXT NOT NULL,
            submitted_at TEXT,
            submission_task_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (business_type, order_id)
        );

        CREATE TABLE IF NOT EXISTS communication_tasks (
            task_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            sender_user_id TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            order_id TEXT NOT NULL,
            raw_content TEXT NOT NULL,
            contact_status TEXT,
            normalized_record TEXT,
            missing_fields TEXT NOT NULL DEFAULT '[]',
            warnings TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT,
            UNIQUE (group_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS communication_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            operator_user_id TEXT NOT NULL,
            event_data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES communication_tasks(task_id)
        );
        """
    )
    connection.commit()
    return connection


def reserve_submission(
    connection: sqlite3.Connection,
    message: IncomingMessage,
    submission: ParsedSubmission,
    business_type: str = "default",
) -> tuple[str, bool]:
    task_id = str(uuid.uuid4())
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT task_id FROM communication_tasks WHERE group_id = ? AND message_id = ?",
            (message.group_id, message.message_id),
        ).fetchone()
        if existing:
            connection.rollback()
            return str(existing[0]), False

        current = connection.execute(
            "SELECT status, submission_task_id FROM work_order_status WHERE business_type = ? AND order_id = ?",
            (business_type, submission.order_id),
        ).fetchone()
        if current and current[0] in {"submitted", "text_ready", "needs_revision"}:
            connection.rollback()
            raise CommunicationError(f"工单 {submission.order_id} 已提交回单")

        connection.execute(
            """
            INSERT INTO communication_tasks (
                task_id, message_id, group_id, sender_user_id, sender_name,
                order_id, raw_content, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?)
            """,
            (
                task_id,
                message.message_id,
                message.group_id,
                message.sender_user_id,
                message.sender_name,
                submission.order_id,
                submission.raw_content,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO work_order_status (
                business_type, order_id, handler_user_id, status,
                submitted_at, submission_task_id, updated_at
            ) VALUES (?, ?, ?, 'submitted', ?, ?, ?)
            ON CONFLICT(business_type, order_id) DO UPDATE SET
                handler_user_id = excluded.handler_user_id,
                status = 'submitted',
                submitted_at = excluded.submitted_at,
                submission_task_id = excluded.submission_task_id,
                updated_at = excluded.updated_at
            """,
            (business_type, submission.order_id, message.sender_user_id, now, task_id, now),
        )
        connection.execute(
            "INSERT INTO communication_events (task_id, event_type, operator_user_id, event_data, created_at) VALUES (?, 'submitted', ?, ?, ?)",
            (task_id, message.sender_user_id, json.dumps(asdict(submission), ensure_ascii=False), now),
        )
        connection.commit()
        return task_id, True
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def save_model_result(
    connection: sqlite3.Connection,
    task_id: str,
    result: ModelResult,
    operator_user_id: str,
    business_type: str = "default",
) -> None:
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            "SELECT order_id FROM communication_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not task:
            raise CommunicationError(f"回单任务不存在：{task_id}")
        connection.execute(
            """
            UPDATE communication_tasks
            SET contact_status = ?, normalized_record = ?, missing_fields = ?,
                warnings = ?, status = 'text_ready', completed_at = ?, error_message = NULL
            WHERE task_id = ?
            """,
            (
                result.status,
                result.text,
                json.dumps(result.missing, ensure_ascii=False),
                json.dumps(result.warnings, ensure_ascii=False),
                now,
                task_id,
            ),
        )
        connection.execute(
            "UPDATE work_order_status SET status = 'text_ready', updated_at = ? WHERE business_type = ? AND order_id = ? AND submission_task_id = ?",
            (now, business_type, task[0], task_id),
        )
        connection.execute(
            "INSERT INTO communication_events (task_id, event_type, operator_user_id, event_data, created_at) VALUES (?, 'text_ready', ?, ?, ?)",
            (task_id, operator_user_id, json.dumps(asdict(result), ensure_ascii=False), now),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def mark_generation_failed(
    connection: sqlite3.Connection,
    task_id: str,
    error_message: str,
    operator_user_id: str,
) -> None:
    """Record text-generation failure without reopening reminder eligibility."""
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """
            UPDATE communication_tasks
            SET status = 'generation_failed', error_message = ?
            WHERE task_id = ? AND status = 'submitted'
            """,
            (error_message, task_id),
        ).rowcount
        if not changed:
            raise CommunicationError(f"无法更新回单任务：{task_id}")
        connection.execute(
            "INSERT INTO communication_events (task_id, event_type, operator_user_id, event_data, created_at) VALUES (?, 'generation_failed', ?, ?, ?)",
            (task_id, operator_user_id, json.dumps({"error": error_message}, ensure_ascii=False), now),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def format_reply(order_id: str, result: ModelResult) -> str:
    lines = ["【沟通记录】", f"工单号：{order_id}", f"联系结果：{result.status}", "", result.text]
    if result.missing:
        lines.extend(["", "待补充：" + "、".join(result.missing)])
    lines.extend(["", "该工单已登记为已回单。"])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Feature two communication-record processor.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate one incoming message without changing state.")
    validate.add_argument("--text", required=True)

    init = subparsers.add_parser("init-db", help="Create feature two tables.")
    init.add_argument("--db", type=Path, required=True)

    process = subparsers.add_parser("process", help="Process one event using a pre-generated model response.")
    process.add_argument("--db", type=Path, required=True)
    process.add_argument("--event", type=Path, required=True)
    process.add_argument("--model-response", type=Path, required=True)
    process.add_argument("--business-type", default="default")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rules = load_json(args.rules.resolve())
        if args.command == "validate":
            print(json.dumps(asdict(parse_submission(args.text, rules)), ensure_ascii=False, indent=2))
            return 0
        if args.command == "init-db":
            init_db(args.db.resolve()).close()
            print(f"initialized: {args.db.resolve()}")
            return 0

        event = load_json(args.event.resolve())
        message = IncomingMessage(
            message_id=normalize_text(event.get("message_id")),
            group_id=normalize_text(event.get("group_id")),
            sender_user_id=normalize_text(event.get("sender_user_id")),
            sender_name=normalize_text(event.get("sender_name")),
            text=str(event.get("text", "")),
        )
        for field_name in ("message_id", "group_id", "sender_user_id"):
            if not getattr(message, field_name):
                raise CommunicationError(f"事件缺少字段：{field_name}")
        submission = parse_submission(message.text, rules)
        connection = init_db(args.db.resolve())
        try:
            task_id, created = reserve_submission(connection, message, submission, args.business_type)
            if not created:
                print(json.dumps({"task_id": task_id, "duplicate": True}, ensure_ascii=False))
                return 0
            try:
                model_result = validate_model_result(load_json(args.model_response.resolve()), rules)
                save_model_result(connection, task_id, model_result, message.sender_user_id, args.business_type)
            except (CommunicationError, OSError, json.JSONDecodeError) as exc:
                mark_generation_failed(connection, task_id, str(exc), message.sender_user_id)
                raise
        finally:
            connection.close()
        print(format_reply(submission.order_id, model_result))
        return 0
    except (CommunicationError, OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
