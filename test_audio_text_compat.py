import tempfile
import unittest
from pathlib import Path

from audio_quality.audio_quality_processor import extract_accepted_number, init_db
from audio_quality.inbound_adapter import ingest_event
from audio_quality.text_compat import repair_multiline_text, repair_text


class AudioTextCompatibilityTests(unittest.TestCase):
    def test_repairs_escaped_message_text(self):
        text = r"@Bot #\u542c\u97f3\u68c0\u6d4b\n\u53d7\u7406\u53f7\u7801\uff1a15978157631"
        repaired = repair_multiline_text(text)
        self.assertIn("#听音检测", repaired)
        self.assertIn("受理号码:15978157631", repaired)
        self.assertEqual("15978157631", extract_accepted_number(repaired))

    def test_windows_path_is_not_control_unescaped(self):
        path = r"D:\temp\audio\call.m4a"
        self.assertEqual(path, repair_text(path))

    def test_repairs_html_entities_and_full_width_characters(self):
        repaired = repair_text("#听音检测&amp;受理号码：１２３ＡＢＣ")
        self.assertEqual("#听音检测&受理号码:123ABC", repaired)

    def test_repairs_escaped_unicode_and_newlines_together(self):
        repaired = repair_multiline_text(r"\u5ba2\u6237\n\u5957\u9910\uff1a\uff18\u5143")
        self.assertEqual("客户\n套餐:8元", repaired)

    def test_ingest_accepts_escaped_trigger_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "2026-07-28_15978157631.m4a"
            audio.write_bytes(b"audio")
            config = {
                "enabled": True,
                "allowed_group_ids": ["924443429"],
                "trigger_aliases": ["#听音检测"],
                "require_structured_at": True,
                "max_attachments": 1,
                "database_path": str(root / "state.sqlite3"),
                "audio_inbox": str(root / "audio-inbox"),
                "supported_audio_suffixes": [".m4a"],
            }
            result = ingest_event({
                "message_id": "m1",
                "group_id": "924443429",
                "sender_user_id": "u1",
                "sender_name": r"\u5f20\u4e09",
                "text": r"@Bot #\u542c\u97f3\u68c0\u6d4b\n\u53d7\u7406\u53f7\u7801\uff1a15978157631",
                "is_at_bot": True,
                "media_paths": [str(audio)],
            }, config)
            connection = init_db(Path(config["database_path"]))
            row = connection.execute("SELECT sender_name,accepted_number,original_filename FROM audio_tasks WHERE task_id=?", (result["task_id"],)).fetchone()
            connection.close()
            self.assertEqual(("张三", "15978157631", audio.name), row)


if __name__ == "__main__":
    unittest.main()
