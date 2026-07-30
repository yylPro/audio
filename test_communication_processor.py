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
    "trigger": "#回单整理",
    "minimum_content_characters": 4,
    "order_id_pattern": r"[A-Za-z0-9_-]{4,64}",
    "customer_number_pattern": r"[0-9A-Za-z*#]{5,32}",
    "allowed_contact_statuses": ["已联系", "未接通"],
    "forbidden_phrases": ["应该已经"],
}


class CommunicationProcessorTests(unittest.TestCase):
    def test_parse_submission(self):
        parsed = parse_submission(
            "@Bot #回单整理\n工单号：202607240001\n客户号码：13800000000\n口语描述：用户说明天下午可以联系", RULES
        )
        self.assertEqual("202607240001", parsed.order_id)
        self.assertEqual("13800000000", parsed.customer_number)
        self.assertEqual("用户说明天下午可以联系", parsed.raw_content)

    def test_parse_submission_requires_order_id(self):
        with self.assertRaisesRegex(CommunicationError, "缺少工单号"):
            parse_submission("@Bot #回单整理\n客户号码：13800000000\n口语描述：用户未接电话", RULES)

    def test_parse_submission_requires_customer_number(self):
        with self.assertRaisesRegex(CommunicationError, "缺少客户号码"):
            parse_submission("@Bot #回单整理\n工单号：A1234\n口语描述：用户未接电话", RULES)

    def test_parse_submission_infers_unlabeled_order_and_customer_number(self):
        parsed = parse_submission(
            "#回单整理 18376697569，20260703174229X749589475，我处已于2026年7月11日18:24分电话13457142185联系客户，客户表示要反馈的是号码13788373366最低消费129元的问题",
            RULES,
        )
        self.assertEqual("20260703174229X749589475", parsed.order_id)
        self.assertEqual("18376697569", parsed.customer_number)
        self.assertIn("最低消费129元", parsed.raw_content)

    def test_model_result_can_require_output_sections(self):
        rules = {
            **RULES,
            "required_text_patterns": [
                {"pattern": "沟通内容", "description": "沟通内容"},
                {"pattern": "处理方案", "description": "处理方案"},
                {"pattern": "客户态度", "description": "客户态度"},
            ],
        }
        validate_model_result(
            {
                "status": "已联系",
                "text": "2026-07-11 18时24分外呼客户，沟通内容：已解释。（处理方案：已记录。）客户态度：客户知晓。",
                "missing": [],
                "warnings": [],
            },
            rules,
        )
        with self.assertRaisesRegex(CommunicationError, "缺少必备内容"):
            validate_model_result(
                {"status": "已联系", "text": "已联系用户。", "missing": [], "warnings": []},
                rules,
            )

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
                "#回单整理 工单号：A1234 客户号码：13800000000 口语描述：已经联系到用户", RULES
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
                "#回单整理 工单号：A1234 客户号码：13800000000 口语描述：已经联系到用户", RULES
            )
            task_id, _ = reserve_submission(connection, message, submission)
            status, customer_number = connection.execute(
                "SELECT status, customer_number FROM work_order_status WHERE order_id = 'A1234'"
            ).fetchone()
            self.assertEqual("submitted", status)
            self.assertEqual("13800000000", customer_number)

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
            self.assertEqual(("text_ready", "replied"), (task_status, order_status))
            connection.close()

    def test_generation_failure_does_not_mark_work_order_replied(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(Path(directory) / "state.sqlite3")
            message = IncomingMessage("m3", "g1", "u1", "张三", "")
            submission = parse_submission(
                "#回单整理 工单号：A1234 客户号码：13800000000 口语描述：已经联系到用户", RULES
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
            self.assertEqual("generation_failed", order_status)
            connection.close()


if __name__ == "__main__":
    unittest.main()
