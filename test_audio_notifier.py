import unittest
from pathlib import Path
from unittest.mock import patch

from audio_quality.notifier import schedule_completion_notification


class AudioNotifierTests(unittest.TestCase):
    def test_openclaw_subprocess_uses_utf8_environment(self):
        calls = {}

        def fake_run(command, **kwargs):
            calls.update(kwargs)
            return type("Completed", (), {"returncode": 0, "stdout": '{"id":"job-1"}', "stderr": ""})()

        with patch.object(Path, "exists", lambda self: True):
            with patch("audio_quality.notifier.subprocess.run", fake_run):
                job_id = schedule_completion_notification({
                    "enabled": True,
                    "openclaw_cmd": "D:\\OpenClaw\\openclaw.cmd",
                    "target": "group:1",
                    "agent_timeout_seconds": 30,
                }, "QA-1", "听音检测完成，已写入质检表。")

        self.assertEqual("job-1", job_id)
        self.assertEqual("utf-8", calls["env"]["PYTHONIOENCODING"])
        self.assertEqual("1", calls["env"]["PYTHONUTF8"])
        self.assertEqual(60, calls["timeout"])


if __name__ == "__main__":
    unittest.main()
