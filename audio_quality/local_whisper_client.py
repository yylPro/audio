from __future__ import annotations

from pathlib import Path
from typing import Any

from .audio_quality_processor import AudioQualityError, TranscriptSegment


class LocalWhisperError(AudioQualityError):
    pass


class LocalFasterWhisperClient:
    def __init__(self, config: dict[str, Any]):
        self.model_name = str(config.get("model", "small"))
        self.model_dir = Path(str(config.get("model_dir", r"D:\代维\工单提醒\audio_quality_runtime\models")))
        self.device = str(config.get("device", "cpu"))
        self.compute_type = str(config.get("compute_type", "int8"))
        self.language = str(config.get("language", "zh"))
        self.beam_size = int(config.get("beam_size", 5))
        self.vad_filter = bool(config.get("vad_filter", True))
        self.min_silence_duration_ms = int(config.get("min_silence_duration_ms", 500))
        self.condition_on_previous_text = bool(config.get("condition_on_previous_text", False))
        self._model = None

    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        if not audio_path.is_file():
            raise LocalWhisperError(f"待转写音频不存在：{audio_path}")
        try:
            from faster_whisper import WhisperModel
        except ModuleNotFoundError as exc:
            raise LocalWhisperError("缺少 faster_whisper Python 依赖") from exc
        if self._model is None:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(self.model_dir),
            )
        segments, _info = self._model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            vad_parameters={"min_silence_duration_ms": self.min_silence_duration_ms},
            condition_on_previous_text=self.condition_on_previous_text,
            word_timestamps=False,
        )
        result = [
            TranscriptSegment(round(segment.start, 3), round(segment.end, 3), "ASR", segment.text.strip())
            for segment in segments
            if segment.text.strip()
        ]
        if not result:
            raise LocalWhisperError("faster-whisper 没有生成有效句子")
        return result
