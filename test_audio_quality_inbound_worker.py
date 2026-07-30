import tempfile
import unittest
import json
from pathlib import Path

import audio_quality.worker as worker_module
from audio_quality.audio_converter import AudioConversionResult
from audio_quality.audio_quality_processor import AudioQualityError, TranscriptSegment, init_db
from audio_quality.deepseek_quality import validate_assessment
from audio_quality.fun_asr_client import FunASRError
from audio_quality.inbound_adapter import ingest_event
from audio_quality.worker import run_one, single_worker_lock


def make_config(root: Path) -> dict:
    return {
        "enabled": True,
        "allowed_group_ids": ["924443429"],
        "trigger_aliases": ["#听音检测", "#听音质检"],
        "require_structured_at": True,
        "max_attachments": 10,
        "database_path": str(root / "state.sqlite3"),
        "audio_inbox": str(root / "audio-inbox"),
        "output_root": str(root / "reports"),
        "asr": {"provider": "aliyun_fun_asr", "max_attempts": 3, "retry_delays_seconds": [0, 0, 0]},
        "quality": {
            "qualified_score": 80,
            "speaker_review_threshold": 0.2,
            "employee_speaker_keywords": ["中国移动", "您好"],
            "dimensions": {"礼貌规范": {"weight": 1, "base_score": 100}},
            "phrase_rules": [{"id": "P01", "pattern": "听不懂", "description": "不礼貌表达", "deduction": 20}],
        },
    }


def make_event(audio: Path, **overrides) -> dict:
    event = {
        "message_id": "message-1",
        "group_id": "924443429",
        "sender_user_id": "sender-1",
        "sender_name": "提交人",
        "text": "@My_Bot #听音检测",
        "is_at_bot": True,
        "media_paths": [str(audio)],
    }
    event.update(overrides)
    return event


class FakeProvider:
    def __init__(self, failures=0, retryable=True):
        self.failures = failures
        self.retryable = retryable
        self.calls = 0
        self.paths = []

    def transcribe(self, audio_path: Path):
        self.calls += 1
        self.paths.append(audio_path)
        if self.calls <= self.failures:
            raise FunASRError("temporary", retryable=self.retryable)
        return [TranscriptSegment(0, 1, "S1", "中国移动您好")]


class FakeDeepSeekScorer:
    def assess(self, segments):
        transcript = " ".join(segment.text for segment in segments)
        actions = {
            rule_id: {"present": True, "evidence": [segments[0].text], "reason": ""}
            for rule_id in (
                "missing_ask",
                "missing_check",
                "missing_compare",
                "missing_calculate",
                "missing_retention_action",
                "missing_retention_success",
            )
        }
        return validate_assessment({"actions": actions, "summary": "双线语义评分"}, transcript)


