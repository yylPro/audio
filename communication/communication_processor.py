from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_RULES = Path(__file__).with_name("communication_rules.json")
DEFAULT_PROMPT = Path(__file__).with_name("prompts") / "communication_record.md"
ORDER_ID_RE = re.compile(r"\b\d{8,14}X\d{6,12}\b")
PHONE_RE = re.compile(r"(?<!\d)1\d{10}(?!\d)")


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
    customer_number: str
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


def normalize_identifier(value: Any) -> str:
    return normalize_text(value).replace(" ", "").replace("-", "")


def _trigger_aliases(rules: dict[str, Any]) -> list[str]:
    trigger = normalize_text(rules.get("trigger", "#回单整理"))
    aliases = [trigger]
    aliases.extend(str(item) for item in rules.get("trigger_aliases", []))
    result: list[str] = []
    for item in aliases:
        alias = normalize_text(item)
        if alias and alias not in result:
            result.append(alias)
    return result or ["#回单整理"]


def _message_after_trigger(text: str, aliases: list[str]) -> str:
    candidates = [(text.find(alias), alias) for alias in aliases if alias in text]
    if not candidates:
        raise CommunicationError(f"消息缺少触发词 {' 或 '.join(aliases)}")
    index, alias = min(candidates, key=lambda item: item[0])
    return text[index + len(alias):].strip()


def extract_order_id(text: str, rules: dict[str, Any]) -> str:
    order_match = re.search(r"(?:工单号|工单流水号|工单编号)\s*[：:]\s*([^\s，,；;]+)", text)
    if order_match:
        return normalize_text(order_match.group(1))
    inferred = ORDER_ID_RE.search(text)
    if inferred:
        return normalize_text(inferred.group(0))
    raise CommunicationError("消息缺少工单号，格式示例：工单号：202607240001")


def extract_customer_number(text: str, order_id: str, rules: dict[str, Any]) -> str:
    labeled = re.search(
        r"(?:客户号码|客户号|客户手机号|联系电话|手机号码|受理号码)\s*[：:]\s*([0-9A-Za-z*# -]{5,32})",
        text,
    )
    if labeled:
        return normalize_identifier(labeled.group(1))

    before_order = text.split(order_id, 1)[0] if order_id and order_id in text else ""
    before_candidates = PHONE_RE.findall(before_order)
    if before_candidates:
        return normalize_identifier(before_candidates[0])

    context_patterns = [
        r"(?:联系|电联|外呼|回访)(?:客户|用户)?\D{0,12}(1\d{10})",
        r"(?:客户|用户)(?:表示|反馈|反映|号码|来电|手机号)?\D{0,12}(1\d{10})",
        r"(?:号码|手机号)\D{0,6}(1\d{10})",
    ]
    for pattern in context_patterns:
        match = re.search(pattern, text)
        if match:
            return normalize_identifier(match.group(1))

    candidates = PHONE_RE.findall(text)
    if candidates:
        return normalize_identifier(candidates[0])
    raise CommunicationError("消息缺少客户号码，格式示例：客户号码：13800000000")


