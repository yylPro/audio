import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from audio_quality.fun_asr_client import AliyunFunASRClient, _build_channel_parameter


class FunASRClientTests(unittest.TestCase):
    def _write_wav(self, path: Path, channels: int) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 1600 * channels)

    def test_auto_channel_parameter_matches_wav_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mono = root / "mono.wav"
            stereo = root / "stereo.wav"
            unknown = root / "audio.m4a"
            self._write_wav(mono, 1)
            self._write_wav(stereo, 2)
            unknown.write_bytes(b"audio")

            self.assertEqual([0], _build_channel_parameter("auto", mono))
            self.assertEqual([0, 1], _build_channel_parameter("auto", stereo))
            self.assertIsNone(_build_channel_parameter("auto", unknown))
            self.assertEqual([0], _build_channel_parameter([0, 1], mono))

    def test_mono_wav_uses_only_channel_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "mono.wav"
            self._write_wav(wav_path, 1)

            with patch("audio_quality.fun_asr_client._required_env", lambda name: "value"):
                client = AliyunFunASRClient({"channel_id": "auto"})
            client._upload_to_oss = lambda audio_path: "oss://bucket/mono.wav"

            calls = {}

            class FakeTranscription:
                @staticmethod
                def async_call(**kwargs):
                    calls.update(kwargs)
                    return {"output": {"task_id": "task-1"}}

                @staticmethod
                def wait(**kwargs):
                    return {"output": {"results": [{"transcription_url": "https://example.test/result.json"}]}}

            payload = b'{"transcripts":[{"channel_id":0,"sentences":[{"begin_time":0,"end_time":1000,"text":"hello"}]}]}'

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return payload

            with patch.dict("sys.modules", {"dashscope.audio.asr": type("Module", (), {"Transcription": FakeTranscription})()}):
                with patch("urllib.request.urlopen", lambda url, timeout=60: FakeResponse()):
                    segments = client.transcribe(wav_path)

            self.assertEqual([0], calls["channel_id"])
            self.assertEqual("hello", segments[0].text)

    def test_oss_url_mode_defaults_to_signed_https(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"audio")

            class FakeBucket:
                def __init__(self, *args, **kwargs):
                    pass

                def put_object_from_file(self, key, path):
                    self.key = key

                def sign_url(self, method, key, expires, slash_safe=True):
                    return f"https://signed.example/{key}?expires={expires}"

            fake_oss2 = types.SimpleNamespace(Auth=lambda *args: object(), Bucket=FakeBucket)
            with patch.dict("sys.modules", {"oss2": fake_oss2}):
                with patch("audio_quality.fun_asr_client._required_env", lambda name: "value"):
                    signed_client = AliyunFunASRClient({"oss_url_mode": "signed_https", "signed_url_expires_seconds": 600})
                    oss_client = AliyunFunASRClient({"oss_url_mode": "oss_uri"})

                    self.assertTrue(signed_client._upload_to_oss(audio).startswith("https://signed.example/"))
                    self.assertTrue(oss_client._upload_to_oss(audio).startswith("oss://value/"))


if __name__ == "__main__":
    unittest.main()
