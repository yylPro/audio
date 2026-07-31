import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from communication.communication_processor import ModelResult
from communication.inbound_adapter import handle_event


RULES = {
    "trigger": "#回单整理",
    "minimum_content_characters": 4,
    "order_id_pattern": r"[A-Za-z0-9_-]{4,64}",
    "customer_number_pattern": r"[0-9A-Za-z*#]{5,32}",
    "allowed_contact_statuses": ["已联系", "未接通", "需转派", "需跟进", "已短信"],
    "required_text_patterns": [],
    "forbidden_phrases": [],
}


class CommunicationInboundAdapterTests(unittest.TestCase):
    def make_config(self, root: Path) -> dict:
        rules_path = root / "rules.json"
        prompt_path = root / "prompt.md"
        rules_path.write_text(json.dumps(RULES, ensure_ascii=False), encoding="utf-8")
        prompt_path.write_text("只返回 JSON。", encoding="utf-8")
        return {
            "paths": {"state_db": str(root / "state.sqlite3")},
            "communication": {
                "enabled": True,
                "rules": str(rules_path),
                "prompt": str(prompt_path),
                "trigger": "#回单整理",
                "require_native_at": True,
                "business_type": "default",
                "model": "deepseek/deepseek-chat",
            },
        }

    def make_event(self, **overrides) -> dict:
        event = {
            "message_id": "m1",
            "group_id": "g1",
            "sender_user_id": "u1",
            "sender_name": "张三",
            "text": "@Bot #回单整理 工单号：A1234 客户号码：13800000000 口语描述：已经联系到用户",
            "is_at_bot": True,
        }
        event.update(overrides)
        return event

    def test_successful_ingress_saves_replied_and_returns_dual_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            deepseek = ModelResult(
                "已联系",
                "沟通内容：已联系客户。处理方案：已记录。客户态度：客户知晓。",
                [],
                [],
            )
            with patch("communication.inbound_adapter.generate_deepseek_result", return_value=deepseek):
                result = handle_event(self.make_event(), config)

            self.assertTrue(result["ok"])
            self.assertEqual("replied", result["status"])
            self.assertIn("Python标准版", result["reply"])
            self.assertIn("DeepSeek智能版", result["reply"])

            import sqlite3

            connection = sqlite3.connect(root / "state.sqlite3")
            try:
                self.assertEqual(
                    ("replied", "text_ready"),
                    connection.execute(
                        """
                        SELECT work_order_status.status, communication_tasks.status
                        FROM work_order_status
                        JOIN communication_tasks ON communication_tasks.task_id = work_order_status.submission_task_id
                        """
                    ).fetchone(),
                )
            finally:
                connection.close()

    def test_native_at_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "原生 AT"):
                handle_event(
                    self.make_event(is_at_bot=False),
                    self.make_config(Path(directory)),
                )

    def test_assignment_match_rejects_unassigned_order(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            config["communication"]["require_order_assignment_match"] = True
            with self.assertRaisesRegex(ValueError, "不在功能一当前派单中"):
                handle_event(self.make_event(), config)

    def test_generation_failure_does_not_mark_replied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            with patch(
                "communication.inbound_adapter.generate_deepseek_result",
                side_effect=ValueError("model unavailable"),
            ):
                result = handle_event(self.make_event(), config)

            self.assertFalse(result["ok"])
            self.assertTrue(result["generation_failed"])

            import sqlite3

            connection = sqlite3.connect(root / "state.sqlite3")
            try:
                self.assertEqual(
                    ("generation_failed", "generation_failed"),
                    connection.execute(
                        """
                        SELECT work_order_status.status, communication_tasks.status
                        FROM work_order_status
                        JOIN communication_tasks ON communication_tasks.task_id = work_order_status.submission_task_id
                        """
                    ).fetchone(),
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