def parse_submission(text: str, rules: dict[str, Any]) -> ParsedSubmission:
    aliases = _trigger_aliases(rules)
    body_after_trigger = _message_after_trigger(text, aliases)

    order_id = extract_order_id(text, rules)
    pattern = str(rules.get("order_id_pattern", r"[A-Za-z0-9_-]{4,64}"))
    if not re.fullmatch(pattern, order_id):
        raise CommunicationError(f"工单号格式不合法：{order_id}")

    customer_number = extract_customer_number(text, order_id, rules)
    customer_pattern = str(rules.get("customer_number_pattern", r"[0-9A-Za-z*#]{5,32}"))
    if not re.fullmatch(customer_pattern, customer_number):
        raise CommunicationError(f"客户号码格式不合法：{customer_number}")

    content_match = re.search(r"(?:口语描述|沟通情况|原始情况|回单内容)\s*[：:]\s*(.+)", text, re.DOTALL)
    raw_content = normalize_text(content_match.group(1) if content_match else body_after_trigger)
    minimum = int(rules.get("minimum_content_characters", 4))
    if len(raw_content) < minimum:
        raise CommunicationError(f"口语描述过短，至少需要 {minimum} 个字符")
    return ParsedSubmission(order_id=order_id, customer_number=customer_number, raw_content=raw_content)


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
    missing_patterns = [
        str(item.get("description") or item.get("pattern") or "")
        for item in rules.get("required_text_patterns", [])
        if isinstance(item, dict)
        and item.get("pattern")
        and not re.search(str(item["pattern"]), text)
    ]
    if missing_patterns:
        raise CommunicationError("规范文本缺少必备内容：" + "、".join(missing_patterns))
    return ModelResult(status=status, text=text, missing=missing, warnings=warnings)


def validate_model_result_matches_submission(
    result: ModelResult,
    submission: ParsedSubmission,
    *,
    require_identifiers: bool = True,
) -> None:
    text = normalize_identifier(result.text)
    order_id = normalize_identifier(submission.order_id)
    customer_number = normalize_identifier(submission.customer_number)
    if require_identifiers and order_id and order_id not in text:
        raise CommunicationError(f"模型回单未包含本次工单号：{submission.order_id}")
    if require_identifiers and customer_number and customer_number not in text:
        raise CommunicationError(f"模型回单未包含本次客户号码：{submission.customer_number}")
    # A call-back number is expected in many records. Only reject numbers that
    # were not present in the current submission, which still blocks leakage
    # from another task without rejecting an explicitly supplied outbound line.
    allowed_numbers = {customer_number}
    allowed_numbers.update(normalize_identifier(item) for item in PHONE_RE.findall(submission.raw_content))
    other_numbers = {
        item for item in PHONE_RE.findall(result.text)
        if normalize_identifier(item) not in allowed_numbers
    }
    if other_numbers:
        raise CommunicationError("模型回单包含非本次客户号码：" + "、".join(sorted(other_numbers)))


