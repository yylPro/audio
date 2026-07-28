import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audio_quality.audio_converter import AudioConversionError, convert_audio_if_needed


class AudioConverterTests(unittest.TestCase):
    def test_skips_regular_audio_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "call.wav"
            source.write_bytes(b"audio")
            result = convert_audio_if_needed(source, "QA-1", {
                "enabled": True,
                "convert_suffixes": [".m4a"],
                "work_dir": str(root / "converted"),
                "ffmpeg_path": str(root / "ffmpeg.exe"),
            })
            self.assertFalse(result.converted)
            self.assertEqual(source, result.audio_path)

    def test_converts_m4a_to_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "call.m4a"
            source.write_bytes(b"audio")
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.write_bytes(b"fake")

            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(b"wav")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("audio_quality.audio_converter.subprocess.run", fake_run):
                result = convert_audio_if_needed(source, "QA-1", {
                    "enabled": True,
                    "target_suffix": ".wav",
                    "sample_rate": 16000,
                    "channels": 1,
                    "convert_suffixes": [".m4a"],
                    "work_dir": str(root / "converted"),
                    "ffmpeg_path": str(ffmpeg),
                })

            self.assertTrue(result.converted)
            self.assertEqual(root / "converted" / "QA-1.wav", result.audio_path)
            self.assertTrue(result.audio_path.is_file())

    def test_auto_channels_does_not_force_mono(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "call.m4a"
            source.write_bytes(b"audio")
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.write_bytes(b"fake")
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                Path(command[-1]).write_bytes(b"wav")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("audio_quality.audio_converter.subprocess.run", fake_run):
                convert_audio_if_needed(source, "QA-1", {
                    "enabled": True,
                    "channels": "auto",
                    "convert_suffixes": [".m4a"],
                    "work_dir": str(root / "converted"),
                    "ffmpeg_path": str(ffmpeg),
                })

            self.assertNotIn("-ac", calls[0])

    def test_missing_ffmpeg_is_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "call.m4a"
            source.write_bytes(b"audio")
            with self.assertRaisesRegex(AudioConversionError, "ffmpeg"):
                convert_audio_if_needed(source, "QA-1", {
                    "enabled": True,
                    "convert_suffixes": [".m4a"],
                    "ffmpeg_path": str(root / "missing.exe"),
                })


if __name__ == "__main__":
    unittest.main()
