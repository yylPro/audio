import tempfile
import unittest
from pathlib import Path

from audio_quality.audio_quality_processor import (
    AudioQualityError, AudioSubmission, IncomingAudioMessage, TranscriptSegment,
    claim_next_task, deterministic_quality_check, evaluate_asr_quality, export_report,
    extract_accepted_number, format_completed_reply, init_db, mark_task_failed,
    parse_submission, parse_transcript, reserve_task, save_organized_call,
    save_quality_result, save_transcript, scan_rule_hits,
)
from audio_quality.batch_transcribe_faster_whisper import parse_filename_metadata
from audio_quality.deepseek_organizer import OrganizedCall


RULES = {
    "trigger": "#听音检测",
    "trigger_aliases": ["#听音检测", "#听音质检"],
    "order_id_pattern": r"[A-Za-z0-9_-]{4,64}",
    "supported_audio_suffixes": [".mp3", ".wav"],
    "max_file_size_bytes": 1000,
    "qualified_score": 80,
    "speaker_review_threshold": 0.75,
    "employee_speaker_keywords": ["中国移动", "您好"],
    "dimensions": {
        "basic_courtesy": {"weight": 50, "base_score": 100, "rule_ids": ["P01"]},
        "respectful_expression": {"weight": 50, "base_score": 100, "rule_ids": ["P01"]},
    },
    "phrase_rules": [{"id": "P01", "pattern": "我也没有办法", "description": "消极表达", "deduction": 10}],
}


def make_message(**overrides):
    values = {
        "message_id": "m1", "group_id": "g1", "sender_user_id": "u1", "sender_name": "提交人",
        "text": "@Bot #听音检测\n员工：张三\n工单号：A1234\n受理号码：15978157631",
        "attachment_id": "f1", "filename": "call.mp3", "file_size": 500,
        "duration_seconds": 30, "attachment_hash": "hash1",
    }
    values.update(overrides)
    return IncomingAudioMessage(**values)


class AudioQualityProcessorTests(unittest.TestCase):
    def test_parse_audio_filename_metadata(self):
        self.assertEqual(("2026-07-23", "15978157631"), parse_filename_metadata("2026-7-23 雷建宏 15978157631号码.m4a"))

    def test_parse_submission_and_attachment(self):
        self.assertEqual(AudioSubmission("张三", "A1234", "15978157631"), parse_submission(make_message().text, RULES))
        with self.assertRaisesRegex(AudioQualityError, "只能执行一个功能"):
            parse_submission("#听音检测 #沟通记录\n员工：张三", RULES)

    def test_extract_accepted_number(self):
        self.assertEqual("15978157631", extract_accepted_number("2026-7-23 15978157631号码.m4a"))

    def test_transcript_validation_and_rule_scan(self):
        segments = parse_transcript([
            {"start_seconds": 0, "end_seconds": 2, "speaker": "EMP", "text": "中国移动您好"},
            {"start_seconds": 3, "end_seconds": 5, "speaker": "EMP", "text": "这个我也没有办法"},
        ])
        self.assertEqual(2, len(segments))
        self.assertEqual(1, len(scan_rule_hits(segments, "EMP", RULES)))
        result = deterministic_quality_check(segments, RULES)
        self.assertEqual(90, result.score)
        self.assertEqual(1, len(result.issues))

    def test_asr_quality_gate_requires_review(self):
        rules = dict(RULES)
        rules["asr_quality"] = {"enabled": True, "min_total_characters": 20, "min_segment_count": 2, "min_distinct_speakers": 2, "key_business_terms": ["套餐"]}
        segments = [TranscriptSegment(0, 2, "EMP", "中国移动您好")]
        self.assertTrue(evaluate_asr_quality(segments, rules))
        result = deterministic_quality_check(segments, rules)
        self.assertTrue(result.needs_human_review)
        self.assertFalse(result.qualified)

    def test_database_report_and_text_file(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(Path(directory) / "state.sqlite3")
            task_id, created = reserve_task(connection, make_message(), parse_submission(make_message().text, RULES))
            self.assertTrue(created)
            self.assertEqual(task_id, claim_next_task(connection, "worker-1"))
            mark_task_failed(connection, task_id, "asr", "temporary")
            row = connection.execute("SELECT status,error_stage FROM audio_tasks WHERE task_id=?", (task_id,)).fetchone()
            self.assertEqual(("failed", "asr"), row)
            connection.execute("UPDATE audio_tasks SET status='received',error_stage=NULL,error_message=NULL WHERE task_id=?", (task_id,))
            connection.commit()
            self.assertEqual(task_id, claim_next_task(connection, "worker-2"))
            segments = [TranscriptSegment(0, 2, "客服", "中国移动您好，我帮您查询套餐。")]
            save_transcript(connection, task_id, segments)
            organized = OrganizedCall(segments, [], "客服：中国移动您好，我帮您查询套餐。", "客户咨询套餐", "引导", "", 0.9)
            save_organized_call(connection, task_id, organized)
            result = deterministic_quality_check(segments, RULES, employee_speaker_override="客服", speaker_confidence_override=0.9, asr_quality_segments=segments)
            save_quality_result(connection, task_id, result)
            output = export_report(connection, Path(directory) / "2026-07-28_降挽质检情况.xlsx")
            connection.close()
            sheet = load_workbook(output).active
            headers = [cell.value for cell in sheet[1]]
            self.assertIn("原始转录文本文件", headers)
            text_path = Path(sheet.cell(row=2, column=headers.index("原始转录文本文件") + 1).value)
            self.assertTrue(text_path.is_file())

    def test_completed_reply(self):
        self.assertEqual("听音检测完成，已写入质检表。", format_completed_reply("QA-1"))
        self.assertEqual("听音检测完成，但需要人工复核。", format_completed_reply("QA-1", True))


if __name__ == "__main__":
    unittest.main()
