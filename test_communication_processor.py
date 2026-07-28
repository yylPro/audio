import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from communication.communication_processor import (
    CommunicationError,
    IncomingMessage,
    ModelResult,
    init_db,
    mark_generation_failed,
    parse_submission,
    reserve_submission,
    save_model_result,
    validate_model_result,
)


RULES = {
    "trigger": "#沟通记录",
    "minimum_content_characters": 4,
    "order_id_pattern": r"[A-Za-z0-9_-]{4,64}",
    "allowed_contact_statuses": ["已联系", "未接通"],
    "forbidden_phrases": ["应该已经"],
}


class CommunicationProcessorTests(unittest.TestCase):
    def test_parse_submission(self):
        parsed = parse_submission(
            "@Bot #沟通记录\n工单号：202607240001\n沟通情况：用户说明天下午可以联系", RULES
        )
        self.assertEqual("202607240001", parsed.order_id)
        self.assertEqual("用户说明天下午可以联系", parsed.raw_content)

    def test_parse_submission_requires_order_id(self):
        with self.assertRaisesRegex(CommunicationError, "缺少工单号"):
            parse_submission("@Bot #沟通记录\n沟通情况：用户未接电话", RULES)

    def test_placeholder_rules_block_model_result(self):
        with self.assertRaisesRegex(CommunicationError, "业务规则尚未配置"):
            validate_model_result({"status": "已联系", "text": "已联系用户。"}, {"allowed_contact_statuses": []})

    def test_model_result_rejects_forbidden_phrase(self):
        with self.assertRaisesRegex(CommunicationError, "禁用表达"):
            validate_model_result(
                {"status": "已联系", "text": "问题应该已经解决。", "missing": [], "warnings": []}, RULES
            )

    def test_duplicate_message_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(Path(directory) / "state.sqlite3")
            message = IncomingMessage("m1", "g1", "u1", "张三", "")
            submission = parse_submission(
                "#沟通记录 工单号：A1234 沟通情况：已经联系到用户", RULES
            )
            first_id, first_created = reserve_submission(connection, message, submission)
            second_id, second_created = reserve_submission(connection, message, submission)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_id, second_id)
            connection.close()

    def test_submission_immediately_stops_reminder_and_result_becomes_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(Path(directory) / "state.sqlite3")
            message = IncomingMessage("m2", "g1", "u1", "张三", "")
            submission = parse_submission(
                "#沟通记录 工单号：A1234 沟通情况：已经联系到用户", RULES
            )
            task_id, _ = reserve_submission(connection, message, submission)
            status = connection.execute(
                "SELECT status FROM work_order_status WHERE order_id = 'A1234'"
            ).fetchone()[0]
            self.assertEqual("submitted", status)

            save_model_result(
                connection,
                task_id,
                ModelResult("已联系", "已联系用户。", [], []),
                "u1",
            )
            task_status, order_status = connection.execute(
                """
                SELECT communication_tasks.status, work_order_status.status
                FROM communication_tasks
                JOIN work_order_status ON work_order_status.submission_task_id = communication_tasks.task_id
                WHERE communication_tasks.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            self.assertEqual(("text_ready", "text_ready"), (task_status, order_status))
            connection.close()

    def test_generation_failure_does_not_reopen_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(Path(directory) / "state.sqlite3")
            message = IncomingMessage("m3", "g1", "u1", "张三", "")
            submission = parse_submission(
                "#沟通记录 工单号：A1234 沟通情况：已经联系到用户", RULES
            )
            task_id, _ = reserve_submission(connection, message, submission)
            mark_generation_failed(connection, task_id, "模型输出无效", "u1")
            task_status = connection.execute(
                "SELECT status FROM communication_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            order_status = connection.execute(
                "SELECT status FROM work_order_status WHERE order_id = 'A1234'"
            ).fetchone()[0]
            self.assertEqual("generation_failed", task_status)
            self.assertEqual("submitted", order_status)
            connection.close()


if __name__ == "__main__":
    unittest.main()