def build_model_prompt(submission: ParsedSubmission, rules: dict[str, Any], prompt_text: str) -> str:
    output_rules = rules.get("output_rules", [])
    examples = rules.get("examples", [])
    payload = {
        "order_id": submission.order_id,
        "customer_number": submission.customer_number,
        "raw_content": submission.raw_content,
        "allowed_contact_statuses": rules.get("allowed_contact_statuses", []),
        "output_rules": output_rules,
        "examples": examples,
    }
    return prompt_text.strip() + "\n\n当前待整理回单：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_python_result(submission: ParsedSubmission, rules: dict[str, Any]) -> ModelResult:
    """Build a conservative baseline without inventing facts.

    This is the comparison version shown alongside the DeepSeek result. It keeps
    the employee's wording as the communication facts and only infers a status
    from explicit contact words.
    """
    content = submission.raw_content
    content = re.sub(rf"^{re.escape(submission.customer_number)}[，,、\s]+", "", content)
    content = re.sub(rf"^{re.escape(submission.order_id)}[，,、\s]+", "", content)
    if any(word in content for word in ("未接通", "无人接听", "未接电话", "打不通")):
        status = "未接通"
    elif "短信" in content and not any(word in content for word in ("联系客户", "电话联系", "外呼客户")):
        status = "已短信"
    elif any(word in content for word in ("转派", "转到", "转交")):
        status = "需转派"
    elif any(word in content for word in ("联系客户", "联系到客户", "外呼客户", "电话联系", "已联系")):
        status = "已联系"
    else:
        status = "需跟进"

    missing = []
    if not re.search(r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日", content):
        missing.append("联系日期时间")
    if not re.search(r"外呼|拨打|电话\s*\d{7,12}|联系号码", content) and not any(
        normalize_identifier(item) != submission.customer_number for item in PHONE_RE.findall(content)
    ):
        missing.append("外呼号码")
    phones = [item for item in PHONE_RE.findall(content) if normalize_identifier(item) != submission.customer_number]
    outbound = phones[0] if phones else "未提供"
    date_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", content)
    time_match = re.search(r"(\d{1,2})[:：](\d{2})", content)
    if date_match:
        year = int(date_match.group(1) or datetime.now().year)
        date_text = f"{year:04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
    else:
        date_text = "日期未提供"
    time_text = f"{int(time_match.group(1))}时{time_match.group(2)}分" if time_match else "时间未提供"
    text = (
        f"{date_text} {time_text}用10086外呼{outbound}号码，"
        f"沟通内容：{content}（处理方案：根据原始描述执行后续处理），"
        "客户态度：根据原始描述记录。"
    )
    return ModelResult(status=status, text=text, missing=missing, warnings=["Python标准版未补充原文未提供的联系时间和外呼号码"])


def generate_deepseek_result(
    submission: ParsedSubmission,
    rules: dict[str, Any],
    prompt_text: str,
    *,
    api_key: str | None = None,
    base_url: str = "https://api.deepseek.com/chat/completions",
    model: str = "deepseek-chat",
    timeout_seconds: int = 90,
) -> ModelResult:
    """Generate and validate the second comparison version through DeepSeek."""
    key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if not key:
        raise CommunicationError("未配置 DEEPSEEK_API_KEY，无法生成 DeepSeek 版本")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "严格按用户提供的回单规则整理，只返回 JSON，不得质疑规则是否确认。"},
            {"role": "user", "content": build_model_prompt(submission, rules, prompt_text)},
        ],
        "temperature": 0.1,
        "stream": False,
    }
    request = urllib.request.Request(
        base_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise CommunicationError(f"DeepSeek 请求失败：HTTP {exc.code} {detail}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CommunicationError(f"DeepSeek 请求失败：{exc}") from exc
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    try:
        start, end = content.find("{"), content.rfind("}")
        payload = json.loads(content[start:end + 1]) if start >= 0 and end >= start else {}
    except json.JSONDecodeError as exc:
        raise CommunicationError(f"DeepSeek 未返回有效 JSON：{exc}") from exc
    result = validate_model_result(payload, rules)
    validate_model_result_matches_submission(result, submission, require_identifiers=False)
    return result


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS work_order_status (
            business_type TEXT NOT NULL,
            order_id TEXT NOT NULL,
            customer_number TEXT,
            handler_user_id TEXT,
            status TEXT NOT NULL,
            submitted_at TEXT,
            submission_task_id TEXT,
            source_digest TEXT,
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
            customer_number TEXT,
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
    ensure_column(connection, "work_order_status", "customer_number", "TEXT")
    ensure_column(connection, "work_order_status", "source_digest", "TEXT")
    ensure_column(connection, "communication_tasks", "customer_number", "TEXT")
    connection.commit()
    return connection


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def reserve_submission(
    connection: sqlite3.Connection,
    message: IncomingMessage,
    submission: ParsedSubmission,
    business_type: str = "default",
    require_assignment_match: bool = False,
) -> tuple[str, bool]:
    task_id = str(uuid.uuid4())
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT task_id, status, order_id FROM communication_tasks WHERE group_id = ? AND message_id = ?",
            (message.group_id, message.message_id),
        ).fetchone()
        if existing:
            if existing[1] == "generation_failed":
                # A transient model/API failure must be retryable with the
                # same inbound message, while successful submissions remain
                # strictly idempotent.
                connection.execute(
                    "UPDATE communication_tasks SET status = 'submitted', error_message = NULL WHERE task_id = ?",
                    (existing[0],),
                )
                connection.execute(
                    "UPDATE work_order_status SET status = 'submitted', updated_at = ? WHERE business_type = ? AND order_id = ? AND submission_task_id = ?",
                    (now, business_type, existing[2], existing[0]),
                )
                connection.commit()
                return str(existing[0]), True
            connection.rollback()
            return str(existing[0]), False

        current = connection.execute(
            "SELECT status, submission_task_id, customer_number, source_digest "
            "FROM work_order_status WHERE business_type = ? AND order_id = ?",
            (business_type, submission.order_id),
        ).fetchone()
        if require_assignment_match and not current:
            connection.rollback()
            raise CommunicationError(f"工单 {submission.order_id} 不在功能一当前派单中，未登记回单")
        if require_assignment_match and current and current[3] is None:
            connection.rollback()
            raise CommunicationError(f"工单 {submission.order_id} 尚未完成功能一派单同步，未登记回单")
        if current and current[2] and normalize_identifier(current[2]) != normalize_identifier(submission.customer_number):
            connection.rollback()
            raise CommunicationError(f"工单 {submission.order_id} 的受理号码与功能一派单不一致，未登记回单")
        connection.execute(
            """
            INSERT INTO communication_tasks (
                task_id, message_id, group_id, sender_user_id, sender_name,
                order_id, customer_number, raw_content, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?)
            """,
            (
                task_id,
                message.message_id,
                message.group_id,
                message.sender_user_id,
                message.sender_name,
                submission.order_id,
                submission.customer_number,
                submission.raw_content,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO work_order_status (
                business_type, order_id, customer_number, handler_user_id, status,
                submitted_at, submission_task_id, updated_at
            ) VALUES (?, ?, ?, ?, 'submitted', ?, ?, ?)
            ON CONFLICT(business_type, order_id) DO UPDATE SET
                customer_number = excluded.customer_number,
                handler_user_id = excluded.handler_user_id,
                status = 'submitted',
                submitted_at = excluded.submitted_at,
                submission_task_id = excluded.submission_task_id,
                updated_at = excluded.updated_at
            """,
            (business_type, submission.order_id, submission.customer_number, message.sender_user_id, now, task_id, now),
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
            "UPDATE work_order_status SET status = 'replied', updated_at = ? WHERE business_type = ? AND order_id = ? AND submission_task_id = ?",
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
    business_type: str = "default",
) -> None:
    """Record text-generation failure without marking the work order as replied."""
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            "SELECT order_id FROM communication_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
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
        if task:
            connection.execute(
                """
                UPDATE work_order_status
                SET status = 'generation_failed', updated_at = ?
                WHERE business_type = ? AND order_id = ? AND submission_task_id = ?
                """,
                (now, business_type, task[0], task_id),
            )
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
    lines = ["【回单整理】", f"工单号：{order_id}", f"回单状态：{result.status}", "", result.text]
    if result.missing:
        lines.extend(["", "待补充：" + "、".join(result.missing)])
    lines.extend(["", "该工单已登记为已回单。"])
    return "\n".join(lines)


def format_dual_reply(order_id: str, python_result: ModelResult, deepseek_result: ModelResult) -> str:
    return "\n".join(
        [
            "【回单整理】",
            f"工单号：{order_id}",
            "",
            "【Python标准版】",
            f"回单状态：{python_result.status}",
            python_result.text,
            "",
            "【DeepSeek智能版】",
            f"回单状态：{deepseek_result.status}",
            deepseek_result.text,
            "",
            "以上两版均依据同一回单规则生成，请对比确认。",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Feature two communication-record processor.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate one incoming message without changing state.")
    validate.add_argument("--text", required=True)

    prompt = subparsers.add_parser("prompt", help="Build the model prompt for one incoming message.")
    prompt.add_argument("--text", required=True)

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
        if args.command == "prompt":
            prompt_text = args.prompt.read_text(encoding="utf-8-sig")
            print(build_model_prompt(parse_submission(args.text, rules), rules, prompt_text))
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
                mark_generation_failed(connection, task_id, str(exc), message.sender_user_id, args.business_type)
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
