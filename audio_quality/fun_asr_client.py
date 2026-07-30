from __future__ import annotations

import json
import os
import tempfile
import warnings
import wave
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio_quality_processor import AudioQualityError, TranscriptSegment


class FunASRError(AudioQualityError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def load_env_file(path: Path | None) -> None:
    if not path or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise FunASRError(f"环境变量 {name} 未配置")
    return value


def _deduplicate_overlapping_channels(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    for segment in sorted(segments, key=lambda item: (item.start_seconds, item.end_seconds, item.speaker)):
        duplicate = False
        for existing in result:
            overlap = min(segment.end_seconds, existing.end_seconds) - max(segment.start_seconds, existing.start_seconds)
            if overlap > 0.5 and segment.text == existing.text:
                duplicate = True
                break
        if not duplicate:
            result.append(segment)
    return sorted(result, key=lambda item: (item.start_seconds, item.end_seconds, item.speaker))


def _wav_channel_count(audio_path: Path) -> int | None:
    if audio_path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(audio_path), "rb") as handle:
            return int(handle.getnchannels())
    except Exception:
        return None


def _build_channel_parameter(configured_channels: Any, audio_path: Path) -> list[int] | None:
    if configured_channels in (None, "", "auto"):
        wav_channels = _wav_channel_count(audio_path)
        if wav_channels is None:
            return None
        return list(range(max(1, wav_channels)))
    if isinstance(configured_channels, int):
        configured = [configured_channels]
    elif isinstance(configured_channels, list):
        configured = [int(item) for item in configured_channels]
    else:
        return None
    wav_channels = _wav_channel_count(audio_path)
    if wav_channels is None:
        return configured
    filtered = [item for item in configured if 0 <= item < wav_channels]
    return filtered or list(range(max(1, wav_channels)))


class AliyunFunASRClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model = str(config.get("model", "fun-asr"))
        self.region = str(config.get("region", "cn-beijing"))
        self.api_key = _required_env(str(config.get("api_key_env", "DASHSCOPE_API_KEY")))
        self.oss_endpoint = _required_env(str(config.get("oss_endpoint_env", "FUN_ASR_OSS_ENDPOINT")))
        self.oss_bucket = _required_env(str(config.get("oss_bucket_env", "FUN_ASR_OSS_BUCKET")))
        self.oss_access_key_id = _required_env(str(config.get("oss_access_key_id_env", "ALIBABA_CLOUD_ACCESS_KEY_ID")))
        self.oss_access_key_secret = _required_env(str(config.get("oss_access_key_secret_env", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")))
        self._temporary_oss_key: str | None = None

    def _oss_bucket_client(self):
        try:
            import oss2
        except ModuleNotFoundError as exc:
            raise FunASRError("缺少 oss2 Python 依赖") from exc
        auth = oss2.Auth(self.oss_access_key_id, self.oss_access_key_secret)
        return oss2.Bucket(auth, self.oss_endpoint, self.oss_bucket)

    def _upload_to_oss(self, audio_path: Path) -> str:
        key = f"work-order-audio/{datetime.now():%Y/%m/%d}/{uuid.uuid4().hex}{audio_path.suffix}"
        bucket = self._oss_bucket_client()
        try:
            bucket.put_object_from_file(key, str(audio_path))
            self._temporary_oss_key = key
            if str(self.config.get("oss_url_mode", "signed_https")) == "oss_uri":
                return f"oss://{self.oss_bucket}/{key}"
            expires = int(self.config.get("signed_url_expires_seconds", 3600))
            return bucket.sign_url("GET", key, expires, slash_safe=True)
        except Exception as exc:
            raise FunASRError(f"临时上传 OSS 失败：{exc}", retryable=True) from exc

    def _delete_temporary_oss_object(self) -> None:
        key = self._temporary_oss_key
        self._temporary_oss_key = None
        if not key or not self.config.get("delete_temporary_oss_object", True):
            return
        try:
            self._oss_bucket_client().delete_object(key)
        except Exception as exc:
            warnings.warn(f"临时 OSS 音频删除失败，将由 Bucket 生命周期规则兜底：{key}：{exc}", RuntimeWarning)

    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        if not audio_path.is_file():
            raise FunASRError(f"待转写音频不存在：{audio_path}")
        try:
            from dashscope.audio.asr import Transcription
        except ModuleNotFoundError as exc:
            raise FunASRError("缺少 dashscope Python 依赖") from exc
        try:
            file_url = self._upload_to_oss(audio_path)
            channel_id = _build_channel_parameter(self.config.get("channel_id", "auto"), audio_path)
            parameters: dict[str, Any] = {
                "disfluency_removal_enabled": bool(self.config.get("disfluency_removal_enabled", False)),
                "timestamp_alignment_enabled": bool(self.config.get("timestamp_alignment_enabled", True)),
            }
            if channel_id is not None:
                parameters["channel_id"] = channel_id
            try:
                submitted = Transcription.async_call(model=self.model, file_urls=[file_url], api_key=self.api_key, **parameters)
                task_id = getattr(getattr(submitted, "output", None), "task_id", None)
                if not task_id and isinstance(submitted, dict):
                    task_id = (((submitted.get("output") or {}).get("task_id")) or submitted.get("task_id"))
                if not task_id:
                    raise FunASRError(f"Fun-ASR 未返回任务 ID：{submitted}")
                response = Transcription.wait(task=task_id, api_key=self.api_key)
            except FunASRError:
                raise
            except Exception as exc:
                raise FunASRError(f"Fun-ASR 请求失败：{exc}", retryable=True) from exc
            data = response if isinstance(response, dict) else json.loads(json.dumps(response, default=lambda obj: getattr(obj, "__dict__", str(obj))))
            output = data.get("output") or {}
            results = output.get("results") or output.get("transcription_url") or []
            if isinstance(results, dict):
                results = [results]
            if not results:
                raise FunASRError(f"Fun-ASR 结果为空：{response}")
            first = results[0]
            url = first.get("transcription_url") if isinstance(first, dict) else str(first)
            if not url:
                raise FunASRError(f"Fun-ASR 未返回转写结果地址：{first}")
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=60) as handle:
                    payload = json.loads(handle.read().decode("utf-8"))
            except Exception as exc:
                raise FunASRError(f"下载 Fun-ASR 结果失败：{exc}", retryable=True) from exc
            transcripts = payload.get("transcripts") or []
            if not transcripts:
                raise FunASRError("Fun-ASR 结果缺少 transcripts")
            segments: list[TranscriptSegment] = []
            for transcript_index, transcript in enumerate(transcripts):
                channel = transcript.get("channel_id", transcript.get("channel", transcript_index))
                label = f"CH{channel}"
                for sentence in transcript.get("sentences") or []:
                    text = str(sentence.get("text", "")).strip()
                    if not text:
                        continue
                    start = float(sentence.get("begin_time", 0)) / 1000
                    end = float(sentence.get("end_time", 0)) / 1000
                    if end <= start:
                        end = start + 0.001
                    raw_speaker = sentence.get("speaker_id") or sentence.get("speaker")
                    speaker = f"{label}:{raw_speaker}" if raw_speaker not in (None, "") else label
                    segments.append(TranscriptSegment(start, end, str(speaker).strip(), text))
            segments = _deduplicate_overlapping_channels(segments)
            if not segments:
                raise FunASRError("Fun-ASR 没有生成有效句子")
            return segments
        finally:
            self._delete_temporary_oss_object()