class AudioQualityInboundWorkerTests(unittest.TestCase):
    def test_ingress_archives_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "2026-7-23 15978157631号码.m4a"
            audio.write_bytes(b"audio-content")
            config = make_config(root)
            first = ingest_event(make_event(audio), config)
            second = ingest_event(make_event(audio), config)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            connection = init_db(Path(config["database_path"]))
            row = connection.execute("SELECT status,accepted_number,archived_path FROM audio_tasks").fetchone()
            connection.close()
            self.assertEqual("received", row[0])
            self.assertEqual("15978157631", row[1])
            self.assertTrue(Path(row[2]).is_file())

    def test_ingress_deduplicates_same_audio_across_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "2026-7-23 15978157631.m4a"
            audio.write_bytes(b"same-audio")
            config = make_config(root)
            first = ingest_event(make_event(audio, message_id="message-1"), config)
            second = ingest_event(make_event(audio, message_id="message-2"), config)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["task_id"], second["task_id"])
            connection = init_db(Path(config["database_path"]))
            try:
                self.assertEqual(1, connection.execute("SELECT count(*) FROM audio_tasks").fetchone()[0])
                event = connection.execute("SELECT event_type FROM audio_events WHERE task_id=? AND event_type='duplicate_audio_ignored'", (first["task_id"],)).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(event)

    def test_ingress_accepts_multiple_audio_files_after_one_at(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_audio = root / "2026-7-23_15978157631.m4a"
            second_audio = root / "2026-7-23_15978157632.m4a"
            first_audio.write_bytes(b"first-audio")
            second_audio.write_bytes(b"second-audio")
            config = make_config(root)

            result = ingest_event(make_event(
                first_audio,
                media_paths=[str(first_audio), str(second_audio)],
            ), config)

            self.assertTrue(result["created"])
            self.assertEqual(2, len(result["task_ids"]))
            self.assertEqual(result["task_ids"], result["created_task_ids"])
            connection = init_db(Path(config["database_path"]))
            try:
                rows = connection.execute(
                    "SELECT archived_path FROM audio_tasks ORDER BY rowid"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(2, len(rows))
            self.assertTrue(all(Path(row[0]).is_file() for row in rows))

    def test_ingress_rejects_bad_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "call.m4a"
            audio.write_bytes(b"audio")
            config = make_config(root)
            with self.assertRaisesRegex(AudioQualityError, "当前群"):
                ingest_event(make_event(audio, group_id="other"), config)
            with self.assertRaisesRegex(AudioQualityError, "原生 AT"):
                ingest_event(make_event(audio, is_at_bot=False), config)
            config["max_attachments"] = 1
            with self.assertRaisesRegex(AudioQualityError, "最多可包含 1 个"):
                ingest_event(make_event(audio, media_paths=[str(audio), str(audio)]), config)

    def test_worker_retries_and_writes_report_and_notification_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "call.m4a"
            audio.write_bytes(b"audio")
            config = make_config(root)
            result = ingest_event(make_event(audio), config)
            connection = init_db(Path(config["database_path"]))
            calls = []
            original = worker_module.schedule_completion_notification
            worker_module.schedule_completion_notification = lambda notify, task_id, reply: calls.append((task_id, reply)) or "notify-1"
            try:
                provider = FakeProvider(failures=2)
                self.assertEqual(result["task_id"], run_one(connection, provider, "worker-1", config["asr"], config["quality"], Path(config["output_root"]), completion_notify={"enabled": True, "target": "group:924443429"}))
                row = connection.execute("SELECT status,asr_provider FROM audio_tasks WHERE task_id=?", (result["task_id"],)).fetchone()
                quality = connection.execute("SELECT score,qualified FROM quality_results WHERE task_id=?", (result["task_id"],)).fetchone()
            finally:
                worker_module.schedule_completion_notification = original
                connection.close()
            self.assertEqual(("completed", "aliyun_fun_asr"), row)
            self.assertEqual((100, 1), quality)
            self.assertEqual(3, provider.calls)
            self.assertTrue(calls)

    def test_permanent_asr_failure_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "call.m4a"
            audio.write_bytes(b"audio")
            config = make_config(root)
            result = ingest_event(make_event(audio), config)
            connection = init_db(Path(config["database_path"]))
            provider = FakeProvider(failures=1, retryable=False)
            run_one(connection, provider, "worker-1", config["asr"], config["quality"], Path(config["output_root"]))
            row = connection.execute("SELECT status,error_stage FROM audio_tasks WHERE task_id=?", (result["task_id"],)).fetchone()
            connection.close()
            self.assertEqual(("failed", "asr"), row)
            lock_path = root / "worker.lock"
            with single_worker_lock(lock_path):
                with self.assertRaisesRegex(AudioQualityError, "已有听音质检 Worker"):
                    with single_worker_lock(lock_path):
                        pass

    def test_worker_transcribes_converted_audio_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "call.m4a"
            converted = root / "converted.wav"
            audio.write_bytes(b"audio")
            converted.write_bytes(b"wav")
            config = make_config(root)
            result = ingest_event(make_event(audio), config)
            connection = init_db(Path(config["database_path"]))
            original = worker_module.convert_audio_if_needed
            worker_module.convert_audio_if_needed = lambda source, task_id, conversion_config: AudioConversionResult(source, converted, True, "converted")
            try:
                provider = FakeProvider()
                run_one(connection, provider, "worker-1", config["asr"], config["quality"], Path(config["output_root"]), audio_conversion={"enabled": True})
                event = connection.execute("SELECT event_type FROM audio_events WHERE task_id=? AND event_type='audio_converted'", (result["task_id"],)).fetchone()
            finally:
                worker_module.convert_audio_if_needed = original
                connection.close()
            self.assertEqual([converted], provider.paths)
            self.assertIsNotNone(event)

    def test_worker_uses_configurable_stale_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "call.m4a"
            audio.write_bytes(b"audio")
            config = make_config(root)
            ingest_event(make_event(audio), config)
            connection = init_db(Path(config["database_path"]))
            original = worker_module.claim_next_task
            calls = []

            def fake_claim(conn, worker_id, stale_after_seconds=1800):
                calls.append(stale_after_seconds)
                return None

            worker_module.claim_next_task = fake_claim
            try:
                run_one(connection, FakeProvider(), "worker-1", config["asr"], stale_after_seconds=123)
            finally:
                worker_module.claim_next_task = original
                connection.close()
            self.assertEqual([123], calls)

    def test_parallel_compare_keeps_python_primary_and_exports_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "2026-07-30_15978157631.m4a"
            audio.write_bytes(b"audio")
            config = make_config(root)
            received = ingest_event(make_event(audio), config)
            connection = init_db(Path(config["database_path"]))
            comparison = root / "reports" / "2026-07-30_降挽质检方案对比-语义评分.xlsx"
            original_export = worker_module.export_deepseek_comparison
            export_calls = []
            worker_module.export_deepseek_comparison = (
                lambda conn, output, days, rules: export_calls.append(days) or [comparison]
            )
            try:
                run_one(
                    connection,
                    FakeProvider(),
                    "worker-1",
                    config["asr"],
                    config["quality"],
                    Path(config["output_root"]),
                    deepseek_scorer=FakeDeepSeekScorer(),
                    deepseek_mode="parallel_compare",
                )
                quality = connection.execute(
                    "SELECT employee_speaker FROM quality_results WHERE task_id=?",
                    (received["task_id"],),
                ).fetchone()
                semantic = connection.execute(
                    "SELECT score FROM deepseek_quality_assessments WHERE task_id=?",
                    (received["task_id"],),
                ).fetchone()
                event_data = connection.execute(
                    "SELECT event_data FROM audio_events WHERE task_id=? AND event_type='completion_reply_ready' ORDER BY event_id DESC LIMIT 1",
                    (received["task_id"],),
                ).fetchone()[0]
            finally:
                worker_module.export_deepseek_comparison = original_export
                connection.close()

            self.assertNotEqual("DeepSeek语义审核", quality[0])
            self.assertIsNotNone(semantic)
            self.assertEqual([{"2026-07-30"}], export_calls)
            self.assertEqual(str(comparison), json.loads(event_data)["comparison_report_path"])


if __name__ == "__main__":
    unittest.main()
